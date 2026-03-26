import * as core from "@actions/core";
import { EnsembleClient, RunResults, WorkflowData } from "./ensemble-client";
import { mapEvent } from "./event-mapper";
import { pollRun, pollEnsemble } from "./poller";
import { postResults } from "./result-poster";
import { checkCostLimit } from "./cost-guard";

async function run(): Promise<void> {
  try {
    // ── Read inputs ──
    const ensembleUrl = core.getInput("ensemble-url", { required: true });
    const ensembleToken = core.getInput("ensemble-token");
    const githubToken = core.getInput("github-token");
    const mode = core.getInput("mode") || "single";
    const workflowName = core.getInput("workflow") || "Bug Fix";
    const postMode = core.getInput("post-results") || "comment";
    const timeoutMinutes = parseInt(core.getInput("timeout") || "30", 10);
    const ensembleSize = parseInt(core.getInput("ensemble-size") || "3", 10);
    const labelTrigger = core.getInput("label-trigger");
    const costLimit = parseFloat(core.getInput("cost-limit") || "0");

    core.info(`Ensemble Action starting...`);
    core.info(`  Server: ${ensembleUrl}`);
    core.info(`  Mode: ${mode}`);
    core.info(`  Workflow: ${workflowName}`);
    core.info(`  Post results: ${postMode}`);
    core.info(`  Timeout: ${timeoutMinutes}m`);

    // ── Map GitHub event to signal ──
    const eventResult = mapEvent(labelTrigger);
    if (!eventResult) {
      core.info("Event skipped (label trigger not matched or unsupported event)");
      core.setOutput("status", "skipped");
      return;
    }

    const { signal: signalParams, context: eventContext } = eventResult;
    core.info(`Mapped event: ${signalParams.title} (${signalParams.source_id})`);

    // ── Initialize client ──
    const client = new EnsembleClient(ensembleUrl, ensembleToken);

    // ── Health check ──
    try {
      const health = await client.healthCheck();
      core.info(`Server healthy: v${health.version}`);
    } catch (err) {
      core.setFailed(
        `Cannot reach Ensemble server at ${ensembleUrl}: ${(err as Error).message}. ` +
          `Is the server running? Check the ensemble-url input.`
      );
      return;
    }

    // ── Resolve workflow ──
    let workflows: WorkflowData[];
    try {
      workflows = await client.listWorkflows();
    } catch (err) {
      core.setFailed(`Failed to list workflows: ${(err as Error).message}`);
      return;
    }

    const workflow = workflows.find(
      (w) => w.name.toLowerCase() === workflowName.toLowerCase()
    );
    if (!workflow) {
      const available = workflows.map((w) => w.name).join(", ");
      core.setFailed(
        `Workflow "${workflowName}" not found. Available: [${available}]`
      );
      return;
    }

    core.info(`Using workflow: ${workflow.name} (${workflow.id})`);

    // ── Check for existing runs (rerun detection) ──
    if (signalParams.source_id) {
      try {
        const existing = await client.findRuns(signalParams.source_id);
        const active = existing.find(
          (r) => r.status === "running" || r.status === "pending"
        );
        if (active) {
          core.info(
            `Analysis already in progress for ${signalParams.source_id} (run ${active.id}, status: ${active.status}). Skipping.`
          );
          core.setOutput("run-id", active.id);
          core.setOutput("status", "already_running");
          return;
        }
      } catch {
        // Non-fatal — source_id filter might not be supported on older servers
        core.debug("Could not check for existing runs (non-fatal)");
      }
    }

    // ── Cost guard ──
    const runCount = mode === "ensemble" ? ensembleSize : 1;
    const costCheck = checkCostLimit(costLimit, runCount, workflowName);
    if (!costCheck.ok) {
      core.setFailed(costCheck.message);
      return;
    }

    // ── Create signal ──
    core.info("Creating signal...");
    const signal = await client.createSignal({
      title: signalParams.title,
      summary: signalParams.summary.substring(0, 5000),
      severity: signalParams.severity,
      source: signalParams.source,
      source_id: signalParams.source_id,
    });
    core.info(`Signal created: ${signal.id}`);

    let finalRunId: string;
    let finalStatus: string;

    if (mode === "ensemble") {
      // ── Ensemble mode ──
      core.info(
        `Creating ensemble (${ensembleSize} runs, workflow: ${workflow.name})...`
      );
      const ensemble = await client.createEnsemble({
        title: signalParams.title,
        task_description: signalParams.summary.substring(0, 5000),
        workflow_id: workflow.id,
        num_runs: ensembleSize,
        auto_approve: true,
      });
      core.info(`Ensemble created: ${ensemble.id}`);

      await client.startEnsemble(ensemble.id);
      core.info("Ensemble started");

      const pollResult = await pollEnsemble(
        client,
        ensemble.id,
        timeoutMinutes
      );
      finalRunId = ensemble.id;
      finalStatus = pollResult.timedOut ? "timeout" : pollResult.status;

      // For ensemble, get results from the first sub-run that completed
      if (!pollResult.timedOut) {
        const ensembleData = await client.getEnsemble(ensemble.id);
        const subRunIds = ensembleData.run_ids || [];
        if (subRunIds.length > 0) {
          // Use first sub-run for results
          finalRunId = subRunIds[0];
        }
      }
    } else {
      // ── Single run mode ──
      core.info(`Creating run (workflow: ${workflow.name})...`);
      const runData = await client.createRun({
        workflow_id: workflow.id,
        title: signalParams.title,
        task_description: signalParams.summary.substring(0, 5000),
        source_type: "github",
        source_id: signalParams.source_id,
        auto_approve: true,
      });
      core.info(`Run created: ${runData.id}`);

      await client.startRun(runData.id);
      core.info("Run started");

      const pollResult = await pollRun(client, runData.id, timeoutMinutes);
      finalRunId = runData.id;
      finalStatus = pollResult.timedOut ? "timeout" : pollResult.status;
    }

    core.setOutput("run-id", finalRunId);
    core.setOutput("status", finalStatus);

    // ── Get results ──
    let results: RunResults;
    try {
      results = await client.getRunResults(finalRunId);
    } catch {
      // Fallback: build minimal results from issues endpoint
      core.debug(
        "Results endpoint not available, falling back to issues endpoint"
      );
      try {
        const issues = await client.getRunIssues(finalRunId);
        const runData = await client.getRun(finalRunId);
        results = {
          run_id: finalRunId,
          status: finalStatus,
          title: runData.title || signalParams.title,
          consensus: issues.consensus || [],
          majority: issues.majority || [],
          unique: issues.unique || [],
          total_reviewers: issues.total_reviewers || 0,
          total_findings:
            (issues.consensus?.length || 0) +
            (issues.majority?.length || 0) +
            (issues.unique?.length || 0),
          duration_minutes: runData.duration_minutes || 0,
          total_cost: runData.total_cost || 0,
          workflow: workflow.name,
        };
      } catch (issuesErr) {
        core.warning(
          `Could not fetch results: ${(issuesErr as Error).message}`
        );
        results = {
          run_id: finalRunId,
          status: finalStatus,
          title: signalParams.title,
          consensus: [],
          majority: [],
          unique: [],
          total_reviewers: 0,
          total_findings: 0,
          duration_minutes: 0,
          total_cost: 0,
          workflow: workflow.name,
        };
      }
    }

    core.setOutput("findings-count", results.total_findings.toString());
    core.info(
      `Analysis complete: ${results.total_findings} findings (${results.consensus.length} consensus, ${results.majority.length} majority, ${results.unique.length} unique)`
    );

    // ── Post results ──
    if (finalStatus === "timeout") {
      // Post timeout message
      results.status = "timeout";
    }

    const commentUrl = await postResults(
      postMode,
      githubToken,
      eventContext,
      results,
      finalRunId
    );

    if (commentUrl) {
      core.setOutput("comment-url", commentUrl);
      core.info(`Comment posted: ${commentUrl}`);
    }

    // ── Set final status ──
    if (finalStatus === "failed") {
      core.warning("Ensemble run failed");
    } else if (finalStatus === "timeout") {
      core.warning("Ensemble run timed out");
    } else {
      core.info(`Ensemble run completed successfully: ${finalStatus}`);
    }
  } catch (error) {
    core.setFailed(`Ensemble Action failed: ${(error as Error).message}`);
  }
}

run();
