"""Structural read-only guard around the shared Mongo handle.

Phase 2 of Simple Assistant is dry-run only. Rather than relying on
convention ("please don't call update_one"), every DB access in
``simple_assistant/commands.py`` goes through ``RDB`` — a proxy that
exposes ONLY read operations and raises on everything else. Any accidental
write becomes an immediate, loud ``RuntimeError`` instead of a silent
mutation.
"""
from __future__ import annotations

from typing import Any

from core import db as _real_db

_READ_OPS = frozenset(
    {
        "find",
        "find_one",
        "aggregate",
        "count_documents",
        "estimated_document_count",
        "distinct",
        "watch",
    }
)


class _ReadOnlyCollection:
    __slots__ = ("_coll", "_name")

    def __init__(self, coll: Any, name: str) -> None:
        self._coll = coll
        self._name = name

    def __getattr__(self, item: str) -> Any:
        if item not in _READ_OPS:
            raise RuntimeError(
                f"simple_assistant is read-only in this phase: "
                f"db.{self._name}.{item}() is not permitted"
            )
        return getattr(self._coll, item)


class _ReadOnlyDB:
    def __getattr__(self, name: str) -> _ReadOnlyCollection:
        return _ReadOnlyCollection(getattr(_real_db, name), name)

    def __getitem__(self, name: str) -> _ReadOnlyCollection:
        return _ReadOnlyCollection(_real_db[name], name)


RDB = _ReadOnlyDB()
