"""Envelope протокола «панель ↔ воркер» (спека §5).

Формат: JSON ``{type, id, ts, payload}`` с тремя типами:

* ``request`` — команда панели; ``payload`` = ``{method, ...params}``
  (дискриминатор ``method``, модели — :mod:`lucrium_protocol.commands`);
* ``response`` — ответ воркера с тем же ``id``; ``payload`` = ``{result}``
  или ``{error}`` (ровно одно из двух);
* ``event`` — асинхронное событие воркера; ``payload`` = ``{event, data}``
  (дискриминатор ``event``, модели — :mod:`lucrium_protocol.events`).

Аутентификация — bearer pairing-токен в HTTP-заголовке WSS-рукопожатия
(:mod:`lucrium_protocol.auth`), не отдельным сообщением.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from .commands import Command, RequestPayloadT, decode_result, encode_result
from .errors import ErrorCode, ErrorObject, ProtocolError
from .events import EVENT_DATA, EventPayloadT, decode_event_data, encode_event_data
from .types import to_wire


def _now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    """UUID4 hex — correlation id запроса/ответа."""
    return uuid.uuid4().hex


class Envelope(BaseModel):
    """База всех сообщений: ``{type, id, ts, payload}``."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id, min_length=1, max_length=64)
    ts: datetime = Field(default_factory=_now, description="UTC время отправки")


class ResponsePayload(BaseModel):
    """Тело ответа: ровно один из ``result`` / ``error``."""

    model_config = ConfigDict(extra="forbid")

    result: dict[str, Any] | None = None
    error: ErrorObject | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> ResponsePayload:
        if (self.result is None) == (self.error is None):
            raise ValueError("response payload must contain exactly one of result/error")
        return self


class Request(Envelope):
    type: Literal["request"] = "request"
    payload: RequestPayloadT


class Response(Envelope):
    type: Literal["response"] = "response"
    payload: ResponsePayload


class Event(Envelope):
    type: Literal["event"] = "event"
    payload: EventPayloadT


EnvelopeT = Annotated[Request | Response | Event, Field(discriminator="type")]

_adapter: TypeAdapter[EnvelopeT] = TypeAdapter(EnvelopeT)


# --- конструкторы -------------------------------------------------------------


def build_request(
    method: str | Command,
    params: BaseModel | dict[str, Any] | None = None,
    *,
    id: str | None = None,
    ts: datetime | None = None,
) -> Request:
    """Собирает request-envelope из модели params (или готового dict)."""
    method_str = str(method)
    payload: dict[str, Any]
    if isinstance(params, BaseModel):
        payload = to_wire(params)
        if payload.get("method", method_str) != method_str:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST,
                f"params model {type(params).__name__} does not match method {method_str!r}",
            )
    elif params is None:
        payload = {"method": method_str}
    else:
        payload = {**params, "method": method_str}
    extra: dict[str, Any] = {}
    if id is not None:
        extra["id"] = id
    if ts is not None:
        extra["ts"] = ts
    try:
        return Request.model_validate({"payload": payload, **extra})
    except ValueError as exc:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, f"cannot build request {method_str}: {exc}") from exc


def build_response(
    request: Request | str,
    result: BaseModel | dict[str, Any],
    *,
    ts: datetime | None = None,
) -> Response:
    """Успешный ответ с ``id`` исходного запроса.

    Если передан исходный ``Request``, result-модель дополнительно сверяется
    с реестром :data:`~lucrium_protocol.commands.COMMAND_RESULTS`.
    """
    request_id = request.id if isinstance(request, Request) else request
    if isinstance(request, Request) and isinstance(result, BaseModel):
        data = encode_result(str(request.payload.method), result)
    elif isinstance(result, BaseModel):
        data = to_wire(result)
    else:
        data = result
    return _validated_response(request_id, {"result": data}, ts)


def build_error_response(
    request: Request | str,
    error: ErrorObject | ProtocolError | Exception,
    *,
    ts: datetime | None = None,
) -> Response:
    """Ответ с ошибкой (``ErrorCode`` из :mod:`lucrium_protocol.errors`)."""
    request_id = request.id if isinstance(request, Request) else request
    if isinstance(error, ErrorObject):
        obj = error
    elif isinstance(error, ProtocolError):
        obj = error.to_object()
    else:
        obj = ProtocolError.internal(error).to_object()
    return _validated_response(request_id, {"error": obj.model_dump(mode="json")}, ts)


def _validated_response(request_id: str, payload: dict[str, Any], ts: datetime | None) -> Response:
    extra: dict[str, Any] = {}
    if ts is not None:
        extra["ts"] = ts
    try:
        return Response.model_validate({"id": request_id, "payload": payload, **extra})
    except ValueError as exc:
        raise ProtocolError(ErrorCode.INTERNAL, f"cannot build response: {exc}") from exc


def build_event(
    event: str,
    data: BaseModel | dict[str, Any],
    *,
    id: str | None = None,
    ts: datetime | None = None,
) -> Event:
    """Собирает event-envelope (``event`` — имя из :class:`ServerEvent`)."""
    if str(event) not in EVENT_DATA:
        raise ProtocolError(ErrorCode.METHOD_NOT_FOUND, f"unknown event: {event}")
    payload_data = encode_event_data(event, data) if isinstance(data, BaseModel) else data
    extra: dict[str, Any] = {}
    if id is not None:
        extra["id"] = id
    if ts is not None:
        extra["ts"] = ts
    try:
        return Event.model_validate({"payload": {"event": str(event), "data": payload_data}, **extra})
    except ValueError as exc:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, f"cannot build event {event}: {exc}") from exc


# --- парсеры -----------------------------------------------------------------


def parse_envelope(raw: str | bytes | dict[str, Any]) -> EnvelopeT:
    """Парсит любое сообщение протокола; ошибки — :class:`ProtocolError`."""
    try:
        if isinstance(raw, dict):
            return _adapter.validate_python(raw)
        return _adapter.validate_json(raw)
    except ValueError as exc:
        code = ErrorCode.PARSE_ERROR if isinstance(raw, str | bytes) else ErrorCode.INVALID_REQUEST
        raise ProtocolError(code, f"malformed envelope: {exc}") from exc


def parse_request(raw: str | bytes | dict[str, Any]) -> Request:
    """Парсит только request; остальное — ``INVALID_REQUEST``."""
    envelope = parse_envelope(raw)
    if not isinstance(envelope, Request):
        raise ProtocolError(ErrorCode.INVALID_REQUEST, f"expected request, got {envelope.type}")
    return envelope


def response_result(response: Response, method: str | Command) -> BaseModel:
    """Достаёт и типизирует ``result`` ответа по методу исходного запроса."""
    if response.payload.error is not None:
        raise ProtocolError.from_object(response.payload.error)
    if response.payload.result is None:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, "response carries neither result nor error")
    return decode_result(method, response.payload.result)


def response_error(response: Response) -> ProtocolError | None:
    """``ProtocolError``, если ответ с ошибкой; ``None`` при успехе."""
    error = response.payload.error
    return None if error is None else ProtocolError.from_object(error)


def event_data(event: Event) -> BaseModel:
    """Типизирует ``payload.data`` события по его имени."""
    data: dict[str, Any] = dict(event.payload.data)
    return decode_event_data(str(event.payload.event), data)


__all__ = [
    "Envelope",
    "EnvelopeT",
    "Event",
    "Request",
    "Response",
    "ResponsePayload",
    "build_error_response",
    "build_event",
    "build_request",
    "build_response",
    "event_data",
    "new_id",
    "parse_envelope",
    "parse_request",
    "response_error",
    "response_result",
]
