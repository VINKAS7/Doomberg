import type { Leverage } from "../types";
import { pct, usd } from "../format";

const price = (x: number) => (x >= 100 ? usd(x) : `$${x.toFixed(4)}`);

/** Balance sheet and liquidation distance of a borrow position (``leverage.health`` in the engine). */
export default function LeverageCard({ lev, horizon }: { lev: Leverage; horizon: number }) {
  const used = Math.min(lev.ltv / lev.lltv, 1);
  const tone = lev.health_factor < 1.1 ? "bad" : lev.health_factor < 1.3 ? "warn" : "ok";
  return (
    <section className="leverage-card" aria-label="Leverage and liquidation">
      <div className="leverage-head">
        <b>Leverage &amp; liquidation</b>
        <small>
          {lev.structure === "loop"
            ? `PT loop · ${lev.leverage.toFixed(2)}x collateral per $ of equity`
            : `${lev.collateral_label} collateral · borrowed ${lev.loan_label} deployed into ${lev.deployed_label}`}
          {lev.live ? ` · live borrower ${lev.live.account.slice(0, 6)}…${lev.live.account.slice(-4)}` : ""}
        </small>
      </div>
      <div className="leverage-grid">
        <div>
          <small>Collateral ({lev.collateral_label})</small>
          <b>{usd(lev.collateral_usd)}</b>
        </div>
        <div>
          <small>Debt ({lev.loan_label})</small>
          <b>{usd(lev.debt_usd)}</b>
        </div>
        {lev.structure === "deploy" && (
          <div>
            <small>Deployed ({lev.deployed_label})</small>
            <b>{usd(lev.deployed_usd)}</b>
          </div>
        )}
        <div>
          <small>Health factor</small>
          <b className={tone}>
            {lev.health_factor.toFixed(2)}
            {lev.live?.health_factor_live != null && (
              <span> Morpho: {lev.live.health_factor_live.toFixed(2)}</span>
            )}
          </b>
        </div>
        <div>
          <small>Oracle drop to liquidation</small>
          <b className={tone}>{pct(lev.drop_to_liquidation)}</b>
        </div>
        <div>
          <small>P(liquidated in {horizon}d)</small>
          <b>{pct(lev.p_liquidation, 2)}</b>
        </div>
        <div>
          <small>Net carry on equity</small>
          <b className={lev.net_apy <= 0 ? "bad" : ""}>
            {pct(lev.net_apy)}
            <span> PT {pct(lev.pt_apy)} − borrow {pct(lev.borrow_apy)}</span>
          </b>
        </div>
      </div>
      <div className="ltv-bar" title={`LTV ${pct(lev.ltv)} of LLTV ${pct(lev.lltv)}`}>
        <div className={`ltv-fill ${tone}`} style={{ width: `${used * 100}%` }} />
        <span>
          LTV {pct(lev.ltv)} / LLTV {pct(lev.lltv)} · liquidation penalty {pct(lev.liquidation_incentive - 1)} of the debt repaid
          {lev.liquidation_price_usd != null ? ` · liquidation price ${price(lev.liquidation_price_usd)}` : ""}
        </span>
      </div>
      <p className="leverage-note">Price feed: {lev.vol_reason}.</p>
    </section>
  );
}
