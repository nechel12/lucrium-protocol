"""Версии контрактов Lucrium.

Единственный источник правды по версиям:

* ``PROTOCOL_VERSION`` — wire-протокол «панель ↔ воркер» (см. ``docs/protocol.md``).
* ``PLUGIN_API_VERSION`` — контракт плагина (поле ``api`` в ``plugin.toml``).

Совместимость: сторона принимает сообщение, если её версия совпадает по major.
Увеличение версии — только при ломающих изменениях (новые обязательные поля,
изменение семантики), новые необязательные поля версию не поднимают.
"""

from __future__ import annotations

PROTOCOL_VERSION = "1"
"""Версия протокола панель ↔ воркер."""

PLUGIN_API_VERSION = "1"
"""Версия контракта ``lucrium-plugin-sdk`` (``api`` в манифесте плагина)."""

SUPPORTED_PLUGIN_API_VERSIONS: tuple[str, ...] = (PLUGIN_API_VERSION,)
"""Версии ``api``, которые понимает эта сборка воркера/SDK."""

__all__ = [
    "PLUGIN_API_VERSION",
    "PROTOCOL_VERSION",
    "SUPPORTED_PLUGIN_API_VERSIONS",
]
