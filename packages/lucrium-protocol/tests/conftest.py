"""Общие фикстуры: валидные модели состояния для тестов протокола."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from lucrium_protocol import (
    AccountCredentials,
    AccountInput,
    AccountState,
    AccountWithCredentials,
    Capability,
    EventKind,
    HubEvent,
    Platform,
    PluginState,
    PluginStatus,
    SessionStatus,
    StateSnapshot,
    StateWithCredentials,
    WorkerStatus,
)

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def status() -> WorkerStatus:
    return WorkerStatus(
        version="0.1.0",
        protocol="1",
        started_at=NOW - timedelta(hours=3),
        uptime_seconds=3 * 3600,
        python="3.14.7",
        os="linux",
    )


@pytest.fixture
def snapshot(status: WorkerStatus) -> StateSnapshot:
    return StateSnapshot(
        revision=7,
        generated_at=NOW,
        status=status,
        accounts=[
            AccountState(
                id="acc-1",
                platform=Platform.FUNPAY,
                label="Основной аккаунт",
                status=SessionStatus.RUNNING,
                capabilities=[Capability.MESSAGES, Capability.ORDERS],
                created_at=NOW - timedelta(days=2),
                updated_at=NOW - timedelta(minutes=5),
                last_event_at=NOW - timedelta(seconds=30),
            )
        ],
        plugins=[
            PluginState(
                id="auto-reply-ai",
                name="AI-автоответчик",
                version="1.0.0",
                api="1",
                status=PluginStatus.RUNNING,
                has_config_schema=True,
                installed_at=NOW - timedelta(days=1),
                updated_at=NOW - timedelta(days=1),
            )
        ],
    )


@pytest.fixture
def state_with_credentials(status: WorkerStatus) -> StateWithCredentials:
    """``get_state``-снапшот с кредами (только в ответе на запрос, не в событиях)."""
    return StateWithCredentials(
        revision=7,
        generated_at=NOW,
        status=status,
        accounts=[
            AccountWithCredentials(
                id="acc-1",
                account=AccountInput(
                    platform=Platform.FUNPAY,
                    label="Основной аккаунт",
                    credentials=AccountCredentials(platform=Platform.FUNPAY, golden_key=SecretStr("gk-live-123")),
                ),
            )
        ],
        plugins=[],
    )


@pytest.fixture
def hub_event() -> HubEvent:
    return HubEvent(
        platform=Platform.PLAYEROK,
        session_id="acc-1",
        kind=EventKind.MESSAGE,
        payload={"type": "NEW_MESSAGE", "chat_id": "42", "text": "Здравствуйте!"},
        ts=NOW,
    )
