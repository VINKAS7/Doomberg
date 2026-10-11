"""Node, edge and evidence types, and the dependency graph container.

Edges point from the dependent to the thing it relies on:
``position --holds--> PT --traded_on--> Pendle AMM --runs_on--> Pendle V2 --settles_on--> chain``, or
``position --deposits_in--> market --collateral_in--> PT`` with ``market --priced_by--> oracle``.
Damage therefore travels against the edge direction.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

import networkx as nx

NODE_TYPES = (
    "position", "market", "oracle", "feed", "asset", "amm", "protocol", "issuer", "mint_authority", "bridge", "chain",
)

EDGE_TYPES = (
    "holds",            # position -> PT
    "deposits_in",      # position -> market
    "denominated_in",   # position -> loan asset the supply is redeemed in
    "lends",            # market -> loan asset (informational, no lender-side propagation)
    "collateral_in",    # market -> PT collateral
    "priced_by",        # market -> oracle
    "reads_feed",       # oracle -> feed / exchange-rate vault
    "backed_by",        # asset -> underlying asset
    "issued_by",        # asset -> issuer
    "mintable_by",      # asset -> mint authority
    "bridged_via",      # asset -> bridge
    "traded_on",        # PT -> Pendle AMM market
    "liquidated_in",    # PT -> lending market where it is posted (liquidations are sold into its AMM)
    "runs_on",          # market / amm -> protocol code
    "settles_on",       # anything -> chain
)


@dataclass
class Evidence:
    source: str                 # morpho-api | rpc | pendle-api | defillama | kyberswap | registry | heuristic
    detail: str
    ref: str | None = None      # contract call, query name or url
    block: int | None = None
    fetched_at: float = field(default_factory=time.time)


@dataclass
class Node:
    id: str
    type: str
    label: str
    chain_id: int | None = None
    address: str | None = None
    layer: int = 0
    attrs: dict[str, Any] = field(default_factory=dict)
    risk_factors: dict[str, Any] = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class Edge:
    source: str
    target: str
    type: str
    weight: float = 1.0
    attrs: dict[str, Any] = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.source}|{self.type}|{self.target}"


class DependencyGraph:
    def __init__(self) -> None:
        self.g = nx.MultiDiGraph()
        self.nodes: dict[str, Node] = {}
        self.edges: dict[str, Edge] = {}
        self.root: str | None = None

    def add_node(self, node: Node) -> Node:
        existing = self.nodes.get(node.id)
        if existing:
            existing.attrs.update({k: v for k, v in node.attrs.items() if v is not None})
            existing.risk_factors.update(node.risk_factors)
            existing.evidence.extend(node.evidence)
            existing.layer = max(existing.layer, node.layer)
            return existing
        self.nodes[node.id] = node
        self.g.add_node(node.id)
        return node

    def add_edge(self, edge: Edge) -> Edge:
        if edge.source == edge.target:
            return edge
        existing = self.edges.get(edge.id)
        if existing:
            existing.weight = max(existing.weight, edge.weight)
            existing.evidence.extend(edge.evidence)
            return existing
        self.edges[edge.id] = edge
        self.g.add_edge(edge.source, edge.target, key=edge.type)
        return edge

    def out_edges(self, node_id: str) -> list[Edge]:
        return [self.edges[f"{u}|{k}|{v}"] for u, v, k in self.g.out_edges(node_id, keys=True)]

    def in_edges(self, node_id: str) -> list[Edge]:
        return [self.edges[f"{u}|{k}|{v}"] for u, v, k in self.g.in_edges(node_id, keys=True)]

    def ancestors_order(self) -> list[str]:
        """Nodes ordered so every dependency comes before its dependents."""
        simple = nx.DiGraph(self.g)
        try:
            return list(reversed(list(nx.topological_sort(simple))))
        except nx.NetworkXUnfeasible:
            # Cycles are rare (e.g. two assets backing each other); break them by depth.
            return sorted(self.nodes, key=lambda n: -self.nodes[n].layer)

    def paths_to(self, target: str, max_paths: int = 6, cutoff: int = 9) -> list[list[str]]:
        if not self.root or target == self.root:
            return [[self.root]] if self.root else []
        simple = nx.DiGraph(self.g)
        paths = []
        for p in nx.all_simple_paths(simple, self.root, target, cutoff=cutoff):
            paths.append(p)
            if len(paths) >= max_paths * 4:
                break
        paths.sort(key=len)
        return paths[:max_paths]

    def edge_between(self, u: str, v: str) -> Edge | None:
        data = self.g.get_edge_data(u, v)
        if not data:
            return None
        key = next(iter(data))
        return self.edges[f"{u}|{key}|{v}"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "nodes": [asdict(n) for n in self.nodes.values()],
            "edges": [{**asdict(e), "id": e.id} for e in self.edges.values()],
        }
