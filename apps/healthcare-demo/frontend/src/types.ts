/**
 * The shapes of the healthcare API's responses, as this UI reads them.
 *
 * Only the fields the UI uses are typed. The backend owns every value here;
 * the UI formats them and never derives a decision from them.
 */

export type Metrics = Record<string, number>;

export interface Dataset {
  name: string;
  citation: string;
  licence: string;
  licence_url: string;
  source_url: string;
  notice: string;
  credit: string;
}

/** Where one hospital keeps its records. */
export interface Store {
  kind: string;
  schema?: string;
  role?: string;
  memory?: string;
}

/** What one hospital reports to the coordinator. */
export interface HospitalSummary {
  site_id: string;
  display_name: string;
  country: string;
  record_count: number;
  entered_count: number;
  labelled_count: number;
  outcome_rate: number | null;
  /** Why the rate is or is not reported. Older recordings lack it. */
  outcome_rate_status?: OutcomeRateStatus;
  minimum_cohort: number;
  store: Store;
}

/** The hospital's reason for reporting its outcome rate or not, from its Guard's planner. */
export type OutcomeRateStatus = "released" | "no_outcomes" | "below_minimum_cohort" | "denied" | "withheld";

export interface GateDefinition {
  gate_id: string;
  metric: string;
  comparison: { kind: string; value: number | null; margin: number | null };
  blocking: boolean;
}

export interface Overview {
  study_id: string;
  condition: {
    condition_id: string;
    display_name: string;
    intended_use: string;
    limitations: string[];
  };
  dataset: Dataset;
  feature_contract: { contract_id: string; version: string; required_fields: string[] };
  /** Each record field's label, from the feature contract. Older recordings lack it. */
  field_labels?: Record<string, string>;
  engine: string;
  secure_aggregation_threshold: number;
  minimum_cohort: number;
  current_model: { version: string; kind: string };
  hospitals: HospitalSummary[];
  release_gates: GateDefinition[];
}

/** What one hospital's store holds, as the status report counts it. */
export interface StoreCount {
  site_id: string;
  record_count: number;
  labelled_count: number;
}

/** Where the records are stored, as the backend reports it. */
export interface FrameworkStatus {
  cohort_source: string;
  store_layout: string;
  dataset: Dataset;
  /** Every store could be read and holds at least one record. Older recordings lack it. */
  ready?: boolean;
  /** Each store's counts, or null when the stores cannot be read. */
  hospitals?: StoreCount[] | null;
}

export interface ModelVersion {
  version: string;
  kind: string;
  digest: string;
  status: string;
  parent_version: string | null;
  created_at: string;
}

export interface ModelsResponse {
  model_in_use: ModelVersion;
  models: ModelVersion[];
}

export interface FieldProfile {
  present: number;
  missing: number;
  kind: string;
  numeric: { count: number; mean: number; variance: number; min: number; max: number } | null;
  categories: Record<string, number>;
  booleans: Record<string, number>;
  other: number;
}

export interface Contribution {
  round_id: string;
  phase: string;
  participation: string;
  contributed_examples: number;
  update_norm: number | null;
  metrics: Metrics;
  reason: string | null;
  submitted_at: string;
}

export interface Dashboard {
  site_id: string;
  display_name: string;
  country: string;
  store: Store;
  counts: { total: number; entered: number; labelled: number };
  outcome_rate: number | null;
  outcome_rate_status?: OutcomeRateStatus;
  minimum_cohort: number;
  max_entered: number;
  profile: {
    record_count: number;
    tracked_fields: string[];
    fields: Record<string, FieldProfile>;
  };
  local_metrics: { model_version: string; labelled_records: number; metrics: Metrics };
  current_model: ModelVersion;
  contributions: Contribution[];
}

export interface Assessment {
  model_version: string;
  model_kind?: string;
  score: number;
  priority: string;
  basis: string[];
  warnings: string[];
  data_quality_issues: string[];
  suppressed: boolean;
}

/** One stored record. A missing value is an absent key. */
export interface EncounterRecord {
  encounter_id: string;
  origin?: string;
  age?: number;
  age_band?: string;
  sex?: string;
  conscious_level?: string;
  hours_since_onset?: number;
  systolic_bp?: number;
  stroke_subtype?: string;
  poor_outcome_6m?: boolean;
  recorded_at?: string;
  outcome_recorded_at?: string;
  [field: string]: unknown;
}

export interface ScoredRecord extends EncounterRecord {
  assessment: Assessment;
}

export interface WorklistResponse {
  site_id: string;
  worklist: ScoredRecord[];
}

export interface EnteredResponse {
  site_id: string;
  origin: string;
  encounters: ScoredRecord[];
  counts: { total: number; entered: number; labelled: number };
  max_entered: number;
}

export interface AuditRecord {
  audit_id: string;
  recorded_at: number;
  boundary: string;
  request_id: string;
  requester_id: string;
  decision: string;
  permitted: boolean;
  granularity: string;
  filtering_score: number;
  withheld_fields: string[];
  violations: string[];
  correlation_id?: string | null;
  contribution_norm_before?: number | null;
  contribution_norm_after?: number | null;
}

export interface AuditResponse {
  site_id: string;
  chain_intact: boolean;
  records: AuditRecord[];
}

export interface FormField {
  name: string;
  label: string;
  type: "integer" | "number" | "choice" | "boolean";
  required: boolean;
  options: string[] | null;
  min: number | null;
  max: number | null;
  unit: string | null;
  help: string | null;
}

export interface EntryForm {
  site_id: string;
  display_name: string;
  fields: FormField[];
  max_entered: number;
  entered_count: number;
  remaining: number;
  idempotency: { header: string; max_length: number; pattern: string };
  notice: string;
}

export interface EntryResult {
  record: EncounterRecord;
  assessment: Assessment;
  counts: { total: number; entered: number; labelled: number };
}

export interface ChartTable {
  columns: string[];
  rows: (string | number | null)[][];
}

export interface ChartInfo {
  chart_id: string;
  title: string;
  description: string;
  alt: string;
  available: boolean;
  reason: string | null;
  table: ChartTable;
  notice: string;
  svg_path: string;
  version: string;
}

export interface FieldRestriction {
  field: string;
  sensitivity: string;
  strategy: string;
  reason: string;
}

export interface GuardProbe {
  site_id: string;
  audit_id?: string;
  permitted: boolean;
  message: string;
  withheld_fields: string[];
  transformed_fields: string[];
  /** How many records or summary rows the Guard would release. The records stay at the hospital. */
  released_count: number;
  /** The field names in the released records. */
  released_fields: string[];
  plan: {
    decision: string;
    filtering_score: number;
    violations: string[];
    obligations: string[];
    assessment: { kyu_score: number; trust: string; rationale: string[] };
    field_restrictions: FieldRestriction[];
  };
}

export interface SiteResult {
  site_id: string;
  participation: string;
  contributed_examples: number;
  update_norm: number | null;
  metrics: Metrics;
}

export interface RoundReport {
  round_id: string;
  phase: string;
  status: string;
  model_version?: string;
  candidate_version?: string;
  plan: {
    selected_sites: string[];
    excluded_sites: { site_id: string; reason: string }[];
    quorum_met: boolean;
    required_participants: number;
    round_digest: string;
  };
  results: SiteResult[];
  readiness: {
    aggregation_permitted: boolean;
    accepted_sites: string[];
    rejected_contributions: { site_id: string; reason: string }[];
    site_weights: Record<string, number>;
  } | null;
}

export interface TrainingPoint {
  round: number;
  round_id: string;
  loss: number;
  start_loss: number;
  max_update_norm: number;
  update_norms: Record<string, number>;
  contributing_sites: string[];
  contributed_examples: number;
  candidate_version: string;
}

export interface GateResult {
  gate_id: string;
  outcome: string;
  observed: number | null;
  threshold?: number | null;
  detail: string;
}

/** What a hospital or the coordinator reports about its audit chain. The records stay with it. */
export interface AuditSummary {
  site_id?: string;
  chain_intact: boolean;
  record_count: number;
}

export interface Pilot {
  pilot_id: string;
  model_in_use: ModelVersion;
  rounds: {
    analytics: RoundReport | null;
    evaluation: RoundReport | null;
    training: RoundReport[];
    candidate_validation: RoundReport | null;
  };
  training_curve: TrainingPoint[];
  training_settings: { rounds: number; epochs: number; learning_rate: number; l2: number; initial_version: string };
  candidate: {
    candidate_version: string;
    parent_version: string;
    contributing_sites: string[];
    contributed_examples: number;
    metrics: Metrics;
    status: string;
  } | null;
  release: {
    guard: { permitted: boolean; message: string; plan: { decision: string } };
    gates: {
      action: string;
      gate_results: GateResult[];
      blocking_failures: string[];
      rollback_version: string | null;
    };
  } | null;
  baseline_metrics: Metrics;
  stop_reason: string | null;
  site_audits: Record<string, AuditSummary>;
  coordinator_audit: AuditSummary;
  notice: string;
}

export interface ApproveResult {
  released: ModelVersion;
  /** The version the hospitals used before. */
  previous: string;
}

/** Whether a joining hospital's LMA runs, as the app's applier reports it. */
export type ServiceState = "running" | "stopped" | "not_started" | "unknown";

/** A hospital that is not in the consortium at start and can join while the demo runs. */
export interface JoinableHospital {
  site_id: string;
  display_name: string;
  country: string;
  /** The records it starts with, from the dataset module. The app never reads them. */
  records: number;
  records_source: "dataset" | "empty";
  in_consortium: boolean;
  service: ServiceState;
}

export type DeploymentStepName = "plan" | "start" | "answer" | "attach" | "enrol" | "join" | "check";
export type DeploymentStepStatus = "done" | "skipped" | "failed" | "undone" | "not_run";

/** One step of adding a hospital, and what happened in it. */
export interface DeploymentStep {
  step: DeploymentStepName;
  status: DeploymentStepStatus;
  detail: string;
  seconds: number;
}

/** Whether the GMA's planner selects the new hospital for the next round. */
export interface NextRound {
  selected: boolean;
  /** The planner's reason when it would not select the hospital. */
  reason: string | null;
}

/** One reason core-service gave for not rendering the Compose file. */
export interface DeploymentIssue {
  service: string | null;
  code: string;
  value: string | null;
}

/** One attempt to add a hospital to the running consortium. */
export interface Deployment {
  deployment_id: string;
  kind: "lma";
  site_id: string;
  service: string;
  applier: "process" | "compose";
  status: "joined" | "failed";
  adopted: boolean;
  /** The Compose file core-service rendered, exactly as it came. */
  compose_yaml: string | null;
  issues: DeploymentIssue[];
  /** The seven steps, in the order they run. */
  steps: DeploymentStep[];
  next_round: NextRound | null;
  started_at: string;
  finished_at: string;
}

/** `GET /api/v1/deployments`: whether this server can add a hospital, and what was tried. */
export interface DeploymentsView {
  available: boolean;
  /** Why adding a hospital is not available, in plain words. */
  reason: string | null;
  applier: "off" | "process" | "compose";
  hospitals: JoinableHospital[];
  /** Newest first. */
  deployments: Deployment[];
}
