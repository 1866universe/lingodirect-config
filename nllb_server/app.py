from flask import (
    Flask,
    request,
    jsonify,
    render_template_string,
    session,
    redirect,
    url_for,
    send_from_directory,
)
from ctranslate2 import Translator
from transformers import AutoTokenizer
from colorama import init, Fore, Style
from importlib.metadata import PackageNotFoundError, version
from logging.handlers import RotatingFileHandler
from contextlib import contextmanager
from pathlib import Path
from functools import wraps
import logging
import threading
import os
import time
import re
import uuid
import json
import msvcrt
import shutil
import tempfile
import subprocess


app = Flask(__name__)

# Secret key برای مدیریت امن Sessionهای پنل وب
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "lingodirect-internal-secure-key-2026")

# -------------------------------------------------------------------
# بارگذاری امن رمز عبور پنل مدیریت (متغیر محیطی یا فایل محلی .admin_secret)
# -------------------------------------------------------------------
def resolve_admin_password() -> str:
    password = os.environ.get("ADMIN_PASSWORD")
    if password and password.strip():
        return password.strip()

    secret_file_path = Path(__file__).resolve().parent / ".admin_secret"
    if secret_file_path.exists():
        try:
            with open(secret_file_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    return content
        except Exception as err:
            logging.error("Failed to read .admin_secret file: %s", err)

    raise RuntimeError(
        "CRITICAL: Admin password is not configured! "
        "Please set the ADMIN_PASSWORD environment variable or create "
        "a local '.admin_secret' file containing your password in the server directory."
    )

ADMIN_PASSWORD = resolve_admin_password()

init(strip=False, autoreset=False)

@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        os.path.join(app.root_path, "static"),
        "favicon.png",
        mimetype="image/png",
        max_age=3600,
    )


# -------------------------------------------------------------------
# Base directory configuration (Cross-platform & Relative)
# -------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent          # nllb_server directory
ROOT_DIR = BASE_DIR.parent                          # lingodirect-config root

# -------------------------------------------------------------------
# Logging configuration
# -------------------------------------------------------------------

LOG_DIR = Path(os.environ.get("LOG_DIR", ROOT_DIR / "logs"))
APP_LOG_PATH = LOG_DIR / "app.log"

os.makedirs(LOG_DIR, exist_ok=True)


class HealthAccessLogFilter(logging.Filter):
    """
    فقط access logهای مربوط به GET /health را عبور می‌دهد.
    """
    def filter(self, record):
        message = record.getMessage()
        return '"GET /health' in message


class NonHealthAccessLogFilter(logging.Filter):
    """
    همهٔ access logها را عبور می‌دهد به‌جز GET /health.
    """
    def filter(self, record):
        message = record.getMessage()
        return '"GET /health' not in message


# -------------------------------------------------------------------
# File handler
# فقط GET /health در app.log ذخیره می‌شود
# -------------------------------------------------------------------

health_file_handler = RotatingFileHandler(
    str(APP_LOG_PATH),
    maxBytes=5 * 1024 * 1024,  # 5 MB
    backupCount=5,
    encoding="utf-8",
    delay=True,
)

health_file_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

health_file_handler.setFormatter(health_file_formatter)
health_file_handler.addFilter(HealthAccessLogFilter())


# -------------------------------------------------------------------
# Console handler
# همهٔ access logها به‌جز GET /health در کنسول نمایش داده می‌شوند
# -------------------------------------------------------------------

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter("%(message)s"))
console_handler.addFilter(NonHealthAccessLogFilter())


# -------------------------------------------------------------------
# Werkzeug logger
# -------------------------------------------------------------------

werkzeug_logger = logging.getLogger("werkzeug")

# جلوگیری از Handlerهای قبلی و لاگ‌های تکراری
werkzeug_logger.handlers.clear()

werkzeug_logger.addHandler(console_handler)
werkzeug_logger.addHandler(health_file_handler)

werkzeug_logger.setLevel(logging.INFO)

# جلوگیری از ارسال مجدد لاگ به root logger
werkzeug_logger.propagate = False


MODEL_NAME = "facebook/nllb-200-1.3B"
MODEL_PATH = os.environ.get("NLLB_MODEL_PATH", str(BASE_DIR / "models" / "nllb-ct2-1.3b"))
DEVICE = os.environ.get("NLLB_DEVICE", "cpu")
COMPUTE_TYPE = os.environ.get("NLLB_COMPUTE_TYPE", "int8")
MAX_INPUT_CHARS = int(os.environ.get("MAX_INPUT_CHARS", "2000"))
BEAM_SIZE = int(os.environ.get("NLLB_BEAM_SIZE", "4"))
MAX_DECODING_LENGTH = int(os.environ.get("NLLB_MAX_DECODING_LENGTH", "128"))
SERVER_VERSION = "baseline-nllb-v7-diagnostic-logs"

CONFIG_FILE_PATH = Path(os.environ.get("CONFIG_PATH", ROOT_DIR / "config.json"))
CONFIG_PATH = str(CONFIG_FILE_PATH)

# مسیرهای ایمن‌سازی config.json
CONFIG_DIRECTORY = CONFIG_FILE_PATH.parent
CONFIG_LOCK_PATH = CONFIG_DIRECTORY / "config.json.lock"
CONFIG_BACKUP_DIRECTORY = CONFIG_DIRECTORY / "config_json_backups"

# حداکثر تعداد نسخه‌های پشتیبان config.json
CONFIG_BACKUP_KEEP_COUNT = 10

# حداکثر زمان انتظار برای قفل مشترک فایل
CONFIG_LOCK_TIMEOUT_SECONDS = 10

DEVICE_ID_MAX_LENGTH = 200
DEVICE_MODEL_MAX_LENGTH = 200

# حافظه زنده وضعیت سرور و تونل برای نمایش در پنل
LATEST_TUNNEL_STATE = {
    "status": "UNKNOWN",
    "public_url": None,
    "details": "No status received yet",
    "updated_at": "N/A",
    "attempt_id": None,
}


def env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default

    normalized_value = value.strip().lower()
    if normalized_value in {"1", "true", "yes", "on"}:
        return True
    if normalized_value in {"0", "false", "no", "off"}:
        return False

    raise ValueError(
        f"Invalid boolean value for {name}: {value!r}. "
        "Use true/false, 1/0, yes/no, or on/off."
    )


DEBUG_LOGS = env_flag("DEBUG_LOGS", False)
ENABLE_EXACT_TRANSLATIONS = env_flag("ENABLE_EXACT_TRANSLATIONS", True)
ENABLE_OUTPUT_NORMALIZATION = env_flag("ENABLE_OUTPUT_NORMALIZATION", True)
ENABLE_POSTPONEMENT_FIXES = env_flag("ENABLE_POSTPONEMENT_FIXES", True)
ENABLE_NEGATION_POLARITY_FIXES = env_flag("ENABLE_NEGATION_POLARITY_FIXES", True)
RUN_NLLB_FOR_EXACT_TRANSLATIONS = env_flag("RUN_NLLB_FOR_EXACT_TRANSLATIONS", False)
LANGUAGE_MAP = {
    "fa": "pes_Arab",
    "en": "eng_Latn",
    "ar": "arb_Arab",
    "fr": "fra_Latn",
    "de": "deu_Latn",
    "es": "spa_Latn",
    "tr": "tur_Latn",
    "ru": "rus_Cyrl",
    "hi": "hin_Deva",
    "ur": "urd_Arab",
    "pes_Arab": "pes_Arab",
    "eng_Latn": "eng_Latn",
    "arb_Arab": "arb_Arab",
    "fra_Latn": "fra_Latn",
    "deu_Latn": "deu_Latn",
    "spa_Latn": "spa_Latn",
    "tur_Latn": "tur_Latn",
    "rus_Cyrl": "rus_Cyrl",
    "hin_Deva": "hin_Deva",
    "urd_Arab": "urd_Arab",
}

PERSIAN_TO_EN_EXACT_TRANSLATION_MAP = {
    "سلام": "Hello",
    "سلام.": "Hello.",
    "سلام!": "Hello!",
    "سلام؟": "Hello?",
}

PERSIAN_OUTPUT_CHAR_MAP = {
    "ي": "ی",
    "ك": "ک",
    "ى": "ی",
    "ۀ": "هٔ",
}

ENGLISH_NEGATION_PATTERNS = (
    r"\bnot\b",
    r"\bnever\b",
    r"\bno\b",
    r"\bcannot\b",
    r"\bcan not\b",
    r"\bdid not\b",
    r"\bdo not\b",
    r"\bdoes not\b",
    r"\bwill not\b",
    r"\bwould not\b",
    r"\bcould not\b",
    r"\bshould not\b",
    r"\bis not\b",
    r"\bare not\b",
    r"\bwas not\b",
    r"\bwere not\b",
    r"\bhave not\b",
    r"\bhas not\b",
    r"\bhad not\b",
    r"\bwon't\b",
    r"\bcan't\b",
    r"\bcouldn't\b",
    r"\bshouldn't\b",
    r"\bwouldn't\b",
    r"\bdon't\b",
    r"\bdoesn't\b",
    r"\bdidn't\b",
    r"\bhaven't\b",
    r"\bhasn't\b",
    r"\bhadn't\b",
    r"\bisn't\b",
    r"\baren't\b",
    r"\bwasn't\b",
    r"\bweren't\b",
)

NEGATION_INTENT_PATTERNS = {
    "understand": (
        r"\bunderstand\b",
        r"\bunderstood\b",
        r"\bunderstanding\b",
        r"\bcatch\b",
        r"\bcaught\b",
        r"\bfollow\b",
        r"\bfollowed\b",
        r"\bget\b",
        r"\bgot\b",
    ),
    "know": (
        r"\bknow\b",
        r"\bknows\b",
        r"\bknew\b",
        r"\bknown\b",
    ),
}

translator = None
tokenizer = None
model_lock = threading.Lock()
config_lock = threading.RLock()
startup_error = None


def load_remote_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as config_file:
            data = json.load(config_file)
        if not isinstance(data, dict):
            return {}
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_access_control_config() -> dict:
    config = load_remote_config()
    app_management = config.get("app_management", {})
    access_control = app_management.get("access_control", {})
    if not isinstance(access_control, dict):
        return {}
    return access_control


@contextmanager
def config_file_lock(timeout_seconds: float = CONFIG_LOCK_TIMEOUT_SECONDS):
    """قفل بین‌پردازه‌ای برای config.json مشترک با tunnel_manager.py"""
    CONFIG_DIRECTORY.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_LOCK_PATH, "a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        deadline = time.monotonic() + timeout_seconds
        lock_acquired = False
        while time.monotonic() < deadline:
            try:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                lock_acquired = True
                break
            except OSError:
                time.sleep(0.1)

        if not lock_acquired:
            raise TimeoutError("Timed out while waiting for config.json shared lock")
        try:
            yield
        finally:
            try:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError as error:
                logging.warning("Could not release config.json shared lock: %s", error)


def create_config_backup() -> Path | None:
    if not CONFIG_FILE_PATH.exists():
        return None
    CONFIG_BACKUP_DIRECTORY.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    unique_suffix = uuid.uuid4().hex[:8]
    backup_path = CONFIG_BACKUP_DIRECTORY / f"config-{timestamp}-{unique_suffix}.json"
    shutil.copy2(CONFIG_FILE_PATH, backup_path)
    return backup_path


def cleanup_old_config_backups() -> None:
    if not CONFIG_BACKUP_DIRECTORY.exists():
        return
    backup_files = sorted(
        CONFIG_BACKUP_DIRECTORY.glob("config-*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for old_backup in backup_files[CONFIG_BACKUP_KEEP_COUNT:]:
        try:
            old_backup.unlink()
        except OSError as error:
            logging.warning("Could not delete old config backup %s: %s", old_backup, error)


def _save_remote_config_atomically(config: dict) -> bool:
    if not isinstance(config, dict):
        logging.error("Refusing to save config.json: config is not a dictionary.")
        return False

    temporary_path = None
    try:
        CONFIG_DIRECTORY.mkdir(parents=True, exist_ok=True)
        backup_path = create_config_backup()

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=CONFIG_DIRECTORY,
            prefix="config.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(config, temporary_file, ensure_ascii=False, indent=2)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        os.replace(temporary_path, CONFIG_FILE_PATH)
        temporary_path = None
        cleanup_old_config_backups()
        return True
    except (OSError, TypeError, ValueError) as error:
        logging.error("Failed to save config.json atomically: %s", error)
        return False
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def save_remote_config(config: dict, *, lock_held: bool = False) -> bool:
    if lock_held:
        return _save_remote_config_atomically(config)
    try:
        with config_file_lock():
            return _save_remote_config_atomically(config)
    except TimeoutError as error:
        logging.error("Could not save config.json: %s", error)
        return False


def normalize_access_control_lists(access_control: dict) -> tuple[list, list]:
    blocked_devices = access_control.get("blocked_devices")
    blocked_ids = access_control.get("blocked_ids")
    merged_blocked = []
    if isinstance(blocked_devices, list):
        merged_blocked.extend(blocked_devices)
    if isinstance(blocked_ids, list):
        for item in blocked_ids:
            if item not in merged_blocked:
                merged_blocked.append(item)

    registered_devices = access_control.get("registered_devices", [])
    if not isinstance(registered_devices, list):
        registered_devices = []
    return merged_blocked, registered_devices


def normalize_device_id(device_id) -> str:
    if device_id is None:
        raise ValueError("deviceId is required")
    if not isinstance(device_id, str):
        raise ValueError("deviceId must be a string")
    normalized = device_id.strip()
    if not normalized:
        raise ValueError("deviceId must not be empty")
    if len(normalized) > DEVICE_ID_MAX_LENGTH:
        raise ValueError(f"deviceId too long (max {DEVICE_ID_MAX_LENGTH} chars)")
    return normalized


def normalize_device_model(device_model) -> str:
    if device_model is None or not isinstance(device_model, str):
        return "Unknown Android device"
    normalized = device_model.strip()
    if not normalized:
        return "Unknown Android device"
    if len(normalized) > DEVICE_MODEL_MAX_LENGTH:
        normalized = normalized[:DEVICE_MODEL_MAX_LENGTH]
    return normalized


def normalize_device_language(language) -> str:
    if language is None or not isinstance(language, str):
        return "unknown"
    normalized = language.strip().lower()
    if normalized.startswith("fa"):
        return "fa"
    if normalized.startswith("en"):
        return "en"
    return normalized[:10] if normalized else "unknown"


def get_next_admin_label(registered_devices: list) -> str:
    highest_number = 0
    for item in registered_devices:
        if not isinstance(item, dict):
            continue
        admin_label = item.get("admin_label")
        if not isinstance(admin_label, str):
            continue
        match = re.fullmatch(r"user-(\d+)", admin_label.strip(), flags=re.IGNORECASE)
        if match:
            try:
                highest_number = max(highest_number, int(match.group(1)))
            except ValueError:
                continue
    return f"user-{highest_number + 1:03d}"


def create_registered_device_entry(
    device_id: str,
    device_model: str,
    language: str,
    registered_devices: list,
) -> dict:
    return {
        "device_id": device_id,
        "admin_label": get_next_admin_label(registered_devices),
        "device_model": normalize_device_model(device_model),
        "language": normalize_device_language(language),
        "status": "allowed",
    }


def is_device_blocked(device_id: str, access_control: dict) -> bool:
    blocked_devices, _ = normalize_access_control_lists(access_control)
    return device_id in blocked_devices


def is_device_registered(device_id: str, registered_devices: list) -> bool:
    for item in registered_devices:
        if isinstance(item, str) and item == device_id:
            return True
        if isinstance(item, dict) and item.get("device_id") == device_id:
            return True
    return False


def get_registered_device_count(registered_devices: list) -> int:
    unique_ids = set()
    for item in registered_devices:
        if isinstance(item, str):
            normalized = item.strip()
            if normalized:
                unique_ids.add(normalized)
        elif isinstance(item, dict):
            raw_device_id = item.get("device_id")
            if isinstance(raw_device_id, str):
                normalized = raw_device_id.strip()
                if normalized:
                    unique_ids.add(normalized)
    return len(unique_ids)


def ensure_access_control_structure(config: dict) -> tuple[dict, dict]:
    if not isinstance(config, dict):
        config = {}
    app_management = config.get("app_management")
    if not isinstance(app_management, dict):
        app_management = {}
        config["app_management"] = app_management
    access_control = app_management.get("access_control")
    if not isinstance(access_control, dict):
        access_control = {}
        app_management["access_control"] = access_control
    registered_devices = access_control.get("registered_devices")
    if not isinstance(registered_devices, list):
        access_control["registered_devices"] = []
    blocked_devices = access_control.get("blocked_devices")
    blocked_ids = access_control.get("blocked_ids")
    if not isinstance(blocked_devices, list) and not isinstance(blocked_ids, list):
        access_control["blocked_devices"] = []
    return config, access_control


def evaluate_device_access(
    device_id: str,
    device_model: str = "Unknown Android device",
    language: str = "unknown",
) -> tuple[bool, int, dict]:
    with config_lock:
        try:
            with config_file_lock():
                config = load_remote_config()
                config, access_control = ensure_access_control_structure(config)

                if is_device_blocked(device_id, access_control):
                    return False, 403, {
                        "allowed": False,
                        "reason": "blocked",
                        "message": "Device is blocked",
                    }

                registered_devices = access_control.get("registered_devices", [])

                if is_device_registered(device_id, registered_devices):
                    return True, 200, {
                        "allowed": True,
                        "reason": "registered",
                        "isNewRegistration": False,
                    }

                enforce_limit = access_control.get("enforce_limit", False)

                raw_max_users = access_control.get("max_users", 0)
                try:
                    max_users = int(raw_max_users)
                except (TypeError, ValueError):
                    logging.warning(
                        "Invalid max_users value in config.json: %r. "
                        "Treating it as 0.",
                        raw_max_users,
                    )
                    max_users = 0

                # مقدار منفی در config نباید باعث رفتار غیرمنتظره شود.
                max_users = max(0, max_users)

                if (
                    enforce_limit is True
                    and get_registered_device_count(registered_devices) >= max_users
                ):
                    return False, 403, {
                        "allowed": False,
                        "reason": "capacity_reached",
                        "message": "User limit reached",
                    }

                device_entry = create_registered_device_entry(
                    device_id=device_id,
                    device_model=device_model,
                    language=language,
                    registered_devices=registered_devices,
                )

                registered_devices.append(device_entry)

                saved = save_remote_config(config, lock_held=True)

                if not saved:
                    return False, 500, {
                        "allowed": False,
                        "reason": "config_save_failed",
                        "message": "Could not safely save device registration",
                    }

                return True, 200, {
                    "allowed": True,
                    "reason": "auto_registered",
                    "isNewRegistration": True,
                    "adminLabel": device_entry["admin_label"],
                }

        except TimeoutError:
            logging.error("Timed out while evaluating access for a device.")
            return False, 503, {
                "allowed": False,
                "reason": "config_lock_timeout",
                "message": "Server configuration is temporarily busy",
            }

        except Exception:
            logging.exception("Unexpected error while evaluating device access.")
            return False, 500, {
                "allowed": False,
                "reason": "internal_server_error",
                "message": "Could not evaluate device access",
            }


def package_version(package_name: str) -> str:
    try:
        return version(package_name)
    except PackageNotFoundError:
        return "unknown"


TRANSFORMERS_VERSION = package_version("transformers")
CTRANSLATE2_VERSION = package_version("ctranslate2")


def load_model():
    global translator, tokenizer, startup_error
    try:
        print(f"[SERVER VERSION] {SERVER_VERSION}")
        print(f"[APP FILE] {os.path.abspath(__file__)}")
        print(f"[MODEL CONFIG] name={MODEL_NAME!r} path={MODEL_PATH!r}")
        print(f"[MODEL CONFIG] device={DEVICE!r} computeType={COMPUTE_TYPE!r}")
        print(f"[DECODING CONFIG] beamSize={BEAM_SIZE} maxDecodingLength={MAX_DECODING_LENGTH}")
        print(f"[INFO] Loading tokenizer from: {MODEL_NAME}")
        tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
        print(f"[INFO] Loading CTranslate2 model from: {MODEL_PATH}")
        translator = Translator(
            MODEL_PATH,
            device=DEVICE,
            compute_type=COMPUTE_TYPE,
            intra_threads=2,
            inter_threads=1
        )
        print("[INFO] Model loaded successfully.")
    except Exception as e:
        startup_error = str(e)
        print(f"[ERROR] Failed to load model: {startup_error}")


def log_debug(*args):
    if DEBUG_LOGS:
        print(*args, flush=True)


def log_translation(request_id: str, message: str, *values):
    if not DEBUG_LOGS:
        return
    prefix = f"[TRANSLATE][{request_id}]"
    if values:
        print(prefix, message, *values, flush=True)
    else:
        print(prefix, message, flush=True)


def map_language(lang_code: str) -> str:
    if not lang_code:
        raise ValueError("Language code is missing")
    normalized = lang_code.strip()
    if "_" not in normalized:
        normalized = normalized.lower()
    if normalized not in LANGUAGE_MAP:
        raise ValueError(f"Unsupported language: {normalized}")
    return LANGUAGE_MAP[normalized]


def validate_text(text: str) -> str:
    if text is None or not isinstance(text, str):
        raise ValueError("text must be a string")
    text = text.strip()
    if not text:
        raise ValueError("text must not be empty")
    if len(text) > MAX_INPUT_CHARS:
        raise ValueError(f"text too long (max {MAX_INPUT_CHARS} chars)")
    return text


def validate_optional_edited_text(text) -> str | None:
    if text is None:
        return None
    if not isinstance(text, str):
        raise ValueError("editedText must be a string when provided")
    if len(text) > MAX_INPUT_CHARS:
        raise ValueError(f"editedText too long (max {MAX_INPUT_CHARS} chars)")
    return text


def normalize_common_spacing(text: str) -> str:
    text = re.sub(r"[ \t\u00A0\u2000-\u200A]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_persian_punctuation_spacing(text: str) -> str:
    text = re.sub(r"\s+([،؛؟!.])", r"\1", text)
    text = re.sub(r"([،؛؟!.])(?=\S)", r"\1 ", text)
    return text.strip()


def preprocess_text(text: str, source_lang: str, target_lang: str) -> str:
    return normalize_common_spacing(text)


def normalize_persian_output_spacing(text: str) -> str:
    for old_char, new_char in PERSIAN_OUTPUT_CHAR_MAP.items():
        text = text.replace(old_char, new_char)
    text = re.sub(r"[\u064B-\u0652\u0656]", "", text)
    text = normalize_common_spacing(text)
    text = normalize_persian_punctuation_spacing(text)
    text = re.sub(r"(?<!\S)نمی\s+(?=\S)", "نمی‌", text)
    text = re.sub(r"(?<!\S)می\s+(?=\S)", "می‌", text)
    return text.strip()


def normalize_english_for_negation_checks(text: str) -> str:
    text = text.lower()
    text = text.replace("’", "'").replace("‘", "'")
    return normalize_common_spacing(text)


def source_has_strong_negation(text: str) -> bool:
    normalized_text = normalize_english_for_negation_checks(text)
    return any(re.search(pattern, normalized_text) for pattern in ENGLISH_NEGATION_PATTERNS)


def detect_negation_intents(text: str) -> set[str]:
    normalized_text = normalize_english_for_negation_checks(text)
    detected = set()
    for intent_name, patterns in NEGATION_INTENT_PATTERNS.items():
        if any(re.search(pattern, normalized_text) for pattern in patterns):
            detected.add(intent_name)
    return detected


def persian_text_has_negation(text: str) -> bool:
    negation_patterns = (
        r"نمی[‌ ]?\S+", r"نمي[‌ ]?\S+", r"\bنه\b", r"\bنیست\b", r"\bنيست\b",
        r"\bنبود\b", r"\bنباش", r"\bنشد\b", r"\bنشدم\b", r"\bنشدی\b",
        r"\bنشدیم\b", r"\bنشدند\b", r"\bنکرد\b", r"\bنکردم\b", r"\bنکردی\b",
        r"\bنکردیم\b", r"\bنکردند\b", r"\bنفهم", r"\bندان", r"\bندون",
        r"متوجه نشد", r"درک نکرد",
    )
    return any(re.search(pattern, text) for pattern in negation_patterns)


def apply_pattern_replacements(text: str, replacements: list[tuple[str, str]]) -> str:
    updated_text = text
    for pattern, replacement in replacements:
        new_text = re.sub(pattern, replacement, updated_text)
        if new_text != updated_text:
            return new_text
    return updated_text


def fix_negation_polarity_flips(
    translated_text: str,
    source_text: str,
    source_lang: str,
    target_lang: str,
) -> str:
    if not ENABLE_NEGATION_POLARITY_FIXES:
        return translated_text
    normalized_source_lang = source_lang.strip().lower() if source_lang else ""
    normalized_target_lang = target_lang.strip().lower() if target_lang else ""
    if normalized_source_lang != "en" or normalized_target_lang != "fa":
        return translated_text
    if not source_text or not translated_text:
        return translated_text
    if not source_has_strong_negation(source_text):
        return translated_text
    detected_intents = detect_negation_intents(source_text)
    if not detected_intents:
        return translated_text
    if persian_text_has_negation(translated_text):
        return translated_text

    fixed_text = translated_text
    if "understand" in detected_intents:
        understand_replacements = [
            (r"(?<!ن)چیزی که گفتی رو فهمیدم", "چیزی که گفتی رو نفهمیدم"),
            (r"(?<!ن)چیزی که گفتی را فهمیدم", "چیزی که گفتی را نفهمیدم"),
            (r"(?<!ن)حرفت رو فهمیدم", "حرفت رو نفهمیدم"),
            (r"(?<!ن)حرفت را فهمیدم", "حرفت را نفهمیدم"),
            (r"(?<!ن)من فهمیدم", "من نفهمیدم"),
            (r"(?<!ن)فهمیدم", "نفهمیدم"),
            (r"(?<!ن)متوجه شدم", "متوجه نشدم"),
            (r"(?<!ن)درک کردم", "درک نکردم"),
            (r"(?<!ن)می‌فهمم", "نمی‌فهمم"),
            (r"(?<!ن)میفهمم", "نمی‌فهمم"),
            (r"(?<!ن)می فهمم", "نمی‌فهمم"),
        ]
        fixed_text = apply_pattern_replacements(fixed_text, understand_replacements)

    if "know" in detected_intents and not persian_text_has_negation(fixed_text):
        know_replacements = [
            (r"(?<!ن)من می‌دانم", "من نمی‌دانم"),
            (r"(?<!ن)من میدانم", "من نمی‌دانم"),
            (r"(?<!ن)من می دانم", "من نمی‌دانم"),
            (r"(?<!ن)می‌دانم", "نمی‌دانم"),
            (r"(?<!ن)میدانم", "نمی‌دانم"),
            (r"(?<!ن)می دانم", "نمی‌دانم"),
            (r"(?<!ن)می‌دونم", "نمی‌دونم"),
            (r"(?<!ن)میدونم", "نمی‌دونم"),
            (r"(?<!ن)می دونم", "نمی‌دونم"),
        ]
        fixed_text = apply_pattern_replacements(fixed_text, know_replacements)

    return fixed_text


def get_exact_direct_translation(text: str, source_lang: str, target_lang: str) -> str | None:
    if not ENABLE_EXACT_TRANSLATIONS:
        return None
    normalized_source = source_lang.strip().lower() if source_lang else ""
    normalized_target = target_lang.strip().lower() if target_lang else ""
    if normalized_source == "fa" and normalized_target == "en":
        return PERSIAN_TO_EN_EXACT_TRANSLATION_MAP.get(text)
    return None


def should_apply_postponement_fixes(source_text: str) -> bool:
    if not source_text:
        return False
    source_text = source_text.strip().lower()
    return any(phrase in source_text for phrase in ("postponed", "put off"))


def fix_persian_postponement_phrases(text: str) -> str:
    replacements = {
        "تعویض شد": "به تعویق افتاد", "تأخیر شد": "به تعویق افتاد",
        "تاخیر شد": "به تعویق افتاد", "تأجيل شد": "به تعویق افتاد",
        "تأجیل شد": "به تعویق افتاد", "تعويق شد": "به تعویق افتاد",
        "تعویق شد": "به تعویق افتاد",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(
        r"(?P<head>\S(?:.*\S)?)\s+به\s+"
        r"(?P<span>(?!مدت\b|خاطر\b|دلیل\b|علت\b|واسطه\b|سبب\b)"
        r"[^.،؛!?]+?)\s+به تعویق افتاد([.؟!]?)$",
        r"\g<head> \g<span> به تعویق افتاد\3",
        text,
    )
    return text.strip()


def postprocess_translation(text: str, source_lang: str, target_lang: str, source_text: str = "") -> tuple[str, list[str]]:
    source_lang = source_lang.strip().lower() if source_lang else ""
    target_lang = target_lang.strip().lower() if target_lang else ""
    applied_stages = []

    if not ENABLE_OUTPUT_NORMALIZATION:
        stripped_text = text.strip()
        if stripped_text != text:
            applied_stages.append("outer-whitespace-strip")
        return stripped_text, applied_stages

    if target_lang == "fa":
        before_normalize = text
        text = normalize_persian_output_spacing(text)
        if text != before_normalize:
            applied_stages.append("persian-output-normalization")

        if ENABLE_POSTPONEMENT_FIXES and source_lang == "en" and should_apply_postponement_fixes(source_text):
            before_fix = text
            text = fix_persian_postponement_phrases(text)
            text = normalize_persian_output_spacing(text)
            if text != before_fix:
                applied_stages.append("postponement-fix")

        if ENABLE_NEGATION_POLARITY_FIXES and source_lang == "en":
            before_negation_fix = text
            text = fix_negation_polarity_flips(text, source_text, source_lang, target_lang)
            text = normalize_persian_output_spacing(text)
            if text != before_negation_fix:
                applied_stages.append("negation-polarity-fix")

        return text, applied_stages

    before_normalize = text
    text = normalize_common_spacing(text)
    if text != before_normalize:
        applied_stages.append("common-output-spacing")
    return text, applied_stages


def translate_text(text: str, source_lang: str, target_lang: str) -> str:
    if translator is None or tokenizer is None:
        raise RuntimeError("Model is not loaded")

    src_lang = map_language(source_lang)
    tgt_lang = map_language(target_lang)

    with model_lock:
        tokenizer.src_lang = src_lang
        input_ids = tokenizer.encode(text)
        input_tokens = tokenizer.convert_ids_to_tokens(input_ids)
        results = translator.translate_batch(
            [input_tokens],
            target_prefix=[[tgt_lang]],
            beam_size=BEAM_SIZE,
            max_decoding_length=MAX_DECODING_LENGTH,
            repetition_penalty=1.1,
            max_batch_size=1,
        )
        output_tokens = results[0].hypotheses[0]
        output_ids = tokenizer.convert_tokens_to_ids(output_tokens)
        translated = tokenizer.decode(output_ids, skip_special_tokens=True)

    return translated.strip()


def pipeline_flags() -> dict:
    return {
        "exactTranslations": ENABLE_EXACT_TRANSLATIONS,
        "outputNormalization": ENABLE_OUTPUT_NORMALIZATION,
        "postponementFixes": ENABLE_POSTPONEMENT_FIXES,
        "negationPolarityFixes": ENABLE_NEGATION_POLARITY_FIXES,
        "diagnosticNllbForExact": RUN_NLLB_FOR_EXACT_TRANSLATIONS,
    }


def get_client_ip() -> str:
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.headers.get("X-Real-IP", request.remote_addr)


@app.before_request
def log_incoming_request():
    user_agent = request.headers.get("User-Agent", "")
    if "Tunnel-Monitor" in user_agent or request.path in ["/health", "/tunnel_status"]:
        return


# -------------------------------------------------------------------
# Core APIs
# -------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    loaded = translator is not None and tokenizer is not None
    return jsonify({
        "status": "ok" if loaded and startup_error is None else "error",
        "modelLoaded": loaded,
        "device": DEVICE,
        "computeType": COMPUTE_TYPE,
        "modelPath": MODEL_PATH,
        "modelName": MODEL_NAME,
        "beamSize": BEAM_SIZE,
        "maxDecodingLength": MAX_DECODING_LENGTH,
        "transformersVersion": TRANSFORMERS_VERSION,
        "ctranslate2Version": CTRANSLATE2_VERSION,
        "pipelineFlags": pipeline_flags(),
        "startupError": startup_error,
        "serverVersion": SERVER_VERSION,
    })


@app.route("/check_access", methods=["POST"])
def check_access():
    request_id = uuid.uuid4().hex[:8]
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("Invalid or missing JSON body")

        raw_device_id = data.get("deviceId") or data.get("device_id") or request.headers.get("X-Device-Id")
        device_id = normalize_device_id(raw_device_id)

        raw_device_model = data.get("deviceModel") or data.get("device_model") or request.headers.get("X-Device-Model")
        device_model = normalize_device_model(raw_device_model)

        safe_device_id = f"{device_id[:8]}...{device_id[-4:]}" if len(device_id) > 12 else device_id
        print(f"[ACCESS][{request_id}] DeviceId={safe_device_id} Model={device_model!r} IP={get_client_ip()}", flush=True)

        raw_lang = data.get("language") or data.get("device_language") or request.headers.get("X-Device-Language")
        lang = normalize_device_language(raw_lang)

        allowed, status_code, payload = evaluate_device_access(device_id, device_model, lang)
        payload["requestId"] = request_id
        response = jsonify(payload)
        response.headers["X-Request-ID"] = request_id
        return response, status_code

    except Exception as e:
        print(f"\033[91m[ERROR][{request_id}] {str(e)}\033[0m", flush=True)
        return jsonify({"allowed": False, "error": str(e), "requestId": request_id}), 400


@app.route("/translate", methods=["POST"])
def translate():
    request_id = uuid.uuid4().hex[:8]
    started_at = time.perf_counter()

    user_agent = request.headers.get("User-Agent", "Unknown Device")
    device_match = re.search(r"Android\s+.*?; (.*?)\s+Build", user_agent)
    device_model = device_match.group(1) if device_match else "Android Device"

    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("Invalid or missing JSON body")

        raw_device_id = data.get("deviceId") or data.get("device_id") or request.headers.get("X-Device-Id")
        device_id = normalize_device_id(raw_device_id)
        safe_device_id = f"{device_id[:8]}...{device_id[-4:]}" if len(device_id) > 12 else device_id

        request_device_model = (
            data.get("deviceModel")
            or data.get("device_model")
            or request.headers.get("X-Device-Model")
            or device_model
        )
        normalized_device_model = normalize_device_model(request_device_model)

        raw_lang = data.get("language") or data.get("device_language") or request.headers.get("X-Device-Language")
        lang = normalize_device_language(raw_lang)

        allowed, status_code, access_payload = evaluate_device_access(
            device_id,
            normalized_device_model,
            lang,
        )

        if not allowed:
            access_payload["requestId"] = request_id
            return jsonify(access_payload), status_code

        received_text = data.get("text")
        source_language = data.get("sourceLanguage")
        target_language = data.get("targetLanguage")

        if not source_language or not target_language:
            raise ValueError("sourceLanguage and targetLanguage are required")

        source_language = source_language.strip()
        target_language = target_language.strip()

        map_language(source_language)
        map_language(target_language)
        text = validate_text(received_text)

        processed_text = preprocess_text(text, source_language, target_language)
        direct_translation = get_exact_direct_translation(processed_text, source_language, target_language)

        if direct_translation is not None:
            translated = direct_translation
            translation_engine = "exact-direct-map"
        else:
            engine_output = translate_text(processed_text, source_language, target_language)
            translation_engine = "nllb"
            translated, _ = postprocess_translation(
                engine_output, source_language, target_language, processed_text
            )

        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        client_ip = get_client_ip()

        print(
            f"{Fore.YELLOW}[TRANSLATE] "
            f"RequestId={request_id} "
            f"DeviceId={safe_device_id} "
            f"IP={client_ip} "
            f"Model='{device_model}' "
            f"Lang={source_language}->{target_language} "
            f"Time={elapsed_ms}ms "
            f"Engine={translation_engine}"
            f"{Style.RESET_ALL}",
            flush=True,
        )

        response_body = {
            "translatedText": translated,
            "sourceLanguage": source_language,
            "targetLanguage": target_language,
            "requestId": request_id,
            "translationEngine": translation_engine,
            "elapsedMs": elapsed_ms,
            "serverVersion": SERVER_VERSION,
        }

        response = jsonify(response_body)
        response.headers["X-Request-ID"] = request_id
        return response

    except Exception as e:
        print(f"\033[91m[ERROR][{request_id}] {str(e)}\033[0m", flush=True)
        return jsonify({"error": str(e), "requestId": request_id}), 400


@app.route("/tunnel_status", methods=["POST"])
def tunnel_status():
    global LATEST_TUNNEL_STATE
    data = request.get_json(silent=True) or {}
    attempt_id = data.get("attempt_id", "unknown")
    status = data.get("status", "unknown")
    details = data.get("details", "")
    public_url = data.get("public_url")
    now_time = time.strftime("%Y-%m-%d %H:%M:%S")

    LATEST_TUNNEL_STATE = {
        "status": status,
        "public_url": public_url,
        "details": details,
        "updated_at": now_time,
        "attempt_id": attempt_id,
    }

    GREEN = "\033[92m"
    RED = "\033[91m"
    RESET = "\033[0m"

    if status == "SUCCESS":
        print(
            f"{GREEN}[TRY-{attempt_id}] [TUNNEL_STATUS] [✔] SUCCESS: Tunnel established at {public_url or details}{RESET}",
            flush=True,
        )
    else:
        print(
            f"{RED}[TRY-{attempt_id}] [TUNNEL_STATUS] [❌] FAILED: {details}{RESET}",
            flush=True,
        )
    return jsonify({"status": "logged"})


# -------------------------------------------------------------------
# Web Management Panel (Authentication, Dashboard, APIs)
# -------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated_function


LOGIN_HTML = """
<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ورود به پنل مدیریت LingoDirect</title>

    <link rel="icon" type="image/png"
          href="{{ url_for('static', filename='favicon.png') }}?v=1">
    <link rel="apple-touch-icon"
          href="{{ url_for('static', filename='favicon.png') }}?v=1">

    <style>
        * { box-sizing: border-box; font-family: Tahoma, 'Segoe UI', sans-serif; }
        body { background: #0f172a; color: #f8fafc; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
        .card { background: #1e293b; padding: 2.5rem; border-radius: 12px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); width: 100%; max-width: 400px; }
        h2 { margin-top: 0; color: #38bdf8; text-align: center; font-size: 1.5rem; }
        .form-group { margin-bottom: 1.5rem; }
        label { display: block; margin-bottom: 0.5rem; font-size: 0.9rem; color: #94a3b8; }
        input[type="password"] { width: 100%; padding: 0.75rem; border-radius: 6px; border: 1px solid #334155; background: #0f172a; color: #fff; font-size: 1rem; outline: none; }
        input[type="password"]:focus { border-color: #38bdf8; }
        button { width: 100%; padding: 0.75rem; background: #0284c7; color: white; border: none; border-radius: 6px; font-size: 1rem; cursor: pointer; font-weight: bold; transition: background 0.2s; }
        button:hover { background: #0369a1; }
        .error { background: #ef444420; border: 1px solid #ef4444; color: #fca5a5; padding: 0.75rem; border-radius: 6px; margin-bottom: 1rem; text-align: center; font-size: 0.85rem; }
    </style>
</head>
<body>
    <div class="card">
        <h2>پنل مدیریت LingoDirect</h2>
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        <form method="POST">
            <div class="form-group">
                <label>رمز عبور مدیریت:</label>
                <input type="password" name="password" required autofocus autocomplete="current-password">
            </div>
            <button type="submit">ورود به داشبورد</button>
        </form>
    </div>
</body>
</html>
"""

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>داشبورد پنل مدیریت LingoDirect</title>

    <link rel="icon" type="image/png"
          href="{{ url_for('static', filename='favicon.png') }}?v=1">
    <link rel="apple-touch-icon"
          href="{{ url_for('static', filename='favicon.png') }}?v=1">

    <style>
        * { box-sizing: border-box; font-family: Tahoma, 'Segoe UI', sans-serif; }
        body { background: #0b0f19; color: #e2e8f0; margin: 0; padding: 20px; }
        .container { max-width: 1200px; margin: 0 auto; }
        .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #1e293b; padding-bottom: 15px; margin-bottom: 25px; }
        .header h1 { margin: 0; color: #38bdf8; font-size: 1.6rem; }
        .btn { padding: 8px 16px; border-radius: 6px; text-decoration: none; font-size: 0.85rem; cursor: pointer; border: none; font-weight: bold; transition: 0.2s; }
        .btn-logout { background: #ef4444; color: #fff; }
        .btn-logout:hover { background: #dc2626; }
        .btn-primary { background: #0284c7; color: #fff; }
        .btn-primary:hover { background: #0369a1; }
        .btn-danger { background: #e11d48; color: #fff; }
        .btn-danger:hover { background: #be123c; }
        .btn-success { background: #10b981; color: #fff; }
        .btn-success:hover { background: #059669; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 20px; margin-bottom: 25px; }
        .card { background: #131c31; border: 1px solid #1e293b; border-radius: 10px; padding: 20px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.3); }
        .card h3 { margin-top: 0; color: #94a3b8; font-size: 0.95rem; border-bottom: 1px solid #1e293b; padding-bottom: 10px; }
        .status-badge { display: inline-block; padding: 4px 10px; border-radius: 20px; font-size: 0.8rem; font-weight: bold; }
        .badge-success { background: #064e3b; color: #34d399; border: 1px solid #059669; }
        .badge-danger { background: #4c0519; color: #fb7185; border: 1px solid #e11d48; }
        .badge-warning { background: #451a03; color: #fbbf24; border: 1px solid #d97706; }
        .url-box { background: #0b0f19; padding: 8px; border-radius: 6px; border: 1px solid #1e293b; font-family: monospace; font-size: 0.85rem; color: #38bdf8; word-break: break-all; margin: 10px 0; direction: ltr; text-align: left; }
        table { width: 100%; border-collapse: collapse; margin-top: 15px; }
        th, td { padding: 12px; text-align: right; border-bottom: 1px solid #1e293b; font-size: 0.85rem; }
        th { background: #0f172a; color: #94a3b8; }
        tr:hover { background: #1e293b50; }
        .git-action-box { display: flex; gap: 10px; align-items: center; margin-top: 15px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>کنترل پنل مرکزی LingoDirect</h1>
            <div>
                <button onclick="publishToGit(this)" class="btn btn-success">🚀 انتشار تغییرات config.json در مخزن Git (Amend & Push)</button>
                <a href="{{ url_for('admin_logout') }}" class="btn btn-logout">خروج</a>
            </div>
        </div>

        <div class="grid">
            <!-- وضعیت سرور محلی و NLLB -->
            <div class="card">
                <h3>وضعیت سرور NLLB & Flask</h3>
                <p>مدل: <strong>{{ model_name }}</strong></p>
                <p>محیط پردازش: <strong>{{ device }} ({{ compute_type }})</strong></p>
                <p>وضعیت لود مدل: 
                    {% if model_loaded %}
                        <span class="status-badge badge-success">آماده به کار (Online)</span>
                    {% else %}
                        <span class="status-badge badge-danger">خطا در بارگذاری</span>
                    {% endif %}
                </p>
                <p>ورژن موتور: <span style="font-family: monospace; font-size: 0.8rem; color: #94a3b8;">{{ server_version }}</span></p>
            </div>

            <!-- وضعیت تونل -->
            <div class="card">
                <h3>وضعیت تونل پویا (localhost.run)</h3>
                <p>وضعیت تونل: 
                    {% if tunnel_state.status == 'SUCCESS' %}
                        <span class="status-badge badge-success">فعال و متصل (UP)</span>
                    {% elif tunnel_state.status == 'DOWN' %}
                        <span class="status-badge badge-danger">قطع شده (DOWN)</span>
                    {% else %}
                        <span class="status-badge badge-warning">{{ tunnel_state.status }}</span>
                    {% endif %}
                </p>
                <p>آدرس عمومی تونل:</p>
                <div class="url-box">{{ tunnel_state.public_url or base_url or 'هنوز ثبت نشده است' }}</div>
                <p style="font-size: 0.8rem; color: #64748b;">آخرین به‌روزرسانی: {{ tunnel_state.updated_at }}</p>
            </div>

            <!-- تنظیمات ظرفیت کاربران -->
            <div class="card">
                <h3>کنترل ظرفیت و دسترسی</h3>
                <form id="settingsForm" onsubmit="updateSettings(event)">
                    <p>کاربران ثبت‌شده: <strong>{{ registered_count }}</strong> از <strong>{{ max_users }}</strong></p>
                    <div style="margin-bottom: 10px;">
                        <label style="font-size: 0.85rem; color: #94a3b8;">سقف مجاز (max_users):</label>
                        <input type="number" id="max_users_input" value="{{ max_users }}" style="width: 80px; padding: 5px; border-radius: 4px; border: 1px solid #334155; background: #0b0f19; color: #fff;">
                    </div>
                    <div style="margin-bottom: 15px;">
                        <label style="font-size: 0.85rem; color: #94a3b8;">
                            <input type="checkbox" id="enforce_limit_input" {% if enforce_limit %}checked{% endif %}>
                            اعمال محدودیت سقف کاربر
                        </label>
                    </div>
                    <button type="submit" class="btn btn-primary">ذخیره تنظیمات ظرفیت</button>
                </form>
            </div>
        </div>

        <!-- جدول دستگاه‌ها -->
        <div class="card">
            <h3>دستگاه‌ها و کلاینت‌های ثبت‌شده ({{ registered_devices|length }})</h3>
            <div style="overflow-x: auto;">
                <table>
                    <thead>
                        <tr>
                            <th>شناسه کاربر (Label)</th>
                            <th>مدل دستگاه</th>
                            <th>زبان</th>
                            <th>شناسه دستگاه (Device ID)</th>
                            <th>وضعیت</th>
                            <th>عملیات</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for dev in registered_devices %}
                        {% set d_id = dev.device_id if dev is mapping else dev %}
                        {% set d_label = dev.admin_label if dev is mapping else 'N/A' %}
                        {% set d_model = dev.device_model if dev is mapping else 'Unknown' %}
                        {% set d_lang = dev.language if dev is mapping else 'unknown' %}
                        {% set is_blocked = d_id in blocked_devices %}
                        <tr>
                            <td><strong>{{ d_label }}</strong></td>
                            <td>{{ d_model }}</td>
                            <td><span style="direction: ltr; display: inline-block;">{{ d_lang }}</span></td>
                            <td style="font-family: monospace; font-size: 0.8rem; color: #94a3b8; direction: ltr; text-align: left;">{{ d_id }}</td>
                            <td>
                                {% if is_blocked %}
                                    <span class="status-badge badge-danger">مسدود (Blocked)</span>
                                {% else %}
                                    <span class="status-badge badge-success">مجاز (Allowed)</span>
                                {% endif %}
                            </td>
                            <td>
                                {% if is_blocked %}
                                    <button onclick="unblockDevice('{{ d_id }}')" class="btn btn-success" style="padding: 4px 8px; font-size: 0.75rem;">آزادسازی</button>
                                {% else %}
                                    <button onclick="blockDevice('{{ d_id }}')" class="btn btn-danger" style="padding: 4px 8px; font-size: 0.75rem;">مسدودسازی</button>
                                {% endif %}
                            </td>
                        </tr>
                        {% else %}
                        <tr>
                            <td colspan="6" style="text-align: center; color: #64748b;">هیچ دستگاهی ثبت نشده است.</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <script>
        async function blockDevice(deviceId) {
            if(!confirm('آیا از مسدودسازی این دستگاه اطمینان دارید؟')) return;
            const res = await fetch('/admin/api/device/block', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({deviceId: deviceId})
            });
            const data = await res.json();
            if(data.success) location.reload();
            else alert('خطا: ' + data.error);
        }

        async function unblockDevice(deviceId) {
            const res = await fetch('/admin/api/device/unblock', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({deviceId: deviceId})
            });
            const data = await res.json();
            if(data.success) location.reload();
            else alert('خطا: ' + data.error);
        }

        async function updateSettings(e) {
            e.preventDefault();
            const maxUsers = parseInt(document.getElementById('max_users_input').value);
            const enforceLimit = document.getElementById('enforce_limit_input').checked;
            const res = await fetch('/admin/api/settings', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({max_users: maxUsers, enforce_limit: enforceLimit})
            });
            const data = await res.json();
            if(data.success) {
                alert('تنظیمات با موفقیت ذخیره شد.');
                location.reload();
            } else {
                alert('خطا: ' + data.error);
            }
        }

        async function publishToGit(btn) {
            if (!confirm('آیا مایلید تغییرات config.json به صورت Amend و Push روی GitHub Pages منتشر شود؟')) {
                return;
            }

            btn.disabled = true;
            btn.innerText = 'در حال انتشار...';
            try {
                const res = await fetch('/admin/api/git/push', { method: 'POST' });
                const data = await res.json();
                if(data.success) {
                    alert('تغییرات با موفقیت روی گیت‌هاب اعمال شد.');
                } else {
                    alert('خطا در انتشار گیت:\\n' + (data.error || data.details));
                }
            } catch(err) {
                alert('خطای ارتباط با سرور: ' + err);
            } finally {
                btn.disabled = false;
                btn.innerText = '🚀 انتشار تغییرات config.json در مخزن Git (Amend & Push)';
            }
        }
    </script>
</body>
</html>
"""


@app.route("/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin_dashboard"))
        else:
            error = "رمز عبور وارد شده نادرست است."
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


@app.route("/")
@app.route("/admin")
@login_required
def admin_dashboard():
    config = load_remote_config()
    _, access_control = ensure_access_control_structure(config)
    blocked_devices, registered_devices = normalize_access_control_lists(access_control)
    
    server_info = config.get("server", {})
    base_url = config.get("base_url") or server_info.get("baseUrl")
    
    return render_template_string(
        DASHBOARD_HTML,
        model_name=MODEL_NAME,
        device=DEVICE,
        compute_type=COMPUTE_TYPE,
        model_loaded=(translator is not None and tokenizer is not None),
        server_version=SERVER_VERSION,
        tunnel_state=LATEST_TUNNEL_STATE,
        base_url=base_url,
        registered_devices=registered_devices,
        blocked_devices=blocked_devices,
        registered_count=get_registered_device_count(registered_devices),
        max_users=access_control.get("max_users", 10),
        enforce_limit=access_control.get("enforce_limit", True),
    )


@app.route("/admin/api/device/block", methods=["POST"])
@login_required
def api_block_device():
    data = request.get_json(silent=True) or {}

    try:
        device_id = normalize_device_id(data.get("deviceId"))
    except ValueError as error:
        return jsonify({
            "success": False,
            "error": str(error),
        }), 400

    with config_lock:
        try:
            with config_file_lock():
                config = load_remote_config()
                config, access_control = ensure_access_control_structure(config)

                blocked_devices, registered_devices = normalize_access_control_lists(
                    access_control
                )

                # منبع اصلی وضعیت Block.
                if device_id not in blocked_devices:
                    blocked_devices.append(device_id)

                access_control["blocked_devices"] = blocked_devices

                # همگام‌سازی وضعیت ذخیره‌شدهٔ دستگاه ثبت‌شده.
                for device in registered_devices:
                    if (
                        isinstance(device, dict)
                        and device.get("device_id") == device_id
                    ):
                        device["status"] = "blocked"
                        break

                access_control["registered_devices"] = registered_devices

                saved = save_remote_config(config, lock_held=True)

                if not saved:
                    return jsonify({
                        "success": False,
                        "error": "ذخیره‌سازی امن config.json ناموفق بود",
                    }), 500

                return jsonify({
                    "success": True,
                    "deviceId": device_id,
                    "status": "blocked",
                })

        except TimeoutError:
            return jsonify({
                "success": False,
                "error": "فایل تنظیمات موقتاً در حال استفاده است؛ دوباره تلاش کنید",
            }), 503

        except Exception as error:
            logging.exception("Unexpected error while blocking a device.")
            return jsonify({
                "success": False,
                "error": str(error),
            }), 500


@app.route("/admin/api/device/unblock", methods=["POST"])
@login_required
def api_unblock_device():
    data = request.get_json(silent=True) or {}

    try:
        device_id = normalize_device_id(data.get("deviceId"))
    except ValueError as error:
        return jsonify({
            "success": False,
            "error": str(error),
        }), 400

    with config_lock:
        try:
            with config_file_lock():
                config = load_remote_config()
                config, access_control = ensure_access_control_structure(config)

                blocked_devices, registered_devices = normalize_access_control_lists(
                    access_control
                )

                # حذف از منبع اصلی وضعیت Block.
                blocked_devices = [
                    blocked_id
                    for blocked_id in blocked_devices
                    if blocked_id != device_id
                ]

                access_control["blocked_devices"] = blocked_devices

                # همگام‌سازی وضعیت دستگاه ثبت‌شده.
                for device in registered_devices:
                    if (
                        isinstance(device, dict)
                        and device.get("device_id") == device_id
                    ):
                        device["status"] = "allowed"
                        break

                access_control["registered_devices"] = registered_devices

                saved = save_remote_config(config, lock_held=True)

                if not saved:
                    return jsonify({
                        "success": False,
                        "error": "ذخیره‌سازی امن config.json ناموفق بود",
                    }), 500

                return jsonify({
                    "success": True,
                    "deviceId": device_id,
                    "status": "allowed",
                })

        except TimeoutError:
            return jsonify({
                "success": False,
                "error": "فایل تنظیمات موقتاً در حال استفاده است؛ دوباره تلاش کنید",
            }), 503

        except Exception as error:
            logging.exception("Unexpected error while unblocking a device.")
            return jsonify({
                "success": False,
                "error": str(error),
            }), 500


@app.route("/admin/api/settings", methods=["POST"])
@login_required
def api_update_settings():
    data = request.get_json(silent=True) or {}

    if not isinstance(data, dict):
        return jsonify({
            "success": False,
            "error": "بدنهٔ درخواست JSON معتبر نیست",
        }), 400

    raw_max_users = data.get("max_users")
    raw_enforce_limit = data.get("enforce_limit")

    # اگر max_users ارسال شده باشد، باید عدد صحیح و غیرمنفی باشد.
    max_users = None
    if raw_max_users is not None:
        try:
            # bool نیز زیرمجموعهٔ int است؛ بنابراین صریحاً رد می‌شود.
            if isinstance(raw_max_users, bool):
                raise ValueError

            max_users = int(raw_max_users)
        except (TypeError, ValueError):
            return jsonify({
                "success": False,
                "error": "max_users باید یک عدد صحیح معتبر باشد",
            }), 400

        if max_users < 0:
            return jsonify({
                "success": False,
                "error": "max_users نمی‌تواند منفی باشد",
            }), 400

        if max_users > 100000:
            return jsonify({
                "success": False,
                "error": "max_users بیش از حد مجاز است",
            }), 400

    # فقط Boolean واقعی پذیرفته می‌شود.
    enforce_limit = None
    if raw_enforce_limit is not None:
        if not isinstance(raw_enforce_limit, bool):
            return jsonify({
                "success": False,
                "error": "enforce_limit باید true یا false باشد",
            }), 400

        enforce_limit = raw_enforce_limit

    # درخواست خالی یا بدون هیچ مقدار قابل‌ویرایش، معتبر نیست.
    if max_users is None and enforce_limit is None:
        return jsonify({
            "success": False,
            "error": "حداقل یکی از تنظیمات max_users یا enforce_limit الزامی است",
        }), 400

    with config_lock:
        try:
            with config_file_lock():
                config = load_remote_config()
                config, access_control = ensure_access_control_structure(config)

                if max_users is not None:
                    access_control["max_users"] = max_users

                if enforce_limit is not None:
                    access_control["enforce_limit"] = enforce_limit

                saved = save_remote_config(config, lock_held=True)

                if not saved:
                    return jsonify({
                        "success": False,
                        "error": "ذخیره‌سازی امن config.json ناموفق بود",
                    }), 500

                return jsonify({
                    "success": True,
                    "max_users": access_control.get("max_users"),
                    "enforce_limit": access_control.get("enforce_limit"),
                })

        except TimeoutError:
            logging.error("Timed out while updating access-control settings.")
            return jsonify({
                "success": False,
                "error": "فایل تنظیمات موقتاً در حال استفاده است؛ دوباره تلاش کنید",
            }), 503

        except Exception as error:
            logging.exception("Unexpected error while updating access-control settings.")
            return jsonify({
                "success": False,
                "error": str(error),
            }), 500


@app.route("/admin/api/git/push", methods=["POST"])
@login_required
def api_git_push():
    """
    فقط config.json را منتشر می‌کند:
    1) حذف همه فایل‌ها از Git staging area
    2) stage کردن فقط config.json
    3) amend آخرین commit
    4) push امن با force-with-lease
    """
    repo_dir = str(CONFIG_DIRECTORY)

    try:
        # 1. پاک کردن staging area بدون تغییر دادن فایل‌های محلی.
        reset_result = subprocess.run(
            ["git", "reset"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )

        if reset_result.returncode != 0:
            return jsonify({
                "success": False,
                "error": "Git reset failed",
                "details": reset_result.stderr.strip() or reset_result.stdout.strip(),
            }), 500

        # 2. فقط config.json وارد staging شود.
        add_result = subprocess.run(
            ["git", "add", "config.json"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )

        if add_result.returncode != 0:
            return jsonify({
                "success": False,
                "error": "Git add failed",
                "details": add_result.stderr.strip() or add_result.stdout.strip(),
            }), 500

        # 3. جایگزین‌کردن آخرین commit با نسخهٔ جدید config.json.
        amend_result = subprocess.run(
            ["git", "commit", "--amend", "--no-edit"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )

        if amend_result.returncode != 0:
            return jsonify({
                "success": False,
                "error": "Git commit amend failed",
                "details": amend_result.stderr.strip() or amend_result.stdout.strip(),
            }), 500

        # 4. انتشار امن روی شاخهٔ main.
        push_result = subprocess.run(
            ["git", "push", "--force-with-lease", "origin", "main"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )

        if push_result.returncode != 0:
            return jsonify({
                "success": False,
                "error": "Git push failed",
                "details": push_result.stderr.strip() or push_result.stdout.strip(),
            }), 500

        return jsonify({
            "success": True,
            "message": "config.json was amended and pushed successfully",
        })

    except Exception as error:
        logging.exception("Unexpected error while publishing config.json")
        return jsonify({
            "success": False,
            "error": str(error),
        }), 500


if __name__ == "__main__":
    load_model()
    app.run(host="0.0.0.0", port=5000, debug=False)
