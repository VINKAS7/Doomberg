export const pct = (x: number | null | undefined, d = 1) =>
  x === null || x === undefined || Number.isNaN(x) ? "n/a" : `${(x * 100).toFixed(x !== 0 && Math.abs(x) < 0.001 ? 3 : d)}%`;

export const usd = (x: number | null | undefined) => {
  if (x === null || x === undefined) return "n/a";
  const a = Math.abs(x);
  if (a >= 1e9) return `$${(x / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `$${(x / 1e6).toFixed(2)}M`;
  if (a >= 1e4) return `$${(x / 1e3).toFixed(1)}k`;
  return `$${x.toFixed(0)}`;
};

export const short = (a?: string | null) => (a && a.length > 14 ? `${a.slice(0, 6)}…${a.slice(-4)}` : a ?? "");

export const CHAINS: Record<number, string> = { 1: "Ethereum", 8453: "Base", 42161: "Arbitrum", 10: "Optimism", 137: "Polygon", 143: "Monad" };

export const TYPE_META: Record<string, { color: string; label: string; glyph: string }> = {
  position: { color: "#75603b", label: "Position", glyph: "◆" },
  market: { color: "#32766b", label: "Lending market", glyph: "⇄" },
  oracle: { color: "#9a6b24", label: "Oracle", glyph: "◉" },
  feed: { color: "#9a6b24", label: "Price feed", glyph: "∿" },
  asset: { color: "#3d7d52", label: "Token", glyph: "●" },
  amm: { color: "#946381", label: "AMM / PT market", glyph: "≋" },
  issuer: { color: "#426786", label: "Issuer / custodian", glyph: "⌂" },
  mint_authority: { color: "#a3554a", label: "Token controls", glyph: "✎" },
  bridge: { color: "#775893", label: "Bridge", glyph: "⇆" },
  protocol: { color: "#676e77", label: "Protocol code", glyph: "{}" },
  chain: { color: "#676e77", label: "Chain", glyph: "⛓" },
};

export const riskColor = (share: number) => {
  if (share >= 0.15) return "#b74437";
  if (share >= 0.06) return "#a76b24";
  if (share >= 0.02) return "#9a752a";
  return "#2c7760";
};

export const lossColor = (l: number) => {
  if (l >= 0.2) return "#b74437";
  if (l >= 0.05) return "#a76b24";
  if (l >= 0.005) return "#9a752a";
  return "#2c7760";
};
