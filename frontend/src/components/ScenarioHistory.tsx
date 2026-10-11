import type { ScenarioRecord } from "../ui/workspace";
import { HISTORY_LIMIT } from "../ui/workspace";
import { pct, usd } from "../format";

export default function ScenarioHistory({
  history,
  selectedId,
  analysisId,
  onSelect,
}: {
  history: ScenarioRecord[];
  selectedId: string | null;
  analysisId: string;
  onSelect: (record: ScenarioRecord) => void;
}) {
  if (!history.length) return null;
  return (
    <details className="scenario-history" open={history.length > 1}>
      <summary>
        Scenario history & comparison <span>{history.length} runs</span>
      </summary>
      <div className="table-scroll">
        <table className="workspace-table">
          <thead>
            <tr>
              <th>Scenario</th>
              <th>Inputs</th>
              <th>Failure probability</th>
              <th>Expected loss</th>
              <th>Exit cost</th>
              <th>Withdrawable</th>
              <th>Model result</th>
            </tr>
          </thead>
          <tbody>
            {history.map((record) => {
              const r = record.result;
              const overridden =
                r.decision.probability_source === "override" ||
                r.exit.override !== undefined;
              return (
                <tr
                  key={record.id}
                  className={record.id === selectedId ? "current-row" : ""}
                >
                  <td>
                    <button
                      className="history-pick"
                      onClick={() => onSelect(record)}
                      aria-pressed={record.id === selectedId}
                    >
                      {r.shock.mode_label}
                    </button>
                    <small>{r.shock.node_label}</small>
                    <small>{new Date(record.createdAt).toLocaleString()}</small>
                    {record.analysisId !== analysisId && (
                      <span className="snapshot-tag">Earlier snapshot</span>
                    )}
                  </td>
                  <td>
                    {usd(r.position.size_usd)}
                    <small>
                      {record.horizonDays} days · {r.shock.alert_label}
                    </small>
                    {overridden && <small>User overrides</small>}
                  </td>
                  <td>{pct(r.decision.probability)}</td>
                  <td>{usd(r.decision.expected_loss_usd)}</td>
                  <td>{usd(r.decision.exit_cost_usd)}</td>
                  <td>{pct(r.exit.instant_fraction, 0)}</td>
                  <td>
                    <span
                      className={`decision-tag ${r.decision.verdict.toLowerCase()}`}
                    >
                      {r.decision.verdict}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="history-note">
        Last {HISTORY_LIMIT} runs saved locally. Compare the recorded size,
        horizon, signal and snapshot; different inputs are not like-for-like.
      </p>
    </details>
  );
}
