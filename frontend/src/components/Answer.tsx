import { useState } from "react";
import ReactMarkdown from "react-markdown";
import {
  Bar,
  BarChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { ArrowRight, GitBranch, Sparkles, X } from "lucide-react";
import { api } from "../api";
import type { Explanation, ShockResult } from "../types";
import { TYPE_META, lossColor, pct, usd } from "../format";
import { exitAmounts } from "../ui/workspace";

type ResultTab = "summary" | "distribution" | "exit" | "assumptions";
function Gauge({
  p,
  ci,
  be,
}: {
  p: number;
  ci: [number, number];
  be: number | null;
}) {
  const scale = (x: number) => `${Math.min(100, Math.max(0, x * 100))}%`;
  return (
    <div className="gauge">
      <div className="gauge-labels">
        <span>
          Modeled probability <b>{pct(p)}</b>
        </span>
        <span>
          Break-even <b>{be === null || be > 1 ? "Not reached" : pct(be)}</b>
        </span>
      </div>
      <div className="gauge-track">
        <div
          className="gauge-ci"
          style={{
            left: scale(ci[0]),
            width: `calc(${scale(ci[1])} - ${scale(ci[0])})`,
          }}
        />
        {be !== null && be <= 1 && (
          <div className="gauge-be" style={{ left: scale(be) }} />
        )}
        <div className="gauge-p" style={{ left: scale(p) }} />
      </div>
      <div className="gauge-axis">
        <span>0%</span>
        <span>
          90% modeled range: {pct(ci[0])} – {pct(ci[1])}
        </span>
        <span>100%</span>
      </div>
    </div>
  );
}
export default function Answer({
  r,
  onClose,
  onInspectPath,
  canInspectPath = true,
}: {
  r: ShockResult;
  onClose: () => void;
  onInspectPath: () => void;
  canInspectPath?: boolean;
}) {
  const [tab, setTab] = useState<ResultTab>("summary");
  // Kept with the result it was written for, so switching saved scenarios drops it.
  const [ai, setAi] = useState<{ for: ShockResult; summary: Explanation } | null>(null);
  const [aiBusy, setAiBusy] = useState<ShockResult | null>(null);
  const [aiError, setAiError] = useState<{ for: ShockResult; message: string } | null>(null);
  const summary = ai?.for === r ? ai.summary : null;
  const generateSummary = async () => {
    setAiBusy(r);
    setAiError(null);
    try {
      setAi({ for: r, summary: await api.explain(r) });
    } catch (e) {
      setAiError({ for: r, message: e instanceof Error ? e.message : "Summary request failed." });
    } finally {
      setAiBusy(null);
    }
  };
  const d = r.decision,
    exit = d.verdict === "EXIT";
  const hist = r.loss.histogram.map((b) => ({
    x: (b.x0 + b.x1) / 2,
    share: b.share,
  }));
  const path = r.paths[0]?.hops ?? [];
  const amounts = exitAmounts(r);
  const [costLow, costHigh] = r.exit.cost_pct_ci.map((c) =>
    usd(c * r.position.size_usd),
  );
  const costRange =
    costLow === costHigh ? costLow : `${costLow} – ${costHigh}`;
  return (
    <section className="answer">
      <div className="result-heading">
        <div>
          <span className="eyebrow">
            {r.shock.incident ? "Historical replay" : "Simulated scenario"}
          </span>
          <h2>{r.shock.mode_label}</h2>
          <p>
            {r.shock.node_label} · {r.shock.alert_label}
            {r.shock.incident
              ? ` · ${r.shock.incident.name} (${r.shock.incident.date})`
              : ""}
          </p>
        </div>
        <button
          className="icon-button"
          aria-label="Clear scenario result"
          title="Clear result"
          onClick={onClose}
        >
          <X size={18} />
        </button>
      </div>
      <div className="answer-grid">
        <div className={`verdict ${exit ? "exit" : "stay"}`}>
          <span className="v-label">Model recommendation</span>
          <b className="v-main">{d.verdict}</b>
          <p>
            {d.reasons[0] ||
              "Based on this scenario's loss and exit assumptions."}
          </p>
          <span className="v-conf">Simulation only · No trade executed</span>
        </div>
        <div className="metrics">
          <div className="m">
            <small>Expected loss if staying</small>
            <b>{usd(d.expected_loss_usd)}</b>
            <em>{pct(d.expected_loss_pct, 2)} of position</em>
          </div>
          <div className="m">
            <small>Estimated exit cost</small>
            <b>{usd(d.exit_cost_usd)}</b>
            <em>
              {pct(d.exit_cost_pct, 2)}
              {r.exit.override !== undefined ? " · override" : ""}
            </em>
          </div>
          <div className="m">
            <small>Withdrawable in scenario</small>
            <b>{pct(r.exit.instant_fraction, 0)}</b>
            <em>Under this scenario</em>
          </div>
          <div className="m">
            <small>Loss if failure occurs</small>
            <b>{pct(d.loss_given_failure.typical)}</b>
            <em>Typical · bad case {pct(d.loss_given_failure.bad_case)}</em>
          </div>
        </div>
      </div>
      <div className="execution-summary">
        <div>
          <small>Immediately withdrawable</small>
          <b>{usd(amounts.withdrawable)}</b>
        </div>
        <div>
          <small>Remaining exposure after immediate exit</small>
          <b>{usd(amounts.remaining)}</b>
        </div>
        <div>
          <small>Estimated cost for modeled exit route</small>
          <b>
            {r.exit.override !== undefined
              ? `${usd(d.exit_cost_usd)} · override`
              : costRange}
          </b>
        </div>
      </div>
      {exit && amounts.remaining > 0 && (
        <p className="execution-note">
          Only a partial immediate exit is available in this scenario. The
          remaining {usd(amounts.remaining)} stays exposed; review the exit plan
          for constraints.
        </p>
      )}
      <div className="result-tabs" role="tablist" aria-label="Result details">
        {(
          [
            ["summary", "Summary"],
            ["distribution", "Loss analysis"],
            ["exit", "Exit plan"],
            ["assumptions", "Assumptions"],
          ] as [ResultTab, string][]
        ).map(([id, label]) => (
          <button
            key={id}
            role="tab"
            aria-selected={tab === id}
            className={tab === id ? "active" : ""}
            onClick={() => setTab(id)}
          >
            {label}
          </button>
        ))}
      </div>
      {tab === "summary" && (
        <div className="answer-cols">
          <div className="card">
            <div className="card-h">Failure path</div>
            <div className="failure-path">
              {path.map((h, i) => (
                <div key={h.node_id} className="hop">
                  {i > 0 && (
                    <div className="hop-edge">
                      {h.edge_type?.replace(/_/g, " ")}
                    </div>
                  )}
                  <div
                    className="hop-node"
                    style={{ borderColor: TYPE_META[h.type]?.color }}
                  >
                    <span title={h.label}>{h.label}</span>
                    <b style={{ color: lossColor(h.mean_loss) }}>
                      {h.mean_loss > 0.0001 ? `−${pct(h.mean_loss)}` : "0"}
                    </b>
                  </div>
                </div>
              ))}
            </div>
            <button
              className="text-button"
              disabled={!canInspectPath}
              title={
                !canInspectPath
                  ? "This result belongs to an earlier analysis snapshot."
                  : undefined
              }
              onClick={onInspectPath}
            >
              <GitBranch size={14} />
              Inspect in graph <ArrowRight size={13} />
            </button>
          </div>
          <div className="card explain">
            <div className="card-h">Why this recommendation</div>
            <ul className="assume">
              {d.reasons.map((x, i) => (
                <li key={i}>{x}</li>
              ))}
            </ul>
            <div className="ai-summary">
              {summary ? (
                <>
                  <span className="muted small">
                    {summary.provider === "template"
                      ? "Rule-based summary"
                      : `AI summary · ${summary.provider} · ${summary.model ?? ""}`}
                  </span>
                  {summary.provider === "template" && (
                    <p className="execution-note">
                      {summary.error
                        ? `The LLM request failed (${summary.error}), so the rule-based summary is shown.`
                        : "No LLM API key is configured on the backend, so the rule-based summary is shown."}
                    </p>
                  )}
                  <div className="md">
                    <ReactMarkdown>{summary.text}</ReactMarkdown>
                  </div>
                </>
              ) : (
                <button
                  className="btn subtle"
                  onClick={() => void generateSummary()}
                  disabled={aiBusy === r}
                  title="Asks the LLM to explain this result. Numbers and the verdict come from the model, not the LLM."
                >
                  <Sparkles size={14} />
                  {aiBusy === r ? "Writing summary…" : "Generate AI summary"}
                </button>
              )}
              {aiError?.for === r && (
                <p className="execution-note" role="alert">
                  {aiError.message}
                </p>
              )}
            </div>
            {r.explanation && (
              <details className="narrative">
                <summary>Rule-based explanation</summary>
                <div className="md">
                  <ReactMarkdown>{r.explanation.text}</ReactMarkdown>
                </div>
              </details>
            )}
          </div>
        </div>
      )}
      {tab === "distribution" && (
        <div className="result-detail">
          <div className="detail-metrics">
            <div className="m">
              <small>
                Failure probability / {r.probability.horizon_days} days
              </small>
              <b>{pct(d.probability)}</b>
              <em>
                {d.probability_source === "override"
                  ? "User override"
                  : "Model estimate"}
              </em>
            </div>
            <div className="m">
              <small>Portfolio impact</small>
              <b>{pct(d.book_impact_pct, 3)}</b>
              <em>Position: {pct(d.position_share_of_book, 0)} of portfolio</em>
            </div>
            <div className="m">
              <small>Bad-case loss / p95</small>
              <b>{usd(d.bad_case_usd)}</b>
              <em>{pct(d.loss_given_failure.bad_case)} of position</em>
            </div>
          </div>
          {d.probability_source === "override" ? (
            <p className="execution-note">
              The decision uses your fixed probability override of{" "}
              {pct(d.probability)}. The original model range is{" "}
              {pct(r.probability.p_ci[0])} – {pct(r.probability.p_ci[1])}.
            </p>
          ) : (
            <Gauge
              p={d.probability}
              ci={r.probability.p_ci}
              be={d.break_even_probability}
            />
          )}
          <div className="card">
            <div className="card-h">Loss distribution if failure occurs</div>
            <div className="loss-chart">
              <ResponsiveContainer>
                <BarChart
                  data={hist}
                  margin={{ top: 20, right: 20, left: 0, bottom: 0 }}
                >
                  <XAxis
                    dataKey="x"
                    tickFormatter={(v) => pct(v, 0)}
                    stroke="#817b73"
                    fontSize={11}
                  />
                  <YAxis
                    tickFormatter={(v) => pct(v, 0)}
                    stroke="#817b73"
                    fontSize={11}
                  />
                  <Tooltip
                    formatter={(v: number) => pct(v)}
                    labelFormatter={(v: number) => `Loss ≈ ${pct(v)}`}
                    contentStyle={{
                      background: "#fff",
                      border: "1px solid #e4e0d9",
                      borderRadius: 5,
                      fontSize: 12,
                    }}
                  />
                  <Bar dataKey="share" fill="#a46a5e" radius={[3, 3, 0, 0]} />
                  {hist.length > 0 && (
                    <ReferenceLine
                      x={
                        hist.reduce((a, b) =>
                          Math.abs(b.x - d.loss_given_failure.mean) <
                          Math.abs(a.x - d.loss_given_failure.mean)
                            ? b
                            : a,
                        ).x
                      }
                      stroke="#8d733d"
                      strokeDasharray="4 3"
                      label={{
                        value: "Mean",
                        fill: "#8d733d",
                        fontSize: 11,
                        position: "top",
                      }}
                    />
                  )}
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>
      )}
      {tab === "exit" && (
        <div className="answer-cols">
          <div className="card">
            <div className="card-h">Estimated exit route</div>
            {r.exit.steps.map((s, i) => (
              <div key={i} className="exit-step">
                <b>{s.step}</b>
                <span className={`src src-${s.source}`}>{s.source}</span>
                <p>{s.detail}</p>
                <em>{pct(s.cost_pct, 2)}</em>
              </div>
            ))}
          </div>
          <div className="card">
            <div className="card-h">Break-even probability</div>
            <p>
              {d.break_even_probability === null || d.break_even_probability > 1
                ? "Exit does not break even under these assumptions."
                : `Exit becomes economical above a failure probability of ${pct(d.break_even_probability)}.`}
            </p>
            <table className="be">
              <thead>
                <tr>
                  <th>Loss if failure occurs</th>
                  <th>Exit above probability</th>
                </tr>
              </thead>
              <tbody>
                {d.break_even_table.map((b) => (
                  <tr key={b.loss_if_fails}>
                    <td>{pct(b.loss_if_fails, 0)}</td>
                    <td>
                      {b.exit_above === null || b.exit_above > 1
                        ? "Not reached"
                        : pct(b.exit_above)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      {tab === "assumptions" && (
        <div className="answer-cols">
          <div className="card">
            <div className="card-h">Decision calculation</div>
            <p className="mono">
              {pct(d.probability)} × {pct(d.loss_given_failure.mean)}
              {d.avoidable_loss_pct !== undefined ? (
                <> × {pct(d.instant_fraction)} withdrawable</>
              ) : null}{" "}
              = {pct(d.avoidable_loss_pct ?? d.expected_loss_pct, 2)}{" "}
              {exit ? ">" : "≤"} {pct(d.exit_cost_pct, 2)} exit cost
            </p>
            {d.avoidable_loss_pct !== undefined && (
              <p className="execution-note">
                An immediate exit can avoid {usd(d.avoidable_loss_usd)} of the{" "}
                {usd(d.expected_loss_usd)} expected loss. The portion that
                cannot be withdrawn stays exposed.
              </p>
            )}
            <p>
              {d.probability_source === "override"
                ? "This recommendation uses a fixed user-supplied probability. It does not provide a calibrated confidence in the decision."
                : `${pct(d.confidence, 0)} of probability draws produce this recommendation. This measures agreement within the model, not the chance the recommendation is correct.`}
            </p>
            <div className="card-h">Probability assumptions</div>
            <ul className="assume">
              {r.probability.reasons.map((x, i) => (
                <li key={i}>{x}</li>
              ))}
            </ul>
          </div>
          <div className="card">
            <div className="card-h">Model & execution assumptions</div>
            <ul className="assume">
              {r.assumptions.map((x, i) => (
                <li key={i}>{x}</li>
              ))}
            </ul>
            {r.findings?.length > 0 && (
              <>
                <div className="card-h">Scenario findings</div>
                <ul className="assume">
                  {r.findings.map((x, i) => (
                    <li key={i}>{x}</li>
                  ))}
                </ul>
              </>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
