"""HTTP API and web UI."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import secrets
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .config import OFFSET_MAX, OFFSET_MIN, Config, ConfigError
from .diagnostics import build_bundle, bundle_name, find_grab, keep_recent_log, list_grabs
from .mpv import MpvError
from .processor import Processor
from .service_settings import LIVE, ServiceSettings
from .settings import SCHEMA, PresetStore


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SettingsUpdate(Strict):
    values: dict[str, Any]
    preset: str | None = None


class PresetCreate(Strict):
    name: str
    copy_from: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)


class PresetRename(Strict):
    name: str


class AutoSwitch(Strict):
    auto: dict[str, str | None] = Field(default_factory=dict)
    enabled: bool | None = None


class OsdMessage(Strict):
    message: str = Field(min_length=1, max_length=200)
    duration_ms: int = Field(default=3000, ge=100, le=60000)


class IntegrationToggle(Strict):
    enabled: bool


class InputSelect(Strict):
    device: str | None = Field(max_length=40)


class FrameGrab(Strict):
    kind: Literal["display", "source"] = "display"


class LipSyncStep(Strict):
    step_ms: int = Field(ge=-100, le=100)


class ServiceSettingsUpdate(Strict):
    changes: dict[str, str | None] = Field(max_length=32)


class StatsToggle(Strict):
    page: int | None = Field(default=None, ge=0, le=9)


def create_app(
    config: Config,
    settings: ServiceSettings | None = None,
    request_restart: Callable[[], None] | None = None,
    startup_warning: str | None = None,
) -> FastAPI:
    """The web app. settings and request_restart come from __main__; without them the settings page is read-only."""
    keep_recent_log()
    store = PresetStore(config.data_dir / "presets.json")
    processor = Processor(config, store)
    settings = settings or ServiceSettings(config.data_dir)
    if startup_warning:
        processor.report("settings", startup_warning)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await processor.start()
        try:
            yield
        finally:
            await processor.stop()

    app = FastAPI(title="vproc", version="1.0.0", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.state.processor = processor

    def token_ok(supplied: str | None) -> bool:
        return not config.api_token or (
            supplied is not None and secrets.compare_digest(supplied.encode(), config.api_token.encode())
        )

    async def require_token(request: Request) -> None:
        header = request.headers.get("authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else None
        if not token_ok(supplied):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or wrong API token",
                                headers={"WWW-Authenticate": "Bearer"})

    @app.exception_handler(ValueError)
    async def bad_value(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(KeyError)
    async def not_found(_: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse({"detail": f"not found: {exc.args[0] if exc.args else ''}"}, status_code=404)

    @app.exception_handler(MpvError)
    async def output_down(_: Request, exc: MpvError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=503)

    # ------------------------------------------------------------- UI & health

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(config.web_dir / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/assets", StaticFiles(directory=config.web_dir), name="assets")

    @app.middleware("http")
    async def revalidate_assets(request: Request, call_next):
        # Without this, browsers keep using an old app.js/style.css after an update.
        # Revalidating is cheap: unchanged files get a 304 thanks to their ETag.
        response = await call_next(request)
        if request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, Any]:
        s = processor.status()
        return {"ok": True, "output": s["output"]["running"], "capture": s["capture"]["running"]}

    # ------------------------------------------------------------------- API

    api = [Depends(require_token)]

    @app.get("/api/status", dependencies=api)
    async def get_status() -> dict[str, Any]:
        return processor.status()

    @app.get("/api/schema", dependencies=api)
    async def get_schema() -> list[dict[str, Any]]:
        return [o.to_dict() for o in SCHEMA]

    @app.get("/api/presets", dependencies=api)
    async def get_presets() -> dict[str, Any]:
        return store.state.to_dict()

    @app.patch("/api/settings", dependencies=api)
    async def patch_settings(body: SettingsUpdate) -> dict[str, Any]:
        applied = await processor.update_settings(body.values, body.preset)
        return {"applied": applied}

    @app.delete("/api/settings/{option}", dependencies=api)
    async def reset_setting(option: str, preset: str | None = None) -> dict[str, Any]:
        await processor.reset_setting(option, preset)
        return {"reset": option}

    @app.post("/api/presets", dependencies=api, status_code=201)
    async def create_preset(body: PresetCreate) -> dict[str, Any]:
        name = store.create(body.name, body.values, body.copy_from)
        await processor.presets_changed()
        return {"created": name}

    @app.post("/api/presets/{name}/activate", dependencies=api)
    async def activate_preset(name: str) -> dict[str, Any]:
        await processor.activate_preset(name)
        return {"active": name}

    @app.patch("/api/presets/{name}", dependencies=api)
    async def rename_preset(name: str, body: PresetRename) -> dict[str, Any]:
        new = store.rename(name, body.name)
        await processor.presets_changed()
        return {"renamed": new}

    @app.delete("/api/presets/{name}", dependencies=api)
    async def delete_preset(name: str) -> dict[str, Any]:
        store.delete(name)
        await processor.presets_changed()
        return {"deleted": name}

    @app.put("/api/auto-switch", dependencies=api)
    async def set_auto(body: AutoSwitch) -> dict[str, Any]:
        store.set_auto(body.auto, body.enabled)
        await processor.presets_changed()
        return {"auto": store.state.auto, "enabled": store.state.auto_switch}

    @app.post("/api/osd", dependencies=api)
    async def osd(body: OsdMessage) -> dict[str, Any]:
        await processor.show_message(body.message, body.duration_ms)
        return {"shown": body.message}

    @app.post("/api/stats-overlay", dependencies=api)
    async def stats_overlay(body: StatsToggle) -> dict[str, Any]:
        await processor.toggle_stats(body.page)
        return {"ok": True}

    @app.post("/api/frame-grab", dependencies=api)
    async def frame_grab(body: FrameGrab) -> dict[str, Any]:
        path = await processor.grab_frame(body.kind)
        return {"name": path.name, "kind": body.kind}

    @app.get("/api/frame-grabs", dependencies=api)
    async def get_frame_grabs() -> dict[str, Any]:
        return {"grabs": [p.name for p in list_grabs(config.data_dir)]}

    @app.get("/api/frame-grabs/{name}", dependencies=api)
    async def get_frame_grab(name: str) -> FileResponse:
        return FileResponse(find_grab(config.data_dir, name), media_type="image/png", filename=name)

    @app.get("/api/diagnostics", dependencies=api)
    async def diagnostics() -> Response:
        page = settings.describe(config)
        data = await asyncio.to_thread(build_bundle, processor.status(), config, page)
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{bundle_name()}"'})

    @app.post("/api/restart/{target}", dependencies=api)
    async def restart(target: Literal["capture", "output"]) -> dict[str, Any]:
        await (processor.restart_capture() if target == "capture" else processor.restart_output())
        return {"restarted": target}

    @app.get("/api/service-settings", dependencies=api)
    async def get_service_settings() -> dict[str, Any]:
        return {**settings.describe(config), "can_restart": request_restart is not None}

    async def save_service_settings(changes: dict[str, str | None]) -> None:
        """Saves changes, and puts the ones that need no restart into effect."""
        nonlocal config
        try:
            settings.update(changes)
        except ConfigError as exc:
            raise HTTPException(400, str(exc)) from None
        saved, _ = settings.config()
        live = {f.attr: getattr(saved, f.attr) for f in LIVE if getattr(saved, f.attr) != getattr(config, f.attr)}
        if live:
            config = dataclasses.replace(config, **live)
            await processor.update_lipsync(config.lipsync_urls, config.lipsync_offset, config.lipsync_method)

    @app.put("/api/service-settings", dependencies=api)
    async def update_service_settings(body: ServiceSettingsUpdate) -> dict[str, Any]:
        await save_service_settings(body.changes)
        return {**settings.describe(config), "can_restart": request_restart is not None}

    @app.post("/api/lipsync/offset", dependencies=api)
    async def step_lipsync_offset(body: LipSyncStep) -> dict[str, Any]:
        """Moves the lip-sync offset by a few ms, e.g. from a remote's buttons, and saves it."""
        offset = max(OFFSET_MIN, min(OFFSET_MAX, config.lipsync_offset + body.step_ms))
        await save_service_settings({"LIPSYNC_OFFSET": str(offset)})
        return processor.lipsync.status()

    @app.post("/api/service-settings/apply", dependencies=api)
    async def apply_service_settings() -> dict[str, Any]:
        if request_restart is None:
            raise HTTPException(409, "restarting is only possible when vproc runs as a service")
        asyncio.get_running_loop().call_later(0.3, request_restart)  # after this response is sent
        return {"restarting": True}

    @app.put("/api/input", dependencies=api)
    async def select_input(body: InputSelect) -> dict[str, Any]:
        await processor.select_input(body.device)
        return {"selected": processor.inputs.device, "selected_in": "web" if processor.inputs.chosen else "env"}

    @app.put("/api/integrations/uc-remote", dependencies=api)
    async def toggle_uc_remote(body: IntegrationToggle) -> dict[str, Any]:
        await processor.remote.set_enabled(body.enabled)
        return processor.remote.status()

    @app.websocket("/api/ws")
    async def ws(socket: WebSocket) -> None:
        if not token_ok(socket.query_params.get("token")):
            await socket.close(code=4401)
            return
        await socket.accept()
        queue = processor.subscribe()
        try:
            await socket.send_json(processor.status())
            while True:
                # Wake up regularly so a dead client is noticed even when idle.
                try:
                    snapshot = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    snapshot = processor.status()
                await socket.send_json(snapshot)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            processor.unsubscribe(queue)

    return app
