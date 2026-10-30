"""Pairing-токен панель ↔ воркер (см. спеку §3.1).

Токен генерирует ``install.sh`` на VPS (криптостойкий, 128+ бит, hex),
печатает `IP:PORT + токен`; панель передаёт его как bearer в HTTP-заголовке
WSS-рукопожатия. Ротация: старый токен отзывается, новый печатается в терминале.

Восстановления пароля нет: кто имеет доступ к VPS — владелец.
"""

from __future__ import annotations

import secrets
from binascii import Error as BinasciiError
from binascii import unhexlify

TOKEN_BYTES = 16
"""16 байт = 128 бит — минимум по спеке §10."""

MIN_TOKEN_HEX_CHARS = TOKEN_BYTES * 2

AUTH_SCHEME = "Bearer"


def generate_pairing_token(nbytes: int = TOKEN_BYTES) -> str:
    """Генерирует криптостойкий hex-токен (по умолчанию 128 бит)."""
    if nbytes < TOKEN_BYTES:
        raise ValueError(f"pairing token must be >= {TOKEN_BYTES} bytes ({TOKEN_BYTES * 8} bits), got {nbytes}")
    return secrets.token_hex(nbytes)


def is_valid_pairing_token_format(token: str) -> bool:
    """Проверяет формат токена (hex, >= 128 бит). Криптостойкость — свойство генерации, не формата."""
    if len(token) < MIN_TOKEN_HEX_CHARS or len(token) % 2 != 0:
        return False
    try:
        unhexlify(token)
    except (BinasciiError, ValueError):
        return False
    return True


def pairing_tokens_equal(a: str, b: str) -> bool:
    """Сравнение токенов за константное время (защита от timing-атак)."""
    return secrets.compare_digest(a, b)


def auth_header_value(token: str) -> str:
    """``Authorization: Bearer <token>`` для WSS-рукопожатия."""
    return f"{AUTH_SCHEME} {token}"


def extract_bearer_token(header_value: str | None) -> str | None:
    """Достаёт токен из заголовка ``Authorization``; ``None``, если схема не Bearer."""
    if not header_value:
        return None
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != AUTH_SCHEME.lower():
        return None
    token = parts[1].strip()
    return token or None


__all__ = [
    "AUTH_SCHEME",
    "MIN_TOKEN_HEX_CHARS",
    "TOKEN_BYTES",
    "auth_header_value",
    "extract_bearer_token",
    "generate_pairing_token",
    "is_valid_pairing_token_format",
    "pairing_tokens_equal",
]
