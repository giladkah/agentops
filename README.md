# ⚡ Ensemble

**AI agents that peer-review each other's code — and ship only when they agree.**

Run your engineering task 2–4 times in parallel. Independent agents plan, code, and review. A consensus agent picks the best approach. You get one clean PR you can trust.

```
Run 1: plan → engineer → 3 reviewers → synthesis ─┐
Run 2: plan → engineer → 3 reviewers → synthesis ─┼→ consensus → PR
Run 3: plan → engineer → 3 reviewers → synthesis ─┘
```

---

## Install (macOS)

```bash
curl -sSL https://raw.githubusercontent.com/giladkah/agentops/main/install.sh | bash
```

That's it. The script checks prerequisites, installs into `~/tools/agentops`, and launches the menubar app.

**Prerequisites:**
- macOS 12+
- Python 3.10+
- [Claude CLI](https://claude.ai/download)
- [GitHub CLI](https://cli.github.com) (for PR creation)
- An [Anthropic API key](https://console.anthropic.com)

---

## First run

1. Click **⚡** in your menu bar
2. Click **"Repo: Not set"** → pick your git repository
3. Click **"Set API Key…"** → paste your `sk-ant-...` key
4. Click **"Start Server"** → browser opens at `localhost:5050`

---

## Usage

**Single run** — one pipeline, three parallel reviewers:
`plan → engineer → [reviewer] [reviewer] [reviewer] → synthesis → PR`
Best for: routine cleanup, bug fixes. ~$0.10–0.30, ~10 min.

**Parallel runs (Drift Detection)** — N full pipelines, consensus picks best:
Select 1×, 2×, 3×, or 4× in the launch modal.
Best for: high-stakes changes, architecture decisions. ~$0.30–1.00, ~20–40 min.

---

## Privacy

Anonymous telemetry only (run counts, durations, costs). No code, paths, or task text ever leaves your machine. Opt out: `AGENTOPS_TELEMETRY=false`

---

## Design partners

[ensemblecode.dev](https://ensemblecode.dev)
