"""Ошибки протокола: коды, объект ошибки envelope и исключение."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
    message: str = Field(min_length=1, max_length=2000)
    data: dict[str, Any] | None = None

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
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
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
    "ErrorCode",
    "ErrorObject",
    "ProtocolError",
]
