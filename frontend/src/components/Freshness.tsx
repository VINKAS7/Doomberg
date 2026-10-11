import { useEffect, useState } from "react";
import { ageLabel, refreshSuggested } from "../ui/workspace";

// Each label keeps its own clock, so the age ticking over re-renders only the
// label instead of every workspace.
function useNow(intervalMs: number) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}

export default function Freshness({
  refreshedAt,
  needsRefresh,
}: {
  refreshedAt: string;
  needsRefresh: boolean;
}) {
  const now = useNow(30_000);
  return (
    <span
      className={`freshness ${needsRefresh || refreshSuggested(refreshedAt, now) ? "needs-refresh" : ""}`}
      title={`Analysis completed ${new Date(refreshedAt).toLocaleString()}. This is snapshot age, not proof that every oracle or data source is current. Refresh is suggested after five minutes; this is a UI reminder, not a risk threshold.`}
    >
      Snapshot · {ageLabel(refreshedAt, now)}
      {needsRefresh
        ? " · refresh required"
        : refreshSuggested(refreshedAt, now)
          ? " · refresh suggested"
          : ""}
    </span>
  );
}
