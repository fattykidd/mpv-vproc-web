"""The integration driver: ucapi entities wired to vproc."""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
from pathlib import Path
from typing import Any

import ucapi
from ucapi import button, select, sensor, switch

from . import entities as e
from .bridge import VprocBridge, VprocError

log = logging.getLogger("vproc.ucremote")

DRIVER_ID = "mpv_vproc_web"


def driver_metadata(version: str, port: int) -> dict[str, Any]:
    """The driver.json ucapi publishes to the remote."""
    return {
        "driver_id": DRIVER_ID,
        "version": version,
        "min_core_api": "0.20.0",
        "name": {"en": "mpv-vproc-web"},
        "description": {
            "en": "HDR video processor: choose picture presets and capture inputs, and see the input signal "
                  "and output mode."
        },
        "port": port,
        "developer": {"name": "mpv-vproc-web contributors", "url": "https://github.com/fattykidd/mpv-vproc-web"},
        "home_page": "https://github.com/fattykidd/mpv-vproc-web",
        "release_date": "2026-11-16",
        "setup_data_schema": {
            "title": {"en": "mpv-vproc-web"},
            "settings": [
                {
                    "id": "info",
                    "label": {"en": "Ready to connect"},
                    "field": {
                        "label": {
                            "value": {
                                "en": "No settings are needed. Finish the setup and choose the entities you want."
                            }
                        }
                    },
                }
            ],
        },
    }


class Driver:
    def __init__(self, api: ucapi.IntegrationAPI, bridge: VprocBridge):
        self.api = api
        self.bridge = bridge
        self._sent: dict[str, dict[str, Any]] = {}

        for entity in self._build_entities():
            api.available_entities.add(entity)

        api.add_listener(ucapi.Events.CONNECT, self._on_connect)

    # ------------------------------------------------------------------ entities

    def _build_entities(self) -> list[ucapi.Entity]:
        initial = e.attributes_for(None)
        return [
            ucapi.Select(e.PRESET, e.NAMES[e.PRESET], {**initial[e.PRESET], e.OPTIONS: [], e.CURRENT_OPTION: ""},
                         cmd_handler=self._on_preset),
            ucapi.Sensor(e.INPUT, e.NAMES[e.INPUT], [], {**initial[e.INPUT], e.VALUE: ""},
                         device_class=sensor.DeviceClasses.CUSTOM),
            ucapi.Sensor(e.OUTPUT, e.NAMES[e.OUTPUT], [], {**initial[e.OUTPUT], e.VALUE: ""},
                         device_class=sensor.DeviceClasses.CUSTOM),
            ucapi.Switch(e.AUTO_SWITCH, e.NAMES[e.AUTO_SWITCH],
                         [switch.Features.ON_OFF, switch.Features.TOGGLE], initial[e.AUTO_SWITCH],
                         device_class=switch.DeviceClasses.SWITCH, cmd_handler=self._on_auto_switch),
            ucapi.Button(e.SHOW_INFO, e.NAMES[e.SHOW_INFO], cmd_handler=self._on_button),
            ucapi.Button(e.STATS, e.NAMES[e.STATS], cmd_handler=self._on_button),
            ucapi.Button(e.RESTART_OUTPUT, e.NAMES[e.RESTART_OUTPUT], cmd_handler=self._on_button),
            ucapi.Sensor(e.DROPPED, e.NAMES[e.DROPPED], [], {**initial[e.DROPPED], e.VALUE: ""},
                         device_class=sensor.DeviceClasses.CUSTOM),
            ucapi.Sensor(e.LATENCY, e.NAMES[e.LATENCY], [], {**initial[e.LATENCY], e.VALUE: ""},
                         device_class=sensor.DeviceClasses.CUSTOM),
            ucapi.Select(e.CAPTURE_INPUT, e.NAMES[e.CAPTURE_INPUT],
                         {**initial[e.CAPTURE_INPUT], e.OPTIONS: [], e.CURRENT_OPTION: ""},
                         cmd_handler=self._on_capture_input),
        ]

    async def publish(self, status: dict[str, Any] | None) -> None:
        """Pushes changed attributes to the remote.

        ucapi shares one entity object between its available and configured lists,
        so updating both keeps the state the remote reads on subscribe current and
        sends an entity_change event for configured entities.
        """
        for entity_id, attributes in e.attributes_for(status).items():
            delta = e.changed(self._sent.get(entity_id), attributes)
            if not delta:
                continue
            self._sent[entity_id] = {**self._sent.get(entity_id, {}), **delta}
            self.api.available_entities.update_attributes(entity_id, delta)
            if self.api.configured_entities.contains(entity_id):
                self.api.configured_entities.update_attributes(entity_id, delta)

    # ------------------------------------------------------------------ remote events

    async def _on_connect(self) -> None:
        await self.api.set_device_state(ucapi.DeviceStates.CONNECTED)

    # ------------------------------------------------------------------ commands

    async def _send(self, method: str, path: str, body: dict[str, Any] | None = None) -> ucapi.StatusCodes:
        try:
            await self.bridge.call(method, path, body)
            return ucapi.StatusCodes.OK
        except VprocError as exc:
            log.warning("command failed: %s", exc)
            return ucapi.StatusCodes.SERVICE_UNAVAILABLE if exc.unreachable else ucapi.StatusCodes.BAD_REQUEST

    async def _on_preset(self, entity: ucapi.Entity, cmd_id: str, params: dict[str, Any] | None) -> ucapi.StatusCodes:
        current = self._sent.get(e.PRESET, {})
        options = current.get(e.OPTIONS, [])
        if cmd_id == select.Commands.SELECT_OPTION:
            name = (params or {}).get("option")
        else:
            name = e.step_option(options, current.get(e.CURRENT_OPTION, ""), cmd_id)
        if not name or name not in options:
            return ucapi.StatusCodes.BAD_REQUEST
        return await self._send("POST", f"/api/presets/{urllib.parse.quote(name, safe='')}/activate")

    async def _on_capture_input(self, entity: ucapi.Entity, cmd_id: str,
                                params: dict[str, Any] | None) -> ucapi.StatusCodes:
        current = self._sent.get(e.CAPTURE_INPUT, {})
        options = current.get(e.OPTIONS, [])
        if cmd_id == select.Commands.SELECT_OPTION:
            name = (params or {}).get("option")
        else:
            name = e.step_option(options, current.get(e.CURRENT_OPTION, ""), cmd_id)
        selector = e.input_selector(self.bridge.status or {}, name) if name in options else None
        if not selector:
            return ucapi.StatusCodes.BAD_REQUEST
        return await self._send("PUT", "/api/input", {"device": selector})

    async def _on_auto_switch(self, entity: ucapi.Entity, cmd_id: str, params: dict[str, Any] | None) -> ucapi.StatusCodes:
        if cmd_id == switch.Commands.ON:
            enabled = True
        elif cmd_id == switch.Commands.OFF:
            enabled = False
        elif cmd_id == switch.Commands.TOGGLE:
            enabled = self._sent.get(e.AUTO_SWITCH, {}).get(e.STATE) != e.ON
        else:
            return ucapi.StatusCodes.BAD_REQUEST
        return await self._send("PUT", "/api/auto-switch", {"enabled": enabled})

    async def _on_button(self, entity: ucapi.Entity, cmd_id: str, params: dict[str, Any] | None) -> ucapi.StatusCodes:
        if cmd_id != button.Commands.PUSH:
            return ucapi.StatusCodes.BAD_REQUEST
        if entity.id == e.SHOW_INFO:
            text = e.input_text(self.bridge.status or {})
            return await self._send("POST", "/api/osd", {"message": text, "duration_ms": 5000})
        if entity.id == e.STATS:
            return await self._send("POST", "/api/stats-overlay", {})
        if entity.id == e.RESTART_OUTPUT:
            return await self._send("POST", "/api/restart/output")
        return ucapi.StatusCodes.NOT_FOUND


async def _setup(request: ucapi.SetupDriver) -> ucapi.SetupAction:
    """There's nothing to configure: vproc is on the same machine."""
    if isinstance(request, ucapi.DriverSetupRequest):
        return ucapi.SetupComplete()
    return ucapi.SetupError()


async def run(*, version: str, port: int, vproc_url: str, token: str, config_dir: Path,
              ws_url: str | None = None) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    metadata = config_dir / "driver.json"
    metadata.write_text(json.dumps(driver_metadata(version, port), indent=2))

    loop = asyncio.get_running_loop()
    api = ucapi.IntegrationAPI(loop)
    bridge = VprocBridge(vproc_url, token, ws_url)
    driver = Driver(api, bridge)

    await api.init(str(metadata), _setup)
    await bridge.follow(driver.publish)
