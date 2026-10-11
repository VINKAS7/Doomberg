import { useEffect, useRef } from "react";
import { Loader2, Plus, X } from "lucide-react";
import type { Investment } from "../ui/investments";
import { investmentLabel } from "../ui/investments";
import { CHAINS, usd } from "../format";

export default function InvestmentTabs({
  items,
  activeId,
  onSelect,
  onClose,
  onAdd,
}: {
  items: Investment[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onClose: (id: string) => void;
  onAdd: () => void;
}) {
  const strip = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const tab = strip.current?.querySelector<HTMLElement>(
      '[aria-selected="true"]',
    );
    tab?.scrollIntoView({ block: "nearest", inline: "nearest" });
    // Switching or closing an investment hides or removes whatever had focus
    // (the previous workspace, a close button, the add dialog's opener).
    // Move focus to the active tab rather than letting it drop to <body>.
    const frame = requestAnimationFrame(() => {
      const focused = document.activeElement;
      if (
        !focused ||
        focused === document.body ||
        !focused.getClientRects().length
      )
        tab?.focus();
    });
    return () => cancelAnimationFrame(frame);
  }, [activeId, items.length]);
  if (!items.length) return null;
  return (
    <div className="investment-bar">
      <span className="investment-caption">
        Investments <b>{items.length}</b>
      </span>
      <div
        className="investment-tab-strip"
        role="tablist"
        aria-orientation="vertical"
        aria-label="Open investments"
        ref={strip}
        onKeyDown={(event) => {
          if (
            !event.target ||
            (event.target as HTMLElement).getAttribute("role") !== "tab"
          )
            return;
          const keys = [
            "ArrowUp",
            "ArrowDown",
            "ArrowLeft",
            "ArrowRight",
            "Home",
            "End",
          ];
          if (!keys.includes(event.key)) return;
          event.preventDefault();
          const current = items.findIndex((item) => item.id === activeId);
          const next =
            event.key === "Home"
              ? 0
              : event.key === "End"
                ? items.length - 1
                : (current +
                    (event.key === "ArrowRight" || event.key === "ArrowDown"
                      ? 1
                      : -1) +
                    items.length) %
                  items.length;
          onSelect(items[next].id);
        }}
      >
        {items.map((item) => {
          const label = investmentLabel(item.analysis);
          const active = item.id === activeId;
          return (
            <div
              key={item.id}
              className={`investment-tab ${active ? "active" : ""}`}
            >
              <button
                role="tab"
                id={`investment-tab-${item.id}`}
                aria-selected={active}
                aria-controls={`investment-panel-${item.id}`}
                tabIndex={active ? 0 : -1}
                className="investment-tab-select"
                onClick={() => onSelect(item.id)}
                title={`${label} · ${CHAINS[item.analysis.request.chain_id]} · ${usd(item.analysis.request.size_usd)}`}
              >
                <span className="investment-tab-label">{label}</span>
                <small>
                  {item.analysis.request.kind === "market"
                    ? "Lending"
                    : item.analysis.request.kind === "borrow"
                      ? "Borrow"
                      : "PT"}{" "}
                  ·{" "}
                  {CHAINS[item.analysis.request.chain_id]} ·{" "}
                  {usd(item.analysis.request.size_usd)}
                </small>
              </button>
              {item.busy ? (
                <Loader2
                  size={13}
                  className="spin investment-tab-busy"
                  aria-label="Calculating investment"
                />
              ) : null}
              <button
                className="investment-tab-close"
                aria-label={`Close ${label}`}
                title="Close investment tab"
                disabled={item.busy}
                onClick={() => onClose(item.id)}
              >
                <X size={13} />
              </button>
            </div>
          );
        })}
      </div>
      <button
        className="investment-tab-add"
        onClick={onAdd}
        title="Add investment"
      >
        <Plus size={15} />
        <span>Add investment</span>
      </button>
    </div>
  );
}
