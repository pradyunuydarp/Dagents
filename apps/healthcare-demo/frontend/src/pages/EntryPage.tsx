import { useEffect, useRef, useState, type FormEvent } from "react";

import { ApiError, UnreachableError, isRecorded, newIdempotencyKey, postJson, userMessage } from "../api";
import { SiteCalls } from "../components/ApiCalls";
import { Chip, ErrorNote, Loading, Panel } from "../components/Basics";
import { fieldValue, fixed, priorityTone, storeText } from "../format";
import { useFieldLabel } from "../labels";
import { href } from "../router";
import type { Dashboard, EntryForm, EntryResult, FormField, Overview } from "../types";
import { useResource } from "../useResource";

type Values = Record<string, string>;

/**
 * The request body for the form's values.
 *
 * An empty input is left out, or sent as null for a yes/no finding that could
 * not be assessed. Numbers are sent as typed; the backend checks every type and
 * range and names each field it refuses.
 */
function toBody(fields: FormField[], values: Values): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  for (const field of fields) {
    const raw = (values[field.name] ?? "").trim();
    if (field.type === "boolean") {
      if (raw === "true") body[field.name] = true;
      else if (raw === "false") body[field.name] = false;
      else if (!field.required) body[field.name] = null;
      continue;
    }
    if (raw === "") continue;
    if (field.type === "integer" || field.type === "number") {
      const number = Number(raw);
      body[field.name] = Number.isFinite(number) ? number : raw;
    } else {
      body[field.name] = raw;
    }
  }
  return body;
}

function Field({
  field,
  value,
  error,
  disabled,
  onChange
}: {
  field: FormField;
  value: string;
  error: string | undefined;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const id = `field-${field.name}`;
  const describedBy = [field.help ? `${id}-help` : "", error ? `${id}-error` : ""].filter(Boolean).join(" ");
  // The unit goes in brackets unless the label already names it.
  const showUnit = field.unit !== null && !field.label.toLowerCase().includes(field.unit.toLowerCase());
  const label = (
    <>
      {field.label}
      {showUnit ? ` (${field.unit})` : ""}
      {field.required ? <span className="required"> required</span> : <span className="optional"> optional</span>}
    </>
  );

  let control;
  if (field.type === "boolean") {
    control = (
      <fieldset className="choice-row" aria-describedby={describedBy || undefined} disabled={disabled}>
        <legend className="field-label">{label}</legend>
        {[
          { value: "true", text: "Yes" },
          { value: "false", text: "No" },
          ...(field.required ? [] : [{ value: "", text: "Not assessed" }])
        ].map((option) => (
          <label key={option.value} className="ds-check">
            <input
              type="radio"
              name={field.name}
              value={option.value}
              checked={value === option.value}
              onChange={() => onChange(option.value)}
            />
            {option.text}
          </label>
        ))}
      </fieldset>
    );
  } else if (field.type === "choice") {
    control = (
      <label className="ds-field" htmlFor={id}>
        <span className="field-label">{label}</span>
        <select
          id={id}
          name={field.name}
          className="ds-input"
          value={value}
          disabled={disabled}
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy || undefined}
          onChange={(event) => onChange(event.target.value)}
        >
          <option value="">{field.required ? "Choose…" : "Not recorded"}</option>
          {(field.options ?? []).map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      </label>
    );
  } else {
    control = (
      <label className="ds-field" htmlFor={id}>
        <span className="field-label">{label}</span>
        <input
          id={id}
          name={field.name}
          className="ds-input"
          type="number"
          inputMode={field.type === "integer" ? "numeric" : "decimal"}
          step={field.type === "integer" ? 1 : "any"}
          min={field.min ?? undefined}
          max={field.max ?? undefined}
          value={value}
          disabled={disabled}
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy || undefined}
          onChange={(event) => onChange(event.target.value)}
        />
      </label>
    );
  }

  return (
    <div className={`form-field${error ? " has-error" : ""}`} data-field={field.name}>
      {control}
      {field.min !== null && field.max !== null && (
        <p className="field-hint">
          From {field.min} to {field.max}
          {field.unit ? ` ${field.unit}` : ""}.
        </p>
      )}
      {field.help && (
        <p className="field-hint" id={`${id}-help`}>
          {field.help}
        </p>
      )}
      {error && (
        <p className="field-error" id={`${id}-error`} role="alert">
          {field.label} {error}.
        </p>
      )}
    </div>
  );
}

/** What the page shows once the record is stored. */
function Saved({
  result,
  form,
  dashboard,
  siteId,
  onAnother
}: {
  result: EntryResult;
  form: EntryForm;
  dashboard: Dashboard | null;
  siteId: string;
  onAnother: () => void;
}) {
  const label = useFieldLabel();
  const record = result.record;
  const shown = form.fields.map((field) => field.name);
  return (
    <Panel hook="entry-result" eyebrow="Stored" title={`Record ${record.encounter_id} saved`}>
      <div
        id="entry-saved"
        className="verdict ds-verdict ds-verdict--permit"
        data-encounter={record.encounter_id}
        tabIndex={-1}
      >
        <strong>SAVED</strong>
        <span className="verdict-metrics">
          <span>record {record.encounter_id}</span>
          <span>{form.display_name}</span>
        </span>
      </div>

      <div className="entry-columns">
        <div>
          <h3>The record</h3>
          <table className="ds-table record">
            <tbody>
              {[...shown, "age_band"].map((name) => (
                <tr key={name}>
                  <th scope="row" data-field={name}>
                    {label(name)}
                  </th>
                  <td>{fieldValue(name, record[name])}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div>
          <h3>Its assessment</h3>
          <p className="ds-row">
            <Chip tone={priorityTone(result.assessment.priority)}>
              {result.assessment.suppressed ? "not assessed" : result.assessment.priority}
            </Chip>
            <span className="ds-mono">score {fixed(result.assessment.score, 3)}</span>
          </p>
          <p className="ds-note">Scored by {result.assessment.model_version}, the model in use at this hospital.</p>
          {result.assessment.basis.length > 0 && (
            <ul className="reasons ds-reasons">
              {result.assessment.basis.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          )}
          {result.assessment.warnings.map((line) => (
            <p key={line} className="assessment-warning">
              <strong>Note:</strong> {line}
            </p>
          ))}
        </div>
      </div>

      <h3>What happens next</h3>
      <ol className="next-steps">
        <li>
          The record is stored only in {form.display_name}&rsquo;s store
          {dashboard ? ` (${storeText(dashboard.store)})` : ""}. The coordinator and the other hospitals
          cannot read it.
        </li>
        <li>
          The hospital&rsquo;s LMA reads the store again, so its running statistics count the record. The
          store now holds {result.counts.total} records, of which {result.counts.entered}{" "}
          {result.counts.entered === 1 ? "was" : "were"} entered in the demo.
        </li>
        <li>It is counted in this hospital&rsquo;s statistics and charts, and ranked in its worklist.</li>
        <li>
          The next pilot trains and evaluates on it once its six-month outcome is recorded. Record the
          outcome on the hospital page.
        </li>
      </ol>
      <SiteCalls calls={result.framework_calls} siteId={siteId} label="Dagents API calls after saving the record" />
      <p className="ds-row">
        <a className="ds-btn ds-btn--primary" href={href(`hospitals/${siteId}`)}>
          Back to {form.display_name}
        </a>
        <button type="button" className="ds-btn" onClick={onAnother}>
          Add another record
        </button>
      </p>
    </Panel>
  );
}

/**
 * The data-entry page for one hospital.
 *
 * The form is built from the backend's definition, so the browser restates no
 * rule: the backend checks every value and names each field it refuses.
 */
export function EntryPage({
  siteId,
  overview,
  onSaved
}: {
  siteId: string;
  overview: Overview | null;
  onSaved: () => void;
}) {
  const base = `/api/v1/hospitals/${encodeURIComponent(siteId)}`;
  const form = useResource<EntryForm>(`${base}/entry-form`);
  const dashboard = useResource<Dashboard>(base);
  const [values, setValues] = useState<Values>({});
  // One key per submission attempt. A retry of the same values reuses it, so a
  // request the network lost and the browser sent again is stored once.
  const [pendingKey, setPendingKey] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(null);
  // The backend's reasons, by field. A field's reason goes when its value changes.
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [result, setResult] = useState<EntryResult | null>(null);
  const errorRef = useRef<HTMLDivElement>(null);
  // Where focus goes after a submission, set when the answer arrives. The
  // effect runs after React has drawn the answer and enabled the controls
  // again; focusing a control that is still disabled does nothing.
  const [focusNext, setFocusNext] = useState<{ to: "saved" | "refused"; at: number } | null>(null);

  useEffect(() => {
    if (focusNext === null) return;
    if (focusNext.to === "saved") {
      document.getElementById("entry-saved")?.focus();
      return;
    }
    const first = document.querySelector<HTMLElement>(".form-field.has-error input, .form-field.has-error select");
    (first ?? errorRef.current)?.focus();
  }, [focusNext]);

  // Never the raw site id: during a cold start the name is not known yet.
  const name =
    form.data?.display_name ?? overview?.hospitals.find((hospital) => hospital.site_id === siteId)?.display_name ?? null;
  const formErrors = Object.entries(fieldErrors).filter(
    ([field]) => !(form.data?.fields ?? []).some((item) => item.name === field)
  );

  const change = (field: string, value: string) => {
    setValues((before) => ({ ...before, [field]: value }));
    // Different values are a different record, so they get a new key.
    setPendingKey(null);
    if (field in fieldErrors) {
      const rest = { ...fieldErrors };
      delete rest[field];
      setFieldErrors(rest);
      // Once every named field has changed, the summary above the button goes too.
      if (Object.keys(rest).length === 0) setError(null);
    }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!form.data || isRecorded) return;
    const key = pendingKey ?? newIdempotencyKey();
    setPendingKey(key);
    setSubmitting(true);
    setError(null);
    setFieldErrors({});
    try {
      const saved = await postJson<EntryResult>(`${base}/encounters`, toBody(form.data.fields, values), {
        [form.data.idempotency.header]: key
      });
      setResult(saved);
      setPendingKey(null);
      onSaved();
      form.reload();
      // The form is replaced by the result. Focus moves to it, which also
      // scrolls it into view, so a keyboard user does not start again at the
      // top of the page.
      setFocusNext({ to: "saved", at: Date.now() });
    } catch (exc) {
      setError(exc);
      setFieldErrors(exc instanceof ApiError ? exc.fieldErrors : {});
      // Keep the key only when the server may not have seen the request.
      if (!(exc instanceof UnreachableError)) setPendingKey(null);
      // The controls were disabled while the request ran, so the focused
      // button lost focus. Move it to the first refused field, or to the
      // error summary when no field was named.
      setFocusNext({ to: "refused", at: Date.now() });
    } finally {
      setSubmitting(false);
    }
  };

  const another = () => {
    setResult(null);
    setValues({});
    setError(null);
    setFieldErrors({});
    window.scrollTo(0, 0);
  };

  return (
    <div className="page">
      <header className="page-head">
        <p className="ds-eyebrow">
          <a href={href(`hospitals/${siteId}`)}>{name ?? "Hospital view"}</a> · Data entry
        </p>
        <h1>Add a record at {name ?? "this hospital"}</h1>
        <p className="lede ds-measure">
          Enter one patient&rsquo;s findings at presentation. The record is stored in this
          hospital&rsquo;s store, scored by the model in use, and counted in the hospital&rsquo;s running
          statistics.
        </p>
      </header>

      {result && form.data ? (
        <Saved result={result} form={form.data} dashboard={dashboard.data} siteId={siteId} onAnother={another} />
      ) : (
        <Panel hook="entry" eyebrow="Fields from the feature contract" title="New record">
          <ErrorNote error={form.error} />
          {form.data ? (
            <form className="entry-form" onSubmit={submit} noValidate>
              {!isRecorded && (
                <p className="banner notice" role="note">
                  <strong>Notice.</strong> {form.data.notice}
                </p>
              )}
              {isRecorded && (
                <p className="banner recorded">
                  <strong>Replay.</strong> This page replays a recorded run. A recording cannot store new
                  records, so the form is turned off. Run the demo locally to enter data.
                </p>
              )}
              {!isRecorded && form.data.remaining === 0 && (
                <p className="banner error">
                  This hospital already has {form.data.max_entered} entered records, the most the demo
                  accepts. No more records can be entered here.
                </p>
              )}
              <div className="form-grid">
                {form.data.fields.map((field) => (
                  <Field
                    key={field.name}
                    field={field}
                    value={values[field.name] ?? ""}
                    error={fieldErrors[field.name]}
                    disabled={isRecorded || submitting}
                    onChange={(value) => change(field.name, value)}
                  />
                ))}
              </div>
              {error !== null && (
                <div className="banner error" role="alert" data-form-error ref={errorRef} tabIndex={-1}>
                  {userMessage(error)}
                  {formErrors.length > 0 && (
                    <ul>
                      {formErrors.map(([field, reason]) => (
                        <li key={field}>
                          {field} {reason}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
              <div className="form-actions ds-row">
                <button
                  type="submit"
                  className="ds-btn ds-btn--primary"
                  disabled={isRecorded || submitting || form.data.remaining === 0}
                >
                  {submitting ? "Saving…" : "Save the record"}
                </button>
                <a className="ds-btn" href={href(`hospitals/${siteId}`)}>
                  Cancel
                </a>
                <span className="ds-note">
                  {form.data.remaining} of {form.data.max_entered} entries left at this hospital.
                </span>
              </div>
              <SiteCalls calls={form.data.framework_calls} siteId={siteId} label="Dagents API calls for this form" />
            </form>
          ) : (
            !form.error && <Loading what="the form" />
          )}
        </Panel>
      )}
    </div>
  );
}
