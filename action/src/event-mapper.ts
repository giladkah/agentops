import * as github from "@actions/github";
import * as core from "@actions/core";

export interface SignalParams {
  title: string;
  summary: string;
  source_id: string;
  severity: string;
  source: string;
  files_hint?: string[];
}

export interface EventContext {
  /** The issue or PR number, if applicable */
  number?: number;
  /** Owner/repo string */
  repo: string;
  owner: string;
}

/**
 * Map the current GitHub event to an Ensemble signal.
 * Returns null if the event should be skipped (e.g., label-trigger not matched).
 */
export function mapEvent(labelTrigger: string): {
  signal: SignalParams;
  context: EventContext;
} | null {
  const ctx = github.context;
  const eventName = ctx.eventName;
  const payload = ctx.payload;
  const repo = ctx.repo.repo;
  const owner = ctx.repo.owner;

  // Label-trigger check: if set, the label must be present
  if (labelTrigger) {
    const labels = getLabels(payload, eventName);
    if (!labels.some((l) => l.toLowerCase() === labelTrigger.toLowerCase())) {
      core.info(
        `Label trigger "${labelTrigger}" not found. Labels: [${labels.join(", ")}]. Skipping.`
      );
      return null;
    }
  }

  if (eventName === "pull_request") {
    return mapPullRequest(payload, owner, repo);
  } else if (eventName === "issues") {
    return mapIssue(payload, owner, repo);
  } else if (eventName === "issue_comment") {
    return mapIssueComment(payload, owner, repo);
  } else if (eventName === "workflow_dispatch") {
    return mapWorkflowDispatch(payload, owner, repo);
  } else if (eventName === "schedule") {
    return mapSchedule(owner, repo);
  }

  core.warning(`Unhandled event type: ${eventName}. Attempting generic mapping.`);
  return mapGeneric(payload, owner, repo, eventName);
}

function getLabels(
  payload: Record<string, unknown>,
  eventName: string
): string[] {
  if (eventName === "pull_request") {
    const pr = payload.pull_request as Record<string, unknown> | undefined;
    const labels = (pr?.labels as Array<Record<string, string>>) || [];
    return labels.map((l) => l.name || "");
  } else if (eventName === "issues" || eventName === "issue_comment") {
    const issue = payload.issue as Record<string, unknown> | undefined;
    const labels = (issue?.labels as Array<Record<string, string>>) || [];
    return labels.map((l) => l.name || "");
  }
  return [];
}

function mapSeverityFromLabels(labels: string[]): string {
  const lower = labels.map((l) => l.toLowerCase());
  if (lower.some((l) => ["critical", "urgent", "p0", "p1"].includes(l)))
    return "critical";
  if (lower.some((l) => ["high", "bug", "p2"].includes(l))) return "high";
  if (lower.some((l) => ["low", "p3", "good first issue"].includes(l)))
    return "low";
  return "medium";
}

function mapPullRequest(
  payload: Record<string, unknown>,
  owner: string,
  repo: string
): { signal: SignalParams; context: EventContext } {
  const pr = payload.pull_request as Record<string, unknown>;
  const number = pr.number as number;
  const title = pr.title as string;
  const body = (pr.body as string) || "";
  const labels = ((pr.labels as Array<Record<string, string>>) || []).map(
    (l) => l.name || ""
  );

  // Extract changed files list from the PR body if available
  const summary = body;

  return {
    signal: {
      title: `PR #${number}: ${title}`,
      summary,
      source_id: `GH-PR-${number}`,
      severity: mapSeverityFromLabels(labels),
      source: "github",
    },
    context: { number, repo, owner },
  };
}

function mapIssue(
  payload: Record<string, unknown>,
  owner: string,
  repo: string
): { signal: SignalParams; context: EventContext } {
  const issue = payload.issue as Record<string, unknown>;
  const number = issue.number as number;
  const title = issue.title as string;
  const body = (issue.body as string) || "";
  const labels = ((issue.labels as Array<Record<string, string>>) || []).map(
    (l) => l.name || ""
  );

  return {
    signal: {
      title: `Issue #${number}: ${title}`,
      summary: body,
      source_id: `GH-ISSUE-${number}`,
      severity: mapSeverityFromLabels(labels),
      source: "github",
    },
    context: { number, repo, owner },
  };
}

function mapIssueComment(
  payload: Record<string, unknown>,
  owner: string,
  repo: string
): { signal: SignalParams; context: EventContext } | null {
  const comment = payload.comment as Record<string, unknown>;
  const commentBody = (comment.body as string) || "";

  // Only handle /ensemble commands
  if (!commentBody.includes("/ensemble")) {
    core.info("Comment does not contain /ensemble command. Skipping.");
    return null;
  }

  const issue = payload.issue as Record<string, unknown>;
  const number = issue.number as number;
  const title = issue.title as string;
  const body = (issue.body as string) || "";
  const isPR = !!(issue.pull_request as unknown);

  const prefix = isPR ? "PR" : "Issue";
  const sourceIdPrefix = isPR ? "GH-PR" : "GH-ISSUE";

  return {
    signal: {
      title: `${prefix} #${number}: ${title}`,
      summary: `${body}\n\n---\nCommand: ${commentBody}`,
      source_id: `${sourceIdPrefix}-${number}`,
      severity: "medium",
      source: "github",
    },
    context: { number, repo, owner },
  };
}

function mapWorkflowDispatch(
  payload: Record<string, unknown>,
  owner: string,
  repo: string
): { signal: SignalParams; context: EventContext } {
  const inputs = (payload.inputs as Record<string, string>) || {};
  const issueNumber = inputs["issue-number"]
    ? parseInt(inputs["issue-number"], 10)
    : undefined;

  return {
    signal: {
      title: issueNumber
        ? `Manual analysis: Issue #${issueNumber}`
        : "Manual Ensemble analysis",
      summary: `Manually triggered via workflow_dispatch.\n${JSON.stringify(inputs, null, 2)}`,
      source_id: issueNumber ? `GH-ISSUE-${issueNumber}` : `GH-DISPATCH-${Date.now()}`,
      severity: "medium",
      source: "github",
    },
    context: { number: issueNumber, repo, owner },
  };
}

function mapSchedule(
  owner: string,
  repo: string
): { signal: SignalParams; context: EventContext } {
  return {
    signal: {
      title: "Scheduled Ensemble sweep",
      summary: `Scheduled analysis run for ${owner}/${repo}`,
      source_id: `GH-SCHEDULE-${Date.now()}`,
      severity: "medium",
      source: "github",
    },
    context: { repo, owner },
  };
}

function mapGeneric(
  payload: Record<string, unknown>,
  owner: string,
  repo: string,
  eventName: string
): { signal: SignalParams; context: EventContext } {
  return {
    signal: {
      title: `GitHub event: ${eventName}`,
      summary: JSON.stringify(payload).substring(0, 2000),
      source_id: `GH-${eventName.toUpperCase()}-${Date.now()}`,
      severity: "medium",
      source: "github",
    },
    context: { repo, owner },
  };
}
