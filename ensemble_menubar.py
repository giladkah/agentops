#!/usr/bin/env python3
"""
Ensemble — Mac Menubar App

A lightweight menubar app that:
  - Lives in the Mac menubar as a small icon
  - Starts/stops the Ensemble Flask server in the background
  - Opens the dashboard in the default browser
  - Shows active run count in the menubar
  - Handles first-run setup (repo picker + API key)

Requirements:
  pip install rumps requests

Usage:
  python ensemble_menubar.py

To package as a standalone .app:
  pip install py2app
  python setup_menubar.py py2app
"""

import os
import sys
import threading
import time
import subprocess
import webbrowser
import json
import keyring  # pip install keyring

try:
    import rumps
except ImportError:
    print("Please install rumps: pip install rumps")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("Please install requests: pip install requests")
    sys.exit(1)


# ── Config ───────────────────────────────────────────────────────────────────
APP_NAME = "Ensemble"
DASHBOARD_URL = "http://localhost:5050"
API_BASE = f"{DASHBOARD_URL}/api"
PORT = 5050
KEYCHAIN_SERVICE = "ensemble-agentops"
KEYCHAIN_KEY_ANTHROPIC = "anthropic_api_key"
PREFS_FILE = os.path.expanduser("~/.ensemble/prefs.json")


def load_prefs() -> dict:
    try:
        with open(PREFS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_prefs(prefs: dict):
    os.makedirs(os.path.dirname(PREFS_FILE), exist_ok=True)
    with open(PREFS_FILE, "w") as f:
        json.dump(prefs, f, indent=2)


def get_api_key() -> str:
    """Read Anthropic API key from Keychain or env."""
    # 1. Env var (for power users / CI)
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    # 2. macOS Keychain
    try:
        key = keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_KEY_ANTHROPIC)
        return key or ""
    except Exception:
        return ""


def set_api_key(key: str):
    try:
        keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_KEY_ANTHROPIC, key)
    except Exception:
        pass


# ── Server management ────────────────────────────────────────────────────────

class ServerManager:
    def __init__(self):
        self.process = None
        self._lock = threading.Lock()

    def is_running(self) -> bool:
        if self.process and self.process.poll() is None:
            return True
        # Also check if port is listening (externally started server)
        try:
            r = requests.get(f"{API_BASE}/stats", timeout=1)
            return r.status_code == 200
        except Exception:
            return False

    def start(self, repo_path: str, api_key: str) -> bool:
        with self._lock:
            if self.is_running():
                return True

            script_dir = os.path.dirname(os.path.abspath(__file__))
            app_py = os.path.join(script_dir, "app.py")

            env = os.environ.copy()
            env["ANTHROPIC_API_KEY"] = api_key

            try:
                self.process = subprocess.Popen(
                    [sys.executable, app_py, "--repo", repo_path, "--port", str(PORT)],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=script_dir,
                )
                # Wait up to 5s for it to become responsive
                for _ in range(10):
                    time.sleep(0.5)
                    if self.is_running():
                        return True
                return False
            except Exception as e:
                print(f"Failed to start server: {e}")
                return False

    def stop(self):
        with self._lock:
            if self.process:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                self.process = None

    def get_stats(self) -> dict:
        try:
            r = requests.get(f"{API_BASE}/stats", timeout=2)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return {}


# ── Menubar App ──────────────────────────────────────────────────────────────

class EnsembleMenubar(rumps.App):
    def __init__(self):
        super().__init__(APP_NAME, title="⚡", quit_button=None)

        self.server = ServerManager()
        self.prefs = load_prefs()
        self._poll_timer = None

        # Build menu items
        self.status_item = rumps.MenuItem("● Not running", callback=None)
        self.status_item.set_callback(None)

        self.open_item = rumps.MenuItem("Open Dashboard", callback=self.open_dashboard)
        self.toggle_item = rumps.MenuItem("Start Server", callback=self.toggle_server)
        self.repo_item = rumps.MenuItem(
            f"Repo: {self._short_path(self.prefs.get('repo_path', 'Not set'))}",
            callback=self.pick_repo
        )
        self.apikey_item = rumps.MenuItem("Set API Key…", callback=self.set_api_key_dialog)
        self.quit_item = rumps.MenuItem("Quit Ensemble", callback=self.quit_app)

        self.menu = [
            self.status_item,
            None,  # separator
            self.open_item,
            self.toggle_item,
            None,
            self.repo_item,
            self.apikey_item,
            None,
            self.quit_item,
        ]

        # Auto-start if we have everything configured
        if self.prefs.get("repo_path") and get_api_key():
            threading.Thread(target=self._auto_start, daemon=True).start()

        # Start polling for status
        self._poll_timer = rumps.Timer(self._update_status, 5)
        self._poll_timer.start()

    # ── Helpers ──

    def _short_path(self, path: str) -> str:
        if not path or path == "Not set":
            return "Not set"
        home = os.path.expanduser("~")
        if path.startswith(home):
            return "~" + path[len(home):]
        return path

    def _auto_start(self):
        time.sleep(1)  # Let the menubar finish loading
        self._start_server()

    # ── Menu callbacks ──

    def open_dashboard(self, _):
        if self.server.is_running():
            webbrowser.open(DASHBOARD_URL)
        else:
            rumps.alert(
                title="Server not running",
                message="Start the server first using 'Start Server'.",
                ok="OK"
            )

    def toggle_server(self, sender):
        if self.server.is_running():
            self._stop_server()
        else:
            self._start_server()

    def pick_repo(self, _):
        """Open a folder picker dialog using osascript."""
        script = '''
        tell application "System Events"
            activate
        end tell
        tell application "Finder"
            set chosen to choose folder with prompt "Select your git repository:"
        end tell
        return POSIX path of chosen
        '''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                path = result.stdout.strip().rstrip("/")
                self.prefs["repo_path"] = path
                save_prefs(self.prefs)
                self.repo_item.title = f"Repo: {self._short_path(path)}"
                rumps.notification(
                    title=APP_NAME,
                    subtitle="Repository updated",
                    message=self._short_path(path)
                )
        except Exception:
            pass

    def set_api_key_dialog(self, _):
        """Prompt for Anthropic API key and save to Keychain."""
        window = rumps.Window(
            message="Enter your Anthropic API key:",
            title=f"{APP_NAME} — API Key",
            default_text=get_api_key() or "sk-ant-...",
            ok="Save",
            cancel="Cancel",
            dimensions=(400, 20),
        )
        response = window.run()
        if response.clicked and response.text.strip():
            key = response.text.strip()
            set_api_key(key)
            rumps.notification(
                title=APP_NAME,
                subtitle="API key saved",
                message="Stored securely in macOS Keychain"
            )

    def quit_app(self, _):
        self._stop_server()
        rumps.quit_application()

    # ── Server control ──

    def _start_server(self):
        repo_path = self.prefs.get("repo_path")
        api_key = get_api_key()

        if not repo_path:
            rumps.alert(
                title="No repository selected",
                message="Please select a git repository first.",
                ok="OK"
            )
            return

        if not api_key:
            rumps.alert(
                title="No API key",
                message="Please set your Anthropic API key first.",
                ok="OK"
            )
            return

        self.title = "⚡ …"
        self.toggle_item.title = "Starting…"
        self.status_item.title = "⏳ Starting server…"

        def _start():
            ok = self.server.start(repo_path, api_key)
            if ok:
                self.title = "⚡"
                self.toggle_item.title = "Stop Server"
                self.status_item.title = "● Running — localhost:5050"
                rumps.notification(
                    title=APP_NAME,
                    subtitle="Server started",
                    message="Dashboard ready at localhost:5050"
                )
                webbrowser.open(DASHBOARD_URL)
            else:
                self.title = "⚡"
                self.toggle_item.title = "Start Server"
                self.status_item.title = "● Not running"
                rumps.alert(
                    title="Failed to start",
                    message="Could not start the Ensemble server. Check your repo path and API key.",
                    ok="OK"
                )

        threading.Thread(target=_start, daemon=True).start()

    def _stop_server(self):
        self.server.stop()
        self.title = "⚡"
        self.toggle_item.title = "Start Server"
        self.status_item.title = "● Not running"

    # ── Status polling ──

    def _update_status(self, _):
        if not self.server.is_running():
            self.title = "⚡"
            self.toggle_item.title = "Start Server"
            self.status_item.title = "● Not running"
            return

        self.toggle_item.title = "Stop Server"

        stats = self.server.get_stats()
        if not stats:
            self.status_item.title = "● Running"
            self.title = "⚡"
            return

        active = stats.get("active_runs", 0) + stats.get("active_ensembles", 0)
        total = stats.get("total_runs", 0)
        cost = stats.get("total_cost", 0)

        if active > 0:
            self.title = f"⚡ {active}"
            self.status_item.title = f"● Running — {active} active run{'s' if active != 1 else ''}"
        else:
            self.title = "⚡"
            self.status_item.title = f"● Idle — {total} runs, ${cost:.2f} total"


def main():
    # Check we're on macOS
    if sys.platform != "darwin":
        print("The menubar app only runs on macOS. Use 'python app.py' directly on other platforms.")
        sys.exit(1)

    # Check for first-run setup
    prefs = load_prefs()
    if not prefs.get("repo_path") or not get_api_key():
        rumps.alert(
            title=f"Welcome to {APP_NAME}",
            message="To get started, you'll need to:\n1. Select your git repository\n2. Enter your Anthropic API key\n\nYou can set these from the menubar icon.",
            ok="Let's go",
        )

    app = EnsembleMenubar()
    app.run()


    @rumps.clicked("Add repository")
    def add_repo_dialog(self, _):
        """Add a new repository via dialog."""
        n = rumps.Window(
            message="Display name (e.g. shield-api):",
            title="Add Repository", default_text="",
            ok="Next", cancel="Cancel", dimensions=(300, 24),
        ).run()
        if not n.clicked or not n.text.strip():
            return
        p = rumps.Window(
            message="Full local path to the git repo:",
            title="Add Repository", default_text=os.path.expanduser("~/code/"),
            ok="Add", cancel="Cancel", dimensions=(420, 24),
        ).run()
        if not p.clicked or not p.text.strip():
            return
        try:
            import requests as _req
            r = _req.post("http://localhost:5050/api/repos",
                          json={"name": n.text.strip(), "path": p.text.strip()}, timeout=5)
            if r.ok:
                rumps.notification("Ensemble", "Repository added", n.text.strip())
            else:
                rumps.alert("Error: " + r.json().get("error", "unknown"))
        except Exception as exc:
            rumps.alert(f"Error: {exc}")


if __name__ == "__main__":
    main()
