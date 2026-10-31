"""Состояние воркера и команды установки: зафиксированные разрешения плагинов."""

from datetime import UTC, datetime

from lucrium_protocol import (
    InstallPluginParams,
    ManifestPermissions,
    Platform,
    PluginState,
    PluginStatus,
    UpdatePluginParams,
)


def _plugin_state(permissions: ManifestPermissions | None) -> PluginState:
    now = datetime(2026, 10, 31, tzinfo=UTC)
    return PluginState(
        id="auto-reply",
        name="Автоответчик",
        version="1.0.0",
        api="1",
        status=PluginStatus.RUNNING,
        permissions=permissions,
        installed_at=now,
        updated_at=now,
    )


def test_plugin_state_carries_permissions_snapshot() -> None:
    state = _plugin_state(ManifestPermissions(accounts=[Platform.FUNPAY], network=True))
    wire = state.model_dump(mode="json")
    assert wire["permissions"] == {"accounts": ["funpay"], "network": True, "finance": False}
    assert PluginState.model_validate(wire) == state


def test_plugin_state_permissions_optional_for_legacy_records() -> None:
    assert _plugin_state(None).permissions is None
    assert PluginState.model_validate(_plugin_state(None).model_dump(mode="json")).permissions is None


def test_install_and_update_require_explicit_permission_ack() -> None:
    assert InstallPluginParams(archive_b64="AAAA").ack_permissions is False
    assert UpdatePluginParams(plugin_id="auto-reply", archive_b64="AAAA").ack_permissions is False
    assert InstallPluginParams(archive_b64="AAAA", ack_permissions=True).ack_permissions is True
