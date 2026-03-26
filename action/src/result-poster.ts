import * as core from "@actions/core";
import * as github from "@actions/github";
import { RunResults, Finding } from "./ensemble-client";
import { EventContext } from "./event-mapper";

type Octokit = ReturnType<typeof github.getOctokit>;

function formatFinding(f: Finding): string {
  const sev = f.severity ? `**[${f.severity.charAt(0).toUpperCase() + f.severity.slice(1)}]**` : "";
  const location = f.file
    ? ` in \`${f.file}${f.line ? `:${f.line}` : ""}\``
    : "";
  const note = f.note ? ` \u2014 ${f.note}` : "";
  return `- ${sev} ${f.title}${location}${note}`;
}

function formatFindingsList(findings: Finding[]): string {
  if (findings.length === 0) return "_None_\n";
  return findings.map(formatFinding).join("\n") + "\n";
}

function buildMarkdownBody(results: RunResults, runId: string): string {
  const statusEmoji =
    results.status === "converged" || results.status === "merged"
      ? "\u2705"
      : results.status === "failed"
        ? "\u274c"
        : "\u23f3";

  const consensusCount = results.consensus.length;
  const majorityCount = results.majority.length;
  const uniqueCount = results.unique.length;

  let agreement = "";
  if (results.total_findings > 0 && results.total_reviewers > 1) {
    const pct = Math.round(
      (consensusCount / results.total_findings) * 100
    );
    agreement = ` | **Agreement**: ${pct}% consensus`;
  }

  let body = `<!-- ensemble-run-${runId} -->\n`;
  body += `## Ensemble Analysis Results\n\n`;
  body += `${statusEmoji} **Status**: ${results.status} | **Findings**: ${results.total_findings}${agreement}\n\n`;

  if (consensusCount > 0) {
    body += `### Consensus Findings (${consensusCount})\n`;
    body += formatFindingsList(results.consensus);
    body += "\n";
  }

  if (majorityCount > 0) {
    body += `### Majority Findings (${majorityCount})\n`;
    body += formatFindingsList(results.majority);
    body += "\n";
  }

  if (uniqueCount > 0) {
    body += `### Unique Findings (${uniqueCount})\n`;
    body += formatFindingsList(results.unique);
    body += "\n";
  }

  if (results.total_findings === 0) {
    body += "_No findings reported._\n\n";
  }

  body += `<details><summary>Run details</summary>\n\n`;
  body += `Run ID: ${runId} | Workflow: ${results.workflow || "N/A"} | Duration: ${results.duration_minutes}m | Cost: $${(results.total_cost || 0).toFixed(3)} | Reviewers: ${results.total_reviewers}\n`;
  body += `</details>\n`;

  return body;
}

/**
 * Post results as a comment on the PR/issue.
 * Uses a sentinel HTML comment for deduplication — updates existing comment on rerun.
 */
async function postComment(
  octokit: Octokit,
  context: EventContext,
  results: RunResults,
  runId: string
): Promise<string | undefined> {
  if (!context.number) {
    core.warning("No issue/PR number — cannot post comment");
    return undefined;
  }

  const body = buildMarkdownBody(results, runId);
  const sentinel = `<!-- ensemble-run-${runId} -->`;
  const owner = context.owner;
  const repo = context.repo;
  const issueNumber = context.number;

  // Look for existing comment with any ensemble sentinel to update
  let existingCommentId: number | undefined;
  try {
    const { data: comments } = await octokit.rest.issues.listComments({
      owner,
      repo,
      issue_number: issueNumber,
      per_page: 100,
    });

    // Find comment with the same run sentinel, or any ensemble sentinel for the same PR
    const sourceIdSentinel = `<!-- ensemble-run-`;
    for (const c of comments) {
      if (c.body?.includes(sentinel)) {
        existingCommentId = c.id;
        break;
      }
    }

    // If no exact match, look for any ensemble comment to update
    if (!existingCommentId) {
      for (const c of comments) {
        if (c.body?.includes(sourceIdSentinel)) {
          existingCommentId = c.id;
          break;
        }
      }
    }
  } catch (err) {
    core.warning(`Failed to list comments: ${(err as Error).message}`);
  }

  try {
    if (existingCommentId) {
      core.info(`Updating existing comment ${existingCommentId}`);
      const { data } = await octokit.rest.issues.updateComment({
        owner,
        repo,
        comment_id: existingCommentId,
        body,
      });
      return data.html_url;
    } else {
      core.info(`Creating new comment on #${issueNumber}`);
      const { data } = await octokit.rest.issues.createComment({
        owner,
        repo,
        issue_number: issueNumber,
        body,
      });
      return data.html_url;
    }
  } catch (err) {
    core.error(`Failed to post comment: ${(err as Error).message}`);
    return undefined;
  }
}

/**
 * Post results as a GitHub Check Run with inline annotations.
 */
async function postCheck(
  octokit: Octokit,
  context: EventContext,
  results: RunResults,
  runId: string
): Promise<void> {
  const allFindings = [
    ...results.consensus,
    ...results.majority,
    ...results.unique,
  ];

  const hasHigh = allFindings.some(
    (f) => f.severity === "critical" || f.severity === "high"
  );
  const conclusion: "success" | "neutral" | "failure" =
    allFindings.length === 0
      ? "success"
      : hasHigh
        ? "failure"
        : "neutral";

  // Build annotations from findings that have file paths
  const annotations = allFindings
    .filter((f) => f.file)
    .slice(0, 50) // GitHub limits to 50 annotations per request
    .map((f) => ({
      path: f.file!,
      start_line: f.line || 1,
      end_line: f.line || 1,
      annotation_level: (f.severity === "critical" || f.severity === "high"
        ? "failure"
        : f.severity === "medium"
          ? "warning"
          : "notice") as "failure" | "warning" | "notice",
      message: `${f.title}${f.note ? `: ${f.note}` : ""}`,
      title: `[${f.severity}] ${f.title}`,
    }));

  const headSha =
    github.context.payload.pull_request?.head?.sha ||
    github.context.sha;

  try {
    await octokit.rest.checks.create({
      owner: context.owner,
      repo: context.repo,
      name: "Ensemble Analysis",
      head_sha: headSha,
      status: "completed",
      conclusion,
      output: {
        title: `Ensemble: ${results.total_findings} findings`,
        summary: buildMarkdownBody(results, runId),
        annotations,
      },
    });
    core.info(`Check run created: ${conclusion}`);
  } catch (err) {
    core.error(`Failed to create check run: ${(err as Error).message}`);
  }
}

/**
 * Write results to $GITHUB_STEP_SUMMARY.
 */
function postSummary(results: RunResults, runId: string): void {
  const body = buildMarkdownBody(results, runId);
  core.summary.addRaw(body);
  core.summary.write();
  core.info("Results written to step summary");
}

/**
 * Post results back to GitHub based on the configured mode.
 */
export async function postResults(
  mode: string,
  githubToken: string,
  context: EventContext,
  results: RunResults,
  runId: string
): Promise<string | undefined> {
  if (mode === "silent") {
    core.info("Silent mode — skipping result posting");
    return undefined;
  }

  if (mode === "summary") {
    postSummary(results, runId);
    return undefined;
  }

  const octokit = github.getOctokit(githubToken);

  if (mode === "check") {
    await postCheck(octokit, context, results, runId);
    // Also write summary
    postSummary(results, runId);
    return undefined;
  }

  // Default: comment mode
  const commentUrl = await postComment(octokit, context, results, runId);
  // Also write summary
  postSummary(results, runId);
  return commentUrl;
}
