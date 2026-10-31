"""Рантайм плагина: подключение к воркеру и диспетчеризация контрактных хуков.

Жизненный цикл (контракт ``api = "1"``):

1. воркер запускает subprocess плагина (``python -m lucrium_plugin_sdk``),
   передавая ``LUCRIUM_PLUGIN_DIR`` и ``LUCRIUM_PLUGIN_SOCKET``;
2. рантайм импортирует точку входа из манифеста (только файл внутри каталога
   плагина), валидирует хуки;
3. host шлёт ``plugin.start`` (конфиг) → ``on_start``;
4. далее поток ``plugin.event`` → ``on_event`` (контекст привязан к сессии события);
5. ``plugin.stop`` → ``on_stop`` → выход.

Исключение в ``on_start`` → ненулевой код выхода (воркер рестартует с backoff);
исключения в ``on_event``/``on_stop`` логируются и не роняют процесс — крах
одного события не должен убивать плагин (спека §6.3).
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import re
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

# Сегмент dotted-пути точки входа; тот же язык, что у PluginManifest.entry.
_ENTRY_PART_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _entry_source_file(root: Path, dotted: str) -> Path:
    """Файл точки входа — только внутри каталога плагина.

    Резолвим ``<root>/<entry>.py`` и ``<root>/<entry>/__init__.py`` сами, а не
    через ``importlib.import_module``: последний взял бы первый попавшийся
    установленный модуль, так что ``entry = "lucrium_worker.config"`` из
    манифеста импортировал бы код воркера (манифест это пропускает — его
    регулярка проверяет только синтаксис dotted-пути).
    """
    if not dotted:
        raise EntryError("entry must not be empty")
    parts = dotted.split(".")
    for part in parts:
        if not _ENTRY_PART_RE.match(part):
            raise EntryError(f"entry {dotted!r} is not a module path inside the plugin directory")
    relative = Path(*parts)
    module_file = (root / relative).with_suffix(".py")
    package_file = root / relative / "__init__.py"
    for candidate in (module_file, package_file):
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root):
            raise EntryError(f"entry {dotted!r} resolves outside the plugin directory: {resolved}")
    if module_file.is_file() and package_file.is_file():
        raise EntryError(f"ambiguous entry {dotted!r}: both {dotted}.py and {dotted}/__init__.py exist")
    for candidate in (module_file, package_file):
        if candidate.is_file():
            return candidate
    raise EntryError(f"entry module {dotted!r} not found inside {root} (tried {dotted}.py, {dotted}/__init__.py)")


def _entry_module_name(root: Path, dotted: str) -> str:
    """Уникальное имя модуля: два плагина с ``entry = "main"`` не затирают друг друга."""
    digest = hashlib.sha256(f"{root}\x00{dotted}".encode()).hexdigest()[:16]
    slug = re.sub(r"[^A-Za-z0-9_]+", "_", f"{root.name}_{dotted}".lower()).strip("_")[:40]
    return f"lucrium_plugin_entry_{slug}_{digest}"


def _add_plugin_dir_to_sys_path(root: Path) -> None:
    """Дописывает каталог плагина в КОНЕЦ ``sys.path`` (не в начало).

    Нужно отложенным импортам соседних файлов плагина (``import helpers``
    внутри хука). В конец — чтобы stdlib, site-packages и ``lucrium_protocol``
    разрешались раньше: ``sys.path.insert(0, root)`` давал бы плагину с
    ``json.py``/``asyncio.py``/``lucrium_protocol/`` подменить их всему процессу
    плагина, включая отложенные импорты самого рантайма.
    """
    directory = str(root)
    if directory not in sys.path:
        sys.path.append(directory)


def import_entry_module(plugin_dir: Path, dotted: str) -> ModuleType:
    """Импортирует модуль-точку входа (``manifest.plugin.entry``) из корня плагина.

    Точка входа обязана лежать файлом внутри директории плагина; импорт
    выполняется через ``spec_from_file_location`` под уникальным именем, поэтому
    ни stdlib, ни ``lucrium_protocol``, ни другой плагин подменены быть не могут.
    """
    root = Path(plugin_dir).resolve()
    source = _entry_source_file(root, dotted)
    name = _entry_module_name(root, dotted)
    cached = sys.modules.get(name)
    if cached is not None and getattr(cached, "__file__", None) == str(source):
        return cached

    _add_plugin_dir_to_sys_path(root)
    spec = importlib.util.spec_from_file_location(name, source)
    if spec is None or spec.loader is None:
        raise EntryError(f"cannot build import spec for {source}")
    module = importlib.util.module_from_spec(spec)
    # регистрация до исполнения: на неё опираются относительные импорты пакета-точки входа
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(name, None)
        raise EntryError(f"cannot import entry module {dotted!r}: {exc}") from exc
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


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
        # Конфиг приходит именно в plugin.start (панель → воркер → плагин);
        # конструктор задаёт только тестовое/начальное значение.
        self._config = ConfigView(params.config)
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
