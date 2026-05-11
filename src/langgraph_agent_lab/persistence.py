"""Checkpointer adapter — supports memory, sqlite, and postgres backends."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def build_checkpointer(kind: str = "memory", database_url: str | None = None) -> Any | None:
    """Return a LangGraph checkpointer based on the requested backend.

    Backends:
    - ``memory``  — in-process MemorySaver, zero dependencies, good for dev/test
    - ``sqlite``  — SqliteSaver backed by a local SQLite file with WAL mode enabled.
                    Survives process restarts; suitable for crash-recovery demos.
    - ``postgres`` — PostgresSaver for production multi-process deployments.
    - ``none``    — No checkpointer; state is not persisted between nodes.

    NOTE (SQLite): The API changed in langgraph-checkpoint-sqlite 3.x.
    Use ``SqliteSaver(conn=sqlite3.connect(...))`` rather than
    ``SqliteSaver.from_conn_string()`` which now returns a context manager,
    not a checkpointer instance directly.
    """
    if kind == "none":
        return None

    if kind == "memory":
        from langgraph.checkpoint.memory import MemorySaver  # type: ignore[import-untyped]

        log.debug("Using MemorySaver checkpointer")
        return MemorySaver()

    if kind == "sqlite":
        import sqlite3
        from pathlib import Path

        try:
            from langgraph.checkpoint.sqlite import SqliteSaver  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "SQLite checkpointer requires: pip install langgraph-checkpoint-sqlite"
            ) from exc

        db_path = database_url or "outputs/checkpoints.db"
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        # Enable Write-Ahead Logging for better concurrent read performance
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
        log.info("Using SqliteSaver checkpointer at %s (WAL mode)", db_path)
        return SqliteSaver(conn=conn)

    if kind == "postgres":
        try:
            from langgraph.checkpoint.postgres import PostgresSaver  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "Postgres checkpointer requires: pip install langgraph-checkpoint-postgres"
            ) from exc
        return PostgresSaver.from_conn_string(database_url or "")

    raise ValueError(f"Unknown checkpointer kind: {kind!r}. Choose one of: memory, sqlite, postgres, none")

