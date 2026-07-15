#!/usr/bin/env python3
"""CLI adapter for the framework-neutral personal-memo core."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _add_core_to_path() -> None:
    skill_root = Path(__file__).resolve().parents[1]
    home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    candidates = (Path(os.environ["PERSONAL_MEMO_CORE_PATH"]) if os.environ.get("PERSONAL_MEMO_CORE_PATH") else None, skill_root, home / "lib")
    for candidate in candidates:
        if candidate is not None and (candidate / "personal_memo_core").is_dir():
            path = str(candidate)
            if path not in sys.path:
                sys.path.insert(0, path)
            return
    raise RuntimeError("personal_memo_core is not installed; run the packaged install.py again")


_add_core_to_path()
from personal_memo_core.service import *  # noqa: F401,F403,E402 - preserve CLI compatibility


if __name__ == "__main__":
    raise SystemExit(main())
