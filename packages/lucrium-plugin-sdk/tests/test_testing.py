"""Тесты инструментов тестирования: фейки и run_lifecycle."""

from __future__ import annotations

from collections.abc import Callable
from types import ModuleType
from typing import Any

import pytest

from lucrium_plugin_sdk.context import HttpResponse
from lucrium_plugin_sdk.events import PluginEvent
from lucrium_plugin_sdk.testing import (
    FakeHttp,
    FakeSession,
    LifecycleResult,
    ListLog,
    MemoryStorage,
    make_context,
    run_lifecycle,
)
from lucrium_protocol.events import HubEvent
from lucrium_protocol.types import EventKind, Platform

ModuleFactory = Callable[..., ModuleType]


# --- MemoryStorage -----------------------------------------------------------


async def test_memory_storage_kv() -> None:
    storage = MemoryStorage({"a": 1})
    assert await storage.get("a") == 1
    assert await storage.get("missing", "fallback") == "fallback"
    await storage.set("a:1", "x")
    await storage.set("b:1", "y")
    assert await storage.keys("a:") == ["a:1"]
    await storage.delete("a")
    assert await storage.get("a") is None


# --- ListLog -----------------------------------------------------------------


def test_list_log_levels() -> None:
    log = ListLog()
    log.debug("d")
    log.info("i", order=42)
    log.warning("w")
    log.error("e")
    assert log.messages() == ["d", "i", "w", "e"]
    assert log.messages(level="warning") == ["w", "e"]  # type: ignore[arg-type]
    assert log.records[1].fields == {"order": 42}


# --- FakeSession -------------------------------------------------------------


async def test_fake_session_records_and_replays() -> None:
    session = FakeSession("funpay-1")
    session.on("chats.get_by_name", {"id": 42})
    result = await session.call("chats.get_by_name", {"name": "buyer"})
    assert result == {"id": 42}
    (call,) = session.calls
    assert call.session_id == "funpay-1"
    assert call.params == {"name": "buyer"}


async def test_fake_session_callable_handler() -> None:
    session = FakeSession()
    session.on("orders.list", lambda params: (None, [{"id": params["limit"]}]))
    result = await session.call("orders.list", {"limit": 3})
    assert result[1] == [{"id": 3}]


async def test_fake_session_missing_handler_raises() -> None:
    session = FakeSession()
    with pytest.raises(LookupError, match="no handler"):
        await session.call("lots.update")


async def test_fake_session_for_session_shares_journal() -> None:
    session = FakeSession()
    session.on("chats.send_message", {"ok": True})
    other = session.for_session("playerok-2")
    await other.call("chats.send_message", {"chat_id": 1})
    # вызов записан в общий журнал с id целевой сессии
    assert session.calls[0].session_id == "playerok-2"
    assert session.last_call("chats.send_message").method == "chats.send_message"


# --- FakeHttp ----------------------------------------------------------------


async def test_fake_http_tuple_and_bytes() -> None:
    http = FakeHttp()
    http.on("https://api.test/v1", (200, '{"ok": true}'))
    http.on("https://api.test/ping", b"pong", method="POST")
    r1 = await http.request("GET", "https://api.test/v1")
    assert r1.ok
    assert r1.json() == {"ok": True}
    r2 = await http.request("POST", "https://api.test/ping")
    assert r2.text() == "pong"
    assert [r.method for r in http.requests] == ["GET", "POST"]


async def test_fake_http_response_object_and_callable() -> None:
    http = FakeHttp()
    http.on("https://api.test/r", HttpResponse(404, {}, b"nope"))
    http.on("https://api.test/echo", lambda req: req.url)
    assert (await http.request("GET", "https://api.test/r")).status == 404
    assert (await http.request("GET", "https://api.test/echo")).text() == "https://api.test/echo"


async def test_fake_http_missing_raises() -> None:
    with pytest.raises(LookupError, match="no response"):
        await FakeHttp().request("GET", "https://none")


# --- make_context ------------------------------------------------------------


async def test_make_context_defaults() -> None:
    ctx = make_context("p", config={"a": {"b": 5}}, session_id="s1")
    assert ctx.plugin_id == "p"
    assert ctx.config["a"]["b"] == 5
    assert ctx.config.get("a.b") == 5  # точечный путь
    await ctx.storage.set("k", "v")
    assert await ctx.storage.get("k") == "v"
    ctx.log.info("hello", x=1)
    assert ctx.log.records[0].message == "hello"  # type: ignore[attr-defined]


# --- run_lifecycle -----------------------------------------------------------


async def test_run_lifecycle_inline_module(hub_event: HubEvent, module_factory: ModuleFactory) -> None:
    seen: dict[str, Any] = {"started": False, "events": [], "stopped": False}

    async def on_start(ctx: Any) -> None:
        seen["started"] = True
        await ctx.storage.set("started_at", "now")

    async def on_event(event: PluginEvent, ctx: Any) -> None:
        seen["events"].append((event.kind, ctx.session_id))
        await ctx.session.call("chats.send_message", {"chat_id": 1, "text": "ok"})

    async def on_stop(ctx: Any) -> None:
        seen["stopped"] = True

    session = FakeSession()
    session.on("chats.send_message", {"ok": True})
    module = module_factory(on_start=on_start, on_event=on_event, on_stop=on_stop)

    result: LifecycleResult = await run_lifecycle(module, events=[hub_event], session=session)

    assert result.started and result.stopped
    assert result.start_error is None and result.stop_error is None
    assert result.event_errors == []
    assert seen["events"] == [(EventKind.MESSAGE, "funpay-1")]
    assert result.storage.data == {"started_at": "now"}
    assert session.calls_for("chats.send_message")


async def test_run_lifecycle_collects_errors(module_factory: ModuleFactory) -> None:
    """Крах on_event не роняет прогон — ошибка копится в результате (как в рантайме)."""

    async def on_event(event: Any, ctx: Any) -> None:
        raise RuntimeError("boom")

    module = module_factory(on_event=on_event)
    event = PluginEvent(
        platform=Platform.FUNPAY,
        session_id="s1",
        kind=EventKind.ORDER,
        payload={},
        ts="2026-01-15T12:00:00Z",  # type: ignore[arg-type]
    )
    result = await run_lifecycle(module, events=[event])
    assert len(result.event_errors) == 1
    assert "boom" in str(result.event_errors[0])


async def test_run_lifecycle_accepts_plugin_event(module_factory: ModuleFactory) -> None:
    async def on_event(event: PluginEvent, ctx: Any) -> None:
        ctx.log.info(f"got {event.kind}")

    module = module_factory(on_event=on_event)
    plugin_event = PluginEvent(
        platform=Platform.GGSEL,
        session_id="ggsel-1",
        kind=EventKind.REVIEW,
        payload={"rating": 5},
        ts="2026-01-15T12:00:00Z",  # type: ignore[arg-type]
    )
    result = await run_lifecycle(module, events=[plugin_event])
    assert result.log.messages() == ["got review"]
