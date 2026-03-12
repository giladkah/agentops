"""
AgentOps Data Models
SQLite-backed models for workflows, runs, agents, and personas.
"""
import json
import uuid
from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Setting(db.Model):
    """Key-value settings store."""
    __tablename__ = "settings"

    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, default="")
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    @staticmethod
    def get(key: str, default: str = "") -> str:
        s = Setting.query.get(key)
        return s.value if s else default

    @staticmethod
    def set(key: str, value: str):
        s = Setting.query.get(key)
        if s:
            s.value = value
            s.updated_at = datetime.now(timezone.utc)
        else:
            s = Setting(key=key, value=value)
            db.session.add(s)
        db.session.commit()
        return s


class Signal(db.Model):
    """A signal from any source — the universal backlog item."""
    __tablename__ = "signals"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))

    # Source
    source = db.Column(db.String(20), nullable=False)  # sentry, shortcut, github, manual, scan
    source_id = db.Column(db.String(200), nullable=True)  # SENTRY-4892, SC-2341, GH-822

    # Normalized fields (extracted by adapter)
    title = db.Column(db.String(300), nullable=False)
    summary = db.Column(db.Text, default="")
    severity = db.Column(db.String(20), default="medium")  # critical, high, medium, low
    files_hint = db.Column(db.Text, default="[]")  # JSON array of file paths

    # Raw webhook payload (preserved for chat to dig into)
    raw_payload = db.Column(db.Text, default="{}")

    # Status flow: new → triaging → ready → running → done | skipped
    status = db.Column(db.String(20), default="new")

    # Chat conversation
    chat_messages = db.Column(db.Text, default="[]")  # JSON array of {role, content, timestamp}

    # Linked run (when a run is created from this signal)
    run_id = db.Column(db.String(36), db.ForeignKey("runs.id"), nullable=True)
    ensemble_id = db.Column(db.String(36), nullable=True)

    # AI-proposed run config (from chat)
    proposed_run = db.Column(db.Text, default="{}")  # JSON: {workflow_id, title, task_description, agent_configs, ...}

    run = db.relationship("Run", backref="signal", foreign_keys=[run_id])

    def get_files_hint(self):
        return json.loads(self.files_hint) if self.files_hint else []

    def get_chat_messages(self):
        return json.loads(self.chat_messages) if self.chat_messages else []

    def add_chat_message(self, role, content):
        msgs = self.get_chat_messages()
        msgs.append({
            "role": role,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        self.chat_messages = json.dumps(msgs)

    def get_raw_payload(self):
        return json.loads(self.raw_payload) if self.raw_payload else {}

    def get_proposed_run(self):
        return json.loads(self.proposed_run) if self.proposed_run else {}

    def to_dict(self):
        return {
            "id": self.id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "source": self.source,
            "source_id": self.source_id,
            "title": self.title,
            "summary": self.summary,
            "severity": self.severity,
            "files_hint": self.get_files_hint(),
            "raw_payload": self.get_raw_payload(),
            "status": self.status,
            "chat_messages": self.get_chat_messages(),
            "run_id": self.run_id,
            "ensemble_id": self.ensemble_id,
            "proposed_run": self.get_proposed_run(),
        }


class Persona(db.Model):
    __tablename__ = "personas"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = db.Column(db.String(100), nullable=False)
    icon = db.Column(db.String(10), default="🤖")
    role = db.Column(db.String(50), nullable=False)  # engineer, reviewer, security, qa, planner
    default_model = db.Column(db.String(50), default="sonnet")
    prompt_template = db.Column(db.Text, nullable=False)
    traits = db.Column(db.Text, default="[]")  # JSON array of trait strings
    color = db.Column(db.String(20), default="blue")  # blue, orange, pink, purple, green
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def get_traits(self):
        return json.loads(self.traits) if self.traits else []

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "icon": self.icon,
            "role": self.role, "default_model": self.default_model,
            "prompt_template": self.prompt_template,
            "traits": self.get_traits(), "color": self.color,
        }


class Workflow(db.Model):
    __tablename__ = "workflows"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = db.Column(db.String(100), nullable=False)
    icon = db.Column(db.String(10), default="⚡")
    description = db.Column(db.Text, default="")
    stages_config = db.Column(db.Text, nullable=False)  # JSON array of stage definitions
    convergence_threshold = db.Column(db.Integer, default=2)  # issues < N = converged
    max_review_rounds = db.Column(db.Integer, default=3)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    times_used = db.Column(db.Integer, default=0)

    def get_stages(self):
        return json.loads(self.stages_config)

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "icon": self.icon,
            "description": self.description, "stages": self.get_stages(),
            "convergence_threshold": self.convergence_threshold,
            "max_review_rounds": self.max_review_rounds,
            "times_used": self.times_used,
        }


class Run(db.Model):
    __tablename__ = "runs"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workflow_id = db.Column(db.String(36), db.ForeignKey("workflows.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    task_description = db.Column(db.Text, nullable=False)
    target_branch = db.Column(db.String(100), default="")
    status = db.Column(db.String(30), default="pending")  # pending, running, needs_approval, converged, merged, failed
    current_stage_index = db.Column(db.Integer, default=0)
    review_round = db.Column(db.Integer, default=0)
    review_history = db.Column(db.Text, default="[]")  # JSON: [{round: 1, issues: 7}, ...]
    total_cost = db.Column(db.Float, default=0.0)
    total_tokens_in = db.Column(db.Integer, default=0)
    total_tokens_out = db.Column(db.Integer, default=0)
    started_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    finished_at = db.Column(db.DateTime, nullable=True)
    source_type = db.Column(db.String(20), default="manual")  # manual, shortcut, sentry
    source_id = db.Column(db.String(100), nullable=True)  # external ticket/issue ID
    auto_approve = db.Column(db.Boolean, default=False)  # skip human checkpoints
    base_branch = db.Column(db.String(100), default="main")  # PR target branch
    ensemble_id = db.Column(db.String(36), nullable=True)  # Parent ensemble if part of drift detection

    workflow = db.relationship("Workflow", backref="runs")

    def get_review_history(self):
        return json.loads(self.review_history) if self.review_history else []

    def add_review_round(self, issues_found):
        history = self.get_review_history()
        history.append({"round": len(history) + 1, "issues": issues_found})
        self.review_history = json.dumps(history)
        self.review_round = len(history)

    def duration_minutes(self):
        end = self.finished_at or datetime.now(timezone.utc)
        start = self.started_at or end
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return int((end - start).total_seconds() / 60)

    def to_dict(self):
        return {
            "id": self.id, "workflow_id": self.workflow_id,
            "title": self.title, "task_description": self.task_description,
            "target_branch": self.target_branch, "status": self.status,
            "current_stage_index": self.current_stage_index,
            "review_round": self.review_round,
            "review_history": self.get_review_history(),
            "total_cost": self.total_cost,
            "total_tokens_in": self.total_tokens_in,
            "total_tokens_out": self.total_tokens_out,
            "duration_minutes": self.duration_minutes(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "source_type": self.source_type, "source_id": self.source_id,
            "auto_approve": self.auto_approve,
            "ensemble_id": self.ensemble_id,
        }


class Ensemble(db.Model):
    """A group of N identical runs for drift detection."""
    __tablename__ = "ensembles"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title = db.Column(db.String(200), nullable=False)
    task_description = db.Column(db.Text, nullable=False)
    workflow_id = db.Column(db.String(36), db.ForeignKey("workflows.id"), nullable=False)
    num_runs = db.Column(db.Integer, default=3)
    status = db.Column(db.String(30), default="running")  # running, comparing, consensus, reviewing, done, failed
    base_branch = db.Column(db.String(100), default="main")
    consensus_agent_id = db.Column(db.String(36), nullable=True)  # Agent doing consensus merge
    reviewer_agent_id = db.Column(db.String(36), nullable=True)  # Agent reviewing consensus
    consensus_worktree = db.Column(db.String(200), nullable=True)
    comparison_data = db.Column(db.Text, default="{}")  # JSON: per-run summaries and diff stats
    total_cost = db.Column(db.Float, default=0.0)
    started_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    finished_at = db.Column(db.DateTime, nullable=True)

    workflow = db.relationship("Workflow")

    def get_runs(self):
        return Run.query.filter_by(ensemble_id=self.id).order_by(Run.started_at).all()

    def get_comparison(self):
        return json.loads(self.comparison_data) if self.comparison_data else {}

    def duration_minutes(self):
        end = self.finished_at or datetime.now(timezone.utc)
        start = self.started_at or end
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return int((end - start).total_seconds() / 60)

    def to_dict(self):
        runs = self.get_runs()
        return {
            "id": self.id, "title": self.title,
            "task_description": self.task_description,
            "workflow_id": self.workflow_id,
            "num_runs": self.num_runs,
            "status": self.status,
            "base_branch": self.base_branch,
            "comparison": self.get_comparison(),
            "total_cost": self.total_cost,
            "duration_minutes": self.duration_minutes(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "runs": [r.to_dict() for r in runs],
            "runs_complete": sum(1 for r in runs if r.status in ("converged", "merged")),
            "runs_total": len(runs),
        }


class Agent(db.Model):
    __tablename__ = "agents"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id = db.Column(db.String(36), db.ForeignKey("runs.id"), nullable=False)
    persona_id = db.Column(db.String(36), db.ForeignKey("personas.id"), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    model = db.Column(db.String(50), default="sonnet")
    status = db.Column(db.String(30), default="waiting")  # waiting, running, done, failed, converged
    stage_name = db.Column(db.String(50), default="")
    worktree_path = db.Column(db.String(200), nullable=True)
    task_prompt = db.Column(db.Text, default="")
    issues_found = db.Column(db.Integer, default=0)
    tokens_in = db.Column(db.Integer, default=0)
    tokens_out = db.Column(db.Integer, default=0)
    cost = db.Column(db.Float, default=0.0)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    output_log = db.Column(db.Text, default="")

    run = db.relationship("Run", backref="agents")
    persona = db.relationship("Persona")

    def duration_minutes(self):
        if not self.started_at:
            return 0
        end = self.finished_at or datetime.now(timezone.utc)
        start = self.started_at
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return int((end - start).total_seconds() / 60)

    def to_dict(self):
        return {
            "id": self.id, "run_id": self.run_id,
            "persona_id": self.persona_id, "name": self.name,
            "model": self.model, "status": self.status,
            "stage_name": self.stage_name,
            "worktree_path": self.worktree_path,
            "issues_found": self.issues_found,
            "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
            "cost": self.cost, "duration_minutes": self.duration_minutes(),
            "output_log": self.output_log or "",
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "persona": self.persona.to_dict() if self.persona else None,
        }


class LogEntry(db.Model):
    __tablename__ = "log_entries"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    run_id = db.Column(db.String(36), db.ForeignKey("runs.id"), nullable=False)
    agent_id = db.Column(db.String(36), nullable=True)
    agent_name = db.Column(db.String(100), default="System")
    level = db.Column(db.String(10), default="info")  # info, success, warning, error
    message = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            "id": self.id, "run_id": self.run_id,
            "agent_name": self.agent_name, "level": self.level,
            "message": self.message,
            "timestamp": self.timestamp.strftime("%H:%M:%S") if self.timestamp else "",
        }


class EnsembleRun(db.Model):
    """An ensemble run launches N identical runs and synthesizes results."""
    __tablename__ = "ensemble_runs"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title = db.Column(db.String(200), nullable=False)
    task_description = db.Column(db.Text, nullable=False)
    workflow_id = db.Column(db.String(36), db.ForeignKey("workflows.id"), nullable=False)
    num_runs = db.Column(db.Integer, default=3)
    status = db.Column(db.String(30), default="pending")  # pending, running, comparing, synthesizing, reviewing, done, failed
    run_ids = db.Column(db.Text, default="[]")  # JSON array of run IDs
    consensus_run_id = db.Column(db.String(36), nullable=True)  # The consensus synthesis run
    review_run_id = db.Column(db.String(36), nullable=True)  # Final review run
    comparison_data = db.Column(db.Text, default="{}")  # JSON: diff stats, shared findings, divergent findings
    total_cost = db.Column(db.Float, default=0.0)
    base_branch = db.Column(db.String(100), default="main")
    auto_approve = db.Column(db.Boolean, default=True)  # Ensemble runs default to autopilot
    individual_prs = db.Column(db.Boolean, default=False)  # Also create PRs for each child run
    started_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    finished_at = db.Column(db.DateTime, nullable=True)

    workflow = db.relationship("Workflow")

    def get_run_ids(self):
        return json.loads(self.run_ids) if self.run_ids else []

    def set_run_ids(self, ids):
        self.run_ids = json.dumps(ids)

    def get_comparison(self):
        return json.loads(self.comparison_data) if self.comparison_data else {}

    def duration_minutes(self):
        end = self.finished_at or datetime.now(timezone.utc)
        start = self.started_at or end
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return int((end - start).total_seconds() / 60)

    def to_dict(self):
        return {
            "id": self.id, "title": self.title,
            "task_description": self.task_description,
            "workflow_id": self.workflow_id,
            "num_runs": self.num_runs,
            "status": self.status,
            "run_ids": self.get_run_ids(),
            "consensus_run_id": self.consensus_run_id,
            "review_run_id": self.review_run_id,
            "comparison": self.get_comparison(),
            "total_cost": self.total_cost,
            "base_branch": self.base_branch,
            "auto_approve": self.auto_approve,
            "individual_prs": self.individual_prs,
            "duration_minutes": self.duration_minutes(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
        }
