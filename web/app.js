"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, props = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null) continue;
    if (key.includes("-")) node.setAttribute(key, value);
    else node[key] = value;
  }
  for (const child of children) if (child != null) node.append(child);
  return node;
};

// Gives a label a description that shows on hover, or on a tap on touch screens
// (a tapped label takes focus, and the description shows while it has it).
const tip = (node, text) => {
  if (!text) return node;
  node.classList.add("tip");
  node.dataset.tip = text;
  node.tabIndex = 0;
  node.setAttribute("aria-description", text);
  return node;
};

// What each status row means, shown on hover or tap.
const STATUS_TIPS = {
  "Format": "The resolution and scan type of the signal arriving at the capture card.",
  "Frame rate": "The frame rate the source says it is sending.",
  "Encoding": "How the signal carries colour (RGB or YCbCr 4:2:2) and its bit depth.",
  "MaxCLL / FALL": "From the HDR metadata: the brightest pixel in the film, and its brightest frame on average.",
  "Mastering": "The brightness range of the display the film was graded on.",
  "State": "Playing: the picture is on screen. Ready: the output is up and waiting for a signal.",
  "Mode": "The resolution and refresh rate the display is driven at.",
  "Refresh": "The display's refresh rate.",
  "Rendering": "The rate frames are handed to the renderer.",
  "Target": "What the picture is prepared for: the display's gamut, gamma and peak brightness.",
  "Dropped": "Frames the renderer threw away because it fell behind. Each one is a visible stutter; it should stay at 0.",
  "Latency": "vproc's own delay from the capture card to the display, averaged over half a minute. "
    + "The display's own processing comes on top.",
  "Audio delay": "The delay last sent to the AV processor: vproc's latency plus your lip-sync offset.",
  "HDMI link": "Whether the display reports a locked HDMI signal. vproc restarts the output if it's lost.",
  "Device": "The capture input in use.",
  "Driver": "The Blackmagic Desktop Video driver version.",
  "Frames": "Frames handed to the renderer, out of everything the card captured.",
  "Skipped": "Frames skipped during playback because the renderer fell behind. Each one is a visible stutter.",
  "Skipped at start": "Frames that arrived while the output was still starting up. Expected, and never seen.",
  "Missed by card": "Frames the source sent that the card never delivered. Each one is a visible stutter.",
  "Source clock": "How far the source's real frame rate is from its nominal rate, in parts per million, "
    + "measured against the server's clock. Shown after a minute of playback.",
};

const KIND_LABEL = { sdr: "SDR", hdr10: "HDR10", hlg: "HLG" };
const state = {
  token: localStorage.getItem("vproc-token") || "",
  schema: [],
  status: null,
  controls: new Map(),   // option name -> { row, set(value) }
  tab: localStorage.getItem("vproc-tab") || null,
  presetsKey: "",
  inputsKey: "",
};

// ------------------------------------------------------------------ helpers

function toast(message, isError = false) {
  const node = el("div", { className: "toast" + (isError ? " error" : ""), textContent: message });
  $("#toasts").append(node);
  setTimeout(() => node.remove(), isError ? 6000 : 2500);
}

function ask(label, { value = "", input = true, ok = "OK" } = {}) {
  const dialog = $("#prompt");
  const field = $("#prompt-input");
  $("#prompt-label").textContent = label;
  $("#prompt-ok").textContent = ok;
  field.hidden = !input;
  field.value = value;
  dialog.showModal();
  if (input) field.select();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => {
      resolve(dialog.returnValue === "ok" ? (input ? field.value.trim() : true) : null);
    }, { once: true });
  });
}

async function api(method, path, body) {
  const headers = { "Content-Type": "application/json" };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (res.status === 401) {
    const token = await ask("This processor needs an API token.", { ok: "Unlock" });
    if (token) {
      state.token = token;
      localStorage.setItem("vproc-token", token);
      connect();
      return api(method, path, body);
    }
    throw new Error("API token required");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join("; ") : data.detail;
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return data;
}

// Fetches a file from the API (with the token) and hands it to the browser as a download.
async function download(path) {
  const headers = state.token ? { Authorization: `Bearer ${state.token}` } : {};
  const res = await fetch(path, { headers });
  if (res.status === 401) {
    await api("GET", "/api/status");  // asks for the token, then try again
    return download(path);
  }
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `${res.status} ${res.statusText}`);
  }
  const name = /filename="?([^";]+)"?/.exec(res.headers.get("Content-Disposition") || "")?.[1] || path.split("/").pop();
  const url = URL.createObjectURL(await res.blob());
  const link = el("a", { href: url, download: name });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

async function run(action, success) {
  try {
    await action();
    if (success) toast(success);
  } catch (err) {
    toast(err.message, true);
  }
}

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

function fmt(value, digits = 3) {
  if (value == null) return "—";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(digits).replace(/\.?0+$/, "");
  return String(value);
}

function facts(target, rows, tips = {}) {
  target.replaceChildren(...rows.filter(Boolean).flatMap(([k, v, bad]) => [
    tip(el("dt", { textContent: k }), tips[k]),
    el("dd", { textContent: v ?? "—", className: bad ? "bad" : "" }),
  ]));
}

// ------------------------------------------------------------------ status

function renderStatus(s) {
  state.status = s;
  const sig = s.signal;
  const pill = $("#signal-pill");
  if (sig.segment) {
    const kind = sig.colorimetry?.kind ?? "sdr";
    pill.dataset.kind = kind;
    pill.textContent = sig.colorimetry?.lldv ? "DOLBY VISION" : KIND_LABEL[kind];
    $("#signal-label").textContent = sig.label;
  } else {
    pill.dataset.kind = "none";
    pill.textContent = sig.present === false ? "NO SIGNAL" : "IDLE";
    $("#signal-label").textContent = s.capture.running ? "Waiting for a source" : "Capture is not running";
  }

  const seg = sig.segment;
  const c = sig.colorimetry;
  facts($("#signal-facts"), seg ? [
    ["Format", `${seg.width}×${seg.height} ${seg.field === "progressive" ? "progressive" : seg.field}`],
    ["Frame rate", `${fmt(seg.fps)} fps`],
    ["Encoding", `${seg.signal === "rgb444" ? "RGB 4:4:4" : "YCbCr 4:2:2"}, ${seg.bit_depth}-bit (${seg.pixel_format})`],
    c && c.kind !== "sdr" && ["MaxCLL / FALL", `${fmt(c.max_cll)} / ${fmt(c.max_fall)} nits`],
    c?.mastering && ["Mastering", `${fmt(c.mastering.min_luminance, 4)}–${fmt(c.mastering.max_luminance)} nits`],
  ] : [], STATUS_TIPS);

  const o = s.output;
  const target = o.target_params || {};
  const drops = (o.dropped_frames ?? 0) + (o.decoder_dropped_frames ?? 0);
  facts($("#output-facts"), [
    ["State", o.playing ? "Playing" : o.running ? "Ready" : "Stopped", !o.running],
    o.display_mode && ["Mode", o.display_mode],
    ["Refresh", o.display_fps ? `${fmt(o.display_fps)} Hz` : null],
    ["Rendering", o.estimated_fps ? `${fmt(o.estimated_fps, 2)} fps` : null],
    ["Target", target.primaries ? `${target.primaries} · ${target.gamma}${target["max-luma"] ? ` · ${fmt(target["max-luma"], 0)} nits` : ""}` : null],
    ["Dropped", String(drops), drops > 0],
    o.latency_ms != null && ["Latency", `≈ ${o.latency_ms} ms (vproc only)`],
    s.lipsync?.enabled && ["Audio delay", s.lipsync.error ? "Failed (see problems)"
      : s.lipsync.delay_ms != null ? `${s.lipsync.delay_ms} ms` : "Waiting for a signal", Boolean(s.lipsync.error)],
    o.hdmi_link?.watched && ["HDMI link",
      o.hdmi_link.locked == null ? "No answer" : o.hdmi_link.locked ? "Locked" : "Not locked", o.hdmi_link.locked === false],
  ], STATUS_TIPS);

  const cap = s.capture;
  const st = cap.stats || {};
  const skipped = cap.skipped || {};
  const missed = cap.missed ?? 0;
  const counting = st.captured != null;
  facts($("#capture-facts"), [
    ["Device", cap.device?.name ?? (cap.running ? "…" : "Not running"), !cap.running],
    ["Driver", cap.driver],
    ["Frames", counting ? `${st.written} shown of ${st.captured}` : null],
    ["Skipped", counting ? String(skipped.playback ?? 0) : null, skipped.playback > 0],
    counting && skipped.startup > 0 && ["Skipped at start", String(skipped.startup)],
    ["Missed by card", counting ? String(missed) : null, missed > 0],
    cap.source_clock && ["Source clock", sourceClockText(cap.source_clock)],
  ], STATUS_TIPS);

  const errors = s.errors || [];
  $("#errors").hidden = errors.length === 0;
  $("#error-list").replaceChildren(...errors.slice(0, 6).map((e) => el("li", {},
    el("time", { textContent: new Date(e.time * 1000).toLocaleTimeString() }),
    el("span", { className: "src", textContent: e.source }),
    el("span", { textContent: e.message }),
  )));

  renderInputs(cap);
  renderPresets(s.presets);
  renderIntegrations(s.integrations || {});
}

function sourceClockText(clock) {
  if (clock.ppm == null) return `Measuring… (${clock.measured_for} s)`;
  const sign = clock.ppm > 0 ? "+" : clock.ppm < 0 ? "−" : "±";
  return `${sign}${Math.abs(clock.ppm).toFixed(1)} ppm · ${clock.fps.toFixed(4)} fps`;
}

// ------------------------------------------------------------------ integrations

function renderIntegrations(integrations) {
  const uc = integrations.uc_remote;
  if (!uc) return;
  const toggle = $("#uc-remote-enabled");
  if (!toggle.disabled) toggle.checked = uc.enabled;
  $("#uc-remote-state").textContent = !uc.enabled ? "Off" : uc.running ? `On · port ${uc.port}` : "Starting…";
  const error = $("#uc-remote-error");
  error.hidden = !uc.error;
  error.textContent = uc.error || "";
}

// The value sent back for an input: its persistent id where the card has one, else its position.
const inputSpec = (d) => (d.persistent_id >= 0 ? `id:${d.persistent_id}` : String(d.index));

function renderInputs(cap) {
  const select = $("#input-select");
  const inputs = (cap.devices || []).filter((d) => d.can_capture);
  select.hidden = inputs.length < 2;
  if (document.activeElement === select) return;
  const key = JSON.stringify([inputs, cap.selected, cap.device?.index]);
  if (key === state.inputsKey) return;
  state.inputsKey = key;

  // What's open now; failing that (e.g. the chosen input is missing), what was asked for.
  const current = inputs.find((d) => d.index === cap.device?.index)
    ?? inputs.find((d) => inputSpec(d) === cap.selected || String(d.index) === cap.selected);
  select.replaceChildren(
    ...(current ? [] : [el("option", { value: "", textContent: "Choose an input", disabled: true })]),
    ...inputs.map((d) => el("option", { value: inputSpec(d), textContent: d.name })),
  );
  select.value = current ? inputSpec(current) : "";
}

function bindInputs() {
  const select = $("#input-select");
  select.addEventListener("change", () => {
    const name = select.selectedOptions[0]?.textContent;
    run(() => api("PUT", "/api/input", { device: select.value }), `Switching to ${name}`);
    select.blur();
  });
}

function bindIntegrations() {
  const toggle = $("#uc-remote-enabled");
  toggle.addEventListener("change", async () => {
    const enabled = toggle.checked;
    toggle.disabled = true;
    try {
      await api("PUT", "/api/integrations/uc-remote", { enabled });
      toast(enabled ? "Remote integration on" : "Remote integration off");
    } catch (err) {
      toggle.checked = !enabled;
      toast(err.message, true);
    } finally {
      toggle.disabled = false;
    }
  });
}

// ------------------------------------------------------------------ presets

function renderPresets(p) {
  const key = JSON.stringify(p);
  if (key === state.presetsKey) return;
  state.presetsKey = key;
  const names = Object.keys(p.presets);

  const select = $("#preset-select");
  if (document.activeElement !== select) {
    select.replaceChildren(...names.map((n) => el("option", { value: n, textContent: n })));
    select.value = p.active;
  }
  $("#auto-enabled").checked = p.auto_switch;

  const map = $("#auto-map");
  map.replaceChildren(...["sdr", "hdr10", "hlg"].map((kind) => {
    const s = el("select", { "aria-label": `Preset for ${KIND_LABEL[kind]}` },
      el("option", { value: "", textContent: "Keep current" }),
      ...names.map((n) => el("option", { value: n, textContent: n })));
    s.value = p.auto[kind] ?? "";
    s.disabled = !p.auto_switch;
    s.addEventListener("change", () => run(() => api("PUT", "/api/auto-switch", { auto: { [kind]: s.value || null } })));
    return el("label", {}, KIND_LABEL[kind], s);
  }));

  const values = p.presets[p.active] || {};
  for (const [name, control] of state.controls) control.set(values[name]);
}

function bindPresetControls() {
  $("#preset-select").addEventListener("change", (e) =>
    run(() => api("POST", `/api/presets/${encodeURIComponent(e.target.value)}/activate`), `Preset “${e.target.value}”`));

  $("#auto-enabled").addEventListener("change", (e) =>
    run(() => api("PUT", "/api/auto-switch", { enabled: e.target.checked })));

  $("#preset-new").addEventListener("click", async () => {
    const active = state.status?.presets.active;
    const name = await ask(`New preset (starts as a copy of “${active}”)`, { ok: "Create" });
    if (!name) return;
    run(async () => {
      await api("POST", "/api/presets", { name, copy_from: active });
      await api("POST", `/api/presets/${encodeURIComponent(name)}/activate`);
    }, `Created “${name}”`);
  });

  $("#preset-rename").addEventListener("click", async () => {
    const active = state.status?.presets.active;
    const name = await ask("Rename preset", { value: active, ok: "Rename" });
    if (!name || name === active) return;
    run(() => api("PATCH", `/api/presets/${encodeURIComponent(active)}`, { name }), "Renamed");
  });

  $("#preset-delete").addEventListener("click", async () => {
    const active = state.status?.presets.active;
    if (!(await ask(`Delete the preset “${active}”?`, { input: false, ok: "Delete" }))) return;
    run(() => api("DELETE", `/api/presets/${encodeURIComponent(active)}`), "Deleted");
  });
}

// ------------------------------------------------------------------ settings

function sendSetting(name, value) {
  return run(() => api("PATCH", "/api/settings", { values: { [name]: value } }));
}

function numberControl(opt, row) {
  const wrap = el("div", { className: "control" });
  const number = el("input", { type: "number", min: opt.minimum, max: opt.maximum, step: opt.step ?? "any", placeholder: "default" });
  const span = (opt.maximum - opt.minimum) / (opt.step || 1);
  const range = span <= 5000 ? el("input", { type: "range", min: opt.minimum, max: opt.maximum, step: opt.step ?? "any" }) : null;
  if (range) range.value = (opt.minimum + opt.maximum) / 2;  // neutral until a value is set
  const chips = (opt.keywords || []).map((k) => el("button", { type: "button", className: "chip", textContent: k }));
  number.setAttribute("aria-label", opt.label);
  range?.setAttribute("aria-label", opt.label);

  const push = debounce((v) => sendSetting(opt.name, v), 150);
  const fromInput = (raw) => {
    if (raw === "") return;
    const v = Number(raw);
    if (Number.isNaN(v)) return;
    number.value = raw;
    if (range) range.value = raw;
    chips.forEach((c) => c.setAttribute("aria-pressed", "false"));
    row.classList.add("overridden");
    push(v);
  };
  range?.addEventListener("input", () => fromInput(range.value));
  number.addEventListener("change", () => fromInput(number.value));
  for (const chip of chips) {
    chip.addEventListener("click", () => {
      chips.forEach((c) => c.setAttribute("aria-pressed", String(c === chip)));
      number.value = "";
      row.classList.add("overridden");
      sendSetting(opt.name, chip.textContent);
    });
  }
  if (range) wrap.append(range);
  wrap.append(number, ...chips);

  return {
    node: wrap,
    set(value) {
      const isKeyword = typeof value === "string" && (opt.keywords || []).includes(value);
      chips.forEach((c) => c.setAttribute("aria-pressed", String(isKeyword && c.textContent === value)));
      if (document.activeElement === number || document.activeElement === range) return;
      number.value = typeof value === "number" ? value : "";
      if (range && typeof value === "number") range.value = value;
    },
  };
}

function choiceControl(opt) {
  const select = el("select", { "aria-label": opt.label },
    el("option", { value: "", textContent: "default", disabled: true }),
    ...opt.choices.map((c) => el("option", { value: c, textContent: c })));
  select.addEventListener("change", () => sendSetting(opt.name, select.value));
  return {
    node: el("div", { className: "control" }, select),
    set(value) { if (document.activeElement !== select) select.value = value ?? ""; },
  };
}

function toggleControl(opt) {
  const input = el("input", { type: "checkbox" });
  input.addEventListener("change", () => sendSetting(opt.name, input.checked));
  const label = el("label", { className: "switch" }, input, el("span", { className: "track" }));
  input.setAttribute("aria-label", opt.label);
  return { node: el("div", { className: "control" }, label), set(value) { input.checked = value === true; } };
}

function buildSettings() {
  const groups = [...new Set(state.schema.map((o) => o.group))];
  if (!groups.includes(state.tab)) state.tab = groups[0];
  const panel = $("#settings");
  const tabs = $("#tabs");

  const show = (group) => {
    state.tab = group;
    localStorage.setItem("vproc-tab", group);
    for (const b of tabs.children) b.setAttribute("aria-selected", String(b.textContent === group));
    for (const { row, group: g } of state.controls.values()) row.hidden = g !== group;
  };

  tabs.replaceChildren(...groups.map((g) => {
    const b = el("button", { type: "button", textContent: g, role: "tab" });
    b.addEventListener("click", () => show(g));
    return b;
  }));

  panel.replaceChildren();
  for (const opt of state.schema) {
    const row = el("div", { className: "setting" });
    const control = opt.kind === "number" ? numberControl(opt, row)
      : opt.kind === "toggle" ? toggleControl(opt) : choiceControl(opt);
    const reset = el("button", { type: "button", className: "ghost small reset", textContent: "Reset", title: "Use mpv's default" });
    reset.addEventListener("click", () => run(() => api("DELETE", `/api/settings/${encodeURIComponent(opt.name)}`)));
    row.append(
      el("div", {},
        tip(el("div", { className: "name", textContent: opt.label }), opt.about),
        opt.help ? el("div", { className: "help", textContent: opt.help }) : null),
      control.node,
      reset,
    );
    panel.append(row);
    state.controls.set(opt.name, {
      row,
      group: opt.group,
      set(value) {
        row.classList.toggle("overridden", value !== undefined);
        control.set(value);
      },
    });
  }
  show(state.tab);
}

// ------------------------------------------------------------------ system settings

const SOURCE_LABEL = { saved: "Set here", env: "From .env", default: "Default" };

function serviceControl(field, save) {
  if (field.kind === "toggle") {
    const box = el("input", { type: "checkbox", checked: field.value === "true" });
    box.addEventListener("change", () => save(box.checked ? "true" : "false"));
    return el("label", { className: "switch" }, box, el("span", { className: "track", "aria-hidden": "true" }));
  }
  if (field.kind === "choice") {
    const select = el("select", {}, ...field.choices.map((c) => el("option", { value: c, textContent: c })));
    select.value = field.value;
    select.addEventListener("change", () => save(select.value));
    return select;
  }
  const input = el("input", {
    type: field.kind === "number" ? "number" : "text",
    value: field.value, placeholder: field.placeholder || "", step: field.kind === "number" ? "any" : undefined,
  });
  input.addEventListener("change", () => save(input.value));
  return input;
}

function renderService(data) {
  state.service = data;
  const box = $("#service-fields");
  const groups = [...new Set(data.fields.map((f) => f.group))];
  box.replaceChildren(...groups.flatMap((group) => [
    el("h3", { textContent: group }),
    ...data.fields.filter((f) => f.group === group).map((field) => {
      const save = (value) => run(async () => renderService(
        await api("PUT", "/api/service-settings", { changes: { [field.key]: value } })), field.live ? "Saved and applied" : "Saved")
        .then(() => renderService(state.service));  // on error, put the field back
      const reset = el("button", { type: "button", className: "ghost small reset", textContent: "Reset",
        title: field.env_value != null ? `Back to .env: ${field.env_value}` : "Back to the default" });
      reset.addEventListener("click", () => save(null));
      return el("div", { className: "setting" + (field.source === "saved" ? " overridden" : "") },
        el("div", {},
          tip(el("div", { className: "name", textContent: field.label }), `In .env: VPROC_${field.key}`),
          el("div", { className: "help", textContent: field.help }),
          el("div", { className: "source", textContent: SOURCE_LABEL[field.source]
            + (field.value !== field.running ? ` · running with ${field.running || "(empty)"}` : "") })),
        serviceControl(field, save),
        reset);
    }),
  ]));
  facts($("#service-host"), data.host_only.map((h) => [h.name, h.help]));

  const apply = $("#service-apply");
  apply.disabled = !data.restart_needed || !data.can_restart;
  $("#service-status").textContent = !data.can_restart ? "Apply is unavailable when vproc runs outside its service."
    : data.restart_needed ? "Saved. Press Apply to use the new settings." : "";
}

function bindService() {
  $("#service").addEventListener("toggle", (e) => {
    if (e.target.open) run(async () => renderService(await api("GET", "/api/service-settings")));
  }, { once: false });
  $("#service-apply").addEventListener("click", async () => {
    const cautions = state.service.fields.filter((f) => f.caution && f.value !== f.running).map((f) => `${f.label}: ${f.caution}`);
    if (cautions.length && !(await ask(`${cautions.join("\n")}\n\nApply anyway?`, { input: false, ok: "Apply" }))) return;
    await run(() => api("POST", "/api/service-settings/apply"), "Restarting vproc…");
    state.reloadService = true;  // read the settings again once the connection is back
  });
}

// ------------------------------------------------------------------ tools

function bindTools() {
  $("#osd-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const message = $("#osd-text").value.trim();
    if (message) run(() => api("POST", "/api/osd", { message, duration_ms: 4000 }), "Message shown");
  });
  const actions = {
    stats: () => run(() => api("POST", "/api/stats-overlay", {})),
    "restart-output": () => run(() => api("POST", "/api/restart/output"), "Restarting output"),
    "restart-capture": () => run(() => api("POST", "/api/restart/capture"), "Restarting capture"),
    grab: () => run(async () => {
      const grab = await api("POST", "/api/frame-grab", { kind: $("#grab-kind").value });
      await download(`/api/frame-grabs/${encodeURIComponent(grab.name)}`);
    }, "Frame grabbed"),
    diagnostics: () => run(() => download("/api/diagnostics"), "Diagnostics downloaded"),
  };
  for (const b of document.querySelectorAll("[data-action]")) b.addEventListener("click", actions[b.dataset.action]);
}

// ------------------------------------------------------------------ live updates

let socket;
let retry = 1000;

function connect() {
  socket?.close();
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const query = state.token ? `?token=${encodeURIComponent(state.token)}` : "";
  const ws = new WebSocket(`${proto}://${location.host}/api/ws${query}`);
  socket = ws;
  const conn = $("#conn");
  ws.onopen = () => {
    retry = 1000; conn.dataset.state = "live"; conn.textContent = "Live";
    if (state.reloadService) {
      state.reloadService = false;
      run(async () => renderService(await api("GET", "/api/service-settings")), "vproc restarted with the new settings");
    }
  };
  ws.onmessage = (e) => renderStatus(JSON.parse(e.data));
  ws.onclose = (e) => {
    if (socket !== ws) return;
    conn.dataset.state = "down";
    if (e.code === 4401) {
      conn.textContent = "Locked";
      api("GET", "/api/status").catch(() => {});  // triggers the token prompt
      return;
    }
    conn.textContent = "Reconnecting…";
    setTimeout(connect, retry);
    retry = Math.min(retry * 2, 10000);
  };
}

async function init() {
  bindPresetControls();
  bindInputs();
  bindIntegrations();
  bindService();
  bindTools();
  try {
    state.schema = await api("GET", "/api/schema");
    buildSettings();
    renderStatus(await api("GET", "/api/status"));
  } catch (err) {
    toast(err.message, true);
  }
  connect();
}

init();
