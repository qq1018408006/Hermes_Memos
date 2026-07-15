# Personal Memo for Hermes

[English](README.md) | [简体中文](README.zh-CN.md)

Persistent, local-first memo, task, link, and reminder management for [Hermes](https://hermes-agent.nousresearch.com/). Personal Memo stores its data in SQLite and exposes the same business logic through a native Hermes plugin, a command-line adapter, and an optional MCP server.

## Highlights

- Native Hermes tools for adding, listing, searching, updating, completing, archiving, restoring, and deleting memo items.
- Local SQLite storage with WAL mode, transactions, idempotent writes, scoped numbered views, backups, and integrity checks.
- Tasks, notes, links, articles, and videos; URL canonicalization and duplicate handling are built in.
- Exact reminders, date-only deadlines, scheduled dates, defer dates, activity history, and Markdown exports.
- Default timezone: `Asia/Shanghai`. Existing items are migrated to Shanghai display time without changing stored exact instants.
- An optional stdio MCP server for clients outside Hermes, using the same database and core library.
- Migration support for both the current schema and the original unversioned Personal Memo database.

## Architecture

```text
Hermes native plugin ─┐
                      ├─> personal_memo_core ─> SQLite database
Optional MCP server ──┘
                      └─> backups and migrations
```

`personal_memo_core` contains all database, migration, backup, concurrency, and business logic. The Hermes plugin and MCP server are adapters only; they do not maintain separate database logic or data stores.

## Requirements

- Python 3.10 or newer.
- A working Hermes installation for the native plugin.
- No third-party Python package is required for the Hermes plugin or CLI.
- MCP is optional and requires the package listed in `memo-mcp/requirements-mcp.txt`.

## Install or upgrade

Download and extract the release package, then run:

```bash
cd personal-memo-hermes-layered
python3 install.py
hermes plugins enable personal-memo
```

Restart the Hermes gateway or start a new Hermes session after enabling the plugin.

The installer:

1. Replaces the previous core, skill, plugin, and MCP-server installation without creating component-directory backups.
2. Installs the shared core under `~/.hermes/lib/personal_memo_core`.
3. Installs the Hermes skill and plugin.
4. Installs the optional MCP server under `~/.hermes/mcp-servers/personal-memo`.
5. Creates a database backup and migrates existing Personal Memo data when needed.
6. Sets the memo timezone and item display timezones to `Asia/Shanghai`; stored exact timestamps remain unchanged.

## Use with Hermes

Talk to Hermes naturally. Examples:

```text
Remember: reply to the client next Friday at 3 PM.
Remind me tomorrow at 10 AM to pay rent.
Save https://example.com so I can read it later.
What do I need to do today?
Search my saved Python links.
Mark item 2 as complete.
Move the project meeting to next Monday afternoon.
```

The native plugin registers tools such as `memo_add`, `memo_list`, `memo_show`, `memo_search`, `memo_today`, `memo_update`, and `memo_transition`. Hermes selects them from natural-language requests; users normally do not need to run Python commands.

For a directly rendered, readable memo list that bypasses LLM reformatting, use `/memos`. Use `/memos all` to include completed, deleted, and archived items.

To verify the database manually:

```bash
HERMES_HOME="$HOME/.hermes" \
python3 "$HOME/.hermes/skills/productivity/personal-memo/scripts/memo.py" \
migrate-timezone Asia/Shanghai --json
```

## Optional MCP server

Use MCP only when another MCP-compatible client should access the same memo database. Do not add it to Hermes merely to use the native Hermes plugin.

Install the optional dependency:

```bash
python3 -m pip install -r ~/.hermes/mcp-servers/personal-memo/requirements-mcp.txt
```

Example MCP client configuration:

```json
{
  "mcpServers": {
    "personal-memo": {
      "command": "python3",
      "args": ["/home/YOUR_USER/.hermes/mcp-servers/personal-memo/server.py"],
      "env": {
        "HERMES_HOME": "/home/YOUR_USER/.hermes"
      }
    }
  }
}
```

The MCP server runs over stdio and exposes `memo_add`, `memo_list`, `memo_show`, `memo_search`, `memo_today`, `memo_update`, `memo_complete`, `memo_delete`, `memo_backup`, and `memo_timezone_migrate`.

## Project layout

```text
personal_memo_core/  Framework-neutral database, migrations, backup, and service logic
hermes-plugin/       Hermes schemas, registration, and tool handlers
memo-mcp/            Optional stdio MCP adapter
scripts/memo.py      Backward-compatible CLI adapter
tests/               Regression and Hermes plugin tests
```

## Data and safety

- The database is local by default: `~/.hermes/data/personal-memo/memos.sqlite3`.
- SQLite WAL mode, foreign keys, a busy timeout, and thread-local plugin connections protect concurrent access.
- Writes create rotating pre-write backups; manual backups can be created with the CLI or MCP tool.
- Deletion is soft by default. Permanent purge and backup restore require explicit confirmation tokens.
- The MCP server and Hermes plugin must point to the same `HERMES_HOME` or `PERSONAL_MEMO_DATA_DIR` to share data.

## Development

Run the full test suite from the repository root:

```bash
python3 -m unittest discover -s tests -v
```

## Contributing

Issues and pull requests are welcome. Keep business behavior in `personal_memo_core`; Hermes and MCP changes should remain thin adapters around that shared core.
