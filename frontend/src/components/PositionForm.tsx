import { useEffect, useState } from "react";
import { Search, Loader2 } from "lucide-react";
import { api } from "../api";
import type { BorrowFields, PositionKind, Preset } from "../types";
import { CHAINS } from "../format";
import { ROLE_LABELS, type PositionRole } from "../ui/workspace";

type Req = {
  kind: PositionKind;
  address: string;
  chain_id: number;
  size_usd: number;
  book_usd: number;
  horizon_days: number;
  label?: string;
} & BorrowFields;

export default function PositionForm({
  onAnalyze,
  busy,
  initial,
  initialRole = "entered",
}: {
  onAnalyze: (r: Req, role: PositionRole) => void;
  busy: boolean;
  initial?: Req;
  initialRole?: PositionRole;
}) {
  const [kind, setKind] = useState<PositionKind>(initial?.kind ?? "market");
  const [address, setAddress] = useState(
    initial?.address ??
      "0x1e9d614631a7df0ec07fb05b2c8cb2491575fd1a63a33bf187a6afb295a4fc64",
  );
  const [chain, setChain] = useState(initial?.chain_id ?? 1);
  const [size, setSize] = useState(initial?.size_usd ?? 1_000_000);
  const [book, setBook] = useState(initial?.book_usd ?? 10_000_000);
  const [horizon, setHorizon] = useState(initial?.horizon_days ?? 30);
  const [presets, setPresets] = useState<Preset[]>([]);
  const [role, setRole] = useState<PositionRole>(initialRole);
  const [ltv, setLtv] = useState(initial?.ltv ?? 0.4);
  const [deploy, setDeploy] = useState(initial?.deploy_address ?? "");
  const [account, setAccount] = useState(initial?.account ?? "");
  const borrow = kind === "borrow";
  const borrowFields = (): BorrowFields =>
    borrow
      ? {
          ltv: account.trim() ? null : ltv,
          deploy_address: deploy.trim() || null,
          account: account.trim() || null,
        }
      : {};

  useEffect(() => {
    api
      .presets()
      .then(setPresets)
      .catch(() => {});
  }, []);

  return (
    <div className="panel-section">
      <p className="form-intro">
        Choose a reference position or enter a contract.
      </p>
      <div className="seg">
        {(
          [
            ["token", "PT token"],
            ["market", "Lending market"],
            ["borrow", "Borrow (leveraged)"],
          ] as const
        ).map(([k, label]) => (
          <button
            key={k}
            className={kind === k ? "on" : ""}
            onClick={() => setKind(k)}
          >
            {label}
          </button>
        ))}
      </div>
      <label className="fld">
        <span>
          {kind === "market"
            ? "Morpho market id (PT collateral)"
            : borrow
              ? "Morpho market id you borrow from"
              : "Pendle PT token address"}
        </span>
        <input
          value={address}
          onChange={(e) => setAddress(e.target.value)}
          spellCheck={false}
          placeholder="0x…"
        />
      </label>
      {borrow && (
        <>
          <label className="fld">
            <span>Borrowed money deployed into PT (empty = PT loop)</span>
            <input
              value={deploy}
              onChange={(e) => setDeploy(e.target.value)}
              spellCheck={false}
              placeholder="0x… Pendle PT address"
            />
          </label>
          <div className="row2">
            <label className="fld">
              <span>LTV (debt / collateral)</span>
              <input
                type="number"
                value={ltv}
                min={0.01}
                max={0.99}
                step={0.05}
                disabled={!!account.trim()}
                onChange={(e) => setLtv(Number(e.target.value))}
              />
            </label>
            <label className="fld">
              <span>Or read a live borrower</span>
              <input
                value={account}
                onChange={(e) => setAccount(e.target.value)}
                spellCheck={false}
                placeholder="0x… wallet (optional)"
              />
            </label>
          </div>
        </>
      )}
      <div className="row2">
        <label className="fld">
          <span>Chain</span>
          <select
            value={chain}
            onChange={(e) => setChain(Number(e.target.value))}
          >
            {Object.entries(CHAINS).map(([id, n]) => (
              <option key={id} value={id}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <label className="fld">
          <span>Horizon (days)</span>
          <input
            type="number"
            value={horizon}
            min={1}
            max={365}
            onChange={(e) => setHorizon(Number(e.target.value))}
          />
        </label>
      </div>
      <div className="row2">
        <label className="fld">
          <span>{borrow ? "Your capital / equity ($)" : "Entered position size ($)"}</span>
          <input
            type="number"
            value={size}
            step={100000}
            onChange={(e) => setSize(Number(e.target.value))}
          />
        </label>
        <label className="fld">
          <span>Portfolio value ($)</span>
          <input
            type="number"
            value={book}
            step={1000000}
            onChange={(e) => setBook(Number(e.target.value))}
          />
        </label>
      </div>
      <label className="fld">
        <span>Position classification</span>
        <select
          value={role}
          onChange={(e) => setRole(e.target.value as PositionRole)}
        >
          {Object.entries(ROLE_LABELS).map(([id, label]) => (
            <option value={id} key={id}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <p className="form-intro">
        Sizes are entered manually. Declared holdings are user-reported, not
        wallet-verified.
      </p>
      <button
        className="btn primary"
        disabled={busy || !address}
        onClick={() =>
          onAnalyze(
            {
              kind,
              address,
              chain_id: chain,
              size_usd: size,
              book_usd: book,
              horizon_days: horizon,
              ...borrowFields(),
            },
            role,
          )
        }
      >
        {busy ? <Loader2 className="spin" size={16} /> : <Search size={16} />}{" "}
        {busy ? "Tracing…" : "Analyse investment"}
      </button>

      <div className="sub-h">Reference positions</div>
      <div className="list">
        {presets.map((p) => (
          <button
            key={p.id}
            className="list-item"
            disabled={busy}
            title={p.note}
            onClick={() => {
              setKind(p.kind);
              setAddress(p.address);
              setChain(p.chain_id);
              setSize(p.size_usd);
              setLtv(p.ltv ?? 0.4);
              setDeploy(p.deploy_address ?? "");
              setAccount(p.account ?? "");
              setRole("example");
              onAnalyze(
                {
                  kind: p.kind,
                  address: p.address,
                  chain_id: p.chain_id,
                  size_usd: p.size_usd,
                  book_usd: book,
                  horizon_days: horizon,
                  ltv: p.ltv ?? null,
                  deploy_address: p.deploy_address ?? null,
                  account: p.account ?? null,
                },
                "example",
              );
            }}
          >
            <div>
              <b>{p.label}</b>
            </div>
            <span className="chip">{CHAINS[p.chain_id]}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
