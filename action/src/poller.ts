import * as core from "@actions/core";
import { EnsembleClient, RunData, EnsembleData } from "./ensemble-client";

const TERMINAL_RUN_STATUSES = new Set([
  "converged",
  "merged",
  "failed",
  "cancelled",
]);
const TERMINAL_ENSEMBLE_STATUSES = new Set(["done", "failed", "cancelled"]);

/** Interval in ms: 30s for first 5 min, then 60s */
function getInterval(elapsedMs: number): number {
  return elapsedMs < 5 * 60 * 1000 ? 30_000 : 60_000;
}

export interface PollResult {
  status: string;
  timedOut: boolean;
}

/**
 * Poll a single run until it reaches a terminal status or times out.
 */
export async function pollRun(
  client: EnsembleClient,
  runId: string,
  timeoutMinutes: number
): Promise<PollResult> {
  const timeoutMs = timeoutMinutes * 60 * 1000;
  const startTime = Date.now();
  let lastStatus = "";

  core.info(`Polling run ${runId} (timeout: ${timeoutMinutes}m)...`);

  while (true) {
    const elapsed = Date.now() - startTime;

    if (elapsed > timeoutMs) {
      core.warning(
        `Run ${runId} timed out after ${timeoutMinutes} minutes (last status: ${lastStatus})`
      );
      return { status: lastStatus || "timeout", timedOut: true };
    }

    try {
      const run: RunData = await client.getRun(runId);
      const status = run.status;

      if (status !== lastStatus) {
        core.info(
          `Run ${runId}: ${status} (${Math.round(elapsed / 1000)}s elapsed, cost: $${(run.total_cost || 0).toFixed(3)})`
        );
        lastStatus = status;
      }

      if (TERMINAL_RUN_STATUSES.has(status)) {
        core.info(
          `Run ${runId} completed: ${status} in ${run.duration_minutes}m`
        );
        return { status, timedOut: false };
      }
    } catch (err) {
      core.warning(`Poll error (will retry): ${(err as Error).message}`);
    }

    const interval = getInterval(Date.now() - startTime);
    await new Promise((r) => setTimeout(r, interval));
  }
}

/**
 * Poll an ensemble run until it reaches a terminal status or times out.
 */
export async function pollEnsemble(
  client: EnsembleClient,
  ensembleId: string,
  timeoutMinutes: number
): Promise<PollResult> {
  const timeoutMs = timeoutMinutes * 60 * 1000;
  const startTime = Date.now();
  let lastStatus = "";

  core.info(
    `Polling ensemble ${ensembleId} (timeout: ${timeoutMinutes}m)...`
  );

  while (true) {
    const elapsed = Date.now() - startTime;

    if (elapsed > timeoutMs) {
      core.warning(
        `Ensemble ${ensembleId} timed out after ${timeoutMinutes} minutes (last status: ${lastStatus})`
      );
      return { status: lastStatus || "timeout", timedOut: true };
    }

    try {
      const ensemble: EnsembleData = await client.getEnsemble(ensembleId);
      const status = ensemble.status;

      if (status !== lastStatus) {
        core.info(
          `Ensemble ${ensembleId}: ${status} (${Math.round(elapsed / 1000)}s elapsed, cost: $${(ensemble.total_cost || 0).toFixed(3)})`
        );
        lastStatus = status;
      }

      if (TERMINAL_ENSEMBLE_STATUSES.has(status)) {
        core.info(
          `Ensemble ${ensembleId} completed: ${status} in ${ensemble.duration_minutes}m`
        );
        return { status, timedOut: false };
      }
    } catch (err) {
      core.warning(`Poll error (will retry): ${(err as Error).message}`);
    }

    const interval = getInterval(Date.now() - startTime);
    await new Promise((r) => setTimeout(r, interval));
  }
}
