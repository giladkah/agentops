"""
GitHub Poller — Periodically fetches new issues and PRs from GitHub repos.

No tokens needed for public repos. For private repos, set GITHUB_TOKEN.
Creates Signal objects for each new issue/PR found.
"""
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError


class GitHubPoller:
    """Polls GitHub repos for new issues/PRs and creates signals."""

    def __init__(self, app=None):
        self.app = app
        self.repos = []  # List of {"owner": "...", "repo": "...", "label_filter": [...]}
        self.poll_interval = 120  # seconds
        self._thread = None
        self._running = False
        self._seen_ids = set()  # Track what we've already imported

    @property
    def token(self):
        """Read token fresh each time (in case it was set after startup)."""
        return os.environ.get("GITHUB_TOKEN", "")

    def _github_get(self, url: str) -> list | dict:
        """Make a GitHub API request."""
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "AgentOps-Poller",
        }
        if self.token:
            headers["Authorization"] = f"token {self.token}"

        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            body = e.read().decode() if e.fp else ""
            raise Exception(f"GitHub API {e.code}: {body[:200]}")
        except URLError as e:
            raise Exception(f"GitHub connection error: {e.reason}")

    def add_repo(self, owner: str, repo: str, label_filter: list = None):
        """Add a repo to poll."""
        key = f"{owner}/{repo}"
        # Don't add duplicates
        if any(r["owner"] == owner and r["repo"] == repo for r in self.repos):
            return
        self.repos.append({
            "owner": owner,
            "repo": repo,
            "label_filter": label_filter or [],
        })
        # Save to DB
        if self.app:
            with self.app.app_context():
                from models import Setting
                Setting.set("github_repos", json.dumps(self.repos))
        print(f"🔗 GitHub poller: added {key}")

    def remove_repo(self, owner: str, repo: str):
        """Remove a repo from polling."""
        self.repos = [r for r in self.repos if not (r["owner"] == owner and r["repo"] == repo)]
        if self.app:
            with self.app.app_context():
                from models import Setting
                Setting.set("github_repos", json.dumps(self.repos))

    def load_repos(self):
        """Load saved repos from DB."""
        if self.app:
            with self.app.app_context():
                from models import Setting
                saved = Setting.get("github_repos", "[]")
                try:
                    self.repos = json.loads(saved)
                except (json.JSONDecodeError, TypeError):
                    self.repos = []

    def load_seen_ids(self):
        """Load already-imported GitHub issue IDs from existing signals."""
        if self.app:
            with self.app.app_context():
                from models import Signal
                existing = Signal.query.filter(
                    Signal.source == "github"
                ).with_entities(Signal.source_id).all()
                self._seen_ids = {s.source_id for s in existing if s.source_id}

    def fetch_issues(self, owner: str, repo: str, label_filter: list = None) -> list:
        """Fetch recent open issues from a repo."""
        url = f"https://api.github.com/repos/{owner}/{repo}/issues?state=open&sort=created&direction=desc&per_page=20"

        # Filter by labels if specified
        if label_filter:
            url += "&labels=" + ",".join(label_filter)

        items = self._github_get(url)

        results = []
        for item in items:
            # Skip pull requests (GitHub API returns them mixed with issues)
            if "pull_request" in item:
                continue

            number = item.get("number", "?")
            source_id = f"GH-{number}"

            # Skip already seen
            if source_id in self._seen_ids:
                continue

            labels = [l.get("name", "") for l in item.get("labels", [])]
            body = item.get("body", "") or ""

            # Map severity from labels
            severity = "medium"
            if any(l.lower() in ("bug", "critical", "urgent", "p0", "p1") for l in labels):
                severity = "high"
            if any(l.lower() in ("critical", "urgent", "p0", "blocker") for l in labels):
                severity = "critical"
            elif any(l.lower() in ("enhancement", "feature", "feature-request") for l in labels):
                severity = "medium"
            elif any(l.lower() in ("good first issue", "low", "p3", "nice to have") for l in labels):
                severity = "low"

            # Extract file hints from body
            files = re.findall(r'[\w/]+\.\w{1,4}(?::\d+)?', body)

            title = item.get("title", "GitHub issue")

            results.append({
                "source_id": source_id,
                "title": title,
                "summary": body[:500],
                "severity": severity,
                "files_hint": files[:10],
                "labels": labels,
                "url": item.get("html_url", ""),
                "user": item.get("user", {}).get("login", ""),
                "created_at": item.get("created_at", ""),
                "raw": {
                    "number": number,
                    "title": title,
                    "body": body[:3000],
                    "labels": labels,
                    "user": item.get("user", {}).get("login", ""),
                    "html_url": item.get("html_url", ""),
                    "created_at": item.get("created_at", ""),
                    "assignees": [a.get("login", "") for a in item.get("assignees", [])],
                    "milestone": (item.get("milestone") or {}).get("title"),
                    "repo": f"{owner}/{repo}",
                },
            })

        return results

    def poll_once(self):
        """Poll all repos once and create signals for new items."""
        if not self.repos:
            return []

        created = []
        for repo_config in self.repos:
            owner = repo_config["owner"]
            repo = repo_config["repo"]
            label_filter = repo_config.get("label_filter", [])

            try:
                items = self.fetch_issues(owner, repo, label_filter)
                for item in items:
                    with self.app.app_context():
                        from models import db, Signal

                        signal = Signal(
                            source="github",
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
                        print(f"📨 GitHub signal: {item['title'][:60]} [{item['severity']}] from {owner}/{repo}")

            except Exception as e:
                print(f"⚠️ GitHub poll error for {owner}/{repo}: {e}")

        return created

    def start(self):
        """Start background polling thread."""
        if self._thread and self._thread.is_alive():
            return

        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        repos_str = ", ".join(f"{r['owner']}/{r['repo']}" for r in self.repos) or "none yet"
        print(f"🔄 GitHub poller started (every {self.poll_interval}s) — repos: {repos_str}")

    def stop(self):
        """Stop background polling."""
        self._running = False

    def _poll_loop(self):
        """Background polling loop."""
        # Initial delay to let the app start
        time.sleep(5)

        while self._running:
            try:
                if self.repos:
                    self.poll_once()
            except Exception as e:
                print(f"⚠️ GitHub poll loop error: {e}")

            # Sleep in small chunks so we can stop quickly
            for _ in range(self.poll_interval):
                if not self._running:
                    return
                time.sleep(1)

    def get_status(self) -> dict:
        return {
            "running": self._running and self._thread is not None and self._thread.is_alive(),
            "repos": self.repos,
            "poll_interval": self.poll_interval,
            "seen_count": len(self._seen_ids),
            "has_token": bool(self.token),
        }
