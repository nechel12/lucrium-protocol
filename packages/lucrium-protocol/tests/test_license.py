"""Тесты JWT-лицензий и ревок-листов (спека §6.2)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from lucrium_protocol.license import (
    DEFAULT_OFFLINE_GRACE_SECONDS,
    DEFAULT_REVOCATION_MAX_AGE_SECONDS,
    MAX_EPOCH_SECONDS,
    MAX_REVOCATION_ENTRIES,
    LicenseClaims,
    LicenseError,
    RevocationList,
    RevokedLicense,
    VerifiedRevocations,
    decode_license,
    decode_license_unverified,
    encode_license,
    is_valid,
    load_revocation_list,
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


def _signed_list(
    private: Ed25519PrivateKey,
    entries: list[RevokedLicense] | None = None,
    **overrides: object,
) -> RevocationList:
    """Ревок-лист, подписанный ``private`` (issued_at по умолчанию — NOW)."""
    overrides.setdefault("issued_at", NOW)
    return sign_revocation_list(entries or [], private, **overrides)  # type: ignore[arg-type]


def _raw(revocations: RevocationList) -> str:
    return revocations.model_dump_json()


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
    # grace не меняет ответ is_valid(): просроченная лицензия невалидна
    assert not is_valid(claims, just_expired)


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


def test_claims_epoch_bounds() -> None:
    """Отрицательных сроков и «веса до 9999 года» не бывает."""
    for expires_at in (-1, MAX_EPOCH_SECONDS + 1):
        with pytest.raises(ValueError):
            _claims(expires_at=expires_at)
    with pytest.raises(ValueError):
        _claims(issued_at=-1)
    with pytest.raises(ValueError):
        _claims(issued_at=MAX_EPOCH_SECONDS + 1)


def test_decode_pins_eddsa_and_rejects_alg_substitution() -> None:
    """Классическая подмена alg: HMAC-секрет = публичный ключ, и «none»."""
    _, public = _keypair()
    payload = _claims().model_dump(mode="json")
    raw_public = public.public_bytes_raw()

    hs256 = jwt.encode(payload, raw_public, algorithm="HS256")
    with pytest.raises(LicenseError, match="unsupported license algorithm"):
        decode_license(hs256, raw_public)

    none_token = jwt.encode(payload, None, algorithm="none")
    with pytest.raises(LicenseError, match="unsupported license algorithm"):
        decode_license(none_token, public)

    # а подменённый только заголовок (EdDSA в header, HS256 в подписи) не проходит
    _, body, _ = hs256.split(".")
    forged_head = base64.urlsafe_b64encode(json.dumps({"alg": "EdDSA", "typ": "JWT"}).encode()).decode().rstrip("=")
    with pytest.raises(LicenseError):
        decode_license(f"{forged_head}.{body}.", public)


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
    revocations = _signed_list(
        private,
        [RevokedLicense(jti="abc123", plugin_id="auto-reply-ai", reason="refund", revoked_at=NOW)],
    )
    verify_revocation_list(revocations, public)
    assert revocations.is_revoked("abc123")
    assert not revocations.is_revoked("other")


def test_revocation_list_tamper_detected() -> None:
    private, public = _keypair()
    revocations = _signed_list(private)
    revocations.revocations.append(RevokedLicense(jti="sneaky", revoked_at=NOW))
    with pytest.raises(LicenseError):
        verify_revocation_list(revocations, public)


def test_revocation_list_signature_is_required() -> None:
    """Неподписанный лист не существует даже как модель."""
    _, public = _keypair()
    with pytest.raises(ValueError):
        RevocationList(issued_at=NOW, revocations=[])  # type: ignore[call-arg]
    with pytest.raises(LicenseError, match="invalid revocation list"):
        load_revocation_list(json.dumps({"issued_at": NOW.isoformat(), "revocations": []}), public_key=public)


def test_load_revocation_list_verifies_signature_and_freshness() -> None:
    private, public = _keypair()
    claims = _claims()
    revocations = _signed_list(private, [RevokedLicense(jti=claims.jti, revoked_at=NOW)])
    verified = load_revocation_list(_raw(revocations), public_key=public, now=NOW)
    assert isinstance(verified, VerifiedRevocations)
    assert verified.issued_at == NOW
    assert verified.is_revoked(claims.jti)

    _, other_public = _keypair()
    with pytest.raises(LicenseError, match="signature mismatch"):
        load_revocation_list(_raw(revocations), public_key=other_public, now=NOW)


def test_load_revocation_list_rejects_stale_by_default() -> None:
    """Свежесть проверяется по умолчанию: подменённый кэш не оживляет отзывы."""
    private, public = _keypair()
    old = _signed_list(private, issued_at=NOW - timedelta(seconds=DEFAULT_REVOCATION_MAX_AGE_SECONDS + 1))
    with pytest.raises(LicenseError, match="stale"):
        load_revocation_list(_raw(old), public_key=public, now=NOW)
    fresh = _signed_list(private, issued_at=NOW - timedelta(hours=1))
    assert load_revocation_list(_raw(fresh), public_key=public, now=NOW) is not None


def test_revocation_list_freshness_and_is_valid() -> None:
    private, public = _keypair()
    claims = _claims()
    revocations = _signed_list(
        private,
        [RevokedLicense(jti=claims.jti, revoked_at=NOW)],
        issued_at=NOW - timedelta(days=2),
    )
    verify_revocation_list(revocations, public, now=NOW)
    with pytest.raises(LicenseError, match="stale"):
        verify_revocation_list(revocations, public, now=NOW, max_age_seconds=3600)
    verified = load_revocation_list(_raw(revocations), public_key=public, now=NOW, max_age_seconds=3 * 24 * 3600)
    assert not is_valid(claims, NOW, verified)


def test_is_valid_accepts_only_verified_revocations() -> None:
    """Сырой RevocationList в is_valid не проходит — иначе проверка подписи обходится."""
    private, _ = _keypair()
    claims = _claims()
    revocations = _signed_list(private, [RevokedLicense(jti=claims.jti, revoked_at=NOW)])
    with pytest.raises(LicenseError, match="load_revocation_list"):
        is_valid(claims, NOW, revocations)  # type: ignore[arg-type]
    # без ревок-листа лицензия валидна, пока не просрочена
    assert is_valid(claims, NOW)
    assert not is_valid(claims, NOW + timedelta(days=31))


def test_revocation_list_entry_count_is_bounded() -> None:
    """Лист декодируется до проверки подписи — число записей ограничено."""
    schema = RevocationList.model_json_schema()
    assert schema["properties"]["signature"]["minLength"] == 1
    assert schema["properties"]["revocations"]["maxItems"] == MAX_REVOCATION_ENTRIES
    with pytest.raises(ValueError):
        RevocationList(
            issued_at=NOW,
            revocations=[RevokedLicense(jti=f"j-{i}", revoked_at=NOW) for i in range(MAX_REVOCATION_ENTRIES + 1)],
            signature="A" * 88,
        )
