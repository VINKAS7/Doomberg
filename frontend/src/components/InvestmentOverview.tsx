import { useEffect, useRef } from "react";
import { X, ArrowUpRight } from "lucide-react";
import type { Investment } from "../ui/investments";
import { investmentLabel } from "../ui/investments";
import { ROLE_LABELS } from "../ui/workspace";
import { CHAINS, pct, usd } from "../format";
import Freshness from "./Freshness";

export default function InvestmentOverview({
  open,
  items,
  onClose,
  onSelect,
}: {
  open: boolean;
  items: Investment[];
  onClose: () => void;
  onSelect: (id: string) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    if (open) dialog.current?.showModal();
    else dialog.current?.close();
  }, [open]);
  const holdings = items.filter((item) => item.role === "holding");
  return (
    <dialog
      ref={dialog}
      className="position-dialog overview-dialog"
      onCancel={onClose}
      onClose={onClose}
    >
      <div className="dialog-header">
        <div>
          <span className="eyebrow">Investment research</span>
          <h2>Investment overview</h2>
        </div>
        <button onClick={onClose} aria-label="Close investment overview">
          <X size={18} />
        </button>
      </div>
      <div className="overview-content">
        <p className="overview-note">
          {items.length} open investments · Declared holdings:{" "}
          {usd(
            holdings.reduce(
              (sum, item) => sum + item.analysis.request.size_usd,
              0,
            ),
          )}
          . Sizes are user-entered, not wallet-verified. Examples and other
          entered positions are excluded from that total.
        </p>
        <div className="table-scroll">
          <table className="workspace-table">
            <thead>
              <tr>
                <th>Investment</th>
                <th>Entered size</th>
                <th>Snapshot</th>
                <th>Baseline modeled loss</th>
                <th>Withdrawable</th>
                <th>Latest saved scenario</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => {
                const a = item.analysis,
                  latest = item.history[0];
                return (
                  <tr key={item.id}>
                    <td>
                      <button
                        className="history-pick"
                        onClick={() => onSelect(item.id)}
                      >
                        {investmentLabel(a)} <ArrowUpRight size={12} />
                      </button>
                      <small>
                        {CHAINS[a.request.chain_id]} · {ROLE_LABELS[item.role]}
                      </small>
                    </td>
                    <td>{usd(a.request.size_usd)}</td>
                    <td>
                      {item.busy ? (
                        "Calculating…"
                      ) : (
                        <Freshness
                          refreshedAt={item.refreshedAt}
                          needsRefresh={item.needsRefresh}
                        />
                      )}
                    </td>
                    <td>
                      {usd(
                        a.screening.total_expected_loss_pct *
                          a.request.size_usd,
                      )}
                      <small>{a.screening.horizon_days}-day horizon</small>
                    </td>
                    <td>
                      {pct(a.exit_now.instant_fraction, 0)}
                      <small>At snapshot</small>
                    </td>
                    <td>
                      {latest ? (
                        <>
                          <span
                            className={`decision-tag ${latest.result.decision.verdict.toLowerCase()}`}
                          >
                            {latest.result.decision.verdict}
                          </span>
                          <small>{latest.result.shock.mode_label}</small>
                          <small>
                            {new Date(latest.createdAt).toLocaleString()}
                          </small>
                          {latest.analysisId !== a.position_id && (
                            <span className="snapshot-tag">
                              Earlier snapshot
                            </span>
                          )}
                        </>
                      ) : (
                        "No scenario run"
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <p className="overview-note">
          Scenario results are model recommendations, not live alerts. Baseline
          losses are shown per investment; shared dependencies are not combined
          into a portfolio loss estimate.
        </p>
      </div>
    </dialog>
  );
}
