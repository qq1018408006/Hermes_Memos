"""Compact JSON schemas for Hermes personal-memo tools."""

from __future__ import annotations


SCOPE = {
    "platform": {"type": "string", "description": "Messaging platform when available."},
    "user_id": {"type": "string", "description": "Platform user identifier when available."},
    "chat_id": {"type": "string", "description": "Chat identifier when available."},
    "topic_id": {"type": "string", "description": "Optional thread or topic identifier."},
    "session_id": {"type": "string", "description": "Optional Hermes session identifier."},
}


def schema(name: str, description: str, properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
    }


MEMO_ADD = schema(
    "memo_add",
    "Create one persistent memo, task, link, or video item. Preserve the user's original content and use an idempotency key for retried writes.",
    {
        "title": {"type": "string", "description": "Short title; may be omitted when content or urls identify the item."},
        "content": {"type": "string", "description": "Original user wording or useful notes."},
        "item_type": {"type": "string", "enum": ["task", "memo", "link", "video"]},
        "urls": {"type": "array", "items": {"type": "string"}},
        "due_at": {"type": "string"}, "due_precision": {"type": "string", "enum": ["date", "datetime", "uncertain"]},
        "due_raw_text": {"type": "string"}, "remind_at": {"type": "string"},
        "remind_precision": {"type": "string", "enum": ["date", "datetime", "uncertain"]},
        "scheduled_for": {"type": "string"}, "scheduled_precision": {"type": "string", "enum": ["date", "datetime", "uncertain"]},
        "defer_until": {"type": "string"}, "defer_precision": {"type": "string", "enum": ["date", "datetime", "uncertain"]},
        "timezone": {"type": "string"}, "priority_level": {"type": "string", "enum": ["urgent", "high", "normal", "low"]},
        "priority_source": {"type": "string", "enum": ["user", "inferred"]}, "priority_reason": {"type": "string"},
        "capture_source": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}}, "time_uncertain": {"type": "boolean"},
        "suggested_action": {"type": "string"}, "action_source": {"type": "string", "enum": ["user", "inferred"]},
        "allow_duplicate": {"type": "boolean"}, "instruction": {"type": "string"},
        "idempotency_key": {"type": "string", "description": "Stable key for a write that can be retried."}, **SCOPE,
    },
)
MEMO_CAPTURE = schema("memo_capture", "Apply conservative capture rules to raw user text; do not silently save possible secrets. After a successful capture, the agent must add a concise summary in the title field with memo_add or memo_update, including for plain-text tasks and reminders.", {
    "text": {"type": "string"}, "chat_type": {"type": "string", "enum": ["private", "group", "unknown"]},
    "explicit": {"type": "boolean"}, "redact_sensitive": {"type": "boolean"}, "idempotency_key": {"type": "string"}, **SCOPE,
}, ("text",))
MEMO_LIST = schema("memo_list", "List items in deterministic order and optionally create a numbered snapshot. The result includes display_markdown: a complete Markdown table that must be shown verbatim to the user without reformatting.", {
    "statuses": {"type": "array", "items": {"type": "string", "enum": ["active", "completed", "deleted", "archived"]}},
    "all": {"type": "boolean"}, "create_snapshot": {"type": "boolean"}, "view_kind": {"type": "string"}, "limit": {"type": "integer", "minimum": 1}, **SCOPE,
})
MEMO_TABLE = schema("memo_table", "Return the final user-facing memo list as Markdown. The markdown field is a complete fixed table and must be shown verbatim, with no bullet-list rewrite or commentary.", {
    "statuses": {"type": "array", "items": {"type": "string", "enum": ["active", "completed", "deleted", "archived"]}},
    "all": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1}, **SCOPE,
})
MEMO_SHOW = schema("memo_show", "Show one item by stable ID or a number from the recent scoped snapshot.", {"reference": {"type": "string"}, **SCOPE}, ("reference",))
MEMO_SEARCH = schema("memo_search", "Search titles, content, saved links, summaries, and actions.", {
    "query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1},
    "statuses": {"type": "array", "items": {"type": "string", "enum": ["active", "completed", "deleted", "archived"]}},
}, ("query",))
MEMO_TODAY = schema("memo_today", "Show overdue, due-today, scheduled-today, and limited suggestions.", dict(SCOPE))
MEMO_ACTIVITY = schema("memo_activity", "List items by real completion, deletion, or archive timestamps.", {
    "kind": {"type": "string", "enum": ["completed", "deleted", "archived"]}, "since": {"type": "string"},
    "until": {"type": "string"}, "days": {"type": "integer", "minimum": 0}, **SCOPE,
}, ("kind",))
MEMO_UPDATE = schema("memo_update", "Update one uniquely identified item without changing its stable ID.", {
    "reference": {"type": "string"}, "updates": {"type": "object"},
    "clear_fields": {"type": "array", "items": {"type": "string"}}, "append_note": {"type": "string"},
    "instruction": {"type": "string"}, "idempotency_key": {"type": "string"}, **SCOPE,
}, ("reference", "updates"))
MEMO_TRANSITION = schema("memo_transition", "Complete, soft-delete, archive, restore, or undo one uniquely identified memo item.", {
    "action": {"type": "string", "enum": ["complete", "delete", "archive", "restore", "undo"]},
    "reference": {"type": "string"}, "instruction": {"type": "string"}, "idempotency_key": {"type": "string"}, **SCOPE,
}, ("action",))
MEMO_HISTORY = schema("memo_history", "Read immutable memo operation history.", {
    "reference": {"type": "string"}, "limit": {"type": "integer", "minimum": 1}, "since": {"type": "string"},
    "operation": {"type": "string"}, **SCOPE,
})
MEMO_SOURCE = schema("memo_source", "Retry link metadata, update trusted source metadata, or link an existing source to another item.", {
    "action": {"type": "string", "enum": ["retry", "update", "link"]}, "reference": {"type": "string"},
    "source_id": {"type": "string"}, "metadata": {"type": "object"}, "instruction": {"type": "string"},
    "idempotency_key": {"type": "string"}, **SCOPE,
}, ("action",))
MEMO_REMINDER = schema("memo_reminder", "Generate a deterministic morning, evening, or manual memo reminder without changing item business state.", {
    "mode": {"type": "string", "enum": ["morning", "evening", "manual"]}, "delivery_target": {"type": "string"},
    "delivery_status": {"type": "string"}, "test": {"type": "boolean"}, **SCOPE,
})
MEMO_ADMIN = schema("memo_admin", "Administrative memo operations. Purge and restore-backup require an exact confirmation token and belong in the opt-in admin toolset.", {
    "action": {"type": "string", "enum": ["backup", "export", "validate", "doctor", "settings_get", "settings_list", "settings_set", "timezone_migrate", "restore_backup", "purge", "dispatch_reminders"]},
    "key": {"type": "string"}, "value": {"type": "string"}, "reference": {"type": "string"},
    "timezone": {"type": "string"},
    "backup": {"type": "string"}, "confirm": {"type": "string"}, "instruction": {"type": "string"},
    "delivery_target": {"type": "string"}, "run_id": {"type": "string"}, "delivery_status": {"type": "string", "enum": ["success", "failed"]},
    "error_message": {"type": "string"}, "now": {"type": "string"}, "test": {"type": "boolean"}, **SCOPE,
}, ("action",))
