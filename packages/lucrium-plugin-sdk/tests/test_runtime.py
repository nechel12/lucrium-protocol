"""Тесты рантайма плагина: диспетчеризация plugin.start/event/stop через FakeHost."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from types import ModuleType
from typing import Any

from lucrium_plugin_sdk.entry import PluginEntry, discover_entry
from lucrium_plugin_sdk.runtime import PluginRuntime, load_entry
from lucrium_plugin_sdk.testing import FakeHost, make_context
from lucrium_protocol.events import HubEvent
from lucrium_protocol.ipc import (
    PLUGIN_EVENT,
    PLUGIN_START,
    PLUGIN_STOP,
    RpcErrorCode,
)
from lucrium_protocol.manifest import PluginManifest

ModuleFactory = Callable[..., ModuleType]


def _entry_and_calls(module_factory: ModuleFactory) -> tuple[PluginEntry, dict[str, Any]]:
    calls: dict[str, Any] = {"start": [], "events": [], "stop": []}

    async def on_start(ctx: Any) -> None:
        calls["start"].append(ctx)

    async def on_event(event: HubEvent, ctx: Any) -> None:
        calls["events"].append((event, ctx))

    async def on_stop(ctx: Any) -> None:
        calls["stop"].append(ctx)

    entry = discover_entry(module_factory(on_start=on_start, on_event=on_event, on_stop=on_stop)).ensure_valid()
    return entry, calls


def _spawn(
    manifest: PluginManifest,
    entry: PluginEntry,
    host: FakeHost,
    *,
    config: dict[str, Any] | None = None,
) -> asyncio.Task[None]:
    """Запускает ``runtime.run()`` — без этого ``wait_for_response`` ждёт вечно."""
    runtime = PluginRuntime(manifest, entry, host, config=config or {})  # type: ignore[arg-type]
    return asyncio.create_task(runtime.run(), name=f"runtime-{manifest.id}")


async def _drain(task: asyncio.Task[None]) -> None:
    """Отменяет рантайм и ждёт завершения (тест не должен оставлять висящих задач)."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_full_lifecycle(
    manifest: PluginManifest,
    hub_event: HubEvent,
    module_factory: ModuleFactory,
) -> None:
    entry, calls = _entry_and_calls(module_factory)
    host = FakeHost()
    task = _spawn(manifest, entry, host, config={"greeting": "Привет"})

    start_request = host.send_request(PLUGIN_START, {"plugin_id": manifest.id, "config": {"greeting": "Привет"}})
    response = await host.wait_for_response()
    assert response.id == start_request.id
    assert response.result == {"ready": True}

    event_request = host.send_request(PLUGIN_EVENT, {"event": hub_event.model_dump(mode="json")})
    response = await host.wait_for_response()
    assert response.id == event_request.id
    assert response.result == {"handled": True}

    stop_request = host.send_request(PLUGIN_STOP, {"reason": "shutdown"})
    response = await host.wait_for_response()
    assert response.id == stop_request.id
    assert response.result == {"stopped": True}
    await asyncio.wait_for(task, timeout=5)

    assert len(calls["start"]) == 1
    assert calls["start"][0].config["greeting"] == "Привет"
    (event, ctx) = calls["events"][0]
    assert event.session_id == hub_event.session_id
    # контекст события привязан к сессии
    assert ctx.session_id == hub_event.session_id
    assert len(calls["stop"]) == 1


async def test_event_as_notification(
    manifest: PluginManifest,
    hub_event: HubEvent,
    module_factory: ModuleFactory,
) -> None:
    """Уведомление plugin.event (без id) обрабатывается без ответа."""
    entry, calls = _entry_and_calls(module_factory)
    host = FakeHost()
    task = _spawn(manifest, entry, host)
    try:
        host.send_request(PLUGIN_START, {"plugin_id": manifest.id, "config": {}})
        await host.wait_for_response()
        host.send_notification(PLUGIN_EVENT, {"event": hub_event.model_dump(mode="json")})

        deadline = asyncio.get_running_loop().time() + 5
        while not calls["events"]:
            assert asyncio.get_running_loop().time() < deadline, "notification event was not dispatched"
            await asyncio.sleep(0.001)
        assert host.responses == []  # на уведомление ответ не отправляется
    finally:
        await _drain(task)


async def test_event_handler_crash_returns_error(
    manifest: PluginManifest,
    hub_event: HubEvent,
    module_factory: ModuleFactory,
) -> None:
    async def on_event(event: HubEvent, ctx: Any) -> None:
        ctx.log.error("boom")
        raise RuntimeError("handler crashed")

    entry = discover_entry(module_factory(on_event=on_event)).ensure_valid()
    host = FakeHost()
    task = _spawn(manifest, entry, host)
    try:
        host.send_request(PLUGIN_START, {"plugin_id": manifest.id, "config": {}})
        await host.wait_for_response()

        host.send_request(PLUGIN_EVENT, {"event": hub_event.model_dump(mode="json")})
        response = await host.wait_for_response()
        assert response.error is not None
        assert response.error.code == RpcErrorCode.INTERNAL_ERROR
        assert "handler crashed" in response.error.message
        # ошибка залогирована через ctx.log (notify в host) — запись fire-and-forget
        deadline = asyncio.get_running_loop().time() + 5
        while not host.log_entries:
            assert asyncio.get_running_loop().time() < deadline, "log entry was not delivered"
            await asyncio.sleep(0.001)
        assert any("boom" in entry_log.message for entry_log in host.log_entries)
    finally:
        await _drain(task)


async def test_event_without_hook_reports_not_handled(
    manifest: PluginManifest,
    hub_event: HubEvent,
    module_factory: ModuleFactory,
) -> None:
    async def on_start(ctx: Any) -> None:
        pass

    entry = discover_entry(module_factory(on_start=on_start)).ensure_valid()
    host = FakeHost()
    task = _spawn(manifest, entry, host)
    try:
        host.send_request(PLUGIN_START, {"plugin_id": manifest.id, "config": {}})
        await host.wait_for_response()

        host.send_request(PLUGIN_EVENT, {"event": hub_event.model_dump(mode="json")})
        response = await host.wait_for_response()
        assert response.result == {"handled": False}
    finally:
        await _drain(task)


async def test_malformed_event_params_rejected(
    manifest: PluginManifest,
    module_factory: ModuleFactory,
) -> None:
    entry, _ = _entry_and_calls(module_factory)
    host = FakeHost()
    task = _spawn(manifest, entry, host)
    try:
        host.send_request(PLUGIN_START, {"plugin_id": manifest.id, "config": {}})
        await host.wait_for_response()

        host.send_request(PLUGIN_EVENT, {"event": {"platform": "no-such-platform"}})
        response = await host.wait_for_response()
        assert response.error is not None
        assert response.error.code == RpcErrorCode.INVALID_PARAMS
    finally:
        await _drain(task)


async def test_unknown_method_rejected(
    manifest: PluginManifest,
    module_factory: ModuleFactory,
) -> None:
    entry, _ = _entry_and_calls(module_factory)
    host = FakeHost()
    task = _spawn(manifest, entry, host)
    try:
        host.send_request(PLUGIN_START, {"plugin_id": manifest.id, "config": {}})
        await host.wait_for_response()

        host.send_request("plugin.explode", {})
        response = await host.wait_for_response()
        assert response.error is not None
        assert response.error.code == RpcErrorCode.METHOD_NOT_FOUND
    finally:
        await _drain(task)


async def test_double_start_rejected(
    manifest: PluginManifest,
    module_factory: ModuleFactory,
) -> None:
    entry, _ = _entry_and_calls(module_factory)
    host = FakeHost()
    task = _spawn(manifest, entry, host)
    try:
        host.send_request(PLUGIN_START, {"plugin_id": manifest.id, "config": {}})
        await host.wait_for_response()

        host.send_request(PLUGIN_START, {"plugin_id": manifest.id, "config": {}})
        response = await host.wait_for_response()
        assert response.error is not None
        assert response.error.code == RpcErrorCode.INVALID_REQUEST
    finally:
        await _drain(task)


async def test_storage_via_context(
    manifest: PluginManifest,
    module_factory: ModuleFactory,
) -> None:
    async def on_start(ctx: Any) -> None:
        await ctx.storage.set("counter", 41)
        current = await ctx.storage.get("counter")
        await ctx.storage.set("counter", current + 1)

    entry = discover_entry(module_factory(on_start=on_start)).ensure_valid()
    host = FakeHost()
    task = _spawn(manifest, entry, host)
    try:
        host.send_request(PLUGIN_START, {"plugin_id": manifest.id, "config": {}})
        await host.wait_for_response()
        assert host.storage["counter"] == 42
    finally:
        await _drain(task)


async def test_session_call_carries_session_id(
    manifest: PluginManifest,
    hub_event: HubEvent,
    module_factory: ModuleFactory,
) -> None:
    async def on_event(event: HubEvent, ctx: Any) -> None:
        await ctx.session.call("chats.send_message", {"chat_id": 1, "text": "hi"})

    entry = discover_entry(module_factory(on_event=on_event)).ensure_valid()
    host = FakeHost(session_data={"id": "msg-1"})
    task = _spawn(manifest, entry, host)
    try:
        host.send_request(PLUGIN_START, {"plugin_id": manifest.id, "config": {}})
        await host.wait_for_response()

        host.send_request(PLUGIN_EVENT, {"event": hub_event.model_dump(mode="json")})
        response = await host.wait_for_response()
        assert response.result == {"handled": True}
        # session.call привязан к сессии события
        assert host.session_calls[0].session_id == hub_event.session_id
        assert host.session_calls[0].method == "chats.send_message"
    finally:
        await _drain(task)


async def test_load_entry_from_directory(tmp_path: Any) -> None:
    """Манифест + точка входа из реального каталога (импорт модуля main)."""
    (tmp_path / "plugin.toml").write_text(
        '[plugin]\nid = "dir-plugin"\nname = "Dir"\nversion = "0.1.0"\nentry = "main"\n',
        encoding="utf-8",
    )
    (tmp_path / "main.py").write_text(
        "seen = []\n\nasync def on_start(ctx):\n    seen.append(ctx.plugin_id)\n",
        encoding="utf-8",
    )
    manifest, entry = load_entry(tmp_path)
    assert manifest.id == "dir-plugin"
    assert entry.on_start is not None

    # хук вызывается с контекстом из фейков
    await entry.on_start(make_context("dir-plugin"))  # type: ignore[misc]
    module = __import__("main")
    assert module.seen == ["dir-plugin"]
