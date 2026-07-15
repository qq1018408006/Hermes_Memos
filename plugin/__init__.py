"""Hermes registration for the personal-memo native tools."""

from __future__ import annotations

import logging
import datetime as dt
import os
from pathlib import Path

from . import schemas, tools
from .store import get_store


logger = logging.getLogger(__name__)


def _memos_command(raw_args: str) -> str:
    """Return the core-rendered table directly, without LLM reformatting."""
    try:
        store, module = get_store()
        statuses = tuple(module.ITEM_STATUSES) if raw_args.strip().lower() in {"all", "全部"} else ("active",)
        result = store.list_items(statuses, create_snapshot=True)
        return module.render_markdown_list(result["items"])
    except Exception as exc:
        return f"无法读取备忘录：{exc}"


_REFRESH_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "item_type": {"type": "string", "enum": ["task", "note", "link", "article", "video", "reference"]},
        "due_at": {"type": ["string", "null"]}, "due_precision": {"type": ["string", "null"], "enum": ["date", "datetime", "uncertain", None]},
        "due_raw_text": {"type": ["string", "null"]},
        "remind_at": {"type": ["string", "null"]}, "remind_precision": {"type": ["string", "null"], "enum": ["date", "datetime", "uncertain", None]},
        "scheduled_for": {"type": ["string", "null"]}, "scheduled_precision": {"type": ["string", "null"], "enum": ["date", "datetime", "uncertain", None]},
        "defer_until": {"type": ["string", "null"]}, "defer_precision": {"type": ["string", "null"], "enum": ["date", "datetime", "uncertain", None]},
        "priority_level": {"type": "string", "enum": ["urgent", "high", "normal", "low"]},
        "priority_reason": {"type": ["string", "null"]},
        "time_uncertain": {"type": "boolean"},
    },
    "required": ["title", "item_type", "due_at", "due_precision", "due_raw_text", "remind_at", "remind_precision", "scheduled_for", "scheduled_precision", "defer_until", "defer_precision", "priority_level", "priority_reason", "time_uncertain"],
}


def _refresh_context() -> str:
    """Load stable persona/profile context for the standalone refresh call."""
    home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    parts = []
    for label, path in (("agent persona", home / "SOUL.md"), ("user profile", home / "memories" / "USER.md"), ("durable memory", home / "memories" / "MEMORY.md")):
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        if text:
            parts.append(f"## {label}\n{text}")
    return "\n\n".join(parts)


def _llm_refresh_item(ctx, store, item: dict):
    """Re-run the agent's extraction step against the immutable original content."""
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    result = ctx.llm.complete_structured(
        instructions=(
            "Re-interpret one saved memo from its original content. Return only the requested JSON. "
            "Generate a concise title that summarizes the memo, including plain-text tasks. "
            "Extract dates and reminders conservatively; use ISO/RFC3339 with the item's timezone, "
            "preserve uncertain wording, and use null when the content does not specify a value. "
            f"Today is {today}. Do not invent facts or change the original content."
        ),
        input=[{"type": "text", "text": str(item.get("content") or "")}],
        json_schema=_REFRESH_SCHEMA,
        system_prompt=(
            "You are refreshing a personal memo for the same user. Use the following stable persona and profile "
            "only to make the summary relevant; never invent facts not supported by the memo.\n\n"
            + _refresh_context()
        ),
        schema_name="personal_memo.refresh",
        purpose="personal-memo-refresh",
        temperature=0.0,
        max_tokens=700,
    )
    if result.parsed is None:
        raise RuntimeError(f"刷新解析失败：{result.text}")
    parsed = dict(result.parsed)
    updates = {key: parsed.get(key) for key in _REFRESH_SCHEMA["properties"]}
    updates["priority_source"] = "user" if item.get("priority_source") == "user" else "inferred"
    if item.get("priority_source") == "user":
        updates.pop("priority_level", None)
        updates.pop("priority_reason", None)
        updates.pop("priority_source", None)
    if item.get("sources"):
        store.retry_source(str(item["id"]))
    # Write the agent's contextual title last so link metadata parsing cannot
    # overwrite the user-relevant summary.
    return store.update_item(str(item["id"]), updates, instruction="agent-refresh")


def _memos_fresh_command(ctx, raw_args: str) -> str:
    """Refresh one item by the current active-list number through the LLM."""
    try:
        store, module = get_store()
        text = raw_args.strip()
        if not text or not text.isdigit() or int(text) < 1:
            return "用法：/memos_fresh NUM（NUM 为当前 /memos 列表序号）"
        result = store.list_items(("active",), create_snapshot=True)
        index = int(text) - 1
        items = result.get("items", [])
        if index >= len(items):
            return f"当前活动备忘录只有 {len(items)} 项，找不到第 {text} 项。"
        refreshed = _llm_refresh_item(ctx, store, items[index])
        return module.render_markdown_list([refreshed], heading="已刷新备忘录")
    except Exception as exc:
        return f"无法刷新备忘录：{exc}"


def _memos_fresh_all_command(ctx, raw_args: str) -> str:
    del raw_args
    try:
        store, module = get_store()
        items = store.list_items(tuple(module.ITEM_STATUSES), create_snapshot=False).get("items", [])
        failures = []
        for item in items:
            try:
                _llm_refresh_item(ctx, store, item)
            except Exception as exc:
                failures.append(f"{item.get('id')}: {exc}")
        if failures:
            return f"已尝试刷新 {len(items)} 条，失败 {len(failures)} 条：\n" + "\n".join(failures)
        return f"已通过 agent 刷新全部 {len(items)} 条备忘录。"
    except Exception as exc:
        return f"无法刷新备忘录：{exc}"


def _numbered_item(store, module, raw: str, *, statuses=("active",)):
    if not raw.strip().isdigit() or int(raw.strip()) < 1:
        raise ValueError("请输入当前 /memos 列表中的正整数序号")
    result = store.list_items(statuses, create_snapshot=True)
    index = int(raw.strip()) - 1
    items = result.get("items", [])
    if index >= len(items):
        raise ValueError(f"当前列表只有 {len(items)} 项")
    return items[index]


_ADD_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"}, "item_type": {"type": "string", "enum": ["task", "note", "link", "article", "video", "reference"]},
        "urls": {"type": "array", "items": {"type": "string"}},
        "due_at": {"type": ["string", "null"]}, "due_precision": {"type": ["string", "null"], "enum": ["date", "datetime", "uncertain", None]}, "due_raw_text": {"type": ["string", "null"]},
        "remind_at": {"type": ["string", "null"]}, "remind_precision": {"type": ["string", "null"], "enum": ["date", "datetime", "uncertain", None]},
        "scheduled_for": {"type": ["string", "null"]}, "scheduled_precision": {"type": ["string", "null"], "enum": ["date", "datetime", "uncertain", None]},
        "defer_until": {"type": ["string", "null"]}, "defer_precision": {"type": ["string", "null"], "enum": ["date", "datetime", "uncertain", None]},
        "priority_level": {"type": "string", "enum": ["urgent", "high", "normal", "low"]}, "priority_reason": {"type": ["string", "null"]}, "time_uncertain": {"type": "boolean"},
    },
    "required": ["title", "item_type", "urls", "due_at", "due_precision", "due_raw_text", "remind_at", "remind_precision", "scheduled_for", "scheduled_precision", "defer_until", "defer_precision", "priority_level", "priority_reason", "time_uncertain"],
}


def _memos_add_command(ctx, raw_args: str) -> str:
    text = raw_args.strip()
    if not text:
        return "用法：/memos_add 内容"
    try:
        store, module = get_store()
        result = ctx.llm.complete_structured(
            instructions="解析用户要保存的备忘录。生成简短摘要 title；保留用户原意；提取 URL、类型、时间和优先级。无法确定的时间返回 null，不要编造。只返回 JSON。",
            input=[{"type": "text", "text": text}], json_schema=_ADD_SCHEMA,
            schema_name="personal_memo.add", purpose="personal-memo-add", temperature=0.0, max_tokens=700,
        )
        if result.parsed is None:
            return f"新增解析失败：{result.text}"
        data = dict(result.parsed)
        item = store.add_item(title=data["title"], content=text, item_type=data["item_type"], urls=data.get("urls") or (),
                              due_at=data.get("due_at"), due_precision=data.get("due_precision"), due_raw_text=data.get("due_raw_text"),
                              remind_at=data.get("remind_at"), remind_precision=data.get("remind_precision"), scheduled_for=data.get("scheduled_for"),
                              scheduled_precision=data.get("scheduled_precision"), defer_until=data.get("defer_until"), defer_precision=data.get("defer_precision"),
                              priority_level=data.get("priority_level", "normal"), priority_reason=data.get("priority_reason"), time_uncertain=bool(data.get("time_uncertain")))
        if item.get("sources"):
            item = store.retry_source(item["id"])
        return module.render_human(item, "show")
    except Exception as exc:
        return f"无法新增备忘录：{exc}"


def _memos_detail_command(raw_args: str) -> str:
    try:
        store, module = get_store(); item = _numbered_item(store, module, raw_args)
        return module.render_human(item, "show")
    except Exception as exc:
        return f"无法查看备忘录：{exc}"


def _memos_transition_command(raw_args: str, action: str) -> str:
    try:
        store, module = get_store(); item = _numbered_item(store, module, raw_args)
        result = getattr(store, action)(item["id"], instruction=f"/memos_{action}")
        return module.render_human(result, "show")
    except Exception as exc:
        return f"无法执行操作：{exc}"


def _memos_search_command(raw_args: str) -> str:
    query = raw_args.strip()
    if not query: return "用法：/memos_search 关键词"
    try:
        store, module = get_store(); return module.render_human(store.search(query), "search")
    except Exception as exc: return f"无法搜索备忘录：{exc}"


def _memos_today_command(raw_args: str) -> str:
    del raw_args
    try:
        store, module = get_store(); return module.render_human(store.today(), "today")
    except Exception as exc: return f"无法读取今日事项：{exc}"


def _memos_edit_command(ctx, raw_args: str) -> str:
    parts = raw_args.strip().split(maxsplit=1)
    if len(parts) < 2: return "用法：/memos_edit NUM 修改要求"
    try:
        store, module = get_store(); item = _numbered_item(store, module, parts[0])
        result = ctx.llm.complete_structured(
            instructions="根据现有备忘录和用户修改要求，返回完整的新字段 JSON。保留未要求修改的字段；不要修改原始 content，除非用户明确要求修改内容。只返回 JSON。",
            input=[{"type": "text", "text": f"现有备忘录：{item}\n修改要求：{parts[1]}"}], json_schema=_REFRESH_SCHEMA,
            schema_name="personal_memo.edit", purpose="personal-memo-edit", temperature=0.0, max_tokens=700,
        )
        if result.parsed is None: return f"修改解析失败：{result.text}"
        updates = {key: result.parsed.get(key) for key in _REFRESH_SCHEMA["properties"]}
        if item.get("priority_source") == "user": updates.pop("priority_level", None); updates.pop("priority_reason", None)
        updated = store.update_item(item["id"], updates, instruction=parts[1])
        return module.render_human(updated, "show")
    except Exception as exc: return f"无法修改备忘录：{exc}"


def register(ctx):
    """Register normal memo tools and an opt-in administration toolset."""
    for schema, handler in (
        (schemas.MEMO_ADD, tools.memo_add),
        (schemas.MEMO_CAPTURE, tools.memo_capture),
        (schemas.MEMO_LIST, tools.memo_list),
        (schemas.MEMO_TABLE, tools.memo_table),
        (schemas.MEMO_SHOW, tools.memo_show),
        (schemas.MEMO_SEARCH, tools.memo_search),
        (schemas.MEMO_TODAY, tools.memo_today),
        (schemas.MEMO_ACTIVITY, tools.memo_activity),
        (schemas.MEMO_UPDATE, tools.memo_update),
        (schemas.MEMO_TRANSITION, tools.memo_transition),
        (schemas.MEMO_HISTORY, tools.memo_history),
        (schemas.MEMO_SOURCE, tools.memo_source),
        (schemas.MEMO_REMINDER, tools.memo_reminder),
    ):
        ctx.register_tool(
            name=schema["name"],
            toolset="personal_memo",
            schema=schema,
            handler=handler,
            description=schema["description"],
        )
    ctx.register_tool(
        name=schemas.MEMO_ADMIN["name"],
        toolset="personal_memo_admin",
        schema=schemas.MEMO_ADMIN,
        handler=tools.memo_admin,
        description=schemas.MEMO_ADMIN["description"],
    )
    register_command = getattr(ctx, "register_command", None)
    if not callable(register_command):
        logger.warning("personal-memo cannot register /memos: this Hermes plugin context has no register_command API")
        return
    try:
        register_command(
            "memos",
            handler=_memos_command,
            description="Display personal memos as a readable list. Add 'all' to include completed and archived items.",
        )
        register_command(
            "memos-fresh",
            handler=lambda raw: _memos_fresh_command(ctx, raw),
            description="Refresh one memo by its current /memos list number.",
        )
        register_command(
            "memos-fresh-all",
            handler=lambda raw: _memos_fresh_all_command(ctx, raw),
            description="Refresh all memo metadata while preserving original content and business state.",
        )
        register_command("memos-add", handler=lambda raw: _memos_add_command(ctx, raw), description="Add a memo through agent extraction.")
        register_command("memos-detail", handler=_memos_detail_command, description="Show one memo by current list number.")
        register_command("memos-done", handler=lambda raw: _memos_transition_command(raw, "complete"), description="Complete one memo by current list number.")
        register_command("memos-delete", handler=lambda raw: _memos_transition_command(raw, "delete"), description="Soft-delete one memo by current list number.")
        register_command("memos-edit", handler=lambda raw: _memos_edit_command(ctx, raw), description="Edit one memo through agent extraction.")
        register_command("memos-search", handler=_memos_search_command, description="Search memos.")
        register_command("memos-today", handler=_memos_today_command, description="Show today's memo items.")
        logger.info("personal-memo registered /memos")
    except Exception:
        logger.exception("personal-memo failed to register /memos")
