"""``python -m lucrium_plugin_sdk`` — запуск плагина воркером.

Воркер создаёт subprocess с переменными окружения::

    LUCRIUM_PLUGIN_DIR   — каталог плагина (с plugin.toml)
    LUCRIUM_PLUGIN_SOCKET — путь к unix socket воркера (NDJSON JSON-RPC 2.0)

Рантайм подключается к воркеру, выполняет контракт
``plugin.start`` → ``plugin.event``* → ``plugin.stop`` и завершается.

Коды выхода: 0 — штатно (получен ``plugin.stop``), 1 — ошибка запуска
(манифест, точка входа, обрыв связи) — воркер делает рестарт с backoff.
"""

from __future__ import annotations

import asyncio
import sys


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(f"[lucrium-plugin] fatal: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
    sys.exit(0)


async def _run() -> None:
    from .runtime import run_plugin

    await run_plugin()


if __name__ == "__main__":
    main()
