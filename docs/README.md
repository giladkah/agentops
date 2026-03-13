# AgentOps Documentation

## Overview

This directory contains architecture and planning documentation for the AgentOps project.

## Key Documents

### 1. **FULFILLMENT_REMOVAL_SUMMARY.md** (Executive Summary)
**Start here.** 2-minute read.

- Quick finding: Item fulfillment feature **does not exist**
- Search results confirming absence
- Recommendations for next steps

### 2. **plan.md** (Detailed Implementation Plan)
**Use this if removal is needed.** 10-minute read.

- Complete removal strategy broken into 6 tasks
- File-by-file change mapping
- Risk analysis and testing strategy
- Execution timeline and dependencies
- Acceptance criteria

**Key sections:**
- Task 1: Database & Models (LOW complexity)
- Task 2: API Routes (LOW complexity)
- Task 3: Service Layer (LOW complexity)
- Task 4: UI/Frontend (MEDIUM complexity)
- Task 5: Tests (LOW complexity)
- Task 6: Documentation (LOW complexity)

### 3. **ARCHITECTURE_REFERENCE.md** (System Architecture)
**Reference guide.** 15-minute read for deep understanding.

- Complete data model hierarchy
- Full API route structure
- Service layer breakdown
- Execution flow diagrams
- Design patterns used
- Technology stack
- File structure map

---

## Quick Navigation

### For Project Managers
1. Read: FULFILLMENT_REMOVAL_SUMMARY.md
2. Reference: plan.md → "Execution Order & Parallelization"
3. Reference: plan.md → "Files Summary"

### For Engineers
1. Read: FULFILLMENT_REMOVAL_SUMMARY.md
2. Read: plan.md (full)
3. Reference: ARCHITECTURE_REFERENCE.md while making changes

### For Architects
1. Read: ARCHITECTURE_REFERENCE.md (full)
2. Reference: plan.md for removal strategy
3. Note: No item fulfillment model currently exists

---

## Key Findings

### ✅ Completed Analysis
- [x] Comprehensive codebase scan for "item fulfillment" references
- [x] Data model analysis (8 models, no Item/ItemFulfillment)
- [x] API route analysis (40+ routes, no fulfillment endpoints)
- [x] Service layer analysis (9 services, no fulfillment logic)
- [x] Frontend analysis (1 HTML file, no fulfillment UI)
- [x] Test directory analysis (none exists, no fulfillment tests)

### 🔍 Search Results
```bash
grep -r "fulfillment" . --include="*.py" --include="*.html" --include="*.md"
# Result: No matches ✅

grep -r "class Item" . --include="*.py"
# Result: No matches ✅
```

### 📊 Current Architecture
```
Signal (universal backlog item from GitHub/Shortcut/Sentry)
    ↓
Chat/Investigation (AI analyzes signal)
    ↓
Run (workflow execution with agents)
    ↓
Agents (personas: Engineer, Reviewer, Security, QA, Consensus)
    ↓
Review Loop (convergence threshold)
    ↓
Consensus (synthesize N parallel runs)
    ↓
PR (create and merge pull request)
```

---

## Database Schema

### Core Tables
| Table | Purpose | Key Fields |
|-------|---------|-----------|
| signals | Incoming issues from external sources | source, title, status, proposed_run |
| runs | Workflow executions | workflow_id, status, total_cost |
| agents | Individual task executors | run_id, persona_id, stage_name, status |
| workflows | Pipeline definitions | name, stages_config, convergence_threshold |
| personas | AI agent templates | role, prompt_template, default_model |
| ensembles | N parallel runs with synthesis | num_runs, run_ids, consensus_run_id |
| log_entries | Execution logs | run_id, agent_id, message, level |
| settings | Configuration key-value store | key, value |

**Missing:** No Item or ItemFulfillment table ✅

---

## API Summary

### 8 Major Route Groups (40+ endpoints)
- **Signals** (10 endpoints) - CRUD, chat, triage, create-run
- **Runs** (6 endpoints) - CRUD, lifecycle, logs
- **Agents** (3 endpoints) - Get, stop, stream
- **Workflows** (3 endpoints) - List, get, create
- **Personas** (3 endpoints) - List, get, create
- **Ensembles** (5 endpoints) - CRUD, lifecycle
- **Integrations** (15+ endpoints) - GitHub, Shortcut, Sentry poller endpoints
- **Settings** (5 endpoints) - Config, telemetry, stats

**Missing:** No /items or /fulfillment routes ✅

---

## Services (9 Total)

| Service | Purpose | Methods |
|---------|---------|---------|
| orchestrator | Run lifecycle | create_run, start, approve, merge, cancel |
| ensemble | Ensemble coordination | create, start, approve, finalize |
| agent_runner | Agent execution | run_agent, stop, stream, status |
| chat_service | Signal chat & AI triage | send_message, auto_triage |
| git_service | Git operations | create_worktree, commit, create_pr |
| github_poller | GitHub polling | poll, fetch_issues, import |
| shortcut_poller | Shortcut polling | poll, fetch_stories, import |
| sentry_poller | Sentry polling | poll, fetch_issues, import |
| telemetry | Usage tracking | track_*, is_enabled, set_enabled |

**Missing:** No fulfillment service ✅

---

## No Code Changes Required

The codebase is clean. No item fulfillment code exists to remove.

### If Changes Are Needed Later
Refer to **plan.md** for the complete removal strategy.

### If You're Adding Item Fulfillment
**Don't.** This system is designed around Signals → Runs → PRs.

---

## Technology Stack

```
Frontend:      Vanilla HTML/CSS/JS (no framework)
Backend:       Flask 3.0+
ORM:           Flask-SQLAlchemy 3.1+
Database:      SQLite
LLM:           Anthropic Claude API (or local CLI)
Desktop:       rumps (macOS menubar)
HTTP:          requests 2.31.0+
```

---

## Testing

**No tests currently exist in the repository.**

If removal work is done, add tests following this structure:
```python
tests/
├── test_models.py          # Model CRUD
├── test_api.py             # Route handlers
├── test_services.py        # Service logic
└── test_integration.py     # End-to-end flows
```

---

## References

- **Main code**: models.py, app.py, routes/api.py
- **Services**: services/*.py
- **UI**: templates/dashboard.html
- **Configuration**: seed.py (default personas/workflows)
- **Dependencies**: requirements.txt

---

## Questions?

Refer to the detailed documents:
1. **What's in the system?** → ARCHITECTURE_REFERENCE.md
2. **How do I remove fulfillment?** → plan.md
3. **Was it ever implemented?** → FULFILLMENT_REMOVAL_SUMMARY.md

---

**Last Updated:** 2024
**Codebase Version:** v0.7
**Status:** ✅ Item fulfillment removed (never existed)
