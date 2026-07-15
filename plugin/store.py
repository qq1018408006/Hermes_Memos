"""Thread-safe access to the framework-neutral personal-memo core."""

from __future__ import annotations

import importlib
import os
import sys
import threading
from pathlib import Path
from types import ModuleType


_module_lock = threading.Lock()
_module: ModuleType | None = None
_local = threading.local()


def _add_core_to_path() -> None:
    """Locate the separately installed core, with a source-tree fallback for tests."""
    home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    source_root = Path(__file__).resolve().parents[1]
    candidates = (
        Path(os.environ["PERSONAL_MEMO_CORE_PATH"]) if os.environ.get("PERSONAL_MEMO_CORE_PATH") else None,
        home / "lib",
        source_root,
    )
    for candidate in candidates:
        if candidate is not None and (candidate / "personal_memo_core").is_dir():
            path = str(candidate)
            if path not in sys.path:
                sys.path.insert(0, path)
            return
    raise RuntimeError("personal_memo_core is not installed; run the paired personal-memo install.py")


def memo_module() -> ModuleType:
    """Load the shared core once per Hermes process."""
    global _module
    with _module_lock:
        if _module is None:
            _add_core_to_path()
            _module = importlib.import_module("personal_memo_core.service")
        return _module


def get_store():
    """Return a store confined to the calling thread.

    Hermes can invoke handlers from multiple threads. sqlite3 connections are
    thread-affine, while the core's WAL and busy timeout coordinate processes.
    """
    module = memo_module()
    paths = module.MemoPaths.resolve()
    database = str(paths.db_path.resolve())
    existing = getattr(_local, "store", None)
    if existing is not None and getattr(_local, "database", None) == database:
        return existing, module
    if existing is not None:
        existing.close()
    store = module.MemoStore(paths)
    _local.store = store
    _local.database = database
    return store, module


def close_thread_store() -> None:
    """Close the current thread's cached store; useful in tests."""
    store = getattr(_local, "store", None)
    if store is not None:
        store.close()
    for attribute in ("store", "database"):
        if hasattr(_local, attribute):
            delattr(_local, attribute)
