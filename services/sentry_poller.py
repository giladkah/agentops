"""
Sentry Poller — Periodically fetches new issues from Sentry projects.

Requires SENTRY_AUTH_TOKEN env var (or set via Settings).
Creates Signal objects for each new unresolved issue found.

Sentry API docs: https://docs.sentry.io/api/
"""
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError


class SentryPoller:
    """Polls Sentry projects for new issues and creates signals."""

    BASE_URL = "https://sentry.io/api/0"

    def __init__(self, app=None):
        self.app = app
        self.projects = []  # List of {"org": "...", "project": "...", "level_filter": [...]}
        self.poll_interval = 120  # seconds
        self._thread = None
        self._running = False
        self._seen_ids = set()  # Track what we've already imported
        self._base_url = self.BASE_URL  # Can be overridden for self-hosted Sentry

    @property
    def token(self):
        """Read token fresh each time (in case it was set after startup)."""
        return os.environ.get("SENTRY_AUTH_TOKEN", "")

    def _sentry_request(self, method: str, path: str, body: dict = None) -> list | dict:
        """Make a Sentry API request."""
        token = self.token
        if not token:
            raise Exception("No SENTRY_AUTH_TOKEN set")

        url = f"{self._base_url}{path}"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }

        data = json.dumps(body).encode() if body else None
        req = Request(url, headers=headers, data=data, method=method)

        try:
            with urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            resp_body = e.read().decode() if e.fp else ""
            raise Exception(f"Sentry API {e.code}: {resp_body[:300]}")
        except URLError as e:
            raise Exception(f"Sentry connection error: {e.reason}")

    def _sentry_get(self, path: str) -> list | dict:
        return self._sentry_request("GET", path)

    # ── Configuration ──

    def add_project(self, org: str, project: str, level_filter: list = None):
        """Add a Sentry project to poll."""
        if any(p["org"] == org and p["project"] == project for p in self.projects):
            return
        self.projects.append({
            "org": org,
            "project": project,
            "level_filter": level_filter or [],  # e.g. ["error", "fatal"]
        })
        self._save_config()
        print(f"🐛 Sentry poller: added {org}/{project}")

    def remove_project(self, org: str, project: str):
        """Remove a project from polling."""
        self.projects = [p for p in self.projects if not (p["org"] == org and p["project"] == project)]
        self._save_config()

    def _save_config(self):
        if self.app:
            with self.app.app_context():
                from models import Setting
                Setting.set("sentry_projects", json.dumps(self.projects))

    def load_config(self):
        """Load saved config from DB."""
        if self.app:
            with self.app.app_context():
                from models import Setting
                saved = Setting.get("sentry_projects", "[]")
                try:
                    self.projects = json.loads(saved)
                except (json.JSONDecodeError, TypeError):
                    self.projects = []

    def load_seen_ids(self):
        """Load already-imported Sentry issue IDs from existing signals."""
        if self.app:
            with self.app.app_context():
                from models import Signal
                existing = Signal.query.filter(
                    Signal.source == "sentry"
                ).with_entities(Signal.source_id).all()
                self._seen_ids = {s.source_id for s in existing if s.source_id}

    # ── API Operations ──

    def validate_token(self) -> dict:
        """Validate the token and return org info."""
        # List organizations the token has access to
        orgs = self._sentry_get("/organizations/?member=1")
        if not orgs:
            raise Exception("No organizations found — check token permissions")

        result = []
        for org in orgs:
            result.append({
                "slug": org.get("slug", ""),
                "name": org.get("name", org.get("slug", "Unknown")),
            })
        return {"organizations": result}

    def fetch_projects(self, org: str) -> list:
        """Fetch all projects in an organization."""
        projects = self._sentry_get(f"/organizations/{org}/projects/?all_projects=1")
        result = []
        for p in projects:
            result.append({
                "slug": p.get("slug", ""),
                "name": p.get("name", p.get("slug", "")),
                "platform": p.get("platform", ""),
                "status": p.get("status", ""),
            })
        return result

    def fetch_issues(self, org: str, project: str, level_filter: list = None) -> list:
        """Fetch recent unresolved issues from a Sentry project.

        Returns normalized items ready to become Signals.
        """
        # Fetch unresolved issues, sorted by last seen (most recent first)
        # Sentry doesn't support OR in search, so fetch per-level and merge
        if level_filter and len(level_filter) > 1:
            seen_ids_local = set()
            issues = []
            per_level_limit = max(10, 25 // len(level_filter))
            for level in level_filter:
                query = f"is:unresolved level:{level}"
                path = f"/projects/{org}/{project}/issues/?query={_url_encode(query)}&sort=date&limit={per_level_limit}"
                try:
                    batch = self._sentry_get(path)
                    for iss in batch:
                        iid = iss.get("id")
                        if iid and iid not in seen_ids_local:
                            seen_ids_local.add(iid)
                            issues.append(iss)
                except Exception:
                    pass
        else:
            query = "is:unresolved"
            if level_filter:
                query += f" level:{level_filter[0]}"
            path = f"/projects/{org}/{project}/issues/?query={_url_encode(query)}&sort=date&limit=25"
            issues = self._sentry_get(path)

        results = []
        for issue in issues:
            if not isinstance(issue, dict):
                continue

            issue_id = str(issue.get("id", ""))
            source_id = f"SENTRY-{issue_id}"

            # Skip already seen
            if source_id in self._seen_ids:
                continue

            title = issue.get("title", "Sentry issue")
            culprit = issue.get("culprit", "")
            metadata = issue.get("metadata", {})
            level = issue.get("level", "error")
            count = issue.get("count", "0")
            user_count = issue.get("userCount", 0)
            first_seen = issue.get("firstSeen", "")
            last_seen = issue.get("lastSeen", "")
            short_id = issue.get("shortId", "")
            permalink = issue.get("permalink", "")
            platform = issue.get("platform", "")

            # Try to get count as int
            try:
                count = int(count)
            except (ValueError, TypeError):
                count = 0

            # Map severity from level + frequency
            severity_map = {"fatal": "critical", "error": "high", "warning": "medium", "info": "low"}
            severity = severity_map.get(level, "medium")

            # Boost severity for high frequency
            if count > 100 or user_count > 20:
                severity = "critical"
            elif count > 50 or user_count > 10:
                if severity != "critical":
                    severity = "high"

            # Extract file hints from culprit + metadata
            files = []
            if culprit:
                # Culprit often looks like "module.submodule.function" or "file.py:function"
                file_match = re.findall(r'[\w/]+\.\w{1,4}(?::\d+)?', culprit)
                files.extend(file_match)

            # Metadata often has type and value
            error_type = metadata.get("type", "")
            error_value = metadata.get("value", "")
            error_filename = metadata.get("filename", "")
            if error_filename:
                files.append(error_filename)

            # Build rich summary
            summary_parts = []
            if error_type and error_value:
                summary_parts.append(f"{error_type}: {error_value}")
            elif error_value:
                summary_parts.append(error_value)

            if culprit:
                summary_parts.append(f"in {culprit}")

            freq_parts = []
            if count:
                freq_parts.append(f"{count} events")
            if user_count:
                freq_parts.append(f"{user_count} users")
            if freq_parts:
                summary_parts.append(f"({', '.join(freq_parts)})")

            if first_seen:
                summary_parts.append(f"First seen: {first_seen[:10]}")

            summary = " — ".join(summary_parts) if summary_parts else title

            results.append({
                "source_id": source_id,
                "title": title,
                "summary": summary[:500],
                "severity": severity,
                "files_hint": files[:10],
                "level": level,
                "url": permalink,
                "short_id": short_id,
                "created_at": first_seen,
                "raw": {
                    "id": issue_id,
                    "shortId": short_id,
                    "title": title,
                    "culprit": culprit,
                    "level": level,
                    "count": count,
                    "userCount": user_count,
                    "firstSeen": first_seen,
                    "lastSeen": last_seen,
                    "permalink": permalink,
                    "platform": platform,
                    "metadata": metadata,
                    "type": error_type,
                    "value": error_value[:1000] if error_value else "",
                    "project": f"{org}/{project}",
                },
            })

        return results

    def fetch_issue_detail(self, issue_id: str) -> dict:
        """Fetch full issue details including latest event with stack trace."""
        issue = self._sentry_get(f"/issues/{issue_id}/")

        result = {
            "id": issue.get("id"),
            "shortId": issue.get("shortId", ""),
            "title": issue.get("title", ""),
            "culprit": issue.get("culprit", ""),
            "level": issue.get("level", ""),
            "count": issue.get("count", 0),
            "userCount": issue.get("userCount", 0),
            "firstSeen": issue.get("firstSeen", ""),
            "lastSeen": issue.get("lastSeen", ""),
            "permalink": issue.get("permalink", ""),
            "platform": issue.get("platform", ""),
            "metadata": issue.get("metadata", {}),
            "tags": [],
            "stacktrace": None,
        }

        # Extract tags
        for tag in issue.get("tags", []):
            if isinstance(tag, dict):
                result["tags"].append({
                    "key": tag.get("key", ""),
                    "name": tag.get("name", ""),
                    "totalValues": tag.get("totalValues", 0),
                    "topValues": [
                        {"value": v.get("value", ""), "count": v.get("count", 0)}
                        for v in tag.get("topValues", [])[:3]
                        if isinstance(v, dict)
                    ],
                })

        # Fetch latest event for stack trace
        try:
            event = self._sentry_get(f"/issues/{issue_id}/events/latest/")
            if event:
                frames = []
                for entry in event.get("entries", []):
                    if entry.get("type") == "exception":
                        for val in entry.get("data", {}).get("values", []):
                            exc_type = val.get("type", "")
                            exc_value = val.get("value", "")
                            for frame in val.get("stacktrace", {}).get("frames", []):
                                frames.append({
                                    "filename": frame.get("filename", ""),
                                    "function": frame.get("function", ""),
                                    "lineNo": frame.get("lineNo", frame.get("lineno")),
                                    "colNo": frame.get("colNo", frame.get("colno")),
                                    "context": frame.get("context", []),
                                    "inApp": frame.get("inApp", False),
                                    "module": frame.get("module", ""),
                                })
                result["stacktrace"] = {
                    "type": exc_type,
                    "value": exc_value[:2000] if exc_value else "",
                    "frames": frames,
                }

                # Also grab breadcrumbs
                for entry in event.get("entries", []):
                    if entry.get("type") == "breadcrumbs":
                        crumbs = entry.get("data", {}).get("values", [])
                        result["breadcrumbs"] = [
                            {
                                "type": c.get("type", ""),
                                "category": c.get("category", ""),
                                "message": c.get("message", ""),
                                "level": c.get("level", ""),
                                "timestamp": c.get("timestamp", ""),
                            }
                            for c in crumbs[-10:]  # Last 10
                            if isinstance(c, dict)
                        ]

        except Exception as e:
            print(f"⚠️ Sentry: couldn't fetch latest event for {issue_id}: {e}")

        return result

    # ── Polling ──

    def poll_once(self):
        """Poll all configured projects and create signals for new issues."""
        if not self.projects:
            return []

        created = []
        for proj_config in self.projects:
            org = proj_config["org"]
            project = proj_config["project"]
            level_filter = proj_config.get("level_filter", [])

            try:
                items = self.fetch_issues(org, project, level_filter)
                for item in items:
                    with self.app.app_context():
                        from models import db, Signal

                        signal = Signal(
                            source="sentry",
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
                        print(f"🐛 Sentry signal: {item['title'][:60]} [{item['severity']}] from {org}/{project}")

            except Exception as e:
                print(f"⚠️ Sentry poll error for {org}/{project}: {e}")

        return created

    def start(self):
        """Start background polling thread."""
        if self._thread and self._thread.is_alive():
            return

        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        projects_str = ", ".join(f"{p['org']}/{p['project']}" for p in self.projects) or "none yet"
        print(f"🔄 Sentry poller started (every {self.poll_interval}s) — projects: {projects_str}")

    def stop(self):
        """Stop background polling."""
        self._running = False

    def _poll_loop(self):
        """Background polling loop."""
        time.sleep(5)

        while self._running:
            try:
                if self.projects:
                    self.poll_once()
            except Exception as e:
                print(f"⚠️ Sentry poll loop error: {e}")

            for _ in range(self.poll_interval):
                if not self._running:
                    return
                time.sleep(1)

    def get_status(self) -> dict:
        return {
            "running": self._running and self._thread is not None and self._thread.is_alive(),
            "projects": self.projects,
            "poll_interval": self.poll_interval,
            "seen_count": len(self._seen_ids),
            "has_token": bool(self.token),
        }


def _url_encode(s: str) -> str:
    """Simple URL encoding for query params."""
    from urllib.parse import quote
    return quote(s, safe="")
