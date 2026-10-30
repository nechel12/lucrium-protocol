"""Тесты дескриптора плагина и верификации доставленного артефакта (спека §6.2)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lucrium_protocol.descriptor import (
    ArtifactError,
    DeliveredArtifact,
    PluginDescriptor,
    artifact_signing_payload,
    descriptor_from_json,
    verify_delivered_artifact,
)
from lucrium_protocol.errors import ErrorCode, ProtocolError

PRIVATE = Ed25519PrivateKey.generate()
PUB_B64 = base64.b64encode(PRIVATE.public_key().public_bytes_raw()).decode()
# дата должна быть в будущем независимо от календаря CI
VALID_UNTIL = datetime.now(UTC) + timedelta(days=30)


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
                "plugin_id": "p",
                "version": "1.0.0",
                "delivery": {"type": "remote", "endpoint": "http://x/deliver", "license_key": "k"},
                "signing": {"public_key": PUB_B64},
            }
        )


def test_public_key_must_be_32_bytes() -> None:
    with pytest.raises(ValueError):
        PluginDescriptor.model_validate(
            {
                "plugin_id": "p",
                "version": "1.0.0",
                "delivery": {"type": "remote", "endpoint": "https://x/deliver", "license_key": "k"},
                "signing": {"public_key": base64.b64encode(b"short").decode()},
            }
        )


def _signed_artifact(plugin_id: str = "auto-reply-ai", version: str = "1.3.0") -> DeliveredArtifact:
    artifact = b"PK\x03\x04fake-zip-bytes"
    payload = artifact_signing_payload(plugin_id, version, VALID_UNTIL, artifact)
    signature = base64.b64encode(PRIVATE.sign(payload)).decode()
    return DeliveredArtifact(
        plugin_id=plugin_id,
        version=version,
        artifact_b64=base64.b64encode(artifact).decode(),
        valid_until=VALID_UNTIL,
        signature=signature,
    )


def test_verify_delivered_artifact_ok() -> None:
    delivered = _signed_artifact()
    data = verify_delivered_artifact(_descriptor(), delivered)
    assert data.startswith(b"PK")


def test_verify_rejects_wrong_signature() -> None:
    delivered = _signed_artifact()
    forged = delivered.model_copy(update={"signature": base64.b64encode(b"x" * 64).decode()})
    with pytest.raises(ArtifactError):
        verify_delivered_artifact(_descriptor(), forged)


def test_verify_rejects_expired() -> None:
    delivered = _signed_artifact()
    past = delivered.model_copy(update={"valid_until": datetime.now(UTC) - timedelta(seconds=1)})
    # подпись переподписана под прошлый срок — но срок всё равно в прошлом
    payload = artifact_signing_payload(past.plugin_id, past.version, past.valid_until, past.artifact_bytes)
    past = past.model_copy(update={"signature": base64.b64encode(PRIVATE.sign(payload)).decode()})
    with pytest.raises(ArtifactError, match="expired"):
        verify_delivered_artifact(_descriptor(), past)


def test_verify_rejects_identity_mismatch() -> None:
    delivered = _signed_artifact(plugin_id="other-plugin")
    with pytest.raises(ArtifactError, match="mismatch"):
        verify_delivered_artifact(_descriptor(), delivered)


def test_signing_payload_is_canonical() -> None:
    a = artifact_signing_payload("p", "1.0.0", VALID_UNTIL, b"data")
    b = artifact_signing_payload("p", "1.0.0", VALID_UNTIL, b"data")
    assert a == b
    # заголовок: plugin_id \n version \n valid_until(Z) \n
    deadline = VALID_UNTIL.astimezone(UTC).isoformat().replace("+00:00", "Z")
    assert a.startswith(f"p\n1.0.0\n{deadline}\n".encode())
    assert a.endswith(b"data")
