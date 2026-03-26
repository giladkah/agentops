"""
Chat Service — AI-powered signal triage conversations.

Uses the Anthropic API with the same codebase tools as agents (read_file, search_files, etc.)
to analyze signals, ask targeted questions, and propose runs.
"""
import json
import os
import subprocess
import threading
from datetime import datetime, timezone
from typing import Optional

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

from models import db, Signal, SignalCluster, Workflow, Persona


# ── Tools for the chat AI (same as agent tools, scoped to repo) ──

CHAT_TOOLS = [
    {
        "name": "read_file",
        "description": "Read the contents of a file in the codebase. Path is relative to the project root.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to project root"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and subdirectories in a directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path relative to project root (use '.' for root)"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_files",
        "description": "Search for a pattern in files using grep. Returns matching lines with file paths.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Search pattern (regex supported)"},
                "path": {"type": "string", "description": "Directory to search in (default: '.')", "default": "."},
                "file_pattern": {"type": "string", "description": "File glob pattern (e.g., '*.py', '*.js')", "default": ""},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "run_command",
        "description": "Run a shell command in the project directory. Use for git log, git diff, pytest, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"}
            },
            "required": ["command"],
        },
    },
    {
        "name": "propose_run",
        "description": "Propose a run configuration based on your analysis. Call this when you have enough context to recommend a specific action plan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "workflow_name": {
                    "type": "string",
                    "description": "Workflow to use: 'Bug Fix', 'Code Cleanup', 'New Feature', 'Security Audit', or 'Test Coverage'",
                },
                "title": {"type": "string", "description": "Short title for the run"},
                "task_description": {
                    "type": "string",
                    "description": "Detailed task description including: what to fix/build, which files to focus on, acceptance criteria, any context the engineer needs.",
                },
                "auto_approve": {
                    "type": "boolean",
                    "description": "Whether to auto-approve checkpoints (true for straightforward fixes, false for risky changes)",
                    "default": True,
                },
                "model": {
                    "type": "string",
                    "description": "Model for the engineer: 'haiku' (default, fast + cheap), 'sonnet' (complex tasks), 'opus' (hardest tasks)",
                    "default": "haiku",
                },
            },
            "required": ["workflow_name", "title", "task_description"],
        },
    },
]

import shutil

COMMAND_TIMEOUT = 30  # seconds for run_command
CLI_TIMEOUT = 300  # 5 min for CLI triage
BLOCKED_COMMANDS = ["rm -rf", "sudo", "> /dev", "mkfs", "dd if="]


def _safe_path(repo_path: str, relative: str) -> Optional[str]:
    """Resolve path and ensure it's within the repo."""
    full = os.path.normpath(os.path.join(repo_path, relative))
    if not full.startswith(os.path.normpath(repo_path)):
        return None
    return full


class ChatService:
    """Manages AI triage conversations for signals. Supports API and CLI modes."""

    def __init__(self, api_key: str = None, repo_path: str = None, app=None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.repo_path = repo_path or os.getcwd()
        self.app = app
        self.client = None

        if HAS_ANTHROPIC and self.api_key:
            self.client = anthropic.Anthropic(api_key=self.api_key)

    def has_cli(self) -> bool:
        return shutil.which("claude") is not None

    def available(self) -> bool:
        """Check if any backend is available."""
        return bool(self.client) or self.has_cli()

    def mode(self) -> str:
        """Return current mode: 'api', 'cli', or 'none'."""
        if self.client:
            return "api"
        if self.has_cli():
            return "cli"
        return "none"

    def _enrich_signal_context(self, signal: Signal) -> str:
        """Fetch full ticket details from source APIs (Shortcut, GitHub)."""
        enriched = ""

        try:
            if signal.source == "shortcut":
                match = __import__("re").match(r"SC-(\d+)", signal.source_id or "")
                if match:
                    from services.shortcut_poller import ShortcutPoller
                    poller = ShortcutPoller()
                    if poller.token:
                        story = poller._sc_get(f"/stories/{match.group(1)}")
                        parts = []
                        desc = story.get("description", "")
                        if desc:
                            parts.append(f"FULL DESCRIPTION:\n{desc[:3000]}")

                        labels = [l.get("name", "") for l in story.get("labels", []) if isinstance(l, dict)]
                        if labels:
                            parts.append(f"LABELS: {', '.join(labels)}")

                        custom_fields = [
                            f"{cf.get('value', '')}"
                            for cf in story.get("custom_fields", [])
                            if isinstance(cf, dict) and cf.get("value")
                        ]
                        if custom_fields:
                            parts.append(f"CUSTOM FIELDS: {', '.join(custom_fields)}")

                        tasks = story.get("tasks", [])
                        if tasks:
                            task_lines = [
                                f"  {'✅' if t.get('complete') else '☐'} {t.get('description', '')}"
                                for t in tasks if isinstance(t, dict)
                            ]
                            parts.append(f"TASKS:\n" + "\n".join(task_lines))

                        comments = sorted(
                            [c for c in story.get("comments", []) if isinstance(c, dict)],
                            key=lambda c: c.get("created_at", "")
                        )
                        if comments:
                            comment_lines = [
                                f"  [{c.get('created_at', '')[:10]}] {c.get('text', '')[:300]}"
                                for c in comments[:10]
                            ]
                            parts.append(f"COMMENTS ({len(comments)} total):\n" + "\n".join(comment_lines))

                        story_type = story.get("story_type", "")
                        if story_type:
                            parts.append(f"STORY TYPE: {story_type}")

                        estimate = story.get("estimate")
                        if estimate:
                            parts.append(f"ESTIMATE: {estimate} points")

                        app_url = story.get("app_url", "")
                        if app_url:
                            parts.append(f"URL: {app_url}")

                        if parts:
                            enriched = "\n\nENRICHED TICKET DATA (from Shortcut API):\n" + "\n".join(parts)

            elif signal.source == "github":
                match = __import__("re").match(r"GH-(\d+)", signal.source_id or "")
                if match:
                    from services.github_poller import GitHubPoller
                    poller = GitHubPoller()
                    if poller.token:
                        raw = signal.get_raw_payload()
                        repo = raw.get("repo", "")
                        if repo:
                            number = match.group(1)
                            issue = poller._github_get(f"https://api.github.com/repos/{repo}/issues/{number}")
                            parts = []
                            body = issue.get("body", "")
                            if body:
                                parts.append(f"FULL DESCRIPTION:\n{body[:3000]}")

                            # Fetch comments
                            comments_url = issue.get("comments_url", "")
                            if comments_url and issue.get("comments", 0) > 0:
                                comments = poller._github_get(comments_url)
                                if comments:
                                    comment_lines = [
                                        f"  [{c.get('created_at', '')[:10]}] @{c.get('user', {}).get('login', '?')}: {c.get('body', '')[:300]}"
                                        for c in comments[:10] if isinstance(c, dict)
                                    ]
                                    parts.append(f"COMMENTS ({len(comments)} total):\n" + "\n".join(comment_lines))

                            if parts:
                                enriched = "\n\nENRICHED TICKET DATA (from GitHub API):\n" + "\n".join(parts)

            elif signal.source == "sentry":
                match = __import__("re").match(r"SENTRY-(\d+)", signal.source_id or "")
                if match:
                    from services.sentry_poller import SentryPoller
                    poller = SentryPoller()
                    if poller.token:
                        issue_id = match.group(1)
                        try:
                            detail = poller.fetch_issue_detail(issue_id)
                            parts = []

                            # Basic info
                            if detail.get("culprit"):
                                parts.append(f"CULPRIT: {detail['culprit']}")
                            parts.append(f"LEVEL: {detail.get('level', '?')}")
                            parts.append(f"EVENTS: {detail.get('count', '?')} | USERS AFFECTED: {detail.get('userCount', '?')}")
                            parts.append(f"FIRST SEEN: {detail.get('firstSeen', '?')}")
                            parts.append(f"LAST SEEN: {detail.get('lastSeen', '?')}")

                            if detail.get("permalink"):
                                parts.append(f"URL: {detail['permalink']}")

                            # Stack trace
                            st = detail.get("stacktrace")
                            if st:
                                parts.append(f"\nEXCEPTION: {st.get('type', '?')}: {st.get('value', '')[:500]}")
                                frames = st.get("frames", [])
                                if frames:
                                    # Show in-app frames first, then all frames
                                    in_app = [f for f in frames if f.get("inApp")]
                                    show_frames = in_app if in_app else frames[-10:]
                                    frame_lines = []
                                    for f in show_frames:
                                        line = f"  {f.get('filename', '?')}:{f.get('lineNo', '?')} in {f.get('function', '?')}"
                                        if f.get("module"):
                                            line += f" [{f['module']}]"
                                        frame_lines.append(line)
                                    parts.append(f"STACK TRACE ({len(in_app)} in-app frames, {len(frames)} total):\n" + "\n".join(frame_lines))

                            # Tags
                            tags = detail.get("tags", [])
                            useful_tags = [t for t in tags if t.get("key") in ("browser", "os", "environment", "server_name", "release", "url", "transaction")]
                            if useful_tags:
                                tag_lines = [
                                    f"  {t['key']}: {', '.join(v['value'] for v in t.get('topValues', []))}"
                                    for t in useful_tags
                                ]
                                parts.append(f"TAGS:\n" + "\n".join(tag_lines))

                            # Breadcrumbs
                            crumbs = detail.get("breadcrumbs", [])
                            if crumbs:
                                crumb_lines = [
                                    f"  [{c.get('category', '?')}] {c.get('message', '')[:100]}"
                                    for c in crumbs[-5:]
                                ]
                                parts.append(f"RECENT BREADCRUMBS:\n" + "\n".join(crumb_lines))

                            if parts:
                                enriched = "\n\nENRICHED ISSUE DATA (from Sentry API — includes stack trace):\n" + "\n".join(parts)

                        except Exception as e:
                            print(f"⚠️ Sentry enrichment detail fetch failed: {e}")

        except Exception as e:
            print(f"⚠️ Signal enrichment failed: {e}")

        return enriched

    def _build_system_prompt(self, signal: Signal, workflows: list, personas: list) -> str:
        """Build the system prompt with signal context and available workflows."""

        # Format workflows
        wf_list = "\n".join([
            f"  - {w['name']} ({w['icon']}): {w.get('description', '')} — stages: {', '.join(s['name'] for s in w.get('stages', []))}"
            for w in workflows
        ])

        # Format personas
        persona_list = "\n".join([
            f"  - {p['name']} ({p['icon']}): role={p['role']}, default_model={p['default_model']}"
            for p in personas
        ])

        # Format signal context
        signal_ctx = f"""
SOURCE: {signal.source}
SOURCE ID: {signal.source_id or 'N/A'}
TITLE: {signal.title}
SUMMARY: {signal.summary}
SEVERITY: {signal.severity}
FILES HINT: {json.dumps(signal.get_files_hint())}
STATUS: {signal.status}
"""

        # Include raw payload summary if it has content
        raw = signal.get_raw_payload()
        raw_summary = ""
        if raw and raw != {}:
            raw_summary = f"\nRAW PAYLOAD (from source):\n{json.dumps(raw, indent=2)[:2000]}"

        # Fetch enriched context from source APIs (Shortcut, GitHub)
        enriched_context = self._enrich_signal_context(signal)

        return f"""You are an intelligent engineering assistant that triages and analyzes signals (bugs, features, issues) for a software project.

You have access to the full codebase through tools: read_file, list_directory, search_files, run_command.
The project is at: {self.repo_path}

YOUR JOB:
1. READ the full ticket context below carefully — understand what is being asked BEFORE touching the codebase
2. Investigate using your tools — read relevant files, check git history, understand the codebase context
3. Share what you found as a STRUCTURED REPORT (see format below)
4. Ask targeted questions about things you can't determine from the code alone
5. ONLY propose a run when you have a clear, specific implementation plan

CRITICAL RULES:
- If the user explicitly asks to "create a run", "prepare a run", "start a run", or "make a run" — propose one immediately based on what you know. Do not investigate further.
- IMPORTANT: You MUST call the propose_run tool to create a run. Writing "Run created" or describing a run in text does NOT create one. The user can only see and start a run if you actually call propose_run.
- Investigate first, but BE DECISIVE. Once you understand the problem and know which files are involved, propose a run. Do not keep investigating endlessly.
- When you DO propose a run, the task_description must be extremely specific with exact file paths, function names, and what to change.
- Always set model to 'haiku' unless the user specifically requests a different model.
- ALWAYS end your response with either: (a) a concrete run proposal via the propose_run tool, or (b) a clear summary of findings + specific questions if you need more info.
- Never leave the user hanging with just investigation notes and no conclusion.

OUTPUT FORMAT — STRICTLY FOLLOW THIS:
Do NOT output raw reasoning or stream-of-consciousness. Never write "Let me check...", "Now I will...", "Interesting!".
Transform your investigation into a clean, structured report:

**Summary** — 2-3 sentences: what is the issue, why it matters
**Root Cause** — Clear, direct explanation (3-4 lines max)
**Key Findings** — Bullet points only, one concrete observation each
**Evidence** — Specific files, line numbers, logs (short references, not dumps)
**Recommended Action** — Numbered steps, clear and executable
**Risks / Notes** — Only if relevant, keep concise

Use markdown formatting: **bold** for headers, `code` for file paths and functions, bullet points for lists.
Keep it scannable. No filler language. No repetition. Write like a senior engineer filing a concise report.

INVESTIGATION APPROACH:
- Investigate immediately with tool calls — don't describe what you'll do
- Be specific: exact file paths, line numbers, function names
- Connect dots: related issues, recent changes
- Adapt to the source type:
  * Sentry: focus on stack traces, recent code changes, error frequency
  * Shortcut: focus on acceptance criteria, affected modules, related items
  * GitHub: focus on issue labels, linked code, existing discussion
  * Manual: ask more questions since there's less structured data

SIGNAL BEING TRIAGED:
{signal_ctx}{raw_summary}{enriched_context}

AVAILABLE WORKFLOWS:
{wf_list}

AVAILABLE PERSONAS:
{persona_list}

When using propose_run, match the workflow to the task type:
- Bugs / regressions → Bug Fix
- Code quality / refactoring → Code Cleanup
- New functionality → New Feature
- Security concerns → Security Audit
- Missing tests → Test Coverage
Always set model to "haiku" unless the user specifically requests a different model.

Keep responses concise but informative. Use code formatting for file paths and code snippets."""

    def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        """Execute a tool call and return the result."""

        if tool_name == "read_file":
            path = _safe_path(self.repo_path, tool_input["path"])
            if not path:
                return "Error: path is outside the project directory."
            if not os.path.isfile(path):
                return f"Error: file not found: {tool_input['path']}"
            try:
                with open(path, "r", errors="replace") as f:
                    content = f.read()
                if len(content) > 12000:
                    content = content[:10000] + "\n\n... [truncated — showing first 10000 chars of " + str(len(content)) + "]"
                return content
            except Exception as e:
                return f"Error reading file: {e}"

        elif tool_name == "list_directory":
            path = _safe_path(self.repo_path, tool_input.get("path", "."))
            if not path:
                return "Error: path is outside the project directory."
            if not os.path.isdir(path):
                return f"Error: not a directory: {tool_input.get('path', '.')}"
            try:
                entries = sorted(os.listdir(path))
                # Filter out common noise
                skip = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}
                entries = [e for e in entries if e not in skip]
                result = []
                for e in entries[:100]:
                    full = os.path.join(path, e)
                    if os.path.isdir(full):
                        result.append(f"  {e}/")
                    else:
                        size = os.path.getsize(full)
                        result.append(f"  {e} ({size} bytes)")
                return "\n".join(result) if result else "(empty directory)"
            except Exception as e:
                return f"Error listing directory: {e}"

        elif tool_name == "search_files":
            pattern = tool_input["pattern"]
            search_path = _safe_path(self.repo_path, tool_input.get("path", "."))
            if not search_path:
                return "Error: path is outside the project directory."
            cmd = ["grep", "-rn", "--include=*", pattern, search_path]
            file_pat = tool_input.get("file_pattern", "")
            if file_pat:
                cmd = ["grep", "-rn", f"--include={file_pat}", pattern, search_path]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, cwd=self.repo_path)
                output = result.stdout[:5000]
                if not output:
                    return f"No matches found for pattern: {pattern}"
                # Make paths relative
                output = output.replace(self.repo_path + "/", "")
                return output
            except subprocess.TimeoutExpired:
                return "Search timed out (pattern too broad?)"
            except Exception as e:
                return f"Search error: {e}"

        elif tool_name == "run_command":
            command = tool_input["command"]
            # Safety check
            for blocked in BLOCKED_COMMANDS:
                if blocked in command:
                    return f"Error: command blocked for safety ({blocked})"
            try:
                result = subprocess.run(
                    command, shell=True, capture_output=True, text=True,
                    timeout=COMMAND_TIMEOUT, cwd=self.repo_path,
                )
                output = ""
                if result.stdout:
                    output += result.stdout[:5000]
                if result.stderr:
                    output += "\n[stderr]\n" + result.stderr[:1000]
                if result.returncode != 0:
                    output += f"\n[exit code: {result.returncode}]"
                return output or "(no output)"
            except subprocess.TimeoutExpired:
                return "Command timed out (30s limit)"
            except Exception as e:
                return f"Command error: {e}"

        elif tool_name == "propose_run":
            # This is handled specially — we save the proposal to the signal
            return json.dumps(tool_input)

        return f"Unknown tool: {tool_name}"

    def send_message(self, signal_id: str, user_message: str) -> dict:
        """
        Send a user message in the signal's chat, get AI response.
        Routes to API or CLI mode depending on availability.

        Returns: {
            "messages": [{"role": "assistant", "content": "...", "tool_calls": [...]}],
            "proposed_run": {...} or None,
            "error": None or "error message"
        }
        """
        if not self.available():
            return {"messages": [], "proposed_run": None,
                    "error": "No AI backend available. Set ANTHROPIC_API_KEY for API mode, or install Claude Code CLI."}

        signal = Signal.query.get(signal_id)
        if not signal:
            return {"messages": [], "proposed_run": None, "error": "Signal not found"}

        # Update status if this is first interaction
        if signal.status == "new":
            signal.status = "investigating"

        # Save user message
        if user_message.strip():
            signal.add_chat_message("user", user_message)
        db.session.commit()

        # Route to the right backend
        if self.client:
            return self._send_api(signal)
        else:
            return self._send_cli(signal, user_message)

    def _send_api(self, signal: Signal) -> dict:
        """API mode: structured tool use loop with Anthropic API."""
        # Build conversation history for API
        chat_history = signal.get_chat_messages()
        api_messages = []
        for msg in chat_history:
            if msg["role"] in ("user", "assistant"):
                # Truncate very long messages to avoid context overflow
                content = msg["content"]
                if len(content) > 8000:
                    content = content[:4000] + "\n\n[...truncated...]\n\n" + content[-2000:]
                api_messages.append({"role": msg["role"], "content": content})

        # Keep only the last N messages to stay within context limits
        if len(api_messages) > 10:
            api_messages = api_messages[:1] + api_messages[-9:]  # Keep first + last 9

        # Ensure conversation starts with a user message (API requirement)
        if api_messages and api_messages[0]["role"] != "user":
            api_messages.insert(0, {"role": "user", "content": "Continue the investigation."})

        # Remove consecutive same-role messages (API rejects these)
        deduped = []
        for msg in api_messages:
            if deduped and deduped[-1]["role"] == msg["role"]:
                deduped[-1]["content"] += "\n\n" + msg["content"]
            else:
                deduped.append(msg)
        api_messages = deduped

        # If no messages yet (first auto-triage), add a trigger message
        if not api_messages:
            api_messages = [{"role": "user", "content": "Analyze this signal and tell me what you find. Use your tools to investigate the codebase."}]

        # Get workflows and personas for system prompt
        workflows = [w.to_dict() for w in Workflow.query.all()]
        personas = [p.to_dict() for p in Persona.query.all()]
        system_prompt = self._build_system_prompt(signal, workflows, personas)

        # Fast path: if user explicitly asks for a run, force propose_run directly
        last_user_msg = ""
        for msg in reversed(api_messages):
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                last_user_msg = msg["content"].lower()
                break
        _run_phrases = [
            "create a run", "start a run", "make a run", "prepare a run",
            "create run", "start run", "run please", "suggest a run",
            "let's create", "let's start", "let's run", "yes, let",
            "yes please", "yes, create", "go ahead", "do it",
        ]
        if any(p in last_user_msg for p in _run_phrases):
            return self._force_propose_run_signal(signal, workflows, api_messages)

        # Run the agentic loop (tool use)
        proposed_run = None
        all_text_parts = []
        tool_calls_log = []
        turns = 0
        max_turns = 25

        try:
            while turns < max_turns:
                turns += 1

                # Inject a wrap-up message when approaching the turn limit
                if turns == max_turns - 3 and api_messages[-1]["role"] != "user":
                    api_messages.append({"role": "user", "content": "[SYSTEM] You have 3 turns left. Start wrapping up your investigation. Summarize what you found so far."})
                elif turns == max_turns - 1:
                    # Force a conclusion on the last turn by removing tools
                    response = self.client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=4096,
                        system=system_prompt + "\n\nFINAL TURN: Summarize your findings and use propose_run if you have enough context for a fix. Do NOT make any more tool calls.",
                        tools=CHAT_TOOLS,
                        messages=api_messages,
                    )
                    # Process this response and break below
                    assistant_content = []
                    text_parts = []
                    tool_use_blocks = []
                    for block in response.content:
                        if block.type == "text":
                            text_parts.append(block.text)
                            assistant_content.append({"type": "text", "text": block.text})
                        elif block.type == "tool_use":
                            tool_use_blocks.append(block)
                            assistant_content.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
                    api_messages.append({"role": "assistant", "content": assistant_content})
                    all_text_parts.extend(text_parts)
                    # Handle propose_run if it used it
                    for tool_block in tool_use_blocks:
                        if tool_block.name == "propose_run":
                            proposed_run = tool_block.input
                            wf_name = proposed_run.get("workflow_name", "")
                            matched_wf = next((w for w in workflows if w["name"].lower() == wf_name.lower()), workflows[0] if workflows else None)
                            if matched_wf:
                                proposed_run["workflow_id"] = matched_wf["id"]
                                proposed_run["workflow"] = matched_wf
                            signal.proposed_run = json.dumps(proposed_run)
                            signal.status = "ready"
                    break

                response = self.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=4096,
                    system=system_prompt,
                    tools=CHAT_TOOLS,
                    messages=api_messages,
                )

                # Process response blocks
                assistant_content = []
                text_parts = []
                tool_use_blocks = []

                for block in response.content:
                    if block.type == "text":
                        text_parts.append(block.text)
                        assistant_content.append({"type": "text", "text": block.text})
                    elif block.type == "tool_use":
                        tool_use_blocks.append(block)
                        assistant_content.append({
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        })

                # Add assistant response to conversation
                api_messages.append({"role": "assistant", "content": assistant_content})

                all_text_parts.extend(text_parts)

                # Execute tool calls
                if tool_use_blocks:
                    tool_results = []
                    for tool_block in tool_use_blocks:
                        result = self._execute_tool(tool_block.name, tool_block.input)
                        tool_calls_log.append({
                            "tool": tool_block.name,
                            "input": tool_block.input,
                            "result_preview": result[:200] if tool_block.name != "propose_run" else result,
                        })

                        # Handle propose_run specially
                        if tool_block.name == "propose_run":
                            proposed_run = tool_block.input
                            # Match workflow name to ID
                            wf_name = proposed_run.get("workflow_name", "")
                            matched_wf = next(
                                (w for w in workflows if w["name"].lower() == wf_name.lower()),
                                workflows[0] if workflows else None,
                            )
                            if matched_wf:
                                proposed_run["workflow_id"] = matched_wf["id"]
                                proposed_run["workflow"] = matched_wf

                            # Save to signal
                            signal.proposed_run = json.dumps(proposed_run)
                            signal.status = "ready"

                            result = "Run proposal saved. The user can now approve or modify it."

                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_block.id,
                            "content": result,
                        })

                    api_messages.append({"role": "user", "content": tool_results})

                # If no tool calls, we're done
                if not tool_use_blocks or response.stop_reason == "end_turn":
                    break

            # Save the assistant's complete response to chat
            full_response = "\n\n".join(all_text_parts)

            # Fallback: detect proposals dumped as JSON text
            if not proposed_run and full_response and "PROPOSED_RUN" in full_response:
                import re as _re
                # Find the JSON by locating PROPOSED_RUN and extracting balanced braces
                def extract_json_with_key(text, key):
                    """Extract a JSON object containing the given key, handling nested braces."""
                    idx = text.find(key)
                    if idx == -1:
                        return None
                    # Walk backwards to find opening brace
                    start = text.rfind('{', 0, idx)
                    if start == -1:
                        return None
                    # Walk forward counting braces to find matching close
                    depth = 0
                    for i in range(start, len(text)):
                        if text[i] == '{':
                            depth += 1
                        elif text[i] == '}':
                            depth -= 1
                            if depth == 0:
                                return text[start:i+1]
                    return None

                # Try inside code block first, then raw text
                json_str = None
                code_match = _re.search(r'```(?:json)?\s*(\{.*?"PROPOSED_RUN".*?\})\s*```', full_response, _re.DOTALL)
                if code_match:
                    json_str = extract_json_with_key(code_match.group(1), "PROPOSED_RUN")
                if not json_str:
                    json_str = extract_json_with_key(full_response, "PROPOSED_RUN")

                if json_str:
                    try:
                        proposal_data = json.loads(json_str)
                        if proposal_data.get("PROPOSED_RUN"):
                            proposed_run = {
                                "workflow_name": proposal_data.get("workflow_name", ""),
                                "title": proposal_data.get("title", ""),
                                "task_description": proposal_data.get("task_description", ""),
                                "model": proposal_data.get("model", "haiku"),
                                "auto_approve": proposal_data.get("auto_approve", False),
                            }
                            # Match workflow
                            for w in workflows:
                                if w.get("name", "").lower() == proposed_run["workflow_name"].lower():
                                    proposed_run["workflow_id"] = w["id"]
                                    proposed_run["workflow"] = w
                                    break
                            signal.proposed_run = json.dumps(proposed_run)
                            # Strip JSON (and surrounding code block) from display text
                            if code_match:
                                full_response = full_response.replace(code_match.group(), "").strip()
                            else:
                                full_response = full_response.replace(json_str, "").strip()
                    except (json.JSONDecodeError, KeyError):
                        pass

            # Force tool call if user asked for a run but AI only generated text
            if not proposed_run:
                last_user_msg = ""
                for msg in reversed(api_messages):
                    if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                        last_user_msg = msg["content"].lower()
                        break
                user_wants_run = any(p in last_user_msg for p in [
                    "create a run", "start a run", "make a run", "prepare a run",
                    "create run", "start run", "run please", "let's run",
                    "let's create", "let's start", "yes, let",
                ])
                ai_described_run = full_response and any(p in full_response.lower() for p in [
                    "run created", "run is ready", "proposed run", "here's the run",
                    "run has been", "run queued", "ready for you to approve",
                ])
                if user_wants_run or ai_described_run:
                    try:
                        files_hint = signal.get_files_hint()[:10]
                        wf_names = ", ".join(w["name"] for w in workflows)
                        force_resp = self.client.messages.create(
                            model="claude-haiku-4-5-20251001",
                            max_tokens=1024,
                            system=f"You are creating a run for a signal. Title: {signal.title}. Summary: {signal.summary or 'N/A'}. Files: {json.dumps(files_hint)}. Available workflows: {wf_names}.",
                            tools=[t for t in CHAT_TOOLS if t["name"] == "propose_run"],
                            tool_choice={"type": "tool", "name": "propose_run"},
                            messages=[{"role": "user", "content": "Call propose_run now. Use Bug Fix for bugs, New Feature for features. Set model to haiku. Be specific with file paths in task_description."}],
                        )
                        for block in force_resp.content:
                            if block.type == "tool_use" and block.name == "propose_run":
                                proposed_run = block.input
                                wf_name = proposed_run.get("workflow_name", "")
                                matched_wf = next(
                                    (w for w in workflows if w["name"].lower() == wf_name.lower()),
                                    workflows[0] if workflows else None,
                                )
                                if matched_wf:
                                    proposed_run["workflow_id"] = matched_wf["id"]
                                    proposed_run["workflow"] = matched_wf
                                signal.proposed_run = json.dumps(proposed_run)
                                break
                    except Exception as e:
                        print(f"⚠️ Force propose_run failed: {e}")

            if full_response.strip():
                signal.add_chat_message("assistant", full_response)

            # Save tool calls metadata for display
            if tool_calls_log:
                signal.add_chat_message("system", json.dumps({
                    "type": "tool_calls",
                    "calls": tool_calls_log,
                }))

            db.session.commit()

            return {
                "messages": signal.get_chat_messages(),
                "proposed_run": proposed_run,
                "error": None,
            }

        except Exception as e:
            return {
                "messages": signal.get_chat_messages(),
                "proposed_run": None,
                "error": str(e),
            }

    def _build_cli_prompt(self, signal: Signal, user_message: str, workflows: list) -> str:
        """Build a rich prompt for Claude Code CLI mode."""

        # Previous conversation context
        chat_history = signal.get_chat_messages()
        history_block = ""
        for msg in chat_history:
            if msg["role"] == "user":
                history_block += f"\nHuman: {msg['content']}\n"
            elif msg["role"] == "assistant":
                history_block += f"\nAssistant: {msg['content']}\n"

        # Signal context
        raw = signal.get_raw_payload()
        raw_snippet = json.dumps(raw, indent=2)[:2000] if raw and raw != {} else ""

        # Workflow options
        wf_list = ", ".join([f'"{w["name"]}"' for w in workflows])

        prompt = f"""You are triaging a signal (bug, feature, or issue) for an engineering team.

SIGNAL:
  Source: {signal.source}
  Source ID: {signal.source_id or 'N/A'}
  Title: {signal.title}
  Summary: {signal.summary}
  Severity: {signal.severity}
  Files hint: {json.dumps(signal.get_files_hint())}
"""
        if raw_snippet:
            prompt += f"\nRAW PAYLOAD:\n{raw_snippet}\n"

        if history_block:
            prompt += f"\nCONVERSATION SO FAR:{history_block}\n"

        prompt += f"""
YOUR TASK:
1. Investigate the codebase — read the relevant files, check git history, understand the context
2. Present findings as a STRUCTURED REPORT (see format below)
3. Ask targeted questions about anything you can't determine from code alone
4. If you have enough context to recommend a fix, end your response with a JSON proposal block

{f'The user says: {user_message}' if user_message else 'This is the initial triage — investigate and report what you find.'}

OUTPUT FORMAT — STRICTLY FOLLOW THIS:
Do NOT output raw reasoning or stream-of-consciousness. Never write "Let me check...", "Now I will...", "Interesting!".
Transform your investigation into a clean, structured report:

**Summary** — 2-3 sentences: what is the issue, why it matters
**Root Cause** — Clear, direct explanation (3-4 lines max)
**Key Findings** — Bullet points only, one concrete observation each
**Evidence** — Specific files, line numbers, logs (short references, not dumps)
**Recommended Action** — Numbered steps, clear and executable

Use markdown formatting: **bold** for headers, `code` for file paths and functions, bullet points for lists.
Keep it scannable. No filler language. No repetition.

IMPORTANT: If you want to propose a run, include a JSON block at the END of your response in exactly this format:
```json
{{"PROPOSED_RUN": true, "workflow_name": "Bug Fix", "title": "Short title", "task_description": "Detailed description for the engineer", "auto_approve": true, "model": "haiku"}}
```
Available workflows: {wf_list}
ALWAYS use "model": "haiku" unless the user specifically asks for a different model."""

        return prompt

    def _force_propose_run_signal(self, signal, workflows, api_messages) -> dict:
        """Bypass investigation and force a propose_run tool call for a signal."""
        try:
            files_hint = signal.get_files_hint()[:10]
            wf_names = ", ".join(w["name"] for w in workflows)

            prior_analysis = ""
            for msg in reversed(api_messages):
                if msg.get("role") == "assistant" and isinstance(msg.get("content"), str):
                    prior_analysis = msg["content"][:2000]
                    break

            force_system = f"""Create a run proposal for this signal.

SIGNAL: {signal.title}
SUMMARY: {signal.summary or 'See details'}
SEVERITY: {signal.severity}
SOURCE: {signal.source}
FILES: {json.dumps(files_hint)}
AVAILABLE WORKFLOWS: {wf_names}

{f'PRIOR ANALYSIS: {prior_analysis}' if prior_analysis else ''}

Rules:
- Match workflow: bugs→Bug Fix, features→New Feature, refactoring→Code Cleanup, security→Security Audit, tests→Test Coverage
- Set model to "haiku"
- task_description must include specific file paths and what to change"""

            force_resp = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=force_system,
                tools=[t for t in CHAT_TOOLS if t["name"] == "propose_run"],
                tool_choice={"type": "tool", "name": "propose_run"},
                messages=[{"role": "user", "content": "Call propose_run now."}],
            )

            proposed_run = None
            for block in force_resp.content:
                if block.type == "tool_use" and block.name == "propose_run":
                    proposed_run = block.input
                    wf_name = proposed_run.get("workflow_name", "")
                    matched_wf = next(
                        (w for w in workflows if w["name"].lower() == wf_name.lower()),
                        workflows[0] if workflows else None,
                    )
                    if matched_wf:
                        proposed_run["workflow_id"] = matched_wf["id"]
                        proposed_run["workflow"] = matched_wf
                    signal.proposed_run = json.dumps(proposed_run)
                    break

            if proposed_run:
                summary = f"Proposed a **{proposed_run.get('workflow_name', 'Bug Fix')}** run: {proposed_run.get('title', signal.title)}"
                signal.add_chat_message("assistant", summary)
            else:
                signal.add_chat_message("assistant", "I wasn't able to create a run proposal. Please try describing what you'd like fixed.")

            db.session.commit()
            return {
                "messages": signal.get_chat_messages(),
                "proposed_run": proposed_run,
                "error": None,
            }
        except Exception as e:
            return {
                "messages": signal.get_chat_messages(),
                "proposed_run": None,
                "error": f"Failed to create run: {e}",
            }

    def _send_cli(self, signal: Signal, user_message: str) -> dict:
        """CLI mode: use Claude Code subprocess for triage."""
        import re

        workflows = [w.to_dict() for w in Workflow.query.all()]
        prompt = self._build_cli_prompt(signal, user_message, workflows)

        try:
            cmd = ["claude", "-p", prompt, "--output-format", "text"]

            print(f"💬 CLI triage: {signal.title[:50]}...")

            result = subprocess.run(
                cmd, cwd=self.repo_path,
                capture_output=True, text=True,
                timeout=CLI_TIMEOUT,
            )

            output = result.stdout or ""
            if result.stderr:
                output += "\n" + result.stderr

            if result.returncode != 0 and not output.strip():
                return {
                    "messages": signal.get_chat_messages(),
                    "proposed_run": None,
                    "error": f"Claude Code exited with code {result.returncode}",
                }

            # Parse the response — extract any proposed run JSON
            proposed_run = None
            clean_output = output

            # Look for PROPOSED_RUN JSON block
            proposed_run = None
            clean_output = output

            if "PROPOSED_RUN" in output:
                def _extract_json(text, key):
                    idx = text.find(key)
                    if idx == -1:
                        return None, None, None
                    start = text.rfind('{', 0, idx)
                    if start == -1:
                        return None, None, None
                    depth = 0
                    for i in range(start, len(text)):
                        if text[i] == '{': depth += 1
                        elif text[i] == '}':
                            depth -= 1
                            if depth == 0:
                                return text[start:i+1], start, i+1
                    return None, None, None

                # Try inside code block first
                code_match = re.search(r'```(?:json)?\s*(.*?)\s*```', output, re.DOTALL)
                json_str, js_start, js_end = None, None, None
                if code_match and "PROPOSED_RUN" in code_match.group(1):
                    json_str, _, _ = _extract_json(code_match.group(1), "PROPOSED_RUN")
                    if json_str:
                        js_start, js_end = code_match.start(), code_match.end()
                if not json_str:
                    json_str, js_start, js_end = _extract_json(output, "PROPOSED_RUN")

                if json_str:
                    try:
                        proposal = json.loads(json_str)
                        proposal.pop("PROPOSED_RUN", None)
                        proposed_run = proposal

                        # Match workflow
                        wf_name = proposed_run.get("workflow_name", "")
                        matched_wf = next(
                            (w for w in workflows if w["name"].lower() == wf_name.lower()),
                            workflows[0] if workflows else None,
                        )
                        if matched_wf:
                            proposed_run["workflow_id"] = matched_wf["id"]
                            proposed_run["workflow"] = matched_wf

                        signal.proposed_run = json.dumps(proposed_run)
                        signal.status = "ready"

                        # Remove the JSON block from displayed output
                        clean_output = output[:js_start].rstrip()

                    except (json.JSONDecodeError, KeyError):
                        pass  # Not valid JSON, just show as text

            # Save assistant response
            if clean_output.strip():
                signal.add_chat_message("assistant", clean_output.strip())

            # Add a system note that CLI was used
            signal.add_chat_message("system", json.dumps({
                "type": "tool_calls",
                "calls": [{"tool": "claude_code", "input": {"mode": "CLI"}, "result_preview": f"Claude Code analyzed the codebase ({len(output)} chars)"}],
            }))

            db.session.commit()

            return {
                "messages": signal.get_chat_messages(),
                "proposed_run": proposed_run,
                "error": None,
            }

        except subprocess.TimeoutExpired:
            return {
                "messages": signal.get_chat_messages(),
                "proposed_run": None,
                "error": "Claude Code timed out (5 min limit)",
            }
        except FileNotFoundError:
            return {
                "messages": signal.get_chat_messages(),
                "proposed_run": None,
                "error": "Claude Code CLI not found. Install it or set ANTHROPIC_API_KEY for API mode.",
            }
        except Exception as e:
            return {
                "messages": signal.get_chat_messages(),
                "proposed_run": None,
                "error": str(e),
            }

    def _build_cluster_system_prompt(self, cluster: SignalCluster, workflows: list, personas: list) -> str:
        """Build system prompt with aggregated context from all member signals."""

        wf_list = "\n".join([
            f"  - {w['name']} ({w['icon']}): {w.get('description', '')} — stages: {', '.join(s['name'] for s in w.get('stages', []))}"
            for w in workflows
        ])

        persona_list = "\n".join([
            f"  - {p['name']} ({p['icon']}): role={p['role']}, default_model={p['default_model']}"
            for p in personas
        ])

        # Build member signals context
        signals = cluster.signals or []
        signal_lines = []
        for i, s in enumerate(signals[:20], 1):
            signal_lines.append(f"{i}. [{s.source}] {s.title} — {s.summary[:200] if s.summary else 'No summary'}")

        signal_block = "\n".join(signal_lines) if signal_lines else "(no signals)"

        # Get enrichment from highest-severity signal
        enriched_context = ""
        if signals:
            sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
            best = min(signals, key=lambda s: sev_order.get(s.severity, 3))
            enriched_context = self._enrich_signal_context(best)

        # Aggregate files_hint
        all_files = []
        for s in signals:
            all_files.extend(s.get_files_hint())
        files_hint = list(dict.fromkeys(all_files))[:20]  # deduplicate, keep order

        return f"""You are an intelligent engineering assistant that triages and analyzes cases (clusters of related signals/bugs/features) for a software project.

You have access to the full codebase through tools: read_file, list_directory, search_files, run_command.
The project is at: {self.repo_path}

YOUR JOB:
1. READ the full case context below carefully — understand the overall problem across all member signals
2. Investigate using your tools — read relevant files, check git history, understand the codebase context
3. Share what you found as a STRUCTURED REPORT (see format below)
4. Ask targeted questions about things you can't determine from the code alone
5. ONLY propose a run when you have a clear, specific implementation plan

CRITICAL RULES:
- If the user explicitly asks to "create a run", "prepare a run", "start a run", or "make a run" — propose one immediately based on what you know. Do not investigate further.
- IMPORTANT: You MUST call the propose_run tool to create a run. Writing "Run created" or describing a run in text does NOT create one. The user can only see and start a run if you actually call propose_run.
- Investigate first, but BE DECISIVE. Once you understand the problem and know which files are involved, propose a run.
- When you DO propose a run, the task_description must be extremely specific with exact file paths, function names, and what to change.
- Always set model to 'haiku' unless the user specifically requests a different model.
- ALWAYS end your response with either: (a) a concrete run proposal via the propose_run tool, or (b) a clear summary of findings + specific questions if you need more info.

OUTPUT FORMAT — STRICTLY FOLLOW THIS:
Do NOT output raw reasoning or stream-of-consciousness. Never write "Let me check...", "Now I will...", "Interesting!".
Transform your investigation into a clean, structured report:

**Summary** — 2-3 sentences: what is the issue, why it matters
**Root Cause** — Clear, direct explanation (3-4 lines max)
**Key Findings** — Bullet points only, one concrete observation each
**Evidence** — Specific files, line numbers, logs (short references, not dumps)
**Recommended Action** — Numbered steps, clear and executable
**Risks / Notes** — Only if relevant, keep concise

Use markdown formatting: **bold** for headers, `code` for file paths and functions, bullet points for lists.
Keep it scannable. No filler language. No repetition. Write like a senior engineer filing a concise report.

CASE BEING INVESTIGATED:
CASE TITLE: {cluster.title}
CASE SUMMARY: {cluster.summary or 'Not yet triaged'}
ROOT CAUSE: {cluster.root_cause or 'Not yet identified'}
SEVERITY: {cluster.severity}
STATUS: {cluster.status}
FILES: {json.dumps(files_hint)}

MEMBER SIGNALS ({len(signals)} total):
{signal_block}
{enriched_context}

AVAILABLE WORKFLOWS:
{wf_list}

AVAILABLE PERSONAS:
{persona_list}

When using propose_run, match the workflow to the task type:
- Bugs / regressions → Bug Fix
- Code quality / refactoring → Code Cleanup
- New functionality → New Feature
- Security concerns → Security Audit
- Missing tests → Test Coverage
Always set model to "haiku" unless the user specifically requests a different model.

Keep responses concise but informative. Use code formatting for file paths and code snippets."""

    def send_cluster_message(self, cluster_id: str, user_message: str) -> dict:
        """
        Send a user message in the case/cluster chat, get AI response.
        Similar to send_message but operates on SignalCluster.

        Returns: {
            "messages": [...],
            "proposed_run": {...} or None,
            "error": None or "error message"
        }
        """
        if not self.available():
            return {"messages": [], "proposed_run": None,
                    "error": "No AI backend available. Set ANTHROPIC_API_KEY for API mode, or install Claude Code CLI."}

        cluster = SignalCluster.query.get(cluster_id)
        if not cluster:
            return {"messages": [], "proposed_run": None, "error": "Case not found"}

        # Save user message
        if user_message.strip():
            cluster.add_chat_message("user", user_message)
        db.session.commit()

        # Route to the right backend
        if self.client:
            return self._send_cluster_api(cluster)
        else:
            return self._send_cluster_cli(cluster, user_message)

    def _send_cluster_api(self, cluster: SignalCluster) -> dict:
        """API mode: structured tool use loop for cluster chat."""
        chat_history = cluster.get_chat_messages()
        api_messages = []
        for msg in chat_history:
            if msg["role"] in ("user", "assistant"):
                content = msg["content"]
                if len(content) > 8000:
                    content = content[:4000] + "\n\n[...truncated...]\n\n" + content[-2000:]
                api_messages.append({"role": msg["role"], "content": content})

        if len(api_messages) > 10:
            api_messages = api_messages[:1] + api_messages[-9:]

        if api_messages and api_messages[0]["role"] != "user":
            api_messages.insert(0, {"role": "user", "content": "Continue the investigation."})

        # Remove consecutive same-role messages
        deduped = []
        for msg in api_messages:
            if deduped and deduped[-1]["role"] == msg["role"]:
                deduped[-1]["content"] += "\n\n" + msg["content"]
            else:
                deduped.append(msg)
        api_messages = deduped

        if not api_messages:
            api_messages = [{"role": "user", "content": "Analyze this case and tell me what you find. Use your tools to investigate the codebase."}]

        workflows = [w.to_dict() for w in Workflow.query.all()]
        personas = [p.to_dict() for p in Persona.query.all()]
        system_prompt = self._build_cluster_system_prompt(cluster, workflows, personas)

        # Fast path: if user explicitly asks for a run, force propose_run directly
        last_user_msg = ""
        for msg in reversed(api_messages):
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                last_user_msg = msg["content"].lower()
                break
        _run_phrases = [
            "create a run", "start a run", "make a run", "prepare a run",
            "create run", "start run", "run please", "suggest a run",
            "let's create", "let's start", "let's run", "yes, let",
            "yes please", "yes, create", "go ahead", "do it",
        ]
        if any(p in last_user_msg for p in _run_phrases):
            return self._force_propose_run_cluster(cluster, workflows, api_messages, system_prompt)

        proposed_run = None
        all_text_parts = []
        tool_calls_log = []
        turns = 0
        max_turns = 25

        try:
            while turns < max_turns:
                turns += 1

                if turns == max_turns - 1:
                    response = self.client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=4096,
                        system=system_prompt + "\n\nFINAL TURN: Summarize your findings and use propose_run if you have enough context. Do NOT make any more tool calls.",
                        tools=CHAT_TOOLS,
                        messages=api_messages,
                    )
                    assistant_content = []
                    text_parts = []
                    for block in response.content:
                        if block.type == "text":
                            text_parts.append(block.text)
                            assistant_content.append({"type": "text", "text": block.text})
                        elif block.type == "tool_use":
                            assistant_content.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
                            if block.name == "propose_run":
                                proposed_run = block.input
                                wf_name = proposed_run.get("workflow_name", "")
                                matched_wf = next((w for w in workflows if w["name"].lower() == wf_name.lower()), workflows[0] if workflows else None)
                                if matched_wf:
                                    proposed_run["workflow_id"] = matched_wf["id"]
                                    proposed_run["workflow"] = matched_wf
                                cluster.proposed_run = json.dumps(proposed_run)
                                cluster.status = "ready"
                    api_messages.append({"role": "assistant", "content": assistant_content})
                    all_text_parts.extend(text_parts)
                    break

                response = self.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=4096,
                    system=system_prompt,
                    tools=CHAT_TOOLS,
                    messages=api_messages,
                )

                assistant_content = []
                text_parts = []
                tool_use_blocks = []

                for block in response.content:
                    if block.type == "text":
                        text_parts.append(block.text)
                        assistant_content.append({"type": "text", "text": block.text})
                    elif block.type == "tool_use":
                        tool_use_blocks.append(block)
                        assistant_content.append({
                            "type": "tool_use", "id": block.id,
                            "name": block.name, "input": block.input,
                        })

                api_messages.append({"role": "assistant", "content": assistant_content})
                all_text_parts.extend(text_parts)

                if tool_use_blocks:
                    tool_results = []
                    for tool_block in tool_use_blocks:
                        result = self._execute_tool(tool_block.name, tool_block.input)
                        tool_calls_log.append({
                            "tool": tool_block.name,
                            "input": tool_block.input,
                            "result_preview": result[:200] if tool_block.name != "propose_run" else result,
                        })

                        if tool_block.name == "propose_run":
                            proposed_run = tool_block.input
                            wf_name = proposed_run.get("workflow_name", "")
                            matched_wf = next(
                                (w for w in workflows if w["name"].lower() == wf_name.lower()),
                                workflows[0] if workflows else None,
                            )
                            if matched_wf:
                                proposed_run["workflow_id"] = matched_wf["id"]
                                proposed_run["workflow"] = matched_wf
                            cluster.proposed_run = json.dumps(proposed_run)
                            cluster.status = "ready"
                            result = "Run proposal saved. The user can now approve or modify it."

                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_block.id,
                            "content": result,
                        })

                    api_messages.append({"role": "user", "content": tool_results})

                if not tool_use_blocks or response.stop_reason == "end_turn":
                    break

            full_response = "\n\n".join(all_text_parts)

            # Safety net: detect if a run should have been proposed but wasn't
            if not proposed_run:
                last_user_msg = ""
                for msg in reversed(api_messages):
                    if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                        last_user_msg = msg["content"].lower()
                        break
                user_wants_run = any(p in last_user_msg for p in [
                    "create a run", "start a run", "make a run", "prepare a run",
                    "create run", "start run", "run please", "let's run",
                    "let's create", "let's start", "yes, let",
                ])
                ai_described_run = full_response and any(p in full_response.lower() for p in [
                    "run created", "run is ready", "proposed run", "here's the run",
                    "run has been", "run queued", "ready for you to approve",
                ])
                if user_wants_run or ai_described_run:
                    try:
                        files_hint = cluster.get_files_hint()[:10]
                        wf_names = ", ".join(w["name"] for w in workflows)
                        force_resp = self.client.messages.create(
                            model="claude-haiku-4-5-20251001",
                            max_tokens=1024,
                            system=f"You are creating a run for a case. Title: {cluster.title}. Summary: {cluster.summary or 'N/A'}. Root cause: {cluster.root_cause or 'N/A'}. Files: {json.dumps(files_hint)}. Available workflows: {wf_names}.",
                            tools=[t for t in CHAT_TOOLS if t["name"] == "propose_run"],
                            tool_choice={"type": "tool", "name": "propose_run"},
                            messages=[{"role": "user", "content": "Call propose_run now. Use Bug Fix for bugs, New Feature for features, Code Cleanup for refactoring. Set model to haiku. Make the task_description specific with file paths."}],
                        )
                        for block in force_resp.content:
                            if block.type == "tool_use" and block.name == "propose_run":
                                proposed_run = block.input
                                wf_name = proposed_run.get("workflow_name", "")
                                matched_wf = next(
                                    (w for w in workflows if w["name"].lower() == wf_name.lower()),
                                    workflows[0] if workflows else None,
                                )
                                if matched_wf:
                                    proposed_run["workflow_id"] = matched_wf["id"]
                                    proposed_run["workflow"] = matched_wf
                                cluster.proposed_run = json.dumps(proposed_run)
                                cluster.status = "ready"
                                break
                    except Exception as e:
                        print(f"⚠️ Force propose_run failed: {e}")

            if full_response.strip():
                cluster.add_chat_message("assistant", full_response)

            if tool_calls_log:
                cluster.add_chat_message("system", json.dumps({
                    "type": "tool_calls",
                    "calls": tool_calls_log,
                }))

            db.session.commit()

            return {
                "messages": cluster.get_chat_messages(),
                "proposed_run": proposed_run,
                "error": None,
            }

        except Exception as e:
            return {
                "messages": cluster.get_chat_messages(),
                "proposed_run": None,
                "error": str(e),
            }

    def _force_propose_run_cluster(self, cluster, workflows, api_messages, system_prompt) -> dict:
        """Bypass the investigation loop and force a propose_run tool call."""
        try:
            files_hint = cluster.get_files_hint()[:10]
            wf_names = ", ".join(w["name"] for w in workflows)

            # Build context from cluster + any prior assistant analysis
            prior_analysis = ""
            for msg in reversed(api_messages):
                if msg.get("role") == "assistant" and isinstance(msg.get("content"), str):
                    prior_analysis = msg["content"][:2000]
                    break

            force_system = f"""Create a run proposal for this engineering case.

CASE: {cluster.title}
SUMMARY: {cluster.summary or 'See signals below'}
ROOT CAUSE: {cluster.root_cause or 'To be investigated'}
FILES: {json.dumps(files_hint)}
AVAILABLE WORKFLOWS: {wf_names}

{f'PRIOR ANALYSIS: {prior_analysis}' if prior_analysis else ''}

Rules:
- Match workflow to type: bugs→Bug Fix, features→New Feature, refactoring→Code Cleanup, security→Security Audit, tests→Test Coverage
- Set model to "haiku"
- task_description must be specific with file paths and what to change"""

            force_resp = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=force_system,
                tools=[t for t in CHAT_TOOLS if t["name"] == "propose_run"],
                tool_choice={"type": "tool", "name": "propose_run"},
                messages=[{"role": "user", "content": "Call propose_run now."}],
            )

            proposed_run = None
            for block in force_resp.content:
                if block.type == "tool_use" and block.name == "propose_run":
                    proposed_run = block.input
                    wf_name = proposed_run.get("workflow_name", "")
                    matched_wf = next(
                        (w for w in workflows if w["name"].lower() == wf_name.lower()),
                        workflows[0] if workflows else None,
                    )
                    if matched_wf:
                        proposed_run["workflow_id"] = matched_wf["id"]
                        proposed_run["workflow"] = matched_wf
                    cluster.proposed_run = json.dumps(proposed_run)
                    cluster.status = "ready"
                    break

            if proposed_run:
                summary = f"Proposed a **{proposed_run.get('workflow_name', 'Bug Fix')}** run: {proposed_run.get('title', cluster.title)}"
                cluster.add_chat_message("assistant", summary)
            else:
                cluster.add_chat_message("assistant", "I wasn't able to create a run proposal. Please try describing what you'd like fixed.")

            db.session.commit()
            return {
                "messages": cluster.get_chat_messages(),
                "proposed_run": proposed_run,
                "error": None,
            }
        except Exception as e:
            return {
                "messages": cluster.get_chat_messages(),
                "proposed_run": None,
                "error": f"Failed to create run: {e}",
            }

    def _send_cluster_cli(self, cluster: SignalCluster, user_message: str) -> dict:
        """CLI mode: use Claude Code subprocess for cluster chat."""
        import re

        workflows = [w.to_dict() for w in Workflow.query.all()]
        signals = cluster.signals or []

        signal_block = "\n".join([
            f"  {i}. [{s.source}] {s.title} — {(s.summary or '')[:200]}"
            for i, s in enumerate(signals[:20], 1)
        ]) or "(no signals)"

        wf_list = ", ".join([f'"{w["name"]}"' for w in workflows])

        # Previous conversation context
        chat_history = cluster.get_chat_messages()
        history_block = ""
        for msg in chat_history:
            if msg["role"] == "user":
                history_block += f"\nHuman: {msg['content']}\n"
            elif msg["role"] == "assistant":
                history_block += f"\nAssistant: {msg['content']}\n"

        prompt = f"""You are triaging a case (cluster of related signals) for an engineering team.

CASE:
  Title: {cluster.title}
  Summary: {cluster.summary or 'Not yet triaged'}
  Root Cause: {cluster.root_cause or 'Not identified'}
  Severity: {cluster.severity}
  Status: {cluster.status}

MEMBER SIGNALS ({len(signals)} total):
{signal_block}
"""
        if history_block:
            prompt += f"\nCONVERSATION SO FAR:{history_block}\n"

        prompt += f"""
YOUR TASK:
1. Investigate the codebase — read the relevant files, check git history, understand the context
2. Present findings as a STRUCTURED REPORT (see format below)
3. Ask targeted questions about anything you can't determine from code alone
4. If you have enough context to recommend a fix, end your response with a JSON proposal block

{f'The user says: {user_message}' if user_message else 'This is the initial investigation — investigate and report what you find.'}

OUTPUT FORMAT — STRICTLY FOLLOW THIS:
Do NOT output raw reasoning or stream-of-consciousness. Never write "Let me check...", "Now I will...", "Interesting!".
Transform your investigation into a clean, structured report:

**Summary** — 2-3 sentences: what is the issue, why it matters
**Root Cause** — Clear, direct explanation (3-4 lines max)
**Key Findings** — Bullet points only, one concrete observation each
**Evidence** — Specific files, line numbers, logs (short references, not dumps)
**Recommended Action** — Numbered steps, clear and executable

Use markdown formatting: **bold** for headers, `code` for file paths and functions, bullet points for lists.
Keep it scannable. No filler language. No repetition.

IMPORTANT: If you want to propose a run, include a JSON block at the END of your response in exactly this format:
```json
{{"PROPOSED_RUN": true, "workflow_name": "Bug Fix", "title": "Short title", "task_description": "Detailed description for the engineer", "auto_approve": true, "model": "haiku"}}
```
Available workflows: {wf_list}
ALWAYS use "model": "haiku" unless the user specifically asks for a different model."""

        try:
            cmd = ["claude", "-p", prompt, "--output-format", "text"]
            print(f"💬 CLI cluster triage: {cluster.title[:50]}...")

            result = subprocess.run(
                cmd, cwd=self.repo_path,
                capture_output=True, text=True,
                timeout=CLI_TIMEOUT,
            )

            output = result.stdout or ""
            if result.stderr:
                output += "\n" + result.stderr

            if result.returncode != 0 and not output.strip():
                return {
                    "messages": cluster.get_chat_messages(),
                    "proposed_run": None,
                    "error": f"Claude Code exited with code {result.returncode}",
                }

            proposed_run = None
            clean_output = output

            if "PROPOSED_RUN" in output:
                def _extract_json(text, key):
                    idx = text.find(key)
                    if idx == -1:
                        return None, None, None
                    start = text.rfind('{', 0, idx)
                    if start == -1:
                        return None, None, None
                    depth = 0
                    for i in range(start, len(text)):
                        if text[i] == '{': depth += 1
                        elif text[i] == '}':
                            depth -= 1
                            if depth == 0:
                                return text[start:i+1], start, i+1
                    return None, None, None

                code_match = re.search(r'```(?:json)?\s*(.*?)\s*```', output, re.DOTALL)
                json_str, js_start, js_end = None, None, None
                if code_match and "PROPOSED_RUN" in code_match.group(1):
                    json_str, _, _ = _extract_json(code_match.group(1), "PROPOSED_RUN")
                    if json_str:
                        js_start, js_end = code_match.start(), code_match.end()
                if not json_str:
                    json_str, js_start, js_end = _extract_json(output, "PROPOSED_RUN")

                if json_str:
                    try:
                        proposal = json.loads(json_str)
                        proposal.pop("PROPOSED_RUN", None)
                        proposed_run = proposal

                        wf_name = proposed_run.get("workflow_name", "")
                        matched_wf = next(
                            (w for w in workflows if w["name"].lower() == wf_name.lower()),
                            workflows[0] if workflows else None,
                        )
                        if matched_wf:
                            proposed_run["workflow_id"] = matched_wf["id"]
                            proposed_run["workflow"] = matched_wf

                        cluster.proposed_run = json.dumps(proposed_run)
                        cluster.status = "ready"

                        clean_output = output[:js_start].rstrip()
                    except (json.JSONDecodeError, KeyError):
                        pass

            if clean_output.strip():
                cluster.add_chat_message("assistant", clean_output.strip())

            cluster.add_chat_message("system", json.dumps({
                "type": "tool_calls",
                "calls": [{"tool": "claude_code", "input": {"mode": "CLI"}, "result_preview": f"Claude Code analyzed the codebase ({len(output)} chars)"}],
            }))

            db.session.commit()

            return {
                "messages": cluster.get_chat_messages(),
                "proposed_run": proposed_run,
                "error": None,
            }

        except subprocess.TimeoutExpired:
            return {
                "messages": cluster.get_chat_messages(),
                "proposed_run": None,
                "error": "Claude Code timed out (5 min limit)",
            }
        except FileNotFoundError:
            return {
                "messages": cluster.get_chat_messages(),
                "proposed_run": None,
                "error": "Claude Code CLI not found. Install it or set ANTHROPIC_API_KEY for API mode.",
            }
        except Exception as e:
            return {
                "messages": cluster.get_chat_messages(),
                "proposed_run": None,
                "error": str(e),
            }

    def auto_triage(self, signal_id: str) -> dict:
        """Trigger automatic triage — AI investigates the signal without user input."""
        return self.send_message(signal_id, "")
