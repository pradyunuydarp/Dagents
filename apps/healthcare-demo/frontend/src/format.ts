/**
 * Wording and number formats for values the backend returns.
 *
 * These helpers only describe a value in plain words. Every decision they
 * describe (a strategy, a gate outcome, a release action, a priority) arrives
 * from the backend, which asks the planner. Nothing here decides anything.
 */

import type { GateDefinition, OutcomeRateStatus, Store } from "./types";

/** The design system's state tones. Colour only ever marks one of these. */
export type Tone = "permit" | "narrow" | "deny" | "inert";

export function percent(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

export function fixed(value: number | null | undefined, digits = 3): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toFixed(digits);
}

const WORDS = ["No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten"];

/** A small count spelled out to start a sentence ("Three hospitals"). */
export function countWord(value: number): string {
  return WORDS[value] ?? String(value);
}

export function count(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString("en-GB");
}

/** Plain names for the metrics the backend reports. */
const METRIC_LABELS: Record<string, string> = {
  auc: "AUC (how well it ranks patients)",
  sensitivity: "Sensitivity (poor outcomes flagged)",
  specificity: "Specificity (good outcomes not flagged)",
  alerts_per_1000: "Alerts per 1,000 patients",
  subgroup_auc_gap: "AUC gap between women and men",
  log_loss: "Log loss (lower is better)",
  record_count: "Records",
  completeness: "Completeness",
  label_coverage: "Share with an outcome",
  outcome_rate: "Poor outcome rate",
  start_loss: "Loss before training",
  train_loss: "Loss after training"
};

export function metricLabel(key: string): string {
  return METRIC_LABELS[key] ?? key.replace(/_/g, " ");
}

/** Short metric names, for a list of values in one cell or sentence. */
const METRIC_SHORT: Record<string, string> = {
  auc: "AUC",
  sensitivity: "sensitivity",
  specificity: "specificity",
  alerts_per_1000: "alerts per 1,000",
  subgroup_auc_gap: "AUC gap",
  log_loss: "log loss",
  record_count: "records",
  completeness: "completeness",
  label_coverage: "with an outcome",
  outcome_rate: "poor outcome rate",
  start_loss: "loss before training",
  train_loss: "loss after training"
};

export function metricShort(key: string): string {
  return METRIC_SHORT[key] ?? key.replace(/_/g, " ");
}

/** Metrics that are shares of a group, shown as percentages. */
const PERCENT_METRICS = new Set(["sensitivity", "specificity", "completeness", "label_coverage", "outcome_rate"]);

export function formatMetric(key: string, value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  if (PERCENT_METRICS.has(key)) return percent(value, 1);
  if (key === "record_count" || key === "alerts_per_1000") return count(Math.round(value));
  return value.toFixed(3);
}

/** A list of reported values, each with its short name: "records 198, completeness 92.4%". */
export function metricList(metrics: Record<string, number>): string {
  return (
    orderedMetrics(Object.keys(metrics))
      .map((key) => `${metricShort(key)} ${formatMetric(key, metrics[key])}`)
      .join(", ") || "—"
  );
}

/** The order the metric tables use. Unknown metrics follow, alphabetically. */
export function orderedMetrics(keys: string[]): string[] {
  const known = Object.keys(METRIC_LABELS);
  return [
    ...known.filter((key) => keys.includes(key)),
    ...keys.filter((key) => !known.includes(key)).sort()
  ];
}

/**
 * Names for the record's fields, used only until `/api/v1/overview` answers
 * and for fields it does not name. The backend's `field_labels` come from the
 * feature contract, so every page names a field the way the entry form does.
 * The keys also give the order a clinician reads a record in.
 */
const FIELD_LABELS: Record<string, string> = {
  age: "Age",
  age_band: "Age band",
  sex: "Sex",
  conscious_level: "Conscious level",
  face_weakness: "Face weakness",
  arm_weakness: "Arm or hand weakness",
  leg_weakness: "Leg or foot weakness",
  speech_difficulty: "Speech difficulty (dysphasia)",
  hours_since_onset: "Time since symptom onset",
  systolic_bp: "Systolic blood pressure",
  visual_field_loss: "Visual field loss (hemianopia)",
  atrial_fibrillation: "Atrial fibrillation",
  onset_on_waking: "Symptoms noticed on waking",
  stroke_subtype: "Stroke subtype (OCSP)",
  poor_outcome_6m: "Dead or dependent at six months",
  origin: "Where the record came from"
};

/** A field's name in words: the backend's label first, then the fallback list. */
export function fieldLabel(name: string, labels?: Record<string, string> | null): string {
  return labels?.[name] ?? FIELD_LABELS[name] ?? name.replace(/_/g, " ");
}

/** Fields in the order a clinician reads a record; unknown fields follow. */
export function fieldOrder(names: string[]): string[] {
  const known = Object.keys(FIELD_LABELS);
  return [...known.filter((name) => names.includes(name)), ...names.filter((name) => !known.includes(name))];
}

const FIELD_UNITS: Record<string, string> = {
  age: "years",
  hours_since_onset: "h",
  systolic_bp: "mmHg"
};

/** One field's value for a table cell. A missing value is an absent key. */
export function fieldValue(name: string, value: unknown): string {
  if (value === undefined || value === null || value === "") return "—";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "number") {
    const unit = FIELD_UNITS[name];
    const text = Number.isInteger(value) ? String(value) : value.toFixed(1);
    return unit ? `${text} ${unit}` : text;
  }
  if (name === "origin") return value === "entered" ? "entered in the demo" : "trial dataset";
  return String(value);
}

/** One field's running statistics, in a line. */
export function profileSummary(
  name: string,
  field: {
    kind: string;
    numeric: { mean: number; variance: number; min: number; max: number } | null;
    categories: Record<string, number>;
    booleans: Record<string, number>;
  }
): string {
  if (field.numeric) {
    const { mean, variance, min, max } = field.numeric;
    const unit = FIELD_UNITS[name] ? ` ${FIELD_UNITS[name]}` : "";
    const sd = Math.sqrt(Math.max(variance, 0));
    return `mean ${mean.toFixed(1)}${unit}, SD ${sd.toFixed(1)}, range ${min}–${max}`;
  }
  const booleans = Object.entries(field.booleans);
  if (booleans.length > 0) {
    const yes = field.booleans["true"] ?? 0;
    const no = field.booleans["false"] ?? 0;
    return `yes ${yes}, no ${no}`;
  }
  const categories = Object.entries(field.categories).sort((a, b) => b[1] - a[1]);
  if (categories.length > 0) {
    return categories.map(([value, n]) => `${fieldValue(name, value)} ${n}`).join(", ");
  }
  return "—";
}

/**
 * What a protection strategy does, in plain words.
 *
 * The token comes from the planner. This only explains it; the Guard in the
 * backend applies it.
 */
export function strategyText(token: string): string {
  const [name, parameter] = token.split(":", 2);
  switch (name) {
    case "allow_full":
      return "Sent as recorded.";
    case "generalize":
      return `Coarsened before it is sent: numbers are rounded and text is shortened (level ${parameter}; a higher level is coarser).`;
    case "aggregate_only":
      return `Sent only as a summary over at least ${parameter} records, never per patient.`;
    case "clip_contribution":
      return `A model update is scaled down so its length (the L2 norm of the update vector) is at most ${parameter}.`;
    case "add_noise":
      return `Random noise with scale ${parameter} is added before it is sent.`;
    case "redact":
      return "Removed from the answer.";
    case "refuse":
      return "Withheld, because the request was denied.";
    default:
      return "The Guard withholds a strategy it does not recognise.";
  }
}

/**
 * A planner reason in plain words.
 *
 * The planner writes its reasons for logs, such as "cohort_size 5 is below
 * minimum_cohort 20". This rewrites the ones the demo can produce; any other
 * reason is shown as the planner wrote it, so nothing is hidden.
 */
export function plannerReason(text: string): string {
  const cohort = /^cohort_size (\d+) is below minimum_cohort (\d+)$/.exec(text.trim());
  if (cohort) return `The request is about ${cohort[1]} records, fewer than the minimum of ${cohort[2]}.`;
  return text;
}

/**
 * An outcome rate, or the hospital's reason for not reporting one.
 *
 * The reason comes from the backend (`outcome_rate_status`), which repeats the
 * planner's answer. The page never works out a reason from the counts.
 */
export function outcomeRateText(
  rate: number | null,
  status: OutcomeRateStatus | undefined,
  minimumCohort: number
): string {
  if (rate !== null) return percent(rate, 1);
  switch (status) {
    case "no_outcomes":
      return "no outcomes yet";
    case "below_minimum_cohort":
      return `suppressed (fewer than ${minimumCohort} with an outcome)`;
    case "denied":
      return "withheld by the Guard";
    case "withheld":
      return "withheld (the planner could not be asked)";
    default:
      return "not reported";
  }
}

export function guardTone(decision: string, permitted: boolean): Tone {
  if (!permitted) return "deny";
  return decision.toLowerCase() === "permit" ? "permit" : "narrow";
}

export function releaseTone(action: string): Tone {
  if (action === "release") return "permit";
  if (action === "reject") return "deny";
  return "narrow";
}

/** The release action, as words. */
export function releaseText(action: string): string {
  if (action === "release") return "release";
  if (action === "reject") return "reject";
  if (action === "another_round") return "another round";
  return action.replace(/_/g, " ");
}

export function gateTone(outcome: string): Tone {
  if (outcome === "passed") return "permit";
  if (outcome === "failed") return "deny";
  return "narrow";
}

export function priorityTone(priority: string): Tone {
  if (priority === "urgent") return "deny";
  if (priority === "elevated") return "narrow";
  return "inert";
}

/** Plain names for the release gates. Unknown gates show their id. */
const GATE_LABELS: Record<string, string> = {
  discrimination: "Discrimination (AUC)",
  improves_on_current_release: "Improves on the model in use (AUC)",
  subgroup_fairness: "Fairness: AUC gap between women and men",
  sensitivity_at_alert_threshold: "Sensitivity at the alert threshold",
  alert_burden: "Alert burden (alerts per 1,000 patients)"
};

export function gateLabel(gateId: string): string {
  return GATE_LABELS[gateId] ?? gateId.replace(/_/g, " ");
}

/** What a gate requires, read from the gate's own definition. */
export function gateRequirement(gate: GateDefinition | undefined): string {
  if (!gate) return "not stated";
  const { kind, value, margin } = gate.comparison;
  const metric = metricShort(gate.metric);
  if (kind === "at_least" && value !== null) return `${metric} at least ${value}`;
  if (kind === "at_most" && value !== null) return `${metric} at most ${value}`;
  if (kind === "improves_on_baseline") {
    return `${metric} at least the model in use's ${metric}${margin ? ` + ${margin}` : ""}`;
  }
  return `${metric}, ${kind.replace(/_/g, " ")}`;
}

/** Plain names for the Guard's boundaries. */
const BOUNDARY_LABELS: Record<string, string> = {
  before_read: "before reading records",
  before_train: "before training",
  before_send: "before sending a result",
  before_release: "before releasing a model"
};

export function boundaryText(boundary: string): string {
  return BOUNDARY_LABELS[boundary] ?? boundary.replace(/_/g, " ");
}

/** Plain names for the round phases. */
const PHASE_LABELS: Record<string, string> = {
  analytics: "Analytics",
  evaluation: "Evaluation of the model in use",
  training: "Training",
  candidate_validation: "Validation of the candidate"
};

export function phaseLabel(phase: string): string {
  return PHASE_LABELS[phase] ?? phase.replace(/_/g, " ");
}

const PHASE_NOTES: Record<string, string> = {
  analytics: "Each hospital reports counts, completeness and its outcome rate from its running statistics.",
  evaluation: "Each hospital scores its labelled records with the model in use and reports the metrics. These are the baseline.",
  candidate_validation: "Each hospital scores its labelled records with the candidate and reports the metrics."
};

export function phaseNote(phase: string): string {
  return PHASE_NOTES[phase] ?? "";
}

/** Where a hospital's records are stored, in a few words. */
export function storeText(store: Store): string {
  if (store.kind === "postgres") return store.schema ? `Postgres schema ${store.schema}` : "Postgres";
  if (store.kind === "in-memory") return "in memory";
  return store.kind;
}

/**
 * The data source the backend reports, in a sentence.
 *
 * `cohort_source` leaves out a hospital that joined while the demo runs,
 * because such a hospital always keeps its records in memory. Each hospital
 * whose own store differs from that source is named after it, from the store
 * the hospital reports.
 */
export function sourceText(
  source: string | undefined,
  hospitals: { display_name: string; store: Store }[] = []
): string {
  let text: string;
  if (source === "postgres") text = "Stored in Postgres, one schema per hospital.";
  else if (source === "in-memory") text = "Held in memory by the demo server, one store per hospital.";
  else return source ? `Reported by the server as ${source}.` : "Not reported.";
  const where = (store: Store) =>
    store.kind === "in-memory" ? "in memory" : store.kind === "postgres" ? `in ${storeText(store)}` : `in a ${store.kind} store`;
  const others = hospitals.filter((hospital) => hospital.store.kind !== source);
  return others.reduce(
    (sentence, hospital) => `${sentence} ${hospital.display_name} keeps its records ${where(hospital.store)}.`,
    text
  );
}

/** A short form of a long identifier, for narrow cells. The full value goes in a title. */
export function shortId(value: string, keep = 12): string {
  return value.length <= keep ? value : `${value.slice(0, keep)}…`;
}

/** A timestamp in seconds or an ISO string, as a short UTC date and time. */
export function when(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return `${date.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}
