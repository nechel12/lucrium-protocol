"""Автоответчик: отвечает на входящее сообщение покупателя шаблоном из настроек.

Демонстрирует:

* ``ctx.session.call`` — вызов метода клиента aiosellers текущей сессии
  (имя метода различается по площадкам — см. :data:`SEND_METHOD`);
* ``ctx.storage`` — пауза на чат, чтобы не спамить;
* ``ctx.config`` — настройки из JSON-схемы (панель рендерит форму сама);
* ``ctx.log`` — журнал в панель и файл воркера.
"""

from __future__ import annotations

from typing import Any

from lucrium_plugin_sdk import PluginContext, PluginEvent
from lucrium_protocol import EventKind, Platform

#: Метод отправки сообщения у каждой площадки (контракт aiosellers).
SEND_METHOD: dict[Platform, str] = {
    Platform.FUNPAY: "chats.send_message",
    Platform.PLAYEROK: "chats.send_message",
    Platform.GGSEL: "chats.send",
}

#: Признак «сообщение отправлено нами» в payload'е события площадки.
_OUTGOING: dict[Platform, tuple[str, Any]] = {
    Platform.FUNPAY: ("message.by_bot", True),
    Platform.PLAYEROK: ("message.direction", "OUT"),
    Platform.GGSEL: ("message.direction", "seller"),
}


def _is_outgoing(event: PluginEvent) -> bool:
    path, expected = _OUTGOING[event.platform]
    return bool(event.get(path) == expected)


async def on_start(ctx: PluginContext) -> None:
    ctx.log.info(
        "auto-reply started",
        enabled=ctx.config.get("enabled", True),
        cooldown_seconds=ctx.config.get("cooldown_seconds", 300),
    )


async def on_event(event: PluginEvent, ctx: PluginContext) -> None:
    if event.kind != EventKind.MESSAGE or _is_outgoing(event):
        return
    if not ctx.config.get("enabled", True):
        return

    text = event.get("message.text")
    if not isinstance(text, str) or not text.strip():
        return  # системные сообщения/картинки без текста не трогаем

    needle = str(ctx.config.get("skip_if_contains", "")).strip().lower()
    if needle and needle in text.lower():
        return

    chat_id = event.get("message.chat_id") or event.get("chat.id")
    if chat_id is None:
        ctx.log.warning("auto-reply: no chat_id in event", kind=event.kind.value)
        return

    cooldown = int(ctx.config.get("cooldown_seconds", 300))
    key = f"cooldown:{event.session_id}:{chat_id}"
    last_sent = await ctx.storage.get(key)
    now = event.ts.timestamp()
    if cooldown > 0 and isinstance(last_sent, int | float) and now - float(last_sent) < cooldown:
        ctx.log.debug("auto-reply skipped by cooldown", chat_id=chat_id)
        return

    reply = ctx.config.require("reply_text")
    await ctx.session.call(SEND_METHOD[event.platform], {"chat_id": chat_id, "text": reply})
    await ctx.storage.set(key, now)
    ctx.log.info("auto-reply sent", platform=event.platform.value, chat_id=chat_id)


async def on_stop(ctx: PluginContext) -> None:
    ctx.log.info("auto-reply stopped")
