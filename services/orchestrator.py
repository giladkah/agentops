"""
Run Orchestrator — Manages the execution of a workflow run.
Handles stage transitions, convergence logic, and human checkpoints.
"""
import json
import os
from datetime import datetime, timezone
from typing import Optional

from models import db, Run, Agent, Workflow, Persona, LogEntry, EnsembleRun
from services.git_service import GitService
from services.agent_runner import AgentRunner, estimate_cost, MAX_AGENTIC_TURNS, MAX_REVIEWER_TURNS, MAX_CONSENSUS_TURNS
import services.telemetry as telemetry



def _parse_structured_issues(output: str) -> list:
    import re as _re, json as _json
    m = _re.search(r'ISSUES_JSON_START\s*(\[.*?\])\s*ISSUES_JSON_END', output, _re.DOTALL)
    if m:
        try:
            issues = _json.loads(m.group(1))
            valid_sev = {"critical", "high", "medium", "low"}
            valid_cat = {"security", "architecture", "quality", "performance", "testing"}
            for iss in issues:
                if iss.get("severity") not in valid_sev: iss["severity"] = "medium"
                if iss.get("category") not in valid_cat: iss["category"] = "quality"
            return issues
        except Exception:
            pass
    return []

class RunOrchestrator:
    """Orchestrates a workflow run through its stages."""

    def __init__(self, git: GitService, runner: AgentRunner, app=None):
        self.git = git
        self.runner = runner
        self.app = app

    def _log(self, run_id: str, message: str, level: str = "info", agent_name: str = "System"):
        print(f"  📋 [{level.upper()}] [{agent_name}] {message}")
        entry = LogEntry(run_id=run_id, agent_name=agent_name, level=level, message=message)
        db.session.add(entry)
        db.session.commit()

    def create_run(self, workflow_id: str, title: str, task_description: str,
                   agent_configs: list[dict], target_branch: str = "",
                   source_type: str = "manual", source_id: str = None,
                   auto_approve: bool = False, base_branch: str = "main") -> Run:
        """
        Create a new run with its agents.

        agent_configs: [{"persona_id": "...", "model": "sonnet-4.5", "stage_name": "engineer"}, ...]
        """
        workflow = Workflow.query.get(workflow_id)
        if not workflow:
            raise ValueError(f"Workflow {workflow_id} not found")

        run = Run(
            workflow_id=workflow_id,
            title=title,
            task_description=task_description,
            target_branch=target_branch or f"agentops/run-{title[:30].replace(' ', '-').lower()}",
            status="pending",
            source_type=source_type,
            source_id=source_id,
            auto_approve=auto_approve,
            base_branch=base_branch,
        )
        db.session.add(run)
        db.session.flush()  # Get the run ID

        # Create agents
        for config in agent_configs:
            persona = Persona.query.get(config["persona_id"])
            if not persona:
                continue

            agent = Agent(
                run_id=run.id,
                persona_id=config["persona_id"],
                name=f"{persona.icon} {persona.name}",
                model=config.get("model", persona.default_model),
                stage_name=config.get("stage_name", persona.role),
                status="waiting",
            )
            db.session.add(agent)

        workflow.times_used += 1
        db.session.commit()

        self._log(run.id, f"Run created: {title}")
        return run

    def start_run(self, run_id: str) -> bool:
        """Start a run — advance to the first stage."""
        run = Run.query.get(run_id)
        if not run or run.status != "pending":
            return False

        run.status = "running"
        run.started_at = datetime.now(timezone.utc)
        db.session.commit()

        self._log(run.id, "Run started")
        return self._advance_stage(run)

    def _advance_stage(self, run: Run) -> bool:
        """Move to the next stage and launch its agents."""
        workflow = run.workflow
        stages = workflow.get_stages()

        if run.current_stage_index >= len(stages):
            # All stages done
            if run.ensemble_id:
                # Part of an ensemble — check if individual PRs are wanted
                ensemble = EnsembleRun.query.get(run.ensemble_id)
                if ensemble and ensemble.individual_prs:
                    # Create individual PR too
                    self._log(run.id, "All stages complete — creating individual PR...", "success")
                    db.session.commit()
                    self.merge_run(run.id, skip_cleanup=True)
                else:
                    # Default: only ensemble gets a PR, preserve worktrees for consensus
                    run.status = "converged"
                    run.finished_at = datetime.now(timezone.utc)
                    db.session.commit()
                    self._log(run.id, "All stages complete — ready for ensemble consensus", "success")
            elif run.auto_approve:
                self._log(run.id, "All stages complete — auto-merging...", "success")
                db.session.commit()
                success, msg = self.merge_run(run.id)
                if not success:
                    run.status = "needs_approval"
                    db.session.commit()
                    self._log(run.id, f"Auto-merge failed: {msg}. Manual merge needed.", "error")
            else:
                run.status = "converged"
                run.finished_at = datetime.now(timezone.utc)
                db.session.commit()
                self._log(run.id, "All stages complete — ready to open PR", "success")
                telemetry.track_run_completed(run)
            return True

        stage = stages[run.current_stage_index]
        stage_name = stage.get("name", f"stage-{run.current_stage_index}")

        self._log(run.id, f"Starting stage: {stage_name}")

        # Find agents for this stage
        stage_agents = [a for a in run.agents if a.stage_name == stage_name]

        if not stage_agents:
            # No agents for this stage, skip it
            run.current_stage_index += 1
            db.session.commit()
            return self._advance_stage(run)

        # Check if this stage has a checkpoint BEFORE running
        if stage.get("checkpoint_before"):
            if run.auto_approve:
                self._log(run.id, f"Auto-approved checkpoint before '{stage_name}'", "success")
            else:
                run.status = "needs_approval"
                db.session.commit()
                self._log(run.id, f"⏸ Checkpoint: approve to start '{stage_name}'", "warning")
                return True

        # Launch agents for this stage
        if stage.get("converge"):
            # Review stages: launch ALL reviewers in PARALLEL (each in own worktree)
            self._log(run.id, f"🔀 Launching {len(stage_agents)} reviewers in parallel")
            for agent in stage_agents:
                self._launch_agent(run, agent, stage)
        else:
            # Non-review stages: launch all in parallel
            for agent in stage_agents:
                self._launch_agent(run, agent, stage)

        return True

    def _launch_agent(self, run: Run, agent: Agent, stage: dict):
        """Create worktree and launch an agent."""
        # Each agent gets a unique worktree name using agent ID + round number to avoid collisions
        round_suffix = f"-r{run.review_round + 1}" if stage.get("converge") and run.review_round > 0 else ""
        wt_name = f"run-{run.id[:8]}-{agent.stage_name}-{agent.id[:8]}{round_suffix}"

        # Determine base branch — use the engineer's branch (or synthesis branch for round 2+)
        base = run.base_branch or "main"

        # Check for synthesis branch from previous review rounds
        synthesis_wt = f"run-{run.id[:8]}-synthesis"
        synthesis_path = os.path.join(self.git.worktree_base, synthesis_wt)
        if stage.get("converge") and os.path.exists(synthesis_path):
            # Round 2+: branch from the synthesized result
            base = f"agentops/{synthesis_wt}"
        else:
            # First round or non-review: branch from the previous stage's output
            for prev_agent in run.agents:
                if prev_agent.stage_name != agent.stage_name and prev_agent.status == "done" and prev_agent.worktree_path:
                    prev_wt_name = prev_agent.worktree_path.split("/")[-1]
                    base = f"agentops/{prev_wt_name}"
                    break

        success, result = self.git.create_worktree(wt_name, base)
        if not success:
            agent.status = "failed"
            db.session.commit()
            self._log(run.id, f"Failed to create worktree: {result}", "error", agent.name)
            return

        agent.worktree_path = result
        wt_path = result

        agent.status = "running"
        agent.started_at = datetime.now(timezone.utc)
        db.session.commit()

        # Build the prompt with enriched context from previous stage handoffs
        persona = agent.persona
        agent_role = persona.role if persona else agent.stage_name

        # Collect handoffs from completed agents in previous stages
        extra_context = f"Stage: {agent.stage_name}\nRun: {run.title}\nBranch: agentops/{wt_name}"

        previous_handoffs = []
        for prev_agent in run.agents:
            if prev_agent.stage_name != agent.stage_name and prev_agent.status in ("done", "converged"):
                try:
                    h = json.loads(prev_agent.handoff_json) if prev_agent.handoff_json else {}
                    if h and h != {}:
                        previous_handoffs.append({
                            "from": f"{prev_agent.name} ({prev_agent.stage_name})",
                            "handoff": h,
                        })
                except (json.JSONDecodeError, TypeError):
                    pass

        if previous_handoffs:
            extra_context += "\n\n## Previous Stage Results\n"
            for ph in previous_handoffs:
                handoff_str = json.dumps(ph['handoff'], indent=2)
                if len(handoff_str) > 3000:
                    handoff_str = handoff_str[:3000] + "\n... [truncated]"
                extra_context += f"\n### From {ph['from']}\n"
                extra_context += handoff_str + "\n"

        # For review round 2+, add round summary
        if stage.get("converge") and run.review_round > 0:
            from services.token_optimizer import build_review_round_summary
            round_summary = build_review_round_summary(run)
            if round_summary:
                extra_context += "\n\n" + round_summary

        prompt = self.runner.build_prompt(
            persona_template=persona.prompt_template,
            task=run.task_description,
            extra_context=extra_context,
        )
        agent.task_prompt = prompt
        db.session.commit()

        self._log(run.id, f"Agent launched (model: {agent.model})", "info", agent.name)

        # Determine turn limit based on role
        reviewer_roles = {"reviewer", "security", "qa", "architect-reviewer", "review", "review-quality", "review-security", "test-runner"}
        consensus_roles = {"consensus"}
        if agent.stage_name in reviewer_roles or (agent.persona and agent.persona.role in reviewer_roles):
            max_turns = MAX_REVIEWER_TURNS
        elif agent.stage_name in consensus_roles or (agent.persona and agent.persona.role in consensus_roles):
            max_turns = MAX_CONSENSUS_TURNS
        else:
            max_turns = MAX_AGENTIC_TURNS

        # Launch the agent process
        def on_complete(agent_id, success, output):
            self._on_agent_complete(agent_id, success, output)

        self.runner.launch_agent(
            agent_id=agent.id,
            worktree_path=wt_path,
            prompt=prompt,
            model=agent.model,
            on_complete=on_complete,
            app=self.app,
            max_turns=max_turns,
            agent_role=agent_role,
        )

    def _on_agent_complete(self, agent_id: str, success: bool, output: str):
        """Called when an agent finishes."""
        print(f"\n  📥 _on_agent_complete called: agent={agent_id[:8]}, success={success}")
        agent = Agent.query.get(agent_id)
        if not agent:
            print(f"  ❌ Agent {agent_id[:8]} not found in DB!")
            return

        agent.status = "done" if success else "failed"
        agent.finished_at = datetime.now(timezone.utc)
        agent.output_log = output[:50000]

        # Try to get EXACT token counts from the stream buffer (API mode)
        buf = self.runner.get_stream_buffer(agent_id)
        exact_stats = None
        if buf:
            for ev in reversed(buf.events):
                if ev.get("type") == "finished" and ev.get("data"):
                    exact_stats = ev["data"]
                    break

        if exact_stats and exact_stats.get("tokens_in"):
            agent.tokens_in = exact_stats["tokens_in"]
            agent.tokens_out = exact_stats["tokens_out"]
            agent.cost = exact_stats["cost"]
        else:
            # Fallback to estimation (shouldn't happen with API mode)
            tokens_in, tokens_out = AgentRunner.estimate_tokens(output)
            agent.tokens_in = tokens_in
            agent.tokens_out = tokens_out
            agent.cost = estimate_cost(agent.model, tokens_in, tokens_out)

        if agent.stage_name in ("review", "review-quality", "review-security"):
            agent.issues_found = AgentRunner.parse_output_issues(output)
            structured = _parse_structured_issues(output)
            if structured:
                import json as _j
                agent.issues_json = _j.dumps(structured)
                agent.issues_found = max(agent.issues_found, len(structured))

        if agent.worktree_path:
            wt_name = agent.worktree_path.split("/")[-1]
            self.git.commit_worktree(wt_name, f"AgentOps: {agent.name} — {agent.stage_name}")

        # Extract structured handoff from agent output
        try:
            from services.token_optimizer import extract_handoff
            handoff = extract_handoff(
                output,
                agent.persona.role if agent.persona else agent.stage_name,
                agent.worktree_path,
            )
            agent.handoff_json = json.dumps(handoff)
        except Exception as e:
            print(f"  ⚠️ Handoff extraction failed: {e}")

        db.session.commit()
        print(f"  ✓ Agent {agent.name} status={agent.status}, cost=${agent.cost:.2f}")

        self._log(
            agent.run_id,
            f"Finished ({agent.duration_minutes()} min, ${agent.cost:.2f})" +
            (f", {agent.issues_found} issues found" if agent.issues_found else ""),
            "success" if success else "error",
            agent.name,
        )

        run = agent.run
        run.total_cost = sum(a.cost for a in run.agents)
        run.total_tokens_in = sum(a.tokens_in for a in run.agents)
        run.total_tokens_out = sum(a.tokens_out for a in run.agents)
        db.session.commit()

        self._check_stage_completion(run)

    def _check_stage_completion(self, run: Run):
        """Check if all agents in the current stage are done."""
        workflow = run.workflow
        stages = workflow.get_stages()
        if run.current_stage_index >= len(stages):
            return

        stage = stages[run.current_stage_index]
        stage_name = stage.get("name", f"stage-{run.current_stage_index}")

        stage_agents = [a for a in run.agents if a.stage_name == stage_name]

        # Check if all agents in this stage are complete
        all_done = all(a.status in ("done", "failed", "converged") for a in stage_agents)
        if not all_done:
            return  # Still waiting for agents

        # ── Review stage with parallel reviewers ──
        if stage.get("converge"):
            total_issues = sum(a.issues_found for a in stage_agents)
            run.add_review_round(total_issues)
            db.session.commit()

            self._log(
                run.id,
                f"Review complete: {total_issues} issues across {len(stage_agents)} parallel reviewers",
                "info",
            )

            # ── Synthesis: merge parallel reviewer branches ──
            successful_agents = [a for a in stage_agents if a.status == "done" and a.worktree_path]
            if len(successful_agents) > 1:
                synth_ok = self._synthesize_reviews(run, successful_agents)
                if not synth_ok:
                    run.status = "needs_approval"
                    db.session.commit()
                    self._log(
                        run.id,
                        "⚠️ Merge conflicts between parallel reviewers. Approve to attempt auto-resolution, or fix manually.",
                        "warning",
                    )
                    return
            elif len(successful_agents) == 1:
                self._log(run.id, "Single reviewer completed — using as synthesis result", "info")
                wt_name = successful_agents[0].worktree_path.split("/")[-1]
                synth_name = f"run-{run.id[:8]}-synthesis"
                synth_path = os.path.join(self.git.worktree_base, synth_name)
                if not os.path.exists(synth_path):
                    branch = f"agentops/{wt_name}"
                    self.git.create_worktree(synth_name, branch)

            # ── Done — mark converged and advance ──
            for a in stage_agents:
                a.status = "converged"
            db.session.commit()
            self._log(run.id, f"✅ Review complete! {total_issues} issues found and fixed by {len(successful_agents)} reviewers.", "success")

            run.current_stage_index += 1
            db.session.commit()

            if stage.get("checkpoint_after", True):
                if run.auto_approve:
                    self._log(run.id, "Auto-approved after review", "success")
                    self._advance_stage(run)
                else:
                    run.status = "needs_approval"
                    db.session.commit()
                    self._log(run.id, "⏸ Reviews complete — approve to open PR?", "warning")
            else:
                self._advance_stage(run)
            return

        # ── Test stage: check for TESTS FAILED marker ──
        if stage_name == "test":
            for a in stage_agents:
                if a.status == "done" and a.output_log and "TESTS FAILED" in a.output_log:
                    run.status = "failed"
                    run.finished_at = datetime.now(timezone.utc)
                    db.session.commit()
                    self._log(run.id, f"❌ Tests failed — run stopped at test stage.", "error")
                    telemetry.track_run_failed(run, reason="tests_failed")
                    return

        # ── Non-review stage complete ──
        # If ALL agents in a non-review stage failed, stop the run
        successful = [a for a in stage_agents if a.status == "done"]
        if not successful:
            run.status = "failed"
            run.finished_at = datetime.now(timezone.utc)
            db.session.commit()
            self._log(run.id, f"❌ Stage '{stage_name}' failed — no successful agents. Run stopped.", "error")
            telemetry.track_run_failed(run, reason=f"stage_{stage_name}_all_failed")
            return

        if stage.get("checkpoint_after"):
            if run.auto_approve:
                self._log(run.id, f"Auto-approved after stage '{stage_name}'", "success")
                run.current_stage_index += 1
                db.session.commit()
                self._advance_stage(run)
            else:
                run.status = "needs_approval"
                db.session.commit()
                self._log(run.id, f"⏸ Stage '{stage_name}' complete — approve to continue?", "warning")
        else:
            run.current_stage_index += 1
            db.session.commit()
            self._advance_stage(run)

    def _synthesize_reviews(self, run: Run, successful_agents: list) -> bool:
        """
        Merge all parallel reviewer branches into a single synthesis worktree.
        Returns True if synthesis succeeded, False if conflicts need resolution.
        """
        self._log(run.id, f"🔀 Synthesizing {len(successful_agents)} parallel reviewer branches...")

        # Commit all reviewer work
        for agent in successful_agents:
            if agent.worktree_path:
                wt_name = agent.worktree_path.split("/")[-1]
                self.git.commit_worktree(wt_name, f"AgentOps: {agent.name} — review")

        # Find the base branch (engineer's branch or previous synthesis)
        base = run.base_branch or "main"
        for prev_agent in run.agents:
            if prev_agent.stage_name not in ("review",) and prev_agent.status == "done" and prev_agent.worktree_path:
                prev_wt_name = prev_agent.worktree_path.split("/")[-1]
                base = f"agentops/{prev_wt_name}"
                break

        # Check if a previous synthesis exists (from an earlier round)
        synth_name = f"run-{run.id[:8]}-synthesis"
        synth_path = os.path.join(self.git.worktree_base, synth_name)
        if os.path.exists(synth_path):
            # Remove old synthesis to create fresh one
            self.git.remove_worktree(synth_name)

        # Create synthesis worktree from the base branch
        ok, result = self.git.create_synthesis_worktree(run.id[:8], base)
        if not ok:
            self._log(run.id, f"Failed to create synthesis worktree: {result}", "error")
            return False

        # Merge each reviewer's branch into synthesis
        reviewer_branches = []
        for agent in successful_agents:
            wt_name = agent.worktree_path.split("/")[-1]
            reviewer_branches.append(f"agentops/{wt_name}")

        all_ok, merged, failed = self.git.merge_branches_into_worktree(synth_name, reviewer_branches, auto_resolve=True)

        if all_ok:
            self._log(
                run.id,
                f"✅ Synthesis complete — merged {len(merged)} reviewer branches cleanly",
                "success",
            )
            # Commit the synthesis
            self.git.commit_worktree(synth_name, f"AgentOps: synthesis of {len(merged)} parallel reviews")
            return True
        else:
            self._log(
                run.id,
                f"⚠️ Synthesis partial — {len(merged)} merged, {len(failed)} had conflicts: {'; '.join(failed)}",
                "warning",
            )
            # Commit what we could merge
            if merged:
                self.git.commit_worktree(synth_name, f"AgentOps: partial synthesis ({len(merged)}/{len(merged)+len(failed)} branches)")
            return False

    def approve_checkpoint(self, run_id: str) -> bool:
        """Human approves a checkpoint — advance to the next stage."""
        run = Run.query.get(run_id)
        if not run or run.status != "needs_approval":
            return False

        run.status = "running"
        run.current_stage_index += 1
        db.session.commit()

        self._log(run.id, "✓ Approved. Continuing...", "success")
        return self._advance_stage(run)

    def merge_run(self, run_id: str, skip_cleanup: bool = False) -> tuple[bool, str]:
        """Push branch and create a pull request for a run.
        skip_cleanup: if True, don't remove worktrees (ensemble child runs need them for consensus).
        """
        run = Run.query.get(run_id)
        if not run:
            return False, "Run not found"

        self._log(run.id, "Preparing pull request...")

        # Find the best branch to push — prefer synthesis worktree, then review, engineer, plan
        synth_name = f"run-{run.id[:8]}-synthesis"
        synth_path = os.path.join(self.git.worktree_base, synth_name)

        if os.path.exists(synth_path):
            # Use synthesis worktree (contains merged parallel reviews)
            wt_name = synth_name
            branch_name = f"agentops/{synth_name}"
            self.git.commit_worktree(synth_name, f"AgentOps: final changes — {run.title}")
        else:
            # Fallback to individual agent worktrees
            seen_paths = set()
            agents_by_stage = {}
            for agent in run.agents:
                if agent.worktree_path and agent.status in ("done", "converged"):
                    if agent.worktree_path not in seen_paths:
                        seen_paths.add(agent.worktree_path)
                        agents_by_stage[agent.stage_name] = agent

            final_agent = agents_by_stage.get("review") or agents_by_stage.get("engineer") or agents_by_stage.get("plan")
            if not final_agent or not final_agent.worktree_path:
                return False, "No branches to push"

            wt_name = final_agent.worktree_path.split("/")[-1]
            branch_name = f"agentops/{wt_name}"
            self.git.commit_worktree(wt_name, f"AgentOps: final changes — {run.title}")

        # Push the branch
        self._log(run.id, f"Pushing {branch_name}...")
        success, msg = self.git.push_branch(branch_name)
        if not success:
            self._log(run.id, f"Push failed: {msg}", "error")
            return False, f"Push failed: {msg}"
        self._log(run.id, "Branch pushed", "success")

        # Build PR body
        pr_body = self._build_pr_body(run)

        # Create PR via GitHub CLI
        self._log(run.id, "Creating pull request...")
        success, pr_url = self.git.create_pr(
            branch=branch_name,
            title=f"[AgentOps] {run.title}",
            body=pr_body,
            base=run.base_branch or "main",
        )

        if not success:
            self._log(run.id, f"PR creation failed: {pr_url}. Branch is pushed — create PR manually.", "warning")
            run.status = "merged"
            run.finished_at = datetime.now(timezone.utc)
            db.session.commit()
            return True, f"Branch pushed but PR failed: {pr_url}"

        self._log(run.id, f"✅ PR created: {pr_url}", "success")
        run.pr_url = pr_url
        db.session.commit()

        # Cleanup all worktrees (unless skip_cleanup for ensemble child runs)
        if not skip_cleanup:
            for agent in run.agents:
                if agent.worktree_path:
                    wt_n = agent.worktree_path.split("/")[-1]
                    self.git.remove_worktree(wt_n)
            # Also clean up synthesis worktree
            synth_cleanup = f"run-{run.id[:8]}-synthesis"
            self.git.remove_worktree(synth_cleanup)

        run.status = "merged"
        run.finished_at = datetime.now(timezone.utc)
        db.session.commit()

        self._log(run.id, f"✅ Done. PR: {pr_url} · Cost: ${run.total_cost:.2f} · Duration: {run.duration_minutes()} min", "success")
        return True, pr_url

    def _build_pr_body(self, run: Run) -> str:
        """Build a markdown PR description from run data."""
        review_history = run.get_review_history()
        review_agents = [a for a in run.agents if a.stage_name == "review"]

        lines = [
            "## 🤖 AgentOps Run",
            "",
            f"**Task:** {run.task_description}",
            "",
        ]

        if len(review_agents) > 1:
            lines.extend([
                f"**Review mode:** 🔀 Parallel ({len(review_agents)} reviewers per round → synthesis merge)",
                "",
            ])

        lines.extend([
            "### Agents",
            "| Agent | Model | Duration | Cost | Status |",
            "|-------|-------|----------|------|--------|",
        ])

        for agent in run.agents:
            lines.append(
                f"| {agent.name} | `{agent.model}` | {agent.duration_minutes()}m | ${agent.cost:.2f} | {agent.status} |"
            )

        if review_history:
            lines.append("")
            lines.append("### Review Convergence")
            rounds = " → ".join([f"Round {r['round']}: {r['issues']} issues" for r in review_history])
            lines.append(rounds)

        lines.extend([
            "",
            "### Summary",
            f"- **Total cost:** ${run.total_cost:.2f}",
            f"- **Duration:** {run.duration_minutes()} min",
            f"- **Review rounds:** {run.review_round}",
            f"- **Mode:** {'🟢 Autopilot' if run.auto_approve else '🛑 Manual'}",
        ])

        for agent in run.agents:
            if agent.output_log and agent.status in ("done", "converged"):
                summary = agent.output_log[:500].strip()
                if summary:
                    lines.extend([
                        "",
                        f"<details><summary>{agent.name} output</summary>",
                        "",
                        "```",
                        summary,
                        "```",
                        "</details>",
                    ])

        lines.extend(["", "---", "*Generated by AgentOps*"])
        return "\n".join(lines)

    def cancel_run(self, run_id: str) -> bool:
        """Cancel a run and clean up."""
        run = Run.query.get(run_id)
        if not run:
            return False

        # Stop any running agents
        for agent in run.agents:
            if agent.status == "running":
                self.runner.stop_agent(agent.id)
                agent.status = "failed"

            # Cleanup worktrees
            if agent.worktree_path:
                wt_name = agent.worktree_path.split("/")[-1]
                self.git.remove_worktree(wt_name)

        run.status = "failed"
        run.finished_at = datetime.now(timezone.utc)
        db.session.commit()

        self._log(run.id, "Run cancelled", "warning")
        return True
