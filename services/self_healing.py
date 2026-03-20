"""
Self-Healing Service — Background daemon that auto-triages signals and auto-launches fix runs.

Follows the same daemon-thread pattern as GitHubPoller / SentryPoller / ShortcutPoller.
"""
import json
import time
import threading
from datetime import datetime, timezone

from models import db, Signal, SignalCluster, Run, Workflow, Persona, Setting


# Severity ordering for comparisons
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

DEFAULT_RULES = {
    "auto_triage": True,
    "auto_launch": True,
    "min_severity": "high",
    "max_concurrent_runs": 3,
    "poll_interval": 30,
    "auto_approve_runs": False,
}


class SelfHealingService:
    """Background daemon that closes the loop: signal → triage → run → PR."""

    def __init__(self, chat_service, orchestrator, app=None):
        self.chat_service = chat_service
        self.orchestrator = orchestrator
        self.app = app

        self._running = False
        self._thread = None
        self._processing = set()  # signal IDs currently being processed
        self._lock = threading.Lock()

        # Stats
        self.stats = {
            "signals_triaged": 0,
            "runs_launched": 0,
            "runs_completed": 0,
            "runs_failed": 0,
            "last_tick": None,
            "errors": [],
        }

    # ── Configuration ──

    def get_rules(self) -> dict:
        raw = Setting.get("self_healing_rules", "")
        if raw:
            try:
                return {**DEFAULT_RULES, **json.loads(raw)}
            except (json.JSONDecodeError, TypeError):
                pass
        return dict(DEFAULT_RULES)

    def set_rules(self, rules: dict):
        merged = {**self.get_rules(), **rules}
        Setting.set("self_healing_rules", json.dumps(merged))
        return merged

    def is_enabled(self) -> bool:
        return Setting.get("self_healing_enabled", "false") == "true"

    # ── Lifecycle ──

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        Setting.set("self_healing_enabled", "true")
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print("🩺 Self-healing service started")

    def stop(self):
        Setting.set("self_healing_enabled", "false")
        self._running = False
        print("🩺 Self-healing service stopped")

    def _loop(self):
        time.sleep(3)  # Initial delay
        while self._running:
            try:
                with self.app.app_context():
                    self._tick()
            except Exception as e:
                self._record_error(f"Tick error: {e}")
            # Sleep in small chunks for fast shutdown
            try:
                with self.app.app_context():
                    rules = self.get_rules()
            except Exception:
                rules = DEFAULT_RULES
            interval = rules.get("poll_interval", 30)
            for _ in range(interval):
                if not self._running:
                    return
                time.sleep(1)

    # ── Core tick ──

    def _tick(self):
        rules = self.get_rules()
        self.stats["last_tick"] = datetime.now(timezone.utc).isoformat()

        # Phase 1: Auto-triage new signals
        if rules.get("auto_triage", True):
            self._phase_triage()

        # Phase 2: Auto-launch runs for triaged signals
        if rules.get("auto_launch", True):
            self._phase_launch(rules)

        # Phase 3: Sync signal status with linked runs
        self._phase_sync()

    # ── Phase 1: Auto-triage ──

    def _phase_triage(self):
        # Skip clustered signals — they're triaged at the cluster level
        signals = Signal.query.filter(
            Signal.status == "new",
            Signal.cluster_id.is_(None),
        ).all()
        for signal in signals:
            if not self._running:
                return  # Bail out early if stopped mid-tick
            if signal.id in self._processing:
                continue
            with self._lock:
                self._processing.add(signal.id)
            try:
                signal.status = "investigating"
                db.session.commit()

                # Step 1: Investigate
                result = self.chat_service.auto_triage(signal.id)
                if result.get("error"):
                    self._record_error(f"Triage failed for {signal.id[:8]}: {result['error']}")
                    continue

                self.stats["signals_triaged"] += 1

                # Step 2: If no proposed_run yet, ask the AI to propose one
                sig = Signal.query.get(signal.id)
                if sig and not sig.get_proposed_run():
                    propose_result = self.chat_service.send_message(
                        signal.id,
                        "Based on your investigation, please propose a run to fix this. Use the propose_run tool or include a PROPOSED_RUN JSON block."
                    )
                    if propose_result.get("error"):
                        self._record_error(f"Propose failed for {signal.id[:8]}: {propose_result['error']}")

                # Update status
                sig = Signal.query.get(signal.id)
                if sig and sig.status == "investigating":
                    sig.status = "investigated"
                    db.session.commit()
                print(f"🩺 Auto-triaged signal: {signal.title[:50]}")
            except Exception as e:
                self._record_error(f"Triage error for {signal.id[:8]}: {e}")
            finally:
                with self._lock:
                    self._processing.discard(signal.id)

    # ── Phase 2: Auto-launch ──

    def _phase_launch(self, rules: dict):
        min_sev = rules.get("min_severity", "high")
        max_concurrent = rules.get("max_concurrent_runs", 3)
        auto_approve = rules.get("auto_approve_runs", False)

        # Count active runs
        active_runs = Run.query.filter(
            Run.status.in_(["running", "needs_approval", "pending"])
        ).count()
        if active_runs >= max_concurrent:
            return

        # Find launchable signals (skip clustered — they launch via cluster)
        signals = Signal.query.filter(
            Signal.status.in_(("ready", "investigated")),
            Signal.proposed_run.isnot(None),
            Signal.proposed_run != "",
            Signal.cluster_id.is_(None),
        ).order_by(Signal.created_at.asc()).all()

        for signal in signals:
            if not self._running:
                return  # Bail out early if stopped mid-tick
            if active_runs >= max_concurrent:
                break
            if signal.id in self._processing:
                continue

            # Check severity threshold
            sig_order = _SEVERITY_ORDER.get(signal.severity, 3)
            min_order = _SEVERITY_ORDER.get(min_sev, 1)
            if sig_order > min_order:
                continue

            with self._lock:
                self._processing.add(signal.id)
            try:
                run = self._create_and_start_run(signal, auto_approve)
                if run:
                    active_runs += 1
                    self.stats["runs_launched"] += 1
                    print(f"🩺 Auto-launched run for signal: {signal.title[:50]}")
            except Exception as e:
                self._record_error(f"Launch error for {signal.id[:8]}: {e}")
            finally:
                with self._lock:
                    self._processing.discard(signal.id)

        # Launch runs for ready clusters
        clusters = SignalCluster.query.filter(
            SignalCluster.status == "ready",
            SignalCluster.proposed_run.isnot(None),
            SignalCluster.proposed_run != "",
        ).order_by(SignalCluster.created_at.asc()).all()

        for cluster in clusters:
            if not self._running:
                return
            if active_runs >= max_concurrent:
                break
            # Check severity threshold
            cl_order = _SEVERITY_ORDER.get(cluster.severity, 3)
            min_order = _SEVERITY_ORDER.get(min_sev, 1)
            if cl_order > min_order:
                continue
            try:
                run = self._create_and_start_cluster_run(cluster, auto_approve)
                if run:
                    active_runs += 1
                    self.stats["runs_launched"] += 1
                    print(f"🩺 Auto-launched cluster run: {cluster.title[:50]}")
            except Exception as e:
                self._record_error(f"Cluster launch error for {cluster.id[:8]}: {e}")

    def _create_and_start_run(self, signal: Signal, auto_approve: bool):
        """Create and start a run from a signal's proposed_run. Mirrors create_run_from_signal logic."""
        proposal = signal.get_proposed_run()
        if not proposal:
            return None

        # Resolve workflow
        wf_id = proposal.get("workflow_id")
        workflow = Workflow.query.get(wf_id) if wf_id else None
        if not workflow:
            wf_name = proposal.get("workflow_name", "")
            if wf_name:
                workflow = Workflow.query.filter(
                    db.func.lower(Workflow.name) == wf_name.lower()
                ).first()
            if not workflow:
                workflow = Workflow.query.first()
            if not workflow:
                self._record_error(f"No workflow found for signal {signal.id[:8]}")
                return None

        # Build agent configs
        try:
            stages = workflow.get_stages()
        except Exception:
            stages = []

        role_map = {
            "plan": "planner",
            "engineer": "engineer",
            "review": "reviewer",
            "security": "security",
            "qa": "qa",
            "test": "test-runner",
        }

        agent_configs = []
        model = proposal.get("model", "haiku")
        for stage in stages:
            stage_name = stage.get("name", "")
            if stage_name == "merge":
                continue
            role = role_map.get(stage_name, stage_name)
            personas = Persona.query.filter_by(role=role).all()
            if not personas and stage_name == "review":
                personas = Persona.query.filter(
                    Persona.role.in_(["reviewer", "security", "architect-reviewer"])
                ).all()
            for persona in personas:
                agent_configs.append({
                    "persona_id": persona.id,
                    "model": model if stage_name == "engineer" else persona.default_model,
                    "stage_name": stage_name,
                })

        if not agent_configs:
            engineer = Persona.query.filter_by(role="engineer").first()
            if engineer:
                agent_configs.append({
                    "persona_id": engineer.id,
                    "model": model,
                    "stage_name": "engineer",
                })
            else:
                self._record_error(f"No engineer persona for signal {signal.id[:8]}")
                return None

        run = self.orchestrator.create_run(
            workflow_id=workflow.id,
            title=proposal.get("title", signal.title),
            task_description=proposal.get("task_description", signal.summary),
            agent_configs=agent_configs,
            source_type=signal.source,
            source_id=signal.source_id or "",
            auto_approve=auto_approve,
        )

        signal.run_id = run.id
        signal.status = "running"
        db.session.commit()

        self.orchestrator.start_run(run.id)
        return run

    def _create_and_start_cluster_run(self, cluster: SignalCluster, auto_approve: bool):
        """Create and start a run from a cluster's proposed_run."""
        proposal = cluster.get_proposed_run()
        if not proposal:
            return None

        # Resolve workflow
        wf_id = proposal.get("workflow_id")
        workflow = Workflow.query.get(wf_id) if wf_id else None
        if not workflow:
            wf_name = proposal.get("workflow_name", "")
            if wf_name:
                workflow = Workflow.query.filter(
                    db.func.lower(Workflow.name) == wf_name.lower()
                ).first()
            if not workflow:
                workflow = Workflow.query.first()
            if not workflow:
                self._record_error(f"No workflow found for cluster {cluster.id[:8]}")
                return None

        try:
            stages = workflow.get_stages()
        except Exception:
            stages = []

        role_map = {
            "plan": "planner", "engineer": "engineer", "review": "reviewer",
            "security": "security", "qa": "qa", "test": "test-runner",
        }

        agent_configs = []
        model = proposal.get("model", "haiku")
        for stage in stages:
            stage_name = stage.get("name", "")
            if stage_name == "merge":
                continue
            role = role_map.get(stage_name, stage_name)
            personas = Persona.query.filter_by(role=role).all()
            if not personas and stage_name == "review":
                personas = Persona.query.filter(
                    Persona.role.in_(["reviewer", "security", "architect-reviewer"])
                ).all()
            for persona in personas:
                agent_configs.append({
                    "persona_id": persona.id,
                    "model": model if stage_name == "engineer" else persona.default_model,
                    "stage_name": stage_name,
                })

        if not agent_configs:
            engineer = Persona.query.filter_by(role="engineer").first()
            if engineer:
                agent_configs.append({"persona_id": engineer.id, "model": model, "stage_name": "engineer"})
            else:
                self._record_error(f"No engineer persona for cluster {cluster.id[:8]}")
                return None

        # Use first member signal's source info
        first_signal = cluster.signals[0] if cluster.signals else None
        source_type = first_signal.source if first_signal else "manual"
        source_id = first_signal.source_id or "" if first_signal else ""

        run = self.orchestrator.create_run(
            workflow_id=workflow.id,
            title=proposal.get("title", cluster.title),
            task_description=proposal.get("task_description", cluster.summary),
            agent_configs=agent_configs,
            source_type=source_type,
            source_id=source_id,
            auto_approve=auto_approve,
        )

        cluster.run_id = run.id
        cluster.status = "running"
        for sig in cluster.signals:
            sig.run_id = run.id
            sig.status = "running"
        db.session.commit()

        self.orchestrator.start_run(run.id)
        return run

    # ── Phase 3: Status sync ──

    def _phase_sync(self):
        # Sync individual signals
        signals = Signal.query.filter(
            Signal.status == "running",
            Signal.cluster_id.is_(None),
        ).all()
        for signal in signals:
            if not signal.run_id:
                continue
            run = Run.query.get(signal.run_id)
            if not run:
                continue
            if run.status in ("merged", "converged"):
                signal.status = "done"
                db.session.commit()
                self.stats["runs_completed"] += 1
            elif run.status == "failed":
                signal.status = "failed"
                db.session.commit()
                self.stats["runs_failed"] += 1

        # Sync clusters
        running_clusters = SignalCluster.query.filter_by(status="running").all()
        for cluster in running_clusters:
            if not cluster.run_id:
                continue
            run = Run.query.get(cluster.run_id)
            if not run:
                continue
            if run.status in ("merged", "converged"):
                cluster.status = "done"
                for sig in cluster.signals:
                    sig.status = "done"
                db.session.commit()
                self.stats["runs_completed"] += 1
            elif run.status == "failed":
                cluster.status = "failed"
                for sig in cluster.signals:
                    sig.status = "failed"
                db.session.commit()
                self.stats["runs_failed"] += 1

    # ── Helpers ──

    def _record_error(self, msg: str):
        print(f"🩺 ❌ {msg}")
        self.stats["errors"].append({
            "time": datetime.now(timezone.utc).isoformat(),
            "message": msg,
        })
        # Keep only last 10 errors
        self.stats["errors"] = self.stats["errors"][-10:]

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "enabled": self.is_enabled(),
            "rules": self.get_rules(),
            "stats": self.stats,
            "processing": list(self._processing),
        }
