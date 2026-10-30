"""Контракт точки входа плагина: ``on_start`` / ``on_event`` / ``on_stop`` (спека §6.1).

Варианты объявления хуков:

1. Именованные как в контракте (рекомендуется)::

       async def on_start(ctx: PluginContext) -> None: ...
       async def on_event(event: PluginEvent, ctx: PluginContext) -> None: ...
       async def on_stop(ctx: PluginContext) -> None: ...

2. Через декораторы — удобно, когда хуки разнесены по модулям/классам::

       from lucrium_plugin_sdk import on_event

       @on_event
       async def reply(event, ctx): ...

Валидация: все хуки обязаны быть ``async def`` (или возвращать awaitable),
``on_event`` — принимать минимум два позициональных аргумента, ``on_start``/
``on_stop`` — минимум один. Плагин обязан иметь хотя бы один хук.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from types import ModuleType
from typing import Any, cast

from .context import PluginContext
from .events import PluginEvent

HookStart = Callable[[PluginContext], Coroutine[Any, Any, None]]
HookEvent = Callable[[PluginEvent, PluginContext], Coroutine[Any, Any, None]]
HookStop = Callable[[PluginContext], Coroutine[Any, Any, None]]

AnyHook = Callable[..., Coroutine[Any, Any, None]]

_HOOK_ATTR = "__lucrium_hook__"


class EntryError(Exception):
    """Точка входа плагина невалидна: нет хуков, не async, не тот арity."""


@dataclass(frozen=True, slots=True)
class PluginEntry:
    """Найденные хуки модуля-точки входа."""

    on_start: HookStart | None = None
    on_event: HookEvent | None = None
    on_stop: HookStop | None = None

    @property
    def has_any(self) -> bool:
        return any([self.on_start, self.on_event, self.on_stop])

    def issues(self) -> list[str]:
        """Список проблем контракта ([] = ок). Пустой набор хуков — тоже проблема."""
        problems: list[str] = []
        if not self.has_any:
            problems.append("no hooks found: define on_start/on_event/on_stop (or use @on_start/@on_event/@on_stop)")
        for name, hook in (("on_start", self.on_start), ("on_event", self.on_event), ("on_stop", self.on_stop)):
            if hook is None:
                continue
            if not inspect.iscoroutinefunction(hook):
                problems.append(f"{name} must be an 'async def' coroutine function")
                continue
            problems.extend(_arity_issues(name, hook))
        return problems

    def ensure_valid(self) -> PluginEntry:
        issues = self.issues()
        if issues:
            raise EntryError("; ".join(issues))
        return self


def _arity_issues(name: str, hook: Callable[..., Any]) -> list[str]:
    try:
        signature = inspect.signature(hook)
    except (TypeError, ValueError):
        return []
    positional = [p for p in signature.parameters.values() if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    has_varargs = any(p.kind is p.VAR_POSITIONAL for p in signature.parameters.values())
    required = 2 if name == "on_event" else 1
    if has_varargs:
        return []
    if len(positional) < required:
        return [
            f"{name} must accept {required} positional argument(s): {'event, ctx' if name == 'on_event' else 'ctx'}"
        ]
    return []


def on_start[HookT: HookStart](func: HookT) -> HookT:
    """Помечает функцию как ``on_start``."""
    return cast("HookT", _mark(func, "start"))


def on_event[HookT: HookEvent](func: HookT) -> HookT:
    """Помечает функцию как ``on_event``."""
    return cast("HookT", _mark(func, "event"))


def on_stop[HookT: HookStop](func: HookT) -> HookT:
    """Помечает функцию как ``on_stop``."""
    return cast("HookT", _mark(func, "stop"))


def _mark(func: AnyHook, kind: str) -> AnyHook:
    if not inspect.iscoroutinefunction(func):
        raise EntryError(f"@{kind} hook must be an async function, got {func!r}")
    setattr(func, _HOOK_ATTR, kind)
    return func


def discover_entry(module: ModuleType | object) -> PluginEntry:
    """Находит хуки в модуле: по имени (``on_event``) или по декоратору.

    Декоратор (:func:`on_event` и т.п.) приоритетнее одноимённой функции без
    декоратора; более одного кандидата на один хук — :class:`EntryError`.
    """
    marked: dict[str, list[Callable[..., Any]]] = {"start": [], "event": [], "stop": []}
    named: dict[str, list[Callable[..., Any]]] = {"start": [], "event": [], "stop": []}

    namespace = getattr(module, "__dict__", {})
    for name, value in namespace.items():
        if not callable(value):
            continue
        kind = getattr(value, _HOOK_ATTR, None)
        if isinstance(kind, str) and kind in marked:
            marked[kind].append(value)
        if name in ("on_start", "on_event", "on_stop"):
            named[name.removeprefix("on_")].append(value)

    def pick(kind: str) -> AnyHook | None:
        found = marked[kind] or named[kind]  # декоратор приоритетнее имени
        if not found:
            return None
        if len(found) > 1:
            raise EntryError(f"ambiguous {kind} hooks: {[f.__name__ for f in found]} — keep exactly one")
        return found[0]

    return PluginEntry(
        on_start=cast("HookStart | None", pick("start")),
        on_event=cast("HookEvent | None", pick("event")),
        on_stop=cast("HookStop | None", pick("stop")),
    )


def load_entry(module: ModuleType | object) -> PluginEntry:
    """:func:`discover_entry` + :meth:`PluginEntry.ensure_valid`."""
    return discover_entry(module).ensure_valid()


__all__ = [
    "EntryError",
    "HookEvent",
    "HookStart",
    "HookStop",
    "PluginEntry",
    "discover_entry",
    "load_entry",
    "on_event",
    "on_start",
    "on_stop",
]
