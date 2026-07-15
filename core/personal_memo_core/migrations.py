"""Schema migration entry points owned by the framework-neutral core."""

from .service import SCHEMA_VERSION, MemoStore


def initialize_store(store: MemoStore) -> dict:
    """Return initialization/migration metadata for an already-open store."""
    return dict(store.initialization)


__all__ = ("SCHEMA_VERSION", "initialize_store")
