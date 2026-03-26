import * as core from "@actions/core";
import https from "https";
import http from "http";
import { URL } from "url";

export interface Signal {
  id: string;
  title: string;
  source: string;
  source_id: string;
  severity: string;
  status: string;
}

export interface RunData {
  id: string;
  status: string;
  title: string;
  workflow_id: string;
  total_cost: number;
  duration_minutes: number;
  source_id?: string;
}

export interface EnsembleData {
  id: string;
  status: string;
  title: string;
  num_runs: number;
  run_ids: string[];
  total_cost: number;
  duration_minutes: number;
}

export interface Finding {
  title: string;
  file?: string;
  line?: number;
  severity: string;
  category?: string;
  note?: string;
  found_count: number;
  total_reviewers: number;
}

export interface RunResults {
  run_id: string;
  status: string;
  title: string;
  consensus: Finding[];
  majority: Finding[];
  unique: Finding[];
  total_reviewers: number;
  total_findings: number;
  duration_minutes: number;
  total_cost: number;
  workflow?: string;
}

export interface RunIssues {
  consensus: Finding[];
  majority: Finding[];
  unique: Finding[];
  total_reviewers: number;
  total_issues: number;
}

export interface WorkflowData {
  id: string;
  name: string;
  description: string;
}

export class EnsembleClient {
  private baseUrl: string;
  private token: string;

  constructor(baseUrl: string, token: string = "") {
    // Strip trailing slash
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.token = token;
  }

  private async request<T>(
    method: string,
    path: string,
    body?: Record<string, unknown>
  ): Promise<T> {
    const url = new URL(path, this.baseUrl);
    const isHttps = url.protocol === "https:";
    const lib = isHttps ? https : http;

    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      Accept: "application/json",
    };
    if (this.token) {
      headers["Authorization"] = `Bearer ${this.token}`;
    }

    const bodyStr = body ? JSON.stringify(body) : undefined;
    if (bodyStr) {
      headers["Content-Length"] = Buffer.byteLength(bodyStr).toString();
    }

    return new Promise<T>((resolve, reject) => {
      const req = lib.request(
        url,
        {
          method,
          headers,
          timeout: 30000,
        },
        (res) => {
          let data = "";
          res.on("data", (chunk: string) => (data += chunk));
          res.on("end", () => {
            if (
              res.statusCode &&
              res.statusCode >= 200 &&
              res.statusCode < 300
            ) {
              try {
                resolve(JSON.parse(data) as T);
              } catch {
                resolve(data as unknown as T);
              }
            } else {
              reject(
                new Error(
                  `HTTP ${res.statusCode}: ${data.substring(0, 500)}`
                )
              );
            }
          });
        }
      );

      req.on("error", reject);
      req.on("timeout", () => {
        req.destroy();
        reject(new Error(`Request timeout: ${method} ${path}`));
      });

      if (bodyStr) {
        req.write(bodyStr);
      }
      req.end();
    });
  }

  private async requestWithRetry<T>(
    method: string,
    path: string,
    body?: Record<string, unknown>,
    retries: number = 3,
    backoffMs: number = 10000
  ): Promise<T> {
    let lastError: Error | undefined;
    for (let attempt = 0; attempt < retries; attempt++) {
      try {
        return await this.request<T>(method, path, body);
      } catch (err) {
        lastError = err as Error;
        if (attempt < retries - 1) {
          const waitMs = backoffMs * (attempt + 1);
          core.warning(
            `Request failed (attempt ${attempt + 1}/${retries}): ${lastError.message}. Retrying in ${waitMs / 1000}s...`
          );
          await new Promise((r) => setTimeout(r, waitMs));
        }
      }
    }
    throw lastError;
  }

  async healthCheck(): Promise<{ status: string; version: string }> {
    return this.requestWithRetry("GET", "/api/health");
  }

  async createSignal(params: {
    title: string;
    summary: string;
    severity: string;
    source: string;
    source_id: string;
    files_hint?: string[];
    raw_payload?: Record<string, unknown>;
  }): Promise<Signal> {
    return this.requestWithRetry("POST", "/api/signals", params as unknown as Record<string, unknown>);
  }

  async listWorkflows(): Promise<WorkflowData[]> {
    return this.requestWithRetry("GET", "/api/workflows");
  }

  async findRuns(sourceId: string): Promise<RunData[]> {
    return this.requestWithRetry("GET", `/api/runs?source_id=${encodeURIComponent(sourceId)}`);
  }

  async createRun(params: {
    workflow_id: string;
    title: string;
    task_description: string;
    source_type?: string;
    source_id?: string;
    auto_approve?: boolean;
    base_branch?: string;
  }): Promise<RunData> {
    return this.requestWithRetry("POST", "/api/runs", params as unknown as Record<string, unknown>);
  }

  async startRun(runId: string): Promise<{ status: string }> {
    return this.requestWithRetry("POST", `/api/runs/${runId}/start`);
  }

  async getRun(runId: string): Promise<RunData> {
    // No retry on poll — single attempt is fine
    return this.request("GET", `/api/runs/${runId}`);
  }

  async getRunIssues(runId: string): Promise<RunIssues> {
    return this.requestWithRetry("GET", `/api/runs/${runId}/issues`);
  }

  async getRunResults(runId: string): Promise<RunResults> {
    return this.requestWithRetry("GET", `/api/runs/${runId}/results`);
  }

  async createEnsemble(params: {
    title: string;
    task_description: string;
    workflow_id: string;
    num_runs?: number;
    auto_approve?: boolean;
    base_branch?: string;
  }): Promise<EnsembleData> {
    return this.requestWithRetry("POST", "/api/ensembles", params as unknown as Record<string, unknown>);
  }

  async startEnsemble(ensembleId: string): Promise<{ status: string }> {
    return this.requestWithRetry("POST", `/api/ensembles/${ensembleId}/start`);
  }

  async getEnsemble(ensembleId: string): Promise<EnsembleData> {
    return this.request("GET", `/api/ensembles/${ensembleId}`);
  }
}
