#!/usr/bin/env python3
"""Persistent personal memo and task store for the personal-memo skill.

The SQLite database is the only source of truth.  Markdown files are exports.
This module intentionally uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import html
import ipaddress
import json
import os
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import textwrap
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA_VERSION = 3
DEFAULT_TIMEZONE = "Asia/Shanghai"
SNAPSHOT_TTL_HOURS = 24
MAX_FETCH_BYTES = 524_288
ITEM_TYPES = {"task", "note", "link", "article", "video", "reference"}
ITEM_STATUSES = {"active", "completed", "deleted", "archived"}
TIME_PRECISIONS = {"date", "datetime", "uncertain"}
PRIORITY_LEVELS = {"urgent", "high", "normal", "low"}
PRIORITY_SOURCES = {"user", "inferred"}
INGEST_STATUSES = {"processing", "complete", "partial", "failed"}
VIDEO_BASES = {
    "title_and_description",
    "title_only",
    "metadata_only",
    "user_context",
    "mixed_metadata_and_user_context",
    "unavailable",
}
VIDEO_HOST_MARKERS = (
    "youtube.com",
    "youtu.be",
    "vimeo.com",
    "bilibili.com",
    "nicovideo.jp",
    "dailymotion.com",
    "tiktok.com",
)
TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "source",
}
SENSITIVE_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:api[_ -]?key|access[_ -]?token|secret|password|passwd)\s*[:=]\s*\S+", re.I),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,})\b"),
    re.compile(r"\b(?:验证码|短信码|verification code)\s*[:：]?\s*\d{4,8}\b", re.I),
)
URL_RE = re.compile(r"https?://[^\s<>\]\[(){}\"']+", re.I)

MORNING_PROMPT = """加载 personal-memo skill，以早间提醒模式从持久化数据库读取备忘录。

列出所有未完成条目，并给出从当前时间开始未来 12 小时内最多 3 项建议执行顺序。

只允许读取、排序、生成提醒和更新本次列表编号快照。

不得完成、删除、归档、改期、修改优先级或改变任何条目的业务状态。"""

EVENING_PROMPT = MORNING_PROMPT.replace("早间提醒", "晚间提醒")

DISPATCH_PROMPT = """加载 personal-memo skill，从持久化数据库分发已经到期的精确时间提醒。

先调用 dispatch-reminders，并只处理返回的到期条目。成功或失败后，用返回的 run_id 记录真实投递结果。

只允许读取、生成提醒、记录投递结果和更新本次列表编号快照。

不得完成、删除、归档、改期、修改优先级或改变任何条目的业务状态。"""

CRON_DEFINITIONS = (
    ("personal-memo-reminder-dispatch", "*/5 * * * *", DISPATCH_PROMPT),
    ("personal-memo-morning", "0 9 * * *", MORNING_PROMPT),
    ("personal-memo-evening", "0 20 * * *", EVENING_PROMPT),
)


class MemoError(RuntimeError):
    """Expected user-facing error."""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso_now() -> str:
    return utc_now().isoformat()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_json(value: str | None, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def check_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise MemoError(f"Unknown timezone: {name}") from exc


def parse_datetime(value: str, timezone: str = DEFAULT_TIMEZONE) -> dt.datetime:
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        date_value = dt.date.fromisoformat(text)
        return dt.datetime.combine(date_value, dt.time.max, check_timezone(timezone))
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MemoError(f"Invalid ISO date/time: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=check_timezone(timezone))
    return parsed


def normalize_temporal(
    value: str | None,
    timezone: str,
    precision: str | None = None,
) -> tuple[str | None, str | None]:
    if value is None:
        return None, precision
    text = value.strip()
    if not text:
        return None, precision
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        dt.date.fromisoformat(text)
        return text, precision or "date"
    parsed = parse_datetime(text, timezone)
    return parsed.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat(), precision or "datetime"


def truncate(text: str | None, limit: int) -> str:
    clean = " ".join((text or "").split())
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def extract_urls(text: str) -> list[str]:
    return [match.group(0).rstrip(".,;:!?，。；：！？") for match in URL_RE.finditer(text)]


def contains_sensitive(text: str) -> bool:
    return any(pattern.search(text) for pattern in SENSITIVE_PATTERNS)


def redact_sensitive(text: str) -> str:
    redacted = text
    for pattern in SENSITIVE_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def canonicalize_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise MemoError(f"Unsupported URL: {url}")
    host = parsed.hostname.lower().rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise MemoError(f"Invalid URL host: {url}") from exc
    port = parsed.port
    netloc = host
    if port and not ((parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443)):
        netloc = f"{host}:{port}"
    query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    filtered = [
        (key, value)
        for key, value in query_pairs
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_QUERY_KEYS
    ]
    query = urllib.parse.urlencode(filtered, doseq=True)
    path = parsed.path or "/"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, path, query, ""))


def source_type_for_url(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return "video" if any(marker in host for marker in VIDEO_HOST_MARKERS) else "web"


def scope_key(
    platform: str | None,
    user_id: str | None,
    chat_id: str | None,
    topic_id: str | None,
    *,
    allow_profile_global: bool = False,
) -> str:
    if platform and user_id and chat_id:
        pieces = [platform, user_id, chat_id]
        if topic_id:
            pieces.append(topic_id)
        return "|".join(pieces)
    return "profile-global" if allow_profile_global else "unscoped"


@dataclass(frozen=True)
class MemoPaths:
    hermes_home: Path
    data_dir: Path
    db_path: Path
    exports_dir: Path
    backups_dir: Path
    current_export: Path
    archive_export: Path
    skill_root: Path

    @classmethod
    def resolve(cls, data_dir: str | os.PathLike[str] | None = None) -> "MemoPaths":
        home = Path(os.environ.get("PERSONAL_MEMO_HOME") or os.environ.get("HERMES_HOME") or "~/.hermes").expanduser().resolve()
        configured = data_dir or os.environ.get("PERSONAL_MEMO_DATA_DIR")
        data = Path(configured).expanduser().resolve() if configured else home / "data" / "personal-memo"
        exports = data / "exports"
        backups = data / "backups"
        return cls(
            hermes_home=home,
            data_dir=data,
            db_path=data / "memos.sqlite3",
            exports_dir=exports,
            backups_dir=backups,
            current_export=exports / "current.md",
            archive_export=exports / "archive.md",
            skill_root=Path(os.environ.get("PERSONAL_MEMO_SKILL_ROOT") or Path(__file__).resolve().parents[1]).expanduser().resolve(),
        )

    def ensure(self) -> None:
        for directory in (self.data_dir, self.exports_dir, self.backups_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            with contextlib.suppress(OSError):
                directory.chmod(0o700)


ITEMS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    item_type TEXT NOT NULL CHECK (item_type IN ('task','note','link','article','video','reference')),
    status TEXT NOT NULL CHECK (status IN ('active','completed','deleted','archived')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    due_at TEXT,
    due_precision TEXT CHECK (due_precision IS NULL OR due_precision IN ('date','datetime','uncertain')),
    due_raw_text TEXT,
    remind_at TEXT,
    remind_precision TEXT CHECK (remind_precision IS NULL OR remind_precision IN ('date','datetime','uncertain')),
    scheduled_for TEXT,
    scheduled_precision TEXT CHECK (scheduled_precision IS NULL OR scheduled_precision IN ('date','datetime','uncertain')),
    defer_until TEXT,
    defer_precision TEXT CHECK (defer_precision IS NULL OR defer_precision IN ('date','datetime','uncertain')),
    timezone TEXT NOT NULL,
    priority_level TEXT NOT NULL CHECK (priority_level IN ('urgent','high','normal','low')),
    priority_source TEXT NOT NULL CHECK (priority_source IN ('user','inferred')),
    priority_reason TEXT,
    completed_at TEXT,
    deleted_at TEXT,
    archived_at TEXT,
    capture_source TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    time_uncertain INTEGER NOT NULL DEFAULT 0 CHECK (time_uncertain IN (0,1))
);
"""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
""" + ITEMS_TABLE_SQL + """
CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    original_url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_title TEXT,
    author_or_channel TEXT,
    platform TEXT,
    published_at TEXT,
    duration_text TEXT,
    thumbnail_url TEXT,
    fetched_at TEXT,
    last_attempt_at TEXT,
    ingest_status TEXT NOT NULL CHECK (ingest_status IN ('processing','complete','partial','failed')),
    understanding_basis TEXT,
    summary TEXT,
    key_points TEXT NOT NULL DEFAULT '[]',
    suggested_action TEXT,
    action_source TEXT CHECK (action_source IS NULL OR action_source IN ('user','inferred')),
    confidence REAL,
    content_hash TEXT,
    access_note TEXT,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS item_sources (
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
    relationship TEXT NOT NULL DEFAULT 'primary' CHECK (relationship IN ('primary','related')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (item_id, source_id)
);

CREATE TABLE IF NOT EXISTS item_events (
    event_id TEXT PRIMARY KEY,
    item_id TEXT REFERENCES items(id) ON DELETE SET NULL,
    stable_item_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    before_data TEXT,
    after_data TEXT,
    original_user_instruction TEXT,
    created_at TEXT NOT NULL,
    platform TEXT,
    user_id TEXT,
    chat_id TEXT,
    session_id TEXT,
    is_cron INTEGER NOT NULL DEFAULT 0 CHECK (is_cron IN (0,1)),
    target_event_id TEXT,
    idempotency_key TEXT
);

CREATE TABLE IF NOT EXISTS view_snapshots (
    snapshot_id TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    view_kind TEXT NOT NULL,
    item_number INTEGER NOT NULL,
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, item_number)
);

CREATE TABLE IF NOT EXISTS reminder_runs (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    item_count INTEGER NOT NULL DEFAULT 0,
    delivery_target TEXT,
    delivery_status TEXT,
    error_message TEXT,
    snapshot_id TEXT,
    is_test INTEGER NOT NULL DEFAULT 0 CHECK (is_test IN (0,1)),
    item_id TEXT REFERENCES items(id) ON DELETE SET NULL,
    remind_at TEXT,
    dedupe_key TEXT,
    attempted_at TEXT,
    success_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_status_due ON items(status, due_at);
CREATE INDEX IF NOT EXISTS idx_items_completed ON items(completed_at);
CREATE INDEX IF NOT EXISTS idx_sources_item ON sources(item_id);
CREATE INDEX IF NOT EXISTS idx_sources_canonical ON sources(canonical_url);
CREATE INDEX IF NOT EXISTS idx_sources_ingest ON sources(ingest_status);
CREATE INDEX IF NOT EXISTS idx_item_sources_source ON item_sources(source_id, item_id);
CREATE INDEX IF NOT EXISTS idx_events_item_created ON item_events(stable_item_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_idempotency ON item_events(idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_snapshots_scope_created ON view_snapshots(scope_key, created_at);
CREATE INDEX IF NOT EXISTS idx_reminder_due ON items(status, remind_at, remind_precision);
CREATE INDEX IF NOT EXISTS idx_reminder_dedupe ON reminder_runs(dedupe_key, delivery_status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reminder_success ON reminder_runs(dedupe_key)
    WHERE dedupe_key IS NOT NULL AND delivery_status='success';
"""


class MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.in_title = False
        self.meta: dict[str, str] = {}
        self.canonical: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs if value is not None}
        if tag.lower() == "title":
            self.in_title = True
        elif tag.lower() == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            content = values.get("content")
            if key and content and key not in self.meta:
                self.meta[key] = content
        elif tag.lower() == "link" and (values.get("rel") or "").lower() == "canonical":
            self.canonical = values.get("href")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)

    @property
    def title(self) -> str | None:
        raw = self.meta.get("og:title") or self.meta.get("twitter:title") or " ".join(self.title_parts)
        return truncate(html.unescape(raw), 300) or None

    @property
    def description(self) -> str | None:
        raw = self.meta.get("og:description") or self.meta.get("twitter:description") or self.meta.get("description")
        return truncate(html.unescape(raw or ""), 600) or None


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate every redirect target before urllib follows it."""

    max_redirections = 5

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        _assert_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _assert_public_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise MemoError("Only http and https URLs may be fetched")
    if parsed.username is not None or parsed.password is not None:
        raise MemoError("Refusing to fetch a URL containing embedded credentials")
    host = parsed.hostname
    if not host:
        raise MemoError("URL has no hostname")
    if host.lower() in {"localhost", "localhost.localdomain"}:
        raise MemoError("Refusing to fetch a local address")
    try:
        addresses = {
            entry[4][0]
            for entry in socket.getaddrinfo(
                host,
                parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as exc:
        raise MemoError(f"DNS lookup failed: {exc}") from exc
    if not addresses or len(addresses) > 16:
        raise MemoError("DNS returned an unsafe number of addresses")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise MemoError("Refusing to fetch a private, loopback, or reserved address")


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


class MemoStore:
    def __init__(
        self,
        paths: MemoPaths | None = None,
        *,
        prewrite_backups: bool = True,
    ) -> None:
        self.paths = paths or MemoPaths.resolve()
        self.prewrite_backups = prewrite_backups
        self.paths.ensure()
        self.conn: sqlite3.Connection | None = None
        self.initialization = self.initialize()

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "MemoStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.paths.db_path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @staticmethod
    def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}

    def _is_legacy_v0_schema(self) -> bool:
        """Recognize the first released Hermes memo schema, without guessing."""
        assert self.conn is not None
        required = {
            "items": {"id", "title", "status", "timezone", "priority_level"},
            "sources": {"id", "original_url", "canonical_url", "ingest_status"},
            "item_sources": {"item_id", "source_id"},
            "tags": {"id", "name"},
            "item_tags": {"item_id", "tag_id"},
            "item_events": {"event_id", "item_id", "operation", "created_at"},
            "view_snapshots": {"snapshot_id", "position", "item_id", "scope_key"},
            "reminder_runs": {"id", "item_id", "run_kind", "attempted_at"},
            "settings": {"key", "value", "updated_at"},
        }
        tables = {str(row[0]) for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return all(name in tables and columns <= self._column_names(self.conn, name) for name, columns in required.items())

    def _migrate_legacy_v0_to_v2(self) -> None:
        """Convert the original unversioned Hermes memo schema to schema v2.

        Legacy tables are retained as ``legacy_v0_*`` until the user chooses to
        remove them, and a SQLite backup is made by ``initialize`` beforehand.
        """
        assert self.conn is not None
        conn = self.conn
        legacy_tables = ("items", "sources", "item_sources", "tags", "item_tags", "item_events", "view_snapshots", "reminder_runs", "settings", "schema_migrations")
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.executescript("BEGIN IMMEDIATE;\n")
            for (index_name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_autoindex_%'"):
                conn.execute(f'DROP INDEX IF EXISTS "{index_name}"')
            for table in legacy_tables:
                if self._column_names(conn, table):
                    conn.execute(f'ALTER TABLE "{table}" RENAME TO "legacy_v0_{table}"')
            conn.executescript(SCHEMA_SQL)

            tags_by_item: dict[str, list[str]] = {}
            for row in conn.execute(
                "SELECT it.item_id,t.name FROM legacy_v0_item_tags it "
                "JOIN legacy_v0_tags t ON t.id=it.tag_id ORDER BY t.name"
            ):
                tags_by_item.setdefault(str(row[0]), []).append(str(row[1]))
            for row in conn.execute("SELECT * FROM legacy_v0_items"):
                item = dict(row)
                item_type = str(item.get("item_type") or "task")
                if item_type == "memo":
                    item_type = "note"
                if item_type not in ITEM_TYPES:
                    item_type = "task"
                status = str(item.get("status") or "active")
                if status == "processing":
                    status = "active"
                if status not in ITEM_STATUSES:
                    status = "active"
                priority = str(item.get("priority_level") or "normal")
                if priority not in PRIORITY_LEVELS:
                    priority = "normal"
                priority_source = str(item.get("priority_source") or "inferred")
                if priority_source not in PRIORITY_SOURCES:
                    priority_source = "inferred"
                conn.execute(
                    """INSERT INTO items(id,title,content,item_type,status,created_at,updated_at,due_at,due_precision,due_raw_text,remind_at,remind_precision,scheduled_for,scheduled_precision,defer_until,defer_precision,timezone,priority_level,priority_source,priority_reason,completed_at,deleted_at,archived_at,capture_source,tags_json,time_uncertain)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (item["id"], item.get("title") or "Untitled", item.get("content") or "", item_type, status,
                     item.get("created_at") or iso_now(), item.get("updated_at") or item.get("created_at") or iso_now(),
                     item.get("due_at"), item.get("due_precision"), item.get("due_raw_text"), item.get("remind_at"), item.get("remind_precision"),
                     item.get("scheduled_for"), item.get("scheduled_precision"), item.get("defer_until"), item.get("defer_precision"),
                     item.get("timezone") or DEFAULT_TIMEZONE, priority, priority_source, item.get("priority_reason"), item.get("completed_at"),
                     item.get("deleted_at"), item.get("archived_at"), item.get("capture_source"),
                     json_dumps(tags_by_item.get(str(item["id"]), [])), 0),
                )

            source_owner = {str(row["source_id"]): str(row["item_id"]) for row in conn.execute("SELECT item_id,source_id FROM legacy_v0_item_sources")}
            valid_items = {str(row[0]) for row in conn.execute("SELECT id FROM items")}
            for row in conn.execute("SELECT * FROM legacy_v0_sources"):
                source = dict(row)
                owner = source_owner.get(str(source["id"]))
                if owner not in valid_items:
                    continue
                status = str(source.get("ingest_status") or "partial")
                if status not in INGEST_STATUSES:
                    status = "partial"
                conn.execute(
                    """INSERT INTO sources(source_id,item_id,original_url,canonical_url,source_type,source_title,author_or_channel,platform,published_at,duration_text,thumbnail_url,fetched_at,last_attempt_at,ingest_status,understanding_basis,summary,key_points,suggested_action,action_source,confidence,content_hash,access_note,failure_reason,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (source["id"], owner, source.get("original_url") or "", source.get("canonical_url") or source.get("original_url") or "",
                     source.get("source_type") or "link", source.get("source_title"), source.get("author_or_channel"), source.get("platform"), source.get("published_at"),
                     source.get("duration_text"), source.get("thumbnail_url"), source.get("fetched_at"), source.get("last_attempt_at") or source.get("fetched_at"), status,
                     source.get("understanding_basis"), source.get("summary"), source.get("key_points") or "[]", source.get("suggested_action"), source.get("action_source"),
                     source.get("confidence"), source.get("content_hash"), source.get("access_note"), source.get("failure_reason"), source.get("created_at") or iso_now(), source.get("updated_at") or source.get("created_at") or iso_now()),
                )
            for row in conn.execute("SELECT item_id,source_id FROM legacy_v0_item_sources"):
                if str(row[0]) in valid_items and conn.execute("SELECT 1 FROM sources WHERE source_id=?", (row[1],)).fetchone():
                    conn.execute("INSERT OR IGNORE INTO item_sources(item_id,source_id,relationship,created_at) VALUES(?,?,?,?)", (row[0], row[1], "primary", iso_now()))
            for row in conn.execute("SELECT * FROM legacy_v0_item_events"):
                event = dict(row)
                stable = str(event.get("item_id") or f"legacy-event-{event['event_id']}")
                conn.execute("INSERT INTO item_events(event_id,item_id,stable_item_id,operation,before_data,after_data,original_user_instruction,created_at,platform,user_id,chat_id,session_id,is_cron,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (event["event_id"], event.get("item_id"), stable, event.get("operation") or "legacy", event.get("before_data"), event.get("after_data"), event.get("original_user_instruction"), event.get("created_at") or iso_now(), event.get("platform"), event.get("user_id"), event.get("chat_id"), event.get("session_id"), int(event.get("is_cron") or 0), event.get("idempotency_key")))
            for row in conn.execute("SELECT * FROM legacy_v0_view_snapshots"):
                view = dict(row)
                if str(view.get("item_id")) in valid_items:
                    conn.execute("INSERT OR IGNORE INTO view_snapshots(snapshot_id,scope_key,view_kind,item_number,item_id,created_at,expires_at) VALUES(?,?,?,?,?,?,?)", (view["snapshot_id"], view.get("scope_key") or "unscoped", view.get("query_type") or "active", view.get("position") or 1, view["item_id"], view.get("created_at") or iso_now(), view.get("expires_at") or iso_now()))
            for row in conn.execute("SELECT * FROM legacy_v0_reminder_runs"):
                reminder = dict(row)
                result = str(reminder.get("result") or "").lower()
                status = "success" if result in {"success", "sent", "ok"} else "failed" if result in {"failed", "error"} else None
                conn.execute("INSERT INTO reminder_runs(run_id,mode,started_at,completed_at,item_count,delivery_target,delivery_status,error_message,is_test,item_id,remind_at,dedupe_key,attempted_at,success_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (reminder["id"], reminder.get("run_kind") or "legacy", reminder.get("attempted_at") or iso_now(), reminder.get("success_at"), 1 if reminder.get("item_id") else 0, reminder.get("target"), status, reminder.get("error"), 0, reminder.get("item_id") if str(reminder.get("item_id")) in valid_items else None, reminder.get("remind_at"), reminder.get("dedupe_key"), reminder.get("attempted_at"), reminder.get("success_at")))
            conn.execute("INSERT OR REPLACE INTO settings(key,value,updated_at) SELECT key,value,updated_at FROM legacy_v0_settings")
            conn.execute("PRAGMA user_version=2")
            conn.execute("INSERT OR REPLACE INTO schema_migrations(version,applied_at,description) VALUES(?,?,?)", (2, iso_now(), "Migrated original unversioned Hermes memo schema"))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

    def _migrate_v1_to_v2(self) -> None:
        """Move link processing to sources and add precise reminder support."""
        assert self.conn is not None
        conn = self.conn
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            with transaction(conn):
                conn.execute("DROP TABLE IF EXISTS items_v2")
                conn.execute(ITEMS_TABLE_SQL.replace("IF NOT EXISTS items", "items_v2", 1))
                conn.execute(
                    """INSERT INTO items_v2(
                        id,title,content,item_type,status,created_at,updated_at,
                        due_at,due_precision,due_raw_text,
                        remind_at,remind_precision,scheduled_for,scheduled_precision,
                        defer_until,defer_precision,timezone,
                        priority_level,priority_source,priority_reason,
                        completed_at,deleted_at,archived_at,capture_source,
                        tags_json,time_uncertain
                    )
                    SELECT
                        id,COALESCE(NULLIF(source_summary,''),title),content,item_type,
                        CASE WHEN status='processing' THEN 'active' ELSE status END,
                        created_at,updated_at,due_at,due_precision,due_raw_text,
                        remind_at,
                        CASE WHEN remind_at IS NULL THEN NULL
                             WHEN time_uncertain=1 THEN 'uncertain'
                             WHEN length(remind_at)=10 THEN 'date' ELSE 'datetime' END,
                        scheduled_for,
                        CASE WHEN scheduled_for IS NULL THEN NULL
                             WHEN time_uncertain=1 THEN 'uncertain'
                             WHEN length(scheduled_for)=10 THEN 'date' ELSE 'datetime' END,
                        defer_until,
                        CASE WHEN defer_until IS NULL THEN NULL
                             WHEN time_uncertain=1 THEN 'uncertain'
                             WHEN length(defer_until)=10 THEN 'date' ELSE 'datetime' END,
                        timezone,priority_level,priority_source,priority_reason,
                        completed_at,deleted_at,archived_at,capture_source,
                        tags_json,time_uncertain
                    FROM items"""
                )
                conn.execute("DROP TABLE items")
                conn.execute("ALTER TABLE items_v2 RENAME TO items")

                source_columns = self._column_names(conn, "sources")
                for name in ("thumbnail_url", "last_attempt_at", "failure_reason", "updated_at"):
                    if name not in source_columns:
                        conn.execute(f"ALTER TABLE sources ADD COLUMN {name} TEXT")
                conn.execute(
                    "UPDATE sources SET updated_at=COALESCE(updated_at,fetched_at,created_at), "
                    "last_attempt_at=COALESCE(last_attempt_at,fetched_at)"
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS item_sources (
                        item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                        source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
                        relationship TEXT NOT NULL DEFAULT 'primary'
                            CHECK (relationship IN ('primary','related')),
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (item_id, source_id)
                    )"""
                )
                conn.execute(
                    "INSERT OR IGNORE INTO item_sources(item_id,source_id,relationship,created_at) "
                    "SELECT item_id,source_id,'primary',created_at FROM sources"
                )

                event_columns = self._column_names(conn, "item_events")
                if "idempotency_key" not in event_columns:
                    conn.execute("ALTER TABLE item_events ADD COLUMN idempotency_key TEXT")

                reminder_columns = self._column_names(conn, "reminder_runs")
                for name in ("item_id", "remind_at", "dedupe_key", "attempted_at", "success_at"):
                    if name not in reminder_columns:
                        conn.execute(f"ALTER TABLE reminder_runs ADD COLUMN {name} TEXT")

                conn.execute("CREATE INDEX IF NOT EXISTS idx_items_status_due ON items(status,due_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_items_completed ON items(completed_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_reminder_due ON items(status,remind_at,remind_precision)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_item_sources_source ON item_sources(source_id,item_id)")
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_idempotency "
                    "ON item_events(idempotency_key) WHERE idempotency_key IS NOT NULL"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_reminder_dedupe "
                    "ON reminder_runs(dedupe_key,delivery_status)"
                )
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_reminder_success ON reminder_runs(dedupe_key) "
                    "WHERE dedupe_key IS NOT NULL AND delivery_status='success'"
                )
                violations = list(conn.execute("PRAGMA foreign_key_check"))
                if violations:
                    raise MemoError(f"Migration would create {len(violations)} foreign-key violation(s)")
                conn.execute("PRAGMA user_version=2")
                conn.execute(
                    "INSERT OR REPLACE INTO schema_migrations(version,applied_at,description) VALUES(?,?,?)",
                    (2, iso_now(), "Active link items, time precision, item_sources, idempotency, and exact reminders"),
                )
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

    def _migrate_v2_to_v3(self) -> None:
        """Use items.title as the single memo summary and remove source_summary."""
        assert self.conn is not None
        conn = self.conn
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            with transaction(conn):
                conn.execute("DROP TABLE IF EXISTS items_v3")
                conn.execute(ITEMS_TABLE_SQL.replace("IF NOT EXISTS items", "items_v3", 1))
                conn.execute(
                    """INSERT INTO items_v3(
                        id,title,content,item_type,status,created_at,updated_at,
                        due_at,due_precision,due_raw_text,remind_at,remind_precision,
                        scheduled_for,scheduled_precision,defer_until,defer_precision,
                        timezone,priority_level,priority_source,priority_reason,
                        completed_at,deleted_at,archived_at,capture_source,tags_json,time_uncertain
                    )
                    SELECT id,COALESCE(NULLIF(source_summary,''),title),content,item_type,status,
                           created_at,updated_at,due_at,due_precision,due_raw_text,
                           remind_at,remind_precision,scheduled_for,scheduled_precision,
                           defer_until,defer_precision,timezone,priority_level,priority_source,
                           priority_reason,completed_at,deleted_at,archived_at,capture_source,
                           tags_json,time_uncertain
                    FROM items"""
                )
                conn.execute("DROP TABLE items")
                conn.execute("ALTER TABLE items_v3 RENAME TO items")
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                conn.execute(
                    "INSERT OR REPLACE INTO schema_migrations(version,applied_at,description) VALUES(?,?,?)",
                    (3, iso_now(), "Merged source_summary into items.title and removed the redundant column"),
                )
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

    def _secure_files(self) -> None:
        for path in (self.paths.db_path, Path(str(self.paths.db_path) + "-wal"), Path(str(self.paths.db_path) + "-shm")):
            if path.exists():
                with contextlib.suppress(OSError):
                    path.chmod(0o600)
        for directory in (self.paths.backups_dir, self.paths.exports_dir):
            if directory.exists():
                with contextlib.suppress(OSError):
                    directory.chmod(0o700)
                for path in directory.iterdir():
                    if path.is_file():
                        with contextlib.suppress(OSError):
                            path.chmod(0o600)

    def initialize(self) -> dict[str, Any]:
        existed = self.paths.db_path.exists() and self.paths.db_path.stat().st_size > 0
        if self.conn is None:
            self.conn = self._open()
        assert self.conn is not None
        current = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if current > SCHEMA_VERSION:
            raise MemoError(f"Database schema {current} is newer than supported schema {SCHEMA_VERSION}")
        if current == 0 and existed:
            known_tables = {
                str(row[0])
                for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "items" in known_tables:
                required = {"id", "status", "time_uncertain", "remind_at", "scheduled_for", "defer_until"}
                if required <= self._column_names(self.conn, "items"):
                    with transaction(self.conn):
                        self.conn.execute(
                            "CREATE TABLE IF NOT EXISTS schema_migrations ("
                            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, description TEXT NOT NULL)"
                        )
                        self.conn.execute(
                            "CREATE TABLE IF NOT EXISTS settings ("
                            "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
                        )
                        self.conn.execute("PRAGMA user_version=1")
                        self.conn.execute(
                            "INSERT OR REPLACE INTO schema_migrations(version,applied_at,description) VALUES(?,?,?)",
                            (1, iso_now(), "Recognized legacy personal-memo schema"),
                        )
                    current = 1
                elif self._is_legacy_v0_schema():
                    self._backup(prefix="migration", keep=10)
                    self._migrate_legacy_v0_to_v2()
                    current = SCHEMA_VERSION
                else:
                    raise MemoError("Existing unversioned database has an unknown schema; it was not modified")
        if existed and current < SCHEMA_VERSION:
            self._backup(prefix="migration", keep=10)
        if current < 1:
            try:
                self.conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA_SQL + f"\nPRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;")
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    self.conn.rollback()
                raise
            with transaction(self.conn):
                self.conn.executemany(
                    "INSERT OR REPLACE INTO schema_migrations(version,applied_at,description) VALUES(?,?,?)",
                    (
                        (1, iso_now(), "Initial personal-memo schema"),
                        (2, iso_now(), "Active link items, time precision, item_sources, idempotency, and exact reminders"),
                    ),
                )
            current = SCHEMA_VERSION
        elif current == 1:
            self._migrate_v1_to_v2()
            current = 2
        elif current == 2:
            self._migrate_v2_to_v3()
            current = SCHEMA_VERSION
        defaults = {
            "timezone": DEFAULT_TIMEZONE,
            "capture_mode": "conservative",
            "snapshot_ttl_hours": str(SNAPSHOT_TTL_HOURS),
            "data_directory": str(self.paths.data_dir),
            "single_user_local": "false",
        }
        with transaction(self.conn):
            for key, value in defaults.items():
                self.conn.execute(
                    "INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES(?,?,?)",
                    (key, value, iso_now()),
                )
        self._secure_files()
        return {
            "initialized": True,
            "already_existed": existed,
            "hermes_home": str(self.paths.hermes_home),
            "database": str(self.paths.db_path),
            "schema_version": SCHEMA_VERSION,
        }

    def _backup(self, prefix: str, keep: int) -> Path:
        assert self.conn is not None
        self.paths.backups_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S.%f")
        version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        destination = self.paths.backups_dir / f"{prefix}-v{version}-{stamp}.sqlite3"
        target = sqlite3.connect(destination)
        try:
            self.conn.backup(target)
        finally:
            target.close()
        destination.chmod(0o600)
        backups = sorted(self.paths.backups_dir.glob(f"{prefix}-*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in backups[keep:]:
            with contextlib.suppress(OSError):
                old.unlink()
        return destination

    def prewrite_backup(self) -> Path | None:
        if not self.prewrite_backups or not self.paths.db_path.exists():
            return None
        today = dt.datetime.now().strftime("%Y%m%d")
        if not list(self.paths.backups_dir.glob(f"daily-v*-{today}T*.sqlite3")):
            self._backup(prefix="daily", keep=30)
        return self._backup(prefix="prewrite", keep=10)

    def manual_backup(self) -> dict[str, Any]:
        path = self._backup(prefix="daily", keep=30)
        return {"backup": str(path), "integrity": self._check_backup(path)}

    @staticmethod
    def _check_backup(path: Path) -> str:
        if not path.exists():
            return "missing"
        uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        except sqlite3.Error as exc:
            return f"error: {exc}"
        finally:
            conn.close()

    def _timezone(self) -> str:
        return self.get_setting("timezone") or DEFAULT_TIMEZONE

    def get_setting(self, key: str) -> str | None:
        assert self.conn is not None
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def set_setting(self, key: str, value: str) -> dict[str, Any]:
        if key == "timezone":
            check_timezone(value)
        if key == "capture_mode" and value not in {"conservative", "proactive"}:
            raise MemoError("capture_mode must be conservative or proactive")
        if key == "single_user_local" and value.lower() not in {"true", "false"}:
            raise MemoError("single_user_local must be true or false")
        if key == "snapshot_ttl_hours":
            try:
                if int(value) < 1 or int(value) > 168:
                    raise ValueError
            except ValueError as exc:
                raise MemoError("snapshot_ttl_hours must be an integer from 1 to 168") from exc
        self.prewrite_backup()
        assert self.conn is not None
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, iso_now()),
            )
        return {"key": key, "value": value}

    def migrate_timezone(self, value: str) -> dict[str, Any]:
        """Set the default and every item display timezone without moving instants.

        Exact timestamps are stored as UTC and date-only values are calendar dates, so
        neither representation is rewritten by this display-timezone migration.
        """
        check_timezone(value)
        previous = self._timezone()
        self.prewrite_backup()
        assert self.conn is not None
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                ("timezone", value, iso_now()),
            )
            updated_items = self.conn.execute(
                "UPDATE items SET timezone=? WHERE timezone<>?", (value, value)
            ).rowcount
        return {
            "from_timezone": previous,
            "timezone": value,
            "updated_items": updated_items,
            "exact_instants_preserved": True,
        }

    def settings(self) -> dict[str, str]:
        assert self.conn is not None
        return {str(row[0]): str(row[1]) for row in self.conn.execute("SELECT key,value FROM settings ORDER BY key")}

    def _stable_id(self) -> str:
        assert self.conn is not None
        local = dt.datetime.now(check_timezone(self._timezone()))
        for _ in range(20):
            candidate = f"M-{local:%Y%m%d}-{uuid.uuid4().hex[:4].upper()}"
            if not self.conn.execute("SELECT 1 FROM items WHERE id=?", (candidate,)).fetchone():
                return candidate
        raise MemoError("Unable to allocate a unique stable ID")

    @staticmethod
    def _event_id() -> str:
        return "E-" + uuid.uuid4().hex.upper()

    @staticmethod
    def _source_id() -> str:
        return "S-" + uuid.uuid4().hex.upper()

    def _allow_profile_global_scope(self) -> bool:
        return (self.get_setting("single_user_local") or "false").lower() == "true"

    def _idempotent_replay(self, key: str | None) -> dict[str, Any] | None:
        if not key:
            return None
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT event_id,stable_item_id,after_data FROM item_events WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        if not row:
            return None
        try:
            result = self._row_item(str(row["stable_item_id"]))
        except MemoError:
            result = parse_json(row["after_data"], {})
        result["event_id"] = str(row["event_id"])
        result["idempotent_replay"] = True
        return result

    def _row_item(self, item_id: str, include_sources: bool = True) -> dict[str, Any]:
        assert self.conn is not None
        row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise MemoError(f"Item not found: {item_id}")
        result = dict(row)
        result["tags"] = parse_json(result.pop("tags_json", "[]"), [])
        result["time_uncertain"] = bool(result["time_uncertain"])
        result["is_overdue"] = False
        if result.get("due_at") and result.get("status") == "active":
            due = parse_datetime(str(result["due_at"]), str(result["timezone"]))
            now = dt.datetime.now(check_timezone(str(result["timezone"])))
            if due.tzinfo:
                now = now.astimezone(due.tzinfo)
            result["is_overdue"] = due < now
        result["time_pending_confirmation"] = bool(result["time_uncertain"])
        if include_sources:
            sources = []
            for source in self.conn.execute(
                """SELECT s.*,x.relationship FROM sources s
                   JOIN item_sources x ON x.source_id=s.source_id
                   WHERE x.item_id=? ORDER BY x.created_at,s.created_at""",
                (item_id,),
            ):
                data = dict(source)
                data["key_points"] = parse_json(data.get("key_points"), [])
                sources.append(data)
            result["sources"] = sources
        return result

    def _record_event(
        self,
        item_id: str,
        operation: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        *,
        instruction: str | None = None,
        platform: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        session_id: str | None = None,
        is_cron: bool = False,
        target_event_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        assert self.conn is not None
        event_id = self._event_id()
        self.conn.execute(
            """INSERT INTO item_events(
                event_id,item_id,stable_item_id,operation,before_data,after_data,
                original_user_instruction,created_at,platform,user_id,chat_id,session_id,is_cron,
                target_event_id,idempotency_key
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id,
                item_id,
                item_id,
                operation,
                json_dumps(before) if before is not None else None,
                json_dumps(after) if after is not None else None,
                instruction,
                iso_now(),
                platform,
                user_id,
                chat_id,
                session_id,
                int(is_cron),
                target_event_id,
                idempotency_key,
            ),
        )
        return event_id

    def add_item(
        self,
        *,
        title: str | None,
        content: str,
        item_type: str = "task",
        urls: Sequence[str] = (),
        due_at: str | None = None,
        due_precision: str | None = None,
        due_raw_text: str | None = None,
        remind_at: str | None = None,
        remind_precision: str | None = None,
        scheduled_for: str | None = None,
        scheduled_precision: str | None = None,
        defer_until: str | None = None,
        defer_precision: str | None = None,
        timezone: str | None = None,
        priority_level: str = "normal",
        priority_source: str = "inferred",
        priority_reason: str | None = None,
        capture_source: str | None = None,
        source_summary: str | None = None,
        tags: Sequence[str] = (),
        time_uncertain: bool = False,
        suggested_action: str | None = None,
        action_source: str | None = None,
        allow_duplicate: bool = False,
        instruction: str | None = None,
        platform: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        session_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if item_type not in ITEM_TYPES:
            raise MemoError(f"Invalid item type: {item_type}")
        if priority_level not in PRIORITY_LEVELS or priority_source not in PRIORITY_SOURCES:
            raise MemoError("Invalid priority")
        if action_source is not None and action_source not in {"user", "inferred"}:
            raise MemoError("action_source must be user or inferred")
        replay = self._idempotent_replay(idempotency_key)
        if replay is not None:
            return replay
        timezone = timezone or self._timezone()
        check_timezone(timezone)
        due_at, due_precision = normalize_temporal(due_at, timezone, due_precision)
        remind_at, remind_precision = normalize_temporal(remind_at, timezone, remind_precision)
        scheduled_for, scheduled_precision = normalize_temporal(scheduled_for, timezone, scheduled_precision)
        defer_until, defer_precision = normalize_temporal(defer_until, timezone, defer_precision)
        canonical_urls = [(url, canonicalize_url(url)) for url in urls]
        assert self.conn is not None
        if len(canonical_urls) == 1 and not allow_duplicate:
            original, canonical = canonical_urls[0]
            duplicate = self.conn.execute(
                """SELECT s.*, i.status FROM sources s
                   JOIN item_sources x ON x.source_id=s.source_id
                   JOIN items i ON i.id=x.item_id
                   WHERE s.original_url=? OR s.canonical_url=?
                   ORDER BY i.updated_at DESC LIMIT 1""",
                (original, canonical),
            ).fetchone()
            if duplicate:
                existing_id = str(duplicate["item_id"])
                existing_before = self._row_item(existing_id)
                self.prewrite_backup()
                with transaction(self.conn):
                    if not self.conn.execute(
                        """SELECT 1 FROM sources s JOIN item_sources x ON x.source_id=s.source_id
                           WHERE x.item_id=? AND s.original_url=?""",
                        (existing_id, original),
                    ).fetchone():
                        source_id = self._source_id()
                        source_now = iso_now()
                        self.conn.execute(
                            """INSERT INTO sources(
                               source_id,item_id,original_url,canonical_url,source_type,platform,
                               ingest_status,understanding_basis,suggested_action,action_source,key_points,
                               created_at,updated_at
                               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                source_id, existing_id, original, canonical, source_type_for_url(original),
                                urllib.parse.urlsplit(original).hostname, "processing", "unavailable",
                                suggested_action, action_source, "[]", source_now, source_now,
                            ),
                        )
                        self.conn.execute(
                            "INSERT INTO item_sources(item_id,source_id,relationship,created_at) VALUES(?,?,?,?)",
                            (existing_id, source_id, "primary", source_now),
                        )
                    self._record_event(
                        existing_id,
                        "duplicate_source_seen",
                        existing_before,
                        self._row_item(existing_id),
                        instruction=instruction,
                        platform=platform,
                        user_id=user_id,
                        chat_id=chat_id,
                        session_id=session_id,
                        idempotency_key=idempotency_key,
                    )
                result = self._row_item(existing_id)
                result["duplicate"] = True
                result["duplicate_action"] = "attached_source_or_recorded_event"
                return result
        now = iso_now()
        item_id = self._stable_id()
        inferred_title = title or source_summary or truncate(content, 60)
        if not inferred_title and canonical_urls:
            inferred_title = urllib.parse.urlsplit(canonical_urls[0][0]).hostname or "Saved link"
        inferred_title = inferred_title or "Untitled memo"
        if canonical_urls and item_type == "task":
            item_type = "video" if any(source_type_for_url(url) == "video" for url, _ in canonical_urls) else "link"
        status = "active"
        self.prewrite_backup()
        with transaction(self.conn):
            self.conn.execute(
                """INSERT INTO items(
                    id,title,content,item_type,status,created_at,updated_at,due_at,due_precision,due_raw_text,
                    remind_at,remind_precision,scheduled_for,scheduled_precision,defer_until,defer_precision,
                    timezone,priority_level,priority_source,priority_reason,
                    capture_source,tags_json,time_uncertain
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item_id, inferred_title, content, item_type, status, now, now, due_at, due_precision,
                    due_raw_text, remind_at, remind_precision, scheduled_for, scheduled_precision,
                    defer_until, defer_precision, timezone, priority_level,
                    priority_source, priority_reason, capture_source,
                    json_dumps(sorted(set(tags))), int(time_uncertain),
                ),
            )
            for original, canonical in canonical_urls:
                kind = source_type_for_url(original)
                basis = "user_context" if content.strip() and content.strip() != original else "unavailable"
                source_id = self._source_id()
                self.conn.execute(
                    """INSERT INTO sources(
                        source_id,item_id,original_url,canonical_url,source_type,platform,ingest_status,
                        understanding_basis,suggested_action,action_source,key_points,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        source_id, item_id, original, canonical, kind,
                        urllib.parse.urlsplit(original).hostname, "processing", basis,
                        suggested_action, action_source, "[]", now, now,
                    ),
                )
                self.conn.execute(
                    "INSERT INTO item_sources(item_id,source_id,relationship,created_at) VALUES(?,?,?,?)",
                    (item_id, source_id, "primary", now),
                )
            after = self._row_item(item_id)
            event_id = self._record_event(
                item_id,
                "create",
                None,
                after,
                instruction=instruction,
                platform=platform,
                user_id=user_id,
                chat_id=chat_id,
                session_id=session_id,
                idempotency_key=idempotency_key,
            )
        result = self._row_item(item_id)
        result["event_id"] = event_id
        result["duplicate"] = False
        return result

    def capture(
        self,
        text: str,
        *,
        chat_type: str = "unknown",
        explicit: bool = False,
        redact: bool = False,
        **context: Any,
    ) -> dict[str, Any]:
        if contains_sensitive(text):
            if not redact:
                return {
                    "saved": False,
                    "status": "confirmation_required",
                    "reason": "possible_secret",
                    "message": "Potential secret detected; confirm a redacted save instead.",
                }
            text = redact_sensitive(text)
        urls = extract_urls(text)
        remaining = URL_RE.sub("", text).strip(" \t\r\n,，。")
        pure_urls = bool(urls) and not remaining
        explicit = explicit or bool(re.search(r"(?:备忘|记一下|记住|保存|存一下|待办|提醒我)", text, re.I))
        future_intent = bool(re.search(r"(?:以后|有空|回头|明天|下周|月底|需要|要做|研究|看看|处理)", text, re.I))
        mode = self.get_setting("capture_mode") or "conservative"
        if pure_urls and chat_type != "private" and not explicit:
            return {"saved": False, "status": "not_captured", "reason": "group_url_requires_confirmation"}
        should_save = explicit or (pure_urls and chat_type == "private") or future_intent
        if mode == "proactive" and (urls or future_intent):
            should_save = True
        if not should_save:
            return {"saved": False, "status": "confirmation_required", "reason": "ambiguous"}
        if pure_urls and len(urls) > 1:
            base_key = context.pop("idempotency_key", None)
            items = [
                self.add_item(
                    title=None,
                    content=url,
                    urls=[url],
                    capture_source=chat_type,
                    instruction=text,
                    idempotency_key=f"{base_key}:{index}" if base_key else None,
                    **context,
                )
                for index, url in enumerate(urls, start=1)
            ]
            return {"saved": True, "status": "saved", "items": items}
        item = self.add_item(
            title=None,
            content=text,
            urls=urls,
            capture_source=chat_type,
            instruction=text,
            **context,
        )
        return {"saved": True, "status": "saved", "item": item}

    def _fetch_metadata(self, url: str) -> dict[str, Any]:
        _assert_public_url(url)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "personal-memo/1.0 (+local metadata fetch)",
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
            },
        )
        opener = urllib.request.build_opener(SafeRedirectHandler())
        try:
            with opener.open(request, timeout=12) as response:
                content_type = response.headers.get_content_type()
                final_url = response.geturl()
                _assert_public_url(final_url)
                data = response.read(MAX_FETCH_BYTES + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MemoError(f"Fetch failed: {exc}") from exc
        was_truncated = len(data) > MAX_FETCH_BYTES
        if was_truncated:
            data = data[:MAX_FETCH_BYTES]
        if content_type not in {"text/html", "application/xhtml+xml"}:
            return {
                "source_title": None,
                "summary": None,
                "canonical_url": canonicalize_url(final_url),
                "content_hash": hashlib.sha256(data).hexdigest(),
                "access_note": f"Metadata only; content type {content_type}",
                "ingest_status": "partial",
                "understanding_basis": "metadata_only",
            }
        charset_match = re.search(br"charset=[\"']?([A-Za-z0-9._-]+)", data[:4096], re.I)
        charset = charset_match.group(1).decode("ascii", "ignore") if charset_match else "utf-8"
        text = data.decode(charset, "replace")
        parser = MetadataParser()
        parser.feed(text)
        kind = source_type_for_url(final_url)
        title = parser.title
        description = parser.description
        if kind == "video":
            if title and description:
                basis, status = "title_and_description", "complete"
                summary = f"根据视频标题和简介，该视频大概与“{truncate(title, 100)}”有关。{truncate(description, 180)}"
            elif title:
                basis, status = "title_only", "partial"
                summary = f"仅根据视频标题判断，该视频大概与“{truncate(title, 120)}”有关；信息可能不完整。"
            else:
                basis, status, summary = "metadata_only", "partial", None
        else:
            basis = "title_and_description" if title and description else "title_only" if title else "metadata_only"
            status = "complete" if title and description else "partial"
            summary = truncate(description or title, 320) or None
        if was_truncated:
            status = "partial"
        canonical = canonicalize_url(urllib.parse.urljoin(final_url, parser.canonical)) if parser.canonical else canonicalize_url(final_url)
        return {
            "source_title": title,
            "author_or_channel": parser.meta.get("author") or parser.meta.get("og:site_name"),
            "published_at": parser.meta.get("article:published_time"),
            "duration_text": parser.meta.get("video:duration"),
            "thumbnail_url": parser.meta.get("og:image") or parser.meta.get("twitter:image"),
            "summary": summary,
            "canonical_url": canonical,
            "content_hash": hashlib.sha256(data).hexdigest(),
            "access_note": (
                f"Fetched {len(data)} bytes of page metadata"
                + (" (response truncated at safety limit)" if was_truncated else "")
                + "; untrusted page instructions were not executed."
            ),
            "ingest_status": status,
            "understanding_basis": basis,
        }

    def update_source(
        self,
        source_id: str,
        updates: dict[str, Any],
        *,
        instruction: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        replay = self._idempotent_replay(idempotency_key)
        if replay is not None:
            return replay
        assert self.conn is not None
        source = self.conn.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
        if not source:
            raise MemoError(f"Source not found: {source_id}")
        allowed = {
            "canonical_url", "source_type", "source_title", "author_or_channel", "platform", "published_at",
            "duration_text", "thumbnail_url", "fetched_at", "last_attempt_at", "ingest_status",
            "understanding_basis", "summary", "key_points", "suggested_action", "action_source",
            "confidence", "content_hash", "access_note", "failure_reason",
        }
        invalid = set(updates) - allowed
        if invalid:
            raise MemoError(f"Unsupported source fields: {', '.join(sorted(invalid))}")
        if updates.get("ingest_status") and updates["ingest_status"] not in INGEST_STATUSES:
            raise MemoError("Invalid ingest_status")
        if updates.get("action_source") and updates["action_source"] not in {"user", "inferred"}:
            raise MemoError("Invalid action_source")
        if updates.get("understanding_basis") and source["source_type"] == "video":
            if updates["understanding_basis"] not in VIDEO_BASES:
                raise MemoError("Invalid video understanding_basis")
        if "key_points" in updates:
            points = parse_json(updates["key_points"], None) if isinstance(updates["key_points"], str) else updates["key_points"]
            if not isinstance(points, list):
                raise MemoError("key_points must be a JSON list")
            maximum = 3 if source["source_type"] == "video" else 5
            if len(points) > maximum:
                raise MemoError(f"key_points may contain at most {maximum} entries")
            updates["key_points"] = json_dumps([truncate(str(point), 300) for point in points])
        item_id = str(source["item_id"])
        before = self._row_item(item_id)
        updates["updated_at"] = iso_now()
        self.prewrite_backup()
        with transaction(self.conn):
            if updates:
                assignments = ",".join(f"{field}=?" for field in updates)
                self.conn.execute(
                    f"UPDATE sources SET {assignments} WHERE source_id=?",
                    (*updates.values(), source_id),
                )
            summaries = [
                str(row[0]) for row in self.conn.execute(
                    """SELECT s.summary FROM sources s JOIN item_sources x ON x.source_id=s.source_id
                       WHERE x.item_id=? AND s.summary IS NOT NULL ORDER BY x.created_at,s.created_at""",
                    (item_id,),
                )
            ]
            if summaries:
                self.conn.execute(
                    "UPDATE items SET title=?,updated_at=? WHERE id=?",
                    (truncate(" ".join(summaries), 320), iso_now(), item_id),
                )
            after = self._row_item(item_id)
            self._record_event(
                item_id,
                "link_parse",
                before,
                after,
                instruction=instruction,
                idempotency_key=idempotency_key,
            )
        return self._row_item(item_id)

    def retry_source(self, reference: str) -> dict[str, Any]:
        assert self.conn is not None
        if reference.startswith("S-"):
            rows = self.conn.execute("SELECT * FROM sources WHERE source_id=?", (reference,)).fetchall()
        else:
            item_id = self.resolve_reference(reference)
            rows = self.conn.execute(
                """SELECT s.* FROM sources s JOIN item_sources x ON x.source_id=s.source_id
                   WHERE x.item_id=? ORDER BY x.created_at""",
                (item_id,),
            ).fetchall()
        if not rows:
            raise MemoError(f"No source found for {reference}")
        results = []
        for row in rows:
            source_id = str(row["source_id"])
            attempted = iso_now()
            try:
                updates = self._fetch_metadata(str(row["original_url"]))
                updates["fetched_at"] = attempted
                updates["last_attempt_at"] = attempted
                updates["failure_reason"] = None
            except MemoError as exc:
                updates = {
                    "fetched_at": attempted,
                    "last_attempt_at": attempted,
                    "ingest_status": "failed",
                    "understanding_basis": "unavailable",
                    "access_note": truncate(str(exc), 500),
                    "failure_reason": truncate(str(exc), 500),
                }
            results.append(self.update_source(source_id, updates, instruction="retry-source"))
        return {"processed": len(results), "items": results}

    def refresh_item(self, reference: str, **scope: Any) -> dict[str, Any]:
        """Refresh derived metadata for one existing item without changing its original content."""
        item_id = self.resolve_reference(reference, **scope)
        item = self._row_item(item_id)
        sources = item.get("sources") or []
        if sources:
            result = self.retry_source(item_id)
        else:
            # Plain-text items have no deterministic web parser; preserve the
            # agent-generated title/summary and original content.
            result = self._row_item(item_id)
        result["refreshed"] = True
        result["refresh_note"] = "链接元数据已重新解析；纯文本摘要保留原有 agent 推断"
        return result

    def refresh_all(self, **scope: Any) -> dict[str, Any]:
        """Refresh every item in the database, preserving content and business state."""
        items = self.list_items(tuple(ITEM_STATUSES), create_snapshot=False, limit=None, **scope).get("items", [])
        refreshed = []
        for item in items:
            refreshed.append(self.refresh_item(str(item["id"]), **scope))
        return {"count": len(refreshed), "items": refreshed}

    def link_source(
        self,
        source_id: str,
        reference: str,
        *,
        instruction: str | None = None,
        idempotency_key: str | None = None,
        **scope: Any,
    ) -> dict[str, Any]:
        replay = self._idempotent_replay(idempotency_key)
        if replay is not None:
            return replay
        assert self.conn is not None
        if not self.conn.execute("SELECT 1 FROM sources WHERE source_id=?", (source_id,)).fetchone():
            raise MemoError(f"Source not found: {source_id}")
        item_id = self.resolve_reference(
            reference,
            platform=scope.get("platform"),
            user_id=scope.get("user_id"),
            chat_id=scope.get("chat_id"),
            topic_id=scope.get("topic_id"),
        )
        before = self._row_item(item_id)
        if self.conn.execute(
            "SELECT 1 FROM item_sources WHERE item_id=? AND source_id=?", (item_id, source_id)
        ).fetchone():
            before["already_linked"] = True
            return before
        self.prewrite_backup()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO item_sources(item_id,source_id,relationship,created_at) VALUES(?,?,?,?)",
                (item_id, source_id, "related", iso_now()),
            )
            after = self._row_item(item_id)
            self._record_event(
                item_id,
                "link_source",
                before,
                after,
                instruction=instruction,
                platform=scope.get("platform"),
                user_id=scope.get("user_id"),
                chat_id=scope.get("chat_id"),
                session_id=scope.get("session_id"),
                idempotency_key=idempotency_key,
            )
        return self._row_item(item_id)

    def _item_sort_key(self, item: dict[str, Any], now: dt.datetime | None = None) -> tuple[Any, ...]:
        timezone = str(item.get("timezone") or self._timezone())
        now = now or dt.datetime.now(check_timezone(timezone))
        priority = {"urgent": 0, "high": 1, "normal": 2, "low": 3}.get(str(item["priority_level"]), 2)
        if item.get("due_at"):
            due = parse_datetime(str(item["due_at"]), timezone)
            overdue = due < now
            return (0, 0 if overdue else 1, due.timestamp(), priority, item["created_at"], item["id"])
        scheduled = parse_datetime(str(item["scheduled_for"]), timezone).timestamp() if item.get("scheduled_for") else float("inf")
        return (1, priority, scheduled, item["created_at"], item["id"])

    def list_items(
        self,
        statuses: Sequence[str] = ("active",),
        *,
        create_snapshot: bool = True,
        platform: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        topic_id: str | None = None,
        view_kind: str = "active",
        limit: int | None = None,
    ) -> dict[str, Any]:
        invalid = set(statuses) - ITEM_STATUSES
        if invalid:
            raise MemoError(f"Invalid statuses: {', '.join(sorted(invalid))}")
        assert self.conn is not None
        placeholders = ",".join("?" for _ in statuses)
        rows = self.conn.execute(f"SELECT id FROM items WHERE status IN ({placeholders})", tuple(statuses)).fetchall()
        items = [self._row_item(str(row[0])) for row in rows]
        items.sort(key=self._item_sort_key)
        if limit is not None:
            items = items[: max(0, limit)]
        snapshot_id = None
        if create_snapshot and items:
            snapshot_id = self.create_snapshot(
                items,
                platform=platform,
                user_id=user_id,
                chat_id=chat_id,
                topic_id=topic_id,
                view_kind=view_kind,
            )
        return {
            "count": len(items),
            "items": items,
            "snapshot_id": snapshot_id,
            "view_kind": view_kind,
            "display_markdown": render_markdown_table(items),
        }

    def create_snapshot(
        self,
        items: Sequence[dict[str, Any]],
        *,
        platform: str | None,
        user_id: str | None,
        chat_id: str | None,
        topic_id: str | None,
        view_kind: str,
    ) -> str:
        assert self.conn is not None
        created = utc_now()
        ttl = int(self.get_setting("snapshot_ttl_hours") or SNAPSHOT_TTL_HOURS)
        expires = created + dt.timedelta(hours=ttl)
        snapshot_id = "V-" + uuid.uuid4().hex.upper()
        scope = scope_key(
            platform,
            user_id,
            chat_id,
            topic_id,
            allow_profile_global=self._allow_profile_global_scope(),
        )
        with transaction(self.conn):
            for number, item in enumerate(items, start=1):
                self.conn.execute(
                    """INSERT INTO view_snapshots(
                        snapshot_id,scope_key,view_kind,item_number,item_id,created_at,expires_at
                    ) VALUES(?,?,?,?,?,?,?)""",
                    (snapshot_id, scope, view_kind, number, item["id"], created.isoformat(), expires.isoformat()),
                )
            self.conn.execute("DELETE FROM view_snapshots WHERE expires_at < ?", (created.isoformat(),))
        return snapshot_id

    def resolve_reference(
        self,
        reference: str,
        *,
        platform: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        topic_id: str | None = None,
    ) -> str:
        assert self.conn is not None
        ref = reference.strip()
        if re.fullmatch(r"M-\d{8}-[A-F0-9]{4,}", ref, re.I):
            row = self.conn.execute("SELECT id FROM items WHERE id=?", (ref.upper(),)).fetchone()
            if not row:
                raise MemoError(f"Item not found: {ref}")
            return str(row[0])
        match = re.fullmatch(r"#?(\d+)", ref)
        if not match:
            candidates = self.search(ref, limit=5)["items"]
            if len(candidates) == 1:
                return str(candidates[0]["id"])
            if not candidates:
                raise MemoError(f"No item matches: {reference}")
            raise MemoError("Reference is ambiguous: " + ", ".join(f"{item['id']} {item['title']}" for item in candidates))
        number = int(match.group(1))
        if not (platform and user_id and chat_id) and not self._allow_profile_global_scope():
            raise MemoError("Numbered actions require platform, user, and chat scope; use the stable ID instead")
        scope = scope_key(
            platform,
            user_id,
            chat_id,
            topic_id,
            allow_profile_global=self._allow_profile_global_scope(),
        )
        now = iso_now()
        row = self.conn.execute(
            """SELECT item_id, snapshot_id, created_at, expires_at FROM view_snapshots
               WHERE scope_key=? AND item_number=?
                 AND snapshot_id=(SELECT snapshot_id FROM view_snapshots WHERE scope_key=? ORDER BY created_at DESC LIMIT 1)
               ORDER BY created_at DESC LIMIT 1""",
            (scope, number, scope),
        ).fetchone()
        if not row:
            raise MemoError("No recent numbered view exists for this chat scope")
        if str(row["expires_at"]) < now:
            raise MemoError("The numbered view is older than 24 hours; display the list again before using a number")
        return str(row["item_id"])

    def show(self, reference: str, **scope: Any) -> dict[str, Any]:
        return self._row_item(self.resolve_reference(reference, **scope))

    def search(self, query: str, *, limit: int = 20, statuses: Sequence[str] | None = None) -> dict[str, Any]:
        assert self.conn is not None
        pattern = f"%{query}%"
        params: list[Any] = [pattern] * 7
        status_clause = ""
        if statuses:
            invalid = set(statuses) - ITEM_STATUSES
            if invalid:
                raise MemoError("Invalid status filter")
            status_clause = " AND i.status IN (" + ",".join("?" for _ in statuses) + ")"
            params.extend(statuses)
        params.append(limit)
        rows = self.conn.execute(
            """SELECT DISTINCT i.id FROM items i
               LEFT JOIN item_sources x ON x.item_id=i.id
               LEFT JOIN sources s ON s.source_id=x.source_id
               WHERE (i.title LIKE ? OR i.content LIKE ? OR i.tags_json LIKE ?
                  OR s.original_url LIKE ? OR s.canonical_url LIKE ? OR s.source_title LIKE ? OR s.summary LIKE ?)"""
            + status_clause
            + " ORDER BY i.updated_at DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return {"query": query, "count": len(rows), "items": [self._row_item(str(row[0])) for row in rows]}

    def update_item(
        self,
        reference: str,
        updates: dict[str, Any],
        *,
        clear_fields: Sequence[str] = (),
        append_note: str | None = None,
        instruction: str | None = None,
        platform: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        topic_id: str | None = None,
        session_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        replay = self._idempotent_replay(idempotency_key)
        if replay is not None:
            return replay
        item_id = self.resolve_reference(
            reference, platform=platform, user_id=user_id, chat_id=chat_id, topic_id=topic_id
        )
        allowed = {
            "title", "content", "item_type", "due_at", "due_precision", "due_raw_text", "remind_at",
            "remind_precision", "scheduled_for", "scheduled_precision", "defer_until", "defer_precision",
            "timezone", "priority_level", "priority_source",
            "priority_reason", "capture_source", "tags", "time_uncertain",
        }
        invalid = (set(updates) | set(clear_fields)) - allowed
        if invalid:
            raise MemoError(f"Unsupported item fields: {', '.join(sorted(invalid))}")
        before = self._row_item(item_id)
        timezone = str(updates.get("timezone") or before["timezone"])
        check_timezone(timezone)
        if "item_type" in updates and updates["item_type"] not in ITEM_TYPES:
            raise MemoError("Invalid item_type")
        if "priority_level" in updates and updates["priority_level"] not in PRIORITY_LEVELS:
            raise MemoError("Invalid priority_level")
        if "priority_source" in updates and updates["priority_source"] not in PRIORITY_SOURCES:
            raise MemoError("Invalid priority_source")
        if before["priority_source"] == "user" and updates.get("priority_source") == "inferred":
            raise MemoError("An inferred priority cannot overwrite a user-set priority")
        if "due_at" in updates:
            updates["due_at"], inferred_precision = normalize_temporal(
                updates.get("due_at"), timezone, updates.get("due_precision")
            )
            updates["due_precision"] = inferred_precision
        for field in ("remind_at", "scheduled_for", "defer_until"):
            if field in updates:
                precision_field = {
                    "remind_at": "remind_precision",
                    "scheduled_for": "scheduled_precision",
                    "defer_until": "defer_precision",
                }[field]
                updates[field], updates[precision_field] = normalize_temporal(
                    updates.get(field), timezone, updates.get(precision_field)
                )
        if "tags" in updates:
            updates["tags_json"] = json_dumps(sorted(set(updates.pop("tags") or [])))
        if "time_uncertain" in updates:
            updates["time_uncertain"] = int(bool(updates["time_uncertain"]))
        for field in clear_fields:
            column = "tags_json" if field == "tags" else field
            updates[column] = "[]" if column == "tags_json" else 0 if column == "time_uncertain" else None
            precision_field = {
                "due_at": "due_precision",
                "remind_at": "remind_precision",
                "scheduled_for": "scheduled_precision",
                "defer_until": "defer_precision",
            }.get(column)
            if precision_field:
                updates[precision_field] = None
        if append_note:
            existing = str(updates.get("content", before["content"]))
            updates["content"] = existing.rstrip() + ("\n\n" if existing.strip() else "") + append_note.strip()
        updates["updated_at"] = iso_now()
        self.prewrite_backup()
        assert self.conn is not None
        with transaction(self.conn):
            if updates:
                assignments = ",".join(f"{key}=?" for key in updates)
                self.conn.execute(
                    f"UPDATE items SET {assignments} WHERE id=?",
                    (*updates.values(), item_id),
                )
            after = self._row_item(item_id)
            self._record_event(
                item_id,
                "update",
                before,
                after,
                instruction=instruction,
                platform=platform,
                user_id=user_id,
                chat_id=chat_id,
                session_id=session_id,
                idempotency_key=idempotency_key,
            )
        return self._row_item(item_id)

    def _transition(
        self,
        reference: str,
        status: str,
        operation: str,
        *,
        instruction: str | None = None,
        platform: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        topic_id: str | None = None,
        session_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if status not in ITEM_STATUSES:
            raise MemoError("Invalid status")
        replay = self._idempotent_replay(idempotency_key)
        if replay is not None:
            return replay
        item_id = self.resolve_reference(
            reference, platform=platform, user_id=user_id, chat_id=chat_id, topic_id=topic_id
        )
        before = self._row_item(item_id)
        now = iso_now()
        timestamps = {
            "completed_at": now if status == "completed" else None,
            "deleted_at": now if status == "deleted" else None,
            "archived_at": now if status == "archived" else None,
        }
        self.prewrite_backup()
        assert self.conn is not None
        with transaction(self.conn):
            self.conn.execute(
                """UPDATE items SET status=?, completed_at=?, deleted_at=?, archived_at=?, updated_at=?
                   WHERE id=?""",
                (status, timestamps["completed_at"], timestamps["deleted_at"], timestamps["archived_at"], now, item_id),
            )
            after = self._row_item(item_id)
            self._record_event(
                item_id,
                operation,
                before,
                after,
                instruction=instruction,
                platform=platform,
                user_id=user_id,
                chat_id=chat_id,
                session_id=session_id,
                idempotency_key=idempotency_key,
            )
        return self._row_item(item_id)

    def complete(self, reference: str, **context: Any) -> dict[str, Any]:
        return self._transition(reference, "completed", "complete", **context)

    def delete(self, reference: str, **context: Any) -> dict[str, Any]:
        return self._transition(reference, "deleted", "delete", **context)

    def archive(self, reference: str, **context: Any) -> dict[str, Any]:
        return self._transition(reference, "archived", "archive", **context)

    def restore(self, reference: str, **context: Any) -> dict[str, Any]:
        item = self.show(reference, **{key: context.get(key) for key in ("platform", "user_id", "chat_id", "topic_id")})
        if item["status"] not in {"completed", "deleted", "archived"}:
            raise MemoError("Only completed, deleted, or archived items can be restored")
        operation = "restore_" + str(item["status"])
        return self._transition(reference, "active", operation, **context)

    def purge(
        self,
        reference: str,
        *,
        confirm: str,
        instruction: str | None = None,
        platform: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        topic_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        item_id = self.resolve_reference(
            reference, platform=platform, user_id=user_id, chat_id=chat_id, topic_id=topic_id
        )
        expected = f"PERMANENTLY-DELETE:{item_id}"
        if confirm != expected:
            raise MemoError(f"Physical deletion requires the exact confirmation token: {expected}")
        before = self._row_item(item_id)
        self.prewrite_backup()
        assert self.conn is not None
        with transaction(self.conn):
            event_id = self._record_event(
                item_id,
                "purge",
                before,
                None,
                instruction=instruction,
                platform=platform,
                user_id=user_id,
                chat_id=chat_id,
                session_id=session_id,
            )
            for source in self.conn.execute("SELECT source_id FROM sources WHERE item_id=?", (item_id,)).fetchall():
                replacement = self.conn.execute(
                    "SELECT item_id FROM item_sources WHERE source_id=? AND item_id<>? ORDER BY created_at LIMIT 1",
                    (source["source_id"], item_id),
                ).fetchone()
                if replacement:
                    self.conn.execute(
                        "UPDATE sources SET item_id=?,updated_at=? WHERE source_id=?",
                        (replacement["item_id"], iso_now(), source["source_id"]),
                    )
            self.conn.execute("DELETE FROM items WHERE id=?", (item_id,))
        return {"purged": True, "id": item_id, "event_id": event_id, "audit_history_retained": True}

    def today(
        self,
        *,
        platform: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        topic_id: str | None = None,
    ) -> dict[str, Any]:
        timezone = self._timezone()
        today = dt.datetime.now(check_timezone(timezone)).date()
        active = self.list_items(create_snapshot=False)["items"]
        selected: list[dict[str, Any]] = []
        reasons: dict[str, list[str]] = {}
        no_due: list[dict[str, Any]] = []
        for item in active:
            item_reasons: list[str] = []
            if item.get("is_overdue"):
                item_reasons.append("逾期未完成")
            if item.get("due_at"):
                due_date = parse_datetime(str(item["due_at"]), str(item["timezone"])).astimezone(
                    check_timezone(timezone)
                ).date()
                if due_date == today:
                    item_reasons.append("今天截止")
            if item.get("scheduled_for"):
                scheduled_date = parse_datetime(str(item["scheduled_for"]), str(item["timezone"])).astimezone(
                    check_timezone(timezone)
                ).date()
                if scheduled_date == today:
                    item_reasons.append("今天计划处理")
            if item_reasons:
                selected.append(item)
                reasons[item["id"]] = item_reasons
            elif not item.get("due_at"):
                no_due.append(item)
        for suggestion in self.suggestions(no_due, hours=12, limit=3):
            item = next(candidate for candidate in no_due if candidate["id"] == suggestion["id"])
            if item["id"] not in reasons:
                selected.append(item)
                reasons[item["id"]] = ["建议今天推进，并非今天到期", suggestion["reason"]]
        selected.sort(key=self._item_sort_key)
        snapshot_id = self.create_snapshot(
            selected,
            platform=platform,
            user_id=user_id,
            chat_id=chat_id,
            topic_id=topic_id,
            view_kind="today",
        ) if selected else None
        return {
            "date": today.isoformat(),
            "timezone": timezone,
            "count": len(selected),
            "items": selected,
            "reasons": reasons,
            "snapshot_id": snapshot_id,
        }

    def activity(
        self,
        kind: str,
        *,
        since: str | None = None,
        until: str | None = None,
        days: int | None = None,
        platform: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        topic_id: str | None = None,
    ) -> dict[str, Any]:
        field_by_kind = {"completed": "completed_at", "deleted": "deleted_at", "archived": "archived_at"}
        if kind not in field_by_kind:
            raise MemoError("Activity kind must be completed, deleted, or archived")
        timezone = check_timezone(self._timezone())
        now = dt.datetime.now(timezone)
        if days is not None and since is None:
            since_dt = (now - dt.timedelta(days=max(0, days))).replace(hour=0, minute=0, second=0, microsecond=0)
        elif since:
            since_dt = parse_datetime(since, self._timezone())
        else:
            since_dt = None
        until_dt = parse_datetime(until, self._timezone()) if until else None
        field = field_by_kind[kind]
        clauses = [f"{field} IS NOT NULL"]
        params: list[Any] = []
        if since_dt:
            clauses.append(f"{field}>=?")
            params.append(since_dt.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat())
        if until_dt:
            clauses.append(f"{field}<=?")
            params.append(until_dt.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat())
        assert self.conn is not None
        rows = self.conn.execute(
            f"SELECT id FROM items WHERE {' AND '.join(clauses)} ORDER BY {field} DESC, id",
            tuple(params),
        ).fetchall()
        items = [self._row_item(str(row[0])) for row in rows]
        snapshot_id = self.create_snapshot(
            items,
            platform=platform,
            user_id=user_id,
            chat_id=chat_id,
            topic_id=topic_id,
            view_kind=f"activity-{kind}",
        ) if items else None
        grouped: dict[str, list[str]] = {}
        for item in items:
            timestamp = parse_datetime(str(item[field]), str(item["timezone"]))
            day = timestamp.astimezone(timezone).date().isoformat()
            grouped.setdefault(day, []).append(item["id"])
        return {"kind": kind, "count": len(items), "items": items, "grouped_ids": grouped, "snapshot_id": snapshot_id}

    def history(
        self,
        reference: str | None = None,
        *,
        limit: int = 100,
        since: str | None = None,
        operation: str | None = None,
        platform: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        topic_id: str | None = None,
    ) -> dict[str, Any]:
        assert self.conn is not None
        clauses: list[str] = []
        params: list[Any] = []
        if reference:
            clauses.append("stable_item_id=?")
            params.append(
                self.resolve_reference(
                    reference, platform=platform, user_id=user_id, chat_id=chat_id, topic_id=topic_id
                )
            )
        if since:
            clauses.append("created_at>=?")
            params.append(parse_datetime(since, self._timezone()).isoformat())
        if operation:
            clauses.append("operation=?")
            params.append(operation)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        rows = self.conn.execute(
            "SELECT * FROM item_events" + where + " ORDER BY created_at DESC, rowid DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            event = dict(row)
            event["before_data"] = parse_json(event["before_data"], None)
            event["after_data"] = parse_json(event["after_data"], None)
            event["is_cron"] = bool(event["is_cron"])
            events.append(event)
        return {"count": len(events), "events": events}

    def _apply_snapshot_to_item(self, item_id: str, snapshot: dict[str, Any]) -> None:
        assert self.conn is not None
        columns = [
            "title", "content", "item_type", "status", "created_at", "updated_at", "due_at", "due_precision",
            "due_raw_text", "remind_at", "remind_precision", "scheduled_for", "scheduled_precision",
            "defer_until", "defer_precision", "timezone", "priority_level",
            "priority_source", "priority_reason", "completed_at", "deleted_at", "archived_at",
            "capture_source", "time_uncertain",
        ]
        values = [snapshot.get(column) for column in columns]
        columns.append("tags_json")
        values.append(json_dumps(snapshot.get("tags", [])))
        assignments = ",".join(f"{column}=?" for column in columns)
        self.conn.execute(f"UPDATE items SET {assignments} WHERE id=?", (*values, item_id))

    def undo(
        self,
        reference: str | None = None,
        *,
        instruction: str | None = None,
        platform: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        topic_id: str | None = None,
        session_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        replay = self._idempotent_replay(idempotency_key)
        if replay is not None:
            return replay
        assert self.conn is not None
        clauses = [
            "operation IN ('create','update','complete','delete','archive','restore_completed','restore_deleted','restore_archived')",
            "event_id NOT IN (SELECT target_event_id FROM item_events WHERE operation='undo' AND target_event_id IS NOT NULL)",
        ]
        params: list[Any] = []
        if reference:
            item_id = self.resolve_reference(
                reference, platform=platform, user_id=user_id, chat_id=chat_id, topic_id=topic_id
            )
            clauses.append("stable_item_id=?")
            params.append(item_id)
        row = self.conn.execute(
            "SELECT * FROM item_events WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            tuple(params),
        ).fetchone()
        if not row:
            raise MemoError("No reversible memo operation found")
        item_id = str(row["stable_item_id"])
        before_undo = self._row_item(item_id)
        prior = parse_json(row["before_data"], None)
        self.prewrite_backup()
        with transaction(self.conn):
            if prior is None:
                self.conn.execute(
                    "UPDATE items SET status='deleted', deleted_at=?, completed_at=NULL, archived_at=NULL, updated_at=? WHERE id=?",
                    (iso_now(), iso_now(), item_id),
                )
            else:
                self._apply_snapshot_to_item(item_id, prior)
                self.conn.execute("UPDATE items SET updated_at=? WHERE id=?", (iso_now(), item_id))
            after_undo = self._row_item(item_id)
            event_id = self._record_event(
                item_id,
                "undo",
                before_undo,
                after_undo,
                instruction=instruction,
                platform=platform,
                user_id=user_id,
                chat_id=chat_id,
                session_id=session_id,
                target_event_id=str(row["event_id"]),
                idempotency_key=idempotency_key,
            )
        result = self._row_item(item_id)
        result["undone_event_id"] = str(row["event_id"])
        result["event_id"] = event_id
        return result

    def suggestions(self, items: Sequence[dict[str, Any]], *, hours: int = 12, limit: int = 3) -> list[dict[str, Any]]:
        now = dt.datetime.now(check_timezone(self._timezone()))
        horizon = now + dt.timedelta(hours=hours)
        candidates: list[tuple[int, dict[str, Any], str]] = []
        priority_score = {"urgent": 35, "high": 25, "normal": 10, "low": 0}
        for item in items:
            if item.get("defer_until"):
                deferred = parse_datetime(str(item["defer_until"]), str(item["timezone"]))
                deferred_local = deferred.astimezone(now.tzinfo) if deferred.tzinfo else deferred
                if deferred_local > now:
                    continue
            score = priority_score.get(str(item["priority_level"]), 10)
            reasons: list[str] = []
            if item.get("due_at"):
                due = parse_datetime(str(item["due_at"]), str(item["timezone"]))
                due_local = due.astimezone(now.tzinfo) if due.tzinfo else due
                if due_local < now:
                    score += 100
                    reasons.append("已逾期")
                elif due_local <= horizon:
                    score += 80
                    reasons.append("未来 12 小时内截止")
            if item.get("scheduled_for"):
                scheduled = parse_datetime(str(item["scheduled_for"]), str(item["timezone"]))
                scheduled_local = scheduled.astimezone(now.tzinfo) if scheduled.tzinfo else scheduled
                if now <= scheduled_local <= horizon:
                    score += 60
                    reasons.append("已计划在未来 12 小时处理")
            if item["priority_level"] in {"urgent", "high"}:
                reasons.append("优先级较高")
            if not reasons and item.get("suggested_action"):
                reasons.append("有明确后续动作")
            if score >= 25 or reasons:
                candidates.append((score, item, "，".join(reasons) or "值得推进"))
        candidates.sort(key=lambda value: (-value[0], self._item_sort_key(value[1])))
        return [
            {"id": item["id"], "title": item["title"], "reason": reason, "score": score}
            for score, item, reason in candidates[:limit]
        ]

    def reminder(
        self,
        mode: str,
        *,
        platform: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        topic_id: str | None = None,
        delivery_target: str | None = None,
        delivery_status: str | None = None,
        is_test: bool = False,
    ) -> dict[str, Any]:
        if mode not in {"morning", "evening", "manual"}:
            raise MemoError("Reminder mode must be morning, evening, or manual")
        started = iso_now()
        view = self.list_items(
            create_snapshot=True,
            platform=platform,
            user_id=user_id,
            chat_id=chat_id,
            topic_id=topic_id,
            view_kind=f"reminder-{mode}",
        )
        suggested = self.suggestions(view["items"], hours=12, limit=3)
        local_today = dt.datetime.now(check_timezone(self._timezone())).date()
        already_delivered_today = False
        assert self.conn is not None
        for row in self.conn.execute(
            "SELECT started_at,delivery_target FROM reminder_runs "
            "WHERE mode IN ('morning','evening') AND delivery_status='success'"
        ):
            if delivery_target and row["delivery_target"] != delivery_target:
                continue
            try:
                if parse_datetime(str(row["started_at"]), self._timezone()).astimezone(
                    check_timezone(self._timezone())
                ).date() == local_today:
                    already_delivered_today = True
                    break
            except MemoError:
                continue
        date_reminders = []
        if not already_delivered_today:
            date_reminders = [
                item
                for item in view["items"]
                if item.get("remind_precision") == "date" and item.get("remind_at") == local_today.isoformat()
            ]
        run_id = "R-" + uuid.uuid4().hex.upper()
        finished = iso_now()
        effective_status = delivery_status or "generated"
        with transaction(self.conn):
            self.conn.execute(
                """INSERT INTO reminder_runs(
                    run_id,mode,started_at,completed_at,item_count,delivery_target,delivery_status,
                    snapshot_id,is_test,attempted_at,success_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, mode, started, finished, view["count"], delivery_target,
                    effective_status, view["snapshot_id"], int(is_test), started,
                    finished if effective_status == "success" else None,
                ),
            )
        return {
            "run_id": run_id,
            "mode": mode,
            "items": view["items"],
            "count": view["count"],
            "snapshot_id": view["snapshot_id"],
            "suggestions": suggested,
            "date_reminders": date_reminders,
            "business_state_changed": False,
            "delivery_target": delivery_target,
            "delivery_status": delivery_status or "generated",
        }

    @staticmethod
    def _reminder_dedupe_key(item_id: str, remind_at: str, delivery_target: str) -> str:
        payload = f"{item_id}\n{remind_at}\n{delivery_target}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def dispatch_reminders(
        self,
        *,
        delivery_target: str | None = None,
        run_id: str | None = None,
        delivery_status: str | None = None,
        error_message: str | None = None,
        now: str | None = None,
        is_test: bool = False,
        platform: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        topic_id: str | None = None,
    ) -> dict[str, Any]:
        """Prepare due exact reminders or record one observed delivery result."""
        assert self.conn is not None
        if run_id:
            if delivery_status not in {"success", "failed"}:
                raise MemoError("Recording a dispatch result requires --delivery-status success or failed")
            row = self.conn.execute("SELECT * FROM reminder_runs WHERE run_id=? AND mode='dispatch'", (run_id,)).fetchone()
            if not row:
                raise MemoError(f"Dispatch run not found: {run_id}")
            if row["delivery_status"] == "success":
                return {"run_id": run_id, "delivery_status": "success", "idempotent_replay": True}
            finished = iso_now()
            try:
                with transaction(self.conn):
                    self.conn.execute(
                        """UPDATE reminder_runs
                           SET completed_at=?,delivery_status=?,error_message=?,success_at=?
                           WHERE run_id=?""",
                        (
                            finished,
                            delivery_status,
                            truncate(error_message, 500) or None,
                            finished if delivery_status == "success" else None,
                            run_id,
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                if delivery_status == "success":
                    return {"run_id": run_id, "delivery_status": "success", "duplicate_success": True}
                raise MemoError(str(exc)) from exc
            return {
                "run_id": run_id,
                "item_id": row["item_id"],
                "delivery_status": delivery_status,
                "success_at": finished if delivery_status == "success" else None,
                "business_state_changed": False,
            }

        if not delivery_target:
            raise MemoError("Preparing exact reminders requires --delivery-target")
        now_dt = parse_datetime(now, self._timezone()) if now else utc_now()
        now_utc = now_dt.astimezone(dt.timezone.utc)
        lease_cutoff = now_utc - dt.timedelta(minutes=10)
        candidates = self.list_items(("active",), create_snapshot=False)["items"]
        due: list[dict[str, Any]] = []
        skipped_pending = 0
        with transaction(self.conn):
            for item in candidates:
                remind_at = item.get("remind_at")
                if not remind_at or item.get("remind_precision") != "datetime":
                    continue
                remind_dt = parse_datetime(str(remind_at), str(item["timezone"])).astimezone(dt.timezone.utc)
                if remind_dt > now_utc:
                    continue
                dedupe = self._reminder_dedupe_key(str(item["id"]), str(remind_at), delivery_target)
                success = self.conn.execute(
                    "SELECT 1 FROM reminder_runs WHERE dedupe_key=? AND delivery_status='success'",
                    (dedupe,),
                ).fetchone()
                if success:
                    continue
                pending = self.conn.execute(
                    """SELECT attempted_at FROM reminder_runs
                       WHERE dedupe_key=? AND delivery_status='pending'
                       ORDER BY attempted_at DESC LIMIT 1""",
                    (dedupe,),
                ).fetchone()
                if pending and pending["attempted_at"]:
                    attempted = parse_datetime(str(pending["attempted_at"]), self._timezone()).astimezone(dt.timezone.utc)
                    if attempted > lease_cutoff:
                        skipped_pending += 1
                        continue
                prepared_at = now_utc.replace(microsecond=0).isoformat()
                prepared_run = "R-" + uuid.uuid4().hex.upper()
                self.conn.execute(
                    """INSERT INTO reminder_runs(
                        run_id,mode,started_at,item_count,delivery_target,delivery_status,is_test,
                        item_id,remind_at,dedupe_key,attempted_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        prepared_run, "dispatch", prepared_at, 1, delivery_target, "pending", int(is_test),
                        item["id"], remind_at, dedupe, prepared_at,
                    ),
                )
                prepared = dict(item)
                prepared["delivery_run_id"] = prepared_run
                prepared["delivery_key"] = dedupe
                due.append(prepared)
        snapshot_id = self.create_snapshot(
            due,
            platform=platform,
            user_id=user_id,
            chat_id=chat_id,
            topic_id=topic_id,
            view_kind="exact-reminders",
        ) if due else None
        return {
            "count": len(due),
            "items": due,
            "snapshot_id": snapshot_id,
            "delivery_target": delivery_target,
            "prepared_at": now_utc.replace(microsecond=0).isoformat(),
            "pending_lease_skips": skipped_pending,
            "business_state_changed": False,
        }

    @staticmethod
    def _display_time(item: dict[str, Any], field: str) -> str:
        value = item.get(field)
        if not value:
            return "无"
        precision_field = {
            "due_at": "due_precision",
            "remind_at": "remind_precision",
            "scheduled_for": "scheduled_precision",
            "defer_until": "defer_precision",
        }.get(field)
        if precision_field and item.get(precision_field) == "date":
            return f"{value}，未指定具体时间"
        if item.get("time_uncertain") and field in {"due_at", "scheduled_for", "remind_at"}:
            return f"{value}（时间待确认）"
        return str(value)

    @staticmethod
    def _priority_label(value: str) -> str:
        return {"urgent": "紧急", "high": "高", "normal": "普通", "low": "低"}.get(value, value)

    def export_markdown(self) -> dict[str, Any]:
        current = self.list_items(create_snapshot=False)["items"]
        archive_rows: list[dict[str, Any]] = []
        for status in ("completed", "deleted", "archived"):
            archive_rows.extend(self.list_items((status,), create_snapshot=False)["items"])
        current_text = self._render_export("Current personal memos", current)
        archive_text = self._render_export("Personal memo archive", archive_rows)
        self.paths.exports_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._atomic_text(self.paths.current_export, current_text)
        self._atomic_text(self.paths.archive_export, archive_text)
        return {
            "current": str(self.paths.current_export),
            "archive": str(self.paths.archive_export),
            "active_count": len(current),
            "archive_count": len(archive_rows),
        }

    @staticmethod
    def _atomic_text(path: Path, text: str) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, path)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(temp_name)

    def _render_export(self, heading: str, items: Sequence[dict[str, Any]]) -> str:
        lines = [f"# {heading}", "", f"Generated: {iso_now()}", "", "SQLite is the source of truth; do not edit this export to change data.", ""]
        for index, item in enumerate(items, start=1):
            lines.extend(
                [
                    f"## #{index} · {item['id']} · {item['title']}",
                    "",
                    f"- Status: {item['status']}",
                    f"- Type: {item['item_type']}",
                    f"- Due: {self._display_time(item, 'due_at')}",
                    f"- Scheduled: {self._display_time(item, 'scheduled_for')}",
                    f"- Reminder: {self._display_time(item, 'remind_at')}",
                    f"- Priority: {item['priority_level']} ({item['priority_source']})",
                    "",
                    item["content"] or "_(no content)_",
                    "",
                ]
            )
            if item.get("title"):
                lines.extend([f"Summary: {item['title']}", ""])
            for source in item.get("sources", []):
                lines.extend(
                    [
                        f"- Source: {source['original_url']}",
                        f"  - Ingest: {source['ingest_status']}",
                        f"  - Suggested action: {source.get('suggested_action') or '之后打开该链接，确认是否值得进一步处理。'}",
                    ]
                )
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def restore_backup(self, backup: str | os.PathLike[str], *, confirm: str) -> dict[str, Any]:
        path = Path(backup).expanduser().resolve()
        if confirm != "RESTORE":
            raise MemoError("Destructive restore requires --confirm RESTORE")
        integrity = self._check_backup(path)
        if integrity != "ok":
            raise MemoError(f"Backup integrity check failed: {integrity}")
        candidate = sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True)
        try:
            candidate_schema = int(candidate.execute("PRAGMA user_version").fetchone()[0])
        finally:
            candidate.close()
        if candidate_schema < 1 or candidate_schema > SCHEMA_VERSION:
            raise MemoError(
                f"Backup schema {candidate_schema} is not safely supported by schema {SCHEMA_VERSION}"
            )
        safety = self._backup(prefix="pre-restore", keep=10)
        assert self.conn is not None
        with contextlib.suppress(sqlite3.Error):
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.close()

        def install(source_path: Path) -> None:
            fd, temp_name = tempfile.mkstemp(prefix="restore-", suffix=".sqlite3", dir=self.paths.data_dir)
            os.close(fd)
            try:
                source = sqlite3.connect(source_path)
                target = sqlite3.connect(temp_name)
                try:
                    source.backup(target)
                    restored_integrity = str(target.execute("PRAGMA integrity_check").fetchone()[0])
                    if restored_integrity != "ok":
                        raise MemoError(f"Restored copy failed integrity check: {restored_integrity}")
                finally:
                    source.close()
                    target.close()
                os.chmod(temp_name, 0o600)
                os.replace(temp_name, self.paths.db_path)
                for suffix in ("-wal", "-shm"):
                    with contextlib.suppress(OSError):
                        Path(str(self.paths.db_path) + suffix).unlink()
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(temp_name)

        try:
            install(path)
            self.conn = self._open()
            self.initialize()
            restored_validation = self.validate()
            if not restored_validation["ok"]:
                raise MemoError("Restored database failed validation: " + "; ".join(restored_validation["issues"]))
        except Exception as exc:
            self.close()
            install(safety)
            self.conn = self._open()
            self.initialize()
            raise MemoError(f"Restore failed; the previous database was recovered: {exc}") from exc
        self._secure_files()
        return {
            "restored_from": str(path),
            "pre_restore_backup": str(safety),
            "schema_version": int(self.conn.execute("PRAGMA user_version").fetchone()[0]),
            "integrity": self.conn.execute("PRAGMA integrity_check").fetchone()[0],
        }

    def validate(self) -> dict[str, Any]:
        assert self.conn is not None
        issues: list[str] = []
        required_tables = {
            "items", "sources", "item_sources", "item_events", "view_snapshots",
            "settings", "schema_migrations", "reminder_runs",
        }
        actual_tables = {
            str(row[0])
            for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing_tables = sorted(required_tables - actual_tables)
        if missing_tables:
            issues.append("missing tables: " + ", ".join(missing_tables))
        try:
            integrity = str(self.conn.execute("PRAGMA integrity_check").fetchone()[0])
        except sqlite3.Error as exc:
            integrity = f"error: {exc}"
        if integrity != "ok":
            issues.append(f"database integrity: {integrity}")
        schema = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if schema != SCHEMA_VERSION:
            issues.append(f"schema version is {schema}, expected {SCHEMA_VERSION}")
        foreign = list(self.conn.execute("PRAGMA foreign_key_check"))
        if foreign:
            issues.append(f"foreign key violations: {len(foreign)}")
        duplicate_ids = int(
            self.conn.execute("SELECT COUNT(*) FROM (SELECT id FROM items GROUP BY id HAVING COUNT(*)>1)").fetchone()[0]
        )
        if duplicate_ids:
            issues.append(f"duplicate stable IDs: {duplicate_ids}")
        invalid_dates = []
        for row in self.conn.execute("SELECT id,due_at,remind_at,scheduled_for,defer_until,timezone FROM items"):
            for field in ("due_at", "remind_at", "scheduled_for", "defer_until"):
                if row[field]:
                    try:
                        parse_datetime(str(row[field]), str(row["timezone"]))
                    except MemoError:
                        invalid_dates.append(f"{row['id']}:{field}")
        if invalid_dates:
            issues.append("invalid dates: " + ", ".join(invalid_dates[:10]))
        invalid_precision = int(
            self.conn.execute(
                """SELECT COUNT(*) FROM items WHERE
                   (remind_at IS NOT NULL AND remind_precision IS NULL) OR
                   (remind_at IS NULL AND remind_precision IS NOT NULL AND remind_precision!='uncertain') OR
                   (scheduled_for IS NOT NULL AND scheduled_precision IS NULL) OR
                   (scheduled_for IS NULL AND scheduled_precision IS NOT NULL AND scheduled_precision!='uncertain') OR
                   (defer_until IS NOT NULL AND defer_precision IS NULL) OR
                   (defer_until IS NULL AND defer_precision IS NOT NULL AND defer_precision!='uncertain') OR
                   (due_at IS NOT NULL AND due_precision IS NULL) OR
                   (due_at IS NULL AND due_precision IS NOT NULL AND due_precision!='uncertain')"""
            ).fetchone()[0]
        )
        if invalid_precision:
            issues.append(f"time/precision mismatches: {invalid_precision}")
        return {
            "ok": not issues,
            "issues": issues,
            "integrity": integrity,
            "schema_version": schema,
            "foreign_key_violations": len(foreign),
            "duplicate_stable_ids": duplicate_ids,
            "missing_tables": missing_tables,
            "time_precision_mismatches": invalid_precision,
            "database": str(self.paths.db_path),
        }

    def _cron_jobs(self) -> list[dict[str, Any]]:
        jobs_path = self.paths.hermes_home / "cron" / "jobs.json"
        if not jobs_path.exists():
            return []
        try:
            data = json.loads(jobs_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        jobs = data.get("jobs", []) if isinstance(data, dict) else data
        return [job for job in jobs if isinstance(job, dict)] if isinstance(jobs, list) else []

    @staticmethod
    def _load_env_keys(path: Path) -> set[str]:
        if not path.exists():
            return set()
        keys: set[str] = set()
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return set()
        for line in lines:
            match = re.match(r"\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*)", line)
            if match and match.group(2).strip().strip("\"'"):
                keys.add(match.group(1))
        return keys

    def _home_channels(self) -> list[str]:
        keys = set(os.environ) | self._load_env_keys(self.paths.hermes_home / ".env")
        platforms = []
        for key in keys:
            if key.endswith("_HOME_CHANNEL") and key not in {"HERMES_HOME_CHANNEL"}:
                platforms.append(key[: -len("_HOME_CHANNEL")].lower())
        return sorted(set(platforms))

    def cron_plan(self, *, provider: str | None, model: str | None, deliver: str | None) -> dict[str, Any]:
        existing = {str(job.get("name")) for job in self._cron_jobs()}
        channels = self._home_channels()
        issues = []
        if not provider or not model:
            issues.append("Provider and model must be explicitly pinned before cron creation.")
        if not deliver:
            issues.append("A configured home-channel delivery target is required.")
        elif deliver in {"local", "origin"}:
            issues.append("Reminder cron must target a gateway home channel, not local/origin delivery.")
        elif deliver == "all" and not channels:
            issues.append("No home channel is configured for deliver=all; run /sethome first.")
        elif ":" not in deliver and deliver not in channels:
            issues.append(f"Home channel for {deliver} is not configured; run /sethome or configure it first.")
        timezone = self._timezone()
        jobs = [
            {
                "name": name,
                "schedule": schedule,
                "timezone": timezone,
                "prompt": prompt,
                "skills": ["personal-memo"],
                "provider": provider,
                "model": model,
                "deliver": deliver,
                "exists": name in existing,
            }
            for name, schedule, prompt in CRON_DEFINITIONS
        ]
        return {
            "ready": not issues,
            "issues": issues,
            "timezone": timezone,
            "jobs": jobs,
            "will_create": [job["name"] for job in jobs if not job["exists"]],
        }

    def _run_hermes(self, args: Sequence[str]) -> dict[str, Any]:
        executable = shutil.which("hermes")
        if not executable:
            return {"available": False, "ok": False, "output": "Hermes CLI not found"}
        env = os.environ.copy()
        env["HERMES_HOME"] = str(self.paths.hermes_home)
        try:
            completed = subprocess.run(
                [executable, *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=20,
                env=env,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"available": True, "ok": False, "output": truncate(str(exc), 1000)}
        return {"available": True, "ok": completed.returncode == 0, "output": truncate(completed.stdout, 2000), "returncode": completed.returncode}

    def _hermes_component_status(self, component: str) -> dict[str, Any]:
        root_help = self._run_hermes(["--help"])
        if not root_help.get("available"):
            return root_help
        if not re.search(rf"\b{re.escape(component)}\b", str(root_help.get("output", "")), re.I):
            return {
                "available": True,
                "ok": False,
                "output": f"Hermes help does not advertise a {component} command; no status command was guessed.",
            }
        component_help = self._run_hermes([component, "--help"])
        if not component_help.get("ok") or not re.search(
            r"\bstatus\b", str(component_help.get("output", "")), re.I
        ):
            return {
                "available": True,
                "ok": False,
                "output": f"Hermes {component} help does not advertise status; no status command was guessed.",
            }
        return self._run_hermes([component, "status"])

    def doctor(self) -> dict[str, Any]:
        validation = self.validate()
        assert self.conn is not None
        backups = sorted(self.paths.backups_dir.glob("*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
        latest_backup = backups[0] if backups else None
        jobs = self._cron_jobs()
        cron_by_name = {str(job.get("name")): job for job in jobs}
        cron_status = self._hermes_component_status("cron")
        gateway_status = self._hermes_component_status("gateway")
        detected_homes: set[str] = set()
        for output in (cron_status.get("output", ""), gateway_status.get("output", "")):
            for match in re.finditer(r"HERMES_HOME\s*[:=]\s*([^\s]+)", str(output), re.I):
                detected_homes.add(str(Path(match.group(1).strip("\"'")).expanduser().resolve()))
        if detected_homes:
            hermes_home_consistent: bool | None = detected_homes == {str(self.paths.hermes_home)}
        else:
            hermes_home_consistent = None
        channels = self._home_channels()
        expected_skill = self.paths.hermes_home / "skills" / "productivity" / "personal-memo" / "SKILL.md"
        alternate_skill = self.paths.hermes_home / "skills" / "personal-memo" / "SKILL.md"
        usage_file = self.paths.hermes_home / "skills" / ".usage.json"
        pinned = False
        if usage_file.exists():
            try:
                usage = json.loads(usage_file.read_text(encoding="utf-8"))
                entry = usage.get("personal-memo", {}) if isinstance(usage, dict) else {}
                pinned = bool(entry.get("pinned")) if isinstance(entry, dict) else False
            except (OSError, json.JSONDecodeError):
                pass
        stale_cutoff = (utc_now() - dt.timedelta(hours=24)).isoformat()
        stale_processing = int(
            self.conn.execute(
                """SELECT COUNT(DISTINCT i.id) FROM items i
                   JOIN item_sources x ON x.item_id=i.id
                   JOIN sources s ON s.source_id=x.source_id
                   WHERE s.ingest_status='processing' AND i.created_at<?""",
                (stale_cutoff,),
            ).fetchone()[0]
        )
        failed_sources = int(self.conn.execute("SELECT COUNT(*) FROM sources WHERE ingest_status='failed'").fetchone()[0])
        orphan_sources = int(
            self.conn.execute(
                """SELECT COUNT(*) FROM sources s
                   LEFT JOIN item_sources x ON x.source_id=s.source_id
                   WHERE x.source_id IS NULL"""
            ).fetchone()[0]
        )
        duplicate_sources = int(
            self.conn.execute(
                """SELECT COUNT(*) FROM (
                       SELECT s.canonical_url FROM sources s
                       JOIN item_sources x ON x.source_id=s.source_id
                       JOIN items i ON i.id=x.item_id
                       WHERE i.status='active'
                       GROUP BY s.canonical_url HAVING COUNT(DISTINCT i.id)>1
                   )"""
            ).fetchone()[0]
        )
        permissions = stat.S_IMODE(self.paths.data_dir.stat().st_mode)
        private_paths = [self.paths.db_path]
        private_paths.extend(self.paths.backups_dir.glob("*.sqlite3"))
        private_paths.extend(self.paths.exports_dir.glob("*.md"))
        for suffix in ("-wal", "-shm"):
            candidate = Path(str(self.paths.db_path) + suffix)
            if candidate.exists():
                private_paths.append(candidate)
        insecure_files = {
            str(path): oct(stat.S_IMODE(path.stat().st_mode))
            for path in private_paths
            if path.exists() and stat.S_IMODE(path.stat().st_mode) & 0o077
        }
        latest_reminder = self.conn.execute(
            """SELECT run_id,mode,attempted_at,completed_at,delivery_target,delivery_status,error_message,success_at
               FROM reminder_runs ORDER BY COALESCE(attempted_at,started_at) DESC LIMIT 1"""
        ).fetchone()
        problems = list(validation["issues"])
        if permissions & 0o077:
            problems.append(f"data directory permissions are {oct(permissions)}, expected 0o700")
        if insecure_files:
            problems.append(f"{len(insecure_files)} memo file(s) are accessible by other users")
        if not (expected_skill.exists() or alternate_skill.exists()):
            problems.append("skill is not installed in this HERMES_HOME")
        if not pinned:
            problems.append("skill is not pinned")
        expected_crons = tuple(name for name, _, _ in CRON_DEFINITIONS)
        for name in expected_crons:
            if name not in cron_by_name:
                problems.append(f"cron job missing: {name}")
        if not channels:
            problems.append("no home channel configured; run /sethome before enabling delivery")
        if not cron_status["available"]:
            problems.append("Hermes CLI unavailable; gateway and scheduler cannot be verified")
        elif hermes_home_consistent is None:
            problems.append("gateway/cron HERMES_HOME could not be verified from Hermes status output")
        elif not hermes_home_consistent:
            problems.append("gateway/cron HERMES_HOME differs from the memo database HERMES_HOME")
        if stale_processing:
            problems.append(f"{stale_processing} item(s) have source ingestion pending for more than 24 hours")
        if failed_sources:
            problems.append(f"{failed_sources} source(s) failed ingestion")
        if orphan_sources:
            problems.append(f"{orphan_sources} source(s) are not linked to an item")
        if duplicate_sources:
            problems.append(f"{duplicate_sources} canonical source(s) are linked to multiple active items")
        return {
            "ok": not problems,
            "hermes_home": str(self.paths.hermes_home),
            "database": str(self.paths.db_path),
            "data_directory_mode": oct(permissions),
            "database_mode": oct(stat.S_IMODE(self.paths.db_path.stat().st_mode)),
            "insecure_files": insecure_files,
            "database_exists": self.paths.db_path.exists(),
            "integrity": validation["integrity"],
            "schema_version": validation["schema_version"],
            "migration_needed": validation["schema_version"] < SCHEMA_VERSION,
            "latest_backup": str(latest_backup) if latest_backup else None,
            "latest_backup_integrity": self._check_backup(latest_backup) if latest_backup else None,
            "cron": {
                name: {
                    "exists": name in cron_by_name,
                    "next_run_at": cron_by_name.get(name, {}).get("next_run_at"),
                    "last_run_at": cron_by_name.get(name, {}).get("last_run_at"),
                    "last_status": cron_by_name.get(name, {}).get("last_status"),
                    "provider": cron_by_name.get(name, {}).get("provider"),
                    "model": cron_by_name.get(name, {}).get("model"),
                    "deliver": cron_by_name.get(name, {}).get("deliver"),
                }
                for name in expected_crons
            },
            "gateway": gateway_status,
            "scheduler": cron_status,
            "home_channels": channels,
            "skill_discovered": expected_skill.exists() or alternate_skill.exists(),
            "skill_pinned": pinned,
            "stale_processing_items": stale_processing,
            "failed_sources": failed_sources,
            "orphan_sources": orphan_sources,
            "duplicate_sources": duplicate_sources,
            "duplicate_stable_ids": validation["duplicate_stable_ids"],
            "hermes_home_consistent": hermes_home_consistent,
            "detected_runtime_homes": sorted(detected_homes),
            "latest_reminder_run": dict(latest_reminder) if latest_reminder else None,
            "problems": problems,
        }


def add_json_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Emit structured JSON")


def add_scope_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--platform")
    parser.add_argument("--user-id")
    parser.add_argument("--chat-id")
    parser.add_argument("--topic-id")
    parser.add_argument("--session-id")


def add_idempotency_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--idempotency-key", help="Stable key used to replay retried writes safely")


def load_json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    raw = value
    if value.startswith("@"):
        raw = Path(value[1:]).expanduser().read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MemoError(f"Invalid input JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise MemoError("Input JSON must be an object")
    return parsed


def scope_from_args(args: argparse.Namespace, *, include_session: bool = False) -> dict[str, Any]:
    result = {
        "platform": getattr(args, "platform", None),
        "user_id": getattr(args, "user_id", None),
        "chat_id": getattr(args, "chat_id", None),
        "topic_id": getattr(args, "topic_id", None),
    }
    if include_session:
        result["session_id"] = getattr(args, "session_id", None)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memo.py",
        description="Persistent personal memo, task, link, and reminder manager",
    )
    parser.add_argument("--data-dir", help="Override the data directory (also PERSONAL_MEMO_DATA_DIR)")
    parser.add_argument("--json", action="store_true", help="Emit structured JSON")
    parser.add_argument("--no-prewrite-backup", action="store_true", help="Disable automatic pre-write backups")
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init", help="Initialize or migrate the database")
    add_json_option(init_parser)

    add_parser = sub.add_parser("add", help="Add a structured memo or task")
    add_parser.add_argument("content", nargs="?", default=None)
    add_parser.add_argument("--input-json", help="Structured JSON object, or @path to a JSON file")
    add_parser.add_argument("--title")
    add_parser.add_argument("--type", dest="item_type", choices=sorted(ITEM_TYPES), default="task")
    add_parser.add_argument("--url", action="append", default=[])
    add_parser.add_argument("--due")
    add_parser.add_argument("--due-precision", choices=("date", "datetime", "uncertain"))
    add_parser.add_argument("--due-raw-text")
    add_parser.add_argument("--remind-at")
    add_parser.add_argument("--remind-precision", choices=sorted(TIME_PRECISIONS))
    add_parser.add_argument("--scheduled-for")
    add_parser.add_argument("--scheduled-precision", choices=sorted(TIME_PRECISIONS))
    add_parser.add_argument("--defer-until")
    add_parser.add_argument("--defer-precision", choices=sorted(TIME_PRECISIONS))
    add_parser.add_argument("--timezone")
    add_parser.add_argument("--priority", choices=sorted(PRIORITY_LEVELS), default="normal")
    add_parser.add_argument("--priority-source", choices=sorted(PRIORITY_SOURCES), default="inferred")
    add_parser.add_argument("--priority-reason")
    add_parser.add_argument("--capture-source")
    add_parser.add_argument("--source-summary")
    add_parser.add_argument("--tag", action="append", default=[])
    add_parser.add_argument("--time-uncertain", action="store_true")
    add_parser.add_argument("--suggested-action")
    add_parser.add_argument("--action-source", choices=("user", "inferred"))
    add_parser.add_argument("--allow-duplicate", action="store_true")
    add_parser.add_argument("--instruction")
    add_idempotency_option(add_parser)
    add_scope_options(add_parser)
    add_json_option(add_parser)

    capture_parser = sub.add_parser("capture", help="Apply conservative/proactive capture rules to natural text")
    capture_parser.add_argument("text")
    capture_parser.add_argument("--chat-type", choices=("private", "group", "unknown"), default="unknown")
    capture_parser.add_argument("--explicit", action="store_true")
    capture_parser.add_argument("--redact-sensitive", action="store_true")
    add_idempotency_option(capture_parser)
    add_scope_options(capture_parser)
    add_json_option(capture_parser)

    list_parser = sub.add_parser("list", help="List items in deterministic order and save a numbered view")
    list_parser.add_argument("--status", action="append", choices=sorted(ITEM_STATUSES))
    list_parser.add_argument("--all", action="store_true")
    list_parser.add_argument("--no-snapshot", action="store_true")
    list_parser.add_argument("--view-kind", default="active")
    list_parser.add_argument("--limit", type=int)
    add_scope_options(list_parser)
    add_json_option(list_parser)

    show_parser = sub.add_parser("show", help="Show one item by stable ID or recent list number")
    show_parser.add_argument("reference")
    add_scope_options(show_parser)
    add_json_option(show_parser)

    search_parser = sub.add_parser("search", help="Search memo text, links, titles, summaries, and actions")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=int, default=20)
    search_parser.add_argument("--status", action="append", choices=sorted(ITEM_STATUSES))
    add_json_option(search_parser)

    today_parser = sub.add_parser("today", help="Show overdue, due-today, scheduled-today, and up to three suggested items")
    add_scope_options(today_parser)
    add_json_option(today_parser)

    activity_parser = sub.add_parser("activity", help="Query actual completion, deletion, or archive timestamps")
    activity_parser.add_argument("kind", choices=("completed", "deleted", "archived"))
    activity_parser.add_argument("--since")
    activity_parser.add_argument("--until")
    activity_parser.add_argument("--days", type=int)
    add_scope_options(activity_parser)
    add_json_option(activity_parser)

    update_parser = sub.add_parser("update", help="Update one item without changing its stable ID")
    update_parser.add_argument("reference")
    update_parser.add_argument("--input-json", help="Structured field JSON, or @path to a JSON file")
    update_parser.add_argument("--title")
    update_parser.add_argument("--content")
    update_parser.add_argument("--type", dest="item_type", choices=sorted(ITEM_TYPES))
    update_parser.add_argument("--due")
    update_parser.add_argument("--due-precision", choices=("date", "datetime", "uncertain"))
    update_parser.add_argument("--due-raw-text")
    update_parser.add_argument("--remind-at")
    update_parser.add_argument("--remind-precision", choices=sorted(TIME_PRECISIONS))
    update_parser.add_argument("--scheduled-for")
    update_parser.add_argument("--scheduled-precision", choices=sorted(TIME_PRECISIONS))
    update_parser.add_argument("--defer-until")
    update_parser.add_argument("--defer-precision", choices=sorted(TIME_PRECISIONS))
    update_parser.add_argument("--timezone")
    update_parser.add_argument("--priority", choices=sorted(PRIORITY_LEVELS))
    update_parser.add_argument("--priority-source", choices=sorted(PRIORITY_SOURCES))
    update_parser.add_argument("--priority-reason")
    update_parser.add_argument("--source-summary")
    update_parser.add_argument("--capture-source")
    update_parser.add_argument("--tag", action="append")
    update_parser.add_argument("--time-uncertain", action=argparse.BooleanOptionalAction, default=None)
    update_parser.add_argument("--append-note")
    update_parser.add_argument(
        "--clear",
        action="append",
        default=[],
        choices=("due_at", "due_precision", "due_raw_text", "remind_at", "remind_precision", "scheduled_for", "scheduled_precision", "defer_until", "defer_precision", "priority_reason", "source_summary", "tags", "time_uncertain"),
    )
    update_parser.add_argument("--instruction")
    add_idempotency_option(update_parser)
    add_scope_options(update_parser)
    add_json_option(update_parser)

    for name, help_text in (
        ("complete", "Mark one uniquely identified item completed"),
        ("restore-item", "Restore a completed, deleted, or archived item to active"),
        ("delete", "Soft-delete one uniquely identified item"),
        ("archive", "Archive one uniquely identified item without completing it"),
    ):
        action_parser = sub.add_parser(name, help=help_text)
        action_parser.add_argument("reference")
        action_parser.add_argument("--instruction")
        add_idempotency_option(action_parser)
        add_scope_options(action_parser)
        add_json_option(action_parser)

    purge_parser = sub.add_parser("purge", help="Physically delete one item after an external second confirmation")
    purge_parser.add_argument("reference")
    purge_parser.add_argument("--confirm", required=True, help="PERMANENTLY-DELETE:<stable-id>")
    purge_parser.add_argument("--instruction")
    add_scope_options(purge_parser)
    add_json_option(purge_parser)

    history_parser = sub.add_parser("history", help="Read immutable memo operation history")
    history_parser.add_argument("reference", nargs="?")
    history_parser.add_argument("--limit", type=int, default=100)
    history_parser.add_argument("--since")
    history_parser.add_argument("--operation")
    add_scope_options(history_parser)
    add_json_option(history_parser)

    undo_parser = sub.add_parser("undo", help="Undo the latest reversible operation and record the undo")
    undo_parser.add_argument("reference", nargs="?")
    undo_parser.add_argument("--instruction")
    add_idempotency_option(undo_parser)
    add_scope_options(undo_parser)
    add_json_option(undo_parser)

    retry_parser = sub.add_parser("retry-source", help="Retry lightweight metadata ingestion for a source or item")
    retry_parser.add_argument("reference")
    add_json_option(retry_parser)

    source_parser = sub.add_parser("source-update", help="Persist trusted, structured metadata for one source")
    source_parser.add_argument("source_id")
    source_parser.add_argument("metadata_json", help="JSON object containing allowed source fields")
    source_parser.add_argument("--instruction")
    add_idempotency_option(source_parser)
    add_json_option(source_parser)

    source_link_parser = sub.add_parser("source-link", help="Relate one existing source to another item")
    source_link_parser.add_argument("source_id")
    source_link_parser.add_argument("reference")
    source_link_parser.add_argument("--instruction")
    add_idempotency_option(source_link_parser)
    add_scope_options(source_link_parser)
    add_json_option(source_link_parser)

    reminder_parser = sub.add_parser("reminder", help="Generate a read-only business-state reminder")
    reminder_parser.add_argument("--mode", choices=("morning", "evening", "manual"), default="manual")
    reminder_parser.add_argument("--delivery-target")
    reminder_parser.add_argument("--delivery-status")
    reminder_parser.add_argument("--test", action="store_true")
    add_scope_options(reminder_parser)
    add_json_option(reminder_parser)

    dispatch_parser = sub.add_parser("dispatch-reminders", help="Prepare due exact reminders or record delivery")
    dispatch_parser.add_argument("--delivery-target")
    dispatch_parser.add_argument("--run-id")
    dispatch_parser.add_argument("--delivery-status", choices=("success", "failed"))
    dispatch_parser.add_argument("--error-message")
    dispatch_parser.add_argument("--now", help="ISO time override for deterministic testing")
    dispatch_parser.add_argument("--test", action="store_true")
    add_scope_options(dispatch_parser)
    add_json_option(dispatch_parser)

    export_parser = sub.add_parser("export", help="Rebuild readable Markdown exports from SQLite")
    add_json_option(export_parser)

    backup_parser = sub.add_parser("backup", help="Create a safe SQLite backup and rotate daily backups")
    add_json_option(backup_parser)

    restore_backup_parser = sub.add_parser("restore-backup", help="Restore a validated SQLite backup")
    restore_backup_parser.add_argument("--backup", required=True)
    restore_backup_parser.add_argument("--confirm", required=True, help="Must be RESTORE")
    add_json_option(restore_backup_parser)

    doctor_parser = sub.add_parser("doctor", help="Check paths, database, backups, skill, cron, gateway, and delivery")
    add_json_option(doctor_parser)

    validate_parser = sub.add_parser("validate", help="Validate database integrity and invariants")
    add_json_option(validate_parser)

    for config_name in ("config", "settings"):
        settings_parser = sub.add_parser(config_name, help="Read or persist user preferences")
        settings_parser.add_argument("action", choices=("list", "get", "set"))
        settings_parser.add_argument("key", nargs="?")
        settings_parser.add_argument("value", nargs="?")
        add_json_option(settings_parser)

    timezone_parser = sub.add_parser(
        "migrate-timezone",
        help="Change default and item display timezones without changing exact instants",
    )
    timezone_parser.add_argument("timezone")
    add_json_option(timezone_parser)

    cron_parser = sub.add_parser("cron-plan", help="Validate and print the three idempotent Hermes cron definitions")
    cron_parser.add_argument("--provider")
    cron_parser.add_argument("--model")
    cron_parser.add_argument("--deliver")
    add_json_option(cron_parser)
    return parser


def _updates_from_args(args: argparse.Namespace) -> dict[str, Any]:
    mapping = {
        "title": "title",
        "content": "content",
        "item_type": "item_type",
        "due": "due_at",
        "due_precision": "due_precision",
        "due_raw_text": "due_raw_text",
        "remind_at": "remind_at",
        "remind_precision": "remind_precision",
        "scheduled_for": "scheduled_for",
        "scheduled_precision": "scheduled_precision",
        "defer_until": "defer_until",
        "defer_precision": "defer_precision",
        "timezone": "timezone",
        "priority": "priority_level",
        "priority_source": "priority_source",
        "priority_reason": "priority_reason",
        "source_summary": "title",
        "capture_source": "capture_source",
        "tag": "tags",
        "time_uncertain": "time_uncertain",
    }
    updates = {}
    for arg_name, field in mapping.items():
        value = getattr(args, arg_name, None)
        if value is not None:
            updates[field] = value
    return updates


def _render_item(item: dict[str, Any], number: int | None = None) -> str:
    prefix = f"#{number} · " if number is not None else ""
    lines = [f"{prefix}{item['id']} · {item['title']}", f"类型：{item['item_type']} · 状态：{item['status']}"]
    for source in item.get("sources", []):
        if source.get("summary"):
            label = "主题概括" if source.get("source_type") == "video" else "摘要"
            lines.append(f"{label}：{source['summary']}")
        if source.get("suggested_action"):
            action_label = "用户要求" if source.get("action_source") == "user" else "建议动作"
            lines.append(f"{action_label}：{source['suggested_action']}")
        if source.get("source_type") == "video":
            basis_label = {
                "title_and_description": "视频标题和简介",
                "title_only": "仅视频标题，内容概括可能不完整",
                "metadata_only": "基础页面元数据",
                "user_context": "用户附带的说明",
                "mixed_metadata_and_user_context": "页面元数据和用户说明",
                "unavailable": "未取得页面信息",
            }.get(source.get("understanding_basis"), source.get("understanding_basis"))
            lines.append(f"理解依据：{basis_label}")
        lines.append(f"链接解析：{source.get('ingest_status')} · {source.get('original_url')}")
    due = MemoStore._display_time(item, "due_at")
    overdue = " · 已逾期" if item.get("is_overdue") else ""
    pending = " · 时间待确认" if item.get("time_pending_confirmation") else ""
    lines.append(f"截止时间：{due}{overdue}{pending} · 优先级：{MemoStore._priority_label(item['priority_level'])}")
    if item.get("scheduled_for"):
        lines.append(f"计划时间：{MemoStore._display_time(item, 'scheduled_for')}")
    if item.get("remind_at"):
        lines.append(f"提醒时间：{MemoStore._display_time(item, 'remind_at')}")
    return "\n".join(lines)


def render_human(result: Any, command: str) -> str:
    if command in {"list", "search", "today", "activity"} and isinstance(result, dict):
        items = result.get("items", [])
        if not items:
            return "没有匹配的事项。"
        return "\n\n".join(_render_item(item, index) for index, item in enumerate(items, start=1))
    if command == "reminder" and isinstance(result, dict):
        blocks = ["所有未完成事项", ""]
        items = result.get("items", [])
        blocks.append("\n\n".join(_render_item(item, index) for index, item in enumerate(items, start=1)) or "没有未完成事项。")
        blocks.extend(["", "未来 12 小时建议", ""])
        suggestions = result.get("suggestions", [])
        blocks.append("\n".join(f"{index}. {item['title']}——{item['reason']}。" for index, item in enumerate(suggestions, start=1)) or "暂无需要特别推进的事项。")
        return "\n".join(blocks)
    if command == "history" and isinstance(result, dict):
        return "\n".join(
            f"{event['created_at']} · {event['operation']} · {event['stable_item_id']} · {event['event_id']}"
            for event in result.get("events", [])
        ) or "没有历史记录。"
    if command == "doctor" and isinstance(result, dict):
        status = "通过" if result.get("ok") else "发现问题"
        lines = [f"Doctor：{status}", f"HERMES_HOME：{result.get('hermes_home')}", f"数据库：{result.get('database')}"]
        lines.extend(f"- {problem}" for problem in result.get("problems", []))
        return "\n".join(lines)
    if isinstance(result, dict) and "id" in result and "title" in result:
        return _render_item(result)
    return json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)


def _table_cell(value: Any, *, limit: int = 72) -> str:
    """Escape one Markdown-table cell without allowing user text to break it."""
    text = truncate(str(value or "—"), limit).replace("\r", " ").replace("\n", "<br>")
    return text.replace("|", "\\|")


def render_markdown_table(items: Sequence[dict[str, Any]], *, heading: str = "当前备忘录") -> str:
    """Render a stable, user-facing Markdown table for any item list."""
    if not items:
        return "没有匹配的事项。"
    type_labels = {
        "task": "待办", "note": "笔记", "link": "链接", "article": "文章",
        "video": "视频", "reference": "参考资料",
    }
    status_labels = {"active": "未完成", "completed": "已完成", "deleted": "已删除", "archived": "已归档"}
    priority_labels = {"urgent": "紧急", "high": "高", "normal": "普通", "low": "低"}
    source_labels = {"processing": "解析中", "complete": "已解析", "partial": "部分解析", "failed": "解析失败"}
    lines = [
        f"**{heading}（{len(items)} 项）**",
        "",
        "| # | 总结 | 类型 | 截止时间 | 优先级 | 状态 | 内容 |",
        "| ---: | --- | --- | --- | --- | --- | --- |",
    ]
    for index, item in enumerate(items, start=1):
        time_value = MemoStore._display_time(item, "due_at") if item.get("due_at") else "—"
        if item.get("due_at") and item.get("is_overdue"):
            time_value += "（已逾期）"
        status = status_labels.get(str(item.get("status")), str(item.get("status") or "—"))
        sources = item.get("sources") or []
        if sources:
            source_status = source_labels.get(str(sources[0].get("ingest_status")), str(sources[0].get("ingest_status") or ""))
            if source_status:
                status += f" · {source_status}"
        content = item.get("content") or "—"
        priority = priority_labels.get(str(item.get("priority_level")), str(item.get("priority_level") or "—"))
        source_summary = item.get("title") or "—"
        lines.append(
            "| {number} | {source_summary} | {item_type} | {time_value} | {priority} | {status} | {content} |".format(
                number=index,
                content=_table_cell(content, limit=64),
                item_type=_table_cell(type_labels.get(str(item.get("item_type")), str(item.get("item_type") or "—")), limit=20),
                time_value=_table_cell(time_value, limit=48),
                priority=_table_cell(priority, limit=20),
                source_summary=_table_cell(source_summary, limit=64),
                status=_table_cell(status, limit=40),
            )
        )
    return "\n".join(lines)


def render_markdown_list(items: Sequence[dict[str, Any]], *, heading: str = "当前备忘录") -> str:
    """Render a compact, readable list for direct chat slash commands."""
    if not items:
        return "没有匹配的事项。"
    type_labels = {"task": "待办", "note": "笔记", "link": "链接", "article": "文章", "video": "视频", "reference": "参考资料"}
    priority_labels = {"urgent": "紧急", "high": "高", "normal": "普通", "low": "低"}
    status_labels = {"active": "未完成", "completed": "已完成", "deleted": "已删除", "archived": "已归档"}
    lines = [f"**{heading}（{len(items)} 项）**", ""]
    for index, item in enumerate(items, start=1):
        summary = item.get("title") or "—"
        due = MemoStore._display_time(item, "due_at") if item.get("due_at") else "—"
        if item.get("due_at") and item.get("is_overdue"):
            due += "（已逾期）"
        lines.extend([
            f"**{index}. {summary}**",
            f"- 类型：{type_labels.get(str(item.get('item_type')), str(item.get('item_type') or '—'))}",
            f"- 截止时间：{due}",
            f"- 优先级：{priority_labels.get(str(item.get('priority_level')), str(item.get('priority_level') or '—'))}",
            f"- 状态：{status_labels.get(str(item.get('status')), str(item.get('status') or '—'))}",
            f"- 内容：{item.get('content') or '—'}",
            "",
        ])
    return "\n".join(lines).rstrip()


def dispatch(store: MemoStore, args: argparse.Namespace) -> Any:
    command = args.command
    if command == "init":
        return store.initialization
    if command == "add":
        kwargs: dict[str, Any] = {
            "title": args.title,
            "content": args.content or "",
            "item_type": args.item_type,
            "urls": args.url,
            "due_at": args.due,
            "due_precision": args.due_precision,
            "due_raw_text": args.due_raw_text,
            "remind_at": args.remind_at,
            "remind_precision": args.remind_precision,
            "scheduled_for": args.scheduled_for,
            "scheduled_precision": args.scheduled_precision,
            "defer_until": args.defer_until,
            "defer_precision": args.defer_precision,
            "timezone": args.timezone,
            "priority_level": args.priority,
            "priority_source": args.priority_source,
            "priority_reason": args.priority_reason,
            "capture_source": args.capture_source,
            "source_summary": args.source_summary,
            "tags": args.tag,
            "time_uncertain": args.time_uncertain,
            "suggested_action": args.suggested_action,
            "action_source": args.action_source,
            "allow_duplicate": args.allow_duplicate,
            "instruction": args.instruction,
            "platform": args.platform,
            "user_id": args.user_id,
            "chat_id": args.chat_id,
            "session_id": args.session_id,
            "idempotency_key": args.idempotency_key,
        }
        payload = load_json_object(args.input_json)
        allowed_payload = {
            "title", "content", "item_type", "urls", "due_at", "due_precision", "due_raw_text",
            "remind_at", "remind_precision", "scheduled_for", "scheduled_precision", "defer_until",
            "defer_precision", "timezone", "priority_level", "priority_source", "priority_reason",
            "capture_source", "source_summary", "tags", "time_uncertain", "suggested_action",
            "action_source", "allow_duplicate", "instruction", "idempotency_key",
        }
        invalid_payload = set(payload) - allowed_payload
        if invalid_payload:
            raise MemoError("Unsupported add JSON fields: " + ", ".join(sorted(invalid_payload)))
        kwargs.update(payload)
        if isinstance(kwargs.get("urls"), str):
            kwargs["urls"] = [kwargs["urls"]]
        return store.add_item(**kwargs)
    if command == "capture":
        context = scope_from_args(args, include_session=True)
        context.pop("topic_id", None)
        return store.capture(
            args.text,
            chat_type=args.chat_type,
            explicit=args.explicit,
            redact=args.redact_sensitive,
            idempotency_key=args.idempotency_key,
            **context,
        )
    if command == "list":
        statuses = tuple(ITEM_STATUSES) if args.all else tuple(args.status or ("active",))
        return store.list_items(
            statuses,
            create_snapshot=not args.no_snapshot,
            view_kind=args.view_kind,
            limit=args.limit,
            **{key: value for key, value in scope_from_args(args).items() if key != "session_id"},
        )
    if command == "show":
        return store.show(args.reference, **scope_from_args(args))
    if command == "search":
        return store.search(args.query, limit=args.limit, statuses=args.status)
    if command == "today":
        return store.today(**scope_from_args(args))
    if command == "activity":
        return store.activity(
            args.kind,
            since=args.since,
            until=args.until,
            days=args.days,
            **scope_from_args(args),
        )
    if command == "update":
        updates = _updates_from_args(args)
        updates.update(load_json_object(args.input_json))
        return store.update_item(
            args.reference,
            updates,
            clear_fields=args.clear,
            append_note=args.append_note,
            instruction=args.instruction,
            idempotency_key=args.idempotency_key,
            **scope_from_args(args, include_session=True),
        )
    if command in {"complete", "restore-item", "delete", "archive"}:
        action = store.restore if command == "restore-item" else getattr(store, command)
        return action(
            args.reference,
            instruction=args.instruction,
            idempotency_key=args.idempotency_key,
            **scope_from_args(args, include_session=True),
        )
    if command == "purge":
        return store.purge(
            args.reference,
            confirm=args.confirm,
            instruction=args.instruction,
            **scope_from_args(args, include_session=True),
        )
    if command == "history":
        return store.history(
            args.reference,
            limit=args.limit,
            since=args.since,
            operation=args.operation,
            **scope_from_args(args),
        )
    if command == "undo":
        return store.undo(
            args.reference,
            instruction=args.instruction,
            idempotency_key=args.idempotency_key,
            **scope_from_args(args, include_session=True),
        )
    if command == "retry-source":
        return store.retry_source(args.reference)
    if command == "source-update":
        try:
            updates = json.loads(args.metadata_json)
        except json.JSONDecodeError as exc:
            raise MemoError(f"Invalid metadata JSON: {exc}") from exc
        if not isinstance(updates, dict):
            raise MemoError("metadata_json must be a JSON object")
        return store.update_source(
            args.source_id,
            updates,
            instruction=args.instruction,
            idempotency_key=args.idempotency_key,
        )
    if command == "source-link":
        return store.link_source(
            args.source_id,
            args.reference,
            instruction=args.instruction,
            idempotency_key=args.idempotency_key,
            **scope_from_args(args, include_session=True),
        )
    if command == "reminder":
        return store.reminder(
            args.mode,
            delivery_target=args.delivery_target,
            delivery_status=args.delivery_status,
            is_test=args.test,
            **scope_from_args(args),
        )
    if command == "dispatch-reminders":
        return store.dispatch_reminders(
            delivery_target=args.delivery_target,
            run_id=args.run_id,
            delivery_status=args.delivery_status,
            error_message=args.error_message,
            now=args.now,
            is_test=args.test,
            **scope_from_args(args),
        )
    if command == "export":
        return store.export_markdown()
    if command == "backup":
        return store.manual_backup()
    if command == "restore-backup":
        return store.restore_backup(args.backup, confirm=args.confirm)
    if command == "doctor":
        return store.doctor()
    if command == "validate":
        return store.validate()
    if command in {"config", "settings"}:
        if args.action == "list":
            return store.settings()
        if not args.key:
            raise MemoError("settings get/set requires a key")
        if args.action == "get":
            return {"key": args.key, "value": store.get_setting(args.key)}
        if args.value is None:
            raise MemoError("settings set requires a value")
        return store.set_setting(args.key, args.value)
    if command == "migrate-timezone":
        return store.migrate_timezone(args.timezone)
    if command == "cron-plan":
        return store.cron_plan(provider=args.provider, model=args.model, deliver=args.deliver)
    raise MemoError(f"Unknown command: {command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = MemoPaths.resolve(args.data_dir)
    try:
        with MemoStore(paths, prewrite_backups=not args.no_prewrite_backup) as store:
            result = dispatch(store, args)
        if getattr(args, "json", False):
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(render_human(result, args.command))
        return 0
    except (MemoError, sqlite3.Error, OSError) as exc:
        error = {"ok": False, "error": str(exc), "command": getattr(args, "command", None)}
        if getattr(args, "json", False):
            print(json.dumps(error, ensure_ascii=False, indent=2), file=sys.stderr)
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
