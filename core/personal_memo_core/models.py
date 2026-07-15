"""Public core model types and errors.

The implementation currently stores records as validated dictionaries to keep
SQLite migrations backwards-compatible; adapters should use these names rather
than importing Hermes or MCP types into the core.
"""

from .service import ITEM_STATUSES, ITEM_TYPES, MemoError, TIME_PRECISIONS

__all__ = ("MemoError", "ITEM_STATUSES", "ITEM_TYPES", "TIME_PRECISIONS")
