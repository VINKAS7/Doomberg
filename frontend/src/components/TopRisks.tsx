import { useState } from "react";
import type { Analysis } from "../types";
import { TYPE_META, pct, riskColor, usd } from "../format";
import LiveMonitor from "./LiveMonitor";

export default function TopRisks({
  analysis,
  onPick,
}: {
  analysis: Analysis;
  onPick: (node: string, mode: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const s = analysis.screening;
  const size = analysis.request.size_usd;
  return (
    <div className="panel-section">
      <LiveMonitor analysis={analysis} onPick={onPick} />
      <div className="sub-h" style={{ marginTop: 0 }}>
        Modeled risk contributors
      </div>
      <div className="risk-share-note">
        Share of modeled risk · not failure probability
      </div>
      <details className="risk-baseline">
        <summary>Baseline estimates</summary>
        <div className="kpis">
          <div>
            <small>Expected loss / {s.horizon_days}d</small>
            <b>{pct(s.total_expected_loss_pct, 2)}</b>
            <em>{usd(s.total_expected_loss_pct * size)}</em>
          </div>
          <div>
            <small>P(any loss event)</small>
            <b>{pct(s.p_any_loss_event)}</b>
            <em>within {s.horizon_days} days</em>
          </div>
        </div>
      </details>
      <div className="risk-list">
        {(expanded ? s.top : s.top.slice(0, 3)).map((r) => (
          <button
            key={r.node_id + r.mode}
            className="risk-row"
            onClick={() => onPick(r.node_id, r.mode)}
            title={`Share of modeled risk, not failure probability. ${r.reasons.join("\n")}`}
          >
            <div className="risk-top">
              <span
                className="dot"
                style={{ background: TYPE_META[r.type]?.color }}
              />
              <b>{r.label}</b>
              <span
                className="risk-share"
                style={{ color: riskColor(r.share) }}
              >
                {pct(r.share, 0)}
              </span>
            </div>
            <div className="risk-meta">{r.mode_label}</div>
            <div className="bar">
              <i
                style={{
                  width: `${Math.min(100, r.share * 100)}%`,
                  background: riskColor(r.share),
                }}
              />
            </div>
          </button>
        ))}
      </div>
      {s.top.length > 3 && (
        <button className="text-button" onClick={() => setExpanded(!expanded)}>
          {expanded ? "Show fewer" : "Show all contributors"}
        </button>
      )}
    </div>
  );
}
