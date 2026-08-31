import json
import logging
import os
import queue
import re
import smtplib
import subprocess
import threading
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from datetime import datetime
from logging.handlers import RotatingFileHandler
from contextlib import contextmanager

import msvcrt
import requests


# --- Configuration ---

CONFIG_PATH = Path(
    r"D:\Android\Projects\LingoDirectWorkspace\lingodirect-config\config.json"
)

# Root directory of the Git repository
REPOSITORY_PATH = CONFIG_PATH.parent

# Store logs inside the repository log directory
LOG_DIR = REPOSITORY_PATH / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

LOCAL_PORT = 5000
LOCAL_SERVER = f"http://127.0.0.1:{LOCAL_PORT}"
LOCAL_HEALTH_URL = f"{LOCAL_SERVER}/health"
TUNNEL_STATUS_URL = f"{LOCAL_SERVER}/tunnel_status"

GITHUB_PAGES_URL = (
    "https://1866universe.github.io/lingodirect-config/config.json"
)

# Paths for Local Server Auto-Restart Watchdog
FLASK_DIR = Path(
    r"D:\Android\Projects\LingoDirectWorkspace\nllb_server"
)
FLASK_APP_PATH = FLASK_DIR / "app.py"
FLASK_PYTHON_EXE = FLASK_DIR / ".venv" / "Scripts" / "python.exe"

# --- Email Alert Settings ---

def _clean_ascii(val: str) -> str:
    """Sanitize input string by stripping invisible Unicode characters (e.g., RLM/LRM) and whitespace."""
    if not val:
        return ""
    return "".join(c for c in str(val) if ord(c) < 128).strip().replace(" ", "")

EMAIL_ALERTS_ENABLED = (
    _clean_ascii(os.environ.get("LINGODIRECT_EMAIL_ALERTS_ENABLED", "true")).lower()
    in {"1", "true", "yes", "on"}
)

SMTP_SERVER = _clean_ascii(
    os.environ.get("LINGODIRECT_SMTP_SERVER", "smtp.gmail.com")
)

try:
    SMTP_PORT = int(_clean_ascii(os.environ.get("LINGODIRECT_SMTP_PORT", "587")))
except ValueError:
    SMTP_PORT = 587

SENDER_EMAIL = _clean_ascii(os.environ.get("LINGODIRECT_ALERT_SENDER_EMAIL", ""))
SENDER_PASSWORD = _clean_ascii(os.environ.get("LINGODIRECT_ALERT_SMTP_PASSWORD", ""))
ALERT_RECIPIENT = _clean_ascii(os.environ.get("LINGODIRECT_ALERT_RECIPIENT", ""))

CONSECUTIVE_FAILURES_FOR_ALERT = 5
ALERT_COOLDOWN_SECONDS = 1800

# Safer than shell=True
TUNNEL_COMMAND = [
    "ssh",
    "-R",
    f"80:127.0.0.1:{LOCAL_PORT}",
    "nokey@localhost.run",
]

STARTUP_TIMEOUT = 45

# Health monitoring
HEALTH_CHECK_INTERVAL = 30
HEALTH_CHECK_TIMEOUT = 8
CONSECUTIVE_PUBLIC_FAILS_BEFORE_DOWN = 3

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
    backupCount=5,               # Keep up to 5 backup log files
    encoding="utf-8",
)

file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter(LOG_FORMAT))

console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter(LOG_FORMAT))

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

# Prevent duplicate handlers
root_logger.handlers.clear()

root_logger.addHandler(file_handler)
root_logger.addHandler(console)


def utc_now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def email_alerts_ready():
    if not EMAIL_ALERTS_ENABLED:
        return False

    missing = []

    if not SENDER_EMAIL:
        missing.append("LINGODIRECT_ALERT_SENDER_EMAIL")

    if not SENDER_PASSWORD:
        missing.append("LINGODIRECT_ALERT_SMTP_PASSWORD")

    if not ALERT_RECIPIENT:
        missing.append("LINGODIRECT_ALERT_RECIPIENT")

    if missing:
        logging.error(
            "Email alerts are enabled but required environment variables are missing: "
            + ", ".join(missing)
        )
        return False

    return True


# --- Email Notification Handler ---

LAST_ALERT_TIME = 0

def send_email_alert_async(subject, message_body):
    """Send alert email asynchronously in a separate thread to prevent blocking main tunnel operations."""
    global LAST_ALERT_TIME

    if not email_alerts_ready():
        return

    current_time = time.time()
    if current_time - LAST_ALERT_TIME < ALERT_COOLDOWN_SECONDS:
        logging.info("Email alert suppressed due to cooldown policy.")
        return

    def _send():
        global LAST_ALERT_TIME
        try:
            msg = MIMEMultipart()
            msg["From"] = SENDER_EMAIL
            msg["To"] = ALERT_RECIPIENT
            msg["Subject"] = f"[LingoDirect Alert] {subject}"
            msg.attach(MIMEText(message_body, "plain", "utf-8"))

            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15)
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
            server.quit()

            LAST_ALERT_TIME = time.time()
            logging.info("Emergency email alert successfully sent.")
        except Exception as e:
            logging.error(f"Failed to send email alert: {e}")

    threading.Thread(target=_send, daemon=True).start()


# --- Watchdog: Local Server Process Management ---

def clean_kill_port(port: int = LOCAL_PORT):
    """
    Find and forcefully terminate any process listening on the specified local port.
    Prevents zombie sockets from blocking the server restart on Windows.
    """
    logging.info(f"[WATCHDOG] Inspecting active listeners on port {port}...")
    try:
        output = subprocess.check_output(
            f"netstat -ano | findstr :{port}",
            shell=True,
            text=True,
            stderr=subprocess.DEVNULL
        )
        pids = set()
        for line in output.strip().splitlines():
            parts = line.split()
            if len(parts) >= 5 and "LISTENING" in parts:
                pid = parts[-1]
                if pid.isdigit() and int(pid) != 0:
                    pids.add(pid)

        for pid in pids:
            logging.warning(f"[WATCHDOG] Forcefully terminating zombie process PID={pid} holding port {port}...")
            subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True, check=False)
            
        time.sleep(1.5)
    except subprocess.CalledProcessError:
        logging.info(f"[WATCHDOG] Port {port} is completely free.")
    except Exception as e:
        logging.error(f"[WATCHDOG] Error terminating port listeners: {e}")


def restart_local_flask_server():
    """
    Watchdog recovery routine: kills stuck processes on port 5000 and restarts Waitress/Flask.
    """
    logging.warning("[WATCHDOG] Triggering automated recovery for local Flask server...")
    
    clean_kill_port(LOCAL_PORT)

    if not FLASK_PYTHON_EXE.exists():
        logging.critical(f"[WATCHDOG] Virtual environment Python executable not found: {FLASK_PYTHON_EXE}")
        return False

    if not FLASK_APP_PATH.exists():
        logging.critical(f"[WATCHDOG] Flask app.py script not found: {FLASK_APP_PATH}")
        return False

    try:
        logging.info(f"[WATCHDOG] Launching Flask server via {FLASK_PYTHON_EXE}...")

        # On Windows: Open a separate console window to monitor Flask output live.
        extra_kwargs = {}
        if os.name == "nt":
            extra_kwargs["creationflags"] = (
                subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
            )

        subprocess.Popen(
            [str(FLASK_PYTHON_EXE), str(FLASK_APP_PATH)],
            cwd=str(FLASK_DIR),
            **extra_kwargs,
        )
    except Exception as e:
        logging.critical(f"[WATCHDOG] Failed to spawn Flask server process: {e}")
        return False

    deadline = time.time() + 45
    logging.info("[WATCHDOG] Waiting for local server to become healthy...")
    while time.time() < deadline:
        if is_local_server_alive():
            logging.info("[WATCHDOG] Local server successfully restarted and verified healthy.")
            return True
        time.sleep(2)

    logging.error("[WATCHDOG] Local server restart timed out before /health became responsive.")
    return False


# --- Git Auto-Heal & Utilities ---

def auto_heal_git_index(repo_dir=REPOSITORY_PATH):
    """
    Automatically recover Git repository from corrupted index files.
    """
    index_file = repo_dir / ".git" / "index"
    logging.warning("Initiating Git Auto-Heal procedure...")
    try:
        if index_file.exists():
            index_file.unlink()
            logging.info("Corrupted .git/index deleted.")
        
        subprocess.run(
            ["git", "reset"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=15,
            check=False
        )
        logging.info("Git Auto-Heal completed successfully.")
        return True
    except Exception as e:
        logging.error(f"Git Auto-Heal failed: {e}")
        return False


def run_command(command, cwd=REPOSITORY_PATH):
    """
    Safely execute Git commands with automated index corruption recovery.
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

        err_msg = result.stderr or ""
        out_msg = result.stdout or ""
        if "index file corrupt" in err_msg or "bad signature" in err_msg or "bad signature" in out_msg:
            logging.warning("Detected corrupted Git index. Triggering Auto-Heal...")
            if auto_heal_git_index(cwd):
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
    """Checks if the local Flask server is running and responding with 200 OK."""
    try:
        response = requests.get(
            LOCAL_HEALTH_URL,
            headers=LOCAL_MONITOR_HEADERS,
            timeout=4,
        )
        return response.status_code == 200
    except requests.RequestException:
        return False


def is_public_tunnel_alive(public_url):
    """
    Checks if the public tunnel URL reaches the Flask /health endpoint.
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
                f"Public health check returned non-200. url={health_url}, status={response.status_code}"
            )
            return False

        return True

    except requests.RequestException as e:
        logging.warning(f"Public health check request failed. url={health_url}, error={e}")
        return False


def validate_public_tunnel(public_url):
    """
    Performs initial public health checks before publishing the URL to GitHub.
    """
    for i in range(1, PUBLIC_VALIDATION_ATTEMPTS + 1):
        if is_public_tunnel_alive(public_url):
            logging.info(f"Public tunnel validation passed: {public_url}")
            return True

        logging.warning(
            f"Public tunnel validation check "
            f"({i}/{PUBLIC_VALIDATION_ATTEMPTS}) failed: {public_url}"
        )
        time.sleep(PUBLIC_VALIDATION_DELAY)

    return False


CONFIG_LOCK_PATH = CONFIG_PATH.parent / "config.json.lock"
CONFIG_LOCK_TIMEOUT_SECONDS = 10

@contextmanager
def config_file_lock(timeout_seconds=CONFIG_LOCK_TIMEOUT_SECONDS):
    """Cross-process lock shared with app.py for concurrent-safe configuration updates."""
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
    """Safely and atomically save URL while preserving user records and app_management structure."""
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

            if CONFIG_PATH.exists():
                try:
                    backup_path.write_text(
                        CONFIG_PATH.read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                except Exception as e:
                    logging.warning(f"Could not create backup: {e}")

            tmp_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            os.replace(tmp_path, CONFIG_PATH)
            logging.info("Local config updated atomically with lock.")
    except Exception as e:
        logging.error(f"Error saving config.json atomically: {e}")


def commit_and_push(new_url):
    """
    Save new URL into config.json and publish it by amending the current commit.
    """
    logging.info("Starting Git update process...")

    save_url(new_url)

    code, out, err = run_command(["git", "status", "--short"])
    if code != 0:
        logging.error(f"Git status failed: {out or err}")
        return False

    code, out, err = run_command(["git", "add", "config.json"])
    if code != 0:
        logging.error(f"Git add failed: {out or err}")
        return False

    code, out, err = run_command(["git", "diff", "--cached", "--name-only"])
    if code != 0:
        logging.error(f"Git staged-files check failed: {out or err}")
        return False

    staged_files = {line.strip() for line in out.splitlines() if line.strip()}
    if "config.json" not in staged_files:
        logging.info("Git: config.json has no staged changes. Skipping amend and push.")
        return True

    code, out, err = run_command(["git", "commit", "--amend", "--no-edit"])
    if code != 0:
        logging.error(f"Git commit amend failed: {out or err}")
        return False

    code, out, err = run_command(["git", "push", "--force-with-lease", "origin", "main"])
    if code != 0:
        logging.error(f"Git push failed: {out or err}")
        return False

    logging.info("Git: config.json published successfully to origin/main.")
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
    logging.info("Starting SSH tunnel process with Keep-Alive parameters...")

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


def wait_for_tunnel_url(process, output_queue, attempt_id):
    """
    Waits for localhost.run/lhr.life to print the initial public tunnel URL.
    """
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
    Sends tunnel status to Flask for dashboard synchronization.
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
    except requests.RequestException:
        pass


def establish_valid_tunnel(attempt_id):
    """
    Guarantees local server health (reviving via watchdog if dead),
    starts SSH tunnel, validates candidate URL publicly, and returns (process, public_url, output_queue).
    """
    if not is_local_server_alive():
        logging.warning(f"[TRY-{attempt_id}] Local server unreachable. Triggering Watchdog restart...")
        if not restart_local_flask_server():
            logging.error(f"[TRY-{attempt_id}] Watchdog failed to recover local server.")
            return None, None, None

    process = start_tunnel()
    output_queue = queue.Queue()

    reader_thread = threading.Thread(
        target=stream_reader,
        args=(process, output_queue),
        daemon=True,
    )
    reader_thread.start()

    public_url = wait_for_tunnel_url(process, output_queue, attempt_id)

    if not public_url:
        shutdown_process(process)
        return None, None, None

    logging.info(f"[TRY-{attempt_id}] Validating initial public tunnel: {public_url}")

    if not validate_public_tunnel(public_url):
        logging.warning(
            f"[TRY-{attempt_id}] Candidate URL failed initial public validation: {public_url}"
        )
        shutdown_process(process)
        return None, None, None

    return process, public_url, output_queue


def publish_tunnel_if_needed(public_url, attempt_id):
    """
    Saves validated tunnel URL into config.json and publishes changes via Git.
    """
    current_url = load_current_url()

    if public_url == current_url:
        logging.info(
            f"[TRY-{attempt_id}] URL already matches local config. No publish needed."
        )
        return True

    logging.info(f"[TRY-{attempt_id}] Publishing validated URL: {public_url}")

    if not commit_and_push(public_url):
        logging.error(
            f"[TRY-{attempt_id}] Failed to update and publish config.json."
        )
        return False

    return True


def monitor_active_tunnel(process, public_url, output_queue, attempt_id):
    """
    Keeps the current tunnel alive. Includes consecutive failure debounce to prevent
    unnecessary restarts during transient 502/network spikes. Also handles remote domain rotation.
    """
    logging.info(f"[TRY-{attempt_id}] Entering active monitor mode: {public_url}")
    current_active_url = public_url
    consecutive_public_fails = 0

    while True:
        # 1. SSH process-level failure
        if process.poll() is not None:
            exit_code = process.poll()
            logging.warning(
                f"[TRY-{attempt_id}] SSH process exited. exit_code={exit_code}"
            )
            return "ssh_process_exited"

        # 2. Local Flask health verification
        if not is_local_server_alive():
            logging.error(
                f"[TRY-{attempt_id}] Local Flask server became unresponsive."
            )
            return "local_server_died"

        # 3. Check for domain rotation printed in SSH output without dying
        while not output_queue.empty():
            try:
                line = output_queue.get_nowait()
                rotated_url = extract_url(line)
                if rotated_url and rotated_url != current_active_url:
                    logging.warning(
                        f"[TRY-{attempt_id}] Domain rotation detected from localhost.run: {rotated_url}"
                    )
                    if validate_public_tunnel(rotated_url):
                        current_active_url = rotated_url
                        publish_tunnel_if_needed(current_active_url, attempt_id)
                        consecutive_public_fails = 0
            except queue.Empty:
                break

        # 4. Public route health check with debounce tolerance
        if is_public_tunnel_alive(current_active_url):
            consecutive_public_fails = 0
        else:
            consecutive_public_fails += 1
            logging.warning(
                f"[TRY-{attempt_id}] Public health check failed ({consecutive_public_fails}/{CONSECUTIVE_PUBLIC_FAILS_BEFORE_DOWN})."
            )
            if consecutive_public_fails >= CONSECUTIVE_PUBLIC_FAILS_BEFORE_DOWN:
                logging.error(
                    f"[TRY-{attempt_id}] Tunnel declared DOWN after {CONSECUTIVE_PUBLIC_FAILS_BEFORE_DOWN} consecutive public check failures: {current_active_url}"
                )
                return "public_health_failed"

        time.sleep(HEALTH_CHECK_INTERVAL)


def tunnel_supervisor():
    """
    Event-driven tunnel supervisor with Fault-Tolerant Monitor, Flask Watchdog, and Git Auto-Heal.
    """
    logging.info("Starting Resilient Tunnel Supervisor (Fault-Tolerant & Debounced)...")

    attempt_id = 0
    retry_index = 0
    consecutive_failures = 0

    while True:
        attempt_id += 1
        logging.info(f"[TRY-{attempt_id}] Starting tunnel establishment attempt...")

        process, public_url, output_queue = establish_valid_tunnel(attempt_id)

        if not process or not public_url:
            consecutive_failures += 1
            delay = RETRY_DELAYS[min(retry_index, len(RETRY_DELAYS) - 1)]
            retry_index += 1

            logging.warning(
                f"[TRY-{attempt_id}] Tunnel establishment failed (Consecutive failures: {consecutive_failures}). "
                f"Retrying in {delay}s..."
            )

            if consecutive_failures >= CONSECUTIVE_FAILURES_FOR_ALERT:
                msg = (
                    f"Warning: LingoDirect tunnel supervisor has failed to establish a connection "
                    f"after {consecutive_failures} consecutive attempts.\n\n"
                    f"Timestamp: {utc_now_iso()}\n"
                    f"Last Attempt ID: {attempt_id}\n\n"
                    f"Please verify network connection or local server status."
                )
                send_email_alert_async("Tunnel Connection Failure", msg)

            time.sleep(delay)
            continue

        if publish_tunnel_if_needed(public_url, attempt_id):
            consecutive_failures = 0
            retry_index = 0

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
            consecutive_failures += 1
            logging.error(
                f"[TRY-{attempt_id}] Tunnel is valid but publish failed. Restarting after cleanup."
            )
            shutdown_process(process)
            time.sleep(10)
            continue

        down_reason = monitor_active_tunnel(process, public_url, output_queue, attempt_id)

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
            f"[TRY-{attempt_id}] Restarting tunnel after DOWN. Reason={down_reason}. Next attempt in {delay}s..."
        )

        time.sleep(delay)


if __name__ == "__main__":
    try:
        tunnel_supervisor()
    except KeyboardInterrupt:
        logging.info("Tunnel supervisor stopped by user.")
