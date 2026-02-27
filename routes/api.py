"""
API Routes — REST endpoints for the AgentOps dashboard.
"""
import json
import time
from flask import Blueprint, request, jsonify, Response
from models import db, Run, Agent, Persona, Workflow, LogEntry, EnsembleRun, Setting
from services.orchestrator import RunOrchestrator
from services.git_service import GitService
from services.agent_runner import AgentRunner
from services.ensemble import EnsembleOrchestrator

api = Blueprint("api", __name__, url_prefix="/api")

# These get initialized in app.py
git_service: GitService = None
agent_runner: AgentRunner = None
orchestrator: RunOrchestrator = None
ensemble_orchestrator: EnsembleOrchestrator = None


def init_services(repo_path: str, app=None, api_key: str = None):
    global git_service, agent_runner, orchestrator, ensemble_orchestrator
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

    print(f"🔌 Runner mode: {mode.upper()}")


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
        return jsonify({"status": "merged", "message": msg})
    return jsonify({"error": msg}), 400


@api.route("/runs/<run_id>/cancel", methods=["POST"])
def cancel_run(run_id):
    success = orchestrator.cancel_run(run_id)
    if success:
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
        default_model=data.get("default_model", "sonnet-4.5"),
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
    return jsonify({"status": "pr_created"})


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

    return jsonify({
        "total_runs": total_runs,
        "total_cost": round(total_cost, 2),
        "active_runs": active_runs,
        "active_agents": active_agents,
        "active_ensembles": active_ensembles,
    })


# ── Clear All ──

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


# ── Settings ──

@api.route("/settings", methods=["GET"])
def get_settings():
    """Get current settings + runner status."""
    status = agent_runner.get_status()
    return jsonify(status)


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
