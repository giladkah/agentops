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

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False


CONSENSUS_PROMPT = """You are a senior consensus engineer. Your job is to synthesize the BEST possible version
of a code change by analyzing multiple independent attempts at the same task.

You have {num_runs} independent diffs — each is a separate team's attempt at the same task.
They worked independently and didn't see each other's work.
{divergence_section}
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
            errors = 0
            max_errors = 10
            while errors < max_errors:
                time.sleep(10)
                try:
                    if self.app:
                        with self.app.app_context():
                            if self._check_done(ensemble_id):
                                return
                    else:
                        if self._check_done(ensemble_id):
                            return
                    errors = 0  # reset on success
                except Exception as e:
                    errors += 1
                    print(f"  🎯 Poll error ({errors}/{max_errors}): {e}")
                    import traceback
                    traceback.print_exc()
                    if errors >= max_errors:
                        print(f"  🎯 Poller giving up after {max_errors} consecutive errors")
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
        self._analyze_divergence(ensemble)

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

    def _analyze_divergence(self, ensemble: EnsembleRun):
        """AI-powered divergence analysis — one-shot Haiku call to explain WHY runs diverged."""
        # Prefer per-ensemble key, fall back to shared runner key
        _api_key = getattr(ensemble, 'anthropic_api_key', '') or self.runner.api_key
        if not HAS_ANTHROPIC or not _api_key:
            return

        comp = ensemble.get_comparison()
        runs_data = comp.get("runs", [])
        if len(runs_data) < 2:
            return

        # Build truncated diffs for the prompt (~3.5KB each)
        diffs_block = ""
        for i, rd in enumerate(runs_data):
            diff = rd.get("diff_full", "")[:3500]
            diffs_block += f"\n### Run {i+1}\n```diff\n{diff}\n```\n"

        prompt = f"""Analyze how these {len(runs_data)} independent code diffs diverge from each other.
They all attempted the same task: {ensemble.task_description}

{diffs_block}

## Divergence types
- converged: runs produced essentially the same code changes
- complementary: runs fixed different parts / files — changes can be merged
- conflicting: runs edited the same code differently — needs human decision
- partial: one run did significantly more work than another
- diverged: mixed / hard to classify

Return ONLY a JSON object (no markdown fences, no explanation):
{{
  "overall_type": "converged|complementary|conflicting|partial|diverged",
  "confidence": "high|medium|low",
  "summary": "2-3 sentence explanation of how and why the runs differ",
  "pairs": [
    {{"run_a": 0, "run_b": 1, "type": "complementary", "detail": "1 sentence"}}
  ],
  "consensus_hints": ["actionable merge hint 1", "hint 2"]
}}"""

        try:
            client = anthropic.Anthropic(api_key=_api_key)
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()

            # Strip markdown fences if present (same pattern as clustering_service)
            if text.startswith("```"):
                lines = text.split("\n")
                lines = [l for l in lines if not l.startswith("```")]
                text = "\n".join(lines)

            result = json.loads(text)
            result["source"] = "ai"

            comp["divergence"] = result
            ensemble.comparison_data = json.dumps(comp)
            db.session.commit()
            self._log(ensemble.id, f"Divergence analysis: {result.get('overall_type', '?')} ({result.get('confidence', '?')})")
        except Exception as e:
            print(f"  🎯 [ENSEMBLE] Divergence analysis failed (non-blocking): {e}")

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

        # Build divergence section for the consensus prompt
        divergence = comp.get("divergence")
        divergence_section = ""
        if divergence and divergence.get("source") == "ai":
            dtype = divergence.get("overall_type", "unknown")
            summary = divergence.get("summary", "")
            hints = divergence.get("consensus_hints", [])
            divergence_section = f"\n## Divergence Analysis (AI)\nOverall type: {dtype}\n{summary}\n"
            if hints:
                divergence_section += "Merge hints:\n" + "\n".join(f"- {h}" for h in hints) + "\n"

        prompt = CONSENSUS_PROMPT.format(
            num_runs=len(runs_data), diffs=diffs_text,
            task=ensemble.task_description, divergence_section=divergence_section,
        )

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
            worktree_path=wt_path, prompt=prompt, model="haiku",
            on_complete=lambda aid, ok, out: self._on_consensus_done(ensemble.id, ok, out),
            app=self.app,
            api_key=getattr(ensemble, 'anthropic_api_key', '') or None,
        )

    def _on_consensus_done(self, ensemble_id: str, success: bool, output: str):
        ensemble = EnsembleRun.query.get(ensemble_id)
        if not ensemble:
            return

        cost = estimate_cost("haiku", *AgentRunner.estimate_tokens(output))
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
            worktree_path=wt_path, prompt=prompt, model="haiku",
            on_complete=lambda aid, ok, out: self._on_review_done(ensemble.id, ok, out),
            app=self.app,
            api_key=getattr(ensemble, 'anthropic_api_key', '') or None,
        )

    def _on_review_done(self, ensemble_id: str, success: bool, output: str):
        ensemble = EnsembleRun.query.get(ensemble_id)
        if not ensemble:
            return

        cost = estimate_cost("haiku", *AgentRunner.estimate_tokens(output))
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
            return None

        comp = ensemble.get_comparison()
        runs_data = comp.get("runs", [])

        # ── Build rich PR body with full run details ──
        sev_icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}
        total_tokens = 0
        total_agents = 0

        md = []
        md.append("## 🎯 AgentOps Ensemble (Drift Detection)\n")
        md.append(f"**Task:** {ensemble.task_description}\n")
        md.append(f"**Method:** {ensemble.num_runs} independent runs → consensus → review\n")

        # Summary stats
        md.append(f"| Metric | Value |")
        md.append(f"|--------|-------|")
        md.append(f"| Parallel runs | {ensemble.num_runs} |")
        md.append(f"| Total cost | ${ensemble.total_cost:.2f} |")
        md.append(f"| Duration | {ensemble.duration_minutes()} min |")
        wf = ensemble.workflow
        if wf:
            md.append(f"| Workflow | {wf.name} |")
        md.append("")

        # Per-run breakdown
        md.append("### Runs\n")
        md.append("| # | Cost | Duration | Reviews | Agents | Status |")
        md.append("|---|------|----------|---------|--------|--------|")
        for i, rd in enumerate(runs_data):
            n_agents = len(rd.get("agents", []))
            total_agents += n_agents
            md.append(f"| {i+1} | ${rd.get('cost',0):.2f} | {rd.get('duration',0)}m | R{rd.get('review_rounds',0)} | {n_agents} | {rd.get('status','?')} |")
        md.append("")

        # Agent breakdown across all runs
        md.append("<details><summary><strong>Agent Breakdown</strong></summary>\n")
        md.append("| Agent | Stage | Issues | Cost | Status |")
        md.append("|-------|-------|--------|------|--------|")
        for i, rd in enumerate(runs_data):
            for ag in rd.get("agents", []):
                cost = ag.get("cost", 0) or 0
                md.append(f"| {ag.get('name','?')} (R{i+1}) | {ag.get('stage','')} | {ag.get('issues_found',0)} | ${cost:.3f} | {ag.get('status','')} |")
        md.append("\n</details>\n")

        # Divergence Analysis section (if AI analysis was run)
        divergence = comp.get("divergence")
        if divergence and divergence.get("source") == "ai":
            dtype = divergence.get("overall_type", "unknown")
            conf = divergence.get("confidence", "?")
            summary = divergence.get("summary", "")
            dtype_icon = {"converged": "=", "complementary": "+", "conflicting": "!", "partial": "~", "diverged": "?"}.get(dtype, "?")
            md.append(f"### {dtype_icon} Divergence Analysis\n")
            md.append(f"**Type:** {dtype} · **Confidence:** {conf}\n")
            md.append(f"{summary}\n")
            pair_details = divergence.get("pairs", [])
            if pair_details:
                md.append("<details><summary><strong>Pairwise Details</strong></summary>\n")
                for p in pair_details:
                    md.append(f"- **R{p.get('run_a',0)+1}:R{p.get('run_b',0)+1}** → {p.get('type','?')} — {p.get('detail','')}")
                md.append("\n</details>\n")
            hints = divergence.get("consensus_hints", [])
            if hints:
                md.append("**Merge hints:**")
                for h in hints:
                    md.append(f"- {h}")
                md.append("")

        # Collect review issues from consensus run (if available)
        consensus_run = Run.query.get(ensemble.consensus_run_id) if ensemble.consensus_run_id else None
        review_issues = []
        if consensus_run:
            for a in consensus_run.agents:
                if a.stage_name in ("review", "review-quality", "review-security", "security"):
                    for iss in a.get_structured_issues():
                        review_issues.append(iss)
            total_tokens += (consensus_run.total_tokens_in or 0) + (consensus_run.total_tokens_out or 0)

        # Also tally tokens from child runs
        for run_id in ensemble.get_run_ids():
            run = Run.query.get(run_id)
            if run:
                total_tokens += (run.total_tokens_in or 0) + (run.total_tokens_out or 0)

        if review_issues:
            md.append("### Review Findings\n")
            for iss in sorted(review_issues, key=lambda x: {"critical":0,"high":1,"medium":2,"low":3}.get(x.get("severity","medium"), 2)):
                sev = iss.get("severity", "medium")
                icon = sev_icon.get(sev, "⚪")
                loc = ""
                if iss.get("file"):
                    loc = f" `{iss['file']}"
                    if iss.get("line"): loc += f":{iss['line']}"
                    loc += "`"
                md.append(f"- {icon} **{iss.get('title', 'Issue')}**{loc}")
                if iss.get("note"):
                    md.append(f"  > {iss['note'][:300]}\n")
            md.append("")

        # Token + cost summary
        md.append("---\n")
        md.append(f"**Totals:** ${ensemble.total_cost:.2f} · {ensemble.duration_minutes()} min · {total_tokens:,} tokens · {total_agents} agents\n")
        md.append("---\n*Generated by [AgentOps](https://github.com/your-org/agentops) Ensemble / Drift Detection*")

        pr_body = "\n".join(md)

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
        return pr_url if ok else None

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

    def cancel_ensemble(self, ensemble_id: str) -> bool:
        """Cancel an ensemble and all its child runs."""
        ensemble = EnsembleRun.query.get(ensemble_id)
        if not ensemble:
            return False
        if ensemble.status in ("done", "failed", "cancelled"):
            return False

        # Cancel all child runs
        child_runs = Run.query.filter_by(ensemble_id=ensemble_id).all()
        for run in child_runs:
            if run.status not in ("done", "merged", "failed"):
                self.orch.cancel_run(run.id)

        ensemble.status = "failed"
        ensemble.finished_at = datetime.now(timezone.utc)
        db.session.commit()
        self._log(ensemble_id, "Ensemble cancelled", "warning")
        return True
