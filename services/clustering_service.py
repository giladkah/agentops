"""
Clustering Service — Background daemon that groups related signals into clusters
and triages them as a unit.

Same daemon-thread pattern as SelfHealingService / pollers.
Owns both clustering (Phase A) and cluster triage (Phase B).
"""
import json
import time
import threading
from datetime import datetime, timezone, timedelta

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

from models import db, Signal, SignalCluster, Setting, Repository

# Severity ordering for comparisons
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

DEFAULT_CONFIG = {
    "enabled": False,
    "poll_interval": 60,
    "min_unclustered": 2,
    "max_cluster_size": 10,
}


class ClusteringService:
    """Background daemon that clusters related signals and triages clusters."""

    def __init__(self, app=None, api_key=None):
        self.app = app
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

        import os
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.client = None
        if HAS_ANTHROPIC and self.api_key:
            self.client = anthropic.Anthropic(api_key=self.api_key)

        self._cluster_attempted_ids = set()  # Signal IDs already sent through clustering

        self.stats = {
            "signals_clustered": 0,
            "clusters_created": 0,
            "clusters_triaged": 0,
            "last_tick": None,
            "errors": [],
        }

    # ── Configuration ──

    def get_config(self) -> dict:
        raw = Setting.get("clustering_config", "")
        if raw:
            try:
                return {**DEFAULT_CONFIG, **json.loads(raw)}
            except (json.JSONDecodeError, TypeError):
                pass
        return dict(DEFAULT_CONFIG)

    def set_config(self, config: dict):
        merged = {**self.get_config(), **config}
        Setting.set("clustering_config", json.dumps(merged))
        return merged

    def is_enabled(self) -> bool:
        return self.get_config().get("enabled", True)

    # ── Lifecycle ──

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        config = self.get_config()
        config["enabled"] = True
        self.set_config(config)
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print("🔗 Clustering service started")

    def stop(self):
        config = self.get_config()
        config["enabled"] = False
        self.set_config(config)
        self._running = False
        print("🔗 Clustering service stopped")

    def _loop(self):
        time.sleep(5)  # Initial delay — let other services start
        while self._running:
            try:
                with self.app.app_context():
                    self._tick()
            except Exception as e:
                self._record_error(f"Tick error: {e}")
            # Sleep in small chunks for fast shutdown
            try:
                with self.app.app_context():
                    config = self.get_config()
            except Exception:
                config = DEFAULT_CONFIG
            interval = config.get("poll_interval", 60)
            for _ in range(interval):
                if not self._running:
                    return
                time.sleep(1)

    # ── Core tick ──

    def _tick(self):
        config = self.get_config()
        self.stats["last_tick"] = datetime.now(timezone.utc).isoformat()

        # Phase A: Cluster unclustered signals
        self._phase_cluster(config)

        # Phase B: Triage open clusters
        self._phase_triage_clusters()

    # ── Phase A: AI Clustering ──

    def _phase_cluster(self, config: dict):
        min_unclustered = config.get("min_unclustered", 2)
        max_cluster_size = config.get("max_cluster_size", 10)

        # Grace period: only cluster signals older than 90 seconds
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=90)

        unclustered = Signal.query.filter(
            Signal.cluster_id.is_(None),
            Signal.status.in_(("new", "investigating", "investigated", "ready")),
            Signal.created_at < cutoff,
        ).order_by(Signal.created_at.asc()).all()

        # Filter out signals we already attempted to cluster (avoid re-sending every tick)
        unclustered = [s for s in unclustered if s.id not in self._cluster_attempted_ids]

        if len(unclustered) < min_unclustered:
            return

        if not self.client:
            return

        # Also include existing open clusters as merge targets
        open_clusters = SignalCluster.query.filter_by(status="open").all()
        cluster_descs = []
        for cl in open_clusters:
            if cl.signal_count() >= max_cluster_size:
                continue
            cluster_descs.append({
                "cluster_id": cl.id,
                "title": cl.title,
                "root_cause": (cl.root_cause or "")[:100],
                "severity": cl.severity,
                "repo_id": cl.repo_id or "",
                "signal_count": cl.signal_count(),
            })

        # Process in batches of 30 to keep prompt + response manageable
        BATCH_SIZE = 30
        for batch_start in range(0, len(unclustered), BATCH_SIZE):
            batch = unclustered[batch_start:batch_start + BATCH_SIZE]

            signal_descs = []
            for sig in batch:
                files = sig.get_files_hint()[:3]
                signal_descs.append({
                    "id": sig.id,
                    "source": sig.source,
                    "title": sig.title,
                    "summary": (sig.summary or "")[:150],
                    "severity": sig.severity,
                    "files_hint": files,
                    "repo_id": sig.repo_id or "",
                })

            result = self._ai_cluster_batch(signal_descs, cluster_descs, max_cluster_size)

            # Mark these signals as attempted regardless of result
            for s in batch:
                self._cluster_attempted_ids.add(s.id)
            # Cap memory usage — evict oldest entries if set gets too large
            if len(self._cluster_attempted_ids) > 5000:
                self._cluster_attempted_ids = set(list(self._cluster_attempted_ids)[-2000:])

            if not result:
                continue

            self._apply_cluster_result(result, {s.id for s in batch})

    def _ai_cluster_batch(self, signal_descs: list, cluster_descs: list, max_cluster_size: int) -> dict | None:
        """Send a batch of signals to Claude Haiku for clustering. Returns parsed result or None."""
        prompt = f"""You are analyzing signals (issues/alerts) to group related ones by root cause.

RULES:
- Only cluster signals that share the SAME root cause (not just similar topics)
- Cross-source clustering is ENCOURAGED: a Sentry error and a Shortcut story about the same issue SHOULD be clustered together
- Signals from different sources (sentry, shortcut, github) often describe the same underlying problem — look at titles, summaries, and file paths to find matches
- If two signals have different repo_ids but are clearly about the same root cause, cluster them (repo_id mismatch is OK)
- Each cluster needs at least 2 signals
- Max {max_cluster_size} signals per cluster
- A signal can only belong to one cluster
- If signals don't clearly share a root cause, leave them unclustered
- Use the FULL signal IDs (UUIDs) exactly as given

SIGNALS:
{json.dumps(signal_descs)}

EXISTING OPEN CLUSTERS (you can merge signals into these):
{json.dumps(cluster_descs) if cluster_descs else "[]"}

Return ONLY valid JSON, no markdown fences, no explanation:
{{"clusters":[{{"cluster_id":null,"title":"...","root_cause":"...","severity":"...","signal_ids":["..."]}}],"unclustered":["..."]}}"""

        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()

            # Strip markdown fences if present
            if text.startswith("```"):
                lines = text.split("\n")
                lines = [l for l in lines if not l.startswith("```")]
                text = "\n".join(lines)

            return json.loads(text)
        except json.JSONDecodeError as e:
            self._record_error(f"AI clustering JSON parse error: {e}")
            return None
        except Exception as e:
            self._record_error(f"AI clustering API error: {e}")
            return None

    def _apply_cluster_result(self, result: dict, valid_ids: set):
        """Apply clustering result — create/merge clusters and assign signals."""
        for cluster_data in result.get("clusters", []):
            signal_ids = [sid for sid in cluster_data.get("signal_ids", []) if sid in valid_ids]
            if len(signal_ids) < 2:
                continue

            existing_cluster_id = cluster_data.get("cluster_id")
            cluster = None

            if existing_cluster_id:
                cluster = SignalCluster.query.get(existing_cluster_id)
                if cluster and cluster.status != "open":
                    cluster = None  # Don't merge into non-open clusters

            if not cluster:
                # Determine repo_id from member signals
                member_signals = Signal.query.filter(Signal.id.in_(signal_ids)).all()
                repo_ids = {s.repo_id for s in member_signals if s.repo_id}
                cluster_repo_id = repo_ids.pop() if len(repo_ids) == 1 else None

                cluster = SignalCluster(
                    title=cluster_data.get("title", "Untitled cluster"),
                    root_cause=cluster_data.get("root_cause", ""),
                    severity=cluster_data.get("severity", "medium"),
                    status="open",
                    repo_id=cluster_repo_id,
                )
                db.session.add(cluster)
                db.session.flush()  # Get the ID
                self.stats["clusters_created"] += 1

            # Assign signals to cluster
            for sid in signal_ids:
                sig = Signal.query.get(sid)
                if sig and not sig.cluster_id:
                    sig.cluster_id = cluster.id
                    sig.proposed_run = ""  # Cluster supersedes individual proposed_run
                    self.stats["signals_clustered"] += 1
                    valid_ids.discard(sid)

            # Flush so cluster.signals relationship is up-to-date
            db.session.flush()

            # Merge files_hint from all member signals
            all_files = set()
            for sig in cluster.signals:
                all_files.update(sig.get_files_hint())
            cluster.files_hint = json.dumps(list(all_files)[:20])

            # Take highest severity
            highest = "low"
            for sig in cluster.signals:
                if _SEVERITY_ORDER.get(sig.severity, 3) < _SEVERITY_ORDER.get(highest, 3):
                    highest = sig.severity
            cluster.severity = highest

        db.session.commit()

    # ── Phase B: Cluster Triage ──

    def _phase_triage_clusters(self):
        clusters = SignalCluster.query.filter_by(status="open").all()
        if not clusters or not self.client:
            return

        for cluster in clusters:
            if not self._running:
                return
            try:
                self._triage_cluster(cluster)
            except Exception as e:
                self._record_error(f"Cluster triage error for {cluster.id[:8]}: {e}")

    def _triage_cluster(self, cluster: SignalCluster):
        """Analyze a cluster and generate a proposed run."""
        # Track retry count to avoid infinite retry loops
        try:
            meta = json.loads(cluster.root_cause) if cluster.root_cause and cluster.root_cause.startswith('{') else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        retry_count = meta.get("_retry_count", 0) if isinstance(meta, dict) else 0

        if retry_count >= 3:
            cluster.status = "failed"
            db.session.commit()
            self._record_error(f"Cluster {cluster.id[:8]} failed after {retry_count} retries — marking as failed")
            return

        cluster.status = "triaging"
        db.session.commit()

        # Build combined context from member signals (keep compact)
        signals_context = []
        for sig in cluster.signals:
            signals_context.append({
                "id": sig.id,
                "source": sig.source,
                "source_id": sig.source_id,
                "title": sig.title,
                "summary": (sig.summary or "")[:300],
                "severity": sig.severity,
                "files_hint": sig.get_files_hint()[:5],
            })

        prompt = f"""You are analyzing a cluster of related signals that share a root cause.
Analyze them and propose a single fix run.

CLUSTER:
Title: {cluster.title}
Root Cause Hypothesis: {cluster.root_cause}
Severity: {cluster.severity}

MEMBER SIGNALS:
{json.dumps(signals_context)}

Tasks:
1. Confirm or refine the root cause analysis
2. Identify the files that need to be fixed
3. Propose a single run that addresses ALL signals in this cluster

Return ONLY valid JSON, no markdown fences, no explanation:
{{"summary":"2-3 sentence summary","root_cause":"Refined root cause","proposed_run":{{"workflow_name":"Bug Fix","title":"Short title","task_description":"What to fix and how","model":"haiku"}}}}
"""

        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()

            # Strip markdown fences if present
            if text.startswith("```"):
                lines = text.split("\n")
                lines = [l for l in lines if not l.startswith("```")]
                text = "\n".join(lines)
            result = json.loads(text)
        except json.JSONDecodeError as e:
            self._record_error(f"Cluster triage JSON parse error: {e}")
            # Increment retry count so we don't loop forever
            cluster.status = "open"
            cluster.root_cause = json.dumps({"_retry_count": retry_count + 1, "text": cluster.root_cause or ""})
            db.session.commit()
            return
        except Exception as e:
            self._record_error(f"Cluster triage API error: {e}")
            cluster.status = "open"
            cluster.root_cause = json.dumps({"_retry_count": retry_count + 1, "text": cluster.root_cause or ""})
            db.session.commit()
            return

        # Update cluster
        cluster.summary = result.get("summary", "")
        cluster.root_cause = result.get("root_cause", cluster.root_cause)
        cluster.proposed_run = json.dumps(result.get("proposed_run", {}))
        cluster.status = "ready"
        db.session.commit()

        # Update member signals
        for sig in cluster.signals:
            if sig.status in ("new", "investigating", "investigated"):
                sig.status = "investigated"
        db.session.commit()

        self.stats["clusters_triaged"] += 1
        print(f"🔗 Triaged cluster: {cluster.title}")

    # ── Manual operations ──

    def run_now(self):
        """Trigger one full clustering cycle manually (Phase A + B)."""
        config = self.get_config()
        self._phase_cluster(config)
        self._phase_triage_clusters()

    def triage_cluster(self, cluster_id: str):
        """Trigger triage on a specific cluster."""
        cluster = SignalCluster.query.get(cluster_id)
        if not cluster:
            return {"error": "Cluster not found"}
        if not self.client:
            return {"error": "No Anthropic API key configured"}
        try:
            self._triage_cluster(cluster)
            return {"status": "triaged", "cluster": cluster.to_dict()}
        except Exception as e:
            return {"error": str(e)}

    # ── Helpers ──

    def _record_error(self, msg: str):
        print(f"🔗 ❌ {msg}")
        self.stats["errors"].append({
            "time": datetime.now(timezone.utc).isoformat(),
            "message": msg,
        })
        self.stats["errors"] = self.stats["errors"][-10:]

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "enabled": self.is_enabled(),
            "config": self.get_config(),
            "stats": self.stats,
        }
