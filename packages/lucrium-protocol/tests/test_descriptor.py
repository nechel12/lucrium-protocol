"""Тесты дескриптора плагина и верификации доставленного артефакта (спека §6.2)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import ValidationError

from lucrium_protocol.descriptor import (
    MAX_ARTIFACT_B64_CHARS,
    ArtifactError,
    DeliveredArtifact,
    DescriptorError,
    PluginDescriptor,
    artifact_signing_payload,
    descriptor_from_json,
    verify_delivered_artifact,
)
from lucrium_protocol.errors import ErrorCode, ProtocolError

PRIVATE = Ed25519PrivateKey.generate()
PUBLIC = PRIVATE.public_key()
PUB_B64 = base64.b64encode(PUBLIC.public_bytes_raw()).decode()
# ключ «автора», которого никто не подтверждал
ROGUE_PRIVATE = Ed25519PrivateKey.generate()
ROGUE_PUBLIC = ROGUE_PRIVATE.public_key()
ROGUE_B64 = base64.b64encode(ROGUE_PUBLIC.public_bytes_raw()).decode()
# дата должна быть в будущем независимо от календаря CI и внутри горизонта кэша
# (cache_ttl_hours=168 + offline-grace 72 ч = 10 суток)
VALID_UNTIL = datetime.now(UTC) + timedelta(days=7)


def _descriptor(**overrides: object) -> PluginDescriptor:
    data: dict[str, object] = {
        "plugin_id": "auto-reply-ai",
        "version": "1.3.0",
        "delivery": {
            "type": "remote",
            "endpoint": "https://author-host:8444/deliver",
            "license_key": "MK-000111222333",
        },
        "signing": {"algo": "ed25519", "public_key": PUB_B64, "cache_ttl_hours": 168},
    }
    data.update(overrides)
    return PluginDescriptor.model_validate(data)


def _ring(*keys: Ed25519PublicKey) -> list[Ed25519PublicKey]:
    return list(keys)


def test_descriptor_from_json() -> None:
    raw = json.dumps(
        {
            "plugin_id": "auto-reply-ai",
            "version": "1.3.0",
            "delivery": {
                "type": "remote",
                "endpoint": "https://author-host:8444/deliver",
                "license_key": "MK-000111222333",
            },
            "signing": {"algo": "ed25519", "public_key": PUB_B64},
        }
    )
    d = descriptor_from_json(raw)
    assert d.delivery.type.value == "remote"
    assert d.signing.cache_ttl_hours == 168
    assert d.delivery.license_key.get_secret_value() == "MK-000111222333"


def test_descriptor_invalid_json() -> None:
    with pytest.raises(ProtocolError) as exc:
        descriptor_from_json("{oops")
    assert exc.value.code == ErrorCode.PARSE_ERROR


def test_endpoint_must_be_https() -> None:
    # явный путь через model_validate: http-эндпоинт отклоняется
    with pytest.raises(ValueError):
        PluginDescriptor.model_validate(
            {
                "plugin_id": "test-plugin",
                "version": "1.0.0",
                "delivery": {"type": "remote", "endpoint": "http://x/deliver", "license_key": "k"},
                "signing": {"public_key": PUB_B64},
            }
        )


def test_public_key_must_be_32_bytes() -> None:
    with pytest.raises(ValueError):
        PluginDescriptor.model_validate(
            {
                "plugin_id": "test-plugin",
                "version": "1.0.0",
                "delivery": {"type": "remote", "endpoint": "https://x/deliver", "license_key": "k"},
                "signing": {"public_key": base64.b64encode(b"short").decode()},
            }
        )


@pytest.mark.parametrize("plugin_id", ["p", "UPPER-plugin", "a/b", "../etc", "with space", "ab", "a" * 65])
def test_descriptor_rejects_unsafe_plugin_id(plugin_id: str) -> None:
    """Id уходит в путь на диске — charset как у манифеста, а не «что угодно до 200 символов»."""
    with pytest.raises(ValidationError):
        _descriptor(plugin_id=plugin_id)


def test_artifact_fields_are_bounded() -> None:
    """Ответ автора декодируется до проверки подписи — поля ограничены сверху."""
    properties = DeliveredArtifact.model_json_schema()["properties"]
    assert properties["artifact_b64"]["maxLength"] == MAX_ARTIFACT_B64_CHARS
    assert properties["plugin_id"]["maxLength"] == 64
    assert properties["version"]["maxLength"] == 64

    base = {
        "plugin_id": "auto-reply-ai",
        "version": "1.0.0",
        "artifact_b64": "AAAA",
        "valid_until": VALID_UNTIL,
        "signature": "A" * 88,
    }
    for field, value in (
        ("plugin_id", "bad/../id"),
        ("plugin_id", "a" * 65),
        ("version", "1.0.0-" + "x" * 64),
        ("signature", "A" * 200),
    ):
        with pytest.raises(ValidationError):
            DeliveredArtifact(**(base | {field: value}))  # type: ignore[arg-type]


def _signed_artifact(
    plugin_id: str = "auto-reply-ai",
    version: str = "1.3.0",
    valid_until: datetime = VALID_UNTIL,
    private: Ed25519PrivateKey = PRIVATE,
    artifact: bytes = b"PK\x03\x04fake-zip-bytes",
) -> DeliveredArtifact:
    payload = artifact_signing_payload(plugin_id, version, valid_until, artifact)
    signature = base64.b64encode(private.sign(payload)).decode()
    return DeliveredArtifact(
        plugin_id=plugin_id,
        version=version,
        artifact_b64=base64.b64encode(artifact).decode(),
        valid_until=valid_until,
        signature=signature,
    )


def test_verify_delivered_artifact_ok() -> None:
    delivered = _signed_artifact()
    data = verify_delivered_artifact(_descriptor(), delivered, trusted_keys=_ring(PUBLIC))
    assert data.startswith(b"PK")


def test_verify_rejects_wrong_signature() -> None:
    delivered = _signed_artifact()
    forged = delivered.model_copy(update={"signature": base64.b64encode(b"x" * 64).decode()})
    with pytest.raises(ArtifactError):
        verify_delivered_artifact(_descriptor(), forged, trusted_keys=_ring(PUBLIC))


def test_verify_rejects_expired() -> None:
    past = datetime.now(UTC) - timedelta(seconds=1)
    delivered = _signed_artifact(valid_until=past)
    # подпись корректна под прошлый срок — но срок всё равно в прошлом
    with pytest.raises(ArtifactError, match="expired"):
        verify_delivered_artifact(_descriptor(), delivered, trusted_keys=_ring(PUBLIC))


def test_verify_rejects_immortal_valid_until() -> None:
    """valid_until «в 9999 году» не превращает кэш в бессмертный."""
    far = datetime.now(UTC) + timedelta(days=365 * 1000)
    delivered = _signed_artifact(valid_until=far)
    with pytest.raises(ArtifactError, match="beyond the cache horizon"):
        verify_delivered_artifact(_descriptor(), delivered, trusted_keys=_ring(PUBLIC))


def test_verify_rejects_valid_until_over_ttl_and_grace() -> None:
    """Горизонт = cache_ttl + offline-grace: чуть больше — и отказ."""
    horizon = timedelta(hours=168) + timedelta(seconds=72 * 3600)
    delivered = _signed_artifact(valid_until=datetime.now(UTC) + horizon + timedelta(minutes=1))
    with pytest.raises(ArtifactError, match="beyond the cache horizon"):
        verify_delivered_artifact(_descriptor(), delivered, trusted_keys=_ring(PUBLIC))
    # на границе — ещё проходит
    ok = _signed_artifact(valid_until=datetime.now(UTC) + horizon - timedelta(minutes=1))
    assert verify_delivered_artifact(_descriptor(), ok, trusted_keys=_ring(PUBLIC)).startswith(b"PK")


def test_verify_rejects_identity_mismatch() -> None:
    delivered = _signed_artifact(plugin_id="other-plugin")
    with pytest.raises(ArtifactError, match="mismatch"):
        verify_delivered_artifact(_descriptor(), delivered, trusted_keys=_ring(PUBLIC))


def test_verify_requires_trusted_keys() -> None:
    """Без якоря доверия проверки нет — пустое кольцо это отказ, а не «доверять всем»."""
    with pytest.raises(DescriptorError, match="no trusted signing keys"):
        verify_delivered_artifact(_descriptor(), _signed_artifact(), trusted_keys=[])


def test_verify_rejects_self_signed_descriptor() -> None:
    """Перепаковка со своим ключом, своим дескриптором и своим эндпоинтом — отказ."""
    rogue_descriptor = _descriptor(signing={"algo": "ed25519", "public_key": ROGUE_B64, "cache_ttl_hours": 168})
    rogue_artifact = _signed_artifact(private=ROGUE_PRIVATE)
    # подпись под ключом из дескриптора верна, но этого ключа в кольце нет
    with pytest.raises(DescriptorError, match="not in the trusted key set"):
        verify_delivered_artifact(rogue_descriptor, rogue_artifact, trusted_keys=_ring(PUBLIC))
    # и если оператор подтвердил только rogue-ключ — обычный путь работает
    assert verify_delivered_artifact(rogue_descriptor, rogue_artifact, trusted_keys=_ring(ROGUE_PUBLIC))


def test_trusted_key_ring_may_contain_several_authors() -> None:
    assert verify_delivered_artifact(_descriptor(), _signed_artifact(), trusted_keys=_ring(ROGUE_PUBLIC, PUBLIC))


def test_descriptor_error_is_an_artifact_error() -> None:
    """Вызывающая сторона, ловящая ArtifactError, обязана ловить и отказ по доверию."""
    with pytest.raises(ArtifactError):
        verify_delivered_artifact(_descriptor(), _signed_artifact(), trusted_keys=[])


def test_signing_payload_is_canonical() -> None:
    a = artifact_signing_payload("p", "1.0.0", VALID_UNTIL, b"data")
    b = artifact_signing_payload("p", "1.0.0", VALID_UNTIL, b"data")
    assert a == b
    deadline = VALID_UNTIL.astimezone(UTC).isoformat().replace("+00:00", "Z")

    def field(value: str) -> bytes:
        raw = value.encode()
        return len(raw).to_bytes(4, "big") + raw

    assert a.startswith(field("p") + field("1.0.0") + field(deadline))
    assert a.endswith(b"data")
