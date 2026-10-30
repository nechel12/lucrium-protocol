"""Рантайм плагина: подключение к воркеру и диспетчеризация контрактных хуков.

Жизненный цикл (контракт ``api = "1"``):

1. воркер запускает subprocess плагина (``python -m lucrium_plugin_sdk``),
   передавая ``LUCRIUM_PLUGIN_DIR`` и ``LUCRIUM_PLUGIN_SOCKET``;
2. рантайм импортирует точку входа из манифеста, валидирует хуки;
3. host шлёт ``plugin.start`` (конфиг) → ``on_start``;
4. далее поток ``plugin.event`` → ``on_event`` (контекст привязан к сессии события);
5. ``plugin.stop`` → ``on_stop`` → выход.

Исключение в ``on_start`` → ненулевой код выхода (воркер рестартует с backoff);
исключения в ``on_event``/``on_stop`` логируются и не роняют процесс — крах
одного события не должен убивать плагин (спека §6.3).
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import traceback
from pathlib import Path
from types import ModuleType
from typing import Any

from lucrium_protocol.ipc import (
    PLUGIN_EVENT,
    PLUGIN_START,
    PLUGIN_STOP,
    EventParams,
    EventResult,
    RpcError,
    RpcErrorCode,
    RpcNotification,
    RpcRequest,
    StartParams,
    StartResult,
    StopParams,
    StopResult,
)
from lucrium_protocol.manifest import PluginManifest

from .context import ConfigView, PluginContext, RpcHttp, RpcLog, RpcSession, RpcStorage
from .entry import EntryError, PluginEntry, discover_entry
from .events import PluginEvent
from .manifest import load_manifest
from .transport import JsonRpcClient, TransportError

PLUGIN_DIR_ENV = "LUCRIUM_PLUGIN_DIR"
PLUGIN_SOCKET_ENV = "LUCRIUM_PLUGIN_SOCKET"


def import_entry_module(plugin_dir: Path, dotted: str) -> ModuleType:
    """Импортирует модуль-точку входа (``manifest.plugin.entry``) из корня плагина."""
    root = str(plugin_dir.resolve())
    if root not in sys.path:
        # sys.path оставляем: модуль точки входа может импортировать соседние файлы плагина
        sys.path.insert(0, root)
    try:
        return importlib.import_module(dotted)
    except Exception as exc:
        raise EntryError(f"cannot import entry module {dotted!r}: {exc}") from exc


def load_entry(plugin_dir: str | Path) -> tuple[PluginManifest, PluginEntry]:
    """Манифест + валидная точка входа плагина."""
    directory = Path(plugin_dir)
    manifest = load_manifest(directory)
    module = import_entry_module(directory, manifest.entry)
    entry = discover_entry(module).ensure_valid()
    return manifest, entry


class PluginRuntime:
    """Диспетчер хуков поверх одного IPC-соединения с воркером."""

    def __init__(
        self,
        manifest: PluginManifest,
        entry: PluginEntry,
        client: JsonRpcClient,
        *,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._manifest = manifest
        self._entry = entry
        self._client = client
        self._config = ConfigView(config or {})
        self._started = False

    # --- контекст -------------------------------------------------------------

    def make_context(self, session_id: str | None = None) -> PluginContext:
        return PluginContext(
            plugin_id=self._manifest.id,
            config=self._config,
            session=RpcSession(self._client, session_id),
            storage=RpcStorage(self._client),
            log=RpcLog(self._client, self._manifest.id),
            http=RpcHttp(self._client),
            session_id=session_id,
        )

    # --- цикл -----------------------------------------------------------------

    async def run(self) -> None:
        """Ждёт ``plugin.start``, обрабатывает события, завершается на ``plugin.stop``."""
        await self._wait_start()
        while True:
            message = await self._client.recv()
            if isinstance(message, RpcNotification):
                if message.method == PLUGIN_EVENT:
                    await self._on_event_params(message.params or {}, respond_to=None)
                # прочие уведомления host игнорируем (forward-compatible)
                continue

            request = message
            if request.method == PLUGIN_EVENT:
                await self._on_event_params(request.params or {}, respond_to=request)
            elif request.method == PLUGIN_STOP:
                await self._stop(StopParams.model_validate(request.params or {}))
                await self._client.respond(request, result=StopResult().model_dump(mode="json"))
                return
            elif request.method == PLUGIN_START:
                await self._client.respond(
                    request,
                    error=RpcError(code=RpcErrorCode.INVALID_REQUEST, message="plugin already started"),
                )
            else:
                await self._client.respond(
                    request,
                    error=RpcError(
                        code=RpcErrorCode.METHOD_NOT_FOUND,
                        message=f"unknown method {request.method!r}",
                    ),
                )

    async def _wait_start(self) -> None:
        """Первое значимое сообщение host'а — ``plugin.start`` (иначе контракт нарушен)."""
        while True:
            message = await self._client.recv()
            if isinstance(message, RpcNotification):
                if message.method != PLUGIN_START:
                    continue  # уведомления до старта игнорируем
                params = StartParams.model_validate(message.params or {})
                await self._start(params)
                self._started = True
                return
            if message.method != PLUGIN_START:
                raise TransportError(f"expected {PLUGIN_START!r}, got {message.method!r}")
            params = StartParams.model_validate(message.params or {})
            await self._start(params)
            await self._client.respond(message, result=StartResult().model_dump(mode="json"))
            self._started = True
            return

    async def _start(self, params: StartParams) -> None:
        if self._entry.on_start is None:
            return
        ctx = self.make_context()
        try:
            await self._entry.on_start(ctx)
        except Exception as exc:
            ctx.log.error(f"on_start failed: {exc}", traceback=traceback.format_exc())
            raise  # ненулевой exit → воркер рестартует с backoff

    async def _on_event_params(
        self,
        params: dict[str, Any],
        *,
        respond_to: RpcRequest | None,
    ) -> None:
        try:
            parsed = EventParams.model_validate(params)
        except ValueError as exc:
            if respond_to is not None:
                await self._client.respond(
                    respond_to,
                    error=RpcError(code=RpcErrorCode.INVALID_PARAMS, message=str(exc)),
                )
            return

        if self._entry.on_event is None:
            if respond_to is not None:
                await self._client.respond(respond_to, result=EventResult(handled=False).model_dump(mode="json"))
            return

        event = PluginEvent.from_hub(parsed.event)
        ctx = self.make_context(event.session_id)
        try:
            await self._entry.on_event(event, ctx)
        except Exception as exc:
            ctx.log.error(
                f"on_event({event.kind.value}) failed: {exc}",
                traceback=traceback.format_exc(),
                session_id=event.session_id,
            )
            if respond_to is not None:
                await self._client.respond(
                    respond_to,
                    error=RpcError(
                        code=RpcErrorCode.INTERNAL_ERROR,
                        message=f"{type(exc).__name__}: {exc}",
                    ),
                )
            return
        if respond_to is not None:
            await self._client.respond(respond_to, result=EventResult().model_dump(mode="json"))

    async def _stop(self, params: StopParams) -> None:
        if self._entry.on_stop is None:
            return
        ctx = self.make_context()
        try:
            await asyncio.wait_for(self._entry.on_stop(ctx), timeout=params.timeout_seconds)
        except TimeoutError:
            ctx.log.warning(f"on_stop timed out after {params.timeout_seconds}s")
        except Exception as exc:
            ctx.log.error(f"on_stop failed: {exc}", traceback=traceback.format_exc())


async def run_plugin(plugin_dir: str | Path | None = None) -> None:
    """Полный цикл плагина: манифест → точка входа → IPC → хуки → выход."""
    directory = Path(plugin_dir or os.environ.get(PLUGIN_DIR_ENV, ".")).resolve()
    socket_path = os.environ.get(PLUGIN_SOCKET_ENV)
    if not socket_path:
        raise TransportError(
            f"{PLUGIN_SOCKET_ENV} is not set — запускайте плагин через воркер "
            f"(python -m lucrium_plugin_sdk), а не вручную; для тестов используйте "
            f"lucrium_plugin_sdk.testing"
        )

    manifest, entry = load_entry(directory)
    client = await JsonRpcClient.connect_unix(socket_path)
    runtime = PluginRuntime(manifest, entry, client)
    try:
        await runtime.run()
    finally:
        await client.close()


__all__ = [
    "PLUGIN_DIR_ENV",
    "PLUGIN_SOCKET_ENV",
    "PluginRuntime",
    "import_entry_module",
    "load_entry",
    "run_plugin",
]
