# AgentOps Architecture Reference

## System Overview

AgentOps is a **multi-agent workflow orchestration system** where independent AI agents collaboratively work on engineering tasks:

```
Signal (Issue) → Chat/Triage → Run → [Agents] → Review Loop → Consensus → PR
```

## Data Model Hierarchy

```
Setting
  ├─ key: String (PK)
  └─ value: String

Persona
  ├─ id: String (PK)
  ├─ name, role, icon, color
  └─ prompt_template, traits, default_model

Workflow
  ├─ id: String (PK)
  ├─ name, icon, description
  ├─ stages_config: JSON
  └─ convergence_threshold, max_review_rounds

Signal
  ├─ id: String (PK)
  ├─ source: "sentry|shortcut|github|manual"
  ├─ title, summary, severity
  ├─ status: "new|triaging|ready|investigating|running|done|skipped"
  ├─ chat_messages: JSON
  ├─ proposed_run: JSON (AI-generated run config)
  ├─ run_id: FK → Run (when activated)
  └─ raw_payload: JSON (preserved for inspection)

Run
  ├─ id: String (PK)
  ├─ workflow_id: FK → Workflow
  ├─ title, task_description, target_branch
  ├─ status: "pending|running|needs_approval|converged|merged|failed"
  ├─ current_stage_index, review_round, review_history: JSON
  ├─ total_cost, total_tokens_in, total_tokens_out
  ├─ auto_approve: Boolean
  ├─ ensemble_id: FK → EnsembleRun (if part of ensemble)
  └─ agents: Relationship → Agent

Agent
  ├─ id: String (PK)
  ├─ run_id: FK → Run
  ├─ persona_id: FK → Persona
  ├─ name, model, stage_name
  ├─ status: "waiting|running|done|failed|converged"
  ├─ worktree_path: String (git worktree)
  ├─ task_prompt, output_log
  ├─ issues_found, tokens_in, tokens_out, cost
  └─ started_at, finished_at: DateTime

LogEntry
  ├─ id: Integer (PK)
  ├─ run_id: FK → Run
  ├─ agent_id: FK → Agent
  ├─ level: "info|success|warning|error"
  ├─ message, timestamp
  └─ agent_name

EnsembleRun
  ├─ id: String (PK)
  ├─ title, task_description
  ├─ workflow_id: FK → Workflow
  ├─ num_runs, status: "pending|running|comparing|synthesizing|reviewing|done|failed"
  ├─ run_ids: JSON (array of Run IDs)
  ├─ consensus_run_id, review_run_id: FK → Run
  ├─ comparison_data: JSON (diff analysis)
  ├─ auto_approve, individual_prs: Boolean
  └─ started_at, finished_at: DateTime

Ensemble (Legacy)
  ├─ id: String (PK)
  ├─ (Similar to EnsembleRun)
  └─ Note: EnsembleRun is the preferred model
```

## API Route Structure

### Signal Management
```
GET    /api/signals                    # List with pagination + filters
GET    /api/signals/<id>               # Get single signal
POST   /api/signals                    # Create manual signal
DELETE /api/signals/<id>               # Delete signal
GET    /api/signals/counts             # Count by source
POST   /api/signals/<id>/skip          # Mark as skipped
POST   /api/signals/<id>/link-run      # Link to existing run
POST   /api/signals/<id>/create-run    # Create + start run from signal
POST   /api/signals/<id>/chat          # Chat in signal conversation
POST   /api/signals/<id>/auto-triage   # Trigger AI investigation
GET    /api/signals/triage-status      # Poll investigation status
```

### Run Management
```
GET    /api/runs                       # List runs (50 newest)
GET    /api/runs/<id>                  # Get run details
POST   /api/runs                       # Create run
POST   /api/runs/<id>/start            # Start execution
POST   /api/runs/<id>/approve          # Approve checkpoint
POST   /api/runs/<id>/merge            # Merge PR
POST   /api/runs/<id>/cancel           # Cancel run
GET    /api/runs/<id>/logs             # Stream logs
POST   /api/runs/clear                 # Clear all runs (ADMIN)
```

### Agent Management
```
GET    /api/agents/<id>                # Get agent state
POST   /api/agents/<id>/stop           # Stop running agent
GET    /api/agents/<id>/stream         # SSE: Stream live events
```

### Workflow & Persona
```
GET    /api/workflows                  # List workflows
GET    /api/workflows/<id>             # Get workflow
POST   /api/workflows                  # Create workflow
GET    /api/personas                   # List personas
GET    /api/personas/<id>              # Get persona
POST   /api/personas                   # Create persona
```

### Ensemble Management
```
GET    /api/ensembles                  # List ensembles
GET    /api/ensembles/<id>             # Get ensemble with sub-runs
POST   /api/ensembles                  # Create ensemble
POST   /api/ensembles/<id>/start       # Start ensemble
POST   /api/ensembles/<id>/approve     # Approve consensus
POST   /api/ensembles/<id>/pr          # Create PR from consensus
```

### Integrations
```
# GitHub
GET    /api/github/status              # Poller status
POST   /api/github/repos               # Add repo
DELETE /api/github/repos               # Remove repo
POST   /api/github/poll                # Manual poll
POST   /api/github/import              # Bulk import
GET    /api/github/issue/<owner>/<repo>/<number>  # Issue details

# Shortcut
GET    /api/shortcut/status            # Poller status
POST   /api/shortcut/connect           # Connect workspace
POST   /api/shortcut/disconnect        # Disconnect workspace
POST   /api/shortcut/query             # Update search query
POST   /api/shortcut/poll              # Manual poll
POST   /api/shortcut/import            # Bulk import
GET    /api/shortcut/story/<id>        # Story details

# Sentry
GET    /api/sentry/status              # Poller status
POST   /api/sentry/connect             # Connect project
POST   /api/sentry/disconnect          # Disconnect project
GET    /api/sentry/orgs                # List organizations
GET    /api/sentry/projects/<org>      # List projects
POST   /api/sentry/poll                # Manual poll
POST   /api/sentry/import              # Bulk import
GET    /api/sentry/issue/<id>          # Issue details

# Webhooks
POST   /api/signals/sentry             # Sentry webhook
POST   /api/signals/shortcut           # Shortcut webhook
POST   /api/signals/github             # GitHub webhook
```

### Settings & Telemetry
```
GET    /api/settings                   # Get settings + runner status
POST   /api/settings                   # Update settings (mode)
GET    /api/telemetry                  # Get telemetry status
POST   /api/telemetry                  # Set telemetry (on/off)
GET    /api/stats                      # Global stats
GET    /api/debug                      # Debug info
```

## Service Layer

### Core Services

**orchestrator.py** - Run Lifecycle
- `create_run()` - Create and stage a run
- `start_run()` - Begin execution
- `approve_checkpoint()` - Allow progress past review
- `merge_run()` - Create and merge PR
- `cancel_run()` - Stop execution

**ensemble.py** - Ensemble Orchestration
- `create_ensemble()` - Create N parallel runs
- `start_ensemble()` - Begin all runs
- `approve_ensemble()` - Approve consensus
- `_finalize()` - Create final PR

**agent_runner.py** - Agent Execution
- `run_agent()` - Execute single agent (API or CLI mode)
- `stop_agent()` - Kill running agent
- `get_stream_buffer()` - Get live execution events
- Supports both Anthropic API (streaming) and Claude CLI modes

**chat_service.py** - Signal Chat & Auto-Triage
- `send_message()` - Chat with AI about signal
- `auto_triage()` - AI investigation of signal
- Proposes run configuration based on signal

**git_service.py** - Git Operations
- `create_worktree()` - Isolated workspace per agent
- `commit_changes()` - Stage and commit
- `create_pr()` - Open pull request on GitHub
- `cleanup_worktrees()` - Clean up workspaces

### Integration Services

**github_poller.py** - GitHub Issue/PR Monitoring
- Continuous polling for new issues
- Configurable label filtering
- Signal creation on new items

**shortcut_poller.py** - Shortcut Story Monitoring
- Continuous polling for stories
- Configurable search queries
- Signal creation on new stories

**sentry_poller.py** - Sentry Issue Monitoring
- Continuous polling for errors
- Configurable severity filtering
- Signal creation on new issues

**telemetry.py** - Anonymous Usage Tracking
- Run counts, durations, costs
- No code, paths, or task descriptions sent
- User opt-out capability

## Execution Flow Example

### Single Run Flow
```
1. User clicks "New Run" on Workflow
2. orchestrator.create_run()
   - Creates Run record (status: pending)
   - Creates N Agent records (one per stage)
3. orchestrator.start_run()
   - Sets status: running
   - Starts first stage agents
4. For each Stage:
   - agent_runner.run_agent(agent_id)
   - Agent executes (Anthropic API or Claude CLI)
   - Events streamed via SSE
   - Log entries written to DB
5. At "review" stage:
   - Run status: needs_approval
   - Human reviews via dashboard
   - orchestrator.approve_checkpoint()
6. After final stage:
   - orchestrator.merge_run()
   - git_service.create_pr()
   - PR created on GitHub
```

### Ensemble (Drift Detection) Flow
```
1. User selects "3×" parallel runs
2. ensemble_orchestrator.create_ensemble()
   - Creates EnsembleRun record
   - Launches 3 identical Runs
3. All 3 runs execute in parallel
4. After all complete:
   - Compare outputs
   - Synthesize consensus run
   - Human review of consensus
5. orchestrator.merge_run() on consensus
   - Single PR created from synthesis
```

### Signal Processing Flow
```
1. GitHub issue created
   → GitHub webhook triggers
   → POST /api/signals/github
   → Adapter normalizes to Signal
   → Signal status: new

2. User clicks "Investigate" on Signal
   → POST /api/signals/{id}/auto-triage
   → chat_service.auto_triage(signal_id)
   → AI reads signal, analyzes issue
   → Proposes run configuration
   → Signal status: investigated

3. User clicks "Create Run" on Signal
   → POST /api/signals/{id}/create-run
   → orchestrator.create_run() with proposed config
   → Signal.run_id = created_run.id
   → Signal status: running
```

## Key Design Patterns

### 1. Universal Backlog Item
- **Signals** are the central abstraction - normalize all input (GitHub issues, Shortcut stories, Sentry errors, manual)
- Chat interface allows humans to refine signals before automation
- AI can propose run configuration before execution

### 2. Stage-Based Workflow
- Workflows are defined as JSON stage arrays
- Each stage can specify which personas to use
- Convergence threshold allows review loops

### 3. Worktree Isolation
- Each agent gets isolated git worktree
- Prevents conflicts between concurrent agents
- Enables parallel work on same task

### 4. Event Streaming
- Agents stream events in real-time
- SSE endpoint (`/api/agents/{id}/stream`) for live UI updates
- Backend buffers events for late subscribers

### 5. Multi-Modal Execution
- **API Mode**: Uses Anthropic Claude API (streaming, recommended)
- **CLI Mode**: Uses local Claude CLI (requires installation)
- Switchable via settings, not while agents running

---

## Technology Stack

- **Framework**: Flask 3.0+
- **ORM**: Flask-SQLAlchemy 3.1+
- **Database**: SQLite (auto-migrated via ORM)
- **LLM**: Anthropic Claude API (via anthropic 0.40.0+)
- **HTTP**: requests 2.31.0+
- **Frontend**: Vanilla JS + CSS (no framework)
- **Desktop**: rumps (macOS menubar only)

---

## File Structure

```
agentops/
├── models.py                      # SQLAlchemy ORM models
├── app.py                        # Flask app factory
├── routes/
│   └── api.py                    # All REST endpoints (1500+ lines)
├── services/
│   ├── orchestrator.py           # Run orchestration
│   ├── ensemble.py               # Ensemble coordination
│   ├── agent_runner.py           # Agent execution (API/CLI)
│   ├── chat_service.py           # Chat + auto-triage
│   ├── git_service.py            # Git operations
│   ├── github_poller.py          # GitHub polling
│   ├── shortcut_poller.py        # Shortcut polling
│   ├── sentry_poller.py          # Sentry polling
│   └── telemetry.py              # Usage tracking
├── templates/
│   └── dashboard.html            # Main UI (3000+ lines)
├── instance/
│   └── agentops.db               # SQLite database
├── seed.py                       # Default personas + workflows
└── requirements.txt              # Dependencies
```

---

## Notes on Item Fulfillment

**This is NOT an item fulfillment system.** Items here refer to:
- **Signals** (incoming issues/tickets from external sources)
- **Runs** (executions/workflows)

NOT:
- Order fulfillment
- Task items with status tracking
- Kanban board items
- Work items management

If you need to understand "what gets fulfilled", it's **Signals** → converted into **Runs** → resulting in **PRs**.
