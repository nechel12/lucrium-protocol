"""``PluginEvent`` — событие, приходящее в ``on_event``.

Обёртка над ``lucrium_protocol.events.HubEvent`` с удобными доступами к
payload'у: оригинальный typed event площадки сериализован воркером в dict
(поле ``client`` из аiosellers в провод не попадает).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from lucrium_protocol.events import HubEvent
from lucrium_protocol.types import EventKind, Platform


@dataclass(frozen=True, slots=True)
class PluginEvent:
    """Нормализованное событие площадки."""

    platform: Platform
    session_id: str
    kind: EventKind
    payload: dict[str, Any]
    ts: datetime

    @classmethod
    def from_hub(cls, hub: HubEvent) -> PluginEvent:
        return cls(
            platform=hub.platform,
            session_id=hub.session_id,
            kind=hub.kind,
            payload=dict(hub.payload),
            ts=hub.ts,
        )

    @property
    def data(self) -> dict[str, Any]:
        """Алиас ``payload``."""
        return self.payload

    def get(self, path: str, default: Any = None) -> Any:
        """Значение по точечному пути: ``event.get("message.text")``."""
        current: Any = self.payload
        for part in path.split("."):
            if isinstance(current, Mapping):
                current = current.get(part)
            else:
                return default
        return default if current is None else current

    def section(self, name: str) -> dict[str, Any]:
        """Подсловарь payload'а (``{"chat": …}`` → ``event.section("chat")``)."""
        value = self.payload.get(name)
        return dict(value) if isinstance(value, Mapping) else {}

    def __repr__(self) -> str:
        return (
            f"PluginEvent(platform={self.platform.value}, session_id={self.session_id!r}, "
            f"kind={self.kind.value!r}, ts={self.ts.isoformat()})"
        )


__all__ = ["PluginEvent"]
