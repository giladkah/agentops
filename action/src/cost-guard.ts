import * as core from "@actions/core";

/** Rough per-run cost estimates by model tier */
const MODEL_COSTS: Record<string, number> = {
  haiku: 0.02,
  sonnet: 0.15,
  opus: 0.75,
};

/**
 * Estimate the cost of a run and check against the limit.
 * Returns true if the run should proceed, false if it exceeds the limit.
 */
export function checkCostLimit(
  costLimit: number,
  ensembleSize: number,
  workflowName: string
): { ok: boolean; estimated: number; message: string } {
  if (costLimit <= 0) {
    return { ok: true, estimated: 0, message: "No cost limit set" };
  }

  // Infer model tier from workflow name heuristics
  const wfLower = workflowName.toLowerCase();
  let modelTier = "sonnet"; // default
  if (wfLower.includes("quick") || wfLower.includes("triage")) {
    modelTier = "haiku";
  } else if (wfLower.includes("deep") || wfLower.includes("thorough")) {
    modelTier = "opus";
  }

  const perRunCost = MODEL_COSTS[modelTier] || MODEL_COSTS.sonnet;
  const estimated = perRunCost * ensembleSize;

  if (estimated > costLimit) {
    const msg = `Estimated cost $${estimated.toFixed(2)} (${ensembleSize} x $${perRunCost.toFixed(2)} ${modelTier}) exceeds limit of $${costLimit.toFixed(2)}`;
    return { ok: false, estimated, message: msg };
  }

  const msg = `Estimated cost: $${estimated.toFixed(2)} (${ensembleSize} x $${perRunCost.toFixed(2)} ${modelTier}), limit: $${costLimit.toFixed(2)}`;
  core.info(msg);
  return { ok: true, estimated, message: msg };
}
