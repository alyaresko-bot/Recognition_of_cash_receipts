"""Ключи состояния загрузки чека (единый список для бота и персистентности)."""

from __future__ import annotations

from typing import Final

UD_MODE: Final = "receipt_mode"
UD_FILE_IDS: Final = "receipt_file_ids"
UD_AWAIT_LAST: Final = "receipt_awaiting_last_confirm"
UD_EXPECT_MORE: Final = "receipt_expect_more_after_no"
UD_SESSION: Final = "receipt_session_id"
UD_STARTED_TS: Final = "receipt_started_ts"
UD_CONFLICT_FILE: Final = "receipt_conflict_file_id"
UD_CONFLICT_MENU: Final = "receipt_conflict_from_menu"
UD_MENU_ACTION: Final = "receipt_pending_menu_action"
UD_EXTRA: Final = "receipt_caption_context"

MODE_SINGLE: Final = "single"
MODE_LONG: Final = "long"

RECEIPT_STATE_KEYS: Final[frozenset[str]] = frozenset(
    {
        UD_MODE,
        UD_FILE_IDS,
        UD_AWAIT_LAST,
        UD_EXPECT_MORE,
        UD_SESSION,
        UD_STARTED_TS,
        UD_CONFLICT_FILE,
        UD_CONFLICT_MENU,
        UD_MENU_ACTION,
        UD_EXTRA,
    }
)
