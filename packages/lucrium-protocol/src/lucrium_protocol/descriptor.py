"""Дескриптор плагина и формат удалённой доставки (спека §6.2, заложен заранее под v2).

Дескриптор — то, что панель получает от магазина после покупки, и передаёт
воркеру. В v1 (локальная установка zip-архивом) дескриптор не используется,
но формат зафиксирован сейчас, чтобы запуск магазина позже не потребовал
переделки воркера и панели.

Доставка: ``GET <endpoint>`` + License-Key → подписанный Ed25519 артефакт
+ ``valid_until``. Воркер верифицирует подпись публичным ключом из дескриптора,
кэширует артефакт и обновляет его раз в неделю (``cache_ttl_hours``).
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .errors import ErrorCode, ProtocolError


class ArtifactError(Exception):
    """Подпись/формат доставленного артефакта невалидны — плагин не запускать."""


class DeliveryType(StrEnum):
    REMOTE = "remote"


class Delivery(BaseModel):
    """Куда воркер ходит за артефактом."""

    model_config = ConfigDict(extra="forbid")

    type: DeliveryType = DeliveryType.REMOTE
    endpoint: str = Field(description="HTTPS URL контейнера-продажника, напр. https://author-host:8444/deliver")
    license_key: SecretStr = Field(description="License-Key, передаётся контейнеру для выдачи артефакта")

    @field_validator("endpoint")
    @classmethod
    def _https_only(cls, v: str) -> str:
        if not v.lower().startswith("https://"):
            raise ValueError("delivery endpoint must be https://")
        if " " in v:
            raise ValueError("endpoint must not contain spaces")
        return v


class Signing(BaseModel):
    """Параметры подписи артефактов контейнера-продажника."""

    model_config = ConfigDict(extra="forbid")

    algo: Literal["ed25519"] = "ed25519"
    public_key: str = Field(description="base64 от сырого 32-байтного Ed25519 публичного ключа")
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

    plugin_id: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=64)
    delivery: Delivery
    signing: Signing


class DeliveredArtifact(BaseModel):
    """Ответ ``GET /deliver`` контейнера-продажника: подписанный артефакт + срок."""

    model_config = ConfigDict(extra="forbid")

    plugin_id: str
    version: str
    artifact_b64: str = Field(description="zip плагина, base64")
    valid_until: datetime = Field(description="срок действия выдачи (UTC)")
    signature: str = Field(description="Ed25519, base64; снимается при верификации")

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
    header = f"{plugin_id}\n{version}\n{deadline}\n".encode()
    return header + artifact


def verify_delivered_artifact(descriptor: PluginDescriptor, delivered: DeliveredArtifact) -> bytes:
    """Верифицирует подпись и срок доставленного артефакта; возвращает байты артефакта.

    Raises:
        ArtifactError: подпись неверна, срок в прошлом, id/версия не совпадают с дескриптором.
    """
    if delivered.plugin_id != descriptor.plugin_id or delivered.version != descriptor.version:
        raise ArtifactError(
            f"artifact identity mismatch: {delivered.plugin_id}@{delivered.version} "
            f"!= {descriptor.plugin_id}@{descriptor.version}"
        )
    if delivered.valid_until.tzinfo is None:
        delivered = delivered.model_copy(update={"valid_until": delivered.valid_until.replace(tzinfo=UTC)})
    if delivered.valid_until <= datetime.now(UTC):
        raise ArtifactError(f"artifact expired at {delivered.valid_until.isoformat()}")

    payload = artifact_signing_payload(
        delivered.plugin_id,
        delivered.version,
        delivered.valid_until,
        delivered.artifact_bytes,
    )
    try:
        descriptor.signing.public_key_object().verify(
            base64.b64decode(delivered.signature, validate=True),
            payload,
        )
    except (InvalidSignature, ValueError) as exc:
        raise ArtifactError(f"artifact signature verification failed: {exc}") from exc
    return delivered.artifact_bytes


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
    "ArtifactError",
    "DeliveredArtifact",
    "Delivery",
    "DeliveryType",
    "PluginDescriptor",
    "Signing",
    "artifact_signing_payload",
    "descriptor_from_json",
    "verify_delivered_artifact",
]
