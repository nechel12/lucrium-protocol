"""JWT-лицензии плагинов и ревок-листы (спека §6.2, план v2).

Лицензия = подписанный JWT. Claims (по спеке): ``plugin_id``, ``licensee``,
``issued_at``, ``expires_at`` (``null`` = пожизненная), ``tier``; дополнительно
``jti`` (id лицензии — нужен для ревок-листов и watermark) и ``iss``.

Подпись — только EdDSA (Ed25519), ключ магазина; ``alg`` из заголовка
проверяется явно, подмена алгоритма (``none``/``HS256`` с публичным ключом в
качестве HMAC-секрета) отклоняется. Проверка офлайн, без интеграции с API
магазина: контейнер-продажник и воркер верифицируют публичным ключом.
Магазин раз в сутки публикует подписанный ревок-лист (возвраты, баны).

Ревок-лист: подпись обязательна (:class:`RevocationList.signature`), а в
:func:`is_valid` попадает только :class:`VerifiedRevocations` — объект,
который даёт :func:`load_revocation_list`, проверивший подпись и свежесть.
Неподписанный/подменённый список (кэш, MITM без пининга) не может вернуть
отозванную лицензию в рабочее состояние.

Offline-grace: при недоступном сервере доставки плагины работают из кэша
ещё 72 часа после ``valid_until``, доставка ретраится в фоне.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from jwt import InvalidTokenError
from pydantic import BaseModel, ConfigDict, Field, field_validator

ISSUER = "lucrium-market"
DEFAULT_OFFLINE_GRACE_SECONDS = 72 * 3600
DEFAULT_ALGORITHM = "EdDSA"
# магазин публикует лист раз в сутки (спека §6.2): два интервала + запас на задержку
DEFAULT_REVOCATION_MAX_AGE_SECONDS = 48 * 3600
# границы epoch: 0 .. 2100-01-01 — отрицательных и «на всегда» сроков не бывает
MIN_EPOCH_SECONDS = 0
MAX_EPOCH_SECONDS = 4_102_444_800
# ответ магазина тоже декодируется до проверки подписи — список записей ограничиваем
MAX_REVOCATION_ENTRIES = 50_000
MAX_SIGNATURE_B64_CHARS = 128


class LicenseError(Exception):
    """Лицензия невалидна: подпись, срок, формат или ревок."""


class LicenseClaims(BaseModel):
    """Claims лицензии плагина (JWT payload)."""

    model_config = ConfigDict(extra="forbid")

    plugin_id: str = Field(min_length=1, max_length=200)
    licensee: str = Field(min_length=1, max_length=320)
    issued_at: int = Field(ge=MIN_EPOCH_SECONDS, le=MAX_EPOCH_SECONDS, description="epoch seconds")
    expires_at: int | None = Field(
        default=None,
        ge=MIN_EPOCH_SECONDS,
        le=MAX_EPOCH_SECONDS,
        description="epoch seconds; null = пожизненная",
    )
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


def encode_license(claims: LicenseClaims, private_key: Any) -> str:
    """Подписывает лицензию приватным ключом магазина (EdDSA) и возвращает JWT."""
    return jwt.encode(
        claims.model_dump(mode="json"),
        private_key,
        algorithm=DEFAULT_ALGORITHM,
    )


def decode_license(
    token: str,
    public_key: Any,
    *,
    expected_issuer: str | None = ISSUER,
    now: datetime | None = None,
    grace_seconds: int = 0,
) -> LicenseClaims:
    """Верифицирует подпись, издателя и срок; при любой ошибке — :class:`LicenseError`.

    Алгоритм зафиксирован (:data:`DEFAULT_ALGORITHM`): ``algorithms`` вызывающей
    стороны нет, чтобы нельзя было согласовать ``none`` или HMAC-ключ из публичного
    ключа. ``grace_seconds`` — допустимое окно после ``expires_at`` (offline-grace);
    по умолчанию 0, то есть просроченная лицензия отклоняется.
    """
    try:
        header = jwt.get_unverified_header(token)
    except Exception as exc:
        raise LicenseError(f"invalid license: {exc}") from exc
    if header.get("alg") != DEFAULT_ALGORITHM:
        raise LicenseError(f"unsupported license algorithm: {header.get('alg')!r} (only {DEFAULT_ALGORITHM!r})")

    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            public_key,
            algorithms=[DEFAULT_ALGORITHM],
            options={"require": ["plugin_id", "licensee", "issued_at", "jti", "iss"]},
        )
        claims = LicenseClaims.model_validate(payload)
    except InvalidTokenError as exc:
        raise LicenseError(f"invalid license: {exc}") from exc
    except ValueError as exc:  # невалидный ключ/claims
        raise LicenseError(f"invalid license: {exc}") from exc

    if expected_issuer is not None and claims.iss != expected_issuer:
        raise LicenseError(f"unexpected license issuer: {claims.iss!r}")
    if claims.expires_at is not None:
        current = now or datetime.now(UTC)
        if int(current.timestamp()) > claims.expires_at + max(0, grace_seconds):
            raise LicenseError(f"license expired at {claims.expires_at_dt().isoformat()}")  # type: ignore[union-attr]
    return claims


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

    @field_validator("revoked_at")
    @classmethod
    def _revoked_at_utc(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class RevocationList(BaseModel):
    """Подписанный ревок-лист магазина (публикуется раз в сутки).

    ``signature`` обязателен: неподписанный список — обычный JSON, который
    подменяет кто угодно с доступом к кэшу или к каналу загрузки, и отозванные
    лицензии снова становятся «валидными».
    """

    model_config = ConfigDict(extra="forbid")

    issued_at: datetime
    revocations: list[RevokedLicense] = Field(default_factory=list, max_length=MAX_REVOCATION_ENTRIES)
    signature: str = Field(
        min_length=1,
        max_length=MAX_SIGNATURE_B64_CHARS,
        description="Ed25519, base64; обязателен",
    )

    @field_validator("issued_at")
    @classmethod
    def _issued_at_utc(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def is_revoked(self, jti: str) -> bool:
        return any(entry.jti == jti for entry in self.revocations)

    def revoked_jtis(self) -> set[str]:
        return {entry.jti for entry in self.revocations}


def _canonical_payload(issued_at: datetime, revocations: Sequence[RevokedLicense]) -> bytes:
    """Канонические байты, которые подписывает/верифицирует магазин (без поля signature)."""
    payload = {
        "issued_at": issued_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "revocations": [
            {
                "jti": e.jti,
                "plugin_id": e.plugin_id,
                "reason": e.reason,
                "revoked_at": e.revoked_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            }
            for e in revocations
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _as_private_key(private_key: Any) -> Ed25519PrivateKey:
    if isinstance(private_key, Ed25519PrivateKey):
        return private_key
    return Ed25519PrivateKey.from_private_bytes(
        private_key if isinstance(private_key, bytes) else str(private_key).encode()
    )


def _as_public_key(public_key: Any) -> Ed25519PublicKey:
    if isinstance(public_key, Ed25519PublicKey):
        return public_key
    return Ed25519PublicKey.from_public_bytes(public_key if isinstance(public_key, bytes) else str(public_key).encode())


def sign_revocation_list(
    revocations: Sequence[RevokedLicense],
    private_key: Any,
    *,
    issued_at: datetime | None = None,
) -> RevocationList:
    """Собирает и подписывает ревок-лист (base64, Ed25519).

    Другого способа получить :class:`RevocationList` нет — модель требует
    подпись, поэтому «неподписанный лист» не существует даже случайно.
    """
    moment = (issued_at or datetime.now(UTC)).astimezone(UTC)
    entries = list(revocations)
    signature = base64.b64encode(_as_private_key(private_key).sign(_canonical_payload(moment, entries))).decode()
    return RevocationList(issued_at=moment, revocations=entries, signature=signature)


def verify_revocation_list(
    revocations: RevocationList,
    public_key: Any,
    *,
    now: datetime | None = None,
    max_age_seconds: int | None = None,
) -> None:
    """Проверяет подпись (и, опционально, свежесть) ревок-листа."""
    if max_age_seconds is not None:
        current = now or datetime.now(UTC)
        age = (current - revocations.issued_at).total_seconds()
        if age < -60 or age > max_age_seconds:
            raise LicenseError(f"revocation list is stale (issued_at={revocations.issued_at.isoformat()})")
    try:
        _as_public_key(public_key).verify(
            base64.b64decode(revocations.signature, validate=True),
            _canonical_payload(revocations.issued_at, revocations.revocations),
        )
    except Exception as exc:
        raise LicenseError(f"revocation list signature mismatch: {exc}") from exc


@dataclass(frozen=True, slots=True)
class VerifiedRevocations:
    """Ревок-лист с проверенными подписью и свежестью — единственный вход :func:`is_valid`."""

    revocations: RevocationList
    verified_at: datetime

    @property
    def issued_at(self) -> datetime:
        return self.revocations.issued_at

    def is_revoked(self, jti: str) -> bool:
        return self.revocations.is_revoked(jti)

    def revoked_jtis(self) -> set[str]:
        return self.revocations.revoked_jtis()


def load_revocation_list(
    raw: str | bytes | dict[str, Any],
    *,
    public_key: Any,
    now: datetime | None = None,
    max_age_seconds: int | None = DEFAULT_REVOCATION_MAX_AGE_SECONDS,
) -> VerifiedRevocations:
    """Парсит ревок-лист, проверяет подпись и свежесть; результат — :class:`VerifiedRevocations`.

    ``max_age_seconds`` по умолчанию = два интервала публикации: протухший
    (и, значит, подменённый кэшем) список не принимается.
    """
    current = now or datetime.now(UTC)
    if isinstance(raw, dict):
        data: Any = raw
    else:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise LicenseError(f"invalid revocation list JSON: {exc}") from exc
    try:
        parsed = RevocationList.model_validate(data)
    except ValueError as exc:
        raise LicenseError(f"invalid revocation list: {exc}") from exc
    verify_revocation_list(parsed, public_key, now=current, max_age_seconds=max_age_seconds)
    return VerifiedRevocations(revocations=parsed, verified_at=current)


def is_valid(
    claims: LicenseClaims,
    now: datetime | None = None,
    revocations: VerifiedRevocations | None = None,
) -> bool:
    """True, если лицензия не просрочена и (если передан проверенный лист) не отозвана.

    Непроверенный :class:`RevocationList` не принимается: только результат
    :func:`load_revocation_list`, иначе подменённый список оживлял бы отзывы.
    """
    if revocations is not None and not isinstance(revocations, VerifiedRevocations):
        raise LicenseError("revocations must come from load_revocation_list(); an unverified list is not trusted")
    if claims.is_expired(now):
        return False
    return revocations is None or not revocations.is_revoked(claims.jti)


__all__ = [
    "DEFAULT_ALGORITHM",
    "DEFAULT_OFFLINE_GRACE_SECONDS",
    "DEFAULT_REVOCATION_MAX_AGE_SECONDS",
    "ISSUER",
    "MAX_EPOCH_SECONDS",
    "MAX_REVOCATION_ENTRIES",
    "MIN_EPOCH_SECONDS",
    "LicenseClaims",
    "LicenseError",
    "RevocationList",
    "RevokedLicense",
    "VerifiedRevocations",
    "decode_license",
    "decode_license_unverified",
    "encode_license",
    "is_valid",
    "load_revocation_list",
    "sign_revocation_list",
    "verify_revocation_list",
]
