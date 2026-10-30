"""Тесты pairing-токена (спека §3.1, §10)."""

from __future__ import annotations

from lucrium_protocol.auth import (
    MIN_TOKEN_HEX_CHARS,
    auth_header_value,
    extract_bearer_token,
    generate_pairing_token,
    is_valid_pairing_token_format,
    pairing_tokens_equal,
)


def test_generate_pairing_token_is_128bit_hex() -> None:
    token = generate_pairing_token()
    assert len(token) == MIN_TOKEN_HEX_CHARS
    assert is_valid_pairing_token_format(token)
    # криптостойкость: две генерации не совпадают
    assert generate_pairing_token() != token


def test_generate_pairing_token_rejects_too_small() -> None:
    try:
        generate_pairing_token(8)
    except ValueError as exc:
        assert "128" in str(exc)
    else:
        raise AssertionError("expected ValueError for <128 bits")


def test_token_format_validation() -> None:
    assert is_valid_pairing_token_format("a" * 32)
    assert is_valid_pairing_token_format("A1" * 16)
    assert not is_valid_pairing_token_format("a" * 31)  # короче 128 бит
    assert not is_valid_pairing_token_format("z" * 32)  # не hex
    assert not is_valid_pairing_token_format("abc")  # нечётная длина


def test_pairing_tokens_equal() -> None:
    token = generate_pairing_token()
    assert pairing_tokens_equal(token, token)
    assert not pairing_tokens_equal(token, generate_pairing_token())


def test_auth_header_roundtrip() -> None:
    token = generate_pairing_token()
    header = auth_header_value(token)
    assert header.startswith("Bearer ")
    assert extract_bearer_token(header) == token


def test_extract_bearer_token_edge_cases() -> None:
    assert extract_bearer_token(None) is None
    assert extract_bearer_token("") is None
    assert extract_bearer_token("Basic dXNlcjpwYXNz") is None
    assert extract_bearer_token("Bearer   ") is None
    assert extract_bearer_token("bearer abc123") == "abc123"
