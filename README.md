# 🤖 AgentOps — Multi-Agent Workflow Dashboard

A Flask-based dashboard for orchestrating multiple Claude Code agents on your codebase. Define workflows, configure personas with different models, track costs, and manage review convergence — all from one screen.

## What's New (v0.2.2 — Parallel Review Improvements)

- **🔧 Round 2+ fix:** Reviewers in subsequent rounds get unique worktree names (no branch collisions)
- **🔀 Auto-conflict resolution:** When merging parallel reviewer branches, conflicts are auto-resolved (favors incoming)
- **📊 Per-reviewer breakdown:** Convergence panel shows how many issues each reviewer found
- **⚡ Enhanced parallel indicator:** Live progress bar with running/synthesis status
- **🔄 Clean round resets:** Agent output logs cleared between rounds for clarity

### How Parallel Reviews Work

```
Engineer finishes → creates branch
    ↓
Round 1: 3 reviewers launch IN PARALLEL
    ├── 🏗️ Senior Reviewer     → own worktree (quality, patterns)
    ├── 🔒 Security Engineer    → own worktree (auth, injection, leaks)
    └── 🏛️ Architecture Reviewer → own worktree (SOLID, design)
    ↓ all finish
Synthesis: auto-merge all reviewer branches
    ↓
Convergence check: total issues < threshold?
    ├── Yes → open PR
    └── No → Round 2 (parallel again on synthesized result)
```

## Quick Start

```bash
cd agentops
pip install -r requirements.txt
python app.py --repo /path/to/your/project
```

Opens at **http://localhost:5050**

## What It Does

- **Workflows** — Reusable templates: "Code Cleanup", "New Feature", "Bug Fix", etc.
- **Runs** — Execute a workflow on a specific task. Track status, cost, duration.
- **Personas** — Agent personalities: Senior Engineer, Reviewer, Security Engineer, QA.
- **Model selection** — Haiku for planning, Sonnet for implementation, Opus for reviews.
- **Cost tracking** — Per-agent, per-model, per-run cost estimates.
- **Review convergence** — Reviewers loop until issues drop below threshold (default: 3 rounds max).
- **Human checkpoints** — Pipeline pauses for your approval at every key stage.

## Architecture

```
agentops/
  app.py                  # Flask entry point
  models.py               # SQLAlchemy models (Run, Agent, Persona, Workflow, LogEntry)
  seed.py                 # Default personas and workflows
  requirements.txt
  services/
    git_service.py         # Git worktree management
    agent_runner.py        # Claude Code process launcher
    orchestrator.py        # Workflow execution engine
  routes/
    api.py                # REST API endpoints
  templates/
    dashboard.html         # Full dashboard UI (single-page app)
```

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/api/runs` | GET | List all runs |
| `/api/runs` | POST | Create a new run |
| `/api/runs/:id` | GET | Run details with agents |
| `/api/runs/:id/start` | POST | Start a run |
| `/api/runs/:id/approve` | POST | Approve a checkpoint |
| `/api/runs/:id/merge` | POST | Merge all branches |
| `/api/runs/:id/cancel` | POST | Cancel a run |
| `/api/runs/:id/logs` | GET | Log stream |
| `/api/workflows` | GET | List workflows |
| `/api/personas` | GET | List personas |
| `/api/stats` | GET | Cost and activity stats |

## How It Works Under the Hood

1. You pick a **Workflow** and create a **Run** with configured agents
2. AgentOps creates **git worktrees** for each agent
3. Agents run via `claude -p "prompt"` in their worktree
4. At **checkpoints**, the dashboard pauses and waits for your click
5. **Reviewers** loop until issues < threshold (convergence)
6. You click **Merge** → branches merge → worktrees cleaned up

## Configuration

```bash
# Point to your repo
python app.py --repo ~/projects/shield

# Change port
python app.py --port 8080

# Debug mode
python app.py --debug
```

## Requirements

- Python 3.10+
- Git
- Claude Code CLI (`claude` command available in PATH)

## Roadmap

- [x] v0.1: Core pipeline — plan → engineer → review → PR
- [x] v0.2: Drift Detection / Ensemble runs + parallel reviews
- [ ] v0.3: Shortcut integration (pull tickets)
- [ ] v0.4: Sentry integration (pull errors)
- [ ] v0.5: macOS polish (auto-start, notifications)
- [ ] v1.0: Claude API direct (streaming, exact tokens, multi-project)
