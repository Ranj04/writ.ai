export type Validity = "VALID" | "NEEDS_REVIEW" | "INVALIDATED";
export type Verdict = "ALLOW" | "REPLAN" | "BLOCK" | "HUMAN_REVIEW";

export interface Artifact {
  id: string;
  kind: string;
  title: string;
  text: string;
  scopes: string[];
  validity: Validity;
  invalidated_scopes: string[];
}

export interface Edge {
  source_id: string;
  target_id: string;
  kind: string;
  scopes?: string[];
  evidence_ref?: string | null;
}

export interface InvalidationPath {
  artifact_id: string;
  node_ids: string[];
}

export interface InvalidationReport {
  graph_version: string;
  changed_decision_id: string;
  superseded_decision_id: string;
  affected_scopes: string[];
  affected_artifact_ids: string[];
  upstream_chain_artifact_ids: string[];
  stopped_work_artifact_ids: string[];
  directly_mentioned_artifact_ids: string[];
  preserved_artifact_ids: string[];
  preserved_task_ids?: string[];
  invalidated_task_ids?: string[];
  needs_review_artifact_ids?: string[];
  paths: InvalidationPath[];
  evidence_refs: string[];
}

export interface PlanAction {
  id: string;
  description: string;
  scopes: string[];
  attributes: Record<string, unknown>;
}

export interface AgentPlan {
  id: string;
  ticket_id: string;
  objective: string;
  actions: PlanAction[];
}
