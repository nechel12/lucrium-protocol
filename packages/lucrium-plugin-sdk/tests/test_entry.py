"""Тесты обнаружения и валидации точки входа плагина (контракт api=1)."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from types import ModuleType
from typing import Any

import pytest

from lucrium_plugin_sdk.entry import EntryError, discover_entry, load_entry, on_event, on_start, on_stop

ModuleFactory = Callable[..., ModuleType]


def test_discover_by_name(module_factory: ModuleFactory) -> None:
    async def on_start(ctx: Any) -> None:
        pass

    async def on_event(event: Any, ctx: Any) -> None:
        pass

    entry = discover_entry(module_factory(on_start=on_start, on_event=on_event))
    assert entry.on_start is on_start
    assert entry.on_event is on_event
    assert entry.on_stop is None
    assert entry.has_any


def test_discover_by_decorators(module_factory: ModuleFactory) -> None:
    calls: list[str] = []

    @on_event
    async def handle(event: Any, ctx: Any) -> None:
        calls.append("event")

    @on_stop
    async def shutdown(ctx: Any) -> None:
        calls.append("stop")

    entry = discover_entry(module_factory(handle=handle, shutdown=shutdown)).ensure_valid()
    assert entry.on_event is handle
    assert entry.on_stop is shutdown
    assert entry.on_start is None


def test_decorator_takes_priority_over_same_name(module_factory: ModuleFactory) -> None:
    """Функция с декоратором приоритетнее одноимённой функции без него."""

    @on_event
    async def decorated(event: Any, ctx: Any) -> None:
        pass

    async def plain_handler(event: Any, ctx: Any) -> None:
        pass

    entry = discover_entry(module_factory(on_event=plain_handler, decorated=decorated))
    assert entry.on_event is decorated


def test_no_hooks_is_invalid(module_factory: ModuleFactory) -> None:
    entry = discover_entry(module_factory(helper=lambda: None))
    assert not entry.has_any
    with pytest.raises(EntryError, match="no hooks"):
        entry.ensure_valid()


def test_sync_hook_rejected(module_factory: ModuleFactory) -> None:
    def on_start(ctx: Any) -> None:
        pass

    entry = discover_entry(module_factory(on_start=on_start))
    assert any("async def" in issue for issue in entry.issues())
    with pytest.raises(EntryError, match="async def"):
        entry.ensure_valid()


def test_decorator_rejects_sync_function() -> None:
    with pytest.raises(EntryError, match="async"):

        @on_start
        def bad(ctx: Any) -> None:
            pass


def test_ambiguous_hooks_rejected(module_factory: ModuleFactory) -> None:
    """Два хука одного kind (два декоратора) — однозначность невозможна."""

    @on_event
    async def first(event: Any, ctx: Any) -> None:
        pass

    @on_event
    async def second(event: Any, ctx: Any) -> None:
        pass

    with pytest.raises(EntryError, match="ambiguous"):
        discover_entry(module_factory(first=first, second=second))


def test_on_event_arity_checked(module_factory: ModuleFactory) -> None:
    async def on_event(event: Any) -> None:
        pass

    entry = discover_entry(module_factory(on_event=on_event))
    assert any("2 positional" in issue for issue in entry.issues())


def test_varargs_satisfies_arity(module_factory: ModuleFactory) -> None:
    async def on_event(*args: Any) -> None:
        pass

    entry = discover_entry(module_factory(on_event=on_event)).ensure_valid()
    assert entry.on_event is on_event


def test_load_entry_validates(module_factory: ModuleFactory) -> None:
    with pytest.raises(EntryError):
        load_entry(module_factory())


def test_imported_decorators_are_not_hooks() -> None:
    """``from lucrium_plugin_sdk import on_event`` без использования не ломает плагин."""

    async def on_start(ctx: Any) -> None:
        return None

    module = ModuleType("main_with_unused_decorators")
    module.__dict__.update({"on_start": on_start, "on_event": on_event, "on_stop": on_stop})
    entry = discover_entry(module).ensure_valid()
    assert entry.on_start is on_start
    assert entry.on_event is None
    assert entry.on_stop is None


def test_hooks_are_coroutines(module_factory: ModuleFactory) -> None:
    """smoke: валидные хуки — действительно coroutine-функции."""

    async def on_start(ctx: Any) -> None:
        pass

    entry = discover_entry(module_factory(on_start=on_start)).ensure_valid()
    assert entry.on_start is not None
    assert inspect.iscoroutinefunction(entry.on_start)
