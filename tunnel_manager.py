import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from contextlib import contextmanager

import msvcrt
import requests


# --- Configuration ---

CONFIG_PATH = Path(
    r"D:\Android\Projects\LingoDirectWorkspace\lingodirect-config\config.json"
)

# پوشهٔ اصلی مخزن Git
REPOSITORY_PATH = CONFIG_PATH.parent

# ذخیرهٔ لاگ‌ها درون همان مخزن/پوشهٔ پروژه
LOG_DIR = REPOSITORY_PATH / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

LOCAL_SERVER = "http://127.0.0.1:5000"
LOCAL_HEALTH_URL = f"{LOCAL_SERVER}/health"
TUNNEL_STATUS_URL = f"{LOCAL_SERVER}/tunnel_status"

GITHUB_PAGES_URL = (
    "https://1866universe.github.io/lingodirect-config/config.json"
)


# Safer than shell=True
TUNNEL_COMMAND = [
    "ssh",
    "-R",
    "80:127.0.0.1:5000",
    "nokey@localhost.run",
]

STARTUP_TIMEOUT = 45

# Health monitoring
HEALTH_CHECK_INTERVAL = 30
HEALTH_CHECK_TIMEOUT = 8
MAX_CONSECUTIVE_FAILURES = 3

# Tunnel validation before publishing
PUBLIC_VALIDATION_ATTEMPTS = 3
PUBLIC_VALIDATION_DELAY = 2

# Retry/backoff
RETRY_DELAYS = [2, 5, 10, 20, 30, 60]

# HTTP headers
LOCAL_MONITOR_HEADERS = {
    "User-Agent": "Tunnel-Manager/Internal-Local-Check"
}

PUBLIC_MONITOR_HEADERS = {
    "User-Agent": "Tunnel-Manager/Public-Health-Check"
}


# --- Logging ---

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

file_handler = RotatingFileHandler(
    filename=str(LOG_DIR / "tunnel_manager.log"),
    maxBytes=10 * 1024 * 1024,  # 10 MB
    backupCount=5,               # حداکثر 5 فایل پشتیبان
    encoding="utf-8",
)

file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter(LOG_FORMAT))

console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter(LOG_FORMAT))

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

# جلوگیری از اضافه شدن Handler تکراری
root_logger.handlers.clear()

root_logger.addHandler(file_handler)
root_logger.addHandler(console)


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


# --- Utility Functions ---

def run_command(command, cwd=REPOSITORY_PATH):
    """
    اجرای امن دستورات ثابت Git.
    command باید به‌صورت list ارسال شود، نه string.
    """
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            shell=False,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        return (
            result.returncode,
            result.stdout.strip(),
            result.stderr.strip(),
        )

    except subprocess.TimeoutExpired:
        command_text = " ".join(command)
        logging.error(
            f"Git command timed out after 60 seconds: {command_text}"
        )
        return 124, "", "Command timed out"

    except OSError as error:
        command_text = " ".join(command)
        logging.error(
            f"Failed to execute command {command_text}: {error}"
        )
        return 1, "", str(error)


def is_local_server_alive():
    """Checks if the local Flask server is running."""
    try:
        response = requests.get(
            LOCAL_HEALTH_URL,
            headers=LOCAL_MONITOR_HEADERS,
            timeout=3,
        )
        return response.status_code == 200
    except requests.RequestException as e:
        logging.warning(f"Local Flask server is not responding: {e}")
        return False


def is_public_tunnel_alive(public_url):
    """
    Checks if the public tunnel URL reaches the Flask /health endpoint.
    This validates the real public route, not just the local Flask server.
    """
    if not public_url:
        return False

    health_url = public_url.rstrip("/") + "/health"

    try:
        response = requests.get(
            health_url,
            headers=PUBLIC_MONITOR_HEADERS,
            timeout=HEALTH_CHECK_TIMEOUT,
        )

        if response.status_code != 200:
            logging.warning(
                f"Public health failed. url={health_url}, status={response.status_code}"
            )
            return False

        # Optional but useful:
        # If your /health endpoint returns JSON, validate it here.
        # For now, status 200 is enough to avoid breaking current Flask code.
        return True

    except requests.RequestException as e:
        logging.warning(f"Public health exception. url={health_url}, error={e}")
        return False


def validate_public_tunnel(public_url):
    """
    Performs multiple public health checks before publishing the URL to GitHub.
    """
    for i in range(1, PUBLIC_VALIDATION_ATTEMPTS + 1):
        if is_public_tunnel_alive(public_url):
            logging.info(f"Public tunnel validation passed: {public_url}")
            return True

        logging.warning(
            f"Public tunnel validation failed "
            f"({i}/{PUBLIC_VALIDATION_ATTEMPTS}): {public_url}"
        )
        time.sleep(PUBLIC_VALIDATION_DELAY)

    return False


CONFIG_LOCK_PATH = CONFIG_PATH.parent / "config.json.lock"
CONFIG_LOCK_TIMEOUT_SECONDS = 10

@contextmanager
def config_file_lock(timeout_seconds=CONFIG_LOCK_TIMEOUT_SECONDS):
    """قفل بین‌پردازه‌ای مشترک با app.py"""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_LOCK_PATH, "a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        deadline = time.time() + timeout_seconds
        lock_acquired = False
        while time.time() < deadline:
            try:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                lock_acquired = True
                break
            except OSError:
                time.sleep(0.1)
        if not lock_acquired:
            raise TimeoutError("Timed out waiting for config.json lock")
        try:
            yield
        finally:
            try:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass


def load_current_url():
    """Reads the current URL from the local config file."""
    if not CONFIG_PATH.exists():
        return None

    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data.get("base_url")
    except (OSError, json.JSONDecodeError):
        return None


def save_url(new_url):
    """ذخیره‌سازی ایمن و اتمیک URL با استفاده از قفل مشترک"""
    try:
        with config_file_lock():
            data = {}

            if CONFIG_PATH.exists():
                try:
                    existing_content = CONFIG_PATH.read_text(encoding="utf-8").strip()
                    if existing_content:
                        loaded_data = json.loads(existing_content)
                        if isinstance(loaded_data, dict):
                            data = loaded_data
                except json.JSONDecodeError:
                    logging.warning(
                        "config.json is corrupted or invalid JSON. Rebuilding structure."
                    )
                except OSError as e:
                    logging.warning(f"Failed to read config.json: {e}")

            if not isinstance(data, dict):
                data = {}

            data["base_url"] = new_url

            if "server" not in data or not isinstance(data["server"], dict):
                data["server"] = {}

            data["server"]["baseUrl"] = new_url
            data["server"]["status"] = "online"

            tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
            backup_path = CONFIG_PATH.with_suffix(".json.bak")

            # بکاپ قبل از نوشتن
            if CONFIG_PATH.exists():
                try:
                    backup_path.write_text(
                        CONFIG_PATH.read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                except Exception as e:
                    logging.warning(f"Could not create backup: {e}")

            # نوشتن اتمیک در فایل موقت
            tmp_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            # جایگزینی اتمیک
            os.replace(tmp_path, CONFIG_PATH)

            logging.info("Local config updated atomically with lock.")
    except Exception as e:
        logging.error(f"Error saving config.json atomically: {e}")


def get_public_url():
    """Reads the current URL from the GitHub Pages config file."""
    try:
        response = requests.get(GITHUB_PAGES_URL, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return data.get("base_url")
    except (requests.RequestException, ValueError) as e:
        logging.warning(f"Failed to read GitHub Pages config: {e}")

    return None


def commit_and_push(new_url):
    """
    ذخیرهٔ URL جدید در config.json و انتشار آن با استفاده از
    amend کردن commit فعلی.

    روند Git:
        git status
        git add config.json
        git commit --amend --no-edit
        git push --force-with-lease origin main
    """
    logging.info("Starting Git update process...")

    # ابتدا URL جدید در فایل محلی ذخیره می‌شود.
    # این تابع سایر اطلاعات config.json را حفظ می‌کند.
    save_url(new_url)

    # بررسی وضعیت مخزن
    code, out, err = run_command(
        ["git", "status", "--short"]
    )

    if code != 0:
        logging.error(f"Git status failed: {out or err}")
        return False

    logging.info(
        f"Git status:\n{out or '(no visible changes)'}"
    )

    # فقط config.json وارد staging می‌شود.
    code, out, err = run_command(
        ["git", "add", "config.json"]
    )

    if code != 0:
        logging.error(f"Git add failed: {out or err}")
        return False

    logging.info("Git: config.json staged successfully.")

    # بررسی اینکه واقعاً config.json در staging قرار گرفته است.
    code, out, err = run_command(
        ["git", "diff", "--cached", "--name-only"]
    )

    if code != 0:
        logging.error(
            f"Git staged-files check failed: {out or err}"
        )
        return False

    staged_files = {
        line.strip()
        for line in out.splitlines()
        if line.strip()
    }

    if "config.json" not in staged_files:
        logging.info(
            "Git: config.json has no staged changes. "
            "Skipping amend and push."
        )
        return True

    # همیشه commit فعلی اصلاح می‌شود.
    # هیچ commit جدیدی ایجاد نمی‌شود.
    code, out, err = run_command(
        ["git", "commit", "--amend", "--no-edit"]
    )

    if code != 0:
        logging.error(
            f"Git commit amend failed: {out or err}"
        )
        return False

    logging.info(
        "Git: Existing commit amended successfully."
    )

    # انتشار commit اصلاح‌شده روی شاخهٔ اصلی
    code, out, err = run_command(
        [
            "git",
            "push",
            "--force-with-lease",
            "origin",
            "main",
        ]
    )

    if code != 0:
        logging.error(
            f"Git push failed: {out or err}"
        )
        return False

    logging.info(
        "Git: config.json pushed successfully to origin/main."
    )
    return True


def extract_url(text):
    """Extracts the public tunnel URL from SSH output."""
    urls = re.findall(
        r"https://[a-zA-Z0-9.-]+\.(?:lhr\.life|localhost\.run)\b",
        text,
    )

    for url in urls:
        if url == "https://admin.localhost.run":
            continue
        return url

    return None


def start_tunnel():
    """Starts the SSH tunnel process."""
    logging.info("Starting SSH tunnel process...")

    process = subprocess.Popen(
        TUNNEL_COMMAND,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    return process


def shutdown_process(process):
    """Safely terminates the SSH tunnel process."""
    if not process:
        return

    if process.poll() is not None:
        return

    logging.info("Terminating SSH tunnel process...")

    try:
        process.terminate()
        process.wait(timeout=5)
        logging.info("SSH tunnel process terminated gracefully.")
    except Exception:
        logging.warning("Graceful termination failed. Killing SSH process...")
        try:
            process.kill()
            process.wait(timeout=5)
            logging.info("SSH tunnel process killed.")
        except Exception as e:
            logging.error(f"Failed to kill SSH process: {e}")


def stream_reader(process, output_queue):
    """
    Reads SSH stdout in a separate thread so the main supervisor does not block.
    """
    try:
        for line in iter(process.stdout.readline, ""):
            if not line:
                break

            clean_line = line.strip()
            output_queue.put(clean_line)
            logging.info(f"SSH: {clean_line}")

    except Exception as e:
        logging.error(f"SSH output reader failed: {e}")


def wait_for_tunnel_url(process, attempt_id):
    """
    Waits for localhost.run/lhr.life to print the public tunnel URL.
    """
    output_queue = queue.Queue()

    reader_thread = threading.Thread(
        target=stream_reader,
        args=(process, output_queue),
        daemon=True,
    )
    reader_thread.start()

    deadline = time.time() + STARTUP_TIMEOUT

    while time.time() < deadline:
        if process.poll() is not None:
            logging.warning(
                f"[TRY-{attempt_id}] SSH process exited before URL was found. "
                f"exit_code={process.poll()}"
            )
            return None

        try:
            line = output_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        public_url = extract_url(line)
        if public_url:
            logging.info(f"[TRY-{attempt_id}] Candidate tunnel URL found: {public_url}")
            return public_url

    logging.warning(f"[TRY-{attempt_id}] Timed out waiting for tunnel URL.")
    return None


def report_tunnel_status(status, details, attempt_id=None, public_url=None):
    """
    Sends only final/meaningful tunnel status to Flask.
    Avoid using this for temporary retry noise.
    """
    payload = {
        "status": status,
        "details": details,
        "attempt_id": attempt_id,
        "public_url": public_url,
        "time": utc_now_iso(),
    }

    try:
        requests.post(
            TUNNEL_STATUS_URL,
            json=payload,
            timeout=2,
        )
    except requests.RequestException as e:
        logging.warning(f"Failed to report tunnel status to Flask: {e}")


def establish_valid_tunnel(attempt_id):
    """
    Starts SSH, extracts candidate URL, validates it publicly,
    and returns (process, public_url) only when the tunnel is actually usable.
    """
    if not is_local_server_alive():
        logging.warning(f"[TRY-{attempt_id}] Local Flask server is offline.")
        return None, None

    process = start_tunnel()
    public_url = wait_for_tunnel_url(process, attempt_id)

    if not public_url:
        shutdown_process(process)
        return None, None

    logging.info(f"[TRY-{attempt_id}] Validating public tunnel: {public_url}")

    if not validate_public_tunnel(public_url):
        logging.warning(
            f"[TRY-{attempt_id}] Candidate URL failed public validation: {public_url}"
        )
        shutdown_process(process)
        return None, None

    return process, public_url


def publish_tunnel_if_needed(public_url, attempt_id):
    """
    URL معتبر تونل را در config.json ذخیره و در Git منتشر می‌کند.

    تشخیص تکراری‌بودن URL فقط بر اساس فایل محلی انجام می‌شود.
    بررسی فوری GitHub Pages حذف شده است، چون ممکن است به‌دلیل cache
    با تأخیر URL جدید را نشان دهد.
    """
    current_url = load_current_url()

    if public_url == current_url:
        logging.info(
            f"[TRY-{attempt_id}] URL already matches local config. "
            f"No publish needed."
        )
        return True

    logging.info(
        f"[TRY-{attempt_id}] Publishing validated URL: {public_url}"
    )

    if not commit_and_push(public_url):
        logging.error(
            f"[TRY-{attempt_id}] Failed to update and publish config.json."
        )
        return False

    return True


def monitor_active_tunnel(process, public_url, attempt_id):
    """
    Keeps the current tunnel alive as long as it is healthy.
    Returns immediately on the first public health failure.
    """
    logging.info(f"[TRY-{attempt_id}] Entering active monitor mode: {public_url}")

    while True:
        # 1. SSH process-level failure
        if process.poll() is not None:
            exit_code = process.poll()
            logging.warning(
                f"[TRY-{attempt_id}] SSH process exited. exit_code={exit_code}"
            )
            return "ssh_process_exited"

        # 2. Public route health check
        if is_public_tunnel_alive(public_url):
            pass
        else:
            logging.error(
                f"[TRY-{attempt_id}] Tunnel considered DOWN after first "
                f"public health failure: {public_url}"
            )
            return "public_health_failed"

        time.sleep(HEALTH_CHECK_INTERVAL)


def tunnel_supervisor():
    """
    Event-driven tunnel supervisor.

    - Does not renew a healthy tunnel.
    - Publishes only validated public URLs.
    - Reports only final UP/DOWN states to Flask.
    - Keeps internal retry noise inside tunnel_manager.log.
    """
    logging.info("Starting Event-Driven Tunnel Supervisor...")

    attempt_id = 0
    retry_index = 0

    process = None
    public_url = None

    while True:
        attempt_id += 1

        logging.info(f"[TRY-{attempt_id}] Starting tunnel establishment attempt...")

        process, public_url = establish_valid_tunnel(attempt_id)

        if not process or not public_url:
            delay = RETRY_DELAYS[min(retry_index, len(RETRY_DELAYS) - 1)]
            retry_index += 1

            logging.warning(
                f"[TRY-{attempt_id}] Tunnel establishment failed. "
                f"Retrying in {delay}s..."
            )

            time.sleep(delay)
            continue

        # A valid tunnel resets retry/backoff
        retry_index = 0

        if publish_tunnel_if_needed(public_url, attempt_id):
            report_tunnel_status(
                status="SUCCESS",
                details=f"Tunnel established and validated at {public_url}",
                attempt_id=attempt_id,
                public_url=public_url,
            )

            logging.info(
                f"[TRY-{attempt_id}] Tunnel is UP and published: {public_url}"
            )
        else:
            # Conservative behavior:
            # If GitHub publish fails, we keep the tunnel process alive briefly,
            # but report no SUCCESS to Flask because Android cannot discover it reliably.
            logging.error(
                f"[TRY-{attempt_id}] Tunnel is valid but publish failed. "
                f"Restarting after cleanup."
            )
            shutdown_process(process)
            time.sleep(10)
            continue

        # Stay here as long as the tunnel is healthy.
        down_reason = monitor_active_tunnel(process, public_url, attempt_id)

        # Confirmed DOWN: report only once.
        report_tunnel_status(
            status="DOWN",
            details=f"Tunnel confirmed down: {down_reason}",
            attempt_id=attempt_id,
            public_url=public_url,
        )

        shutdown_process(process)

        delay = RETRY_DELAYS[min(retry_index, len(RETRY_DELAYS) - 1)]
        retry_index += 1

        logging.warning(
            f"[TRY-{attempt_id}] Restarting tunnel after DOWN. "
            f"Reason={down_reason}. Next attempt in {delay}s..."
        )

        time.sleep(delay)


if __name__ == "__main__":
    try:
        tunnel_supervisor()
    except KeyboardInterrupt:
        logging.info("Tunnel supervisor stopped by user.")
