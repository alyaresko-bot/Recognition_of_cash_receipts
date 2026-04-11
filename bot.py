from __future__ import annotations

import asyncio
import functools
import logging
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import TELEGRAM_BOT_TOKEN
from google_sheets import append_receipt_row
from ocr import analyze_receipt_image
from receipt_state_keys import (
    MODE_LONG,
    MODE_SINGLE,
    UD_AWAIT_LAST,
    UD_CONFLICT_FILE,
    UD_CONFLICT_MENU,
    UD_EXPECT_MORE,
    UD_EXTRA,
    UD_FILE_IDS,
    UD_MENU_ACTION,
    UD_MODE,
    UD_SESSION,
    UD_STARTED_TS,
)
from receipt_state_store import (
    ReceiptStateStore,
    clear_receipt_keys,
    create_receipt_state_store,
    receipt_sync_in,
    receipt_sync_out,
)


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN: Final[str] = TELEGRAM_BOT_TOKEN

CB_LAST_YES = "rc_last_yes"
CB_LAST_NO = "rc_last_no"
CB_CONT = "rc_cont"
CB_NEW = "rc_new"
CB_MENU_CONT = "rc_menu_cont"
CB_MENU_NEW = "rc_menu_new"

REMINDER_JOB_PREFIX = "rc_reminder_"

MSK = timezone(timedelta(hours=3))

BTN_SINGLE = "🧾 Один чек (одно фото)"
BTN_LONG = "📜 Длинный чек"
BTN_DONE = "✅ Загрузка завершена"
BTN_HELP = "❓ Помощь"

# Рекомендация при съёмке чека по частям (один и тот же текст в инструкциях)
TEXT_MULTI_FRAME_HINT = (
    "Если чек не помещается в один кадр, фотографируйте его строго по частям сверху вниз. "
    "Каждый новый снимок должен начинаться со следующей новой позиции без повторения строк с предыдущего фото."
)


def _reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_SINGLE)],
            [KeyboardButton(BTN_LONG)],
            [KeyboardButton(BTN_DONE)],
            [KeyboardButton(BTN_HELP)],
        ],
        resize_keyboard=True,
    )


def _format_started_at(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=MSK)
    return dt.strftime("%d.%m.%Y %H:%M (МСК)")


def has_pending_receipt(ud: dict[str, Any]) -> bool:
    return bool(ud.get(UD_FILE_IDS))


def _store(context: ContextTypes.DEFAULT_TYPE) -> ReceiptStateStore:
    return context.application.bot_data["receipt_state_store"]


def with_persisted_receipt_state(
    handler: Any,
) -> Any:
    @functools.wraps(handler)
    async def wrapped(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        store = _store(context)
        await receipt_sync_in(context, store)
        try:
            await handler(update, context)
        finally:
            await receipt_sync_out(context, store)

    return wrapped


def cancel_reminder_jobs(application: object, user_id: int) -> None:
    jq = application.job_queue
    if not jq:
        return
    name = f"{REMINDER_JOB_PREFIX}{user_id}"
    for job in jq.get_jobs_by_name(name):
        job.schedule_removal()


def schedule_incomplete_reminder(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    jq = context.application.job_queue
    if not jq or not update.effective_chat:
        return
    uid = update.effective_user.id if update.effective_user else None
    if uid is None:
        return
    cancel_reminder_jobs(context.application, uid)
    session_id = context.user_data.get(UD_SESSION)
    if not session_id:
        return
    chat_id = update.effective_chat.id

    async def _fire(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        store: ReceiptStateStore = ctx.application.bot_data["receipt_state_store"]
        ud = await store.load(uid)
        if ud.get(UD_SESSION) != session_id:
            return
        if not has_pending_receipt(ud):
            return
        mode = ud.get(UD_MODE)
        lines = [
            "Напоминание: вы не завершили загрузку чека.",
        ]
        if mode == MODE_LONG:
            lines.append(
                "Нажмите «✅ Загрузка завершена» в меню или напишите: Загрузка завершена."
            )
        elif ud.get(UD_AWAIT_LAST):
            lines.append(
                "Ответьте на вопрос «Это крайнее фото чека?» кнопками под сообщением."
            )
        else:
            lines.append(
                "Отправьте следующее фото фрагмента или завершите загрузку текстом "
                "«Загрузка завершена», если все части уже отправлены."
            )
        try:
            await ctx.bot.send_message(chat_id=chat_id, text="\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось отправить напоминание: %s", exc)

    jq.run_once(
        _fire,
        when=timedelta(minutes=5),
        chat_id=chat_id,
        user_id=uid,
        name=f"{REMINDER_JOB_PREFIX}{uid}",
    )


def matches_upload_done(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    if "загрузка завершена" in t:
        return True
    return t == BTN_DONE.lower() or t.endswith("загрузка завершена")


def is_menu_single(text: str) -> bool:
    t = (text or "").strip()
    return t == BTN_SINGLE or "один чек" in t.lower()


def is_menu_long(text: str) -> bool:
    t = (text or "").strip()
    return t == BTN_LONG or ("длинный" in t.lower() and "чек" in t.lower())


def is_menu_help(text: str) -> bool:
    t = (text or "").strip()
    return t == BTN_HELP or t.lower() in ("/help", "помощь")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "Привет! Выберите режим загрузки чека кнопками внизу.\n\n"
        "• **Один чек** — если чек помещается на одно фото (после каждого фото спрошу, "
        "крайнее ли оно).\n"
        "• **Длинный чек** — если чек на несколько фото: отправляйте части по порядку, "
        "затем нажмите **Загрузка завершена** или напишите это фразой.\n\n"
        f"{TEXT_MULTI_FRAME_HINT}\n\n"
        "Можно просто прислать фото без меню — тогда используется режим «один чек».",
        reply_markup=_reply_keyboard(),
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "ОБЩЕЕ ПРАВИЛО: фотографируйте чек так, чтобы было видно все позиции ЧЕТКО, не более 10 позиций на фото!!!\n"
        "Кнопки меню:\n"
        f"• {BTN_SINGLE} — один снимок или несколько с подтверждением «крайнее фото».\n"
        f"• {BTN_LONG} — много фото подряд, в конце — {BTN_DONE} или текст «Загрузка завершена».\n"
        f"{TEXT_MULTI_FRAME_HINT}\n"
        "Если через 5 минут нет завершения, бот пришлёт напоминание.\n"
        "При незавершённой загрузке новое фото может запросить выбор: продолжить старый чек или начать новый.",
        reply_markup=_reply_keyboard(),
    )


def _inline_last_photo() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Да, крайнее", callback_data=CB_LAST_YES),
                InlineKeyboardButton("Нет, ещё фото", callback_data=CB_LAST_NO),
            ]
        ]
    )


def _inline_conflict() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Продолжить чек", callback_data=CB_CONT)],
            [InlineKeyboardButton("Начать новый", callback_data=CB_NEW)],
        ]
    )


def _inline_menu_conflict() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Продолжить текущий", callback_data=CB_MENU_CONT)],
            [InlineKeyboardButton("Начать новый (старый удалится)", callback_data=CB_MENU_NEW)],
        ]
    )


async def _send_unfinished_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    started_ts: float,
) -> None:
    text = (
        f"Я вижу, у вас остался незавершённый чек от {_format_started_at(started_ts)}.\n"
        "Вы хотите продолжить загрузку этого чека или начать новый? "
        "Если начнёте новый — текущий незавершённый набор фото будет удалён."
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=_inline_conflict())
    elif update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(
            text, reply_markup=_inline_conflict()
        )


async def _send_menu_conflict(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    started_ts: float,
    action_label: str,
) -> None:
    text = (
        f"Сначала завершите незавершённый чек от {_format_started_at(started_ts)} "
        f"или отмените его.\nВы нажали: {action_label}.\n"
        "Продолжить текущую загрузку или начать новую (текущая будет удалена)?"
    )
    context.user_data[UD_CONFLICT_MENU] = True
    if update.message:
        await update.message.reply_text(text, reply_markup=_inline_menu_conflict())


async def process_buffered_receipt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_ids: list[str],
    user_comment: Optional[str],
    skip_intro: bool = False,
) -> None:
    chat = update.effective_chat
    if not chat:
        return
    if update.effective_user:
        cancel_reminder_jobs(context.application, update.effective_user.id)
    if not skip_intro:
        await context.bot.send_message(
            chat_id=chat.id,
            text="Загрузка принята, распознаю чек с помощью GPT…",
        )
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths: list[str] = []
            for i, fid in enumerate(file_ids):
                f = await context.bot.get_file(fid)
                p = os.path.join(tmpdir, f"receipt_{i}.jpg")
                await f.download_to_drive(p)
                paths.append(p)
            receipt_data = analyze_receipt_image(
                image_paths=paths,
                extra_context=user_comment,
            )
            meta = receipt_data.get("_meta") or {}
            if meta.get("needs_review"):
                diff = meta.get("sum_check_diff")
                items_count = None
                receipt_total = None
                items_total = None
                math_error_count = None
                parsed_items = receipt_data.get("items") or []
                if isinstance(parsed_items, list):
                    items_count = len(parsed_items)
                    # Простейший разбор чисел (дубликат логики из google_sheets.py, чтобы не зависеть от него)
                    def _to_number(value: Any) -> Optional[float]:
                        if value is None:
                            return None
                        if isinstance(value, (int, float)):
                            return float(value)
                        if not isinstance(value, str):
                            return None
                        txt = value.strip()
                        if not txt:
                            return None
                        for ch in ["₽", "р", "RUB", "руб", "руб.", "руб"]:
                            txt = txt.replace(ch, "")
                        txt = txt.replace(",", ".").strip()
                        try:
                            return float(txt)
                        except ValueError:
                            return None

                    receipt_info = receipt_data.get("receipt_info") or {}
                    receipt_total = _to_number(receipt_info.get("total_amount"))
                    items_gross_sum = 0.0
                    items_discount_sum = 0.0
                    any_total = False
                    for item in parsed_items:
                        if not isinstance(item, dict):
                            continue
                        num = _to_number(item.get("item_total"))
                        if num is None:
                            continue
                        any_total = True
                        items_gross_sum += num
                        disc = _to_number(item.get("item_discount"))
                        if disc is not None:
                            items_discount_sum += disc
                    items_total = items_gross_sum if any_total else None
                    items_net_total = (
                        (items_gross_sum - items_discount_sum) if any_total else None
                    )
                if diff is not None:
                    await context.bot.send_message(
                        chat_id=chat.id,
                        text=(
                            "Внимание: похоже, чек распознан не полностью или есть ошибки в позициях. "
                            f"Расхождение суммы по позициям и итогу: {diff:.2f}. "
                            "Проверьте данные перед дальнейшими действиями."
                        ),
                    )
                else:
                    await context.bot.send_message(
                        chat_id=chat.id,
                        text=(
                            "Внимание: похоже, чек распознан не полностью или есть ошибки в позициях. "
                            "Проверьте данные перед дальнейшими действиями."
                        ),
                    )

                # Короткий debug-блок, чтобы можно было понять, какие позиции потерялись
                # (и затем точечно поправить промпт/дедупликацию/обрезку).
                try:
                    names = []
                    if isinstance(parsed_items, list):
                        for i, it in enumerate(parsed_items, start=1):
                            if not isinstance(it, dict):
                                continue
                            name = it.get("name") or ""
                            qty = it.get("quantity") or ""
                            total = it.get("item_total") or ""
                            names.append(f"{i}) {name} | qty={qty} | total={total}")
                            if len(names) >= 12:
                                names.append("…")
                                break
                    debug_lines = [
                        "DEBUG по позиции/суммам:",
                        f"items_count={items_count}",
                        f"receipt_total={receipt_total}",
                        f"items_gross_total={items_total}",
                        f"items_discount_total={items_discount_sum if any_total else None}",
                        f"items_net_total={items_net_total}",
                        f"attempts_used={meta.get('attempts_used')}",
                        f"math_error_count={meta.get('math_error_count')}",
                        f"unparsed_items={meta.get('unparsed_items')}",
                        f"unparsed_discounts={meta.get('unparsed_discounts')}",
                        "items:",
                        *names,
                    ]
                    await context.bot.send_message(
                        chat_id=chat.id,
                        text="\n".join(debug_lines),
                    )
                except Exception:  # noqa: BLE001
                    pass
            res = append_receipt_row(receipt_data)
            if isinstance(res, dict) and res.get("skipped"):
                if res.get("warning_updated"):
                    warn_text = (res.get("validation_comment") or "").strip()
                    if warn_text:
                        await context.bot.send_message(
                            chat_id=chat.id,
                            text=(
                                "Чек уже присутствует в таблице (защита от дублей), "
                                "но предупреждение по валидации суммы обновлено в существующей строке:\n"
                                f"{warn_text}"
                            ),
                        )
                    else:
                        await context.bot.send_message(
                            chat_id=chat.id,
                            text=(
                                "Чек уже присутствует в таблице (защита от дублей), "
                                "но предупреждение по валидации обновлено в существующей строке."
                            ),
                        )
                    return
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=(
                        "Чек уже присутствует в таблице (защита от дублей). "
                        "Повторная запись выполнена не будет."
                    ),
                )
                return
        await context.bot.send_message(
            chat_id=chat.id,
            text="Чек обработан и данные отправлены в гугл-таблицу.",
        )
        if isinstance(res, dict):
            validation_status = (res.get("validation_status") or "").strip().lower()
            if validation_status and validation_status != "ok":
                validation_comment = (res.get("validation_comment") or "").strip()
                if validation_comment:
                    await context.bot.send_message(
                        chat_id=chat.id,
                        text=(
                            "Предупреждение: обнаружено несоответствие/риск в данных чека.\n"
                            f"{validation_comment}"
                        ),
                    )
                else:
                    await context.bot.send_message(
                        chat_id=chat.id,
                        text=(
                            "Предупреждение: обнаружено несоответствие/риск в данных чека. "
                            "Проверьте суммы и позиции в таблице."
                        ),
                    )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка обработки чека: %s", exc)
        await context.bot.send_message(
            chat_id=chat.id,
            text="Произошла ошибка при обработке чека. Проверь настройки и логи сервера.",
        )


async def handle_upload_done(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message:
        return
    ud = context.user_data
    if ud.get(UD_CONFLICT_FILE) or ud.get(UD_CONFLICT_MENU):
        await update.message.reply_text(
            "Сначала ответьте на вопрос с кнопками выше (продолжить или начать новый)."
        )
        return
    mode = ud.get(UD_MODE)
    ids = list(ud.get(UD_FILE_IDS) or [])
    if not ids:
        await update.message.reply_text(
            "Нет фото для обработки. Сначала отправьте хотя бы одно изображение чека."
        )
        return
    if mode == MODE_SINGLE and ud.get(UD_AWAIT_LAST):
        await update.message.reply_text(
            "Сначала ответьте на вопрос «Это крайнее фото чека?» кнопками под предыдущим сообщением."
        )
        return
    msg_text = update.message.text or ""
    text_comment = None if matches_upload_done(msg_text) else msg_text
    caption_extra = ud.get(UD_EXTRA)
    parts = [x for x in (caption_extra, text_comment) if x]
    comment = "\n".join(parts) if parts else None
    if update.effective_user:
        cancel_reminder_jobs(context.application, update.effective_user.id)
    clear_receipt_keys(ud)
    await process_buffered_receipt(update, context, ids, comment)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    text = update.message.text
    if update.message.entities and any(e.type == "bot_command" for e in update.message.entities):
        return

    ud = context.user_data
    if ud.get(UD_CONFLICT_FILE) or ud.get(UD_CONFLICT_MENU):
        await update.message.reply_text(
            "Сначала выберите вариант кнопками в сообщении выше."
        )
        return

    if matches_upload_done(text):
        await handle_upload_done(update, context)
        return

    if is_menu_help(text):
        await help_command(update, context)
        return

    if is_menu_long(text):
        if has_pending_receipt(ud):
            st = float(ud.get(UD_STARTED_TS) or 0)
            await _send_menu_conflict(update, context, st, BTN_LONG)
            context.user_data[UD_MENU_ACTION] = MODE_LONG
            return
        clear_receipt_keys(ud)
        ud[UD_MODE] = MODE_LONG
        await update.message.reply_text(
            "Режим **длинного чека**.\nОтправляйте фото частей чека сверху вниз по порядку. "
            f"{TEXT_MULTI_FRAME_HINT} "
            "Когда все части отправлены — нажмите «✅ Загрузка завершена» "
            "или напишите: **Загрузка завершена**.",
            parse_mode="Markdown",
            reply_markup=_reply_keyboard(),
        )
        return

    if is_menu_single(text):
        if has_pending_receipt(ud):
            st = float(ud.get(UD_STARTED_TS) or 0)
            await _send_menu_conflict(update, context, st, BTN_SINGLE)
            context.user_data[UD_MENU_ACTION] = MODE_SINGLE
            return
        clear_receipt_keys(ud)
        ud[UD_MODE] = MODE_SINGLE
        await update.message.reply_text(
            "Режим **одного чека**.\nОтправьте фото. После каждого снимка я спрошу, "
            "крайнее ли это фото чека. "
            f"{TEXT_MULTI_FRAME_HINT}",
            parse_mode="Markdown",
            reply_markup=_reply_keyboard(),
        )
        return

    if has_pending_receipt(ud) and ud.get(UD_MODE) == MODE_LONG:
        await update.message.reply_text(
            "Для завершения длинного чека нажмите «Загрузка завершена» в меню "
            "или напишите эту фразу отдельным сообщением."
        )
        return

    if has_pending_receipt(ud):
        await update.message.reply_text(
            "Сейчас жду продолжение загрузки: следующее фото чека, ответ на вопрос "
            "«крайнее фото?» кнопками или завершение длинного чека "
            "(кнопка / текст «Загрузка завершена»)."
        )
        return


async def _append_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
) -> None:
    ud = context.user_data
    ids = list(ud.get(UD_FILE_IDS) or [])
    ids.append(file_id)
    ud[UD_FILE_IDS] = ids
    if update.message and update.message.caption:
        ud[UD_EXTRA] = update.message.caption
    if not ud.get(UD_SESSION):
        ud[UD_SESSION] = uuid.uuid4().hex
    if not ud.get(UD_STARTED_TS):
        ud[UD_STARTED_TS] = datetime.now(tz=timezone.utc).timestamp()
    schedule_incomplete_reminder(update, context)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.photo:
        return
    photo = update.message.photo[-1]
    file_id = photo.file_id
    ud = context.user_data

    if ud.get(UD_CONFLICT_FILE):
        await update.message.reply_text(
            "Сначала ответьте кнопками: продолжить чек или начать новый."
        )
        return
    if ud.get(UD_CONFLICT_MENU):
        await update.message.reply_text(
            "Сначала ответьте кнопками в сообщении о конфликте режимов."
        )
        return

    mode = ud.get(UD_MODE) or MODE_SINGLE
    if not ud.get(UD_MODE):
        ud[UD_MODE] = MODE_SINGLE

    if mode == MODE_LONG:
        await _append_photo(update, context, file_id)
        n = len(ud[UD_FILE_IDS])
        await update.message.reply_text(
            f"Фото {n} принято. Когда закончите — «✅ Загрузка завершена» или текст «Загрузка завершена».",
            reply_markup=_reply_keyboard(),
        )
        return

    if ud.get(UD_AWAIT_LAST):
        ud[UD_CONFLICT_FILE] = file_id
        st = float(ud.get(UD_STARTED_TS) or 0)
        await _send_unfinished_prompt(update, context, st)
        return

    await _append_photo(update, context, file_id)
    ud[UD_AWAIT_LAST] = True
    ud[UD_EXPECT_MORE] = False
    await update.message.reply_text(
        "Это крайнее фото чека?",
        reply_markup=_inline_last_photo(),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.data:
        return
    await q.answer()
    ud = context.user_data
    data = q.data
    uid = q.from_user.id if q.from_user else None

    if data == CB_LAST_YES:
        ids = list(ud.get(UD_FILE_IDS) or [])
        if not ids:
            await q.edit_message_text("Нет сохранённых фото. Отправьте фото снова.")
            return
        if uid is not None:
            cancel_reminder_jobs(context.application, uid)
        caption_extra = ud.get(UD_EXTRA)
        clear_receipt_keys(ud)
        await q.edit_message_text("Фото принято, распознаю чек…")
        await process_buffered_receipt(
            update, context, ids, caption_extra, skip_intro=True
        )
        return

    if data == CB_LAST_NO:
        ud[UD_AWAIT_LAST] = False
        ud[UD_EXPECT_MORE] = True
        await q.edit_message_text(
            "Хорошо, пришлите следующее фото. После него снова спрошу, крайнее ли оно."
        )
        schedule_incomplete_reminder(update, context)
        return

    if data == CB_CONT:
        new_id = ud.pop(UD_CONFLICT_FILE, None)
        if not new_id:
            await q.edit_message_text("Нет данных для продолжения. Отправьте фото снова.")
            return
        ids = list(ud.get(UD_FILE_IDS) or [])
        ids.append(new_id)
        ud[UD_FILE_IDS] = ids
        ud[UD_AWAIT_LAST] = True
        ud[UD_EXPECT_MORE] = False
        await q.edit_message_text(
            "Фото добавлено к текущему чеку.\nЭто крайнее фото чека?",
            reply_markup=_inline_last_photo(),
        )
        schedule_incomplete_reminder(update, context)
        return

    if data == CB_NEW:
        new_id = ud.pop(UD_CONFLICT_FILE, None)
        if uid is not None:
            cancel_reminder_jobs(context.application, uid)
        clear_receipt_keys(ud)
        if new_id:
            ud[UD_MODE] = MODE_SINGLE
            ud[UD_FILE_IDS] = [new_id]
            ud[UD_SESSION] = uuid.uuid4().hex
            ud[UD_STARTED_TS] = datetime.now(tz=timezone.utc).timestamp()
            ud[UD_AWAIT_LAST] = True
            ud[UD_EXPECT_MORE] = False
            await q.edit_message_text(
                "Предыдущий незавершённый чек снят. Начинаем новый с присланного фото.\n"
                "Это крайнее фото чека?",
                reply_markup=_inline_last_photo(),
            )
            schedule_incomplete_reminder(update, context)
        else:
            await q.edit_message_text(
                "Текущий незавершённый чек удалён. Отправьте фото для нового чека."
            )
        return

    if data == CB_MENU_CONT:
        ud.pop(UD_CONFLICT_MENU, None)
        ud.pop(UD_MENU_ACTION, None)
        await q.edit_message_text("Продолжаем текущую загрузку. Пришлите фото или завершите, как раньше.")
        schedule_incomplete_reminder(update, context)
        return

    if data == CB_MENU_NEW:
        wanted = ud.pop(UD_MENU_ACTION, None)
        ud.pop(UD_CONFLICT_MENU, None)
        if uid is not None:
            cancel_reminder_jobs(context.application, uid)
        clear_receipt_keys(ud)
        await q.edit_message_text("Текущий чек сброшен.")
        if wanted == MODE_LONG:
            ud[UD_MODE] = MODE_LONG
            if q.message:
                await q.message.reply_text(
                    "Режим **длинного чека**. Отправляйте фото по порядку, затем — завершение загрузки. "
                    f"{TEXT_MULTI_FRAME_HINT}",
                    parse_mode="Markdown",
                    reply_markup=_reply_keyboard(),
                )
        elif wanted == MODE_SINGLE:
            ud[UD_MODE] = MODE_SINGLE
            if q.message:
                await q.message.reply_text(
                    "Режим **одного чека**. Отправьте фото. "
                    f"{TEXT_MULTI_FRAME_HINT}",
                    parse_mode="Markdown",
                    reply_markup=_reply_keyboard(),
                )
        return


async def _post_shutdown(app: Application) -> None:
    store = app.bot_data.get("receipt_state_store")
    if isinstance(store, ReceiptStateStore):
        await store.close()


def main() -> None:
    if not TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN не задан. Укажите его в файле .env или переменных окружения."
        )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    receipt_store = create_receipt_state_store()

    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.bot_data["receipt_state_store"] = receipt_store

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(with_persisted_receipt_state(on_callback)))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, with_persisted_receipt_state(handle_text))
    )
    application.add_handler(
        MessageHandler(filters.PHOTO & ~filters.COMMAND, with_persisted_receipt_state(handle_photo))
    )

    application.run_polling()


if __name__ == "__main__":
    main()
