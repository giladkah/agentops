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

    _VALID_SEVERITIES = {"critical", "high", "medium", "low"}

    # ── Re-triage / Re-check ──

    _RETRIAGE_BATCH_SIZE = 40  # max findings per AI call

    def retriage_all(self) -> dict:
        """Review ALL open findings in batched AI passes — merge duplicates, close fixed, re-rank severity."""
        if not self.client:
            return {"error": "No Anthropic API key configured"}

        clusters = SignalCluster.query.filter(
            SignalCluster.status.in_(("open", "ready", "triaging"))
        ).all()

        if not clusters:
            return {"actions": [], "reviewed": 0, "message": "No open findings to review"}

        # Build compact descriptions — use short IDs for token efficiency
        id_map = {}  # short_id -> full_id
        findings = []
        for c in clusters:
            # Use 8-char prefix; extend on collision
            short_id = c.id[:8]
            prefix_len = 8
            while short_id in id_map and id_map[short_id] != c.id:
                prefix_len += 4
                short_id = c.id[:prefix_len]
            id_map[short_id] = c.id
            signals_summary = []
            for sig in c.signals[:3]:
                signals_summary.append(f"{sig.source}: {sig.title}")
            findings.append({
                "id": short_id,
                "title": c.title,
                "summary": (c.summary or "")[:150],
                "severity": c.severity,
                "root_cause": (c.root_cause or "")[:120],
                "signals": c.signal_count(),
                "files": c.get_files_hint()[:5],
                "src": signals_summary,
            })

        print(f"🔄 Re-triage: reviewing {len(findings)} findings in batches of {self._RETRIAGE_BATCH_SIZE}...")

        # Process in batches
        all_applied = []
        all_kept = []
        all_skipped = []
        all_summaries = []
        total_reviewed = len(findings)

        for batch_start in range(0, len(findings), self._RETRIAGE_BATCH_SIZE):
            batch = findings[batch_start:batch_start + self._RETRIAGE_BATCH_SIZE]
            batch_num = batch_start // self._RETRIAGE_BATCH_SIZE + 1
            total_batches = (len(findings) + self._RETRIAGE_BATCH_SIZE - 1) // self._RETRIAGE_BATCH_SIZE
            print(f"  📦 Batch {batch_num}/{total_batches}: {len(batch)} findings")

            result = self._retriage_batch(batch, id_map)
            if result.get("error"):
                print(f"  ❌ Batch {batch_num} error: {result['error']}")
                all_skipped.append({"type": "batch_error", "reason": result["error"], "batch": batch_num})
                continue

            all_applied.extend(result.get("applied", []))
            all_kept.extend(result.get("kept", []))
            all_skipped.extend(result.get("skipped", []))
            if result.get("summary"):
                all_summaries.append(result["summary"])

        db.session.commit()
        summary = " | ".join(all_summaries) if all_summaries else ""
        print(f"🔄 Re-triage complete: {len(all_applied)} actions applied, {len(all_kept)} kept, {len(all_skipped)} skipped")
        return {
            "actions": all_applied,
            "kept": all_kept,
            "skipped": all_skipped,
            "summary": summary,
            "reviewed": total_reviewed,
        }

    def _retriage_batch(self, batch: list, id_map: dict) -> dict:
        """Process one batch of findings through the AI."""
        # Pre-compute overlap hints within this batch
        overlap_hints = []
        for i, a in enumerate(batch):
            a_files = set(a.get("files", []))
            a_words = set(a.get("title", "").lower().split())
            for j in range(i + 1, len(batch)):
                b = batch[j]
                b_files = set(b.get("files", []))
                b_words = set(b.get("title", "").lower().split())
                shared_files = a_files & b_files
                shared_words = a_words & b_words - {"the", "a", "an", "in", "to", "of", "and", "or", "is", "for", "with"}
                if shared_files or len(shared_words) >= 3:
                    hint = f"  - [{a['id']}] \"{a['title']}\" <-> [{b['id']}] \"{b['title']}\""
                    if shared_files:
                        hint += f" (shared files: {', '.join(list(shared_files)[:3])})"
                    overlap_hints.append(hint)

        overlap_section = ""
        if overlap_hints:
            overlap_section = f"\nDETECTED OVERLAPS — likely duplicates:\n" + "\n".join(overlap_hints[:20]) + "\n"

        prompt = f"""Review these {len(batch)} open findings. Be AGGRESSIVE about cleanup.

FINDINGS:
{json.dumps(batch)}
{overlap_section}
RULES:
1. MERGE findings about the same problem (same files, same root cause, similar titles). Use the best one as survivor.
2. CLOSE findings that are trivial, cosmetic-only, or low-value noise.
3. SEVERITY: downgrade style/cosmetic to "low", upgrade security/data-loss to "high"/"critical".
4. ONLY return actions that CHANGE something. Do NOT return "keep" — omitted findings are kept automatically.

Return ONLY valid JSON, no markdown wrapping:
{{"actions":[{{"type":"merge","into":"<survivor_short_id>","ids":["<id1>","<id2>"],"reason":"short reason"}},{{"type":"close","ids":["<id>"],"reason":"short reason"}},{{"type":"severity","ids":["<id>"],"sev":"low|medium|high|critical","reason":"short reason"}}],"summary":"1-sentence assessment"}}

Keep reasons under 15 words. Use the 8-char IDs as shown."""

        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=8192,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()

            # Check for truncation
            if response.stop_reason == "max_tokens":
                print(f"    ⚠️ Response truncated — trying to salvage partial JSON")
                text = self._salvage_truncated_json(text)

            if text.startswith("```"):
                lines = text.split("\n")
                lines = [l for l in lines if not l.startswith("```")]
                text = "\n".join(lines)
            result = json.loads(text)
        except json.JSONDecodeError as e:
            print(f"    ❌ JSON parse error: {e}")
            # Try harder to salvage
            try:
                text = self._salvage_truncated_json(text)
                result = json.loads(text)
            except Exception:
                return {"error": f"AI returned invalid JSON: {str(e)[:100]}"}
        except Exception as e:
            return {"error": str(e)}

        raw_actions = result.get("actions", [])
        print(f"    AI returned {len(raw_actions)} change actions")

        # Apply actions — resolve short IDs to full IDs
        applied = []
        skipped = []
        kept = []
        for action in raw_actions:
            a_type = action.get("type")
            # Resolve short IDs to full IDs
            raw_ids = action.get("ids", [])
            ids = [id_map.get(sid, sid) for sid in raw_ids]
            reason = action.get("reason", "")

            if a_type == "keep":
                kept.append({"ids": raw_ids, "reason": reason})
                continue

            if a_type == "merge":
                raw_into = action.get("into", raw_ids[0] if raw_ids else "")
                survivor_id = id_map.get(raw_into, raw_into)
                all_ids = list(ids)
                if survivor_id not in all_ids:
                    all_ids.append(survivor_id)
                if len(all_ids) < 2:
                    skipped.append({"type": "merge", "reason": "insufficient IDs"})
                    continue

                survivor = SignalCluster.query.get(survivor_id)
                if not survivor:
                    skipped.append({"type": "merge", "reason": f"survivor {raw_into} not found"})
                    continue
                merged_ids = []
                for mid in all_ids:
                    if mid == survivor_id:
                        continue
                    merge_c = SignalCluster.query.get(mid)
                    if merge_c:
                        for sig in merge_c.signals:
                            sig.cluster_id = survivor.id
                        survivor.add_chat_message("system", json.dumps({
                            "type": "retriage_merge",
                            "merged_from": merge_c.title,
                            "merged_id": mid,
                            "reason": reason,
                        }))
                        merge_c.add_chat_message("system", json.dumps({
                            "type": "retriage_merged_into",
                            "into_id": survivor_id,
                            "into_title": survivor.title,
                            "reason": reason,
                        }))
                        merge_c.status = "done"
                        merged_ids.append(mid)
                if merged_ids:
                    db.session.flush()
                    applied.append({"type": "merge", "into": survivor_id, "merged": merged_ids, "reason": reason})
                    print(f"    ✅ Merged {len(merged_ids)} into {raw_into}")

            elif a_type == "close":
                closed_ids = []
                for cid in ids:
                    c = SignalCluster.query.get(cid)
                    if c and c.status not in ("done", "running"):
                        c.status = "done"
                        c.add_chat_message("system", json.dumps({
                            "type": "retriage_close",
                            "reason": reason,
                        }))
                        closed_ids.append(cid)
                if closed_ids:
                    applied.append({"type": "close", "ids": closed_ids, "reason": reason})
                    print(f"    ✅ Closed {len(closed_ids)}: {reason[:60]}")

            elif a_type in ("update_severity", "severity"):
                new_sev = action.get("severity", action.get("sev", "medium")).lower()
                if new_sev not in self._VALID_SEVERITIES:
                    new_sev = "medium"
                updated_ids = []
                for cid in ids:
                    c = SignalCluster.query.get(cid)
                    if c and c.severity != new_sev:
                        old_sev = c.severity
                        c.severity = new_sev
                        c.add_chat_message("system", json.dumps({
                            "type": "retriage_severity",
                            "old": old_sev,
                            "new": new_sev,
                            "reason": reason,
                        }))
                        updated_ids.append(cid)
                if updated_ids:
                    applied.append({"type": "update_severity", "ids": updated_ids, "severity": new_sev, "reason": reason})
                    print(f"    ✅ Severity → {new_sev} for {len(updated_ids)}")

        return {"applied": applied, "kept": kept, "skipped": skipped, "summary": result.get("summary", "")}

    @staticmethod
    def _salvage_truncated_json(text: str) -> str:
        """Try to fix truncated JSON by closing open structures."""
        # Find the last complete action object (ends with })
        last_brace = text.rfind("}")
        if last_brace == -1:
            return text
        # Trim to last complete object, close the array and outer object
        trimmed = text[:last_brace + 1]
        # Count open brackets/braces to figure out what needs closing
        opens = trimmed.count("[") - trimmed.count("]")
        braces = trimmed.count("{") - trimmed.count("}")
        trimmed += "]" * max(opens, 0)
        trimmed += "}" * max(braces, 0)
        return trimmed

    def recheck_cluster(self, cluster_id: str) -> dict:
        """Re-investigate a single finding — check if it's still relevant, update severity/summary."""
        if not self.client:
            return {"error": "No Anthropic API key configured"}

        cluster = SignalCluster.query.get(cluster_id)
        if not cluster:
            return {"error": "Finding not found"}

        signals_ctx = []
        for sig in cluster.signals:
            signals_ctx.append({
                "source": sig.source,
                "title": sig.title,
                "summary": (sig.summary or "")[:300],
                "severity": sig.severity,
                "files": sig.get_files_hint()[:5],
            })

        recent_msgs = cluster.get_chat_messages()[-5:]
        chat_ctx = [{"role": m["role"], "content": m["content"][:300]} for m in recent_msgs if m["role"] in ("user", "assistant")]

        prompt = f"""Re-check this finding. Is it still relevant? Should severity change? Is the root cause accurate?

FINDING:
Title: {cluster.title}
Summary: {cluster.summary or 'N/A'}
Severity: {cluster.severity}
Root cause: {cluster.root_cause or 'N/A'}
Status: {cluster.status}

MEMBER SIGNALS ({len(signals_ctx)}):
{json.dumps(signals_ctx)}

RECENT CHAT CONTEXT:
{json.dumps(chat_ctx) if chat_ctx else 'None'}

Return ONLY valid JSON:
{{"still_relevant":true,"severity":"critical|high|medium|low","summary":"updated 1-2 sentence summary","root_cause":"updated root cause","reason":"why you made these changes"}}"""

        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                lines = [l for l in lines if not l.startswith("```")]
                text = "\n".join(lines)
            result = json.loads(text)
        except Exception as e:
            return {"error": str(e)}

        changes = []
        old_sev = cluster.severity
        new_sev = (result.get("severity") or "").lower()
        if new_sev and new_sev in self._VALID_SEVERITIES and new_sev != old_sev:
            cluster.severity = new_sev
            changes.append(f"severity: {old_sev} → {new_sev}")

        if result.get("summary"):
            cluster.summary = result["summary"]
            changes.append("summary updated")

        if result.get("root_cause"):
            cluster.root_cause = result["root_cause"]
            changes.append("root cause updated")

        if not result.get("still_relevant", True):
            cluster.status = "done"
            changes.append("closed as no longer relevant")

        cluster.add_chat_message("system", json.dumps({
            "type": "recheck",
            "changes": changes,
            "reason": result.get("reason", ""),
            "still_relevant": result.get("still_relevant", True),
        }))

        db.session.commit()
        return {"cluster": cluster.to_dict(), "changes": changes, "reason": result.get("reason", "")}

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
