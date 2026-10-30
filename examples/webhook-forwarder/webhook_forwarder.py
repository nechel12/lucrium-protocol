"""Webhook-форвардер: отправляет события площадок во внешний HTTP-endpoint.

Демонстрирует ``ctx.http`` — прямой HTTP доступ, который воркер разрешает
только при ``permissions.network = true`` в манифесте.
"""

from __future__ import annotations

import json
from typing import Any

from lucrium_plugin_sdk import PluginContext, PluginEvent


def _envelope(event: PluginEvent) -> dict[str, Any]:
    """Тело webhook'а: стабильный формат, не зависящий от SDK."""
    return {
        "platform": event.platform.value,
        "session_id": event.session_id,
        "kind": event.kind.value,
        "ts": event.ts.isoformat(),
        "payload": event.payload,
    }


async def on_start(ctx: PluginContext) -> None:
    url = ctx.config.get("url")
    if not url:
        ctx.log.warning("webhook-forwarder: url is not configured, events will be ignored")
    else:
        ctx.log.info("webhook-forwarder started", url=url, kinds=ctx.config.get("kinds", []))


async def on_event(event: PluginEvent, ctx: PluginContext) -> None:
    url = ctx.config.get("url")
    if not url:
        return
    kinds = ctx.config.get("kinds") or []
    if kinds and event.kind.value not in kinds:
        return

    headers = {"Content-Type": "application/json"}
    secret = ctx.config.get("secret")
    if secret:
        headers["X-Lucrium-Secret"] = str(secret)

    timeout = float(ctx.config.get("timeout_seconds", 10))
    response = await ctx.http.request(
        "POST",
        str(url),
        headers=headers,
        body=json.dumps(_envelope(event), ensure_ascii=False).encode(),
        timeout=timeout,
    )
    if not response.ok:
        # вебхук не должен ронять плагин: фиксируем и живём дальше
        ctx.log.warning(
            "webhook delivery failed",
            status=response.status,
            kind=event.kind.value,
            platform=event.platform.value,
        )
        return
    ctx.log.debug("webhook delivered", status=response.status, kind=event.kind.value)


async def on_stop(ctx: PluginContext) -> None:
    ctx.log.info("webhook-forwarder stopped")
