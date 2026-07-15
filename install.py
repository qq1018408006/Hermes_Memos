#!/usr/bin/env python3
"""Install a packaged personal-memo Skill and Hermes plugin without touching data."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def replace_component(source: Path, destination: Path) -> None:
    """Replace an installed code component without creating a code backup."""
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install personal-memo Skill and Hermes plugin")
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", "~/.hermes"))
    args = parser.parse_args()
    package = Path(__file__).resolve().parent
    skill = package / "skill"
    plugin = package / "plugin"
    core = package / "core" / "personal_memo_core"
    mcp = package / "mcp"
    if not (skill / "SKILL.md").is_file() or not (plugin / "plugin.yaml").is_file() or not (core / "service.py").is_file() or not (mcp / "server.py").is_file():
        raise SystemExit("Run install.py from the extracted personal-memo package root.")
    home = Path(args.hermes_home).expanduser().resolve()
    replace_component(core, home / "lib" / "personal_memo_core")
    replace_component(skill, home / "skills" / "productivity" / "personal-memo")
    replace_component(plugin, home / "plugins" / "personal-memo")
    replace_component(mcp, home / "mcp-servers" / "personal-memo")
    migration_env = os.environ.copy()
    migration_env["HERMES_HOME"] = str(home)
    migration_env["PERSONAL_MEMO_SKILL_ROOT"] = str(home / "skills" / "productivity" / "personal-memo")
    try:
        migration = subprocess.run(
            [sys.executable, str(home / "skills" / "productivity" / "personal-memo" / "scripts" / "memo.py"), "--json", "migrate-timezone", "Asia/Shanghai"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=migration_env,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise SystemExit("Skill and plugin were installed, but timezone migration failed:\n" + detail) from exc
    migration_result = json.loads(migration.stdout)
    print("Installed core:", home / "lib" / "personal_memo_core")
    print("Installed skill:", home / "skills" / "productivity" / "personal-memo")
    print("Installed plugin:", home / "plugins" / "personal-memo")
    print("Installed MCP server:", home / "mcp-servers" / "personal-memo")
    print("Memo timezone:", migration_result["timezone"], f"(updated items: {migration_result['updated_items']})")
    print("Next: run `hermes plugins enable personal-memo`, then restart the Hermes gateway or start a new Hermes session.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
