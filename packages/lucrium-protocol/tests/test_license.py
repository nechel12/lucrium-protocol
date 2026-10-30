"""Тесты JWT-лицензий и ревок-листов (спека §6.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from lucrium_protocol.license import (
    DEFAULT_OFFLINE_GRACE_SECONDS,
    LicenseClaims,
    LicenseError,
    RevocationList,
    RevokedLicense,
    decode_license,
    decode_license_unverified,
    encode_license,
    is_valid,
    sign_revocation_list,
    verify_revocation_list,
)

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    private = Ed25519PrivateKey.generate()
    return private, private.public_key()


def _claims(**overrides: object) -> LicenseClaims:
    data: dict[str, object] = {
        "plugin_id": "auto-reply-ai",
        "licensee": "buyer@example.com",
        "issued_at": int(NOW.timestamp()),
        "expires_at": int((NOW + timedelta(days=30)).timestamp()),
        "tier": "pro",
    }
    data.update(overrides)
    return LicenseClaims.model_validate(data)


def test_encode_decode_roundtrip() -> None:
    private, public = _keypair()
    claims = _claims()
    token = encode_license(claims, private)
    decoded = decode_license(token, public, now=NOW)
    assert decoded == claims
    assert decoded.tier == "pro"
    assert decoded.jti == claims.jti


def test_lifetime_license() -> None:
    claims = _claims(expires_at=None)
    assert claims.is_lifetime
    assert not claims.is_expired(NOW + timedelta(days=10_000))
    assert claims.within_offline_grace(NOW + timedelta(days=10_000))


def test_expiry_and_grace_window() -> None:
    expires = NOW + timedelta(hours=1)
    claims = _claims(expires_at=int(expires.timestamp()))
    # ещё не просрочена
    assert not claims.is_expired(expires - timedelta(seconds=1))
    # просрочена 1 час назад — внутри offline-grace 72 ч
    just_expired = expires + timedelta(seconds=1)
    assert claims.is_expired(just_expired)
    assert claims.within_offline_grace(just_expired)
    # просрочена дольше grace
    too_late = expires + timedelta(seconds=DEFAULT_OFFLINE_GRACE_SECONDS + 1)
    assert not claims.within_offline_grace(too_late)


def test_expired_license_rejected_by_decode() -> None:
    private, public = _keypair()
    claims = _claims(expires_at=int((NOW - timedelta(seconds=1)).timestamp()))
    token = encode_license(claims, private)
    with pytest.raises(LicenseError, match="expired"):
        decode_license(token, public, now=NOW)
    # offline-grace разрешает работу ещё 72 часа
    decoded = decode_license(token, public, now=NOW, grace_seconds=DEFAULT_OFFLINE_GRACE_SECONDS)
    assert decoded.is_expired(NOW)


def test_wrong_issuer_rejected() -> None:
    private, public = _keypair()
    claims = _claims(iss="other-issuer")
    token = encode_license(claims, private)
    with pytest.raises(LicenseError, match="issuer"):
        decode_license(token, public)


def test_revocation_list_freshness_and_is_valid() -> None:
    private, public = _keypair()
    claims = _claims()
    revocations = RevocationList(
        issued_at=NOW - timedelta(days=2),
        revocations=[RevokedLicense(jti=claims.jti, revoked_at=NOW)],
    )
    sign_revocation_list(revocations, private)
    verify_revocation_list(revocations, public, now=NOW)
    with pytest.raises(LicenseError, match="stale"):
        verify_revocation_list(revocations, public, now=NOW, max_age_seconds=3600)
    assert not is_valid(claims, NOW, revocations)


def test_tampered_token_rejected() -> None:
    private, public = _keypair()
    token = encode_license(_claims(), private)
    head, body, sig = token.split(".")
    tampered = f"{head}.{body}x.{sig}"
    with pytest.raises(LicenseError):
        decode_license(tampered, public)


def test_wrong_key_rejected() -> None:
    private, _ = _keypair()
    _, other_public = _keypair()
    token = encode_license(_claims(), private)
    with pytest.raises(LicenseError):
        decode_license(token, other_public)


def test_unverified_decode_is_diagnostics_only() -> None:
    private, _ = _keypair()
    token = encode_license(_claims(plugin_id="some-plugin"), private)
    claims = decode_license_unverified(token)
    assert claims.plugin_id == "some-plugin"
    # невалидный JSON-совместимый мусор
    with pytest.raises(LicenseError):
        decode_license_unverified("not-a-jwt")


def test_revocation_list_sign_verify() -> None:
    private, public = _keypair()
    revocations = RevocationList(
        issued_at=NOW,
        revocations=[
            RevokedLicense(jti="abc123", plugin_id="auto-reply-ai", reason="refund", revoked_at=NOW),
        ],
    )
    sign_revocation_list(revocations, private)
    verify_revocation_list(revocations, public)
    assert revocations.is_revoked("abc123")
    assert not revocations.is_revoked("other")


def test_revocation_list_tamper_detected() -> None:
    private, public = _keypair()
    revocations = RevocationList(issued_at=NOW, revocations=[])
    sign_revocation_list(revocations, private)
    revocations.revocations.append(RevokedLicense(jti="sneaky", revoked_at=NOW))
    with pytest.raises(LicenseError):
        verify_revocation_list(revocations, public)


def test_revocation_list_requires_signature() -> None:
    _, public = _keypair()
    with pytest.raises(LicenseError):
        verify_revocation_list(RevocationList(issued_at=NOW), public)
