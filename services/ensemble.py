"""
Ensemble Orchestrator — Drift Detection.
Launches N identical runs in parallel, compares results, synthesizes consensus.
"""
import json
import threading
from datetime import datetime, timezone

from models import db, Run, Agent, Workflow, Persona, LogEntry, EnsembleRun
from services.orchestrator import RunOrchestrator
from services.git_service import GitService
from services.agent_runner import AgentRunner, estimate_cost


CONSENSUS_PROMPT = """You are a senior consensus engineer. Your job is to synthesize the BEST possible version
of a code change by analyzing multiple independent attempts at the same task.

You have {num_runs} independent diffs — each is a separate team's attempt at the same task.
They worked independently and didn't see each other's work.

## Process
1. READ all diffs carefully
2. Changes ALL runs agree on → high confidence, apply them
3. Changes MOST runs agree on → medium confidence, apply with judgment
4. Divergent changes → pick the best version or skip
5. APPLY the consensus changes to the codebase
6. Do NOT invent new changes that none of the runs suggested
7. Run tests after applying

## Diffs:
{diffs}

## Original task:
{task}

At the end, output:
✅ CONSENSUS COMPLETE
- High confidence (N/N agree): [list]
- Medium confidence (majority): [list]
- Skipped (low agreement): [list]
"""

CONSENSUS_REVIEW_PROMPT = """You are reviewing a consensus synthesis.

The consensus agent merged {num_runs} independent attempts at the same task. Verify:
1. Did it correctly identify shared changes?
2. Did it miss changes multiple runs agreed on?
3. Did it incorrectly apply something only one run suggested?
4. Run tests to verify.

If you find issues, FIX them directly.

## Original task:
{task}

## The {num_runs} original diffs:
{diffs}

Report: ✅ REVIEW COMPLETE - Issues found: N - Issues fixed: N
"""


class EnsembleOrchestrator:

    def __init__(self, orchestrator: RunOrchestrator, git: GitService, runner: AgentRunner, app=None):
        self.orch = orchestrator
        self.git = git
        self.runner = runner
        self.app = app

    def _log(self, ensemble_id: str, message: str, level: str = "info"):
        print(f"  🎯 [ENSEMBLE] [{level.upper()}] {message}")
        run = Run.query.filter_by(ensemble_id=ensemble_id).first()
        if run:
            entry = LogEntry(run_id=run.id, agent_name="🎯 Ensemble", level=level, message=message)
            db.session.add(entry)
            db.session.commit()

    def create_ensemble(self, workflow_id: str, title: str, task_description: str,
                        agent_configs: list[dict], num_runs: int = 3,
                        base_branch: str = "main", auto_approve: bool = True,
                        individual_prs: bool = False) -> EnsembleRun:
        workflow = Workflow.query.get(workflow_id)
        if not workflow:
            raise ValueError(f"Workflow {workflow_id} not found")

        ensemble = EnsembleRun(
            title=title,
            task_description=task_description,
            workflow_id=workflow_id,
            num_runs=num_runs,
            status="pending",
            base_branch=base_branch,
            auto_approve=auto_approve,
            individual_prs=individual_prs,
        )
        db.session.add(ensemble)
        db.session.flush()

        run_ids = []
        for i in range(num_runs):
            run = self.orch.create_run(
                workflow_id=workflow_id,
                title=f"{title} [Run {i+1}/{num_runs}]",
                task_description=task_description,
                agent_configs=agent_configs,
                target_branch=f"agentops/ensemble-{ensemble.id[:8]}-run{i+1}",
                source_type="ensemble",
                source_id=ensemble.id,
                auto_approve=True,
                base_branch=base_branch,
            )
            run.ensemble_id = ensemble.id
            run_ids.append(run.id)
            db.session.commit()

        ensemble.set_run_ids(run_ids)
        db.session.commit()
        print(f"\n🎯 ENSEMBLE CREATED: {ensemble.id[:8]} — {num_runs} runs")
        return ensemble

    def start_ensemble(self, ensemble_id: str) -> bool:
        ensemble = EnsembleRun.query.get(ensemble_id)
        if not ensemble or ensemble.status != "pending":
            return False

        ensemble.status = "running"
        ensemble.started_at = datetime.now(timezone.utc)
        db.session.commit()
        self._log(ensemble_id, f"Starting {ensemble.num_runs} parallel runs...")

        for run_id in ensemble.get_run_ids():
            self.orch.start_run(run_id)

        self._start_poller(ensemble_id)
        return True

    def _start_poller(self, ensemble_id: str):
        def _poll():
            import time
            while True:
                time.sleep(10)
                try:
                    if self.app:
                        with self.app.app_context():
                            if self._check_done(ensemble_id):
                                return
                    else:
                        if self._check_done(ensemble_id):
                            return
                except Exception as e:
                    print(f"  🎯 Poll error: {e}")
                    import traceback
                    traceback.print_exc()
                    return

        threading.Thread(target=_poll, daemon=True).start()

    def _check_done(self, ensemble_id: str) -> bool:
        ensemble = EnsembleRun.query.get(ensemble_id)
        if not ensemble or ensemble.status != "running":
            return True

        runs = Run.query.filter(Run.ensemble_id == ensemble_id).all()
        terminal = {"converged", "merged", "failed"}
        if any(r.status not in terminal for r in runs):
            return False

        ensemble.total_cost = sum(r.total_cost for r in runs)
        db.session.commit()

        success_count = sum(1 for r in runs if r.status in ("converged", "merged"))
        self._log(ensemble_id, f"All {len(runs)} runs complete. {success_count} succeeded.", "success")

        if success_count < 2:
            ensemble.status = "failed"
            db.session.commit()
            self._log(ensemble_id, "Need at least 2 successful runs.", "error")
            return True

        self._build_comparison(ensemble)

        if ensemble.auto_approve:
            self._start_consensus(ensemble)
        else:
            ensemble.status = "comparing"
            db.session.commit()
            self._log(ensemble_id, "⏸ Comparison ready. Approve to start consensus.", "warning")
        return True

    def _build_comparison(self, ensemble: EnsembleRun):
        runs = Run.query.filter(
            Run.ensemble_id == ensemble.id,
            Run.status.in_(["converged", "merged"])
        ).all()

        comparison = {"runs": [], "total_runs": len(runs)}
        for run in runs:
            rd = {
                "id": run.id, "title": run.title, "status": run.status,
                "cost": run.total_cost, "duration": run.duration_minutes(),
                "review_rounds": run.review_round,
                "review_history": run.get_review_history(),
                "agents": [{"name": a.name, "stage": a.stage_name,
                            "issues_found": a.issues_found, "cost": a.cost,
                            "status": a.status,
                            "output_preview": (a.output_log or "")[:500]} for a in run.agents],
            }

            # Check for synthesis worktree first (parallel reviews merge into this)
            synth_name = f"run-{run.id[:8]}-synthesis"
            synth_path = self.git.worktree_base + "/" + synth_name
            import os
            if os.path.exists(synth_path):
                branch = f"agentops/{synth_name}"
                rd["diff_stat"] = self.git.get_diff(branch, ensemble.base_branch or "main")
                rd["diff_full"] = self.git.get_diff_full(branch, ensemble.base_branch or "main")[:10000]
                rd["source"] = "synthesis"
            else:
                # Fallback to individual agent worktrees
                best = next((a for a in run.agents if a.stage_name == "review" and a.worktree_path),
                            next((a for a in run.agents if a.stage_name == "engineer" and a.worktree_path), None))
                if best and best.worktree_path:
                    wt = best.worktree_path.split("/")[-1]
                    rd["diff_stat"] = self.git.get_diff(f"agentops/{wt}", ensemble.base_branch or "main")
                    rd["diff_full"] = self.git.get_diff_full(f"agentops/{wt}", ensemble.base_branch or "main")[:10000]
                    rd["source"] = "agent"
            comparison["runs"].append(rd)

        ensemble.comparison_data = json.dumps(comparison)
        db.session.commit()
        self._log(ensemble.id, f"Comparison built: {len(runs)} runs analyzed")

    def _start_consensus(self, ensemble: EnsembleRun):
        ensemble.status = "synthesizing"
        db.session.commit()
        self._log(ensemble.id, "🧮 Starting consensus synthesis...")

        comp = ensemble.get_comparison()
        runs_data = comp.get("runs", [])

        diffs_text = ""
        for i, rd in enumerate(runs_data):
            diffs_text += f"\n### Run {i+1}: {rd.get('title','?')}\n"
            diffs_text += f"Cost: ${rd.get('cost',0):.2f}, Duration: {rd.get('duration',0)}m\n"
            for ag in rd.get("agents", []):
                if ag.get("output_preview"):
                    diffs_text += f"\n**{ag['name']}** ({ag['stage']}):\n{ag['output_preview']}\n"
            diff = rd.get("diff_full", "")
            if diff:
                diffs_text += f"\n```diff\n{diff[:5000]}\n```\n"

        prompt = CONSENSUS_PROMPT.format(num_runs=len(runs_data), diffs=diffs_text, task=ensemble.task_description)

        # Base off first successful run's synthesis or review branch
        import os
        base_branch = ensemble.base_branch or "main"
        for rd in runs_data:
            r = Run.query.get(rd["id"])
            if r:
                # Check for synthesis worktree first (parallel reviews)
                synth_name = f"run-{r.id[:8]}-synthesis"
                synth_path = self.git.worktree_base + "/" + synth_name
                if os.path.exists(synth_path):
                    base_branch = f"agentops/{synth_name}"
                    break
                # Fallback to individual agent worktree
                best = next((a for a in r.agents if a.stage_name == "review" and a.worktree_path),
                            next((a for a in r.agents if a.stage_name == "engineer" and a.worktree_path), None))
                if best and best.worktree_path:
                    base_branch = f"agentops/{best.worktree_path.split('/')[-1]}"
                    break

        wt_name = f"ensemble-{ensemble.id[:8]}-consensus"
        success, wt_path = self.git.create_worktree(wt_name, base_branch)
        if not success:
            self._log(ensemble.id, f"Worktree failed: {wt_path}", "error")
            ensemble.status = "failed"
            db.session.commit()
            return

        ensemble.consensus_run_id = wt_name
        db.session.commit()

        self.runner.launch_agent(
            agent_id=f"consensus-{ensemble.id[:8]}",
            worktree_path=wt_path, prompt=prompt, model="opus",
            on_complete=lambda aid, ok, out: self._on_consensus_done(ensemble.id, ok, out),
            app=self.app,
        )

    def _on_consensus_done(self, ensemble_id: str, success: bool, output: str):
        ensemble = EnsembleRun.query.get(ensemble_id)
        if not ensemble:
            return

        cost = estimate_cost("opus", *AgentRunner.estimate_tokens(output))
        ensemble.total_cost += cost
        db.session.commit()

        if not success:
            self._log(ensemble_id, f"Consensus failed (${cost:.2f})", "error")
            ensemble.status = "failed"
            db.session.commit()
            return

        wt = ensemble.consensus_run_id or f"ensemble-{ensemble.id[:8]}-consensus"
        self.git.commit_worktree(wt, f"AgentOps: consensus — {ensemble.title}")
        self._log(ensemble_id, f"Consensus done (${cost:.2f}). Reviewing...", "success")
        self._start_review(ensemble)

    def _start_review(self, ensemble: EnsembleRun):
        ensemble.status = "reviewing"
        db.session.commit()

        comp = ensemble.get_comparison()
        runs_data = comp.get("runs", [])
        diffs = "\n".join(f"### Run {i+1}:\n```diff\n{rd.get('diff_full','')[:3000]}\n```"
                          for i, rd in enumerate(runs_data))

        prompt = CONSENSUS_REVIEW_PROMPT.format(num_runs=len(runs_data), diffs=diffs, task=ensemble.task_description)
        wt = ensemble.consensus_run_id or f"ensemble-{ensemble.id[:8]}-consensus"
        wt_path = self.git.worktree_base + "/" + wt

        self._log(ensemble.id, "🔍 Consensus reviewer started")
        self.runner.launch_agent(
            agent_id=f"review-{ensemble.id[:8]}",
            worktree_path=wt_path, prompt=prompt, model="opus",
            on_complete=lambda aid, ok, out: self._on_review_done(ensemble.id, ok, out),
            app=self.app,
        )

    def _on_review_done(self, ensemble_id: str, success: bool, output: str):
        ensemble = EnsembleRun.query.get(ensemble_id)
        if not ensemble:
            return

        cost = estimate_cost("opus", *AgentRunner.estimate_tokens(output))
        ensemble.total_cost += cost
        db.session.commit()

        wt = ensemble.consensus_run_id or f"ensemble-{ensemble.id[:8]}-consensus"
        self.git.commit_worktree(wt, f"AgentOps: consensus review — {ensemble.title}")
        self._log(ensemble_id, f"Review done (${cost:.2f}). Total: ${ensemble.total_cost:.2f}", "success")

        if ensemble.auto_approve:
            self._finalize(ensemble)
        else:
            ensemble.status = "done"
            db.session.commit()
            self._log(ensemble_id, "⏸ Ready for PR.", "warning")

    def _finalize(self, ensemble: EnsembleRun):
        wt = ensemble.consensus_run_id or f"ensemble-{ensemble.id[:8]}-consensus"
        branch = f"agentops/{wt}"

        self.git.commit_worktree(wt, f"AgentOps: final — {ensemble.title}")
        ok, msg = self.git.push_branch(branch)
        if not ok:
            self._log(ensemble.id, f"Push failed: {msg}", "error")
            ensemble.status = "done"
            db.session.commit()
            return

        comp = ensemble.get_comparison()
        runs_data = comp.get("runs", [])
        pr_body = "\n".join([
            "## 🎯 AgentOps Ensemble (Drift Detection)",
            "", f"**Task:** {ensemble.task_description}",
            f"**Method:** {ensemble.num_runs} independent runs → consensus → review",
            "", "### Runs",
            "| # | Cost | Duration | Reviews | Status |",
            "|---|------|----------|---------|--------|",
        ] + [f"| {i+1} | ${rd.get('cost',0):.2f} | {rd.get('duration',0)}m | {rd.get('review_rounds',0)} | {rd.get('status','?')} |"
             for i, rd in enumerate(runs_data)] + [
            "", f"**Total:** ${ensemble.total_cost:.2f} · {ensemble.duration_minutes()} min",
            "", "---", "*AgentOps Ensemble / Drift Detection*",
        ])

        ok, pr_url = self.git.create_pr(branch=branch, title=f"[AgentOps Ensemble] {ensemble.title}",
                                         body=pr_body, base=ensemble.base_branch or "main")
        if ok:
            self._log(ensemble.id, f"✅ PR: {pr_url}", "success")
        else:
            self._log(ensemble.id, f"PR failed: {pr_url}. Branch pushed.", "warning")

        # Cleanup
        for run_id in ensemble.get_run_ids():
            run = Run.query.get(run_id)
            if run:
                for a in run.agents:
                    if a.worktree_path:
                        self.git.remove_worktree(a.worktree_path.split("/")[-1])
                # Also clean up synthesis worktree from parallel reviews
                synth_wt = f"run-{run.id[:8]}-synthesis"
                self.git.remove_worktree(synth_wt)
        self.git.remove_worktree(wt)

        ensemble.status = "done"
        ensemble.finished_at = datetime.now(timezone.utc)
        db.session.commit()
        self._log(ensemble.id,
            f"✅ ENSEMBLE COMPLETE. {ensemble.num_runs} runs → consensus → PR. "
            f"${ensemble.total_cost:.2f}, {ensemble.duration_minutes()} min", "success")

    def approve_ensemble(self, ensemble_id: str) -> bool:
        ensemble = EnsembleRun.query.get(ensemble_id)
        if not ensemble:
            return False
        if ensemble.status == "comparing":
            self._start_consensus(ensemble)
            return True
        elif ensemble.status == "done" and not ensemble.finished_at:
            self._finalize(ensemble)
            return True
        return False
