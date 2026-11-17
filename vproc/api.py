"""HTTP API and web UI."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .config import Config
from .mpv import MpvError
from .processor import Processor
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


class StatsToggle(Strict):
    page: int | None = Field(default=None, ge=0, le=9)


def create_app(config: Config) -> FastAPI:
    store = PresetStore(config.data_dir / "presets.json")
    processor = Processor(config, store)

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

    @app.post("/api/restart/{target}", dependencies=api)
    async def restart(target: Literal["capture", "output"]) -> dict[str, Any]:
        await (processor.restart_capture() if target == "capture" else processor.restart_output())
        return {"restarted": target}

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
