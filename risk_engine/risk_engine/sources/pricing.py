"""Prices (DefiLlama) and size-aware swap quotes (KyberSwap aggregator)."""

from __future__ import annotations

import math
from .http import get_json

LLAMA_CHAINS = {1: "ethereum", 8453: "base", 42161: "arbitrum", 10: "optimism", 137: "polygon", 143: "monad"}
KYBER_CHAINS = {1: "ethereum", 8453: "base", 42161: "arbitrum", 10: "optimism", 137: "polygon"}
USDC = {
    1: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    8453: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    42161: "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    10: "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
    137: "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
}
USDT = {
    1: "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    8453: "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2",
    42161: "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
    10: "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58",
    137: "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
}


def price_usd(address: str, chain_id: int) -> dict | None:
    chain = LLAMA_CHAINS.get(chain_id)
    if not chain:
        return None
    key = f"{chain}:{address}"
    try:
        d = get_json(f"https://coins.llama.fi/prices/current/{key}", ttl=120)
        return d.get("coins", {}).get(key)
    except Exception:
        return None


def protocol_tvl(slug: str) -> float | None:
    try:
        v = get_json(f"https://api.llama.fi/tvl/{slug}", ttl=3600)
        return float(v)
    except Exception:
        return None


def exit_token(token_in: str, chain_id: int, excluded: set[str] | None = None) -> str | None:
    excluded = {address.lower() for address in (excluded or set())} | {token_in.lower()}
    return next((token for token in (USDC.get(chain_id), USDT.get(chain_id))
                 if token and token.lower() not in excluded), None)


def output_value_usd(token_out: str, amount_raw: int, chain_id: int) -> float | None:
    """Value proceeds independently of the route's own input/output marks. Never assume a $1 peg."""
    mark = price_usd(token_out, chain_id)
    if not mark:
        return None
    try:
        price, decimals = float(mark["price"]), int(mark["decimals"])
        if price <= 0 or not 0 <= decimals <= 36 or not math.isfinite(price):
            return None
        value = amount_raw / 10 ** decimals * price
        return value if math.isfinite(value) and value > 0 else None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def swap_quote(token_in: str, decimals: int, amount_usd: float, price: float, chain_id: int,
               token_out: str | None = None) -> dict | None:
    """Quote selling ``amount_usd`` worth of ``token_in`` for USDC. Returns price impact as a fraction."""
    chain = KYBER_CHAINS.get(chain_id)
    token_out = token_out or exit_token(token_in, chain_id)
    if token_out and token_in.lower() == token_out.lower():
        token_out = USDT.get(chain_id)
    if not chain or not token_out or not price or price <= 0:
        return None
    amount = int(amount_usd / price * 10 ** decimals)
    if amount <= 0:
        return None
    try:
        d = get_json(f"https://aggregator-api.kyberswap.com/{chain}/api/v1/routes",
                     params={"tokenIn": token_in, "tokenOut": token_out, "amountIn": str(amount)},
                     headers={"x-client-id": "belem-risk-engine"}, ttl=120)
    except Exception:
        return None
    rs = (d or {}).get("data", {}).get("routeSummary")
    if not rs:
        return None
    usd_in = float(rs.get("amountInUsd") or 0)
    gas = float(rs.get("gasUsd") or 0)
    # Raw output units are valued using an independent price and decimals;
    # route marks can hide slippage, and the output stablecoin may itself depeg.
    usd_out = output_value_usd(token_out, int(rs.get("amountOut") or 0), chain_id)
    if usd_out is None:
        return None
    return {
        "amount_in_usd": usd_in,
        "amount_out_usd": usd_out,
        "token_out": token_out,
        "gas_usd": gas,
        "price_impact": max(0.0, 1 - (usd_out - gas) / amount_usd),
        "route_hops": len(rs.get("route") or []),
    }
