"""Тесты envelope: request/response/event, дискриминаторы, реестры команд (спека §5)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, SecretStr, ValidationError

from lucrium_protocol import (
    COMMAND_PARAMS,
    COMMAND_RESULTS,
    AccountCredentials,
    AccountInput,
    AccountRef,
    AddAccountParams,
    CallClientParams,
    Command,
    ErrorCode,
    Event,
    ExportStateParams,
    GetEventsParams,
    GetLogsParams,
    GetPluginSchemaParams,
    GetStateParams,
    ImportStateParams,
    InstallPluginParams,
    LogScope,
    PingParams,
    PingResult,
    Platform,
    PluginRef,
    ProtocolError,
    RemoveAccountParams,
    RemovePluginParams,
    Request,
    Response,
    RestartParams,
    RestartResult,
    RotateTokenParams,
    ServerEvent,
    SessionStatus,
    SetPluginConfigParams,
    StartSessionParams,
    StateSnapshot,
    StateWithCredentials,
    StopSessionParams,
    UpdateAccountParams,
    UpdatePluginParams,
    WorkerStatus,
    build_error_response,
    build_event,
    build_request,
    build_response,
    event_data,
    parse_envelope,
    parse_request,
    response_error,
    response_result,
    to_wire,
)

SAMPLE_PARAMS: dict[Command, BaseModel] = {
    Command.GET_STATE: GetStateParams(),
    Command.ADD_ACCOUNT: AddAccountParams(
        account=AccountInput.model_validate(
            {
                "platform": "funpay",
                "label": "Основной",
                "credentials": {"platform": "funpay", "golden_key": "gk-123"},
            }
        )
    ),
    Command.UPDATE_ACCOUNT: UpdateAccountParams(account_id="acc-1", label="Новое имя"),
    Command.REMOVE_ACCOUNT: RemoveAccountParams(account_id="acc-1"),
    Command.START_SESSION: StartSessionParams(account_id="acc-1"),
    Command.STOP_SESSION: StopSessionParams(account_id="acc-1"),
    Command.INSTALL_PLUGIN: InstallPluginParams(archive_b64="UEsDBA=="),
    Command.UPDATE_PLUGIN: UpdatePluginParams(plugin_id="p1", archive_b64="UEsDBA=="),
    Command.REMOVE_PLUGIN: RemovePluginParams(plugin_id="p1"),
    Command.SET_PLUGIN_CONFIG: SetPluginConfigParams(plugin_id="p1", config={"a": 1}),
    Command.GET_PLUGIN_SCHEMA: GetPluginSchemaParams(plugin_id="p1"),
    Command.CALL_CLIENT: CallClientParams(
        session_id="acc-1", client_method="lots.list_self", params={"subcategory_id": 1}
    ),
    Command.EXPORT_STATE: ExportStateParams(passphrase=SecretStr("correct-horse-battery")),
    Command.IMPORT_STATE: ImportStateParams(
        blob_b64="UEsDBA==",
        passphrase=SecretStr("correct-horse-battery"),
    ),
    Command.GET_LOGS: GetLogsParams(limit=10),
    Command.GET_EVENTS: GetEventsParams(limit=10),
    Command.ROTATE_TOKEN: RotateTokenParams(),
    Command.RESTART: RestartParams(),
    Command.PING: PingParams(nonce="rtt-1"),
}


def test_request_roundtrip_json() -> None:
    request = build_request(Command.GET_STATE, GetStateParams())
    raw = request.model_dump_json()
    parsed = parse_request(raw)
    assert isinstance(parsed, Request)
    assert parsed.type == "request"
    assert parsed.payload.method == "get_state"
    assert parsed.id == request.id


def test_request_dict_payload() -> None:
    request = build_request("get_logs", {"limit": 10, "scope": "plugin", "plugin_id": "p1"})
    parsed = parse_request(request.model_dump(mode="json"))
    assert isinstance(parsed.payload, GetLogsParams)
    assert parsed.payload.limit == 10
    assert parsed.payload.scope is LogScope.PLUGIN


def test_request_params_model_mismatch() -> None:
    with pytest.raises(ProtocolError) as exc:
        build_request(
            Command.GET_STATE,
            AddAccountParams(
                account=AccountInput.model_validate(
                    {
                        "platform": "funpay",
                        "label": "x",
                        "credentials": {"platform": "funpay", "golden_key": "abc"},
                    }
                )
            ),
        )
    assert exc.value.code == ErrorCode.INVALID_REQUEST


def test_all_commands_have_params_and_results() -> None:
    """Регрессия: новая команда обязана попасть в оба реестра и собираться в envelope."""
    for command in Command:
        assert command in COMMAND_PARAMS
        assert command in COMMAND_RESULTS
        request = build_request(command, SAMPLE_PARAMS[command])
        parsed = parse_request(request.model_dump_json())
        assert parsed.payload.method == command.value


def test_response_roundtrip_typed_result(state_with_credentials: StateWithCredentials) -> None:
    request = build_request(Command.GET_STATE, GetStateParams())
    response = build_response(request, state_with_credentials)
    assert response.id == request.id
    assert response.payload.error is None

    parsed = parse_envelope(response.model_dump_json())
    assert isinstance(parsed, Response)
    result = response_result(parsed, Command.GET_STATE)
    assert isinstance(result, StateWithCredentials)
    assert result.revision == state_with_credentials.revision
    # креды раскрыты для панели: канал аутентифицирован, панель восстанавливает сессии
    creds = result.credentials_by_account()["acc-1"]
    assert creds.secret_value() == "gk-live-123"


def test_state_event_masks_credentials(state_with_credentials: StateWithCredentials) -> None:
    """Событие ``state`` (broadcast) несёт StateSnapshot без кредов: секреты видны только по запросу."""
    snapshot = StateSnapshot.model_validate(
        {
            "revision": state_with_credentials.revision,
            "generated_at": state_with_credentials.generated_at,
            "status": state_with_credentials.status.model_dump(mode="json"),
            "accounts": [
                {
                    "id": entry.id,
                    "platform": entry.account.platform,
                    "label": entry.account.label,
                    "status": SessionStatus.STOPPED,
                    "created_at": state_with_credentials.generated_at,
                    "updated_at": state_with_credentials.generated_at,
                }
                for entry in state_with_credentials.accounts
            ],
        }
    )
    event = build_event(ServerEvent.STATE, snapshot)
    assert "gk-live-123" not in event.model_dump_json()


def test_ping_pong_roundtrip() -> None:
    """Heartbeat: nonce эхо + ревизия снапшота (панель ловит пропущенные state)."""
    request = build_request(Command.PING, PingParams(nonce="abc"))
    response = build_response(
        request,
        PingResult(nonce="abc", server_time=datetime(2026, 1, 15, 12, 0, tzinfo=UTC), revision=42),
    )
    parsed = parse_envelope(response.model_dump_json())
    assert isinstance(parsed, Response)
    pong = response_result(parsed, Command.PING)
    assert isinstance(pong, PingResult)
    assert pong.nonce == "abc"
    assert pong.revision == 42


def test_response_wrong_result_model_rejected(snapshot: StateSnapshot) -> None:
    request = build_request(Command.GET_STATE, GetStateParams())
    with pytest.raises(ProtocolError) as exc:
        build_response(request, PluginRef(plugin_id="x"))
    assert exc.value.code == ErrorCode.INTERNAL


def test_error_response() -> None:
    request = build_request(Command.ROTATE_TOKEN)
    response = build_error_response(request, ProtocolError(ErrorCode.RATE_LIMITED, "slow down"))
    parsed = parse_envelope(response.model_dump_json())
    assert isinstance(parsed, Response)
    assert response_error(parsed) is not None
    with pytest.raises(ProtocolError) as exc:
        response_result(parsed, Command.ROTATE_TOKEN)
    assert exc.value.code == ErrorCode.RATE_LIMITED


def test_error_response_wraps_generic_exception() -> None:
    response = build_error_response("some-id", RuntimeError("boom"))
    assert response.payload.error is not None
    assert response.payload.error.code is ErrorCode.INTERNAL
    assert response.id == "some-id"


def test_response_payload_requires_exactly_one() -> None:
    with pytest.raises(ValidationError):
        Response.model_validate({"id": "x", "payload": {}})
    with pytest.raises(ValidationError):
        Response.model_validate({"id": "x", "payload": {"result": {"a": 1}, "error": {"code": "x", "message": "y"}}})


def test_event_roundtrip_status(status: WorkerStatus) -> None:
    event = build_event(ServerEvent.STATUS, status)
    raw = json.loads(event.model_dump_json())
    assert raw["payload"]["event"] == "status"
    parsed = parse_envelope(event.model_dump_json())
    assert isinstance(parsed, Event)
    data = event_data(parsed)
    assert isinstance(data, WorkerStatus)
    assert data.uptime_seconds == status.uptime_seconds


def test_event_roundtrip_hub(hub_event) -> None:  # type: ignore[no-untyped-def]
    parsed = parse_envelope(build_event(ServerEvent.HUB, hub_event).model_dump_json())
    assert isinstance(parsed, Event)
    data = event_data(parsed)
    assert data == hub_event


def test_unknown_event_rejected() -> None:
    with pytest.raises(ProtocolError) as exc:
        build_event("no_such_event", {})
    assert exc.value.code == ErrorCode.METHOD_NOT_FOUND


def test_parse_invalid_json() -> None:
    with pytest.raises(ProtocolError) as exc:
        parse_envelope("{broken")
    assert exc.value.code == ErrorCode.PARSE_ERROR


def test_parse_unknown_envelope_type() -> None:
    with pytest.raises(ProtocolError):
        parse_envelope({"type": "magic", "id": "1", "ts": "2026-01-01T00:00:00Z", "payload": {}})


def test_parse_unknown_command_method() -> None:
    with pytest.raises(ProtocolError):
        parse_envelope(
            {
                "type": "request",
                "id": "1",
                "ts": "2026-01-01T00:00:00Z",
                "payload": {"method": "launch_missiles"},
            }
        )


def test_account_credentials_validation() -> None:
    # funpay без golden_key — нет
    with pytest.raises(ValidationError):
        AccountCredentials(platform=Platform.FUNPAY)
    # чужие поля не подмешиваются
    with pytest.raises(ValidationError):
        AccountCredentials(platform=Platform.FUNPAY, golden_key=SecretStr("k"), access_token=SecretStr("leak"))
    # ggsel требует seller_id
    with pytest.raises(ValidationError):
        AccountCredentials(platform=Platform.GGSEL, api_key=SecretStr("k"))
    creds = AccountCredentials(platform=Platform.GGSEL, api_key=SecretStr("k"), seller_id="77")
    assert creds.secret_value() == "k"


def test_account_credentials_hidden_in_repr() -> None:
    creds = AccountCredentials(platform=Platform.PLAYEROK, access_token=SecretStr("super-secret-token"))
    # repr/str и JSON-дамп (который видит панель в state) маскируют секрет
    assert "super-secret-token" not in repr(creds)
    assert "super-secret-token" not in str(creds)
    assert "**********" in creds.model_dump_json()
    # но для провода (панель → воркер) to_wire раскрывает значение
    assert to_wire(creds)["access_token"] == "super-secret-token"


def test_proxy_password_unwrapped_for_wire() -> None:
    creds = AccountInput.model_validate(
        {
            "platform": "funpay",
            "label": "x",
            "credentials": {"platform": "funpay", "golden_key": "gk"},
            "proxy": {"scheme": "socks5", "host": "1.2.3.4", "port": 1080, "username": "u", "password": "p@ss"},
        }
    )
    wire = to_wire(creds)
    assert wire["proxy"]["password"] == "p@ss"
    assert wire["credentials"]["golden_key"] == "gk"
    # URL для aiosellers
    assert creds.proxy is not None
    assert creds.proxy.to_url() == "socks5://u:p%40ss@1.2.3.4:1080"


def test_account_input_requires_matching_platform() -> None:
    with pytest.raises(ValidationError):
        AccountInput.model_validate(
            {
                "platform": "playerok",
                "label": "x",
                "credentials": {"platform": "funpay", "golden_key": "k"},
            }
        )


def test_restart_result_roundtrip() -> None:
    request = build_request(Command.RESTART)
    response = build_response(request, RestartResult(scheduled=True, delay_seconds=1.5))
    parsed = parse_envelope(response.model_dump_json())
    assert isinstance(parsed, Response)
    result = response_result(parsed, Command.RESTART)
    assert isinstance(result, RestartResult)
    assert result.delay_seconds == 1.5


def test_account_ref_result() -> None:
    request = build_request(Command.REMOVE_ACCOUNT, RemoveAccountParams(account_id="acc-1"))
    response = build_response(request, AccountRef(account_id="acc-1"))
    parsed = parse_envelope(response.model_dump_json())
    assert isinstance(parsed, Response)
    result = response_result(parsed, Command.REMOVE_ACCOUNT)
    assert isinstance(result, AccountRef)
    assert result.account_id == "acc-1"
