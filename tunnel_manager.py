import json
import re
import subprocess
import time
from pathlib import Path

import requests

# --- Configuration ---
CONFIG_PATH = Path("config.json")
LOCAL_SERVER = "http://127.0.0.1:5000"
# مسیر جدید برای بررسی سلامت بدون ایجاد خطای 404 نامفهوم
HEALTH_CHECK_URL = f"{LOCAL_SERVER}/health"
GITHUB_PAGES_URL = "https://1866nazari.github.io/lingodirect-config/config.json"
TUNNEL_COMMAND = "ssh -R 80:127.0.0.1:5000 nokey@localhost.run"
STARTUP_TIMEOUT = 30  # Increased timeout for a fresh connection
RENEWAL_INTERVAL = 600  # هر 10 دقیقه یکبار تونل را ری استارت و زنده می کند (600 ثانیه)


# --- Utility Functions ---

def run_command(command, cwd=None):
    """Executes a shell command."""
    result = subprocess.run(
        command,
        cwd=cwd,
        shell=True,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def is_server_alive():
    """Checks if the local Flask server is running using the new health endpoint."""
    try:
        # ارسال User-Agent اختصاصی برای تشخیص در لاگ‌های فلسک
        headers = {"User-Agent": "Tunnel-Monitor/1.0"}
        response = requests.get(HEALTH_CHECK_URL, headers=headers, timeout=3)
        return response.status_code == 200
    except requests.RequestException:
        print("Local Flask server is not responding.")
        return False


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
    """Writes the new URL to the local config file while preserving existing settings."""
    # ۱. تعریف ساختار پیش‌فرض در صورتی که فایل هنوز ایجاد نشده یا خراب باشد
    default_data = {
        "server": {
            "baseUrl": new_url,
            "status": "online"
        },
        "app_management": {
            "latest_version": {
                "versionCode": 2,
                "versionName": "1.0.1",
                "apkUrl": "https://github.com/your-repo/releases/download/v1.0.1/app.apk",
                "isCritical": False
            },
            "access_control": {
                "max_users": 10,
                "enforce_limit": True,
                "blocked_devices": [],
                "registered_devices": []
            }
        },
        "base_url": new_url
    }

    data = default_data

    # ۲. تلاش برای خواندن اطلاعات موجود و حفظ ساختار قبلی
    if CONFIG_PATH.exists():
        try:
            existing_content = CONFIG_PATH.read_text(encoding="utf-8")
            if existing_content.strip():
                loaded_data = json.loads(existing_content)
                if isinstance(loaded_data, dict):
                    data = loaded_data
                    
                    # به‌روزرسانی فقط فیلدهای آدرس و وضعیت سرور
                    data["base_url"] = new_url
                    
                    if "server" not in data or not isinstance(data["server"], dict):
                        data["server"] = {}
                    data["server"]["baseUrl"] = new_url
                    data["server"]["status"] = "online"
                    
                    # اطمینان از وجود بخش مدیریت اپلیکیشن بدون دست زدن به مقادیر آن
                    if "app_management" not in data:
                        data["app_management"] = default_data["app_management"]
                        
                else:
                    print("Warning: config.json format is not a dictionary. Overwriting with default.")
        except json.JSONDecodeError:
            print("Warning: config.json is corrupted. Rebuilding with default structure.")
        except Exception as e:
            print(f"Warning: Failed to read existing config.json: {e}")

    # ۳. ذخیره‌سازی نهایی فایل با حفظ فرمت زیبا و متون یونیکد (فارسی)
    try:
        CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"-> Local config updated successfully. Preserved existing configurations.")
    except Exception as e:
        print(f"Error saving config.json: {e}")


def get_public_url():
    """Reads the current URL from the GitHub Pages config file."""
    try:
        response = requests.get(GITHUB_PAGES_URL, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return data.get("base_url")
    except (requests.RequestException, ValueError):
        pass

    return None


def commit_and_push(new_url):
    """Updates config.json, keeps tunnel URL in a single amendable commit, and pushes safely on current branch."""
    print("Starting Git update process...")

    TUNNEL_COMMIT_PREFIX = "TunnelURL:"
    TUNNEL_COMMIT_MESSAGE = f"{TUNNEL_COMMIT_PREFIX} update active tunnel endpoint"

    # 1. Get current branch (instead of forcing main)
    code, out, err = run_command("git branch --show-current")
    current_branch = (out or "").strip()
    print(f"-> Git: Working on branch: {current_branch}")

    # 2. Pull latest changes safely
    print(f"-> Git: Pulling latest changes on {current_branch}...")
    # ابتدا بررسی می‌کنیم که آیا این شاخه در ریموت وجود دارد یا خیر
    code, out, err = run_command(f"git ls-remote --heads origin {current_branch}")
    if (out or "").strip():
        # اگر شاخه در ریموت وجود دارد، pull انجام بده
        code, out, err = run_command(f"git pull --ff-only origin {current_branch}")
        if code != 0:
            print("Git pull failed:", out or err)
            return False
    else:
        print("-> Git: Remote branch does not exist yet. Skipping pull.")

    # 3. Save new URL locally
    save_url(new_url)

    # Stage
    run_command("git add config.json")
    print(f"-> Git: Staging file with URL: {new_url}")

    # If nothing changed, skip
    code, out, err = run_command("git diff --cached --name-only")
    staged = (out or "").strip()
    if not staged:
        print("-> Git: No staged changes. Skipping commit/push.")
        return True

    # 4. Amend or Commit
    code, out, err = run_command('git log -1 --pretty=%B')
    last_msg = (out or "").strip()

    if last_msg.startswith(TUNNEL_COMMIT_PREFIX):
        # Amend
        print("-> Git: Amending previous tunnel commit...")
        code, out, err = run_command(f'git commit --amend -m "{TUNNEL_COMMIT_MESSAGE}"')
        
        print(f"-> Git: Pushing amended commit to origin/{current_branch}...")
        code, out, err = run_command(f"git push --force-with-lease origin {current_branch}")
        if code != 0:
            print("Git push failed:", out or err)
            return False
        return True

    else:
        # Create new
        print("-> Git: Creating a new dedicated tunnel commit...")
        code, out, err = run_command(f'git commit -m "{TUNNEL_COMMIT_MESSAGE}"')
        
        print(f"-> Git: Pushing to origin/{current_branch}...")
        code, out, err = run_command(f"git push origin {current_branch}")
        if code != 0:
            print("Git push failed:", out or err)
            return False
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
    # Use Popen to run in the background
    process = subprocess.Popen(
        TUNNEL_COMMAND,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1, # Line buffering
    )
    return process


def shutdown_process(process):
    """Safely terminates the SSH tunnel process."""
    if process.poll() is not None:
        return

    try:
        process.terminate()
        process.wait(timeout=3)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=3)
        except Exception:
            pass


def monitor_tunnel_renewal():
    """
    Implements the proactive renewal strategy with tagged health checks.
    """
    print(f"Starting Proactive Tunnel Renewal Loop (Interval: {RENEWAL_INTERVAL}s)...")
    
    process = None
    attempt_id = 0 # شمارنده برای تشخیص در لاگ فلسک

    while True:
        attempt_id += 1
        # --- 1. Shut down existing tunnel ---
        if process:
            print(f"[{attempt_id}] Shutting down old tunnel for renewal...")
            shutdown_process(process)
            time.sleep(2)

        # --- 2. Check local server health (با ارسال شناسه تلاش) ---
        try:
            # ارسال شماره تلاش در User-Agent
            headers = {"User-Agent": f"Tunnel-Monitor/1.0 (Attempt-{attempt_id})"}
            response = requests.get(HEALTH_CHECK_URL, headers=headers, timeout=3)
            server_ok = (response.status_code == 200)
        except:
            server_ok = False

        if not server_ok:
            print(f"[{attempt_id}] Flask server offline. Retrying in {RENEWAL_INTERVAL}s...")
            time.sleep(RENEWAL_INTERVAL)
            continue
        
        # --- 3. Start new tunnel ---
        print(f"[{attempt_id}] Attempting to start new tunnel...")
        process = start_tunnel()
        public_url = None
        startup_deadline = time.time() + STARTUP_TIMEOUT

        # --- 4. Extract URL ---
        while time.time() < startup_deadline:
            if process.poll() is not None:
                break
            try:
                line = process.stdout.readline()
                if line:
                    line = line.strip()
                    print(f"[{attempt_id}] SSH: {line}")
                    public_url = extract_url(line)
                    if public_url:
                        break
            except Exception:
                pass
            time.sleep(0.5)

        # --- 5. Finalize (ارسال وضعیت بدون تغییر در منطق چرخه اصلی) ---
        if public_url:
            print(f"[{attempt_id}] SUCCESS: {public_url}")
            
            # اطلاع‌رسانی موفقیت به فلسک
            try:
                requests.post(
                    f"{LOCAL_SERVER}/tunnel_status",
                    json={"attempt_id": attempt_id, "status": "SUCCESS", "details": public_url},
                    timeout=2
                )
            except:
                pass

            # ادامه منطق پایدار قبلی شما
            current_url = load_current_url()
            github_url = get_public_url()
            if public_url != current_url or public_url != github_url:
                if not commit_and_push(public_url):
                    print(f"[{attempt_id}] Failed to update GitHub.")
            
            print(f"[{attempt_id}] Waiting {RENEWAL_INTERVAL}s for next renewal...")
            time.sleep(RENEWAL_INTERVAL)
        else:
            print(f"[{attempt_id}] FAILED to get URL. Retrying in 10s...")
            
            # اطلاع‌رسانی شکست به فلسک
            try:
                requests.post(
                    f"{LOCAL_SERVER}/tunnel_status",
                    json={"attempt_id": attempt_id, "status": "FAILED", "details": "Timeout or connection failed"},
                    timeout=2
                )
            except:
                pass

            shutdown_process(process)
            time.sleep(10)


if __name__ == "__main__":
    monitor_tunnel_renewal()
