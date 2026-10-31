"""Lucrium Plugin SDK — SDK для авторов плагинов (Apache-2.0).

Минимальный плагин::

    # main.py
    from lucrium_plugin_sdk import PluginContext, PluginEvent

    async def on_start(ctx: PluginContext) -> None:
        ctx.log.info("started")

    async def on_event(event: PluginEvent, ctx: PluginContext) -> None:
        if event.kind == "message":
            await ctx.session.call("chats.send_message", {"chat_id": ..., "text": "Привет!"})

Каталог плагина::

    my-plugin/
    ├── plugin.toml            # манифест (id, версия, права, схема конфига)
    ├── config_schema.json     # JSON Schema — панель рендерит форму настроек сама
    └── main.py                # точка входа с хуками on_start/on_event/on_stop

Документация: https://github.com/nechel12/lucrium-protocol/blob/main/packages/lucrium-plugin-sdk/docs/writing-plugins.md
"""

from __future__ import annotations

from .context import (
    ConfigView,
    HttpResponse,
    HttpView,
    LogView,
    PluginContext,
    PluginUsageError,
    SessionView,
    StorageView,
)
from .entry import (
    EntryError,
    HookEvent,
    HookStart,
    HookStop,
    PluginEntry,
    discover_entry,
    load_entry,
    on_event,
    on_start,
    on_stop,
)
from .events import PluginEvent
from .manifest import ConfigSchemaError, load_config_schema, load_manifest
from .runtime import (
    PLUGIN_DIR_ENV,
    PLUGIN_SOCKET_ENV,
    PluginRuntime,
    run_plugin,
)
from .runtime import (
    load_entry as load_plugin,
)
from .transport import JsonRpcClient, RpcRemoteError, TransportError

__version__ = "0.1.2"

__all__ = [
    "PLUGIN_DIR_ENV",
    "PLUGIN_SOCKET_ENV",
    "ConfigSchemaError",
    "ConfigView",
    "EntryError",
    "HookEvent",
    "HookStart",
    "HookStop",
    "HttpResponse",
    "HttpView",
    "JsonRpcClient",
    "LogView",
    "PluginContext",
    "PluginEntry",
    "PluginEvent",
    "PluginRuntime",
    "PluginUsageError",
    "RpcRemoteError",
    "SessionView",
    "StorageView",
    "TransportError",
    "discover_entry",
    "load_config_schema",
    "load_entry",
    "load_manifest",
    "load_plugin",
    "on_event",
    "on_start",
    "on_stop",
    "run_plugin",
]
