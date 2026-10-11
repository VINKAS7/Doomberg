import type { GNode } from "../types";
import { evidenceStatus, evidenceSummary } from "../ui/workspace";
export default function EvidenceBadge({ node }: { node: GNode }) {
  const label = evidenceStatus(node);
  return (
    <span
      className={`evidence-badge evidence-${label === "Unverified" ? "unknown" : label === "Inferred" ? "inferred" : "sourced"}`}
      title={evidenceSummary(node)}
    >
      {label}
    </span>
  );
}
