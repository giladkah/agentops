"""
Agent Runner — Dual-mode: Anthropic API (streaming + tool use) or Claude Code CLI (subprocess).
Mode is switchable at runtime via settings. Both backends share the same launch_agent() interface.
"""
import os
import re
import subprocess
import threading
import time
import json
import shutil
from datetime import datetime, timezone
from typing import Optional, Callable

# API import is optional — only needed for API mode
try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

# ── Model mapping ──

MODEL_MAP = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-5-20250929",
    "opus": "claude-opus-4-6",
    "claude-haiku-4-5-20251001": "claude-haiku-4-5-20251001",
    "claude-sonnet-4-5-20250929": "claude-sonnet-4-5-20250929",
    "claude-opus-4-6": "claude-opus-4-6",
}

MODEL_COSTS = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-5-20250929": {"input": 3.00, "output": 15.00},
    "claude-opus-4-6": {"input": 15.00, "output": 75.00},
    "haiku": {"input": 0.80, "output": 4.00},
    "sonnet": {"input": 3.00, "output": 15.00},
    "opus": {"input": 15.00, "output": 75.00},
}

MAX_TOKENS_PER_TURN = 16384
MAX_AGENTIC_TURNS = 40           # builder / engineer / planner
MAX_REVIEWER_TURNS = 20          # reviewers: focused on diff, not full exploration
MAX_CONSENSUS_TURNS = 30         # consensus: needs to synthesize but not rebuild
COMMAND_TIMEOUT = 120
CLI_TIMEOUT = 2700  # 45 min for CLI subprocess


def resolve_model(short_name: str) -> str:
    return MODEL_MAP.get(short_name, short_name)


def compute_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    rates = MODEL_COSTS.get(model, MODEL_COSTS.get("sonnet"))
    cost = (tokens_in / 1_000_000) * rates["input"] + (tokens_out / 1_000_000) * rates["output"]
    return round(cost, 6)


# backward compat alias
def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    return compute_cost(model, tokens_in, tokens_out)


# ── API Tool Definitions ──

AGENT_TOOLS = [
    {
        "name": "read_file",
        "description": "Read the contents of a file. Path is relative to the project root.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to project root"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file. Creates the file and parent directories if they don't exist.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to project root"},
                "content": {"type": "string", "description": "Complete file content to write"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and directories. Returns names with '/' suffix for directories.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path relative to project root. Use '.' for root."},
                "recursive": {"type": "boolean", "description": "List recursively (max 3 levels)", "default": False},
            },
            "required": ["path"],
        },
    },
    {
        "name": "run_command",
        "description": "Run a shell command in the project directory. Use for running tests, git status, linting, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 120)", "default": 120},
            },
            "required": ["command"],
        },
    },
    {
        "name": "search_files",
        "description": "Search for a text pattern across files. Returns matching lines with file:line format.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Text or regex pattern to search for"},
                "path": {"type": "string", "description": "Directory to search in", "default": "."},
                "file_pattern": {"type": "string", "description": "File glob (e.g. '*.py')", "default": ""},
                "max_results": {"type": "integer", "description": "Max results (default 50)", "default": 50},
            },
            "required": ["pattern"],
        },
    },
]


# ── Tool Execution (API mode) ──

def _safe_path(worktree: str, path: str) -> Optional[str]:
    resolved = os.path.normpath(os.path.join(worktree, path))
    if not resolved.startswith(os.path.normpath(worktree)):
        return None
    return resolved


def execute_tool(tool_name: str, tool_input: dict, worktree: str) -> str:
    try:
        if tool_name == "read_file":
            return _tool_read_file(worktree, tool_input)
        elif tool_name == "write_file":
            return _tool_write_file(worktree, tool_input)
        elif tool_name == "list_directory":
            return _tool_list_directory(worktree, tool_input)
        elif tool_name == "run_command":
            return _tool_run_command(worktree, tool_input)
        elif tool_name == "search_files":
            return _tool_search_files(worktree, tool_input)
        else:
            return f"Error: Unknown tool '{tool_name}'"
    except Exception as e:
        return f"Error executing {tool_name}: {type(e).__name__}: {e}"


def _tool_read_file(wt, inp):
    path = _safe_path(wt, inp["path"])
    if not path: return "Error: Path escapes project directory"
    if not os.path.exists(path): return f"Error: File not found: {inp['path']}"
    if not os.path.isfile(path): return f"Error: Not a file: {inp['path']}"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if len(content) > 100_000:
            return f"[File truncated — {len(content)} chars, showing first 100K]\n{content[:100_000]}"
        return content
    except Exception as e:
        return f"Error reading: {e}"


def _tool_write_file(wt, inp):
    path = _safe_path(wt, inp["path"])
    if not path: return "Error: Path escapes project directory"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(inp["content"])
        return f"Successfully wrote {len(inp['content'])} chars to {inp['path']}"
    except Exception as e:
        return f"Error writing: {e}"


def _tool_list_directory(wt, inp):
    path = _safe_path(wt, inp.get("path", "."))
    if not path: return "Error: Path escapes project directory"
    if not os.path.isdir(path): return f"Error: Not a directory: {inp.get('path', '.')}"
    recursive = inp.get("recursive", False)
    results = []
    if recursive:
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__", "venv", ".venv")]
            if root.replace(path, "").count(os.sep) >= 3:
                dirs.clear(); continue
            rel = os.path.relpath(root, path)
            for f in sorted(files):
                results.append(f if rel == "." else os.path.join(rel, f))
    else:
        for item in sorted(os.listdir(path)):
            full = os.path.join(path, item)
            results.append(f"{item}/" if os.path.isdir(full) else item)
    return "\n".join(results[:500]) if results else "(empty directory)"


def _tool_run_command(wt, inp):
    command = inp["command"]
    timeout = min(inp.get("timeout", COMMAND_TIMEOUT), 300)
    try:
        result = subprocess.run(command, shell=True, cwd=wt, capture_output=True, text=True, timeout=timeout)
        output = ""
        if result.stdout: output += result.stdout
        if result.stderr: output += ("\n--- stderr ---\n" if output else "") + result.stderr
        output += f"\n[exit code: {result.returncode}]"
        if len(output) > 50_000:
            output = output[:25_000] + "\n\n... [truncated] ...\n\n" + output[-25_000:]
        return output
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout}s"
    except Exception as e:
        return f"Error running command: {e}"


def _tool_search_files(wt, inp):
    pattern = inp["pattern"]
    search_path = _safe_path(wt, inp.get("path", "."))
    if not search_path: return "Error: Path escapes project directory"
    file_pat = inp.get("file_pattern", "")
    max_results = min(inp.get("max_results", 50), 200)
    cmd = ["grep", "-rn"]
    if file_pat: cmd.extend(["--include", file_pat])
    cmd.extend([pattern, search_path])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=wt)
        lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
        cleaned = [l.replace(wt + "/", "").replace(wt, "") for l in lines[:max_results]]
        return "\n".join(cleaned) if cleaned else f"No matches found for '{pattern}'"
    except Exception as e:
        return f"Error searching: {e}"


# ── Stream Buffer ──

class StreamBuffer:
    """Thread-safe buffer for streaming agent events to the UI."""

    def __init__(self):
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self.finished = False

    def add(self, event_type: str, data: dict):
        with self.lock:
            self.events.append({
                "type": event_type,
                "data": data,
                "ts": time.time(),
                "idx": len(self.events),
            })

    def get_since(self, after_index: int = 0) -> list[dict]:
        with self.lock:
            return [e for e in self.events if e["idx"] > after_index]

    def mark_done(self):
        self.finished = True
        self.add("done", {})


# ── Agent Runner (Dual Mode) ──

class AgentRunner:
    """
    Manages running Claude agents. Supports two backends:
      - "api": Anthropic Messages API with tool use + streaming (exact costs)
      - "cli": Claude Code CLI subprocess (battle-tested, no API key needed)
    """

    def __init__(self, api_key: str = None, mode: str = "api", log_callback: Optional[Callable] = None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._mode = mode
        self.log_callback = log_callback
        self.stream_buffers: dict[str, StreamBuffer] = {}
        self._client = None

        # Unified tracking — maps agent_id → cancel mechanism
        self._active_cancel: dict[str, threading.Event] = {}  # API mode: cancel events
        self._active_procs: dict[str, subprocess.Popen] = {}  # CLI mode: subprocess handles

    @property
    def mode(self) -> str:
        return self._mode

    @mode.setter
    def mode(self, value: str):
        if value not in ("api", "cli"):
            raise ValueError(f"Invalid mode: {value}. Use 'api' or 'cli'.")
        self._mode = value
        print(f"🔌 Agent runner mode changed to: {value.upper()}")

    @property
    def client(self):
        if not HAS_ANTHROPIC:
            raise ImportError("anthropic package not installed. Run: pip install anthropic")
        if self._client is None:
            if not self.api_key:
                raise ValueError("No API key. Set ANTHROPIC_API_KEY or pass --api-key.")
            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    @property
    def active_processes(self):
        """Backward compat — dict-like access for debug/clear endpoints."""
        class _FakeProc:
            def __init__(self): self.pid = 0; self.returncode = None
            def poll(self): return None
        result = {}
        for aid in self._active_cancel:
            result[aid] = _FakeProc()
        for aid, proc in self._active_procs.items():
            result[aid] = proc
        return result

    @property
    def active_agents(self):
        """All active agent IDs across both modes."""
        return {**{k: v for k, v in self._active_cancel.items()}, **{k: v for k, v in self._active_procs.items()}}

    def _log(self, agent_id: str, level: str, message: str):
        if self.log_callback:
            self.log_callback(agent_id, level, message)

    def get_stream_buffer(self, agent_id: str) -> Optional[StreamBuffer]:
        return self.stream_buffers.get(agent_id)

    def has_api_key(self) -> bool:
        return bool(self.api_key)

    def has_cli(self) -> bool:
        return shutil.which("claude") is not None

    def get_status(self) -> dict:
        """Return status info for the settings page."""
        return {
            "mode": self._mode,
            "api_key_set": self.has_api_key(),
            "cli_available": self.has_cli(),
            "active_count": self.count_active(),
            "anthropic_installed": HAS_ANTHROPIC,
        }

    def build_prompt(self, persona_template: str, task: str, extra_context: str = "") -> str:
        """Assemble the full prompt — adjusts rules based on mode."""
        parts = [persona_template.strip()]
        if extra_context:
            parts.append(f"\n## Context\n{extra_context.strip()}")
        parts.append(f"\n## Your Task\n{task.strip()}")

        if self._mode == "api":
            parts.append(
                "\n## Rules\n"
                "- Use the provided tools to read, write, and search files.\n"
                "- Run pytest after every code change.\n"
                "- When done, output '✅ TASK COMPLETE' and a summary of what you did.\n"
                "- If you're stuck, output '❌ BLOCKED: [reason]'.\n"
                "- Be focused and efficient — don't read files you don't need.\n"
                "- Read only files directly relevant to the task. Never read the whole codebase.\n"
                "- Stop as soon as the task is done — don't over-explore."
            )
        else:  # cli mode
            parts.append(
                "\n## Rules\n"
                "- Run pytest after every change.\n"
                "- When done, output '✅ TASK COMPLETE' and a summary of what you did.\n"
                "- If you're stuck, output '❌ BLOCKED: [reason]'.\n"
                "- Read ONLY files directly relevant to the task. Do not explore the whole codebase.\n"
                "- Stop as soon as the task is complete — do not keep reading or refactoring."
            )
        return "\n\n".join(parts)

    # ── Launch (delegates to the right backend) ──

    def launch_agent(
        self,
        agent_id: str,
        worktree_path: str,
        prompt: str,
        model: str = "sonnet",
        on_complete: Optional[Callable] = None,
        app=None,
        max_turns: int = None,
    ) -> bool:
        """Launch a Claude agent — dispatches to API or CLI backend based on current mode."""
        if agent_id in self._active_cancel or agent_id in self._active_procs:
            return False

        if max_turns is None:
            max_turns = MAX_AGENTIC_TURNS

        buf = StreamBuffer()
        self.stream_buffers[agent_id] = buf

        if self._mode == "api":
            return self._launch_api(agent_id, worktree_path, prompt, model, on_complete, app, buf, max_turns)
        else:
            return self._launch_cli(agent_id, worktree_path, prompt, model, on_complete, app, buf, max_turns)

    # ── API Backend (streaming + tool use) ──

    def _launch_api(self, agent_id, worktree_path, prompt, model, on_complete, app, buf, max_turns=MAX_AGENTIC_TURNS):
        cancel_event = threading.Event()
        self._active_cancel[agent_id] = cancel_event

        def _run():
            api_model = resolve_model(model)
            total_in = 0
            total_out = 0
            all_text = []
            tool_calls = 0
            turns = 0
            success = False

            self._log(agent_id, "info", f"Starting agent (API mode, model: {api_model})")
            print(f"\n{'='*60}")
            print(f"🚀 AGENT LAUNCHED: {agent_id[:8]}")
            print(f"   📂 Worktree: {worktree_path}")
            print(f"   🤖 Model: {api_model}")
            print(f"   🔌 Mode: Anthropic API + Tool Use + Streaming")
            print(f"{'='*60}")

            buf.add("start", {"model": api_model, "agent_id": agent_id[:8], "mode": "api"})

            try:
                messages = [{"role": "user", "content": prompt}]

                while turns < max_turns:
                    if cancel_event.is_set():
                        buf.add("cancelled", {})
                        all_text.append("\n❌ CANCELLED by user")
                        break

                    turns += 1
                    buf.add("turn", {"turn": turns})

                    try:
                        response_text = ""

                        with self.client.messages.stream(
                            model=api_model,
                            max_tokens=MAX_TOKENS_PER_TURN,
                            messages=messages,
                            tools=AGENT_TOOLS,
                        ) as stream:
                            for event in stream:
                                if cancel_event.is_set():
                                    break
                                if hasattr(event, 'type'):
                                    if event.type == "content_block_start":
                                        cb = getattr(event, 'content_block', None)
                                        if cb and cb.type == "tool_use":
                                            buf.add("tool_start", {"tool": cb.name, "id": cb.id})
                                    elif event.type == "content_block_delta":
                                        delta = getattr(event, 'delta', None)
                                        if delta and delta.type == "text_delta":
                                            response_text += delta.text
                                            buf.add("text", {"text": delta.text})

                            final = stream.get_final_message()

                        total_in += final.usage.input_tokens
                        total_out += final.usage.output_tokens
                        cost_so_far = compute_cost(api_model, total_in, total_out)

                        buf.add("usage", {
                            "input_tokens": final.usage.input_tokens,
                            "output_tokens": final.usage.output_tokens,
                            "total_in": total_in, "total_out": total_out,
                            "cost": cost_so_far,
                        })

                        if response_text:
                            all_text.append(response_text)

                        if final.stop_reason == "end_turn":
                            success = True
                            buf.add("complete", {"reason": "end_turn"})
                            print(f"   ✅ Done: {turns} turns, {tool_calls} tool calls, ${cost_so_far:.4f}")
                            break

                        tool_results = []
                        for block in final.content:
                            if block.type == "tool_use":
                                tool_calls += 1
                                t_name, t_input = block.name, block.input
                                inp_preview = json.dumps(t_input)[:150]
                                print(f"   🔧 [{turns}] {t_name}({inp_preview})")
                                buf.add("tool_call", {"tool": t_name, "input": t_input, "n": tool_calls})

                                result = execute_tool(t_name, t_input, worktree_path)
                                buf.add("tool_result", {
                                    "tool": t_name, "preview": result[:300],
                                    "length": len(result), "ok": not result.startswith("Error"),
                                })
                                tool_results.append({
                                    "type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": result,
                                })

                        if not tool_results:
                            success = True
                            buf.add("complete", {"reason": "no_tools"})
                            break

                        messages.append({"role": "assistant", "content": final.content})
                        messages.append({"role": "user", "content": tool_results})

                    except anthropic.APIError as e:
                        err = f"API Error: {e.message}"
                        print(f"   ❌ {err}")
                        buf.add("error", {"message": err})
                        all_text.append(f"\n❌ {err}")
                        break
                else:
                    buf.add("error", {"message": f"Hit max turns ({max_turns})"})
                    all_text.append(f"\n⚠️ Max turns reached")

                output = "\n".join(all_text)
                cost = compute_cost(api_model, total_in, total_out)

                print(f"\n{'='*60}")
                print(f"🏁 AGENT FINISHED: {agent_id[:8]}")
                print(f"   Success: {success} | Turns: {turns} | Tools: {tool_calls}")
                print(f"   Tokens: {total_in:,} in / {total_out:,} out | Cost: ${cost:.4f}")
                print(f"{'='*60}\n")

                buf.add("finished", {
                    "success": success, "turns": turns, "tool_calls": tool_calls,
                    "tokens_in": total_in, "tokens_out": total_out, "cost": cost,
                })
                buf.mark_done()

                if on_complete:
                    if app:
                        with app.app_context(): on_complete(agent_id, success, output)
                    else:
                        on_complete(agent_id, success, output)

            except anthropic.AuthenticationError:
                err = "Invalid API key. Check ANTHROPIC_API_KEY."
                print(f"   ❌ AUTH: {err}")
                buf.add("error", {"message": err}); buf.mark_done()
                if on_complete:
                    if app:
                        with app.app_context(): on_complete(agent_id, False, err)
                    else:
                        on_complete(agent_id, False, err)

            except Exception as e:
                import traceback
                err = f"{type(e).__name__}: {e}"
                print(f"   ❌ EXCEPTION: {err}"); traceback.print_exc()
                buf.add("error", {"message": err}); buf.mark_done()
                if on_complete:
                    if app:
                        with app.app_context(): on_complete(agent_id, False, err)
                    else:
                        on_complete(agent_id, False, err)

            finally:
                self._active_cancel.pop(agent_id, None)
                print(f"   🧹 Agent {agent_id[:8]} done. Active: {self.count_active()}")

        threading.Thread(target=_run, daemon=True).start()
        return True

    # ── CLI Backend (Claude Code subprocess) ──

    def _launch_cli(self, agent_id, worktree_path, prompt, model, on_complete, app, buf, max_turns=MAX_AGENTIC_TURNS):

        def _monitor_worktree(process, buf, worktree_path, agent_id):
            """Background thread: monitors worktree for file changes every 15s + enforces timeout."""
            known_files = set()
            heartbeat = 0
            start_time = time.time()
            try:
                while process.poll() is None:
                    time.sleep(15)
                    heartbeat += 1
                    if process.poll() is not None:
                        break

                    # Enforce timeout
                    elapsed = time.time() - start_time
                    if elapsed > CLI_TIMEOUT:
                        print(f"   ⏰ CLI timeout ({CLI_TIMEOUT // 60}m) — killing process")
                        try:
                            pgid = os.getpgid(process.pid)
                            import signal
                            os.killpg(pgid, signal.SIGKILL)
                        except Exception:
                            process.kill()
                        buf.add("error", {"message": f"Timeout after {CLI_TIMEOUT // 60} minutes"})
                        return

                    # Check for file changes via git
                    try:
                        result = subprocess.run(
                            ["git", "diff", "--name-only", "HEAD"],
                            cwd=worktree_path, capture_output=True, text=True, timeout=5,
                        )
                        changed = set(result.stdout.strip().split("\n")) if result.stdout.strip() else set()

                        # Also check untracked files
                        result2 = subprocess.run(
                            ["git", "ls-files", "--others", "--exclude-standard"],
                            cwd=worktree_path, capture_output=True, text=True, timeout=5,
                        )
                        untracked = set(result2.stdout.strip().split("\n")) if result2.stdout.strip() else set()
                        changed = changed | untracked

                        new_files = changed - known_files
                        if new_files:
                            for f in sorted(new_files):
                                if f:
                                    buf.add("tool_call", {"tool": "write_file", "input": {"path": f}, "n": len(known_files) + 1})
                                    buf.add("tool_result", {"tool": "write_file", "preview": f"Modified: {f}", "length": 0, "ok": True})
                            known_files = changed
                            buf.add("text", {"text": f"\n📝 Files modified: {', '.join(sorted(changed))}\n"})
                        else:
                            # Heartbeat — just confirm it's alive
                            mins = heartbeat * 15 // 60
                            secs = (heartbeat * 15) % 60
                            buf.add("usage", {
                                "input_tokens": 0, "output_tokens": 0,
                                "total_in": 0, "total_out": 0, "cost": 0,
                                "heartbeat": f"{mins}m{secs:02d}s",
                                "files_changed": len(known_files),
                            })
                    except Exception:
                        pass

            except Exception:
                pass

        def _run():
            self._log(agent_id, "info", f"Starting agent (CLI mode, model: {model})")
            print(f"\n{'='*60}")
            print(f"🚀 AGENT LAUNCHED: {agent_id[:8]}")
            print(f"   📂 Worktree: {worktree_path}")
            print(f"   🤖 Model: {model}")
            print(f"   🔌 Mode: Claude Code CLI (subprocess)")
            print(f"{'='*60}")

            buf.add("start", {"model": model, "agent_id": agent_id[:8], "mode": "cli"})
            buf.add("text", {"text": f"⚡ Running claude -p '...' --model {model}\n"})

            try:
                cmd = ["claude", "-p", prompt, "--output-format", "text", "--permission-mode", "acceptEdits"]
                if model:
                    cmd.extend(["--model", model])

                print(f"   ▶ Running: claude -p '...' --output-format text --permission-mode acceptEdits --model {model}")

                process = subprocess.Popen(
                    cmd, cwd=worktree_path,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    start_new_session=True,
                )
                self._active_procs[agent_id] = process
                print(f"   ✓ Process started (PID: {process.pid})")

                buf.add("text", {"text": f"PID: {process.pid}\n"})

                # Start worktree monitor thread
                monitor = threading.Thread(
                    target=_monitor_worktree,
                    args=(process, buf, worktree_path, agent_id),
                    daemon=True,
                )
                monitor.start()

                # Read stdout line-by-line for live output
                all_stdout = []
                for line in process.stdout:
                    all_stdout.append(line)
                    stripped = line.rstrip('\n')
                    if stripped:
                        buf.add("text", {"text": stripped + "\n"})
                        print(f"   📤 [{agent_id[:8]}] {stripped[:120]}")

                # Read any remaining stderr
                stderr = process.stderr.read() if process.stderr else ""

                process.wait(timeout=30)
                output = "".join(all_stdout) + stderr
                success = process.returncode == 0

                print(f"\n{'='*60}")
                print(f"🏁 AGENT FINISHED: {agent_id[:8]}")
                print(f"   Return code: {process.returncode}")
                print(f"   Output length: {len(output)} chars")
                print(f"   Success: {success}")
                print(f"{'='*60}\n")

                # Estimate tokens (CLI doesn't report exact counts)
                tokens_in, tokens_out = self.estimate_tokens(output)
                cost = compute_cost(model, tokens_in, tokens_out)

                buf.add("finished", {
                    "success": success, "turns": 1, "tool_calls": 0,
                    "tokens_in": tokens_in, "tokens_out": tokens_out,
                    "cost": cost, "estimated": True,
                })
                buf.mark_done()

                if on_complete:
                    if app:
                        with app.app_context(): on_complete(agent_id, success, output)
                    else:
                        on_complete(agent_id, success, output)

            except subprocess.TimeoutExpired:
                process.kill()
                err = f"Timeout after {CLI_TIMEOUT // 60} minutes"
                print(f"   ❌ {err}")
                buf.add("error", {"message": err}); buf.mark_done()
                if on_complete:
                    if app:
                        with app.app_context(): on_complete(agent_id, False, err)
                    else:
                        on_complete(agent_id, False, err)

            except FileNotFoundError:
                err = "Claude CLI not found! Is 'claude' in PATH?"
                print(f"   ❌ {err}")
                buf.add("error", {"message": err}); buf.mark_done()
                if on_complete:
                    if app:
                        with app.app_context(): on_complete(agent_id, False, err)
                    else:
                        on_complete(agent_id, False, err)

            except Exception as e:
                import traceback
                err = f"{type(e).__name__}: {e}"
                print(f"   ❌ EXCEPTION: {err}"); traceback.print_exc()
                buf.add("error", {"message": err}); buf.mark_done()
                if on_complete:
                    if app:
                        with app.app_context(): on_complete(agent_id, False, err)
                    else:
                        on_complete(agent_id, False, err)

            finally:
                self._active_procs.pop(agent_id, None)
                print(f"   🧹 Agent {agent_id[:8]} done. Active: {self.count_active()}")

        threading.Thread(target=_run, daemon=True).start()
        return True

    # ── Agent Management ──

    def stop_agent(self, agent_id: str) -> bool:
        """Stop a running agent (works for both modes)."""
        # API mode — signal cancel event
        cancel = self._active_cancel.get(agent_id)
        if cancel:
            cancel.set()
            self._active_cancel.pop(agent_id, None)
            self._log(agent_id, "warning", "Agent stopped by user")
            buf = self.stream_buffers.get(agent_id)
            if buf: buf.add("cancelled", {}); buf.mark_done()
            return True

        # CLI mode — kill subprocess and its children
        proc = self._active_procs.get(agent_id)
        if proc:
            import signal
            try:
                # Kill entire process group (catches child processes too)
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                proc.terminate()
                # Give it a moment, then force kill
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:
                pass
            self._active_procs.pop(agent_id, None)
            self._log(agent_id, "warning", "Agent stopped by user")
            buf = self.stream_buffers.get(agent_id)
            if buf: buf.add("cancelled", {}); buf.mark_done()
            return True

        return False

    def is_running(self, agent_id: str) -> bool:
        return agent_id in self._active_cancel or agent_id in self._active_procs

    def count_active(self) -> int:
        return len(self._active_cancel) + len(self._active_procs)

    def cleanup_stream_buffer(self, agent_id: str):
        self.stream_buffers.pop(agent_id, None)

    @staticmethod
    def parse_output_issues(output: str) -> int:
        patterns = [
            r"[Ff]ound (\d+) issues?",
            r"(\d+) issues? found",
            r"[Ff]ixed (\d+) issues?",
            r"Issues found: (\d+)",
        ]
        total = 0
        for pat in patterns:
            for m in re.findall(pat, output):
                total += int(m)
        return total

    @staticmethod
    def estimate_tokens(output: str) -> tuple[int, int]:
        """Rough estimate (~4 chars/token, 3:1 in:out ratio). Used for CLI mode."""
        out_t = max(len(output) // 4, 100)
        return out_t * 3, out_t
