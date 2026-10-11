"""Morpho GraphQL API client (https://api.morpho.org/graphql), used for PT-collateral markets and token metadata. Public, no key."""

from __future__ import annotations

from typing import Any

from .http import post_json

URL = "https://api.morpho.org/graphql"


class MorphoError(RuntimeError):
    pass


def query(q: str, variables: dict | None = None, ttl: float = 120) -> dict[str, Any]:
    res = post_json(URL, {"query": q, "variables": variables or {}}, ttl=ttl)
    if res.get("errors"):
        raise MorphoError("; ".join(e.get("message", "?") for e in res["errors"]))
    return res["data"]


ASSET = "address symbol name decimals tags price { usd }"

ORACLE = """oracle { address type data { __typename
  ... on MorphoChainlinkOracleV2Data { baseFeedOne { address } baseFeedTwo { address } quoteFeedOne { address } quoteFeedTwo { address }
        baseOracleVault { address } quoteOracleVault { address } }
  ... on MorphoChainlinkOracleData { baseFeedOne { address } baseFeedTwo { address } quoteFeedOne { address } quoteFeedTwo { address }
        baseOracleVault { address } } } }"""

MARKET = f"""marketId lltv irmAddress creationTimestamp
  loanAsset {{ {ASSET} }} collateralAsset {{ {ASSET} }}
  {ORACLE}
  morphoBlue {{ address }}
  state {{ supplyAssetsUsd borrowAssetsUsd liquidityAssetsUsd collateralAssetsUsd utilization supplyApy borrowApy dailyPriceVariation price }}
  badDebt {{ usd }} realizedBadDebt {{ usd }}
  warnings {{ type level }}"""


def market(market_id: str, chain_id: int) -> dict | None:
    try:
        d = query(f"query($k:String!,$c:Int!){{ marketById(marketId:$k, chainId:$c) {{ {MARKET} chain {{ id network }} }} }}",
                  {"k": market_id, "c": chain_id})
        return d.get("marketById")
    except MorphoError:
        d = query(f"query($k:[String!]){{ markets(first:1, where:{{ uniqueKey_in:$k }}) {{ items {{ {MARKET} chain {{ id network }} }} }} }}",
                  {"k": [market_id]})
        items = d["markets"]["items"]
        return items[0] if items else None


def markets_by_collateral(address: str, chain_id: int, first: int = 10) -> list[dict]:
    """Morpho markets that accept ``address`` as collateral, largest supply first."""
    try:
        d = query(f"query($a:[String!],$c:[Int!]){{ markets(first:{int(first)}, orderBy: SupplyAssetsUsd, orderDirection: Desc, "
                  f"where:{{ collateralAssetAddress_in:$a, chainId_in:$c }}) {{ items {{ {MARKET} }} }} }}",
                  {"a": [address], "c": [chain_id]})
    except MorphoError:
        return []
    return (d.get("markets") or {}).get("items") or []


def market_position(user: str, market_id: str, chain_id: int) -> dict | None:
    """A borrower's live position in one market: collateral and debt in USD, and Morpho's health factor."""
    try:
        d = query("query($u:String!,$k:String!,$c:Int!){ marketPosition(userAddress:$u, marketUniqueKey:$k, chainId:$c) "
                  "{ healthFactor state { collateral collateralUsd borrowAssets borrowAssetsUsd } } }",
                  {"u": user, "k": market_id, "c": chain_id}, ttl=60)
        return d.get("marketPosition")
    except MorphoError:
        return None


def asset(address: str, chain_id: int) -> dict | None:
    try:
        d = query(f"query($a:String!,$c:Int!){{ assetByAddress(address:$a, chainId:$c) {{ {ASSET} }} }}",
                  {"a": address, "c": chain_id}, ttl=600)
        return d.get("assetByAddress")
    except MorphoError:
        return None
