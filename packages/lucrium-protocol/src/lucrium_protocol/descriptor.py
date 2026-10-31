"""Дескриптор плагина и формат удалённой доставки (спека §6.2, заложен заранее под v2).

Дескриптор — то, что панель получает от магазина после покупки, и передаёт
воркеру. В v1 (локальная установка zip-архивом) дескриптор не используется,
но формат зафиксирован сейчас, чтобы запуск магазина позже не потребовал
переделки воркера и панели.

Доставка: ``GET <endpoint>`` + License-Key → подписанный Ed25519 артефакт
+ ``valid_until``. Воркер верифицирует подпись, кэширует артефакт и обновляет
его раз в неделю (``cache_ttl_hours``). ``valid_until`` не может выходить за
горизонт кэша — ``now + cache_ttl_hours + offline-grace``: иначе закешированная
выдача («valid_until = 9999 год») никогда не требует повторной проверки.

Модель доверия (важно). И дескриптор, и артефакт приходят от того, кто
доставляет плагин, поэтому ``signing.public_key`` из дескриптора НЕ является
якорем доверия: при self-signed схеме перепакованный zip со своей парой
ключей, своим дескриптором и своим эндпоинтом проходил бы проверку. Якорь
задаёт принимающая сторона (воркер/панель) из внеполосного источника —
подтверждённые при публикации публичные ключи авторов-контейнеров.
:func:`verify_delivered_artifact` требует их обязательным keyword-аргументом
``trusted_keys`` и проверяет подпись только если ``descriptor.signing.public_key``
входит в этот список. Пути «доверять ключу из дескриптора» нет: пустой
``trusted_keys`` → :class:`DescriptorError`. Подмена эндпоинта или артефакта
злоумышленником не приводит к запуску чужого кода — подписи под его ключом
в кольце нет.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .errors import ErrorCode, ProtocolError
from .license import DEFAULT_OFFLINE_GRACE_SECONDS

# Тот же charset, что у ``ManifestPlugin.id`` и у валидатора путей воркера: id
# уходит в имя каталога на диске, поэтому точки/слэши/``..`` недопустимы.
PLUGIN_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{2,63}$"

MAX_PLUGIN_ID_CHARS = 64
MAX_VERSION_CHARS = 64
# потолок ответа контейнера: base64 от 48 МиБ архива (лимит установщика воркера)
MAX_ARTIFACT_B64_CHARS = 64 * 1024 * 1024
# Ed25519-подпись = 64 байта → 88 символов base64; запас на вариант кодирования
MAX_SIGNATURE_B64_CHARS = 128
MAX_ENDPOINT_CHARS = 4096
MAX_LICENSE_KEY_CHARS = 512


class ArtifactError(Exception):
    """Подпись/формат доставленного артефакта невалидны — плагин не запускать."""


class DescriptorError(ArtifactError):
    """Нарушен якорь доверия: нет доверенных ключей либо ключ дескриптора им не подтверждён.

    Подкласс :class:`ArtifactError`, чтобы вызывающая сторона, ловящая «артефакт
    нельзя запускать», не пропустила отказ по доверию.
    """


class DeliveryType(StrEnum):
    REMOTE = "remote"


class Delivery(BaseModel):
    """Куда воркер ходит за артефактом."""

    model_config = ConfigDict(extra="forbid")

    type: DeliveryType = DeliveryType.REMOTE
    endpoint: str = Field(
        max_length=MAX_ENDPOINT_CHARS,
        description="HTTPS URL контейнера-продажника, напр. https://author-host:8444/deliver",
    )
    license_key: SecretStr = Field(
        max_length=MAX_LICENSE_KEY_CHARS,
        description="License-Key, передаётся контейнеру для выдачи артефакта",
    )

    @field_validator("endpoint")
    @classmethod
    def _https_only(cls, v: str) -> str:
        if not v.lower().startswith("https://"):
            raise ValueError("delivery endpoint must be https://")
        if " " in v:
            raise ValueError("endpoint must not contain spaces")
        return v


class Signing(BaseModel):
    """Параметры подписи артефактов контейнера-продажника.

    ``public_key`` — это *идентификатор* автора, а не якорь доверия: доверие к
    нему задаёт принимающая сторона списком ``trusted_keys`` (см. докстринг модуля).
    """

    model_config = ConfigDict(extra="forbid")

    algo: Literal["ed25519"] = "ed25519"
    public_key: str = Field(max_length=64, description="base64 от сырого 32-байтного Ed25519 публичного ключа")
    cache_ttl_hours: int = Field(
        default=168, ge=1, le=24 * 30, description="обновление артефакта, раз в неделю по умолчанию"
    )

    @field_validator("public_key")
    @classmethod
    def _raw_ed25519(cls, v: str) -> str:
        try:
            raw = base64.b64decode(v, validate=True)
        except Exception as exc:
            raise ValueError("public_key must be base64") from exc
        if len(raw) != 32:
            raise ValueError(f"ed25519 public key must be 32 bytes, got {len(raw)}")
        return v

    def public_key_bytes(self) -> bytes:
        return base64.b64decode(self.public_key)

    def public_key_object(self) -> Ed25519PublicKey:
        return Ed25519PublicKey.from_public_bytes(self.public_key_bytes())


class PluginDescriptor(BaseModel):
    """Дескриптор плагина — выдача магазина после покупки (§6.2)."""

    model_config = ConfigDict(extra="forbid")

    plugin_id: str = Field(min_length=3, max_length=MAX_PLUGIN_ID_CHARS, pattern=PLUGIN_ID_PATTERN)
    version: str = Field(min_length=1, max_length=MAX_VERSION_CHARS)
    delivery: Delivery
    signing: Signing


class DeliveredArtifact(BaseModel):
    """Ответ ``GET /deliver`` контейнера-продажника: подписанный артефакт + срок.

    Все поля ограничены сверху: модель декодирует ответ автора ДО проверки
    подписи, поэтому неограниченная строка = бесконтрольная аллокация.
    """

    model_config = ConfigDict(extra="forbid")

    plugin_id: str = Field(min_length=3, max_length=MAX_PLUGIN_ID_CHARS, pattern=PLUGIN_ID_PATTERN)
    version: str = Field(min_length=1, max_length=MAX_VERSION_CHARS)
    artifact_b64: str = Field(
        min_length=1,
        max_length=MAX_ARTIFACT_B64_CHARS,
        description="zip плагина, base64",
    )
    valid_until: datetime = Field(description="срок действия выдачи (UTC)")
    signature: str = Field(
        min_length=1,
        max_length=MAX_SIGNATURE_B64_CHARS,
        description="Ed25519, base64; снимается при верификации",
    )

    @field_validator("valid_until")
    @classmethod
    def _valid_until_utc(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @property
    def artifact_bytes(self) -> bytes:
        return base64.b64decode(self.artifact_b64, validate=True)


def artifact_signing_payload(
    plugin_id: str,
    version: str,
    valid_until: datetime,
    artifact: bytes,
) -> bytes:
    """Канонические байты подписи артефакта.

    Единая точка истины для контейнера-продажника (что подписывает) и воркера
    (что верифицирует) — менять нельзя без подъёма версии протокола.
    """
    deadline = valid_until.astimezone(UTC).isoformat().replace("+00:00", "Z")

    def field(value: str) -> bytes:
        raw = value.encode("utf-8")
        return len(raw).to_bytes(4, "big") + raw

    # Length-prefixed поля исключают неоднозначность при newline в id/version.
    return field(plugin_id) + field(version) + field(deadline) + artifact


def _is_trusted(key: Ed25519PublicKey, trusted_keys: Sequence[Ed25519PublicKey]) -> bool:
    raw = key.public_bytes_raw()
    return any(candidate.public_bytes_raw() == raw for candidate in trusted_keys)


def verify_delivered_artifact(
    descriptor: PluginDescriptor,
    delivered: DeliveredArtifact,
    *,
    trusted_keys: Sequence[Ed25519PublicKey],
) -> bytes:
    """Верифицирует подпись и срок доставленного артефакта; возвращает байты артефакта.

    ``trusted_keys`` — якорь доверия (публичные ключи авторов, подтверждённые
    принимающей стороной вне этого дескриптора). Аргумент обязателен и не может
    быть пустым: проверять подпись ключом из самого дескриптора — no-op.

    Raises:
        DescriptorError: доверенных ключей нет либо ``descriptor.signing.public_key``
            среди них не найдена (дескриптор подписан сам собой).
        ArtifactError: подпись неверна, срок в прошлом, срок за горизонтом кэша,
            id/версия не совпадают с дескриптором.
    """
    if not trusted_keys:
        raise DescriptorError("no trusted signing keys configured — refusing to verify a delivered artifact")
    if delivered.plugin_id != descriptor.plugin_id or delivered.version != descriptor.version:
        raise ArtifactError(
            f"artifact identity mismatch: {delivered.plugin_id}@{delivered.version} "
            f"!= {descriptor.plugin_id}@{descriptor.version}"
        )

    anchor = descriptor.signing.public_key_object()
    if not _is_trusted(anchor, trusted_keys):
        raise DescriptorError(
            f"descriptor signing key of {descriptor.plugin_id} is not in the trusted key set: "
            "the descriptor signs itself"
        )

    now = datetime.now(UTC)
    if delivered.valid_until.tzinfo is None:
        delivered = delivered.model_copy(update={"valid_until": delivered.valid_until.replace(tzinfo=UTC)})
    if delivered.valid_until <= now:
        raise ArtifactError(f"artifact expired at {delivered.valid_until.isoformat()}")
    # Срок выдачи не должен переживать окно кэширования: valid_until = 9999 год
    # превращал бы закешированный артефакт в бессмертный.
    horizon = now + timedelta(hours=descriptor.signing.cache_ttl_hours)
    horizon += timedelta(seconds=DEFAULT_OFFLINE_GRACE_SECONDS)
    if delivered.valid_until > horizon:
        raise ArtifactError(
            f"artifact valid_until {delivered.valid_until.isoformat()} "
            f"is beyond the cache horizon {horizon.isoformat()}"
        )

    artifact = delivered.artifact_bytes
    payload = artifact_signing_payload(
        delivered.plugin_id,
        delivered.version,
        delivered.valid_until,
        artifact,
    )
    try:
        anchor.verify(
            base64.b64decode(delivered.signature, validate=True),
            payload,
        )
    except (InvalidSignature, ValueError) as exc:
        raise ArtifactError(f"artifact signature verification failed: {exc}") from exc
    return artifact


def descriptor_from_json(raw: str | bytes | dict[str, Any]) -> PluginDescriptor:
    """Парсит дескриптор из JSON (то, что панель получила от магазина)."""
    if isinstance(raw, dict):
        data: Any = raw
    else:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProtocolError(ErrorCode.PARSE_ERROR, f"invalid descriptor JSON: {exc}") from exc
    try:
        return PluginDescriptor.model_validate(data)
    except ValueError as exc:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, f"invalid descriptor: {exc}") from exc


__all__ = [
    "MAX_ARTIFACT_B64_CHARS",
    "MAX_ENDPOINT_CHARS",
    "MAX_LICENSE_KEY_CHARS",
    "MAX_PLUGIN_ID_CHARS",
    "MAX_SIGNATURE_B64_CHARS",
    "MAX_VERSION_CHARS",
    "PLUGIN_ID_PATTERN",
    "ArtifactError",
    "DeliveredArtifact",
    "Delivery",
    "DeliveryType",
    "DescriptorError",
    "PluginDescriptor",
    "Signing",
    "artifact_signing_payload",
    "descriptor_from_json",
    "verify_delivered_artifact",
]
