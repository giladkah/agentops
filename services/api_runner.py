"""
API Agent Runner — Runs Claude agents via the Anthropic API with tool use.
Replaces the CLI-based runner with direct API calls for:
  - Exact token counts and costs
  - Structured tool use (read/write files, run commands)
  - Real-time output updates (dashboard sees progress between turns)
  - No dependency on Claude CLI
"""
import os
import subprocess
import threading
import time
import json
from datetime import datetime, timezone
from typing import Optional, Callable
from pathlib import Path

import anthropic

# ── Model mapping ──
# Short names → full API model strings
MODEL_MAP = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-5-20250929",
    "opus": "claude-opus-4-6",
}

# Cost per 1M tokens (exact API pricing)
MODEL_COSTS = {
    "haiku": {"input": 0.80, "output": 4.00},
    "sonnet": {"input": 3.00, "output": 15.00},
    "opus": {"input": 15.00, "output": 75.00},
}

# ── Tool definitions ──
TOOLS = [
    {
        "name": "read_file",
        "description": "Read the contents of a file. Returns the full text content. Use this to understand existing code before making changes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from the project root (e.g., 'src/auth/login.py')"
                }
            },
            "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a file with the given content. Use for creating new files or completely rewriting existing ones.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from the project root"
                },
                "content": {
                    "type": "string",
                    "description": "The full file content to write"
                }
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "edit_file",
        "description": "Edit a file by replacing a specific text block with new text. The old_text must match EXACTLY (including whitespace). Use this for surgical edits to existing files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from the project root"
                },
                "old_text": {
                    "type": "string",
                    "description": "The exact text to find and replace (must be unique in the file)"
                },
                "new_text": {
                    "type": "string",
                    "description": "The replacement text"
                }
            },
            "required": ["path", "old_text", "new_text"]
        }
    },
    {
        "name": "list_directory",
        "description": "List files and directories at the given path. Shows file sizes and whether entries are files or directories.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from the project root (use '.' for root)",
                    "default": "."
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Maximum directory depth to traverse (default: 2)",
                    "default": 2
                }
            },
            "required": []
        }
    },
    {
        "name": "run_command",
        "description": "Execute a shell command in the project directory. Use for running tests (pytest), git commands (git diff), or other tools. Commands have a 120-second timeout.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute (e.g., 'pytest tests/', 'git diff HEAD')"
                }
            },
            "required": ["command"]
        }
    },
    {
        "name": "search_files",
        "description": "Search for a text pattern across files in the project. Returns matching lines with file paths and line numbers. Like grep -rn.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Text or regex pattern to search for"
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in (relative to project root, default: '.')",
                    "default": "."
                },
                "file_pattern": {
                    "type": "string",
                    "description": "Glob pattern to filter files (e.g., '*.py', '*.ts')",
                    "default": ""
                }
            },
            "required": ["pattern"]
        }
    },
]


def resolve_model(short_name: str) -> str:
    """Convert short model name to full API model string."""
    return MODEL_MAP.get(short_name, short_name)


def get_model_short_name(model: str) -> str:
    """Get the short name for cost lookup."""
    for short, full in MODEL_MAP.items():
        if model == full or model == short:
            return short
    return "sonnet"  # fallback


def calculate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Calculate exact cost from token counts."""
    short = get_model_short_name(model)
    rates = MODEL_COSTS.get(short, MODEL_COSTS["sonnet"])
    cost = (tokens_in / 1_000_000) * rates["input"] + (tokens_out / 1_000_000) * rates["output"]
    return round(cost, 6)


class APIAgentRunner:
    """Runs Claude agents via the Anthropic API with tool use."""

    MAX_TURNS = 100       # Max tool-use round trips per agent
    MAX_TOKENS = 16384    # Max output tokens per API call
    CMD_TIMEOUT = 120     # Shell command timeout in seconds

    def __init__(self, api_key: str = None, log_callback: Optional[Callable] = None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.log_callback = log_callback
        self.active_agents: dict[str, dict] = {}  # agent_id -> {thread, cancel_event}

    def _log(self, agent_id: str, level: str, message: str):
        if self.log_callback:
            self.log_callback(agent_id, level, message)

    def _create_client(self) -> anthropic.Anthropic:
        """Create a new Anthropic client (thread-safe — one per agent)."""
        return anthropic.Anthropic(api_key=self.api_key)

    # ── Tool execution ──

    def _execute_tool(self, tool_name: str, tool_input: dict, worktree_path: str) -> str:
        """Execute a tool call and return the result string."""
        try:
            if tool_name == "read_file":
                return self._tool_read_file(worktree_path, tool_input["path"])
            elif tool_name == "write_file":
                return self._tool_write_file(worktree_path, tool_input["path"], tool_input["content"])
            elif tool_name == "edit_file":
                return self._tool_edit_file(worktree_path, tool_input["path"], tool_input["old_text"], tool_input["new_text"])
            elif tool_name == "list_directory":
                return self._tool_list_directory(worktree_path, tool_input.get("path", "."), tool_input.get("max_depth", 2))
            elif tool_name == "run_command":
                return self._tool_run_command(worktree_path, tool_input["command"])
            elif tool_name == "search_files":
                return self._tool_search_files(worktree_path, tool_input["pattern"], tool_input.get("path", "."), tool_input.get("file_pattern", ""))
            else:
                return f"Unknown tool: {tool_name}"
        except Exception as e:
            return f"Error executing {tool_name}: {type(e).__name__}: {str(e)}"

    def _tool_read_file(self, worktree: str, path: str) -> str:
        full_path = os.path.join(worktree, path)
        if not os.path.exists(full_path):
            return f"File not found: {path}"
        if not os.path.isfile(full_path):
            return f"Not a file: {path}"
        try:
            size = os.path.getsize(full_path)
            if size > 500_000:  # 500KB limit
                return f"File too large ({size:,} bytes). Use search_files to find specific content."
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            return content
        except Exception as e:
            return f"Error reading {path}: {e}"

    def _tool_write_file(self, worktree: str, path: str, content: str) -> str:
        full_path = os.path.join(worktree, path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        try:
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
            size = os.path.getsize(full_path)
            return f"Written {size:,} bytes to {path}"
        except Exception as e:
            return f"Error writing {path}: {e}"

    def _tool_edit_file(self, worktree: str, path: str, old_text: str, new_text: str) -> str:
        full_path = os.path.join(worktree, path)
        if not os.path.exists(full_path):
            return f"File not found: {path}"
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read()
            count = content.count(old_text)
            if count == 0:
                # Show a preview of the file to help the agent find the right text
                lines = content.split("\n")
                preview = "\n".join(f"{i+1}: {l}" for i, l in enumerate(lines[:50]))
                return f"old_text not found in {path}. File has {len(lines)} lines. First 50:\n{preview}"
            if count > 1:
                return f"old_text found {count} times in {path}. Must be unique. Add more context to make it unique."
            new_content = content.replace(old_text, new_text, 1)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            return f"Edited {path}: replaced {len(old_text)} chars with {len(new_text)} chars"
        except Exception as e:
            return f"Error editing {path}: {e}"

    def _tool_list_directory(self, worktree: str, path: str, max_depth: int) -> str:
        full_path = os.path.join(worktree, path)
        if not os.path.exists(full_path):
            return f"Directory not found: {path}"
        if not os.path.isdir(full_path):
            return f"Not a directory: {path}"

        result_lines = []
        skip_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv", ".wt", ".mypy_cache", ".pytest_cache"}

        def _walk(current: str, prefix: str, depth: int):
            if depth > max_depth:
                return
            try:
                entries = sorted(os.listdir(current))
            except PermissionError:
                return
            dirs = []
            files = []
            for e in entries:
                if e.startswith(".") and e != ".env":
                    continue
                fp = os.path.join(current, e)
                if os.path.isdir(fp):
                    if e not in skip_dirs:
                        dirs.append(e)
                else:
                    size = os.path.getsize(fp)
                    files.append(f"{prefix}{e} ({size:,} bytes)")

            for f in files:
                result_lines.append(f)
            for d in dirs:
                result_lines.append(f"{prefix}{d}/")
                _walk(os.path.join(current, d), prefix + "  ", depth + 1)

        _walk(full_path, "", 0)
        if not result_lines:
            return f"Directory {path} is empty or contains only hidden files."
        return "\n".join(result_lines[:500])  # Cap at 500 lines

    def _tool_run_command(self, worktree: str, command: str) -> str:
        # Security: block dangerous commands
        dangerous = ["rm -rf /", "rm -rf ~", "mkfs", "dd if=", "> /dev/"]
        for d in dangerous:
            if d in command:
                return f"Blocked dangerous command: {command}"

        try:
            result = subprocess.run(
                command, shell=True, cwd=worktree,
                capture_output=True, text=True,
                timeout=self.CMD_TIMEOUT,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                if output:
                    output += "\n--- stderr ---\n"
                output += result.stderr

            # Truncate very long outputs
            if len(output) > 50_000:
                output = output[:25_000] + f"\n\n... truncated ({len(output):,} chars total) ...\n\n" + output[-25_000:]

            exit_info = f"[exit code: {result.returncode}]"
            return f"{exit_info}\n{output}" if output else exit_info
        except subprocess.TimeoutExpired:
            return f"Command timed out after {self.CMD_TIMEOUT}s: {command}"
        except Exception as e:
            return f"Command error: {e}"

    def _tool_search_files(self, worktree: str, pattern: str, path: str, file_pattern: str) -> str:
        search_path = os.path.join(worktree, path)
        if not os.path.exists(search_path):
            return f"Path not found: {path}"

        cmd = ["grep", "-rn", "--include", file_pattern or "*", pattern, search_path]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30, cwd=worktree,
            )
            output = result.stdout.strip()
            if not output:
                return f"No matches found for '{pattern}'"
            # Make paths relative
            output = output.replace(worktree + "/", "")
            lines = output.split("\n")
            if len(lines) > 100:
                return "\n".join(lines[:100]) + f"\n\n... and {len(lines) - 100} more matches"
            return output
        except subprocess.TimeoutExpired:
            return "Search timed out — try a more specific pattern or path"
        except Exception as e:
            return f"Search error: {e}"

    # ── Agent launch ──

    def build_prompt(self, persona_template: str, task: str, extra_context: str = "") -> str:
        """Assemble the full prompt from persona + task + context."""
        parts = [persona_template.strip()]
        if extra_context:
            parts.append(f"\n## Context\n{extra_context.strip()}")
        parts.append(f"\n## Your Task\n{task.strip()}")
        parts.append(
            "\n## Rules\n"
            "- Read relevant files before making changes.\n"
            "- Run tests (pytest) after every significant change.\n"
            "- When done, state '✅ TASK COMPLETE' and summarize what you did.\n"
            "- If blocked, state '❌ BLOCKED: [reason]'.\n"
            "- Make minimal, focused changes — don't refactor unrelated code."
        )
        return "\n\n".join(parts)

    def launch_agent(
        self,
        agent_id: str,
        worktree_path: str,
        prompt: str,
        model: str = "sonnet",
        on_complete: Optional[Callable] = None,
        app=None,
        agent_role: str = None,
    ) -> bool:
        """
        Launch an API-based agent in a background thread.
        Same interface as the CLI AgentRunner.
        """
        if agent_id in self.active_agents:
            return False

        cancel_event = threading.Event()

        def _run():
            self._run_agent_loop(
                agent_id=agent_id,
                worktree_path=worktree_path,
                prompt=prompt,
                model=model,
                on_complete=on_complete,
                app=app,
                cancel_event=cancel_event,
                agent_role=agent_role,
            )

        thread = threading.Thread(target=_run, daemon=True)
        self.active_agents[agent_id] = {"thread": thread, "cancel": cancel_event}
        thread.start()
        return True

    def _run_agent_loop(
        self,
        agent_id: str,
        worktree_path: str,
        prompt: str,
        model: str,
        on_complete: Optional[Callable],
        app,
        cancel_event: threading.Event,
        agent_role: str = None,
    ):
        """The main agentic loop — send messages, handle tool calls, repeat."""
        full_model = resolve_model(model)
        short_model = get_model_short_name(model)

        print(f"\n{'='*60}")
        print(f"🚀 API AGENT LAUNCHED: {agent_id[:8]}")
        print(f"   📂 Worktree: {worktree_path}")
        print(f"   🤖 Model: {short_model} ({full_model})")
        print(f"   📝 Prompt length: {len(prompt)} chars")
        print(f"   🔧 Tools: {len(TOOLS)} available")
        print(f"{'='*60}")

        client = self._create_client()
        total_input_tokens = 0
        total_output_tokens = 0
        turn_count = 0
        output_parts = []  # Collect all text output

        # Conversation history
        messages = [{"role": "user", "content": prompt}]

        success = False
        final_output = ""

        try:
            while turn_count < self.MAX_TURNS:
                if cancel_event.is_set():
                    output_parts.append("\n⏹ Agent cancelled by user.")
                    break

                turn_count += 1
                print(f"   🔄 Turn {turn_count}/{self.MAX_TURNS}...")

                # Make API call
                try:
                    response = client.messages.create(
                        model=full_model,
                        max_tokens=self.MAX_TOKENS,
                        system="You are a code agent with access to file and shell tools. Work methodically: read first, plan, then make changes. Always verify your work by running tests.",
                        messages=messages,
                        tools=TOOLS,
                    )
                except anthropic.APIError as e:
                    error_msg = f"API error: {type(e).__name__}: {e}"
                    print(f"   ❌ {error_msg}")
                    output_parts.append(f"\n❌ {error_msg}")
                    break

                # Track tokens
                usage = response.usage
                total_input_tokens += usage.input_tokens
                total_output_tokens += usage.output_tokens
                cost_so_far = calculate_cost(full_model, total_input_tokens, total_output_tokens)

                print(f"   📊 Tokens: +{usage.input_tokens}in/+{usage.output_tokens}out (total: {total_input_tokens}/{total_output_tokens}, ${cost_so_far:.4f})")

                # Process response content
                assistant_content = response.content
                has_tool_use = any(block.type == "tool_use" for block in assistant_content)

                # Extract text blocks for output
                for block in assistant_content:
                    if block.type == "text" and block.text.strip():
                        output_parts.append(block.text)
                        # Print a preview
                        preview = block.text[:200].replace('\n', ' ')
                        print(f"   💬 {preview}{'...' if len(block.text) > 200 else ''}")

                # Update output in DB periodically (every turn)
                current_output = "\n\n".join(output_parts)
                self._update_agent_output(agent_id, current_output, total_input_tokens, total_output_tokens, cost_so_far, app)

                # If no tool use, we're done
                if not has_tool_use or response.stop_reason == "end_turn":
                    success = True
                    if "BLOCKED" in current_output:
                        success = False
                    print(f"   ✅ Agent finished (stop_reason={response.stop_reason})")
                    break

                # Handle tool calls
                messages.append({"role": "assistant", "content": assistant_content})
                tool_results = []

                for block in assistant_content:
                    if block.type == "tool_use":
                        tool_name = block.name
                        tool_input = block.input
                        tool_id = block.id

                        print(f"   🔧 Tool: {tool_name}({json.dumps(tool_input)[:100]}...)")
                        result = self._execute_tool(tool_name, tool_input, worktree_path)
                        # Compress tool output for token savings
                        from services.token_optimizer import compress_tool_output
                        result = compress_tool_output(tool_name, tool_input, result, agent_role)

                        # Log tool actions for output
                        if tool_name in ("write_file", "edit_file"):
                            output_parts.append(f"📝 {tool_name}: {tool_input.get('path', '?')} → {result}")
                        elif tool_name == "run_command":
                            cmd_preview = tool_input.get("command", "?")
                            result_preview = result[:200] if result else ""
                            output_parts.append(f"💻 $ {cmd_preview}\n{result_preview}")

                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": result[:50_000],  # Cap tool result size
                        })

                        print(f"   📤 Result: {result[:150]}{'...' if len(result) > 150 else ''}")

                messages.append({"role": "user", "content": tool_results})

                # Compress old messages every 3 turns after turn 4
                if turn_count > 4 and turn_count % 3 == 0:
                    from services.token_optimizer import summarize_old_messages
                    messages = summarize_old_messages(messages, keep_last_n=3)

            else:
                # Hit max turns
                output_parts.append(f"\n⚠️ Hit maximum turns ({self.MAX_TURNS}). Stopping.")
                print(f"   ⚠️ Max turns reached")

        except Exception as e:
            import traceback
            error_msg = f"Agent error: {type(e).__name__}: {e}"
            print(f"   ❌ {error_msg}")
            traceback.print_exc()
            output_parts.append(f"\n❌ {error_msg}")
            success = False

        finally:
            final_output = "\n\n".join(output_parts)
            total_cost = calculate_cost(full_model, total_input_tokens, total_output_tokens)

            print(f"\n{'='*60}")
            print(f"🏁 API AGENT FINISHED: {agent_id[:8]}")
            print(f"   Turns: {turn_count}")
            print(f"   Tokens: {total_input_tokens:,} in / {total_output_tokens:,} out")
            print(f"   Cost: ${total_cost:.4f}")
            print(f"   Success: {success}")
            print(f"   Output length: {len(final_output)} chars")
            if final_output:
                preview = final_output[-500:].replace('\n', '\n   ')
                print(f"   Output tail:\n   {preview}")
            print(f"{'='*60}\n")

            # Final DB update
            self._update_agent_output(agent_id, final_output, total_input_tokens, total_output_tokens, total_cost, app)

            # Call completion callback
            if on_complete:
                try:
                    if app:
                        with app.app_context():
                            on_complete(agent_id, success, final_output)
                    else:
                        on_complete(agent_id, success, final_output)
                except Exception as e:
                    print(f"   ❌ on_complete error: {e}")

            # Remove from active
            self.active_agents.pop(agent_id, None)
            print(f"   🧹 Agent {agent_id[:8]} removed. Active: {len(self.active_agents)}")

    def _update_agent_output(self, agent_id: str, output: str, tokens_in: int, tokens_out: int, cost: float, app):
        """Update the agent's output_log and token counts in the DB (called every turn)."""
        try:
            from models import db, Agent
            if app:
                with app.app_context():
                    agent = Agent.query.get(agent_id)
                    if agent:
                        agent.output_log = output
                        agent.tokens_in = tokens_in
                        agent.tokens_out = tokens_out
                        agent.cost = cost
                        db.session.commit()
            else:
                agent = Agent.query.get(agent_id)
                if agent:
                    agent.output_log = output
                    agent.tokens_in = tokens_in
                    agent.tokens_out = tokens_out
                    agent.cost = cost
                    db.session.commit()
        except Exception as e:
            # Don't let DB update failures kill the agent
            print(f"   ⚠️ DB update error (non-fatal): {e}")

    # ── Control ──

    def stop_agent(self, agent_id: str) -> bool:
        """Stop a running agent."""
        info = self.active_agents.get(agent_id)
        if info:
            info["cancel"].set()
            self._log(agent_id, "warning", "Agent stop requested")
            return True
        return False

    def is_running(self, agent_id: str) -> bool:
        return agent_id in self.active_agents

    def count_active(self) -> int:
        return len(self.active_agents)

    @staticmethod
    def parse_output_issues(output: str) -> int:
        """Count issues found from agent output."""
        import re
        patterns = [
            r"[Ff]ound (\d+) issues?",
            r"(\d+) issues? found",
            r"[Ff]ixed (\d+) issues?",
            r"Issues found: (\d+)",
            r"Issues fixed: (\d+)",
        ]
        total = 0
        for pattern in patterns:
            matches = re.findall(pattern, output)
            for m in matches:
                total += int(m)
        return total

    @staticmethod
    def estimate_tokens(output: str) -> tuple[int, int]:
        """Not needed with API (we have exact counts), but kept for compatibility."""
        out_tokens = max(len(output) // 4, 100)
        in_tokens = out_tokens * 3
        return in_tokens, out_tokens
