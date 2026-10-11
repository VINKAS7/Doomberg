import { useEffect, useMemo, useState } from "react";
import { Zap, History, ExternalLink, Loader2 } from "lucide-react";
import type { Analysis, Catalog } from "../types";
import { TYPE_META, pct, short, usd } from "../format";
import EvidenceBadge from "./EvidenceBadge";
import { evidenceSource, evidenceSummary } from "../ui/workspace";

type ShockReq = {
  node_id: string;
  mode: string;
  alert: string;
  probability_override: number | null;
  exit_cost_override: number | null;
};

const SHOW_ATTRS: Record<string, [string, (v: any) => string][]> = {
  market: [
    ["supply_usd", usd],
    ["borrow_usd", usd],
    ["liquidity_usd", usd],
    ["utilization", pct],
    ["lltv", pct],
    ["supply_apy", pct],
    ["age_days", (v) => `${v} days`],
    ["bad_debt_usd", usd],
  ],
  oracle: [
    ["oracle_type", String],
    ["tracking_reason", String],
    ["feed_descriptions", (v) => v?.join(" × ")],
  ],
  feed: [
    ["answer", (v) => (v ?? "n/a").toLocaleString()],
    [
      "age_s",
      (v) => (v != null ? `${Math.round(v / 60)} min at snapshot` : "n/a"),
    ],
    ["stale", (v) => (v ? "YES" : "no")],
    ["nav_feed", (v) => (v ? "issuer NAV" : "market")],
  ],
  asset: [
    ["kind", String],
    ["price_usd", (v) => (v ? `$${Number(v).toFixed(4)}` : "n/a")],
    ["issuer", String],
    ["backing", String],
    [
      "total_supply",
      (v) =>
        v
          ? Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 })
          : "n/a",
    ],
    ["upgradeable", (v) => (v ? "yes" : "no")],
    ["expiry", String],
  ],
  amm: [
    ["liquidity_usd", usd],
    ["expiry", String],
    ["implied_apy", pct],
  ],
  mint_authority: [
    ["role", String],
    ["controller_kind", String],
    ["upgradeable_token", (v) => (v ? "yes - admin can change code" : "no")],
    ["implementation", short],
  ],
  issuer: [
    ["tier", String],
    ["backing", String],
  ],
  protocol: [["tvl_usd", usd]],
  chain: [
    ["chain_class", String],
    ["head_block", String],
    ["note", String],
  ],
  bridge: [
    ["canonical", (v) => (v ? "canonical (L1-secured)" : "third-party")],
  ],
};

export default function Inspector({
  analysis,
  catalog,
  selected,
  preMode,
  busy,
  blocked = false,
  onShock,
  onReplay,
  scenarioView,
  onScenario,
}: {
  analysis: Analysis;
  catalog: Catalog | null;
  selected: string | null;
  preMode: string | null;
  busy: boolean;
  blocked?: boolean;
  onShock: (r: ShockReq) => void;
  onReplay: (incident: string, exitOverride: number | null) => void;
  scenarioView: boolean;
  onScenario: () => void;
}) {
  const node = useMemo(
    () => analysis.graph.nodes.find((n) => n.id === selected) ?? null,
    [analysis, selected],
  );
  const [mode, setMode] = useState<string | null>(null);
  const [alert, setAlert] = useState("alert");
  const [useP, setUseP] = useState(false);
  const [pOver, setPOver] = useState(0.3);
  const [useC, setUseC] = useState(false);
  const [cOver, setCOver] = useState(0.11);
  const [incident, setIncident] = useState("");

  useEffect(() => {
    if (!node) return setMode(null);
    setMode(
      preMode && node.modes.some((m) => m.id === preMode)
        ? preMode
        : (node.risk?.top_mode ?? node.modes[0]?.id ?? null),
    );
  }, [node, preMode]);

  const evidence = node?.evidence ?? [];
  const meta = node ? TYPE_META[node.type] : null;
  const modeRisk = (id: string) => node?.risk?.modes.find((m) => m.mode === id);

  return (
    <div className="panel-section">
      {blocked && (
        <div className="hint">
          Refresh this investment before running a new scenario.
        </div>
      )}
      {!node && (
        <div className="hint">
          Select a graph component or a modeled risk contributor.
        </div>
      )}
      {node && meta && (
        <>
          <div className="insp-head" style={{ borderColor: meta.color }}>
            <div className="insp-type" style={{ color: meta.color }}>
              {meta.glyph} {meta.label}
            </div>
            <div className="insp-label">{node.label}</div>
            <EvidenceBadge node={node} />
            {node.address && (
              <a
                className="addr"
                href={node.attrs.explorer || node.attrs.url || "#"}
                target="_blank"
                rel="noreferrer"
              >
                {short(node.address)} <ExternalLink size={11} />
              </a>
            )}
            {node.attrs.url && !node.address?.startsWith("0x") && (
              <a
                className="addr"
                href={node.attrs.url}
                target="_blank"
                rel="noreferrer"
              >
                open <ExternalLink size={11} />
              </a>
            )}
          </div>
          <details className="component-metrics" open={!scenarioView}>
            <summary>Component information</summary>
            <div className="attrs">
              {(SHOW_ATTRS[node.type] ?? [])
                .filter(
                  ([k]) =>
                    node.attrs[k] !== undefined && node.attrs[k] !== null,
                )
                .map(([k, f]) => (
                  <div key={k}>
                    <span>{k.replace(/_/g, " ")}</span>
                    <b>{f(node.attrs[k])}</b>
                  </div>
                ))}
              {node.risk && (
                <div>
                  <span>share of modeled risk</span>
                  <b>{pct(node.risk.share)}</b>
                </div>
              )}
            </div>
          </details>
          {!scenarioView && (
            <button className="btn primary" onClick={onScenario}>
              Run a scenario
            </button>
          )}
          {scenarioView &&
            (node.modes.length > 0 ? (
              <>
                <div className="sub-h">Failure type</div>
                <div className="modes">
                  {node.modes.map((m) => {
                    const r = modeRisk(m.id);
                    return (
                      <button
                        key={m.id}
                        className={`mode ${mode === m.id ? "on" : ""}`}
                        onClick={() => setMode(m.id)}
                      >
                        <b>{m.label}</b>
                        {r && (
                          <small>
                            base p {pct(r.p, 2)} /{" "}
                            {analysis.screening.horizon_days}d · loss if it
                            fails {pct(r.loss_given_failure)}
                          </small>
                        )}
                      </button>
                    );
                  })}
                </div>
                <div className="sub-h">Signal strength</div>
                <select
                  className="sel"
                  aria-label="Signal strength"
                  value={alert}
                  onChange={(e) => setAlert(e.target.value)}
                >
                  {(
                    catalog?.alerts ?? [
                      { id: "alert", label: "Monitoring alert" },
                    ]
                  ).map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.label}
                    </option>
                  ))}
                </select>
                <details className="advanced">
                  <summary>Advanced overrides</summary>
                  <label className="ovr">
                    <input
                      type="checkbox"
                      checked={useP}
                      onChange={(e) => setUseP(e.target.checked)}
                    />
                    <span>Override probability</span>
                    <input
                      type="range"
                      min={0}
                      max={1}
                      step={0.01}
                      value={pOver}
                      disabled={!useP}
                      onChange={(e) => setPOver(Number(e.target.value))}
                    />
                    <b>{pct(pOver, 0)}</b>
                  </label>
                  <label className="ovr">
                    <input
                      type="checkbox"
                      checked={useC}
                      onChange={(e) => setUseC(e.target.checked)}
                    />
                    <span>Override exit cost</span>
                    <input
                      type="range"
                      min={0}
                      max={0.3}
                      step={0.005}
                      value={cOver}
                      disabled={!useC}
                      onChange={(e) => setCOver(Number(e.target.value))}
                    />
                    <b>{pct(cOver, 1)}</b>
                  </label>
                </details>
                <button
                  className="btn primary"
                  disabled={busy || blocked || !mode}
                  onClick={() =>
                    mode &&
                    onShock({
                      node_id: node.id,
                      mode,
                      alert,
                      probability_override: useP ? pOver : null,
                      exit_cost_override: useC ? cOver : null,
                    })
                  }
                >
                  {busy ? (
                    <Loader2 className="spin" size={16} />
                  ) : (
                    <Zap size={16} />
                  )}{" "}
                  Simulate impact
                </button>
              </>
            ) : (
              <div className="hint">
                This component has no direct failure mode; pick one of its
                dependencies.
              </div>
            ))}

          <details className="evidence">
            <summary>Evidence ({evidence.length})</summary>
            <p className="hint small">{evidenceSummary(node)}</p>
            {evidence.map((e, i) => (
              <div key={i} className="ev">
                <span className={`src src-${e.source}`}>
                  {evidenceSource(e)}
                </span>
                <div>
                  {e.detail}
                  {e.block ? <small> · block {e.block}</small> : null}
                  {e.ref?.startsWith("http") ? (
                    <a href={e.ref} target="_blank" rel="noreferrer">
                      {" "}
                      source
                    </a>
                  ) : e.ref ? (
                    <small className="mono"> {e.ref}</small>
                  ) : null}
                </div>
              </div>
            ))}
          </details>
        </>
      )}

      {catalog && scenarioView && (
        <>
          <div className="sub-h">Historical replay</div>
          <div className="row-replay">
            <select
              className="sel"
              aria-label="Historical incident"
              value={incident}
              onChange={(e) => setIncident(e.target.value)}
            >
              <option value="">Choose an incident…</option>
              {catalog.incidents.map((i) => (
                <option key={i.id} value={i.id}>
                  {i.date.slice(0, 4)} · {i.name}
                </option>
              ))}
            </select>
            <button
              className="btn"
              disabled={!incident || busy || blocked}
              onClick={() => onReplay(incident, useC ? cOver : null)}
            >
              <History size={15} /> Replay
            </button>
          </div>
          {incident && (
            <div className="hint small">
              {catalog.incidents.find((i) => i.id === incident)?.summary}
            </div>
          )}
          {useC && (
            <div className="hint small">
              Replay uses your exit cost override of {pct(cOver, 1)}.{" "}
              <button className="text-button" onClick={() => setUseC(false)}>
                Remove override
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
