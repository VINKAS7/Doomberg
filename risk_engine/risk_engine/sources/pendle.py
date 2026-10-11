"""Pendle v2 API: resolves a PT address to its market, underlying, maturity and liquidity, and quotes PT sales."""

from __future__ import annotations

from .http import SourceUnreachable, get_json

BASE = "https://api-v2.pendle.finance/core"


def _strip(chain_prefixed: str | None) -> str | None:
    if not chain_prefixed:
        return None
    return chain_prefixed.split("-", 1)[-1].lower()


def active_markets(chain_id: int) -> list[dict]:
    try:
        d = get_json(f"{BASE}/v1/{chain_id}/markets/active", ttl=900)
    except SourceUnreachable:
        raise  # without the market list every address would look like "not a PT"
    except Exception:
        return []
    return d.get("markets", []) if isinstance(d, dict) else []


def find_pt(pt_address: str, chain_id: int) -> dict | None:
    pt = pt_address.lower()
    for m in active_markets(chain_id):
        if _strip(m.get("pt")) == pt:
            det = m.get("details") or {}
            return {
                "market": m.get("address"),
                "name": m.get("name"),
                "expiry": m.get("expiry"),
                "pt": _strip(m.get("pt")),
                "yt": _strip(m.get("yt")),
                "sy": _strip(m.get("sy")),
                "underlying": _strip(m.get("underlyingAsset")),
                "liquidity_usd": det.get("liquidity"),
                "implied_apy": det.get("impliedApy"),
                "url": f"https://app.pendle.finance/trade/markets/{m.get('address')}?chain={chain_id}",
            }
    return None


def asset(address: str, chain_id: int) -> dict | None:
    """USD price and decimals of a Pendle asset (PT, YT, SY) from Pendle's own asset list."""
    try:
        d = get_json(f"{BASE}/v1/{chain_id}/assets/all", ttl=900)
    except Exception:
        return None
    rows = d if isinstance(d, list) else (d or {}).get("assets", [])
    a = next((x for x in rows if (x.get("address") or "").lower() == address.lower()), None)
    if not a:
        return None
    return {"price_usd": (a.get("price") or {}).get("usd"), "decimals": a.get("decimals")}


def swap_quote(market: str, pt: str, token_out: str, amount_raw: int, chain_id: int) -> dict | None:
    """Quote a complete PT sale into the requested exit token, including aggregator routing."""
    if amount_raw <= 0:
        return None
    try:
        d = get_json(f"{BASE}/v2/sdk/{chain_id}/markets/{market}/swap",
                     params={"receiver": "0x000000000000000000000000000000000000dEaD", "slippage": 0.01,
                             "enableAggregator": "true", "tokenIn": pt, "tokenOut": token_out, "amountIn": str(amount_raw)},
                     ttl=120)
    except Exception:
        return None
    data = (d or {}).get("data") or {}
    if data.get("priceImpact") is None:
        return None
    # Pendle reports impact as a signed change in price: negative means the seller gets less.
    try:
        amount_out = int(data["amountOut"])
        if amount_out <= 0:
            return None
        return {"amount_out": amount_out, "token_out": token_out,
                "price_impact": max(0.0, -float(data["priceImpact"])),
                "fee_usd": float((data.get("fee") or {}).get("usd") or 0)}
    except (KeyError, TypeError, ValueError):
        return None
