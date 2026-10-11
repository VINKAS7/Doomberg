"""Stage 1 - Discover: walk from a Pendle PT position (held, or used as Morpho collateral) down to the settlement chain.

Every component is asked "what do you rely on?" using, in order of preference:
on-chain reads (RPC), protocol APIs (Morpho, Pendle), price APIs (DefiLlama), and finally the
curated registry for off-chain facts (issuers, custodians). Each node and edge carries the evidence
that produced it.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .graph import DependencyGraph, Edge, Evidence, Node
from .leverage import balance_sheet, liquidation_incentive
from .sources import morpho, pendle
from .sources.pricing import protocol_tvl
from .sources.rpc import Rpc

DATA = Path(__file__).parent / "data"
REGISTRY = json.loads((DATA / "registry.json").read_text(encoding="utf-8"))

KNOWN_ADDRESSES = {  # underlying assets referenced by symbol in the registry
    1: {"weth": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        "steth": "0xae7ab96520de3a18e5e111b5eaab095312d7fe84",
        "usde": "0x4c9edd5852cd905f086c759e8383e09bff1e68b3"},
}
MAX_ASSET_DEPTH = 4
# Collateral venues of a PT: markets below this size cannot move its AMM; the largest few carry the liquidation volume.
VENUE_MIN_USD = 250_000
MAX_VENUES = 5

# Legacy (ZeppelinOS) proxy slots used by USDC-style FiatToken proxies.
ZOS_IMPL_SLOT = "0x7050c9e0f4ca769c69bd3a8ef740bc37934f8e2c036e5a723fd8ee048ed3f8c3"
ZOS_ADMIN_SLOT = "0x10d6a54a4754c8869d6886b5f5d7fbfa5b4522237ea5c60d11bc4e7a1ff9390b"
MASTER_MINTER = "0x35d99f35"


@dataclass
class PositionRequest:
    kind: str = "token"              # token (hold a PT) | market (supply to a Morpho market with PT collateral) | borrow
    address: str = ""                # PT token address or Morpho market id
    chain_id: int = 1
    size_usd: float = 1_000_000.0    # for a borrow position: the equity (the client's capital)
    book_usd: float = 10_000_000.0
    label: str | None = None
    ltv: float | None = None         # borrow: debt / collateral value
    deploy_address: str | None = None  # borrow: PT the borrowed loan asset is deployed into (none = PT loop)
    account: str | None = None       # borrow: read collateral and debt of this borrower from Morpho


def short(a: str | None) -> str:
    return f"{a[:6]}...{a[-4:]}" if a and len(a) > 12 else (a or "")


def explorer(chain_id: int, address: str | None) -> str | None:
    c = REGISTRY["chains"].get(str(chain_id))
    return f"{c['explorer']}/address/{address}" if c and address and address.startswith("0x") and len(address) == 42 else None


class Discovery:
    def __init__(self, req: PositionRequest):
        self.req = req
        self.g = DependencyGraph()
        self.rpcs: dict[int, Rpc] = {}
        self.warnings: list[str] = []
        self.t0 = time.time()

    # ------------------------------------------------------------------ helpers
    def rpc(self, chain_id: int) -> Rpc:
        if chain_id not in self.rpcs:
            self.rpcs[chain_id] = Rpc(chain_id)
        return self.rpcs[chain_id]

    def link(self, u: str, v: str, type_: str, ev: Evidence | None = None, weight: float = 1.0, **attrs):
        self.g.add_edge(Edge(u, v, type_, weight=weight, attrs=attrs, evidence=[ev] if ev else []))

    # ------------------------------------------------------------------ entry
    def run(self) -> DependencyGraph:
        r = self.req
        addr = r.address.strip()
        pos = Node("position", "position", r.label or "Position", r.chain_id, None, 0,
                   attrs={"size_usd": r.size_usd, "book_usd": r.book_usd, "kind": r.kind})
        self.g.add_node(pos)
        self.g.root = pos.id

        if r.kind == "token":
            a = morpho.asset(addr, r.chain_id) or {"address": addr, "symbol": None, "decimals": None, "tags": [], "price": None}
            aid = self.add_asset(a, r.chain_id, 0)
            if self.g.nodes[aid].attrs["kind"] != "pt":
                raise ValueError(f"{self.g.nodes[aid].label} ({addr}) is not a Pendle PT token; only PT positions are supported")
            self.link(pos.id, aid, "holds", Evidence("user", f"Holding ${r.size_usd:,.0f}"))
            pos.label = r.label or f"${r.size_usd / 1e6:,.2f}M of {self.g.nodes[aid].label}"
        elif r.kind == "market":
            m = morpho.market(addr, r.chain_id)
            if not m:
                raise ValueError(f"No Morpho market {addr} on chain {r.chain_id}")
            coll = (m.get("collateralAsset") or {}).get("symbol") or "?"
            if not coll.lower().startswith("pt-"):
                raise ValueError(f"Market collateral {coll} is not a Pendle PT; only markets with PT collateral are supported")
            mid = self.add_market(m, r.chain_id)
            self.link(pos.id, mid, "deposits_in", Evidence("user", f"Supply of ${r.size_usd:,.0f}"))
            lid = self.add_asset(m["loanAsset"], r.chain_id, 1)
            self.link(pos.id, lid, "denominated_in", Evidence("morpho-api", "Supply is redeemed in the loan asset"))
            pos.label = r.label or f"${r.size_usd / 1e6:,.2f}M supplied to {self.g.nodes[mid].label}"
        elif r.kind == "borrow":
            self.add_borrow(pos, addr)
        else:
            raise ValueError(f"Unsupported position kind: {r.kind}")

        self.assign_layers()
        self.g.nodes[pos.id].attrs["discovery_seconds"] = round(time.time() - self.t0, 2)
        self.g.nodes[pos.id].attrs["warnings"] = self.warnings
        return self.g

    # ------------------------------------------------------------------ chain / protocol
    def add_chain(self, chain_id: int) -> str:
        cid = f"chain:{chain_id}"
        if cid in self.g.nodes:
            return cid
        c = REGISTRY["chains"].get(str(chain_id), {"name": f"Chain {chain_id}", "chain_class": "l1"})
        rpc = self.rpc(chain_id)
        block = rpc.block_number()
        self.g.add_node(Node(cid, "chain", c["name"], chain_id, None,
                             attrs={"chain_class": c["chain_class"], "note": c.get("note"), "head_block": block},
                             risk_factors={"chain_class": c["chain_class"]},
                             evidence=[Evidence("registry", f"{c['name']} classified as {c['chain_class']}"),
                                       Evidence("rpc", f"Head block {block}" if block else "RPC unreachable", ref="eth_blockNumber", block=block)]))
        if c.get("settles_to"):
            parent = self.add_chain(c["settles_to"])
            self.link(cid, parent, "settles_on", Evidence("registry", f"{c['name']} posts its state to {self.g.nodes[parent].label}"))
        return cid

    def add_protocol(self, key: str, chain_id: int) -> str:
        pid = f"protocol:{key}:{chain_id}"
        if pid in self.g.nodes:
            return pid
        p = REGISTRY["protocols"][key]
        age = (time.time() - time.mktime(time.strptime(p["launched"], "%Y-%m-%d"))) / 86400
        tvl = protocol_tvl(p["llama"]) if p.get("llama") else None
        rf = {"immutable_core": p.get("immutable_core", False), "audited_major": p.get("audited_major", False),
              "age_days": round(age), "tvl_usd": tvl}
        ev = [Evidence("registry", f"{p['label']}: launched {p['launched']}", ref=p.get("source"))]
        if tvl:
            ev.append(Evidence("defillama", f"Protocol TVL ${tvl / 1e9:,.2f}B", ref=f"https://api.llama.fi/tvl/{p['llama']}"))
        self.g.add_node(Node(pid, "protocol", p["label"], chain_id, None, attrs={"tvl_usd": tvl, "url": p.get("source")},
                             risk_factors=rf, evidence=ev))
        self.link(pid, self.add_chain(chain_id), "settles_on", Evidence("registry", "Contracts deployed on this chain"))
        return pid

    # ------------------------------------------------------------------ borrow
    def add_borrow(self, pos: Node, market_id: str):
        """Collateral posted in a Morpho market, a loan against it, and optionally the PT the loan is deployed into.

        The position must reach a PT: either the collateral is one (a PT loop) or ``deploy_address`` names it.
        With ``account`` the size and LTV are read from that borrower's live position.
        """
        r = self.req
        m = morpho.market(market_id, r.chain_id)
        if not m:
            raise ValueError(f"No Morpho market {market_id} on chain {r.chain_id}")
        mid = self.add_market(m, r.chain_id, role="borrow_market")
        market = self.g.nodes[mid]
        lltv = market.attrs["lltv"]
        coll_id = next(e.target for e in self.g.out_edges(mid) if e.type == "collateral_in")
        loan_id = next(e.target for e in self.g.out_edges(mid) if e.type == "lends")
        coll, loan = self.g.nodes[coll_id], self.g.nodes[loan_id]
        dep_id = None
        if r.deploy_address and r.deploy_address.strip():
            da = r.deploy_address.strip()
            dep_id = self.add_asset(morpho.asset(da, r.chain_id) or {"address": da}, r.chain_id, 1)
            if self.g.nodes[dep_id].attrs["kind"] != "pt":
                raise ValueError(f"{self.g.nodes[dep_id].label} ({da}) is not a Pendle PT token; the loan must be deployed into a PT")
        elif coll.attrs["kind"] != "pt":
            raise ValueError(f"Collateral {coll.label} is not a Pendle PT: give the PT the borrowed {loan.label} is deployed into")
        structure = "deploy" if dep_id else "loop"
        if r.account and r.account.strip():
            acct = r.account.strip()
            live = morpho.market_position(acct, market_id, r.chain_id) or {}
            st = live.get("state") or {}
            cu, bu = st.get("collateralUsd") or 0.0, st.get("borrowAssetsUsd") or 0.0
            if cu <= 0 or bu <= 0:
                raise ValueError(f"{short(acct)} has no collateral and debt in market {short(market_id)}")
            r.ltv = bu / cu
            # Deploy: the loan sits in the PT, so the client's capital is the collateral. Loop: collateral minus debt.
            r.size_usd = cu if dep_id else cu - bu
            pos.attrs.update({"account": acct, "health_factor_live": live.get("healthFactor")})
            pos.evidence.append(Evidence("morpho-api", f"Borrower {acct}: ${cu:,.0f} collateral, ${bu:,.0f} debt "
                                                       f"(LTV {r.ltv:.1%}, health factor {live.get('healthFactor')})",
                                         ref=f"marketPosition({acct}, {market_id})"))
        if r.ltv is None or not 0 < r.ltv < 1:
            raise ValueError("A borrow position needs an LTV between 0 and 1 (or a borrower account to read it from)")
        if r.ltv >= lltv:
            raise ValueError(f"LTV {r.ltv:.1%} is at or above the market's LLTV {lltv:.1%}: the position is already liquidatable")
        E, ltv = r.size_usd, r.ltv
        C, D, V = balance_sheet(structure, ltv)
        pos.attrs.update({"size_usd": E, "equity_usd": E, "structure": structure, "ltv": ltv, "lltv": lltv,
                          "lif": liquidation_incentive(lltv), "collateral_usd": C * E, "debt_usd": D * E, "deployed_usd": V * E})
        self.link(pos.id, mid, "borrows_from", Evidence(
            "user" if not r.account else "morpho-api",
            f"${C * E:,.0f} of {coll.label} posted, ${D * E:,.0f} {loan.label} borrowed (LTV {ltv:.1%} vs LLTV {lltv:.1%})"))
        if dep_id:
            self.link(pos.id, dep_id, "deploys_into", Evidence("user", f"${V * E:,.0f} borrowed {loan.label} deployed into {self.g.nodes[dep_id].label}"))
        pos.label = r.label or (f"${E / 1e6:,.2f}M {coll.label} -> borrow {loan.label} at {ltv:.0%} LTV -> {self.g.nodes[dep_id].label}"
                                if dep_id else f"${E / 1e6:,.2f}M {coll.label} loop at {ltv:.0%} LTV ({C:.1f}x)")

    # ------------------------------------------------------------------ markets
    def add_market(self, m: dict, chain_id: int, role: str = "position_market") -> str:
        """The position's own market: lenders lose when its PT collateral loses value; borrowers get liquidated."""
        mid = f"market:{chain_id}:{m['marketId'].lower()}"
        if mid in self.g.nodes:
            return mid
        st = m.get("state") or {}
        coll, loan = m.get("collateralAsset") or {}, m["loanAsset"]
        cid = self.add_asset(coll, chain_id, 1)
        self._market_node(m, chain_id, role=role)
        self.link(mid, cid, "collateral_in", Evidence("morpho-api", f"Collateral {coll.get('symbol')} (${st.get('collateralAssetsUsd') or 0:,.0f} posted)"))
        lid = self.add_asset(loan, chain_id, 1)
        self.link(mid, lid, "lends", Evidence("morpho-api", f"Loan asset {loan['symbol']}"))
        return mid

    def add_collateral_venues(self, pt_id: str, address: str, chain_id: int, amm_id: str):
        """Other Morpho markets holding this PT as collateral: their liquidations can only be sold into its AMM."""
        own = self.req.address.strip().lower() if self.req.kind in ("market", "borrow") else None
        found = [m for m in morpho.markets_by_collateral(address, chain_id)
                 if m.get("marketId", "").lower() != own
                 and ((m.get("state") or {}).get("collateralAssetsUsd") or 0) >= VENUE_MIN_USD]
        found.sort(key=lambda m: -(m["state"]["collateralAssetsUsd"]))
        if not found:
            return
        pt, amm = self.g.nodes[pt_id], self.g.nodes[amm_id]
        total = sum(m["state"]["collateralAssetsUsd"] for m in found)
        liq = amm.attrs.get("liquidity_usd")
        for m in found[:MAX_VENUES]:
            st = m["state"]
            vid = self._market_node(m, chain_id, role="collateral_venue")
            self.link(pt_id, vid, "liquidated_in", Evidence(
                "morpho-api", f"${st['collateralAssetsUsd']:,.0f} of {pt.label} posted against ${st.get('borrowAssetsUsd') or 0:,.0f} "
                              f"borrowed; liquidations are sold into {amm.label}", ref=f"market {m['marketId']}"))
        if len(found) > MAX_VENUES:
            self.warnings.append(f"{pt.label}: {len(found) - MAX_VENUES} smaller collateral markets not modelled")
        ratio = total / liq if liq else None
        pt.attrs.update({"collateral_elsewhere_usd": total, "collateral_to_amm": ratio})
        pt.evidence.append(Evidence("morpho-api", f"${total:,.0f} posted as collateral in {len(found)} other Morpho market(s)"
                                                  + (f" vs ${liq:,.0f} AMM liquidity ({ratio:.1f}x)" if ratio else ""),
                                    ref="markets(where: collateralAssetAddress_in)"))

    def _market_node(self, m: dict, chain_id: int, role: str = "position_market") -> str:
        """Market node with its protocol and oracle. ``collateral_in`` is linked by the caller, so a venue
        reached from its PT does not point back at it (that would be a cycle)."""
        mid = f"market:{chain_id}:{m['marketId'].lower()}"
        if mid in self.g.nodes:
            return mid
        primary = role != "collateral_venue"
        st = m.get("state") or {}
        lltv = int(m["lltv"]) / 1e18
        coll, loan = m.get("collateralAsset") or {}, m["loanAsset"]
        created = m.get("creationTimestamp")
        age_days = round((time.time() - int(created)) / 86400) if created else None
        label = f"{coll.get('symbol', '?')} / {loan['symbol']}"
        # Morpho oracle price is scaled by 1e36 * 10^(loan decimals - collateral decimals): loan units per collateral unit.
        oracle_px = dex_px = gap = None
        if st.get("price") and coll.get("decimals") is not None and loan.get("decimals") is not None:
            oracle_px = int(st["price"]) / 10 ** (36 + int(loan["decimals"]) - int(coll["decimals"]))
        cu, lu = (coll.get("price") or {}).get("usd"), (loan.get("price") or {}).get("usd")
        if cu and lu:
            dex_px = cu / lu
        if oracle_px and dex_px:
            gap = oracle_px / dex_px - 1
        self.g.add_node(Node(mid, "market", label, chain_id, m["marketId"], attrs={
            "lltv": lltv, "utilization": st.get("utilization"), "supply_usd": st.get("supplyAssetsUsd"),
            "borrow_usd": st.get("borrowAssetsUsd"), "liquidity_usd": st.get("liquidityAssetsUsd"),
            "collateral_usd": st.get("collateralAssetsUsd"), "supply_apy": st.get("supplyApy"),
            "borrow_apy": st.get("borrowApy"), "bad_debt_usd": (m.get("badDebt") or {}).get("usd"),
            "realized_bad_debt_usd": (m.get("realizedBadDebt") or {}).get("usd"), "age_days": age_days,
            "oracle_price": oracle_px, "market_price": dex_px, "oracle_dex_gap": gap,
            "daily_price_variation": st.get("dailyPriceVariation"),
            "subtitle": (f"collateral venue - ${(st.get('collateralAssetsUsd') or 0) / 1e6:,.1f}M PT posted, LLTV {lltv:.1%}"
                         if not primary else f"LLTV {lltv:.1%} - borrow APY {(st.get('borrowApy') or 0):.2%}" if role == "borrow_market"
                         else f"LLTV {lltv:.1%} - util {(st.get('utilization') or 0):.0%}"),
            "url": f"https://app.morpho.org/market?id={m['marketId']}",
            "warnings": [w["type"] for w in m.get("warnings") or []],
            "role": role,
        }, risk_factors={"utilization": st.get("utilization"), "age_days": age_days},
            evidence=[Evidence("morpho-api", f"Market {short(m['marketId'])}: supply ${st.get('supplyAssetsUsd') or 0:,.0f}, "
                                            f"borrow ${st.get('borrowAssetsUsd') or 0:,.0f}, LLTV {lltv:.1%}", ref=f"market {m['marketId']}")]))
        self.link(mid, self.add_protocol("morpho_blue", chain_id), "runs_on",
                  Evidence("morpho-api", f"Morpho Blue at {short((m.get('morphoBlue') or {}).get('address'))}"))
        if m.get("oracle"):
            oid = self.add_oracle(m["oracle"], chain_id, coll, st, age_days, primary=primary)
            self.link(mid, oid, "priced_by", Evidence("morpho-api", f"Oracle {short(m['oracle']['address'])} ({m['oracle']['type']})"))
            # A shared oracle shows the gap seen by the position's own market; a venue only fills it in when absent.
            if gap is not None and (primary or self.g.nodes[oid].attrs.get("oracle_dex_gap") is None):
                self.g.nodes[oid].attrs.update({"oracle_price": oracle_px, "market_price": dex_px, "oracle_dex_gap": gap})
                self.g.nodes[oid].evidence.append(Evidence(
                    "morpho-api", f"Oracle price {oracle_px:,.6f} vs market price {dex_px:,.6f} {loan['symbol']} per "
                                  f"{coll.get('symbol')} ({gap:+.2%})", ref="market.state.price"))
        else:
            self.warnings.append(f"Market {label} has no oracle data")
        return mid

    # ------------------------------------------------------------------ oracles
    def add_oracle(self, o: dict, chain_id: int, coll: dict, mstate: dict, market_age_days: int | None,
                   primary: bool = True) -> str:
        """``primary`` is the position's own market: its collateral size and age set the oracle's risk factors.
        A collateral venue sharing an already classified oracle leaves it as it is."""
        oid = f"oracle:{chain_id}:{o['address'].lower()}"
        if not primary and oid in self.g.nodes:
            return oid
        data = o.get("data") or {}
        feeds = {k: (data.get(k) or {}).get("address") for k in ("baseFeedOne", "baseFeedTwo", "quoteFeedOne", "quoteFeedTwo")}
        base_vault = (data.get("baseOracleVault") or {}).get("address")
        if oid not in self.g.nodes:
            self.g.add_node(Node(oid, "oracle", f"Oracle {coll.get('symbol', '')}", chain_id, o["address"],
                                 attrs={"oracle_type": o.get("type"), "explorer": explorer(chain_id, o["address"]),
                                        "subtitle": o.get("type")},
                                 evidence=[Evidence("morpho-api", f"{o.get('type')} at {o['address']}", ref="market.oracle")]))
        node = self.g.nodes[oid]
        rpc = self.rpc(chain_id)
        csym = (coll.get("symbol") or "").lower()
        ckind = self.g.nodes.get(f"asset:{chain_id}:{(coll.get('address') or '').lower()}", Node("", "", "")).attrs.get("kind")
        descriptions, stale, base_feeds, nav_feed = [], False, 0, False
        for role, addr in feeds.items():
            if not addr or int(addr, 16) == 0:
                continue
            info = rpc.read_feed(addr)
            desc = info.get("description") or f"feed {short(addr)}"
            age = info.get("age_s")
            fid = f"feed:{chain_id}:{addr.lower()}"
            # A feed named after an RWA / yield token is the issuer pushing its NAV, not a market price.
            is_nav = bool(role.startswith("base") and csym and csym in desc.lower() and ckind in ("rwa", "yield_stable", "unknown"))
            nav_feed |= is_nav
            is_stale = bool(age and age > (35 * 86400 if is_nav else 26 * 3600))
            stale |= is_stale and role.startswith("base")
            if role.startswith("base"):
                base_feeds += 1
                descriptions.append(desc)
            self.g.add_node(Node(fid, "feed", desc, chain_id, addr, attrs={
                "role": role, "answer": info.get("answer"), "age_s": age, "stale": is_stale, "nav_feed": is_nav,
                "subtitle": (f"{info['answer']:,.4f}" if info.get("answer") is not None else "no data") + (f" - {age // 60} min old" if age else ""),
                "explorer": explorer(chain_id, addr)},
                risk_factors={"stale_now": is_stale, "single_feed": True},
                evidence=[Evidence("rpc", f"description()='{info.get('description')}', latestRoundData answer={info.get('answer')}, "
                                          f"updated {age}s ago", ref=f"{addr}.latestRoundData()", block=rpc.block_number())]))
            self.link(oid, fid, "reads_feed", Evidence("morpho-api", f"{role} of the oracle"), role=role)
        own_feed = any(csym and csym.replace("pt-", "") in d.lower() and not re.search(r"exchange|rate|nav|fundamental|fixed", d.lower())
                       for d in descriptions) and not nav_feed
        if base_vault:
            cid = f"asset:{chain_id}:{base_vault.lower()}"
            if cid in self.g.nodes:
                self.link(oid, cid, "reads_feed", Evidence("morpho-api", "Reads ERC-4626 conversion rate of the collateral (exchange rate, not market price)"), role="baseVault")
        tracks = own_feed
        reason = ("A base feed prices the collateral token itself" if own_feed else
                  "Issuer-reported NAV feed: the oracle shows what the issuer says, not what the token trades at" if nav_feed else
                  "Exchange-rate / reference-asset pricing: a depeg of the collateral token itself would NOT be seen" if (base_feeds or base_vault) else
                  "No base feed: the collateral is priced at a fixed value")
        node.risk_factors.update({"tracks_market": tracks, "single_feed": base_feeds <= 1, "stale_now": stale,
                                  "collateral_tvl_usd": mstate.get("collateralAssetsUsd"), "age_days": market_age_days})
        node.attrs.update({"tracks_market": tracks, "tracking_reason": reason, "feed_descriptions": descriptions})
        node.evidence.append(Evidence("heuristic", reason))
        return oid

    # ------------------------------------------------------------------ assets
    def classify(self, symbol: str, tags: list[str], erc4626: bool) -> tuple[str, dict, bool]:
        s = (symbol or "").lower()
        reg = REGISTRY["tokens"].get(s)
        if reg:
            return reg["kind"], reg, True
        if s.startswith("pt-"):
            return "pt", {}, True
        if re.match(r"^m[A-Z]", symbol or "") or "rwa" in (tags or []):
            return "rwa", {}, False
        if re.search(r"usd|eur|gho|dai", s):
            return ("yield_stable" if erc4626 or s.startswith(("s", "st", "e", "x")) else "synthetic_stable"), {}, False
        if "btc" in s:
            return "wrapped_btc", {}, False
        if "eth" in s:
            return "lrt", {}, False
        return "unknown", {}, False

    def add_asset(self, a: dict, chain_id: int, depth: int) -> str:
        address = (a.get("address") or "").lower()
        aid = f"asset:{chain_id}:{address or (a.get('symbol') or '?').lower()}"
        if aid in self.g.nodes:
            return aid
        rpc = self.rpc(chain_id)
        info = rpc.inspect_contract(address) if address.startswith("0x") and len(address) == 42 else {}
        symbol = a.get("symbol") or info.get("symbol") or short(address)
        tags = a.get("tags") or []
        kind, reg, known = self.classify(symbol, tags, bool(info.get("erc4626_asset")))
        price = (a.get("price") or {}).get("usd")
        node = Node(aid, "asset", symbol, chain_id, address or None, attrs={
            "name": a.get("name"), "kind": kind, "price_usd": price, "tags": tags,
            "issuer": reg.get("issuer"), "backing": reg.get("backing"),
            "total_supply": (info.get("total_supply_raw") / 10 ** (a.get("decimals") or info.get("decimals") or 18)) if info.get("total_supply_raw") else None,
            "decimals": a.get("decimals") or info.get("decimals"),
            "upgradeable": bool(info.get("proxy_implementation")),
            "explorer": explorer(chain_id, address), "subtitle": kind.replace("_", " "),
        }, risk_factors={"offchain_backing": reg.get("offchain_backing", False), "unverified": not known,
                         **({"issuer_tier": reg["issuer_tier"]} if reg.get("issuer_tier") else {})})
        node.evidence.append(Evidence("registry" if reg else "heuristic",
                                      f"{symbol} classified as {kind}" + (f": {reg['backing']}" if reg.get("backing") else " from its symbol (unverified)"),
                                      ref=reg.get("source")))
        if info.get("reachable"):
            node.evidence.append(Evidence("rpc", f"totalSupply={node.attrs['total_supply']}, owner={info.get('owner')}, "
                                                 f"proxy impl={info.get('proxy_implementation')}, asset()={info.get('erc4626_asset')}",
                                          ref=f"{address} getCode/owner/totalSupply/asset", block=info.get("block")))
        if price and reg.get("peg"):
            node.risk_factors["price_deviation"] = abs(price / reg["peg"] - 1)
        self.g.add_node(node)

        if depth > MAX_ASSET_DEPTH:
            return aid

        # who can mint / upgrade
        if info.get("reachable"):
            self.add_mint_authority(aid, address, info, chain_id, reg)

        # issuer
        if kind != "pt" and reg.get("issuer_tier") != "none":
            issuer = reg.get("issuer") or f"Issuer of {symbol} (unverified)"
            iid = f"issuer:{issuer.lower()}"
            self.g.add_node(Node(iid, "issuer", issuer, None, None,
                                 attrs={"tier": reg.get("issuer_tier", "unknown"), "backing": reg.get("backing"), "url": reg.get("source")},
                                 risk_factors={"issuer_tier": reg.get("issuer_tier", "unknown")},
                                 evidence=[Evidence("registry" if reg else "heuristic",
                                                    reg.get("backing") or "Off-chain issuer not in registry: cannot be verified from chain data",
                                                    ref=reg.get("source"))]))
            self.link(aid, iid, "issued_by", Evidence("registry" if reg else "heuristic", f"{symbol} is issued and redeemed by {issuer}"))

        # Pendle PT -> market + underlying
        if kind == "pt" and address:
            pt = pendle.find_pt(address, chain_id)
            if pt:
                amm = f"amm:{chain_id}:{pt['market']}"
                exp = (pt.get("expiry") or "")[:10]
                self.g.add_node(Node(amm, "amm", f"Pendle market {pt['name']}", chain_id, pt["market"], attrs={
                    "liquidity_usd": pt.get("liquidity_usd"), "expiry": exp, "implied_apy": pt.get("implied_apy"),
                    "underlying": pt.get("underlying"),
                    "subtitle": f"expiry {exp} - liq ${(pt.get('liquidity_usd') or 0) / 1e6:,.1f}M", "url": pt["url"]},
                    evidence=[Evidence("pendle-api", f"PT market {pt['market']}, liquidity ${pt.get('liquidity_usd') or 0:,.0f}, expiry {exp}",
                                       ref=f"/v1/{chain_id}/markets/active")]))
                self.link(aid, amm, "traded_on", Evidence("pendle-api", "PT is priced and exited through this AMM"))
                self.link(amm, self.add_protocol("pendle_v2", chain_id), "runs_on", Evidence("pendle-api", "Pendle V2 AMM"))
                self.add_collateral_venues(aid, address, chain_id, amm)
                node.attrs["expiry"] = exp
                node.attrs["subtitle"] = f"Pendle PT - matures {exp}"
                if not node.attrs.get("price_usd") or not node.attrs.get("decimals"):
                    pa = pendle.asset(address, chain_id) or {}
                    node.attrs["price_usd"] = node.attrs.get("price_usd") or pa.get("price_usd")
                    node.attrs["decimals"] = node.attrs.get("decimals") or pa.get("decimals")
                if pt.get("underlying"):
                    ua = morpho.asset(pt["underlying"], chain_id) or {"address": pt["underlying"]}
                    uid = self.add_asset(ua, chain_id, depth + 1)
                    self.link(aid, uid, "backed_by", Evidence("pendle-api", f"PT redeems 1:1 into {self.g.nodes[uid].label} at maturity"))
            else:
                self.warnings.append(f"{symbol}: Pendle market not found among active markets")
                m = re.match(r"pt-(.+?)-\d", symbol.lower())
                if m:
                    self.add_symbolic_underlying(aid, m.group(1), chain_id, depth, "Underlying inferred from PT symbol")

        # ERC-4626 wrapper -> underlying
        elif info.get("erc4626_asset"):
            under = info["erc4626_asset"].lower()
            if under != address:
                ua = morpho.asset(under, chain_id) or {"address": under}
                uid = self.add_asset(ua, chain_id, depth + 1)
                self.link(aid, uid, "backed_by", Evidence("rpc", f"ERC-4626 asset() = {under}", ref=f"{address}.asset()", block=info.get("block")))
                node.attrs["wraps"] = self.g.nodes[uid].label
        elif reg.get("underlying_symbol"):
            self.add_symbolic_underlying(aid, reg["underlying_symbol"], chain_id, depth, "Registry: wraps / stakes the underlying")

        # bridged copy
        bridge = (reg.get("bridged") or {}).get(str(chain_id))
        if bridge:
            bid = f"bridge:{bridge.lower()}"
            self.g.add_node(Node(bid, "bridge", bridge, chain_id, None, attrs={"canonical": "canonical" in bridge.lower()},
                                 risk_factors={"canonical": "canonical" in bridge.lower()},
                                 evidence=[Evidence("registry", f"{symbol} on chain {chain_id} is a bridged copy via {bridge}")]))
            self.link(aid, bid, "bridged_via", Evidence("registry", f"Locked on Ethereum, minted on chain {chain_id}"))
            self.link(bid, self.add_chain(1), "settles_on", Evidence("registry", "Source chain"))
            self.link(bid, self.add_chain(chain_id), "settles_on", Evidence("registry", "Destination chain"))
        return aid

    def add_symbolic_underlying(self, aid: str, sym: str, chain_id: int, depth: int, why: str):
        addr = KNOWN_ADDRESSES.get(chain_id, {}).get(sym)
        ua = (morpho.asset(addr, chain_id) if addr else None) or {"address": addr, "symbol": sym.upper() if len(sym) <= 5 else sym}
        uid = self.add_asset(ua, chain_id, depth + 1)
        self.link(aid, uid, "backed_by", Evidence("registry", why))
        self.g.nodes[aid].attrs["wraps"] = self.g.nodes[uid].label

    def add_mint_authority(self, aid: str, address: str, info: dict, chain_id: int, reg: dict):
        rpc = self.rpc(chain_id)
        node = self.g.nodes[aid]
        impl, admin = info.get("proxy_implementation"), info.get("proxy_admin")
        if not impl:
            zos = rpc.batch([("eth_getStorageAt", [address, ZOS_IMPL_SLOT, "latest"]),
                             ("eth_getStorageAt", [address, ZOS_ADMIN_SLOT, "latest"])])
            from .sources.rpc import _slot_addr
            impl, admin = _slot_addr(zos[0]), admin or _slot_addr(zos[1])
        mm = rpc.batch([("eth_call", [{"to": address, "data": MASTER_MINTER}, "latest"])])[0]
        from .sources.rpc import _addr
        master_minter = _addr(mm)
        owner = info.get("owner")
        upgradeable = bool(impl)
        node.attrs["upgradeable"] = upgradeable
        controller, role = (master_minter, "masterMinter") if master_minter else (owner, "owner()") if owner else (admin, "proxy admin") if admin else (None, None)
        if reg.get("immutable") and not controller and not upgradeable:
            node.risk_factors["immutable"] = True
            return
        ctl = rpc.inspect_controller(controller) if controller else {"kind": "none_detected" if not upgradeable else "unknown"}
        # ProxyAdmin-style contracts: look one hop further to the real signer set.
        if ctl["kind"] == "contract" and controller:
            inner = rpc.inspect_contract(controller).get("owner")
            if inner:
                ctl2 = rpc.inspect_controller(inner)
                if ctl2["kind"] != "unknown":
                    ctl = {**ctl2, "via": controller}
                    controller = inner
        kind_label = {"eoa": "single key (EOA)", "multisig": f"multisig {ctl.get('threshold')}/{ctl.get('signers') or '?'}",
                      "timelock": f"timelock {int(ctl.get('min_delay_s') or 0) // 3600}h", "contract": "contract-controlled",
                      "unknown": "role-based (not resolved)", "none_detected": "no owner or proxy detected"}[ctl["kind"]]
        label = f"{node.label} minter: {kind_label}"
        mid = f"mint:{chain_id}:{address.lower()}"
        via = f" via {short(ctl['via'])}" if ctl.get("via") else ""
        self.g.add_node(Node(mid, "mint_authority", label, chain_id, controller, attrs={
            "role": role, "controller_kind": ctl["kind"], "upgradeable_token": upgradeable, "proxy_admin": admin,
            "implementation": impl, "explorer": explorer(chain_id, controller), "via": ctl.get("via"),
            "subtitle": f"{role or 'access control'}{via}" + (" - can upgrade code" if upgradeable else "")},
            risk_factors={"controller_kind": ctl["kind"], "threshold": ctl.get("threshold"), "signers": ctl.get("signers"),
                          "upgradeable": upgradeable, "immutable": False, "issuer_tier": reg.get("issuer_tier")},
            evidence=[Evidence("rpc", f"{role or 'controller'}={controller}; code check -> {ctl['kind']}"
                                      + (f" (threshold {ctl.get('threshold')})" if ctl.get("threshold") else "")
                                      + (f"; EIP-1967 implementation {impl}" if impl else ""),
                               ref=f"{address}.{role or 'owner()'}", block=rpc.block_number())]))
        self.link(aid, mid, "mintable_by", Evidence("rpc", f"{role or 'admin'} can mint or upgrade {node.label}"))

    # ------------------------------------------------------------------ layout hint
    def assign_layers(self):
        order = {"position": 0, "market": 1, "oracle": 2, "asset": 2, "amm": 3, "feed": 3,
                 "issuer": 4, "mint_authority": 4, "bridge": 4, "protocol": 5, "chain": 6}
        for n in self.g.nodes.values():
            # A collateral venue sits beside the PT's AMM: both are where the PT gets sold.
            n.layer = 3 if n.attrs.get("role") == "collateral_venue" else order.get(n.type, 3)
