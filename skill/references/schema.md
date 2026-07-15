# Personal Memo schema and invariants

## Contents

- [Paths](#paths)
- [Runtime guarantees](#runtime-guarantees)
- [Schema version 2](#schema-version-2)
- [Items](#items)
- [Sources](#sources)
- [Item/source relationships](#itemsource-relationships)
- [Events](#events)
- [View snapshots](#view-snapshots)
- [Settings and migrations](#settings-and-migrations)
- [Reminder runs](#reminder-runs)
- [Sorting](#sorting)
- [Dates and time zones](#dates-and-time-zones)
- [State transitions](#state-transitions)
- [Link ingestion](#link-ingestion)
- [Backups and exports](#backups-and-exports)
- [Command reference](#command-reference)

## Paths

Resolve paths in this order:

1. Read `HERMES_HOME`.
2. Fall back to `~/.hermes` only when the environment variable is absent.
3. Read `PERSONAL_MEMO_DATA_DIR` or the command-level `--data-dir` override for the data directory.
4. Default to `${HERMES_HOME}/data/personal-memo`.

The normal layout is:

```text
${HERMES_HOME}/skills/productivity/personal-memo/
├── SKILL.md
├── agents/openai.yaml
├── scripts/memo.py
├── tests/test_memo.py
└── references/schema.md

${HERMES_HOME}/data/personal-memo/
├── memos.sqlite3
├── exports/
│   ├── current.md
│   └── archive.md
└── backups/
```

Directories use mode `0700`; the database, backups, and exports use `0600` when the platform permits POSIX modes.

## Runtime guarantees

The database is the only source of truth. The implementation enables:

- SQLite WAL mode
- foreign keys
- a 5000 ms busy timeout
- explicit `BEGIN IMMEDIATE` write transactions
- rollback on errors
- `PRAGMA user_version`
- ordered migrations in `schema_migrations`
- `PRAGMA integrity_check`
- `PRAGMA foreign_key_check`
- automatic safe backups before important writes

Never edit the database manually, run ad-hoc SQL from the model, or treat Markdown exports as writable state.

## Schema version 2

The logical tables are:

| Table | Purpose |
| --- | --- |
| `items` | Stable memo/task objects and business state |
| `sources` | Original and canonical URLs plus lightweight understanding |
| `item_sources` | Many-to-many item/source links with primary or related roles |
| `item_events` | Append-only operation history and undo audit |
| `view_snapshots` | Expiring numbered-view mappings scoped beyond a session |
| `settings` | Timezone, capture mode, TTL, and data directory |
| `schema_migrations` | Applied schema versions |
| `reminder_runs` | Reminder generation and delivery observations |

## Items

`items.id` is a stable, human-readable ID such as `M-20260714-A83F`. It never changes and is never replaced by a display number.

| Field | Meaning |
| --- | --- |
| `id` | Stable primary key |
| `title` | Short generated or user-supplied title |
| `content` | Full user meaning/original text |
| `item_type` | `task`, `note`, `link`, `article`, `video`, or `reference` |
| `status` | `active`, `completed`, `deleted`, or `archived`; link parsing is never an item status |
| `created_at` | UTC creation timestamp |
| `updated_at` | UTC most recent mutation timestamp |
| `due_at` | Due date or due timestamp |
| `due_precision` | `date`, `datetime`, `uncertain`, or null |
| `due_raw_text` | User's original time phrase |
| `remind_at` | Notification date/time |
| `remind_precision` | `date`, `datetime`, `uncertain`, or null |
| `scheduled_for` | Planned work date/time |
| `scheduled_precision` | `date`, `datetime`, `uncertain`, or null |
| `defer_until` | Do-not-recommend-before date/time |
| `defer_precision` | `date`, `datetime`, `uncertain`, or null |
| `timezone` | IANA timezone used to interpret local values |
| `priority_level` | `urgent`, `high`, `normal`, or `low` |
| `priority_source` | `user` or `inferred` |
| `priority_reason` | Brief explanation for an inferred/user priority |
| `completed_at` | Actual completion timestamp |
| `deleted_at` | Soft-deletion timestamp |
| `archived_at` | Archival timestamp |
| `title` | Short summary of linked source or user-provided content; the former `source_summary` field was merged into this column in schema v3 |
| `capture_source` | Private/group/platform capture origin |
| `tags_json` | JSON array of tags |
| `time_uncertain` | Boolean ambiguity marker |

Status and type values are protected by database checks.

## Sources

One item can own multiple `sources` rows. `sources.item_id` remains the primary owner for safe upgrades; all reads and relationships use `item_sources`, so a source can also be related to other items.

| Field | Meaning |
| --- | --- |
| `source_id` | Stable source key |
| `item_id` | Owning memo ID |
| `original_url` | Exact URL supplied by the user; never replace it |
| `canonical_url` | Tracking-cleaned URL used for duplicate checks |
| `source_type` | `web`, `video`, or another conservative source class |
| `source_title` | Page/video title |
| `author_or_channel` | Author, website, uploader, or channel |
| `platform` | Source host/platform |
| `published_at` | Reliably exposed publication timestamp |
| `duration_text` | Directly exposed duration text |
| `thumbnail_url` | Low-cost page metadata thumbnail URL |
| `fetched_at` | Last metadata attempt |
| `last_attempt_at` | Most recent bounded ingestion attempt |
| `ingest_status` | `processing`, `complete`, `partial`, or `failed` |
| `understanding_basis` | Evidence used for the summary |
| `summary` | Short summary of linked page/video or user-provided content |
| `key_points` | JSON list, normally no more than five |
| `suggested_action` | User action or conservative next step |
| `action_source` | `user` or `inferred` |
| `confidence` | Optional confidence value |
| `content_hash` | SHA-256 of fetched bytes when available |
| `access_note` | Failure, partial-access, or safety note |
| `failure_reason` | Sanitized reason for the most recent failed ingestion |
| `created_at` | Source capture timestamp |
| `updated_at` | Most recent source mutation timestamp |

## Item/source relationships

`item_sources` stores `(item_id, source_id)` as a composite primary key. `relationship` is `primary` or `related`. Every source has at least one link. When the primary owner is physically deleted but another item still links the source, ownership is transferred before deletion so the shared source is preserved.

Video `understanding_basis` is restricted in normal operation to:

- `title_and_description`
- `title_only`
- `metadata_only`
- `user_context`
- `mixed_metadata_and_user_context`
- `unavailable`

Do not use transcript, subtitle, audio, video-content, or vision-analysis bases.

## Events

Every business mutation appends an `item_events` row. Do not delete old rows to implement undo.

| Field | Meaning |
| --- | --- |
| `event_id` | Immutable event key |
| `item_id` | Current foreign-key link; null only after physical deletion |
| `stable_item_id` | Permanent memo ID retained for audit |
| `operation` | `create`, `update`, `complete`, `delete`, `archive`, `restore_*`, `link_parse`, `undo`, etc. |
| `before_data` | JSON snapshot before the operation |
| `after_data` | JSON snapshot after the operation |
| `original_user_instruction` | User wording that authorized the change |
| `created_at` | Event timestamp |
| `platform` | Chat platform |
| `user_id` | User scope |
| `chat_id` | Chat scope |
| `session_id` | Audit-only session identifier |
| `is_cron` | Whether the event was produced by cron |
| `target_event_id` | Event reversed by an undo |
| `idempotency_key` | Optional unique retry key for a business write |

Number resolution must never depend only on `session_id`.

## View snapshots

Each displayed list creates one `snapshot_id` with ordered rows:

- `scope_key`: `platform|user_id|chat_id|topic_id`; use `profile-global` only when `single_user_local=true`, otherwise store an unscoped display snapshot that cannot authorize numbered writes
- `view_kind`: active list, reminder mode, or another named view
- `item_number`: one-based display number
- `item_id`: stable ID target
- `created_at`: creation timestamp
- `expires_at`: normally 24 hours later

Resolve a number against the most recent snapshot for the same scope. Reject absent, expired, or missing numbers; never re-sort before resolution.

## Settings and migrations

Default settings:

| Key | Default |
| --- | --- |
| `timezone` | `Asia/Shanghai` |
| `capture_mode` | `conservative` |
| `snapshot_ttl_hours` | `24` |
| `data_directory` | Resolved data directory |
| `single_user_local` | `false`; only `true` permits profile-global numbered actions |

`capture_mode` may be `conservative` or `proactive`. Validate IANA timezones through `zoneinfo`.

`schema_migrations` records the version, timestamp, and description. Version 2 migrates every legacy link item from `processing` to `active`, preserves source processing state, backfills `item_sources`, and adds precise reminder/idempotency fields. Before every migration, create a versioned timestamp backup. Roll back failed migrations and never initialize over a newer unknown schema or clear user data to fix a migration.

## Reminder runs

`reminder_runs` records summary generation and exact reminder delivery attempts:

- mode (`morning`, `evening`, `manual`, or `dispatch`)
- start and completion timestamps
- number of items shown
- delivery target and observed delivery status
- error message when applicable
- numbered snapshot ID
- whether the run was a test
- exact reminder `item_id`, `remind_at`, delivery `dedupe_key`, attempt time, and success time

Exact reminder keys are derived from the stable item ID, saved reminder instant, and verified delivery target. A failed run may be retried; a successful key is unique and is never delivered again. A pending preparation has a short lease to prevent concurrent duplicate delivery.

A reminder may add a reminder-run row and a view snapshot. It must not change item status, times, priority, or other business fields.

## Sorting

Sort active items deterministically.

1. Put every item with a due date/time before every item without one.
2. Within the due group, put overdue items first, then sort by due instant.
3. Break equal due times with `priority_level`, creation time, and stable ID.
4. Within the no-due group, sort by `priority_level`, `scheduled_for`, creation time, and stable ID.

Priority order is `urgent`, `high`, `normal`, `low`. `priority_source` controls authority—an inferred priority cannot overwrite a user-set priority—but never changes formal sorting by itself.

Suggestions are a separate view. They may score overdue, within-12-hour due, current schedule, and priority, but they never rewrite formal order or saved fields. Return no more than three.

## Dates and time zones

Persist date-only values as `YYYY-MM-DD`. Parse them internally as end-of-day for ordering, but display “未指定具体时间.” A date-only due item becomes overdue only after its local calendar date has passed.

Persist exact timestamps as RFC 3339 UTC. Keep the IANA timezone for display. Persist date-only values as local calendar dates with the corresponding precision field. Preserve relative wording in `due_raw_text`. Store `uncertain` precision or an ambiguity flag instead of inventing a date for phrases such as “过几天” or “有空”.

Keep these concepts separate:

- `due_at`: deadline
- `remind_at`: notification time
- `scheduled_for`: work plan
- `defer_until`: recommendation suppression

## State transitions

Allowed user-authorized transitions include:

- `active` → `completed`
- any non-physically-deleted state → `deleted` (soft)
- any non-physically-deleted state → `archived`
- `completed`/`deleted`/`archived` → `active`

Clear status timestamps that no longer apply. Never complete an item because its due time passed. Never treat archive as completion.

Single-item complete/delete/restore/archive commands constitute explicit action only after the agent uniquely resolves the user's instruction. Confirm bulk and physical operations twice before execution.

## Link ingestion

Phase 1 must commit before any network access:

1. Create the item with `status=active`.
2. Create every source with `ingest_status=processing`.
3. Preserve the original URL and user text.
4. Commit the transaction and return the stable ID.

Phase 2 performs a bounded metadata request. Reject loopback, private, link-local, multicast, and reserved addresses. Limit bytes and time. Do not execute HTML, scripts, or instructions. Use `complete`, `partial`, or `failed` without deleting the captured source.

Canonicalization removes fragments and common tracking query parameters. Duplicate detection checks original URL and canonical URL. A second original URL may be attached to an existing item so the exact user-supplied form remains available.

## Backups and exports

Use the SQLite backup API, not live-file copying. Rotate:

- `prewrite-v<schema>-*.sqlite3`: latest 10
- `daily-v<schema>-*.sqlite3`: latest 30; create the first one automatically before the first write each day
- `migration-v<schema>-*.sqlite3`: latest 10
- `pre-restore-v<schema>-*.sqlite3`: latest 10

Validate a restore candidate with `PRAGMA integrity_check`. Back up the current database before replacing it. Require the exact confirmation token `RESTORE`.

Generate `current.md` from active items in formal order. Generate `archive.md` from completed, deleted, and archived items. Rebuild both atomically; never import them.

## Command reference

Run `python3 scripts/memo.py COMMAND --help` for exact flags.

| Command | Purpose |
| --- | --- |
| `init` | Initialize or migrate safely |
| `add` | Add structured content and phase-one URLs |
| `capture` | Apply conservative/proactive natural-text capture |
| `list` | Deterministic list plus optional numbered snapshot |
| `show` | Resolve stable ID or scoped number |
| `search` | Search item and source text |
| `today` | Show overdue, due-today, scheduled-today, and suggested work |
| `activity` | Query actual completion, deletion, or archive timestamps |
| `update` | Edit fields, add notes/tags, or clear time fields |
| `complete` | Explicitly complete one item |
| `restore-item` | Restore one completed/deleted/archived item |
| `delete` | Soft-delete one item |
| `archive` | Archive without completing |
| `purge` | Physically delete one item after an exact second-confirmation token |
| `history` | Read immutable operation history |
| `undo` | Restore the state before one event and append an undo event |
| `retry-source` | Retry bounded metadata parsing |
| `source-update` | Store structured metadata obtained by the agent |
| `source-link` | Relate one existing source to another item |
| `dispatch-reminders` | Prepare due exact reminders or record real delivery results |
| `reminder` | List all unfinished items and up to three suggestions |
| `export` | Rebuild Markdown exports |
| `backup` | Create a daily backup |
| `restore-backup` | Validate and restore a backup with confirmation |
| `config` | Read or change persistent preferences (`settings` remains a compatibility alias) |
| `migrate-timezone` | Change default and item display timezones while preserving exact instants |
| `cron-plan` | Validate the three expected Hermes cron definitions |
| `doctor` | Check database, paths, backups, skill, cron, gateway, and delivery |
| `validate` | Check integrity and core invariants |
