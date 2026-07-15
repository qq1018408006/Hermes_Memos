#!/usr/bin/env python3
"""Optional stdio MCP adapter for the shared personal-memo core.

Install ``requirements-mcp.txt`` in the Python environment that launches this
server. The server owns no business rules and shares the same SQLite database
as Hermes through PERSONAL_MEMO_DATA_DIR or PERSONAL_MEMO_HOME.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


def _add_core_to_path() -> None:
    server_root = Path(__file__).resolve().parents[1]
    home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    candidates = (
        Path(os.environ["PERSONAL_MEMO_CORE_PATH"]) if os.environ.get("PERSONAL_MEMO_CORE_PATH") else None,
        server_root,
        home / "lib",
    )
    for candidate in candidates:
        if candidate is not None and (candidate / "personal_memo_core").is_dir():
            path = str(candidate)
            if path not in sys.path:
                sys.path.insert(0, path)
            return
    raise RuntimeError("personal_memo_core is not installed; set PERSONAL_MEMO_CORE_PATH or run install.py")


_add_core_to_path()
from personal_memo_core.service import MemoPaths, MemoStore  # noqa: E402

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - depends on optional MCP SDK
    raise SystemExit("Install MCP support first: python3 -m pip install -r requirements-mcp.txt") from exc


mcp = FastMCP("personal-memo")


def _run(callback):
    with MemoStore(MemoPaths.resolve()) as store:
        return callback(store)


@mcp.tool()
def memo_add(
    title: str,
    content: str = "",
    item_type: str = "task",
    due_at: str | None = None,
    remind_at: str | None = None,
    scheduled_for: str | None = None,
    timezone: str | None = None,
    priority_level: str = "normal",
    tags: list[str] | None = None,
    urls: list[str] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Create one persistent memo, task, link, or video."""
    return _run(lambda store: store.add_item(
        title=title, content=content, item_type=item_type, due_at=due_at,
        remind_at=remind_at, scheduled_for=scheduled_for, timezone=timezone,
        priority_level=priority_level, tags=tags or (), urls=urls or (),
        idempotency_key=idempotency_key,
    ))


@mcp.tool()
def memo_list(statuses: list[str] | None = None, limit: int | None = None) -> dict[str, Any]:
    """List persistent memo items, defaulting to active items."""
    return _run(lambda store: store.list_items(tuple(statuses or ("active",)), create_snapshot=False, limit=limit))


@mcp.tool()
def memo_show(reference: str) -> dict[str, Any]:
    """Show one item by stable ID."""
    return _run(lambda store: store.show(reference))


@mcp.tool()
def memo_search(query: str, limit: int = 20) -> dict[str, Any]:
    """Search saved titles, content, URLs, and summaries."""
    return _run(lambda store: store.search(query, limit=limit))


@mcp.tool()
def memo_today() -> dict[str, Any]:
    """Show overdue, due-today, and scheduled-today items."""
    return _run(lambda store: store.today())


@mcp.tool()
def memo_update(reference: str, updates: dict[str, Any], clear_fields: list[str] | None = None) -> dict[str, Any]:
    """Update a memo without changing its stable ID."""
    return _run(lambda store: store.update_item(reference, updates, clear_fields=clear_fields or ()))


@mcp.tool()
def memo_complete(reference: str) -> dict[str, Any]:
    """Mark a memo item completed."""
    return _run(lambda store: store.complete(reference))


@mcp.tool()
def memo_delete(reference: str) -> dict[str, Any]:
    """Soft-delete a memo item; it remains recoverable."""
    return _run(lambda store: store.delete(reference))


@mcp.tool()
def memo_backup() -> dict[str, Any]:
    """Create a verified SQLite backup."""
    return _run(lambda store: store.manual_backup())


@mcp.tool()
def memo_timezone_migrate(timezone: str = "Asia/Shanghai") -> dict[str, Any]:
    """Change display timezone without moving stored exact instants."""
    return _run(lambda store: store.migrate_timezone(timezone))


if __name__ == "__main__":
    mcp.run(transport="stdio")
