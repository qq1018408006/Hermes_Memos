"""Backup operations exposed independently of Hermes and MCP adapters."""

from .service import MemoStore


def create_backup(store: MemoStore) -> dict:
    return store.manual_backup()


def restore_backup(store: MemoStore, backup: str, *, confirm: str) -> dict:
    return store.restore_backup(backup, confirm=confirm)


__all__ = ("create_backup", "restore_backup")
