"""Сквозные тесты примеров: жизненный цикл плагина через фейки SDK.

Примеры — это документация, которая обязана работать: тесты гоняют их
``on_start``/``on_event``/``on_stop`` без воркера и площадок.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lucrium_plugin_sdk.manifest import load_config_schema, load_manifest
from lucrium_plugin_sdk.testing import FakeHttp, FakeSession, LifecycleResult, run_lifecycle
from lucrium_protocol import EventKind, HubEvent, Platform

EXAMPLES = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

AUTO_REPLY = EXAMPLES / "auto-reply"
WEBHOOK = EXAMPLES / "webhook-forwarder"


def message_event(
    platform: Platform = Platform.FUNPAY,
    *,
    text: str = "Здравствуйте! Есть в наличии?",
    chat_id: int | str = 42,
    payload_extra: dict[str, Any] | None = None,
) -> HubEvent:
    message: dict[str, Any] = {"id": 7, "text": text, "chat_id": chat_id, **(payload_extra or {})}
    payload: dict[str, Any] = {"chat": {"id": chat_id}, "message": message}
    return HubEvent(platform=platform, session_id="acc-1", kind=EventKind.MESSAGE, payload=payload, ts=NOW)


# --- auto-reply --------------------------------------------------------------


async def test_auto_reply_sends_template_on_incoming_message() -> None:
    session = FakeSession().on("chats.send_message", {"id": 100})
    result: LifecycleResult = await run_lifecycle(
        AUTO_REPLY,
        config={"reply_text": "Привет! Да, всё в наличии."},
        events=[message_event()],
        session=session,
    )

    assert result.started and result.stopped
    assert result.event_errors == []
    call = session.last_call("chats.send_message")
    assert call.params == {"chat_id": 42, "text": "Привет! Да, всё в наличии."}
    assert call.session_id == "acc-1"
    # пауза на чат сохранена в хранилище
    assert result.storage.data["cooldown:acc-1:42"] == NOW.timestamp()


async def test_auto_reply_skips_outgoing_message() -> None:
    session = FakeSession().on("chats.send_message", {})
    result = await run_lifecycle(
        AUTO_REPLY,
        config={"reply_text": "Привет"},
        events=[message_event(payload_extra={"by_bot": True})],
        session=session,
    )
    assert result.event_errors == []
    assert session.calls == []


async def test_auto_reply_respects_cooldown() -> None:
    session = FakeSession().on("chats.send_message", {})
    first = message_event()
    second = message_event(text="Алло?")
    result = await run_lifecycle(
        AUTO_REPLY,
        config={"reply_text": "Привет", "cooldown_seconds": 3600},
        events=[first, second],
        session=session,
    )
    assert result.event_errors == []
    assert len(session.calls_for("chats.send_message")) == 1


async def test_auto_reply_uses_platform_send_method() -> None:
    session = FakeSession().on("chats.send", {"id": 1})
    result = await run_lifecycle(
        AUTO_REPLY,
        config={"reply_text": "Привет"},
        events=[message_event(Platform.GGSEL, payload_extra={"direction": "buyer"})],
        session=session,
    )
    assert result.event_errors == []
    assert session.last_call("chats.send").params["chat_id"] == 42


async def test_auto_reply_disabled_sends_nothing() -> None:
    session = FakeSession().on("chats.send_message", {})
    result = await run_lifecycle(
        AUTO_REPLY,
        config={"reply_text": "Привет", "enabled": False},
        events=[message_event()],
        session=session,
    )
    assert result.event_errors == []
    assert session.calls == []


# --- webhook-forwarder -------------------------------------------------------


async def test_webhook_forwarder_posts_event_with_secret() -> None:
    http = FakeHttp().on("https://example.test/hook", (200, "ok"), method="POST")
    result = await run_lifecycle(
        WEBHOOK,
        config={"url": "https://example.test/hook", "secret": "s3cret"},
        events=[message_event()],
        http=http,
    )

    assert result.started and result.stopped
    assert result.event_errors == []
    (request,) = http.requests
    assert request.method == "POST"
    assert request.headers["X-Lucrium-Secret"] == "s3cret"
    body = json.loads(request.body or b"{}")
    assert body["platform"] == "funpay"
    assert body["kind"] == "message"
    assert body["payload"]["message"]["text"] == "Здравствуйте! Есть в наличии?"
    assert result.log.messages() == [
        "webhook-forwarder started",
        "webhook delivered",
        "webhook-forwarder stopped",
    ]


async def test_webhook_forwarder_filters_kinds() -> None:
    http = FakeHttp().on("https://example.test/hook", (200, "ok"), method="POST")
    order = HubEvent(
        platform=Platform.PLAYEROK,
        session_id="acc-1",
        kind=EventKind.ORDER,
        payload={"order": {"id": "o-1"}},
        ts=NOW,
    )
    result = await run_lifecycle(
        WEBHOOK,
        config={"url": "https://example.test/hook", "kinds": ["message"]},
        events=[order],
        http=http,
    )
    assert result.event_errors == []
    assert http.requests == []


async def test_webhook_forwarder_logs_failed_delivery() -> None:
    http = FakeHttp().on("https://example.test/hook", (503, "nope"), method="POST")
    result = await run_lifecycle(
        WEBHOOK,
        config={"url": "https://example.test/hook"},
        events=[message_event()],
        http=http,
    )
    assert result.event_errors == []
    assert any("webhook delivery failed" in message for message in result.log.messages())


# --- манифесты и схемы -------------------------------------------------------


def test_examples_manifests_and_schemas_are_valid() -> None:
    for directory in (AUTO_REPLY, WEBHOOK):
        manifest = load_manifest(directory)
        schema = load_config_schema(directory, manifest)
        assert schema is not None, directory
        assert schema["type"] == "object"
        assert schema["properties"], directory
