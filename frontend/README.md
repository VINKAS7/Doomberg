# Doomberg Terminal UI

The terminal presents Belem Capital dependency research and scenario recommendations. It does not execute trades or continuously monitor oracles.

- **Refresh investment** reruns dependency discovery and baseline screening. Snapshot age is shown separately from API connectivity. A five-minute refresh reminder is a UI convenience, not an oracle risk threshold.
- **Investment overview**, above the sidebar investment tabs, compares entered sizes, snapshot age, baseline loss, withdrawability and the latest saved scenario. Positions are classified as examples, entered positions or declared holdings. All sizes are user-entered; holdings are not wallet-verified. Shared dependencies are not aggregated into a portfolio loss estimate.
- **Evidence badges** distinguish on-chain evidence, inferred relationships, registry references, API evidence and unverified components. Inspect a component to see its source details. Evidence for one attribute does not validate the entire dependency.
- **Results** show immediately withdrawable dollars, remaining exposure and estimated costs for the modeled exit route. Partial availability does not change the backend recommendation. Probability and exit-cost overrides are labelled explicitly.
- **Scenario history & comparison** retains the latest 20 runs per investment, including their original inputs, horizon and analysis timestamp. Older analysis snapshots cannot be overlaid on a newer dependency graph.
- **Saved workspaces** use browser-local storage (`doomberg.workspace.v1`) for investment inputs, classifications, analysis snapshots and scenario history. Reloading restores snapshots and refreshes each investment when selected before enabling new scenarios. API analysis IDs are temporary. Data is specific to this browser and is not shared across devices; storage failures are shown in the UI. Closing an investment removes it and its history from the saved workspace.
- **Graph controls** include connection labels, direct-neighbour highlighting, simplified/full views and explicit fit/reset controls. Selecting a node does not fit the viewport or change its dimensions. Category groups still expand to expose all original dependencies.

From the repository root, run `npm run dev`. Frontend checks: `npm --prefix frontend test` and `npm --prefix frontend run build`.
