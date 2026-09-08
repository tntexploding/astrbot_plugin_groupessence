"""Bounded, aggregate-only sync timings; never retain IDs, URLs or payloads."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

_STAGES = ("queue", "list", "detail", "normalize", "database_read", "history", "persist")
_current: ContextVar[dict[str, float] | None] = ContextVar("ge_sync_timing", default=None)


@contextmanager
def stage(name: str) -> Iterator[None]:
    values = _current.get()
    if values is None or name not in _STAGES:
        yield
        return
    started = perf_counter()
    try:
        yield
    finally:
        values[name] += max(0.0, perf_counter() - started)


@contextmanager
def measure_sync(emit: Callable[[str], object] | None) -> Iterator[None]:
    values = dict.fromkeys(_STAGES, 0.0)
    token = _current.set(values)
    started = perf_counter()
    outcome = "ok"
    try:
        yield
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except BaseException:
        outcome = "error"
        raise
    finally:
        total = max(0.0, perf_counter() - started)
        _current.reset(token)
        if emit is not None:
            fields = ", ".join(f"{key}_ms={int(value * 1000)}" for key, value in values.items())
            try:
                emit(f"GroupEssence 同步耗时：outcome={outcome}, total_ms={int(total * 1000)}, {fields}")
            except Exception:
                # Diagnostics must not change sync results or hide the original error.
                pass
