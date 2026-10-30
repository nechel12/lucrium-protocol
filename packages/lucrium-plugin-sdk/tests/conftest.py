"""Общие фикстуры тестов SDK: манифест, inline-модули с хуками."""

from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest

from lucrium_protocol.events import HubEvent
from lucrium_protocol.manifest import PluginManifest, parse_manifest_text
from lucrium_protocol.types import EventKind, Platform

NOW_TS = "2026-01-15T12:00:00Z"

MANIFEST_TEXT = """
[plugin]
id = "test-plugin"
name = "Тестовый плагин"
version = "1.0.0"
api = "1"
entry = "main"

[permissions]
accounts = ["funpay"]
network = false
"""


@pytest.fixture
def manifest() -> PluginManifest:
    return parse_manifest_text(MANIFEST_TEXT)


@pytest.fixture
def hub_event() -> HubEvent:
    return HubEvent(
        platform=Platform.FUNPAY,
        session_id="funpay-1",
        kind=EventKind.MESSAGE,
        payload={"type": "NEW_MESSAGE", "chat": {"id": 42}, "message": {"text": "Привет", "id": 7}},
        ts=NOW_TS,  # type: ignore[arg-type]
    )


def make_module(**namespace: Any) -> ModuleType:
    """Создаёт модуль с заданными атрибутами (хуками) — без файловой системы."""
    module = ModuleType("inline_plugin")
    for name, value in namespace.items():
        setattr(module, name, value)
    return module


@pytest.fixture
def module_factory():
    """Фабрика inline-модулей для тестов entry/runtime."""
    return make_module
