"""
AgentOps Telemetry — Anonymous usage tracking via Aptabase.

What we track:
  - app_started / app_stopped
  - run_started / run_completed / run_failed / run_cancelled
  - ensemble_started / ensemble_completed
  - pr_created
  - signal_received

Privacy design:
  - User ID is a one-way hash of hostname + username — we can tell if the same
    person returns, but cannot identify who they are.
  - No file paths, repo names, task descriptions, or code ever leave the machine.
  - All events fire in a background thread — telemetry never blocks the app.
  - Opt out: set AGENTOPS_TELEMETRY=false, or toggle via the dashboard Settings.

Aptabase docs: https://aptabase.com/docs
"""

import hashlib
import os
import platform
import socket
import threading
import time
import urllib.request
import json
from datetime import datetime, timezone
from typing import Optional


# ── Aptabase config ──────────────────────────────────────────────────────────
# Get your key from https://aptabase.com — create a project, copy the App Key
APTABASE_APP_KEY = "A-US-6856289788"
APTABASE_API_URL = "https://api.aptabase.com/v0/event"

# ── Module state ─────────────────────────────────────────────────────────────
_distinct_id: Optional[str] = None
_enabled: bool = True
_initialized: bool = False
_app_start_time: float = time.time()
_app_version = "0.4.0"


def _get_distinct_id() -> str:
    raw = f"{socket.gethostname()}:{os.getenv('USER', os.getenv('USERNAME', 'unknown'))}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _get_system_props() -> dict:
    return {
        "appVersion": _app_version,
        "osName": platform.system(),
        "osVersion": platform.release(),
    }


def init(app=None):
    global _distinct_id, _enabled, _initialized
    if _initialized:
        return

    env_val = os.getenv("AGENTOPS_TELEMETRY", "").lower()
    if env_val in ("false", "0", "no", "off"):
        _enabled = False
        _initialized = True
        return

    if app:
        try:
            with app.app_context():
                from models import Setting
                db_val = Setting.get("telemetry_enabled", "true")
                if db_val.lower() in ("false", "0", "no", "off"):
                    _enabled = False
                    _initialized = True
                    return
        except Exception:
            pass

    _distinct_id = _get_distinct_id()
    _initialized = True
    _track_bg("app_started", {"session_id": _distinct_id[:8]})


def set_enabled(enabled: bool, app=None):
    global _enabled
    _enabled = enabled
    if app:
        try:
            with app.app_context():
                from models import Setting
                Setting.set("telemetry_enabled", "true" if enabled else "false")
        except Exception:
            pass


def is_enabled() -> bool:
    return _enabled and _initialized


def _send_event(event_name: str, props: dict):
    if "REPLACE" in APTABASE_APP_KEY or not APTABASE_APP_KEY.startswith("A-"):
        return

    payload = json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sessionId": _distinct_id or "unknown",
        "eventName": event_name,
        "props": {**_get_system_props(), **props},
    }).encode("utf-8")

    req = urllib.request.Request(
        APTABASE_API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "App-Key": APTABASE_APP_KEY,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception:
        pass


def _track_bg(event: str, props: dict):
    if not is_enabled():
        return
    t = threading.Thread(target=_send_event, args=(event, props), daemon=True)
    t.start()


def track_run_started(run):
    agents = run.agents if hasattr(run, "agents") else []
    agent_roles = [a.persona.role if a.persona else "unknown" for a in agents]
    _track_bg("run_started", {
        "workflowName": run.workflow.name if run.workflow else "unknown",
        "numAgents": len(agents),
        "agentRoles": ",".join(agent_roles),
        "sourceType": getattr(run, "source_type", "manual"),
        "isEnsembleChild": str(run.ensemble_id is not None if hasattr(run, "ensemble_id") else False),
    })


def track_run_completed(run):
    started = run.started_at
    completed = getattr(run, "finished_at", None) or datetime.now(timezone.utc)
    duration_s = None
    if started:
        if hasattr(started, "tzinfo") and started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if hasattr(completed, "tzinfo") and completed.tzinfo is None:
            completed = completed.replace(tzinfo=timezone.utc)
        try:
            duration_s = int((completed - started).total_seconds())
        except Exception:
            pass
    _track_bg("run_completed", {
        "workflowName": run.workflow.name if run.workflow else "unknown",
        "numAgents": len(run.agents) if hasattr(run, "agents") else 0,
        "durationSeconds": duration_s,
        "totalCostUsd": round(float(run.total_cost or 0), 4),
        "sourceType": getattr(run, "source_type", "manual"),
    })


def track_run_failed(run, reason: str = "unknown"):
    _track_bg("run_failed", {
        "workflowName": run.workflow.name if run.workflow else "unknown",
        "reason": reason,
    })


def track_run_cancelled(run):
    _track_bg("run_cancelled", {
        "workflowName": run.workflow.name if run.workflow else "unknown",
    })


def track_ensemble_started(ensemble):
    _track_bg("ensemble_started", {
        "numRuns": getattr(ensemble, "num_runs", 0),
        "workflowName": ensemble.workflow.name if hasattr(ensemble, "workflow") and ensemble.workflow else "unknown",
    })


def track_ensemble_completed(ensemble):
    started = ensemble.started_at
    completed = getattr(ensemble, "completed_at", None) or datetime.now(timezone.utc)
    duration_s = None
    if started:
        if hasattr(started, "tzinfo") and started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if hasattr(completed, "tzinfo") and completed.tzinfo is None:
            completed = completed.replace(tzinfo=timezone.utc)
        try:
            duration_s = int((completed - started).total_seconds())
        except Exception:
            pass
    _track_bg("ensemble_completed", {
        "numRuns": getattr(ensemble, "num_runs", 0),
        "workflowName": ensemble.workflow.name if hasattr(ensemble, "workflow") and ensemble.workflow else "unknown",
        "durationSeconds": duration_s,
        "totalCostUsd": round(float(ensemble.total_cost or 0), 4),
    })


def track_pr_created(source: str = "single_run"):
    _track_bg("pr_created", {"source": source})


def track_signal_received(source: str):
    _track_bg("signal_received", {"source": source})


def track_app_stopped():
    uptime_s = int(time.time() - _app_start_time)
    _track_bg("app_stopped", {"uptimeSeconds": uptime_s})
    time.sleep(0.5)
