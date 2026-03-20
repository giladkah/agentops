"""
API Routes — REST endpoints for the AgentOps dashboard.
"""
import json
import time
import threading
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, Response, current_app
from models import db, Run, Agent, Persona, Workflow, LogEntry, EnsembleRun, Setting, Signal, SignalCluster
from services.orchestrator import RunOrchestrator
from services.git_service import GitService
from services.agent_runner import AgentRunner
from services.ensemble import EnsembleOrchestrator
from services.chat_service import ChatService
from services.github_poller import GitHubPoller
from services.shortcut_poller import ShortcutPoller
from services.sentry_poller import SentryPoller
from services.self_healing import SelfHealingService
from services.clustering_service import ClusteringService
import services.telemetry as telemetry

api = Blueprint("api", __name__, url_prefix="/api")


@api.errorhandler(404)
def api_not_found(e):
    return jsonify({"error": "Not found"}), 404


@api.errorhandler(500)
def api_server_error(e):
    return jsonify({"error": f"Internal server error: {str(e)}"}), 500

# These get initialized in app.py
git_service: GitService = None
agent_runner: AgentRunner = None
orchestrator: RunOrchestrator = None
ensemble_orchestrator: EnsembleOrchestrator = None
chat_service: ChatService = None
github_poller: GitHubPoller = None
shortcut_poller: ShortcutPoller = None
sentry_poller: SentryPoller = None
self_healing: SelfHealingService = None
clustering_service: ClusteringService = None
_active_triages = {}  # signal_id -> {"status": "running"|"done"|"error", ...}


def init_services(repo_path: str, app=None, api_key: str = None):
    global git_service, agent_runner, orchestrator, ensemble_orchestrator, chat_service, github_poller, shortcut_poller, sentry_poller, self_healing, clustering_service
    git_service = GitService(repo_path)

    # Load saved mode from DB (default: "api" if API key set, else "cli")
    saved_mode = Setting.get("runner_mode", "")
    if saved_mode in ("api", "cli"):
        mode = saved_mode
    elif api_key or __import__("os").environ.get("ANTHROPIC_API_KEY"):
        mode = "api"
    else:
        mode = "cli"

    agent_runner = AgentRunner(api_key=api_key, mode=mode, log_callback=_agent_log_callback)
    orchestrator = RunOrchestrator(git_service, agent_runner, app=app)
    ensemble_orchestrator = EnsembleOrchestrator(orchestrator, git_service, agent_runner, app=app)
    chat_service = ChatService(api_key=api_key, repo_path=repo_path, app=app)

    # GitHub poller
    github_poller = GitHubPoller(app=app)
    github_poller.load_repos()
    github_poller.load_seen_ids()
    if github_poller.repos:
        github_poller.start()

    # Shortcut poller
    shortcut_poller = ShortcutPoller(app=app)
    shortcut_poller.load_config()
    shortcut_poller.load_seen_ids()
    if shortcut_poller.workspaces:
        shortcut_poller.start()

    # Sentry poller
    sentry_poller = SentryPoller(app=app)
    sentry_poller.load_config()
    sentry_poller.load_seen_ids()
    if sentry_poller.projects:
        sentry_poller.start()

    # Self-healing service
    self_healing = SelfHealingService(chat_service, orchestrator, app=app)
    if self_healing.is_enabled():
        self_healing.start()

    # Clustering service
    clustering_service = ClusteringService(app=app, api_key=api_key)
    if clustering_service.is_enabled():
        clustering_service.start()

    print(f"🔌 Runner mode: {mode.upper()}")
    chat_mode = chat_service.mode()
    if chat_mode == "api":
        print(f"💬 Chat service: ✅ API mode")
    elif chat_mode == "cli":
        print(f"💬 Chat service: ✅ CLI mode (Claude Code)")
    else:
        print(f"💬 Chat service: ⚠️ no backend (set API key or install Claude Code)")
    if github_poller.repos:
        print(f"🔗 GitHub poller: {len(github_poller.repos)} repo(s) configured")
    if shortcut_poller.workspaces:
        print(f"🎯 Shortcut poller: {len(shortcut_poller.workspaces)} workspace(s) configured")
    if sentry_poller.projects:
        print(f"🐛 Sentry poller: {len(sentry_poller.projects)} project(s) configured")
    if self_healing and self_healing._running:
        print(f"🩺 Self-healing: enabled")
    if clustering_service and clustering_service._running:
        print(f"🔗 Clustering: enabled")


def _agent_log_callback(agent_id, level, message):
    """Callback for agent runner logs — writes to DB."""
    try:
        from flask import current_app
        # Check if we're already in an app context
        try:
            _ = current_app.name
            agent = Agent.query.get(agent_id)
            if agent:
                entry = LogEntry(
                    run_id=agent.run_id, agent_id=agent_id,
                    agent_name=agent.name, level=level, message=message,
                )
                db.session.add(entry)
                db.session.commit()
        except RuntimeError:
            # No app context — this is from a background thread, skip DB logging
            print(f"[{level}] Agent {agent_id}: {message}")
    except Exception as e:
        print(f"Log callback error: {e}")


# ── Runs ──



# ── Repository CRUD (multi-repo support) ─────────────────────────────────────

@api.route("/repos", methods=["GET"])
def list_repos():
    from models import Repository
    repos = Repository.query.order_by(Repository.created_at).all()
    return jsonify([r.to_dict() for r in repos])


@api.route("/repos", methods=["POST"])
def add_repo_endpoint():
    import services.repos as _r
    d = request.json or {}
    name = (d.get("name") or "").strip()
    path = (d.get("path") or "").strip()
    if not name or not path:
        return jsonify({"error": "name and path are required"}), 400
    try:
        repo = _r.add_repo(name=name, path=path)
        return jsonify(repo.to_dict()), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@api.route("/repos/<repo_id>", methods=["PUT"])
def update_repo_endpoint(repo_id):
    import services.repos as _r
    d = request.json or {}
    try:
        repo = _r.update_repo(repo_id, name=d.get("name"), path=d.get("path"))
        return jsonify(repo.to_dict())
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


@api.route("/repos/<repo_id>", methods=["DELETE"])
def delete_repo_endpoint(repo_id):
    import services.repos as _r
    if not _r.remove_repo(repo_id):
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


@api.route("/repos/<repo_id>/set-default", methods=["POST"])
def set_default_repo_endpoint(repo_id):
    import services.repos as _r
    try:
        repo = _r.set_default(repo_id)
        return jsonify(repo.to_dict())
    except ValueError as e:
        return jsonify({"error": str(e)}), 404

# ── End repository CRUD ───────────────────────────────────────────────────────

@api.route("/runs", methods=["GET"])
def list_runs():
    runs = Run.query.order_by(Run.started_at.desc()).limit(50).all()
    result = []
    for r in runs:
        d = r.to_dict()
        # Include lightweight agent info for ensemble detail views
        d["agents"] = [{"id": a.id, "name": a.name, "status": a.status,
                        "stage_name": a.stage_name, "model": a.model,
                        "issues_found": a.issues_found, "cost": a.cost,
                        "duration_minutes": a.duration_minutes(),
                        "persona": {"icon": a.persona.icon, "name": a.persona.name, "role": a.persona.role} if a.persona else None}
                       for a in r.agents]
        result.append(d)
    return jsonify(result)


@api.route("/runs/<run_id>", methods=["GET"])
def get_run(run_id):
    run = Run.query.get_or_404(run_id)
    data = run.to_dict()
    data["agents"] = [a.to_dict() for a in run.agents]
    data["workflow"] = run.workflow.to_dict() if run.workflow else None
    return jsonify(data)


@api.route("/runs", methods=["POST"])
def create_run():
    data = request.json
    try:
        run = orchestrator.create_run(
            workflow_id=data["workflow_id"],
            title=data["title"],
            task_description=data["task_description"],
            agent_configs=data.get("agent_configs", []),
            target_branch=data.get("target_branch", ""),
            source_type=data.get("source_type", "manual"),
            source_id=data.get("source_id"),
            auto_approve=data.get("auto_approve", False),
            base_branch=data.get("base_branch", "main"),
        )
        return jsonify(run.to_dict()), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@api.route("/runs/<run_id>/start", methods=["POST"])
def start_run(run_id):
    success = orchestrator.start_run(run_id)
    if success:
        run = Run.query.get(run_id)
        if run:
            telemetry.track_run_started(run)
        return jsonify({"status": "started"})
    return jsonify({"error": "Cannot start run"}), 400


@api.route("/runs/<run_id>/approve", methods=["POST"])
def approve_checkpoint(run_id):
    success = orchestrator.approve_checkpoint(run_id)
    if success:
        return jsonify({"status": "approved"})
    return jsonify({"error": "No checkpoint to approve"}), 400


@api.route("/runs/<run_id>/merge", methods=["POST"])
def merge_run(run_id):
    success, msg = orchestrator.merge_run(run_id)
    if success:
        telemetry.track_pr_created(source="single_run")
        return jsonify({"status": "merged", "message": msg})
    return jsonify({"error": msg}), 400


@api.route("/runs/<run_id>/cancel", methods=["POST"])
def cancel_run(run_id):
    success = orchestrator.cancel_run(run_id)
    if success:
        run = Run.query.get(run_id)
        if run:
            telemetry.track_run_cancelled(run)
        return jsonify({"status": "cancelled"})
    return jsonify({"error": "Cannot cancel"}), 400


@api.route("/runs/<run_id>/logs", methods=["GET"])
def get_run_logs(run_id):
    limit = request.args.get("limit", 100, type=int)
    after_id = request.args.get("after", 0, type=int)

    query = LogEntry.query.filter_by(run_id=run_id)
    if after_id:
        query = query.filter(LogEntry.id > after_id)
    logs = query.order_by(LogEntry.timestamp.asc()).limit(limit).all()

    return jsonify([l.to_dict() for l in logs])



@api.route("/runs/<run_id>/issues", methods=["GET"])
def get_run_issues(run_id):
    run = Run.query.get_or_404(run_id)
    review_stages = {"review", "review-quality", "review-security", "security"}
    review_agents = [a for a in run.agents if a.stage_name in review_stages and a.status in ("done", "converged")]
    if not review_agents:
        return jsonify({"consensus": [], "majority": [], "unique": [], "total_reviewers": 0, "total_issues": 0})

    import re as _re, json as _j

    def _norm(title):
        t = title.lower()
        t = _re.sub(r"[^a-z0-9 ]", " ", t)
        stop = {"the","a","an","is","are","in","on","at","to","of","and","or","not","for","with","that","this","be","been","as","by","do","it","its"}
        return {w for w in t.split() if w not in stop and len(w) > 2}

    def _match(a, b):
        ta, tb = _norm(a.get("title","")), _norm(b.get("title",""))
        if not ta or not tb: return False
        fa = (a.get("file","") or "").split("/")[-1].lower()
        fb = (b.get("file","") or "").split("/")[-1].lower()
        file_hit = bool(fa and fb and fa == fb)
        inter = len(ta & tb)
        mn = min(len(ta), len(tb))
        score = (inter / mn * 0.7) if mn else 0
        score = max(score, inter / len(ta | tb) if ta | tb else 0)
        if file_hit: score += 0.15
        return score >= 0.38

    _sev_ord = {"critical": 0, "high": 1, "medium": 2, "low": 3}

    all_issues = []
    for ag in review_agents:
        for iss in ag.get_structured_issues():
            all_issues.append({**iss, "_aid": ag.id, "_aname": ag.name,
                "_aicon": ag.persona.icon if ag.persona else "🤖",
                "_arole": ag.persona.role if ag.persona else "reviewer"})

    groups = []
    for iss in all_issues:
        placed = False
        for grp in groups:
            if any(_match(iss, g) for g in grp):
                grp.append(iss); placed = True; break
        if not placed:
            groups.append([iss])

    n = len(review_agents)
    all_agent_info = [{"id": a.id, "name": a.name,
        "icon": a.persona.icon if a.persona else "🤖",
        "role": a.persona.role if a.persona else "reviewer"} for a in review_agents]

    consensus, majority, unique = [], [], []
    for grp in groups:
        by_agent = {}
        for i in grp:
            aid = i["_aid"]
            if aid not in by_agent or len(i.get("note","")) > len(by_agent[aid].get("note","")): by_agent[aid] = i
        found_ids = set(by_agent.keys())
        count = len(by_agent)
        primary = max(grp, key=lambda x: len((x.get("title",""))+(x.get("note",""))))
        merged = {"title": primary.get("title",""), "file": primary.get("file",""),
            "line": primary.get("line"), "severity": min([i.get("severity","medium") for i in by_agent.values()], key=lambda s: _sev_ord.get(s,2)),
            "category": primary.get("category","quality"), "note": primary.get("note",""),
            "found_count": count, "total_reviewers": n,
            "agents_found": [{**a, "note": by_agent[a["id"]].get("note","") if a["id"] in by_agent else ""} for a in all_agent_info if a["id"] in found_ids],
            "agents_missed": [a for a in all_agent_info if a["id"] not in found_ids]}
        if count >= n: consensus.append(merged)
        elif count > n / 2: majority.append(merged)
        else: unique.append(merged)

    for tier in (consensus, majority, unique):
        tier.sort(key=lambda i: _sev_ord.get(i.get("severity","medium"), 2))

    return jsonify({"consensus": consensus, "majority": majority, "unique": unique,
        "total_reviewers": n, "total_issues": len(all_issues), "reviewers": all_agent_info})

# ── Agents ──

@api.route("/agents/<agent_id>", methods=["GET"])
def get_agent(agent_id):
    agent = Agent.query.get_or_404(agent_id)
    return jsonify(agent.to_dict())


@api.route("/agents/<agent_id>/stop", methods=["POST"])
def stop_agent(agent_id):
    success = agent_runner.stop_agent(agent_id)
    if success:
        agent = Agent.query.get(agent_id)
        if agent:
            agent.status = "failed"
            db.session.commit()
        return jsonify({"status": "stopped"})
    return jsonify({"error": "Agent not running"}), 400


@api.route("/agents/<agent_id>/stream")
def agent_stream(agent_id):
    """SSE endpoint — streams live agent events (tool calls, text, usage)."""
    after = int(request.args.get("after", -1))

    def generate():
        last_idx = after
        max_wait = 600  # 10 min max connection
        start = time.time()

        while time.time() - start < max_wait:
            buf = agent_runner.get_stream_buffer(agent_id)
            if not buf:
                # Agent hasn't started or buffer cleaned up
                yield f"data: {json.dumps({'type': 'waiting'})}\n\n"
                time.sleep(1)
                continue

            events = buf.get_since(last_idx)
            for ev in events:
                yield f"data: {json.dumps(ev)}\n\n"
                last_idx = ev["idx"]

            if buf.finished:
                yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"
                return

            time.sleep(0.2)  # 200ms poll interval for responsiveness

        yield f"data: {json.dumps({'type': 'timeout'})}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Workflows ──

@api.route("/workflows", methods=["GET"])
def list_workflows():
    workflows = Workflow.query.order_by(Workflow.times_used.desc()).all()
    return jsonify([w.to_dict() for w in workflows])


@api.route("/workflows/<workflow_id>", methods=["GET"])
def get_workflow(workflow_id):
    workflow = Workflow.query.get_or_404(workflow_id)
    return jsonify(workflow.to_dict())


@api.route("/workflows", methods=["POST"])
def create_workflow():
    data = request.json
    workflow = Workflow(
        name=data["name"],
        icon=data.get("icon", "⚡"),
        description=data.get("description", ""),
        stages_config=data.get("stages_config", "[]") if isinstance(data.get("stages_config"), str) else json.dumps(data.get("stages", [])),
        convergence_threshold=data.get("convergence_threshold", 2),
        max_review_rounds=data.get("max_review_rounds", 3),
    )
    db.session.add(workflow)
    db.session.commit()
    return jsonify(workflow.to_dict()), 201


# ── Personas ──

@api.route("/personas", methods=["GET"])
def list_personas():
    personas = Persona.query.all()
    return jsonify([p.to_dict() for p in personas])


@api.route("/personas/<persona_id>", methods=["GET"])
def get_persona(persona_id):
    persona = Persona.query.get_or_404(persona_id)
    return jsonify(persona.to_dict())


@api.route("/personas", methods=["POST"])
def create_persona():
    data = request.json
    persona = Persona(
        name=data["name"],
        icon=data.get("icon", "🤖"),
        role=data["role"],
        default_model=data.get("default_model", "haiku"),
        prompt_template=data["prompt_template"],
        traits=json.dumps(data.get("traits", [])),
        color=data.get("color", "blue"),
    )
    db.session.add(persona)
    db.session.commit()
    return jsonify(persona.to_dict()), 201


# ── Ensemble (Drift Detection) ──

@api.route("/ensembles", methods=["GET"])
def list_ensembles():
    ensembles = EnsembleRun.query.order_by(EnsembleRun.started_at.desc()).limit(20).all()
    return jsonify([e.to_dict() for e in ensembles])


@api.route("/ensembles/<eid>", methods=["GET"])
def get_ensemble(eid):
    ensemble = EnsembleRun.query.get_or_404(eid)
    data = ensemble.to_dict()

    # Include sub-run details
    sub_runs = []
    for rid in ensemble.get_run_ids():
        run = Run.query.get(rid)
        if run:
            rd = run.to_dict()
            rd["agents"] = [a.to_dict() for a in run.agents]
            sub_runs.append(rd)
    data["sub_runs"] = sub_runs

    # Include consensus run
    if ensemble.consensus_run_id:
        cr = Run.query.get(ensemble.consensus_run_id)
        if cr:
            crd = cr.to_dict()
            crd["agents"] = [a.to_dict() for a in cr.agents]
            data["consensus_run"] = crd

    # Include review run
    if ensemble.review_run_id:
        rr = Run.query.get(ensemble.review_run_id)
        if rr:
            rrd = rr.to_dict()
            rrd["agents"] = [a.to_dict() for a in rr.agents]
            data["review_run"] = rrd

    return jsonify(data)


@api.route("/ensembles", methods=["POST"])
def create_ensemble():
    data = request.json
    try:
        ensemble = ensemble_orchestrator.create_ensemble(
            title=data["title"],
            task_description=data["task_description"],
            workflow_id=data["workflow_id"],
            agent_configs=data.get("agent_configs", []),
            num_runs=data.get("num_runs", 3),
            base_branch=data.get("base_branch", "main"),
            auto_approve=data.get("auto_approve", True),
            individual_prs=data.get("individual_prs", False),
        )
        return jsonify(ensemble.to_dict()), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@api.route("/ensembles/<eid>/start", methods=["POST"])
def start_ensemble(eid):
    success = ensemble_orchestrator.start_ensemble(eid)
    if success:
        ensemble = EnsembleRun.query.get(eid)
        if ensemble:
            telemetry.track_ensemble_started(ensemble)
        return jsonify({"status": "started"})
    return jsonify({"error": "Cannot start ensemble"}), 400


@api.route("/ensembles/<eid>/approve", methods=["POST"])
def approve_ensemble(eid):
    success = ensemble_orchestrator.approve_ensemble(eid)
    if success:
        return jsonify({"status": "approved"})
    return jsonify({"error": "Cannot approve in current state"}), 400


@api.route("/ensembles/<eid>/pr", methods=["POST"])
def ensemble_create_pr(eid):
    ensemble = EnsembleRun.query.get_or_404(eid)
    ensemble_orchestrator._finalize(ensemble)
    telemetry.track_pr_created(source="ensemble")
    return jsonify({"status": "pr_created"})


# ── Signals ──

@api.route("/signals", methods=["GET"])
def list_signals():
    """List signals with pagination, newest first. Params: ?status=, ?source=, ?offset=, ?limit="""
    query = Signal.query.order_by(Signal.created_at.desc())
    status = request.args.get("status")
    if status:
        query = query.filter_by(status=status)
    source = request.args.get("source")
    if source:
        query = query.filter_by(source=source)

    # Get total count before pagination
    total = query.count()

    offset = request.args.get("offset", 0, type=int)
    limit = request.args.get("limit", 50, type=int)
    limit = min(limit, 200)  # Cap at 200

    signals = query.offset(offset).limit(limit).all()
    return jsonify({
        "signals": [s.to_dict() for s in signals],
        "total": total,
        "offset": offset,
        "limit": limit,
    })


@api.route("/signals/counts", methods=["GET"])
def signal_counts():
    """Get signal counts by source (lightweight, no payload)."""
    from sqlalchemy import func
    rows = db.session.query(Signal.source, func.count(Signal.id)).group_by(Signal.source).all()
    counts = {source: count for source, count in rows}
    counts["total"] = sum(counts.values())
    return jsonify(counts)


@api.route("/signals/<signal_id>", methods=["GET"])
def get_signal(signal_id):
    signal = Signal.query.get_or_404(signal_id)
    return jsonify(signal.to_dict())


@api.route("/signals", methods=["POST"])
def create_signal():
    """Create a manual signal."""
    data = request.json
    signal = Signal(
        source="manual",
        title=data.get("title", "Manual signal"),
        summary=data.get("summary", data.get("text", "")),
        severity=data.get("severity", "medium"),
        files_hint=json.dumps(data.get("files_hint", [])),
        raw_payload=json.dumps({"text": data.get("text", data.get("summary", ""))}),
        status="new",
    )
    db.session.add(signal)
    db.session.commit()
    telemetry.track_signal_received("manual")
    return jsonify(signal.to_dict()), 201


@api.route("/signals/sentry", methods=["POST"])
def sentry_webhook():
    """Sentry webhook adapter — normalizes Sentry alert into a Signal."""
    payload = request.json or {}

    # Sentry webhook format: {action, data: {issue: {...}}}
    issue = payload.get("data", {}).get("issue", payload.get("issue", payload))
    event = payload.get("data", {}).get("event", {})

    title = issue.get("title", "Sentry alert")
    culprit = issue.get("culprit", "")
    message = event.get("message", issue.get("metadata", {}).get("value", ""))

    # Extract file paths from stack trace
    files = []
    for entry in event.get("entries", []):
        if entry.get("type") == "exception":
            for val in entry.get("data", {}).get("values", []):
                for frame in val.get("stacktrace", {}).get("frames", []):
                    filename = frame.get("filename", "")
                    lineno = frame.get("lineNo", frame.get("lineno", ""))
                    if filename and not filename.startswith("<"):
                        files.append(f"{filename}:{lineno}" if lineno else filename)

    # Map severity
    level = issue.get("level", "error")
    severity_map = {"fatal": "critical", "error": "high", "warning": "medium", "info": "low"}
    severity = severity_map.get(level, "medium")

    # Boost severity for high frequency
    count = issue.get("count", 0)
    user_count = issue.get("userCount", 0)
    if count > 50 or user_count > 10:
        severity = "critical"

    summary = f"{culprit}: {message}" if culprit else message
    if count:
        summary += f" ({count} events"
        if user_count:
            summary += f", {user_count} users"
        summary += ")"

    signal = Signal(
        source="sentry",
        source_id=f"SENTRY-{issue.get('id', 'unknown')}",
        title=title,
        summary=summary[:500],
        severity=severity,
        files_hint=json.dumps(files[:10]),
        raw_payload=json.dumps(payload)[:50000],
        status="new",
    )
    db.session.add(signal)
    db.session.commit()

    print(f"📨 Sentry signal: {title} [{severity}]")
    return jsonify(signal.to_dict()), 201


@api.route("/signals/shortcut", methods=["POST"])
def shortcut_webhook():
    """Shortcut webhook adapter — normalizes Shortcut story into a Signal."""
    payload = request.json or {}

    # Shortcut webhook: {actions: [{...}], ...} or direct story object
    actions = payload.get("actions", [{}])
    action = actions[0] if actions else payload
    story = action.get("entity_body", action.get("story", payload))

    title = story.get("name", "Shortcut story")
    description = story.get("description", "")
    story_type = story.get("story_type", "feature")
    labels = [l.get("name", "") for l in story.get("labels", [])]

    # Map severity from story type + labels
    severity = "medium"
    if story_type == "bug":
        severity = "high"
    if any(l.lower() in ("urgent", "p1", "critical", "blocker") for l in labels):
        severity = "critical"
    elif any(l.lower() in ("p2", "high") for l in labels):
        severity = "high"

    # Extract file hints from description
    import re
    files = re.findall(r'[\w/]+\.\w{1,4}(?::\d+)?', description)

    summary = description[:500] if description else f"{story_type}: {title}"

    signal = Signal(
        source="shortcut",
        source_id=f"SC-{story.get('id', 'unknown')}",
        title=title,
        summary=summary,
        severity=severity,
        files_hint=json.dumps(files[:10]),
        raw_payload=json.dumps(payload)[:50000],
        status="new",
    )
    db.session.add(signal)
    db.session.commit()

    print(f"📨 Shortcut signal: {title} [{severity}]")
    return jsonify(signal.to_dict()), 201


@api.route("/signals/github", methods=["POST"])
def github_webhook():
    """GitHub webhook adapter — normalizes GitHub issue/PR into a Signal."""
    payload = request.json or {}
    event_type = request.headers.get("X-GitHub-Event", "issues")

    if event_type == "issues":
        issue = payload.get("issue", payload)
        title = issue.get("title", "GitHub issue")
        body = issue.get("body", "")
        labels = [l.get("name", "") for l in issue.get("labels", [])]
        number = issue.get("number", "?")
        source_id = f"GH-{number}"
    elif event_type == "pull_request":
        pr = payload.get("pull_request", payload)
        title = f"PR: {pr.get('title', 'Pull request')}"
        body = pr.get("body", "")
        labels = [l.get("name", "") for l in pr.get("labels", [])]
        number = pr.get("number", "?")
        source_id = f"GH-PR-{number}"
    else:
        return jsonify({"status": "ignored", "reason": f"unhandled event: {event_type}"}), 200

    # Map severity from labels
    severity = "medium"
    if any(l.lower() in ("bug", "critical", "urgent", "p1") for l in labels):
        severity = "high"
    elif any(l.lower() in ("enhancement", "feature") for l in labels):
        severity = "medium"
    elif any(l.lower() in ("good first issue", "low", "p3") for l in labels):
        severity = "low"

    # Extract file hints
    import re
    files = re.findall(r'[\w/]+\.\w{1,4}(?::\d+)?', body or "")

    signal = Signal(
        source="github",
        source_id=source_id,
        title=title,
        summary=(body or "")[:500],
        severity=severity,
        files_hint=json.dumps(files[:10]),
        raw_payload=json.dumps(payload)[:50000],
        status="new",
    )
    db.session.add(signal)
    db.session.commit()

    print(f"📨 GitHub signal: {title} [{severity}]")
    return jsonify(signal.to_dict()), 201


@api.route("/signals/<signal_id>/chat", methods=["POST"])
def signal_chat(signal_id):
    """Send a message in the signal's chat conversation. Returns AI response."""
    data = request.json or {}
    message = data.get("message", "")

    result = chat_service.send_message(signal_id, message)
    if result["error"]:
        return jsonify(result), 500
    return jsonify(result)


@api.route("/signals/<signal_id>/auto-triage", methods=["POST"])
def auto_triage_signal(signal_id):
    """Trigger AI investigation in background. Optional body: {"message": "..."}"""
    # Check if already running
    if signal_id in _active_triages and _active_triages[signal_id].get("status") == "running":
        return jsonify({"status": "already_running", "signal_id": signal_id})

    data = request.json or {}
    user_message = data.get("message", "").strip()

    # Mark as investigating
    _active_triages[signal_id] = {"status": "running", "started": datetime.now(timezone.utc).isoformat()}

    # Update signal status
    signal = Signal.query.get(signal_id)
    if signal:
        signal.status = "investigating"
        db.session.commit()

    def _run_investigation(app, sid, msg):
        with app.app_context():
            try:
                if msg:
                    result = chat_service.send_message(sid, msg)
                else:
                    result = chat_service.auto_triage(sid)

                _active_triages[sid] = {
                    "status": "error" if result.get("error") else "done",
                    "error": result.get("error"),
                    "finished": datetime.now(timezone.utc).isoformat(),
                }
                # Update signal status
                sig = Signal.query.get(sid)
                if sig and sig.status == "investigating":
                    sig.status = "investigated"
                    db.session.commit()
                print(f"✅ Investigation complete for {sid}" + (" (with message)" if msg else ""))
            except Exception as e:
                print(f"❌ Investigation error for {sid}: {e}")
                _active_triages[sid] = {
                    "status": "error",
                    "error": str(e),
                    "finished": datetime.now(timezone.utc).isoformat(),
                }

    t = threading.Thread(target=_run_investigation, args=(current_app._get_current_object(), signal_id, user_message), daemon=True)
    t.start()

    return jsonify({"status": "started", "signal_id": signal_id})


@api.route("/signals/triage-status", methods=["GET"])
def triage_status():
    """Check triage status for all active investigations. Called by frontend polling."""
    result = {}
    to_remove = []

    for sid, info in list(_active_triages.items()):
        # Return clean status (no internal flags)
        clean = {k: v for k, v in info.items() if not k.startswith("_")}
        result[sid] = clean

        # Track completed items for cleanup
        if info["status"] in ("done", "error"):
            report_count = info.get("_report_count", 0) + 1
            _active_triages[sid]["_report_count"] = report_count
            # Keep for 3 poll cycles (6 seconds) to ensure frontend catches it
            if report_count >= 3:
                to_remove.append(sid)

    for sid in to_remove:
        del _active_triages[sid]

    return jsonify(result)


@api.route("/signals/<signal_id>/create-run", methods=["POST"])
def create_run_from_signal(signal_id):
    """Create a run from the signal's proposed run configuration."""
    try:
        signal = Signal.query.get(signal_id)
        if not signal:
            return jsonify({"error": "Signal not found"}), 404

        proposal = signal.get_proposed_run()
        if not proposal:
            return jsonify({"error": "No run proposal — chat with the signal first"}), 400

        # Get workflow — try ID first, then match by name
        wf_id = proposal.get("workflow_id")
        workflow = Workflow.query.get(wf_id) if wf_id else None

        if not workflow:
            # Fallback: match by name
            wf_name = proposal.get("workflow_name", "")
            if wf_name:
                workflow = Workflow.query.filter(
                    db.func.lower(Workflow.name) == wf_name.lower()
                ).first()
            # Last resort: use first available workflow
            if not workflow:
                workflow = Workflow.query.first()
            if not workflow:
                return jsonify({"error": "No workflows configured"}), 400

        # Build agent configs from workflow stages + personas
        try:
            stages = workflow.get_stages()
        except Exception:
            stages = []

        agent_configs = []
        model = proposal.get("model", "haiku")

        for stage in stages:
            stage_name = stage.get("name", "")
            if stage_name == "merge":
                continue

            # Map stage to persona role
            role_map = {
                "plan": "planner",
                "engineer": "engineer",
                "review": "reviewer",
                "security": "security",
                "qa": "qa",
                "test": "test-runner",
            }
            role = role_map.get(stage_name, stage_name)

            # Find matching personas
            personas = Persona.query.filter_by(role=role).all()
            if not personas and stage_name == "review":
                personas = Persona.query.filter(Persona.role.in_(["reviewer", "security", "architect-reviewer"])).all()

            for persona in personas:
                agent_configs.append({
                    "persona_id": persona.id,
                    "model": model if stage_name == "engineer" else persona.default_model,
                    "stage_name": stage_name,
                })

        # Fallback: if no agents matched, just use an engineer
        if not agent_configs:
            engineer = Persona.query.filter_by(role="engineer").first()
            if engineer:
                agent_configs.append({
                    "persona_id": engineer.id,
                    "model": model,
                    "stage_name": "engineer",
                })
            else:
                return jsonify({"error": "No engineer persona found — check your personas"}), 400

        # Allow overrides from request body
        data = request.json or {}
        auto_approve = data.get("auto_approve", proposal.get("auto_approve", True))

        run = orchestrator.create_run(
            workflow_id=workflow.id,
            title=proposal.get("title", signal.title),
            task_description=proposal.get("task_description", signal.summary),
            agent_configs=agent_configs,
            source_type=signal.source,
            source_id=signal.source_id or "",
            auto_approve=auto_approve,
        )

        # Link signal to run
        signal.run_id = run.id
        signal.status = "running"
        db.session.commit()

        # Auto-start the run
        orchestrator.start_run(run.id)

        return jsonify({"run": run.to_dict(), "signal": signal.to_dict()}), 201

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Failed to create run: {str(e)}"}), 500


@api.route("/signals/<signal_id>/skip", methods=["POST"])
def skip_signal(signal_id):
    """Mark a signal as skipped."""
    signal = Signal.query.get_or_404(signal_id)
    signal.status = "skipped"
    db.session.commit()
    return jsonify(signal.to_dict())


@api.route("/signals/<signal_id>/link-run", methods=["POST"])
def link_signal_run(signal_id):
    """Link a signal to a run (used when launching from the standard modal)."""
    try:
        signal = Signal.query.get(signal_id)
        if not signal:
            return jsonify({"error": "Signal not found"}), 404
        data = request.json or {}
        run_id = data.get("run_id")
        if run_id:
            signal.run_id = run_id
            signal.status = "running"
            db.session.commit()
        return jsonify(signal.to_dict())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@api.route("/signals/<signal_id>", methods=["DELETE"])
def delete_signal(signal_id):
    signal = Signal.query.get_or_404(signal_id)
    db.session.delete(signal)
    db.session.commit()
    return jsonify({"status": "deleted"})


# ── GitHub Poller ──

@api.route("/github/status", methods=["GET"])
def github_status():
    """Get GitHub poller status."""
    if not github_poller:
        return jsonify({"running": False, "repos": [], "has_token": False})
    return jsonify(github_poller.get_status())


@api.route("/github/repos", methods=["POST"])
def github_add_repo():
    """Add a repo to poll. Body: {"owner": "...", "repo": "...", "label_filter": [...]}"""
    data = request.json or {}
    owner = data.get("owner", "").strip()
    repo = data.get("repo", "").strip()

    if not owner or not repo:
        return jsonify({"error": "owner and repo are required"}), 400

    # Validate the repo exists by trying to fetch it
    try:
        github_poller._github_get(f"https://api.github.com/repos/{owner}/{repo}")
    except Exception as e:
        token_hint = ""
        if "404" in str(e):
            if github_poller.token:
                token_hint = " (token IS set but may lack permissions — needs 'repo' scope or fine-grained read access to this repo)"
            else:
                token_hint = " (no GITHUB_TOKEN found — required for private repos)"
        return jsonify({"error": f"Can't access {owner}/{repo}: {e}{token_hint}"}), 400

    label_filter = data.get("label_filter", [])
    github_poller.add_repo(owner, repo, label_filter)

    # Start poller if not running
    if not github_poller._running:
        github_poller.load_seen_ids()
        github_poller.start()

    return jsonify(github_poller.get_status())


@api.route("/github/repos", methods=["DELETE"])
def github_remove_repo():
    """Remove a repo from polling. Body: {"owner": "...", "repo": "..."}"""
    data = request.json or {}
    owner = data.get("owner", "").strip()
    repo = data.get("repo", "").strip()
    github_poller.remove_repo(owner, repo)

    # Stop poller if no repos left
    if not github_poller.repos:
        github_poller.stop()

    return jsonify(github_poller.get_status())


@api.route("/github/poll", methods=["POST"])
def github_poll_now():
    """Manually trigger a poll cycle."""
    if not github_poller or not github_poller.repos:
        return jsonify({"error": "No repos configured"}), 400

    created = github_poller.poll_once()
    return jsonify({"created": len(created), "signals": created})


@api.route("/github/import", methods=["POST"])
def github_import():
    """One-time import of recent issues from a repo. Body: {"owner": "...", "repo": "...", "count": 20}"""
    data = request.json or {}
    owner = data.get("owner", "").strip()
    repo = data.get("repo", "").strip()
    if not owner or not repo:
        return jsonify({"error": "owner and repo required"}), 400

    label_filter = data.get("label_filter", [])

    try:
        items = github_poller.fetch_issues(owner, repo, label_filter)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    created = []
    for item in items:
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
        github_poller._seen_ids.add(item["source_id"])
        created.append(item["title"])

    db.session.commit()
    return jsonify({"imported": len(created), "titles": created})


@api.route("/github/issue/<owner>/<repo>/<int:number>", methods=["GET"])
def github_issue_detail(owner, repo, number):
    """Fetch full issue details including comments from GitHub API."""
    if not github_poller:
        return jsonify({"error": "GitHub not configured"}), 400

    try:
        issue = github_poller._github_get(f"https://api.github.com/repos/{owner}/{repo}/issues/{number}")
        result = {
            "number": issue.get("number"),
            "title": issue.get("title", ""),
            "body": issue.get("body", ""),
            "state": issue.get("state", ""),
            "html_url": issue.get("html_url", ""),
            "labels": [l.get("name", "") for l in issue.get("labels", []) if isinstance(l, dict)],
            "user": issue.get("user", {}).get("login", ""),
            "assignees": [a.get("login", "") for a in issue.get("assignees", []) if isinstance(a, dict)],
            "created_at": issue.get("created_at", ""),
            "updated_at": issue.get("updated_at", ""),
            "comments": [],
        }

        # Fetch comments if any
        if issue.get("comments", 0) > 0:
            comments_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments"
            comments = github_poller._github_get(comments_url)
            result["comments"] = [
                {
                    "author": c.get("user", {}).get("login", ""),
                    "text": c.get("body", ""),
                    "created_at": c.get("created_at", ""),
                }
                for c in comments[:20] if isinstance(c, dict)
            ]

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ── Stats ──

@api.route("/stats", methods=["GET"])
def get_stats():
    from sqlalchemy import func
    total_runs = Run.query.count()
    total_cost = db.session.query(func.sum(Run.total_cost)).scalar() or 0
    active_runs = Run.query.filter(Run.status.in_(["running", "needs_approval"])).count()
    active_agents = agent_runner.count_active()

    active_ensembles = EnsembleRun.query.filter(
        EnsembleRun.status.in_(["running", "comparing", "synthesizing", "reviewing"])
    ).count()

    signal_count = Signal.query.filter(Signal.status.in_(["new", "investigating", "ready"])).count()

    return jsonify({
        "total_runs": total_runs,
        "total_cost": round(total_cost, 2),
        "active_runs": active_runs,
        "active_agents": active_agents,
        "active_ensembles": active_ensembles,
        "pending_signals": signal_count,
    })


# ── Telemetry Settings ──

@api.route("/telemetry", methods=["GET"])
def get_telemetry_settings():
    """Get current telemetry opt-in/out status."""
    return jsonify({
        "enabled": telemetry.is_enabled(),
        "description": "Anonymous usage stats — no code, paths, or task descriptions are sent.",
    })


@api.route("/telemetry", methods=["POST"])
def set_telemetry_settings():
    """Toggle telemetry on or off."""
    data = request.json or {}
    enabled = bool(data.get("enabled", True))
    telemetry.set_enabled(enabled, app=current_app._get_current_object())
    return jsonify({"enabled": telemetry.is_enabled()})



@api.route("/runs/clear", methods=["POST"])
def clear_all_runs():
    """Stop all agents, remove all worktrees, delete all runs."""
    # Stop active agents
    for agent_id in list(agent_runner.active_processes.keys()):
        agent_runner.stop_agent(agent_id)

    # Clean up worktrees
    try:
        if git_service:
            git_service.cleanup_all_worktrees()
    except Exception as e:
        print(f"Worktree cleanup error (non-fatal): {e}")

    # Delete all data
    LogEntry.query.delete()
    Agent.query.delete()
    Run.query.delete()
    EnsembleRun.query.delete()
    db.session.commit()

    print("\n🧹 CLEARED ALL RUNS\n")
    return jsonify({"status": "cleared"})


# ── Debug ──

@api.route("/debug", methods=["GET"])
def debug_info():
    """Debug info about active processes and state."""
    active = {}
    for aid in agent_runner.active_agents:
        buf = agent_runner.get_stream_buffer(aid)
        event_count = len(buf.events) if buf else 0
        active[aid[:8]] = {
            "mode": agent_runner.mode,
            "stream_events": event_count,
            "finished": buf.finished if buf else False,
        }

    runs = Run.query.all()
    run_info = []
    for r in runs:
        agents = [{"name": a.name, "status": a.status, "stage": a.stage_name,
                    "worktree": a.worktree_path, "model": a.model} for a in r.agents]
        run_info.append({"id": r.id[:8], "title": r.title, "status": r.status,
                         "stage_index": r.current_stage_index, "agents": agents})

    try:
        worktrees = git_service.list_worktrees() if git_service else []
    except Exception:
        worktrees = []

    return jsonify({
        "active_processes": active,
        "runner_mode": agent_runner.mode,
        "runs": run_info,
        "worktrees": worktrees,
    })


# ── Shortcut Poller ──


@api.route("/shortcut/status", methods=["GET"])
def shortcut_status():
    """Get Shortcut poller status."""
    if not shortcut_poller:
        return jsonify({"running": False, "workspaces": [], "has_token": False})
    return jsonify(shortcut_poller.get_status())


@api.route("/shortcut/connect", methods=["POST"])
def shortcut_connect():
    """Connect to Shortcut. Body: {"name": "workspace name", "query": "optional search query"}"""
    data = request.json or {}
    name = data.get("name", "").strip() or "My Workspace"
    query = data.get("query", "").strip()

    # Validate the token works
    try:
        info = shortcut_poller.validate_token()
        # Use actual workspace name from API
        name = info.get("workspace", name)
    except Exception as e:
        token_hint = ""
        if not shortcut_poller.token:
            token_hint = " — set SHORTCUT_API_TOKEN env var first"
        return jsonify({"error": f"Can't connect to Shortcut: {e}{token_hint}"}), 400

    shortcut_poller.add_workspace(name, query)

    # Start poller if not running
    if not shortcut_poller._running:
        shortcut_poller.load_seen_ids()
        shortcut_poller.start()

    return jsonify(shortcut_poller.get_status())


@api.route("/shortcut/disconnect", methods=["POST"])
def shortcut_disconnect():
    """Disconnect a workspace. Body: {"name": "workspace name"}"""
    data = request.json or {}
    name = data.get("name", "").strip()
    if name:
        shortcut_poller.remove_workspace(name)

    if not shortcut_poller.workspaces:
        shortcut_poller.stop()

    return jsonify(shortcut_poller.get_status())


@api.route("/shortcut/query", methods=["POST"])
def shortcut_update_query():
    """Update the search query for a workspace. Body: {"name": "...", "query": "..."}"""
    data = request.json or {}
    name = data.get("name", "").strip()
    query = data.get("query", "").strip()
    if name:
        shortcut_poller.update_query(name, query)
    return jsonify(shortcut_poller.get_status())


@api.route("/shortcut/poll", methods=["POST"])
def shortcut_poll_now():
    """Trigger an immediate poll."""
    if not shortcut_poller or not shortcut_poller.workspaces:
        return jsonify({"error": "No Shortcut workspaces configured"}), 400
    created = shortcut_poller.poll_once()
    return jsonify({"created": len(created), "signals": created})


@api.route("/shortcut/story/<int:story_id>", methods=["GET"])
def shortcut_story_detail(story_id):
    """Fetch full story details including comments from Shortcut API."""
    if not shortcut_poller or not shortcut_poller.token:
        return jsonify({"error": "Shortcut not configured"}), 400

    try:
        story = shortcut_poller._sc_get(f"/stories/{story_id}")
        comments = story.get("comments", [])

        return jsonify({
            "id": story.get("id"),
            "name": story.get("name", ""),
            "description": story.get("description", ""),
            "story_type": story.get("story_type", ""),
            "app_url": story.get("app_url", ""),
            "labels": [l.get("name", "") for l in story.get("labels", []) if isinstance(l, dict)],
            "custom_fields": [
                {"name": cf.get("field_id", ""), "value": cf.get("value", "")}
                for cf in story.get("custom_fields", []) if isinstance(cf, dict) and cf.get("value")
            ],
            "tasks": [
                {"description": t.get("description", ""), "complete": t.get("complete", False)}
                for t in story.get("tasks", []) if isinstance(t, dict)
            ],
            "comments": [
                {
                    "author": c.get("author_id", ""),
                    "text": c.get("text", ""),
                    "created_at": c.get("created_at", ""),
                }
                for c in sorted(comments, key=lambda x: x.get("created_at", ""))
                if isinstance(c, dict)
            ],
            "estimate": story.get("estimate"),
            "deadline": story.get("deadline"),
            "created_at": story.get("created_at", ""),
            "updated_at": story.get("updated_at", ""),
            "owners": story.get("owner_ids", []),
            "workflow_state_id": story.get("workflow_state_id"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@api.route("/shortcut/import", methods=["POST"])
def shortcut_import():
    """One-time import of stories matching the query."""
    data = request.json or {}
    query = data.get("query", "")

    try:
        items = shortcut_poller.fetch_stories(query)
        created = 0
        for item in items:
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
            shortcut_poller._seen_ids.add(item["source_id"])
            created += 1

        return jsonify({"imported": created})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ── Settings ──

@api.route("/settings", methods=["GET"])
def get_settings():
    """Get current settings + runner status."""
    status = agent_runner.get_status()
    # Add chat service info
    status["chat_available"] = chat_service.available() if chat_service else False
    status["chat_mode"] = chat_service.mode() if chat_service else "none"
    return jsonify(status)


# ── Sentry Poller ──

@api.route("/sentry/status", methods=["GET"])
def sentry_status():
    """Get Sentry poller status."""
    if not sentry_poller:
        return jsonify({"running": False, "projects": [], "has_token": False})
    return jsonify(sentry_poller.get_status())


@api.route("/sentry/connect", methods=["POST"])
def sentry_connect():
    """Connect to a Sentry project. Body: {"org": "...", "project": "...", "level_filter": [...]}"""
    data = request.json or {}
    org = data.get("org", "").strip()
    project = data.get("project", "").strip()

    if not org or not project:
        return jsonify({"error": "org and project are required"}), 400

    # Validate the token works and the project exists
    try:
        sentry_poller._sentry_get(f"/projects/{org}/{project}/")
    except Exception as e:
        token_hint = ""
        if not sentry_poller.token:
            token_hint = " — set SENTRY_AUTH_TOKEN env var first"
        elif "401" in str(e):
            token_hint = " — token may be invalid or expired"
        elif "403" in str(e):
            token_hint = " — token may lack permissions (needs project:read scope)"
        elif "404" in str(e):
            token_hint = " — project not found (check org slug and project slug)"
        return jsonify({"error": f"Can't connect to Sentry {org}/{project}: {e}{token_hint}"}), 400

    level_filter = data.get("level_filter", [])
    sentry_poller.add_project(org, project, level_filter)

    # Start poller if not running
    if not sentry_poller._running:
        sentry_poller.load_seen_ids()
        sentry_poller.start()

    return jsonify(sentry_poller.get_status())


@api.route("/sentry/disconnect", methods=["POST"])
def sentry_disconnect():
    """Disconnect a project. Body: {"org": "...", "project": "..."}"""
    data = request.json or {}
    org = data.get("org", "").strip()
    project = data.get("project", "").strip()
    if org and project:
        sentry_poller.remove_project(org, project)

    if not sentry_poller.projects:
        sentry_poller.stop()

    return jsonify(sentry_poller.get_status())


@api.route("/sentry/orgs", methods=["GET"])
def sentry_list_orgs():
    """List organizations accessible with the current token."""
    if not sentry_poller or not sentry_poller.token:
        return jsonify({"error": "No SENTRY_AUTH_TOKEN configured"}), 400
    try:
        result = sentry_poller.validate_token()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@api.route("/sentry/projects/<org>", methods=["GET"])
def sentry_list_projects(org):
    """List projects in a Sentry organization."""
    if not sentry_poller or not sentry_poller.token:
        return jsonify({"error": "No SENTRY_AUTH_TOKEN configured"}), 400
    try:
        projects = sentry_poller.fetch_projects(org)
        return jsonify({"projects": projects})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@api.route("/sentry/poll", methods=["POST"])
def sentry_poll_now():
    """Trigger an immediate poll."""
    if not sentry_poller or not sentry_poller.projects:
        return jsonify({"error": "No Sentry projects configured"}), 400
    created = sentry_poller.poll_once()
    return jsonify({"created": len(created), "signals": created})


@api.route("/sentry/import", methods=["POST"])
def sentry_import():
    """One-time import of issues from a project. Body: {"org": "...", "project": "...", "level_filter": [...]}"""
    data = request.json or {}
    org = data.get("org", "").strip()
    project = data.get("project", "").strip()
    if not org or not project:
        return jsonify({"error": "org and project required"}), 400

    level_filter = data.get("level_filter", [])

    try:
        items = sentry_poller.fetch_issues(org, project, level_filter)
        created = 0
        for item in items:
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
            sentry_poller._seen_ids.add(item["source_id"])
            created += 1

        db.session.commit()
        return jsonify({"imported": created})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@api.route("/sentry/issue/<issue_id>", methods=["GET"])
def sentry_issue_detail(issue_id):
    """Fetch full issue details including stack trace from Sentry API."""
    if not sentry_poller or not sentry_poller.token:
        return jsonify({"error": "Sentry not configured"}), 400

    try:
        detail = sentry_poller.fetch_issue_detail(issue_id)
        return jsonify(detail)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ── Clusters ──

@api.route("/clusters", methods=["GET"])
def list_clusters():
    """List clusters with optional status filter and pagination."""
    query = SignalCluster.query.order_by(SignalCluster.created_at.desc())
    status = request.args.get("status")
    if status:
        query = query.filter_by(status=status)
    offset = request.args.get("offset", 0, type=int)
    limit = min(request.args.get("limit", 50, type=int), 200)
    total = query.count()
    clusters = query.offset(offset).limit(limit).all()
    return jsonify({
        "clusters": [c.to_dict() for c in clusters],
        "total": total,
        "offset": offset,
        "limit": limit,
    })


@api.route("/clusters/<cluster_id>", methods=["GET"])
def get_cluster(cluster_id):
    """Get cluster detail with member signal summaries."""
    cluster = SignalCluster.query.get_or_404(cluster_id)
    return jsonify(cluster.to_dict())


@api.route("/clusters", methods=["POST"])
def create_cluster():
    """Manually create a cluster from signal IDs. Body: {"signal_ids": [...], "title": "..."}"""
    data = request.json or {}
    signal_ids = data.get("signal_ids", [])
    if len(signal_ids) < 2:
        return jsonify({"error": "At least 2 signal IDs required"}), 400

    signals = Signal.query.filter(Signal.id.in_(signal_ids)).all()
    if len(signals) < 2:
        return jsonify({"error": "Not enough valid signals found"}), 400

    # Determine repo_id from signals
    repo_ids = {s.repo_id for s in signals if s.repo_id}
    cluster_repo_id = repo_ids.pop() if len(repo_ids) == 1 else None

    # Determine severity (highest)
    severity = "low"
    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    for s in signals:
        if sev_order.get(s.severity, 3) < sev_order.get(severity, 3):
            severity = s.severity

    # Merge files_hint
    all_files = set()
    for s in signals:
        all_files.update(s.get_files_hint())

    cluster = SignalCluster(
        title=data.get("title", f"Cluster of {len(signals)} signals"),
        severity=severity,
        status="open",
        repo_id=cluster_repo_id,
        files_hint=json.dumps(list(all_files)[:20]),
    )
    db.session.add(cluster)
    db.session.flush()

    for s in signals:
        s.cluster_id = cluster.id
        s.proposed_run = ""
    db.session.commit()

    return jsonify(cluster.to_dict()), 201


@api.route("/clusters/<cluster_id>", methods=["DELETE"])
def delete_cluster(cluster_id):
    """Delete a cluster and uncluster all member signals."""
    cluster = SignalCluster.query.get_or_404(cluster_id)
    for sig in cluster.signals:
        sig.cluster_id = None
    db.session.delete(cluster)
    db.session.commit()
    return jsonify({"status": "deleted"})


@api.route("/clusters/<cluster_id>/signals", methods=["POST"])
def modify_cluster_signals(cluster_id):
    """Add/remove signals from a cluster. Body: {"add": [...], "remove": [...]}"""
    cluster = SignalCluster.query.get_or_404(cluster_id)
    data = request.json or {}

    for sid in data.get("add", []):
        sig = Signal.query.get(sid)
        if sig:
            sig.cluster_id = cluster.id
            sig.proposed_run = ""

    for sid in data.get("remove", []):
        sig = Signal.query.get(sid)
        if sig and sig.cluster_id == cluster.id:
            sig.cluster_id = None

    db.session.commit()

    # Auto-delete empty cluster
    if cluster.signal_count() == 0:
        db.session.delete(cluster)
        db.session.commit()
        return jsonify({"status": "cluster_deleted_empty"})

    # Recalculate severity and files_hint
    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    highest = "low"
    all_files = set()
    for sig in cluster.signals:
        if sev_order.get(sig.severity, 3) < sev_order.get(highest, 3):
            highest = sig.severity
        all_files.update(sig.get_files_hint())
    cluster.severity = highest
    cluster.files_hint = json.dumps(list(all_files)[:20])
    db.session.commit()

    return jsonify(cluster.to_dict())


@api.route("/cases/from-signal", methods=["POST"])
def create_case_from_signal():
    """Promote an inbox signal to a single-signal case (SignalCluster)."""
    data = request.json or {}
    signal_id = data.get("signal_id")
    if not signal_id:
        return jsonify({"error": "signal_id is required"}), 400

    signal = Signal.query.get(signal_id)
    if not signal:
        return jsonify({"error": "Signal not found"}), 404
    if signal.cluster_id:
        return jsonify({"error": "Signal already belongs to a case"}), 400

    cluster = SignalCluster(
        title=signal.title,
        summary=signal.summary or "",
        severity=signal.severity or "medium",
        status="open",
        files_hint=signal.files_hint or "[]",
        repo_id=signal.repo_id,
    )
    db.session.add(cluster)
    db.session.flush()

    signal.cluster_id = cluster.id
    db.session.commit()

    return jsonify(cluster.to_dict()), 201


@api.route("/clusters/<cluster_id>/triage", methods=["POST"])
def triage_cluster(cluster_id):
    """Trigger triage on a specific cluster."""
    if not clustering_service:
        return jsonify({"error": "Clustering service not initialized"}), 500
    result = clustering_service.triage_cluster(cluster_id)
    if result.get("error"):
        return jsonify(result), 400
    return jsonify(result)


@api.route("/clusters/<cluster_id>/retry", methods=["POST"])
def retry_cluster(cluster_id):
    """Reset a failed cluster to open for re-triage."""
    cluster = SignalCluster.query.get_or_404(cluster_id)
    if cluster.status not in ("failed", "ready"):
        return jsonify({"error": f"Cannot retry cluster in status '{cluster.status}'"}), 400
    cluster.status = "open"
    cluster.proposed_run = ""
    cluster.run_id = None
    db.session.commit()
    return jsonify(cluster.to_dict())


@api.route("/clusters/run-now", methods=["POST"])
def clusters_run_now():
    """Trigger one full clustering cycle (Phase A + B) manually."""
    if not clustering_service:
        return jsonify({"error": "Clustering service not initialized"}), 500
    clustering_service.run_now()
    return jsonify({"status": "completed", **clustering_service.get_status()})


# ── Clustering Service Config ──

@api.route("/clustering/status", methods=["GET"])
def clustering_status():
    """Get clustering service status + config + stats."""
    if not clustering_service:
        return jsonify({"running": False, "enabled": False, "config": {}, "stats": {}})
    return jsonify(clustering_service.get_status())


@api.route("/clustering/config", methods=["POST"])
def clustering_config():
    """Update clustering config."""
    if not clustering_service:
        return jsonify({"error": "Clustering service not initialized"}), 500
    data = request.json or {}

    # Handle start/stop
    if "enabled" in data:
        if data["enabled"] and not clustering_service._running:
            clustering_service.start()
        elif not data["enabled"] and clustering_service._running:
            clustering_service.stop()

    updated = clustering_service.set_config(data)
    return jsonify({"config": updated})


# ── Self-Healing ──

@api.route("/self-healing/status", methods=["GET"])
def self_healing_status():
    """Get self-healing service status, rules, and stats."""
    if not self_healing:
        return jsonify({"running": False, "enabled": False, "rules": {}, "stats": {}})
    return jsonify(self_healing.get_status())


@api.route("/self-healing/start", methods=["POST"])
def self_healing_start():
    """Enable and start the self-healing loop. Optionally pass rules in body."""
    if not self_healing:
        return jsonify({"error": "Self-healing service not initialized"}), 500
    data = request.json or {}
    if data.get("rules"):
        self_healing.set_rules(data["rules"])
    self_healing.start()
    return jsonify(self_healing.get_status())


@api.route("/self-healing/stop", methods=["POST"])
def self_healing_stop():
    """Disable and stop the self-healing loop."""
    if not self_healing:
        return jsonify({"error": "Self-healing service not initialized"}), 500
    self_healing.stop()
    return jsonify(self_healing.get_status())


@api.route("/self-healing/rules", methods=["POST"])
def self_healing_rules():
    """Update self-healing rules without restarting."""
    if not self_healing:
        return jsonify({"error": "Self-healing service not initialized"}), 500
    data = request.json or {}
    updated = self_healing.set_rules(data)
    return jsonify({"rules": updated})


@api.route("/settings", methods=["POST"])
def update_settings():
    """Update settings. Supports: {"mode": "api"|"cli"}"""
    data = request.json

    if "mode" in data:
        new_mode = data["mode"]
        if new_mode not in ("api", "cli"):
            return jsonify({"error": "Mode must be 'api' or 'cli'"}), 400

        # Validate prerequisites
        if new_mode == "api" and not agent_runner.has_api_key():
            return jsonify({"error": "Cannot switch to API mode — no API key configured. Set ANTHROPIC_API_KEY."}), 400
        if new_mode == "cli" and not agent_runner.has_cli():
            return jsonify({"error": "Cannot switch to CLI mode — 'claude' command not found in PATH."}), 400

        # Don't switch while agents are running
        if agent_runner.count_active() > 0:
            return jsonify({"error": f"Cannot switch mode while {agent_runner.count_active()} agent(s) are running."}), 400

        agent_runner.mode = new_mode
        Setting.set("runner_mode", new_mode)
        print(f"⚙️ Runner mode changed to: {new_mode.upper()}")

    return jsonify(agent_runner.get_status())


# Need json import for workflow creation
# (already imported at top)
