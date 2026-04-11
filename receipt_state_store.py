from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from typing import Any

import aiosqlite

from config import RECEIPT_STATE_BACKEND, RECEIPT_STATE_SQLITE_PATH, REDIS_URL
from receipt_state_keys import MODE_LONG, MODE_SINGLE, RECEIPT_STATE_KEYS


def _snapshot(user_data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in RECEIPT_STATE_KEYS:
        if k in user_data:
            out[k] = user_data[k]
    return out


def _should_persist(state: dict[str, Any]) -> bool:
    if not state:
        return False
    if state.get("receipt_file_ids"):
        return True
    if state.get("receipt_conflict_file_id") or state.get("receipt_conflict_from_menu"):
        return True
    if state.get("receipt_mode") in (MODE_SINGLE, MODE_LONG):
        return True
    return False


def clear_receipt_keys(user_data: dict[str, Any]) -> None:
    for k in RECEIPT_STATE_KEYS:
        user_data.pop(k, None)


class ReceiptStateStore(ABC):
    @abstractmethod
    async def load(self, user_id: int) -> dict[str, Any]:
        """Пустой dict, если записи нет."""

    @abstractmethod
    async def save(self, user_id: int, state: dict[str, Any]) -> None:
        """Полная замена состояния пользователя."""

    @abstractmethod
    async def delete(self, user_id: int) -> None:
        """Удалить сохранённое состояние."""

    async def close(self) -> None:
        """Освободить соединения (Redis и т.п.)."""
        return None


class SQLiteReceiptStateStore(ReceiptStateStore):
    def __init__(self, db_path: str) -> None:
        self._path = db_path

    async def _ensure_schema(self, db: aiosqlite.Connection) -> None:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS receipt_state (
                user_id INTEGER PRIMARY KEY NOT NULL,
                payload TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        await db.commit()

    async def load(self, user_id: int) -> dict[str, Any]:
        async with aiosqlite.connect(self._path) as db:
            await self._ensure_schema(db)
            async with db.execute(
                "SELECT payload FROM receipt_state WHERE user_id = ?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
            if not row or not row[0]:
                return {}
            try:
                data = json.loads(row[0])
            except json.JSONDecodeError:
                return {}
            if not isinstance(data, dict):
                return {}
            return {k: data[k] for k in RECEIPT_STATE_KEYS if k in data}

    async def save(self, user_id: int, state: dict[str, Any]) -> None:
        payload = json.dumps(state, ensure_ascii=False)
        now = time.time()
        async with aiosqlite.connect(self._path) as db:
            await self._ensure_schema(db)
            await db.execute(
                """
                INSERT INTO receipt_state (user_id, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (user_id, payload, now),
            )
            await db.commit()

    async def delete(self, user_id: int) -> None:
        async with aiosqlite.connect(self._path) as db:
            await self._ensure_schema(db)
            await db.execute("DELETE FROM receipt_state WHERE user_id = ?", (user_id,))
            await db.commit()


class RedisReceiptStateStore(ReceiptStateStore):
    _KEY_PREFIX = "chekbot:receipt_state:"

    def __init__(self, url: str) -> None:
        import redis.asyncio as redis  # type: ignore[import-untyped]

        self._redis = redis.from_url(url, decode_responses=True)

    def _key(self, user_id: int) -> str:
        return f"{self._KEY_PREFIX}{user_id}"

    async def load(self, user_id: int) -> dict[str, Any]:
        raw = await self._redis.get(self._key(user_id))
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: data[k] for k in RECEIPT_STATE_KEYS if k in data}

    async def save(self, user_id: int, state: dict[str, Any]) -> None:
        await self._redis.set(self._key(user_id), json.dumps(state, ensure_ascii=False))

    async def delete(self, user_id: int) -> None:
        await self._redis.delete(self._key(user_id))

    async def close(self) -> None:
        await self._redis.aclose()


def create_receipt_state_store() -> ReceiptStateStore:
    backend = (RECEIPT_STATE_BACKEND or "sqlite").strip().lower()
    if backend == "redis":
        if not REDIS_URL:
            raise RuntimeError(
                "RECEIPT_STATE_BACKEND=redis, но REDIS_URL не задан в .env"
            )
        return RedisReceiptStateStore(REDIS_URL)
    if backend != "sqlite":
        raise RuntimeError(
            f"Неизвестный RECEIPT_STATE_BACKEND={backend!r}. Используйте sqlite или redis."
        )
    return SQLiteReceiptStateStore(RECEIPT_STATE_SQLITE_PATH)


async def receipt_sync_in(context: Any, store: ReceiptStateStore) -> None:
    user = getattr(context, "user_id", None)
    if user is None:
        return
    clear_receipt_keys(context.user_data)
    data = await store.load(user)
    for k, v in data.items():
        context.user_data[k] = v


async def receipt_sync_out(context: Any, store: ReceiptStateStore) -> None:
    user = getattr(context, "user_id", None)
    if user is None:
        return
    snap = _snapshot(context.user_data)
    if _should_persist(snap):
        await store.save(user, snap)
    else:
        await store.delete(user)
