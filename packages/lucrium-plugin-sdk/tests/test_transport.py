"""Тесты NDJSON-транспорта: реальный сервер поверх TCP loopback.

Воркер в проде использует unix socket (Linux), но фрейминг и корреляция
ответов platform-agnostic — здесь проверяются на loopback-TCP, доступном
и на Windows, где asyncio не поддерживает unix-сокеты.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import pytest

from lucrium_plugin_sdk.transport import JsonRpcClient, RpcRemoteError, TransportError

HOST = "127.0.0.1"


async def _start_echo_host(
    handler: Any = None,
) -> tuple[asyncio.AbstractServer, list[asyncio.StreamWriter], str]:
    """TCP-сервер, эмулирующий воркер: читает NDJSON-строки, пишет ответы."""
    writers: list[asyncio.StreamWriter] = []

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writers.append(writer)
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                message = json.loads(raw)
                if handler is not None:
                    response = await handler(message, writer)
                    if response is not None:
                        writer.write((json.dumps(response) + "\n").encode())
                        await writer.drain()
        finally:
            # закрываем соединение: Server.wait_closed() ждёт завершения всех handlers
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    server = await asyncio.start_server(_handle, HOST, 0)
    port = server.sockets[0].getsockname()[1]
    return server, writers, port


async def _connect(port: int) -> JsonRpcClient:
    reader, writer = await asyncio.open_connection(HOST, port)
    return JsonRpcClient(reader, writer)


async def test_request_response_roundtrip() -> None:
    async def handler(message: dict[str, Any], writer: asyncio.StreamWriter) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": message["id"], "result": {"echo": message["params"]}}

    server, _, port = await _start_echo_host(handler)
    client = await _connect(port)
    try:
        result = await client.request("storage.get", {"key": "k"}, timeout=5)
        assert result == {"echo": {"key": "k"}}
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


async def test_remote_error_raises() -> None:
    async def handler(message: dict[str, Any], writer: asyncio.StreamWriter) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "error": {"code": -32004, "message": "no key"},
        }

    server, _, port = await _start_echo_host(handler)
    client = await _connect(port)
    try:
        with pytest.raises(RpcRemoteError) as excinfo:
            await client.request("storage.get", {"key": "missing"}, timeout=5)
        assert excinfo.value.code == -32004
        assert excinfo.value.message == "no key"
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


async def test_notify_and_incoming_request() -> None:
    """Плагин шлёт notify (log.write) и обслуживает входящий запрос host'а."""
    got_notifications: list[dict[str, Any]] = []

    async def handler(message: dict[str, Any], writer: asyncio.StreamWriter) -> dict[str, Any] | None:
        if "id" not in message:  # notify от плагина
            got_notifications.append(message)
            return None
        return {"jsonrpc": "2.0", "id": message["id"], "result": {"ok": True}}

    server, writers, port = await _start_echo_host(handler)
    client = await _connect(port)
    try:
        await client.notify("log.write", {"level": "info", "message": "hi"})
        # host инициирует запрос к плагину прямо на открытом соединении
        writers[0].write(b'{"jsonrpc": "2.0", "id": 99, "method": "plugin.ping", "params": {}}\n')
        await writers[0].drain()

        incoming = await asyncio.wait_for(client.recv(), timeout=5)
        assert getattr(incoming, "method", None) == "plugin.ping"
        await client.respond(incoming, result={"pong": True})  # type: ignore[arg-type]

        deadline = asyncio.get_running_loop().time() + 5
        while not got_notifications:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.001)
        assert got_notifications[0]["method"] == "log.write"
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


async def test_server_close_fails_pending_requests() -> None:
    """Обрыв связи: ожидающий запрос падает с TransportError, recv сообщает об обрыве."""

    async def handler(message: dict[str, Any], writer: asyncio.StreamWriter) -> dict[str, Any] | None:
        return None  # никогда не отвечаем

    server, writers, port = await _start_echo_host(handler)
    client = await _connect(port)
    try:
        pending = asyncio.create_task(client.request("slow.method", timeout=30))
        await asyncio.sleep(0.05)
        writers[0].close()
        await writers[0].wait_closed()
        with pytest.raises(TransportError):
            await asyncio.wait_for(pending, timeout=5)
        with pytest.raises(TransportError, match="closed"):
            await asyncio.wait_for(client.recv(), timeout=5)
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


async def test_request_timeout() -> None:
    async def handler(message: dict[str, Any], writer: asyncio.StreamWriter) -> dict[str, Any] | None:
        return None

    server, _, port = await _start_echo_host(handler)
    client = await _connect(port)
    try:
        with pytest.raises(TransportError, match="timeout"):
            await client.request("never", timeout=0.2)
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


async def test_malformed_json_breaks_connection() -> None:
    """Мусор вместо JSON — транспорт сообщает об ошибке и закрывается."""
    server, writers, port = await _start_echo_host()
    client = await _connect(port)
    try:
        writers[0].write(b"this is not json\n")
        await writers[0].drain()
        with pytest.raises(TransportError):
            await asyncio.wait_for(client.recv(), timeout=5)
        # ожидающие запросы тоже падают
        with pytest.raises(TransportError):
            await asyncio.wait_for(client.request("x"), timeout=5)
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


async def test_close_is_idempotent() -> None:
    server, _, port = await _start_echo_host()
    client = await _connect(port)
    await client.close()
    await client.close()  # повторный вызов — no-op
    assert client.closed
    with pytest.raises(TransportError, match="closed"):
        await client.request("x")
    server.close()
    await server.wait_closed()
