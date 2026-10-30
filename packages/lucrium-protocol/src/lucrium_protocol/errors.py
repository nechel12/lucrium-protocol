"""Ошибки протокола: коды, объект ошибки envelope и исключение."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_ERROR_MESSAGE = 2000
"""Ограничение ``ErrorObject.message``/``RpcError.message`` на проводе."""
MAX_ERROR_DATA_BYTES = 64 * 1024
"""Максимальный сериализованный размер ``ErrorObject.data``."""


class ErrorCode(StrEnum):
    """Коды ошибок протокола панель ↔ воркер (в ``Response.payload.error.code``)."""

    PARSE_ERROR = "parse_error"
    INVALID_REQUEST = "invalid_request"
    METHOD_NOT_FOUND = "method_not_found"
    INVALID_PARAMS = "invalid_params"
    UNAUTHORIZED = "unauthorized"
    RATE_LIMITED = "rate_limited"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"
    INTERNAL = "internal"


class ErrorObject(BaseModel):
    """Тело ошибки в ``Response``-envelope."""

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str = Field(min_length=1, max_length=MAX_ERROR_MESSAGE)
    data: dict[str, Any] | None = None

    @field_validator("message", mode="before")
    @classmethod
    def _truncate_message(cls, value: object) -> str:
        return (str(value) or "error")[:MAX_ERROR_MESSAGE]

    @field_validator("data", mode="before")
    @classmethod
    def _limit_data(cls, value: Any) -> Any:
        if value is None:
            return None
        try:
            encoded = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"error data must be JSON-serializable: {exc}") from exc
        if len(encoded.encode("utf-8")) > MAX_ERROR_DATA_BYTES:
            return {"truncated": True}
        return value

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class ProtocolError(Exception):
    """Ошибка протокола, полученная от удалённой стороны (или локально сгенерированная)."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        safe_message = (message or "error")[:MAX_ERROR_MESSAGE]
        super().__init__(f"{code}: {safe_message}")
        self.code = code
        self.message = safe_message
        self.data = data

    def to_object(self) -> ErrorObject:
        return ErrorObject(code=self.code, message=self.message, data=self.data)

    @classmethod
    def from_object(cls, obj: ErrorObject) -> ProtocolError:
        return cls(obj.code, obj.message, obj.data)

    @classmethod
    def internal(cls, exc: Exception) -> ProtocolError:
        """Оборачивает произвольное исключение в ошибку протокола (без утечки traceback наружу)."""
        return cls(ErrorCode.INTERNAL, f"{type(exc).__name__}: {exc}")


__all__ = [
    "MAX_ERROR_DATA_BYTES",
    "MAX_ERROR_MESSAGE",
    "ErrorCode",
    "ErrorObject",
    "ProtocolError",
]
