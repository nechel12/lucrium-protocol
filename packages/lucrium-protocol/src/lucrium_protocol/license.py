"""JWT-лицензии плагинов и ревок-листы (спека §6.2, план v2).

Лицензия = подписанный JWT. Claims (по спеке): ``plugin_id``, ``licensee``,
``issued_at``, ``expires_at`` (``null`` = пожизненная), ``tier``; дополнительно
``jti`` (id лицензии — нужен для ревок-листов и watermark) и ``iss``.

Подпись — EdDSA (Ed25519), ключ магазина. Проверка офлайн, без интеграции
с API магазина: контейнер-продажник и воркер верифицируют публичным ключом.
Магазин раз в сутки публикует подписанный ревок-лист (возвраты, баны).

Offline-grace: при недоступном сервере доставки плагины работают из кэша
ещё 72 часа после ``valid_until``, доставка ретраится в фоне.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import jwt
from jwt import InvalidTokenError
from pydantic import BaseModel, ConfigDict, Field, field_validator

ISSUER = "lucrium-market"
DEFAULT_OFFLINE_GRACE_SECONDS = 72 * 3600
DEFAULT_ALGORITHM = "EdDSA"


class LicenseError(Exception):
    """Лицензия невалидна: подпись, срок, формат или ревок."""


class LicenseClaims(BaseModel):
    """Claims лицензии плагина (JWT payload)."""

    model_config = ConfigDict(extra="forbid")

    plugin_id: str = Field(min_length=1, max_length=200)
    licensee: str = Field(min_length=1, max_length=320)
    issued_at: int = Field(ge=0, description="epoch seconds")
    expires_at: int | None = Field(default=None, description="epoch seconds; null = пожизненная")
    tier: str = Field(default="standard", min_length=1, max_length=64)
    jti: str = Field(default_factory=lambda: uuid.uuid4().hex, max_length=128)
    iss: str = ISSUER

    @field_validator("plugin_id", "licensee", "tier")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v

    @property
    def is_lifetime(self) -> bool:
        return self.expires_at is None

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        current = now or datetime.now(UTC)
        return int(current.timestamp()) > self.expires_at

    def expires_at_dt(self) -> datetime | None:
        return None if self.expires_at is None else datetime.fromtimestamp(self.expires_at, UTC)

    def within_offline_grace(
        self,
        now: datetime | None = None,
        grace_seconds: int = DEFAULT_OFFLINE_GRACE_SECONDS,
    ) -> bool:
        """True, если лицензия просрочена, но не дольше offline-grace (по умолчанию 72 ч)."""
        if self.expires_at is None:
            return True
        current = now or datetime.now(UTC)
        return 0 <= int(current.timestamp()) - self.expires_at <= grace_seconds


def encode_license(
    claims: LicenseClaims,
    private_key: Any,
    algorithm: str = DEFAULT_ALGORITHM,
) -> str:
    """Подписывает лицензию приватным ключом магазина и возвращает JWT."""
    return jwt.encode(
        claims.model_dump(mode="json"),
        private_key,
        algorithm=algorithm,
    )


def decode_license(
    token: str,
    public_key: Any,
    algorithms: tuple[str, ...] = (DEFAULT_ALGORITHM,),
) -> LicenseClaims:
    """Верифицирует подпись и возвращает claims; при любой ошибке — :class:`LicenseError`."""
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            public_key,
            algorithms=list(algorithms),
            options={"require": ["plugin_id", "licensee", "issued_at", "jti", "iss"]},
        )
        return LicenseClaims.model_validate(payload)
    except InvalidTokenError as exc:
        raise LicenseError(f"invalid license: {exc}") from exc
    except ValueError as exc:  # невалидный ключ
        raise LicenseError(f"invalid verification key: {exc}") from exc


def decode_license_unverified(token: str) -> LicenseClaims:
    """Разбирает JWT без проверки подписи — только для диагностики/логов. НЕ для решений о доступе."""
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            options={"verify_signature": False},
            algorithms=[],
        )
        return LicenseClaims.model_validate(payload)
    except (InvalidTokenError, ValueError) as exc:
        raise LicenseError(f"malformed license: {exc}") from exc


class RevokedLicense(BaseModel):
    """Запись отзыва в ревок-листе."""

    model_config = ConfigDict(extra="forbid")

    jti: str = Field(min_length=1, max_length=128)
    plugin_id: str | None = Field(default=None, max_length=200)
    reason: str | None = Field(default=None, max_length=500)
    revoked_at: datetime


class RevocationList(BaseModel):
    """Подписанный ревок-лист магазина (публикуется раз в сутки)."""

    model_config = ConfigDict(extra="forbid")

    issued_at: datetime
    revocations: list[RevokedLicense] = Field(default_factory=list)
    signature: str | None = Field(default=None, description="Ed25519, base64; снимается при проверке")

    def is_revoked(self, jti: str) -> bool:
        return any(entry.jti == jti for entry in self.revocations)

    def revoked_jtis(self) -> set[str]:
        return {entry.jti for entry in self.revocations}


def _canonical_payload(revocations: RevocationList) -> bytes:
    """Канонические байты, которые подписывает/верифицирует магазин (без поля signature)."""
    payload = {
        "issued_at": revocations.issued_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "revocations": [
            {
                "jti": e.jti,
                "plugin_id": e.plugin_id,
                "reason": e.reason,
                "revoked_at": e.revoked_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            }
            for e in revocations.revocations
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sign_revocation_list(revocations: RevocationList, private_key: Any) -> str:
    """Подписывает ревок-лист и записывает ``signature`` (base64, Ed25519); возвращает подпись."""
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = (
        private_key
        if isinstance(private_key, Ed25519PrivateKey)
        else Ed25519PrivateKey.from_private_bytes(
            private_key if isinstance(private_key, bytes) else str(private_key).encode()
        )
    )
    signature = base64.b64encode(key.sign(_canonical_payload(revocations))).decode()
    revocations.signature = signature
    return signature


def verify_revocation_list(revocations: RevocationList, public_key: Any) -> None:
    """Проверяет подпись ревок-листа; :class:`LicenseError`, если подписи нет или она неверна."""
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if not revocations.signature:
        raise LicenseError("revocation list is not signed")
    key = (
        public_key
        if isinstance(public_key, Ed25519PublicKey)
        else Ed25519PublicKey.from_public_bytes(
            public_key if isinstance(public_key, bytes) else str(public_key).encode()
        )
    )
    try:
        key.verify(base64.b64decode(revocations.signature, validate=True), _canonical_payload(revocations))
    except Exception as exc:
        raise LicenseError(f"revocation list signature mismatch: {exc}") from exc


def is_valid(claims: LicenseClaims, now: datetime | None = None) -> bool:
    """True, если лицензия не просрочена (без учёта grace) — удобный хелпер для контейнера-продажника."""
    return not claims.is_expired(now)


__all__ = [
    "DEFAULT_ALGORITHM",
    "DEFAULT_OFFLINE_GRACE_SECONDS",
    "ISSUER",
    "LicenseClaims",
    "LicenseError",
    "RevocationList",
    "RevokedLicense",
    "decode_license",
    "decode_license_unverified",
    "encode_license",
    "is_valid",
    "sign_revocation_list",
    "verify_revocation_list",
]
