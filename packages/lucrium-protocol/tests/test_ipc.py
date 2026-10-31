"""Тесты JSON-RPC IPC-контракта «воркер ↔ плагин» (спека §6.3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lucrium_protocol import (
    EventParams,
    HttpRequestParams,
    LogLevel,
    LogWriteParams,
    RpcError,
    RpcErrorCode,
    RpcNotification,
    RpcRequest,
    RpcResponse,
    SessionCallParams,
    StartParams,
    StopParams,
    StorageSetParams,
)
from lucrium_protocol.ipc import MAX_HTTP_BODY_B64_CHARS, MAX_LOG_FIELDS_BYTES


def test_request_roundtrip() -> None:
    req = RpcRequest(id=1, method="storage.get", params={"key": "k"})
    parsed = RpcRequest.model_validate_json(req.model_dump_json())
    assert parsed.jsonrpc == "2.0"
    assert parsed.params == {"key": "k"}


def test_notification_has_no_id() -> None:
    note = RpcNotification(method="log.write", params={"level": "info", "message": "hi"})
    data = note.model_dump(mode="json")
    assert "id" not in data
    with pytest.raises(ValidationError):
        RpcRequest.model_validate(data)


def test_response_success_and_error() -> None:
    ok = RpcResponse(id=1, result={"value": None})
    assert ok.ok
    err = RpcResponse(id=1, error=RpcError(code=RpcErrorCode.NOT_FOUND, message="no key"))
    assert not err.ok
    with pytest.raises(ValidationError):
        RpcResponse(id=1, result={"a": 1}, error=RpcError(code=-32603, message="x"))


def test_rpc_error_codes_match_jsonrpc() -> None:
    assert RpcErrorCode.PARSE_ERROR == -32700
    assert RpcErrorCode.METHOD_NOT_FOUND == -32601
    assert RpcErrorCode.INVALID_PARAMS == -32602


def test_session_call_method_pattern() -> None:
    assert SessionCallParams(method="chats.get_by_name").method == "chats.get_by_name"
    assert SessionCallParams(method="lots.fields").method == "lots.fields"
    # session_id опционален (одна сессия у плагина), но ограничен по длине
    assert SessionCallParams(session_id="funpay-1", method="chats.list").session_id == "funpay-1"
    for bad in ["", "chats.", ".chats", "chats..get", "chats get", "1chats"]:
        with pytest.raises(ValidationError):
            SessionCallParams(method=bad)
    with pytest.raises(ValidationError):
        SessionCallParams(session_id="x" * 65, method="chats.list")


def test_start_stop_params() -> None:
    start = StartParams(plugin_id="p", config={"a": 1})
    assert start.config == {"a": 1}
    stop = StopParams()
    assert stop.timeout_seconds == 10.0
    with pytest.raises(ValidationError):
        StopParams(timeout_seconds=0)


def test_event_params_carries_hub_event(hub_event) -> None:  # type: ignore[no-untyped-def]
    params = EventParams(event=hub_event)
    restored = EventParams.model_validate_json(params.model_dump_json())
    assert restored.event.kind == hub_event.kind
    assert restored.event.payload["chat_id"] == "42"


def test_log_write_params_levels() -> None:
    params = LogWriteParams(message="hi", level=LogLevel.WARNING, fields={"order": 1})
    assert params.level is LogLevel.WARNING
    with pytest.raises(ValidationError):
        LogWriteParams(message="hi", level="critical")  # type: ignore[arg-type]


def test_http_request_params() -> None:
    req = HttpRequestParams(method="GET", url="https://api.example.com/x")
    assert req.timeout_seconds == 20.0
    with pytest.raises(ValidationError):
        HttpRequestParams(method="TRACE", url="https://x")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        HttpRequestParams(url="not-a-url-without-scheme")


def test_http_request_params_are_bounded() -> None:
    """Плагин не должен мочь передать воркеру бесконечное или инъективное тело."""
    with pytest.raises(ValidationError):
        HttpRequestParams(url="https://example.com/a\r\nX-Injected: 1")
    with pytest.raises(ValidationError):
        HttpRequestParams(url="https://example.com/", headers={"X-A": "v\r\nY: z"})
    with pytest.raises(ValidationError):
        HttpRequestParams(url="https://example.com/", headers={f"X-{i}": "v" for i in range(40)})
    with pytest.raises(ValidationError):
        HttpRequestParams(url="https://example.com/", headers={"X" * 200: "v"})
    with pytest.raises(ValidationError):
        HttpRequestParams(url="https://example.com/", body_b64="A" * (MAX_HTTP_BODY_B64_CHARS + 4))
    assert HttpRequestParams(url="https://example.com/", headers={"X-A": "v"}, body_b64="AAAA").method == "GET"


def test_log_fields_are_bounded() -> None:
    """ctx.log не должен превращаться в переносчик данных через IPC."""
    with pytest.raises(ValidationError):
        LogWriteParams(message="m", fields={f"k{i}": "v" for i in range(40)})
    with pytest.raises(ValidationError):
        LogWriteParams(message="m", fields={"blob": "x" * (MAX_LOG_FIELDS_BYTES + 1)})
    assert LogWriteParams(message="m", fields={"chat_id": 1}).fields == {"chat_id": 1}


def test_storage_set_accepts_arbitrary_json() -> None:
    params = StorageSetParams(key="counters:a", value={"nested": [1, 2, {"x": None}]})
    roundtrip = StorageSetParams.model_validate_json(params.model_dump_json())
    assert roundtrip.value == {"nested": [1, 2, {"x": None}]}
