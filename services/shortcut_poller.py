"""
Shortcut Poller — Periodically fetches new stories from Shortcut workspaces.

Requires SHORTCUT_API_TOKEN env var.
Creates Signal objects for each new story found.
"""
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError


class ShortcutPoller:
    """Polls Shortcut for new stories and creates signals."""

    BASE_URL = "https://api.app.shortcut.com/api/v3"

    def __init__(self, app=None):
        self.app = app
        self.workspaces = []  # List of {"name": "...", "query": "..."}
        self.poll_interval = 120  # seconds
        self._thread = None
        self._running = False
        self._seen_ids = set()  # Track what we've already imported
        self._workflow_cache = None  # Cache {state_id: {name, type, num_stories}}
        self._workflow_cache_time = 0

    @property
    def token(self):
        """Read token fresh each time (in case it was set after startup)."""
        return os.environ.get("SHORTCUT_API_TOKEN", "")

    def _sc_request(self, method: str, path: str, body: dict = None) -> list | dict:
        """Make a Shortcut API request."""
        token = self.token
        if not token:
            raise Exception("No SHORTCUT_API_TOKEN set")

        url = f"{self.BASE_URL}{path}"
        headers = {
            "Content-Type": "application/json",
            "Shortcut-Token": token,
        }

        data = json.dumps(body).encode() if body else None
        req = Request(url, headers=headers, data=data, method=method)

        try:
            with urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            resp_body = e.read().decode() if e.fp else ""
            raise Exception(f"Shortcut API {e.code}: {resp_body[:200]}")
        except URLError as e:
            raise Exception(f"Shortcut connection error: {e.reason}")

    def _sc_get(self, path: str) -> list | dict:
        return self._sc_request("GET", path)

    def _sc_post(self, path: str, body: dict = None) -> list | dict:
        return self._sc_request("POST", path, body)

    def add_workspace(self, name: str, query: str = ""):
        """Add a workspace/query to poll."""
        # Don't add duplicates
        if any(w["name"] == name for w in self.workspaces):
            return
        self.workspaces.append({
            "name": name,
            "query": query or "",
        })
        self._save_config()
        print(f"🎯 Shortcut poller: added workspace '{name}' (query: '{query or 'all recent'}')")

    def remove_workspace(self, name: str):
        """Remove a workspace from polling."""
        self.workspaces = [w for w in self.workspaces if w["name"] != name]
        self._save_config()

    def update_query(self, name: str, query: str):
        """Update the search query for a workspace."""
        for w in self.workspaces:
            if w["name"] == name:
                w["query"] = query
                self._save_config()
                return

    def _save_config(self):
        if self.app:
            with self.app.app_context():
                from models import Setting
                Setting.set("shortcut_workspaces", json.dumps(self.workspaces))

    def load_config(self):
        """Load saved config from DB."""
        if self.app:
            with self.app.app_context():
                from models import Setting
                saved = Setting.get("shortcut_workspaces", "[]")
                try:
                    self.workspaces = json.loads(saved)
                except (json.JSONDecodeError, TypeError):
                    self.workspaces = []

    def load_seen_ids(self):
        """Load already-imported Shortcut story IDs from existing signals."""
        if self.app:
            with self.app.app_context():
                from models import Signal
                existing = Signal.query.filter(
                    Signal.source == "shortcut"
                ).with_entities(Signal.source_id).all()
                self._seen_ids = {s.source_id for s in existing if s.source_id}

    def validate_token(self) -> dict:
        """Validate the token by fetching current member info."""
        member = self._sc_get("/member")
        return {
            "name": member.get("profile", {}).get("name", "Unknown"),
            "workspace": member.get("workspace2", {}).get("name", "Unknown"),
        }

    def fetch_workflows(self, force: bool = False) -> dict:
        """Fetch workflow states and return mapping of state_id -> {name, type, num_stories}.
        Cached for 5 minutes."""
        now = time.time()
        if not force and self._workflow_cache and (now - self._workflow_cache_time) < 300:
            return self._workflow_cache

        workflows = self._sc_get("/workflows")
        state_map = {}
        for wf in workflows:
            for state in wf.get("states", []):
                state_map[state["id"]] = {
                    "name": state.get("name", ""),
                    "type": state.get("type", ""),  # backlog, unstarted, started, done
                    "num_stories": state.get("num_stories", 0),
                }
        self._workflow_cache = state_map
        self._workflow_cache_time = now
        return state_map

    def fetch_stories(self, query: str = "", include_backlog: bool = False) -> list:
        """Fetch active stories from Shortcut, filtered by workflow state.

        Fetches by state to avoid pulling all 3000+ stories.
        Default: To Do + In Progress + In Review + Ready for Testing + In Testing.
        Set include_backlog=True to also include Backlog stories.
        """
        # Get workflow states
        state_map = self.fetch_workflows()

        # Determine which state types to include
        include_types = {"unstarted", "started"}  # To Do + In Progress/Review/Testing
        if include_backlog:
            include_types.add("backlog")

        # Parse query for additional filters
        story_type_filter = None
        if query:
            q = query.lower().strip()
            if "bug" in q:
                story_type_filter = "bug"
            elif "feature" in q:
                story_type_filter = "feature"
            elif "chore" in q:
                story_type_filter = "chore"
            if "backlog" in q:
                include_types.add("backlog")

        # Collect active state IDs
        active_state_ids = [
            sid for sid, info in state_map.items()
            if info["type"] in include_types
        ]

        if not active_state_ids:
            return []

        # Fetch stories for each active state
        all_stories = []
        for state_id in active_state_ids:
            state_info = state_map[state_id]
            search_body = {
                "archived": False,
                "workflow_state_id": state_id,
            }
            if story_type_filter:
                search_body["story_type"] = story_type_filter

            try:
                stories = self._sc_post("/stories/search", search_body)
                if isinstance(stories, list):
                    print(f"  📋 {state_info['name']}: {len(stories)} stories")
                    all_stories.extend(stories)
            except Exception as e:
                print(f"  ⚠️ Failed to fetch state {state_info['name']}: {e}")

        print(f"🎯 Shortcut: {len(all_stories)} total active stories across {len(active_state_ids)} states")

        results = []
        for story in all_stories:
            if not isinstance(story, dict):
                continue

            story_id = story.get("id", "?")
            source_id = f"SC-{story_id}"

            # Skip already seen
            if source_id in self._seen_ids:
                continue

            name = story.get("name", "Shortcut story")
            description = story.get("description", "") or ""
            story_type = story.get("story_type", "feature")
            labels = [l.get("name", "") for l in story.get("labels", []) if isinstance(l, dict)]
            workflow_state = story.get("workflow_state_id")
            workflow_state_name = state_map.get(workflow_state, {}).get("name", "") if workflow_state else ""
            epic_id = story.get("epic_id")

            # Map severity from story type + labels
            severity = "medium"
            if story_type == "bug":
                severity = "high"
            if any(l.lower() in ("urgent", "p0", "p1", "critical", "blocker") for l in labels):
                severity = "critical"
            elif any(l.lower() in ("p2", "high", "important") for l in labels):
                severity = "high"
            elif any(l.lower() in ("p3", "low", "nice to have", "minor") for l in labels):
                severity = "low"

            # Extract file hints from description
            files = re.findall(r'[\w/]+\.\w{1,4}(?::\d+)?', description)

            # Build summary with useful context
            summary_parts = []
            if description:
                summary_parts.append(description[:400])
            else:
                if story_type != "feature":
                    summary_parts.append(f"[{story_type}]")
                if labels:
                    summary_parts.append(f"Labels: {', '.join(labels)}")
            summary = " ".join(summary_parts) if summary_parts else name

            results.append({
                "source_id": source_id,
                "title": name,
                "summary": summary[:500],
                "severity": severity,
                "files_hint": files[:10],
                "labels": labels,
                "story_type": story_type,
                "url": story.get("app_url", ""),
                "created_at": story.get("created_at", ""),
                "raw": {
                    "id": story_id,
                    "name": name,
                    "description": description[:3000],
                    "story_type": story_type,
                    "labels": labels,
                    "app_url": story.get("app_url", ""),
                    "created_at": story.get("created_at", ""),
                    "updated_at": story.get("updated_at", ""),
                    "owners": story.get("owner_ids", []),
                    "epic_id": epic_id,
                    "workflow_state_id": workflow_state,
                    "workflow_state_name": workflow_state_name,
                    "estimate": story.get("estimate"),
                    "tasks": [t.get("description", "") for t in story.get("tasks", []) if isinstance(t, dict)],
                },
            })

        return results

    def poll_once(self):
        """Poll all configured workspaces and create signals for new stories."""
        if not self.workspaces:
            return []

        created = []
        for ws_config in self.workspaces:
            query = ws_config.get("query", "")

            try:
                items = self.fetch_stories(query)
                for item in items:
                    with self.app.app_context():
                        from models import db, Signal

                        signal = Signal(
                            source="shortcut",
                            source_id=item["source_id"],
                            title=item["title"],
                            summary=item["summary"],
                            severity=item["severity"],
                            files_hint=json.dumps(item["files_hint"]),
                            raw_payload=json.dumps(item["raw"]),
                            status="new",
                        )
                        db.session.add(signal)
                        db.session.commit()

                        self._seen_ids.add(item["source_id"])
                        created.append(signal.to_dict())
                        print(f"🎯 Shortcut signal: {item['title'][:60]} [{item['severity']}]")

            except Exception as e:
                print(f"⚠️ Shortcut poll error: {e}")

        return created

    def start(self):
        """Start background polling thread."""
        if self._thread and self._thread.is_alive():
            return

        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        print(f"🔄 Shortcut poller started (every {self.poll_interval}s)")

    def stop(self):
        """Stop background polling."""
        self._running = False

    def _poll_loop(self):
        """Background polling loop."""
        time.sleep(5)

        while self._running:
            try:
                if self.workspaces:
                    self.poll_once()
            except Exception as e:
                print(f"⚠️ Shortcut poll loop error: {e}")

            for _ in range(self.poll_interval):
                if not self._running:
                    return
                time.sleep(1)

    def get_status(self) -> dict:
        status = {
            "running": self._running and self._thread is not None and self._thread.is_alive(),
            "workspaces": self.workspaces,
            "poll_interval": self.poll_interval,
            "seen_count": len(self._seen_ids),
            "has_token": bool(self.token),
        }
        # Include workflow state info if token available
        try:
            if self.token:
                state_map = self.fetch_workflows()
                states = []
                for sid, info in sorted(state_map.items()):
                    if info["type"] not in ("done",):  # Show everything except Done
                        states.append({
                            "id": sid,
                            "name": info["name"],
                            "type": info["type"],
                            "count": info["num_stories"],
                        })
                status["workflow_states"] = states
        except Exception:
            pass
        return status


def _url_encode(s: str) -> str:
    """Simple URL encoding for query params."""
    from urllib.parse import quote
    return quote(s, safe="")
