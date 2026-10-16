import { count, outcomeRateText, sourceText, storeText } from "../format";
import { href } from "../router";
import type { FrameworkStatus, Overview } from "../types";
import { ApiCalls } from "./ApiCalls";
import { Panel } from "./Basics";

/**
 * The hospitals, as the coordinator sees them: only the numbers each hospital
 * reports. No patient rows reach this page.
 */
export function HospitalsTable({ overview, status }: { overview: Overview; status: FrameworkStatus | null }) {
  return (
    <Panel hook="consortium" eyebrow="What each hospital reports" title="The hospitals">
      <dl className="ds-facts">
        <div>
          <dt>Model in use</dt>
          <dd>{overview.current_model.version}</dd>
        </div>
        <div>
          <dt>Feature contract</dt>
          <dd>{overview.feature_contract.version}</dd>
        </div>
        <div>
          <dt>Minimum cohort</dt>
          <dd>{overview.minimum_cohort} records</dd>
        </div>
        <div>
          <dt>Records</dt>
          <dd className="plain">{sourceText(status?.cohort_source)}</dd>
        </div>
      </dl>
      <div className="ds-table-scroll">
        <table className="ds-table hospitals">
          <thead>
            <tr>
              <th scope="col">Hospital</th>
              <th scope="col" className="num">
                Records
              </th>
              <th scope="col" className="num wide-only">
                Entered
              </th>
              <th scope="col" className="num">
                With outcome
              </th>
              <th scope="col">Poor outcome rate</th>
              <th scope="col" className="wide-only">
                Where the data is stored
              </th>
            </tr>
          </thead>
          <tbody>
            {overview.hospitals.map((hospital) => (
              <tr key={hospital.site_id} data-site={hospital.site_id}>
                <td>
                  <a href={href(`hospitals/${hospital.site_id}`)}>{hospital.display_name}</a>
                  <span className="cell-sub">{hospital.country}</span>
                  <span className="cell-sub narrow-only">{storeText(hospital.store)}</span>
                </td>
                {hospital.record_count === 0 ? (
                  // An empty store reports nothing to count, so the row says so
                  // instead of showing zeros and a suppressed rate.
                  <td colSpan={5} className="empty-store" data-empty-store>
                    Its store holds no records yet.
                    <span className="cell-sub wide-only">{storeText(hospital.store)}</span>
                  </td>
                ) : (
                  <>
                    <td className="num">
                      {count(hospital.record_count)}
                      <span className="cell-sub narrow-only">{count(hospital.entered_count)} entered</span>
                    </td>
                    <td className="num wide-only">{count(hospital.entered_count)}</td>
                    <td className="num">{count(hospital.labelled_count)}</td>
                    <td data-rate-status={hospital.outcome_rate_status ?? "unknown"}>
                      <span className={hospital.outcome_rate === null ? "suppressed" : "ds-mono"}>
                        {outcomeRateText(hospital.outcome_rate, hospital.outcome_rate_status, hospital.minimum_cohort)}
                      </span>
                    </td>
                    <td className="wide-only">
                      <span className="ds-mono">{storeText(hospital.store)}</span>
                    </td>
                  </>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {overview.hospitals.length > 0 && overview.hospitals.every((hospital) => hospital.record_count === 0) && (
        <p className="ds-note empty">No hospital has records in its store yet.</p>
      )}
      <p className="note ds-note">
        Each hospital counts its own records and sends only these totals. A hospital reports its
        poor outcome rate only when its Guard&rsquo;s planner permits it: at least{" "}
        {overview.minimum_cohort} of its records must have a recorded outcome, so a small group
        cannot point to one patient. When a rate is not reported, the table gives the
        hospital&rsquo;s reason. Poor outcome means dead or dependent six months after the stroke.
      </p>
      <p className="note ds-note">
        The records, the records with an outcome and the rate come from each hospital&rsquo;s LMA, which
        reports them to the GMA. The entered count and where the data is stored come from the app at
        each hospital.
      </p>
      <ApiCalls calls={overview.framework_calls} />
    </Panel>
  );
}
