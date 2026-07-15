"""Hermes handlers for personal-memo. All handlers return JSON strings."""

from __future__ import annotations

import json
from typing import Any

from .store import get_store


_SCOPE = ("platform", "user_id", "chat_id", "topic_id")


def _scope(args: dict[str, Any], *, session: bool = False, topic: bool = True) -> dict[str, Any]:
    result = {key: args.get(key) for key in _SCOPE if topic or key != "topic_id"}
    if session:
        result["session_id"] = args.get("session_id")
    return result


def _result(callback):
    try:
        return json.dumps({"ok": True, "result": callback()}, ensure_ascii=False, sort_keys=True)
    except Exception as exc:  # Hermes handlers must not crash the tool loop.
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True)


def memo_add(args: dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    def run():
        store, _ = get_store()
        return store.add_item(
            title=args.get("title"), content=args.get("content", ""), item_type=args.get("item_type", "task"),
            urls=args.get("urls", ()), due_at=args.get("due_at"), due_precision=args.get("due_precision"),
            due_raw_text=args.get("due_raw_text"), remind_at=args.get("remind_at"), remind_precision=args.get("remind_precision"),
            scheduled_for=args.get("scheduled_for"), scheduled_precision=args.get("scheduled_precision"),
            defer_until=args.get("defer_until"), defer_precision=args.get("defer_precision"), timezone=args.get("timezone"),
            priority_level=args.get("priority_level", "normal"), priority_source=args.get("priority_source", "inferred"),
            priority_reason=args.get("priority_reason"), capture_source=args.get("capture_source"),
            tags=args.get("tags", ()), time_uncertain=bool(args.get("time_uncertain", False)),
            suggested_action=args.get("suggested_action"), action_source=args.get("action_source"),
            allow_duplicate=bool(args.get("allow_duplicate", False)), instruction=args.get("instruction"),
            idempotency_key=args.get("idempotency_key"), **_scope(args, session=True, topic=False),
        )
    return _result(run)


def memo_capture(args: dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    return _result(lambda: get_store()[0].capture(
        args["text"], chat_type=args.get("chat_type", "unknown"), explicit=bool(args.get("explicit", False)),
        redact=bool(args.get("redact_sensitive", False)), idempotency_key=args.get("idempotency_key"), **_scope(args, session=True, topic=False),
    ))


def memo_list(args: dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    def run():
        store, module = get_store()
        statuses = tuple(module.ITEM_STATUSES) if args.get("all") else tuple(args.get("statuses") or ("active",))
        return store.list_items(statuses, create_snapshot=args.get("create_snapshot", True), view_kind=args.get("view_kind", "active"), limit=args.get("limit"), **_scope(args))
    return _result(run)


def memo_table(args: dict[str, Any], **kwargs: Any) -> str:
    """Return only the core-rendered table for agents that need fixed output."""
    del kwargs
    def run():
        store, module = get_store()
        statuses = tuple(module.ITEM_STATUSES) if args.get("all") else tuple(args.get("statuses") or ("active",))
        result = store.list_items(
            statuses,
            create_snapshot=True,
            view_kind="table",
            limit=args.get("limit"),
            **_scope(args),
        )
        return {"count": result["count"], "snapshot_id": result["snapshot_id"], "markdown": result["display_markdown"]}
    return _result(run)


def memo_show(args: dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    return _result(lambda: get_store()[0].show(args["reference"], **_scope(args)))


def memo_search(args: dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    return _result(lambda: get_store()[0].search(args["query"], limit=args.get("limit", 20), statuses=args.get("statuses")))


def memo_today(args: dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    return _result(lambda: get_store()[0].today(**_scope(args)))


def memo_activity(args: dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    return _result(lambda: get_store()[0].activity(args["kind"], since=args.get("since"), until=args.get("until"), days=args.get("days"), **_scope(args)))


def memo_update(args: dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    return _result(lambda: get_store()[0].update_item(
        args["reference"], dict(args["updates"]), clear_fields=args.get("clear_fields", ()), append_note=args.get("append_note"),
        instruction=args.get("instruction"), idempotency_key=args.get("idempotency_key"), **_scope(args, session=True),
    ))


def memo_transition(args: dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    def run():
        store, _ = get_store()
        action = args["action"]
        reference = args.get("reference")
        context = {"instruction": args.get("instruction"), "idempotency_key": args.get("idempotency_key"), **_scope(args, session=True)}
        if action == "undo":
            return store.undo(reference, **context)
        if not reference:
            raise ValueError(f"{action} requires reference")
        handlers = {"complete": store.complete, "delete": store.delete, "archive": store.archive, "restore": store.restore}
        return handlers[action](reference, **context)
    return _result(run)


def memo_history(args: dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    return _result(lambda: get_store()[0].history(
        args.get("reference"), limit=args.get("limit", 100), since=args.get("since"), operation=args.get("operation"), **_scope(args),
    ))


def memo_source(args: dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    def run():
        store, _ = get_store()
        action = args["action"]
        if action == "retry":
            return store.retry_source(args["reference"])
        if action == "update":
            return store.update_source(args["source_id"], dict(args["metadata"]), instruction=args.get("instruction"), idempotency_key=args.get("idempotency_key"))
        if action == "link":
            return store.link_source(args["source_id"], args["reference"], instruction=args.get("instruction"), idempotency_key=args.get("idempotency_key"), **_scope(args, session=True))
        raise ValueError(f"Unsupported source action: {action}")
    return _result(run)


def memo_reminder(args: dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    return _result(lambda: get_store()[0].reminder(
        args.get("mode", "manual"), delivery_target=args.get("delivery_target"), delivery_status=args.get("delivery_status"),
        is_test=bool(args.get("test", False)), **_scope(args),
    ))


def memo_admin(args: dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    def run():
        store, _ = get_store()
        action = args["action"]
        if action == "backup": return store.manual_backup()
        if action == "export": return store.export_markdown()
        if action == "validate": return store.validate()
        if action == "doctor": return store.doctor()
        if action == "settings_list": return store.settings()
        if action == "settings_get": return {"key": args["key"], "value": store.get_setting(args["key"])}
        if action == "settings_set": return store.set_setting(args["key"], args["value"])
        if action == "timezone_migrate": return store.migrate_timezone(args["timezone"])
        if action == "restore_backup": return store.restore_backup(args["backup"], confirm=args["confirm"])
        if action == "purge": return store.purge(args["reference"], confirm=args["confirm"], instruction=args.get("instruction"), **_scope(args, session=True))
        if action == "dispatch_reminders": return store.dispatch_reminders(
            delivery_target=args.get("delivery_target"), run_id=args.get("run_id"), delivery_status=args.get("delivery_status"),
            error_message=args.get("error_message"), now=args.get("now"), is_test=bool(args.get("test", False)), **_scope(args),
        )
        raise ValueError(f"Unsupported admin action: {action}")
    return _result(run)
