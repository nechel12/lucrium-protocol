"""Тесты манифеста plugin.toml (спека §6.1)."""

from __future__ import annotations

import pytest

from lucrium_protocol.manifest import (
    MANIFEST_FILENAME,
    ManifestError,
    load_manifest,
    parse_manifest_text,
)

VALID_MANIFEST = """
[plugin]
id = "auto-reply-ai"
name = "AI-автоответчик"
version = "1.0.0"
api = "1"
entry = "main"
description = "Отвечает покупателям автоматически"

[config]
schema = "config_schema.json"

[permissions]
accounts = ["funpay", "playerok"]
network = true
"""


def test_parse_valid_manifest() -> None:
    m = parse_manifest_text(VALID_MANIFEST)
    assert m.id == "auto-reply-ai"
    assert m.name == "AI-автоответчик"
    assert m.version == "1.0.0"
    assert m.api == "1"
    assert m.entry == "main"
    assert m.schema_path == "config_schema.json"
    assert [p.value for p in m.permissions.accounts] == ["funpay", "playerok"]
    assert m.permissions.network is True


def test_defaults() -> None:
    m = parse_manifest_text(
        """
[plugin]
id = "demo-plugin"
name = "Demo"
version = "0.0.1"
"""
    )
    assert m.entry == "main"
    assert m.api == "1"
    assert m.schema_path is None
    assert m.permissions.network is False
    assert m.permissions.finance is False
    assert m.permissions.accounts == []


@pytest.mark.parametrize(
    "bad_id",
    ["UPPER", "ok!", "-leading", "a", "с-кириллица"],
)
def test_invalid_ids(bad_id: str) -> None:
    with pytest.raises(ManifestError) as exc:
        parse_manifest_text(f'[plugin]\nid = "{bad_id}"\nname = "X"\nversion = "1.0.0"\n')
    assert any("id" in issue for issue in exc.value.issues)


@pytest.mark.parametrize("bad_version", ["1.0", "v1.0.0", "1.0.0.0", "latest"])
def test_invalid_semver(bad_version: str) -> None:
    with pytest.raises(ManifestError):
        parse_manifest_text(f'[plugin]\nid = "demo-plugin"\nname = "X"\nversion = "{bad_version}"\n')


def test_invalid_entry_module() -> None:
    with pytest.raises(ManifestError):
        parse_manifest_text('[plugin]\nid = "demo-plugin"\nname = "X"\nversion = "1.0.0"\nentry = "1bad"\n')


def test_unsupported_api_version() -> None:
    with pytest.raises(ManifestError) as exc:
        parse_manifest_text('[plugin]\nid = "demo-plugin"\nname = "X"\nversion = "1.0.0"\napi = "99"\n')
    assert any("api" in issue for issue in exc.value.issues)


def test_schema_path_traversal_rejected() -> None:
    with pytest.raises(ManifestError):
        parse_manifest_text(
            '[plugin]\nid = "demo-plugin"\nname = "X"\nversion = "1.0.0"\n[config]\nschema = "../../etc/passwd"\n'
        )


def test_extra_keys_rejected() -> None:
    with pytest.raises(ManifestError):
        parse_manifest_text('[plugin]\nid = "demo-plugin"\nname = "X"\nversion = "1.0.0"\nmystery = 1\n')


def test_invalid_toml() -> None:
    with pytest.raises(ManifestError) as exc:
        parse_manifest_text("[plugin\nid = ")
    assert "TOML" in str(exc.value)


def test_invalid_platform_in_permissions() -> None:
    with pytest.raises(ManifestError):
        parse_manifest_text(
            '[plugin]\nid = "demo-plugin"\nname = "X"\nversion = "1.0.0"\n[permissions]\naccounts = ["steam"]\n'
        )


def test_finance_permission_is_opt_in() -> None:
    m = parse_manifest_text(
        '[plugin]\nid = "demo-plugin"\nname = "X"\nversion = "1.0.0"\n[permissions]\nfinance = true\n'
    )
    assert m.permissions.finance is True
    assert m.permissions.network is False


def test_load_manifest_from_directory(tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / MANIFEST_FILENAME).write_text(VALID_MANIFEST, encoding="utf-8")
    m = load_manifest(tmp_path)
    assert m.id == "auto-reply-ai"


def test_load_manifest_missing_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ManifestError) as exc:
        load_manifest(tmp_path)
    assert MANIFEST_FILENAME in str(exc.value)
