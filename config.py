import os
from dotenv import load_dotenv


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _project_path(path: str) -> str:
    """Путь относительно каталога проекта, если не абсолютный (CWD не важен)."""
    expanded = os.path.expanduser(path.strip())
    if os.path.isabs(expanded):
        return os.path.normpath(expanded)
    return os.path.normpath(os.path.join(BASE_DIR, expanded))


load_dotenv(os.path.join(BASE_DIR, ".env"))


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GOOGLE_SHEETS_SPREADSHEET_ID = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "")
GOOGLE_SHEETS_SHEET_NAME = os.getenv("GOOGLE_SHEETS_SHEET_NAME", "Sheet1")
GOOGLE_SHEETS_ITEMS_SHEET_NAME = os.getenv("GOOGLE_SHEETS_ITEMS_SHEET_NAME", "Товар")
GOOGLE_SERVICE_ACCOUNT_FILE = _project_path(
    os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials.json")
)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Поддерживаем оба варианта именования:
# - OPENAI_VISION_MODEL (предпочтительно для vision-модели)
# - OPENAI_MODEL (общий alias, если хотите переопределить явно)
_OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", _OPENAI_VISION_MODEL or "gpt-4o-mini")

SYSTEM_PROMPT_PATH = os.path.join(BASE_DIR, "system_prompt.txt")

# Персистентность состояния загрузки чека: sqlite (файл) или redis
RECEIPT_STATE_BACKEND = os.getenv("RECEIPT_STATE_BACKEND", "sqlite").strip().lower()
_receipt_sqlite_env = os.getenv("RECEIPT_STATE_SQLITE_PATH", "").strip()
RECEIPT_STATE_SQLITE_PATH = (
    _project_path(_receipt_sqlite_env)
    if _receipt_sqlite_env
    else os.path.join(BASE_DIR, "receipt_state.db")
)
REDIS_URL = os.getenv("REDIS_URL", "").strip()

# Предобработка фото перед vision API: EXIF-поворот, лимит размера, обрезка полей
_PREPROC = os.getenv("RECEIPT_PREPROCESS_ENABLED", "1").strip().lower()
RECEIPT_PREPROCESS_ENABLED = _PREPROC not in ("0", "false", "no", "off")
# Для чеков с длинными названиями полезно иметь больше пикселей,
# чтобы LLM стабильнее считывала `цена*количество` и правую колонку.
RECEIPT_PREPROCESS_MAX_SIDE = int(os.getenv("RECEIPT_PREPROCESS_MAX_SIDE", "4500"))

# Обрезка по контурам/полям (эвристика). Если у вас LLM часто путает строки/колонки,
# лучше отключить обрезку и оставить только EXIF-поворот и resize.
_CROP = os.getenv("RECEIPT_PREPROCESS_CROP_ENABLED", "0").strip().lower()
RECEIPT_PREPROCESS_CROP_ENABLED = _CROP not in ("0", "false", "no", "off")

# Чтение фискального QR (QReader при наличии, иначе OpenCV) перед vision API
_RECEIPT_QR = os.getenv("RECEIPT_QR_ENABLED", "1").strip().lower()
RECEIPT_QR_ENABLED = _RECEIPT_QR not in ("0", "false", "no", "off")
