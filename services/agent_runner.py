"""
Agent Runner — Launches Claude Code (or Claude API) agents as subprocesses.
Manages agent lifecycle: start, monitor, capture output, track costs.
"""
import os
import subprocess
import threading
import time
import json
from datetime import datetime, timezone
from typing import Optional, Callable

# Cost per 1M tokens (approximate)
MODEL_COSTS = {
    "haiku": {"input": 0.80, "output": 4.00},
    "sonnet": {"input": 3.00, "output": 15.00},
    "opus": {"input": 15.00, "output": 75.00},
}


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Estimate cost in dollars for a given model and token count."""
    rates = MODEL_COSTS.get(model, MODEL_COSTS["sonnet"])
    cost = (tokens_in / 1_000_000) * rates["input"] + (tokens_out / 1_000_000) * rates["output"]
    return round(cost, 4)


class AgentRunner:
    """Manages running Claude Code agents in worktrees."""

    def __init__(self, log_callback: Optional[Callable] = None):
        self.active_processes: dict[str, subprocess.Popen] = {}
        self.log_callback = log_callback  # fn(agent_id, level, message)

    def _log(self, agent_id: str, level: str, message: str):
        if self.log_callback:
            self.log_callback(agent_id, level, message)

    def build_prompt(self, persona_template: str, task: str, extra_context: str = "") -> str:
        """Assemble the full prompt from persona + task + context."""
        parts = [persona_template.strip()]
        if extra_context:
            parts.append(f"\n## Context\n{extra_context.strip()}")
        parts.append(f"\n## Your Task\n{task.strip()}")
        parts.append("\n## Rules\n- Run pytest after every change.\n- When done, output '✅ TASK COMPLETE' and a summary of what you did.\n- If you're stuck, output '❌ BLOCKED: [reason]'.")
        return "\n\n".join(parts)

    def launch_agent(
        self,
        agent_id: str,
        worktree_path: str,
        prompt: str,
        model: str = "sonnet-4.5",
        on_complete: Optional[Callable] = None,
        app=None,
    ) -> bool:
        """
        Launch a Claude Code agent in a worktree.
        Runs in a background thread. Calls on_complete(agent_id, success, output) when done.
        """
        if agent_id in self.active_processes:
            return False  # Already running

        def _run_agent():
            self._log(agent_id, "info", f"Starting agent in {worktree_path}")
            print(f"\n{'='*60}")
            print(f"🚀 AGENT LAUNCHED: {agent_id[:8]}")
            print(f"   📂 Worktree: {worktree_path}")
            print(f"   🤖 Model: {model}")
            print(f"   📝 Prompt length: {len(prompt)} chars")
            print(f"{'='*60}")

            try:
                cmd = ["claude", "-p", prompt, "--output-format", "text", "--permission-mode", "acceptEdits"]
                if model:
                    cmd.extend(["--model", model])

                print(f"   ▶ Running: claude -p '...' --output-format text --permission-mode acceptEdits --model {model}")

                process = subprocess.Popen(
                    cmd,
                    cwd=worktree_path,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                self.active_processes[agent_id] = process
                print(f"   ✓ Process started (PID: {process.pid})")

                stdout, stderr = process.communicate(timeout=1800)
                output = stdout + stderr
                success = process.returncode == 0

                print(f"\n{'='*60}")
                print(f"🏁 AGENT FINISHED: {agent_id[:8]}")
                print(f"   Return code: {process.returncode}")
                print(f"   Output length: {len(output)} chars")
                print(f"   Success: {success}")
                if output:
                    preview = output[:500].replace('\n', '\n   ')
                    print(f"   Output preview:\n   {preview}")
                    if len(output) > 500:
                        print(f"   ... ({len(output) - 500} more chars)")
                if stderr and stderr != stdout:
                    print(f"   Stderr: {stderr[:200]}")
                print(f"{'='*60}\n")

                if on_complete:
                    print(f"   📤 Calling on_complete callback...")
                    if app:
                        with app.app_context():
                            on_complete(agent_id, success, output)
                            print(f"   ✓ on_complete finished (with app context)")
                    else:
                        on_complete(agent_id, success, output)
                        print(f"   ✓ on_complete finished (no app context)")

            except subprocess.TimeoutExpired:
                process.kill()
                print(f"   ❌ TIMEOUT after 30 min: {agent_id[:8]}")
                if on_complete:
                    if app:
                        with app.app_context():
                            on_complete(agent_id, False, "Timeout after 30 minutes")
                    else:
                        on_complete(agent_id, False, "Timeout after 30 minutes")

            except FileNotFoundError:
                print(f"   ❌ Claude CLI not found! Is 'claude' in PATH?")
                if on_complete:
                    if app:
                        with app.app_context():
                            on_complete(agent_id, False, "Claude CLI not found")
                    else:
                        on_complete(agent_id, False, "Claude CLI not found")

            except Exception as e:
                print(f"   ❌ EXCEPTION: {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
                if on_complete:
                    if app:
                        with app.app_context():
                            on_complete(agent_id, False, str(e))
                    else:
                        on_complete(agent_id, False, str(e))

            finally:
                self.active_processes.pop(agent_id, None)
                print(f"   🧹 Agent {agent_id[:8]} removed from active list. Active: {len(self.active_processes)}")

        thread = threading.Thread(target=_run_agent, daemon=True)
        thread.start()
        return True

    def stop_agent(self, agent_id: str) -> bool:
        """Stop a running agent."""
        process = self.active_processes.get(agent_id)
        if process:
            process.terminate()
            self.active_processes.pop(agent_id, None)
            self._log(agent_id, "warning", "Agent stopped by user")
            return True
        return False

    def is_running(self, agent_id: str) -> bool:
        return agent_id in self.active_processes

    def count_active(self) -> int:
        return len(self.active_processes)

    @staticmethod
    def parse_output_issues(output: str) -> int:
        """
        Try to count issues found from agent output.
        Looks for patterns like 'Found 3 issues' or 'X issues'.
        """
        import re
        patterns = [
            r"[Ff]ound (\d+) issues?",
            r"(\d+) issues? found",
            r"[Ff]ixed (\d+) issues?",
        ]
        total = 0
        for pattern in patterns:
            matches = re.findall(pattern, output)
            for m in matches:
                total += int(m)
        return total

    @staticmethod
    def estimate_tokens(output: str) -> tuple[int, int]:
        """
        Rough token estimation from output length.
        In production, use the Claude API for exact counts.
        """
        # Very rough: ~4 chars per token for English
        out_tokens = max(len(output) // 4, 100)
        # Assume input was ~3x output for a typical agent run
        in_tokens = out_tokens * 3
        return in_tokens, out_tokens
