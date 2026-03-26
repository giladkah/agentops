"""
Token Optimizer — Compresses tool outputs, summarizes message history,
extracts structured handoffs between agents, and builds review round summaries.

Goal: ~60-70% token reduction across the plan→engineer→review workflow.
"""
import json
import re
import subprocess
import os
from typing import Optional


# ── Settings check ──

def is_optimization_enabled() -> bool:
    """Check if token optimization is enabled in settings."""
    try:
        from models import Setting
        return Setting.get("token_optimization", "enabled") != "disabled"
    except Exception:
        return True


# ═══════════════════════════════════════════════════════════════════
# 1. Tool Output Compression
# ═══════════════════════════════════════════════════════════════════

def compress_tool_output(tool_name: str, tool_input: dict, result: str, agent_role: str = None) -> str:
    """Dispatch to role-aware compression for a tool result."""
    if not is_optimization_enabled():
        return result

    if tool_name == "read_file":
        return compress_read_file(result, tool_input.get("path", ""), agent_role)
    elif tool_name == "run_command":
        return compress_run_command(result, tool_input.get("command", ""))
    elif tool_name == "search_files":
        return compress_search_files(result)
    # write_file, edit_file, list_directory — pass through (already small)
    return result


def compress_read_file(content: str, file_path: str, agent_role: str = None) -> str:
    """Compress file read output based on agent role."""
    if content.startswith("Error") or content.startswith("File not found"):
        return content

    size = len(content)

    if agent_role == "planner":
        # Planner only needs structural skeleton
        return _extract_skeleton(content, file_path, max_chars=8_000)

    if agent_role in ("reviewer", "security", "architect-reviewer"):
        # Reviewers: cap at 15KB head + 5KB tail for large files
        if size > 10_000:
            return _head_tail(content, head=15_000, tail=5_000)
        return content

    if agent_role == "engineer":
        # Engineer: full content but cap at 30KB
        if size > 30_000:
            return _head_tail(content, head=25_000, tail=5_000)
        return content

    # Default: cap at 30KB
    if size > 30_000:
        return _head_tail(content, head=25_000, tail=5_000)
    return content


def compress_run_command(output: str, command: str) -> str:
    """Compress command output based on what command was run."""
    if not output or len(output) < 500:
        return output

    cmd_lower = command.lower().strip()

    # pytest: keep summary + failed blocks only
    if "pytest" in cmd_lower:
        return _compress_pytest(output)

    # git diff: keep as-is but strip context lines if huge
    if cmd_lower.startswith("git diff"):
        if len(output) > 20_000:
            return _compress_git_diff(output)
        return output

    # git log: cap at 2KB
    if cmd_lower.startswith("git log"):
        if len(output) > 2_000:
            return output[:2_000] + f"\n... [truncated, {len(output):,} chars total]"
        return output

    # Other commands: head 5KB + tail 2KB
    if len(output) > 7_000:
        return _head_tail(output, head=5_000, tail=2_000)

    return output


def compress_search_files(output: str) -> str:
    """Compress search results: group by file, show first 2 matches per file, cap at 30 results."""
    if not output or output.startswith("No matches") or output.startswith("Error"):
        return output

    lines = output.strip().split("\n")
    if len(lines) <= 30:
        return output

    # Group by file
    file_matches: dict[str, list[str]] = {}
    for line in lines:
        # Format: file:line:content or file:content
        colon_idx = line.find(":")
        if colon_idx > 0:
            fpath = line[:colon_idx]
            if fpath not in file_matches:
                file_matches[fpath] = []
            file_matches[fpath].append(line)
        else:
            file_matches.setdefault("_other", []).append(line)

    # Build compressed output: first 2 matches per file, cap at 30 total
    result_lines = []
    total = 0
    for fpath, matches in file_matches.items():
        if total >= 30:
            result_lines.append(f"... and {len(lines) - total} more matches in other files")
            break
        for m in matches[:2]:
            result_lines.append(m)
            total += 1
        if len(matches) > 2:
            result_lines.append(f"  ... and {len(matches) - 2} more in {fpath}")

    return "\n".join(result_lines)


# ── Compression helpers ──

def _head_tail(text: str, head: int = 5_000, tail: int = 2_000) -> str:
    """Keep head and tail of text, truncating the middle."""
    if len(text) <= head + tail + 100:
        return text
    return (
        text[:head]
        + f"\n\n... [{len(text):,} chars total, middle truncated] ...\n\n"
        + text[-tail:]
    )


_STRUCTURE_RE = re.compile(
    r'^(class |def |async def |function |export |import |from .+ import |const |let |var |interface |type |enum )',
    re.MULTILINE,
)


def _extract_skeleton(content: str, file_path: str, max_chars: int = 8_000) -> str:
    """Extract structural skeleton: imports + class/function signatures + docstrings."""
    lines = content.split("\n")
    skeleton_lines = []
    in_docstring = False
    docstring_count = 0

    for line in lines:
        stripped = line.strip()

        # Track docstrings (keep first 3 lines of each)
        if '"""' in stripped or "'''" in stripped:
            if in_docstring:
                in_docstring = False
                skeleton_lines.append(line)
                continue
            else:
                # Single-line docstring
                if stripped.count('"""') >= 2 or stripped.count("'''") >= 2:
                    skeleton_lines.append(line)
                    continue
                in_docstring = True
                docstring_count = 0

        if in_docstring:
            docstring_count += 1
            if docstring_count <= 3:
                skeleton_lines.append(line)
            continue

        # Keep structural lines
        if _STRUCTURE_RE.match(stripped) or stripped.startswith("#") or stripped.startswith("@"):
            skeleton_lines.append(line)

    result = "\n".join(skeleton_lines)
    if len(result) > max_chars:
        result = result[:max_chars] + "\n... [skeleton truncated]"

    header = f"[structural skeleton of {file_path} — {len(content):,} chars, {len(lines)} lines]\n"
    return header + result


def _compress_pytest(output: str) -> str:
    """Keep pytest summary line + FAILED test blocks only."""
    lines = output.split("\n")
    result_lines = []
    in_failure = False

    for line in lines:
        # Keep the summary line (e.g., "12 passed, 2 failed in 3.45s")
        if re.search(r'\d+ passed|PASSED|FAILED|ERROR', line) and ('passed' in line or 'failed' in line or 'error' in line):
            result_lines.append(line)
            continue

        # Keep FAILURES section
        if line.strip().startswith("FAILURES") or line.strip().startswith("=== FAILURES"):
            in_failure = True
            result_lines.append(line)
            continue

        if in_failure:
            # End of failures section
            if line.startswith("=") and "short test summary" in line.lower():
                in_failure = False
                result_lines.append(line)
                continue
            result_lines.append(line)
            continue

        # Keep short test summary section
        if "short test summary" in line.lower():
            result_lines.append(line)
            in_failure = True  # reuse flag to capture summary lines
            continue

        # Keep exit code
        if "[exit code:" in line:
            result_lines.append(line)

    result = "\n".join(result_lines)

    # Collapse repeated error patterns
    result = _collapse_repeated_errors(result)

    if len(result) > 5_000:
        result = _head_tail(result, head=4_000, tail=1_000)

    if not result.strip():
        # Fallback: just keep first 2KB + last 1KB
        return _head_tail(output, head=2_000, tail=1_000)

    return result


def _compress_git_diff(output: str) -> str:
    """Compress large git diff by removing unchanged context lines."""
    lines = output.split("\n")
    result_lines = []
    context_count = 0

    for line in lines:
        if line.startswith("@@"):
            result_lines.append(line)
            context_count = 0
        elif line.startswith("+") or line.startswith("-"):
            result_lines.append(line)
            context_count = 0
        elif line.startswith("diff ") or line.startswith("index ") or line.startswith("---") or line.startswith("+++"):
            result_lines.append(line)
        else:
            # Context line — keep max 2 around changes
            context_count += 1
            if context_count <= 2:
                result_lines.append(line)

    result = "\n".join(result_lines)
    if len(result) > 20_000:
        result = _head_tail(result, head=15_000, tail=5_000)
    return result


def _collapse_repeated_errors(text: str) -> str:
    """Collapse repeated identical error lines."""
    lines = text.split("\n")
    if len(lines) < 10:
        return text

    result = []
    prev_line = None
    repeat_count = 0

    for line in lines:
        if line == prev_line:
            repeat_count += 1
        else:
            if repeat_count > 2:
                result.append(f"  ... (repeated {repeat_count} more times)")
            elif repeat_count > 0:
                for _ in range(repeat_count):
                    result.append(prev_line)
            result.append(line)
            prev_line = line
            repeat_count = 0

    if repeat_count > 2:
        result.append(f"  ... (repeated {repeat_count} more times)")
    elif repeat_count > 0:
        for _ in range(repeat_count):
            result.append(prev_line)

    return "\n".join(result)


# ═══════════════════════════════════════════════════════════════════
# 2. Message History Summarization
# ═══════════════════════════════════════════════════════════════════

def summarize_old_messages(messages: list, keep_last_n: int = 3) -> list:
    """
    Replace old tool results with one-line summaries to prevent linear token growth.

    Keeps:
    - messages[0] (initial prompt) intact
    - Last keep_last_n assistant+user turn pairs intact
    - Replaces older tool_result contents with compressed summaries
    """
    if not is_optimization_enabled():
        return messages

    if len(messages) <= 1 + (keep_last_n * 2):
        return messages  # Not enough messages to compress

    # messages[0] = initial user prompt
    # Then alternating: assistant, user (tool_results), assistant, user, ...
    # Keep first message + last keep_last_n*2 messages
    keep_tail = keep_last_n * 2
    if len(messages) <= 1 + keep_tail:
        return messages

    result = [messages[0]]  # Keep initial prompt

    # Middle messages to compress (between first and last N)
    middle = messages[1:-keep_tail]
    tail = messages[-keep_tail:]

    for msg in middle:
        if msg.get("role") == "user" and isinstance(msg.get("content"), list):
            # This is a tool_results message — compress each result
            compressed_content = []
            for item in msg["content"]:
                if item.get("type") == "tool_result":
                    summary = _summarize_tool_result(item)
                    compressed_content.append({
                        "type": "tool_result",
                        "tool_use_id": item.get("tool_use_id", ""),
                        "content": summary,
                    })
                else:
                    compressed_content.append(item)
            result.append({"role": "user", "content": compressed_content})
        elif msg.get("role") == "assistant" and isinstance(msg.get("content"), list):
            # Assistant message with tool_use blocks — keep structure but compress text
            compressed_content = []
            for block in msg["content"]:
                if hasattr(block, "type"):
                    # This is an API response object — keep as-is (tool_use blocks are small)
                    compressed_content.append(block)
                elif isinstance(block, dict):
                    compressed_content.append(block)
                else:
                    compressed_content.append(block)
            result.append({"role": "assistant", "content": compressed_content})
        else:
            # Keep other messages as-is (they're usually small)
            result.append(msg)

    # Append the tail (recent messages) intact
    result.extend(tail)
    return result


def _summarize_tool_result(item: dict) -> str:
    """Generate a one-line summary for a tool result."""
    content = item.get("content", "")
    if not isinstance(content, str):
        content = str(content)

    # Try to detect what tool produced this
    length = len(content)

    # read_file patterns
    if "structural skeleton of" in content:
        return content[:200]  # Already compressed

    # write_file result
    if content.startswith("Successfully wrote") or content.startswith("Written"):
        return content[:200]

    # edit_file result
    if content.startswith("Edited "):
        return content[:200]

    # run_command with pytest
    if "passed" in content and ("failed" in content or "error" in content or "pytest" in content.lower()):
        # Extract summary line
        for line in content.split("\n"):
            if re.search(r'\d+ passed', line):
                return f"[run_command: pytest — {line.strip()}]"
        return f"[run_command: pytest output — {length:,} chars]"

    # run_command with exit code
    exit_match = re.search(r'\[exit code: (\d+)\]', content)
    if exit_match:
        code = exit_match.group(1)
        # Get the command from context if possible
        return f"[run_command: exit {code} — {length:,} chars]"

    # search_files — count matches
    if content.startswith("No matches"):
        return content

    lines = content.split("\n")
    if length > 500 and len(lines) > 5:
        # Looks like search results or file content
        files_seen = set()
        for line in lines:
            colon = line.find(":")
            if colon > 0:
                files_seen.add(line[:colon])
        if files_seen:
            return f"[search_files: {len(lines)} matches across {len(files_seen)} files]"
        return f"[tool output: {length:,} chars, {len(lines)} lines]"

    # Short result — keep as-is
    if length < 500:
        return content

    # Generic large output
    return f"[tool output: {length:,} chars, {len(lines)} lines — {content[:100]}...]"


# ═══════════════════════════════════════════════════════════════════
# 3. Handoff Extraction
# ═══════════════════════════════════════════════════════════════════

HANDOFF_START = "HANDOFF_JSON_START"
HANDOFF_END = "HANDOFF_JSON_END"


def extract_handoff(output: str, agent_role: str, worktree_path: str = None) -> dict:
    """
    Extract structured handoff from agent output.
    Strategy 1: Look for HANDOFF_JSON_START/END markers.
    Strategy 2: Fallback — parse TASK COMPLETE summary + git diff.
    """
    if not output:
        return {}

    # Strategy 1: marker-based extraction
    handoff = _extract_marker_handoff(output)
    if handoff:
        handoff["_source"] = "marker"
        return handoff

    # Strategy 2: fallback extraction
    return _extract_fallback_handoff(output, agent_role, worktree_path)


def _extract_marker_handoff(output: str) -> Optional[dict]:
    """Extract JSON between HANDOFF_JSON_START and HANDOFF_JSON_END markers."""
    match = re.search(
        rf'{HANDOFF_START}\s*(\{{.*\}})\s*{HANDOFF_END}',
        output,
        re.DOTALL,
    )
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    return None


def _extract_fallback_handoff(output: str, agent_role: str, worktree_path: str = None) -> dict:
    """Build a minimal handoff from TASK COMPLETE summary and git diff."""
    handoff: dict = {"type": agent_role, "_source": "fallback"}

    # Extract summary from TASK COMPLETE line
    tc_match = re.search(r'TASK COMPLETE[:\s]*(.+?)(?:\n|$)', output, re.IGNORECASE)
    if tc_match:
        handoff["summary"] = tc_match.group(1).strip()[:500]
    else:
        # Take last 300 chars as summary
        handoff["summary"] = output[-300:].strip()

    # Get changed files from git if worktree available
    if worktree_path and os.path.isdir(worktree_path):
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD~1"],
                cwd=worktree_path, capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
                handoff["files_changed"] = files[:20]
        except Exception:
            pass

    return handoff


# ═══════════════════════════════════════════════════════════════════
# 4. Review Round Summary
# ═══════════════════════════════════════════════════════════════════

def build_review_round_summary(run) -> str:
    """
    For review round 2+, compress previous round results into ~200-500 tokens.
    `run` is a Run model instance.
    """
    review_history = run.get_review_history()
    if not review_history:
        return ""

    last_round = review_history[-1]
    round_num = last_round.get("round", 0)
    issues_count = last_round.get("issues", 0)

    # Collect reviewer info from agents
    review_agents = [a for a in run.agents if a.stage_name in ("review", "review-quality", "review-security")
                     and a.status in ("done", "converged")]

    reviewer_names = [a.name for a in review_agents]
    all_issues = []
    for a in review_agents:
        structured = a.get_structured_issues()
        for iss in structured:
            all_issues.append(iss)

    critical = [i for i in all_issues if i.get("severity") in ("critical", "high")]
    minor = [i for i in all_issues if i.get("severity") in ("medium", "low")]

    lines = [
        f"## Previous Review Round {round_num}",
        f"Reviewers: {', '.join(reviewer_names) if reviewer_names else 'N/A'}",
        f"Total issues: {issues_count}",
    ]

    if critical:
        fixed_list = "; ".join(f"{i.get('file', '?')}: {i.get('title', '?')}" for i in critical[:5])
        lines.append(f"Critical/high issues ({len(critical)}): {fixed_list}")

    if minor:
        lines.append(f"Minor issues flagged: {len(minor)}")

    # Overall verdict from handoffs if available
    for a in review_agents:
        try:
            h = json.loads(a.handoff_json) if hasattr(a, 'handoff_json') and a.handoff_json else {}
            if h.get("verdict"):
                lines.append(f"Verdict from {a.name}: {h['verdict']}")
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

    return "\n".join(lines)
