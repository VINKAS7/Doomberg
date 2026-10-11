"""Minimal JSON-RPC reader for the on-chain facts the graph needs.

Only raw ``eth_call`` / ``eth_getCode`` / ``eth_getStorageAt`` so there is no web3 dependency.
"""

from __future__ import annotations

import os
import time
from typing import Any

from .http import cached, send

DEFAULT_RPCS = {
    1: "https://ethereum-rpc.publicnode.com",
    8453: "https://base-rpc.publicnode.com",
    42161: "https://arbitrum-one-rpc.publicnode.com",
    10: "https://optimism-rpc.publicnode.com",
    137: "https://polygon-bor-rpc.publicnode.com",
    143: "https://rpc.monad.xyz",
}

SEL = {
    "description": "0x7284e416",
    "latestRoundData": "0xfeaf968c",
    "decimals": "0x313ce567",
    "totalSupply": "0x18160ddd",
    "owner": "0x8da5cb5b",
    "asset": "0x38d52e0f",
    "getThreshold": "0xe75235b8",
    "getOwners": "0xa0e67e2b",
    "getMinDelay": "0xf27a0c92",
    "symbol": "0x95d89b41",
}
IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920c3ca505d382bbc"
ADMIN_SLOT = "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103"
ZERO = "0x" + "0" * 40


def rpc_url(chain_id: int) -> str | None:
    return os.getenv(f"RPC_URL_{chain_id}") or (os.getenv("RPC_URL") if chain_id == 1 else None) or DEFAULT_RPCS.get(chain_id)


class Rpc:
    def __init__(self, chain_id: int):
        self.chain_id = chain_id
        self.url = rpc_url(chain_id)
        self.ok = self.url is not None
        self._block: int | None = None

    def batch(self, calls: list[tuple[str, list]]) -> list[Any]:
        if not self.ok or not calls:
            return [None] * len(calls)
        key = f"rpc {self.chain_id} {calls}"

        def run():
            body = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)]
            out: list[Any] = [None] * len(calls)
            for chunk_start in range(0, len(body), 40):
                chunk = body[chunk_start:chunk_start + 40]
                try:
                    r = send("POST", self.url, json=chunk, timeout=20)
                    res = r.json()
                except Exception:
                    continue
                if isinstance(res, dict):
                    res = [res]
                for item in res:
                    if isinstance(item, dict) and "result" in item and isinstance(item.get("id"), int):
                        out[item["id"]] = item["result"]
            return out

        try:
            return cached(key, 300, run)
        except Exception:
            self.ok = False
            return [None] * len(calls)

    def block_number(self) -> int | None:
        if self._block is None:
            r = self.batch([("eth_blockNumber", [])])[0]
            self._block = int(r, 16) if r else None
        return self._block

    def calls(self, to: str, names: list[str]) -> dict[str, str | None]:
        res = self.batch([("eth_call", [{"to": to, "data": SEL[n]}, "latest"]) for n in names])
        return {n: (r if r and r != "0x" else None) for n, r in zip(names, res)}

    def inspect_contract(self, address: str) -> dict[str, Any]:
        """Code size, proxy admin/implementation, owner, totalSupply, ERC4626 asset in one batch."""
        names = ["owner", "totalSupply", "asset", "decimals", "symbol"]
        calls = [("eth_getCode", [address, "latest"]),
                 ("eth_getStorageAt", [address, IMPL_SLOT, "latest"]),
                 ("eth_getStorageAt", [address, ADMIN_SLOT, "latest"])]
        calls += [("eth_call", [{"to": address, "data": SEL[n]}, "latest"]) for n in names]
        res = self.batch(calls)
        code, impl, admin, *vals = res
        out: dict[str, Any] = {"reachable": code is not None}
        out["is_contract"] = bool(code and code != "0x")
        out["proxy_implementation"] = _slot_addr(impl)
        out["proxy_admin"] = _slot_addr(admin)
        raw = dict(zip(names, vals))
        out["owner"] = _addr(raw["owner"])
        out["total_supply_raw"] = _uint(raw["totalSupply"])
        out["erc4626_asset"] = _addr(raw["asset"])
        out["decimals"] = _uint(raw["decimals"])
        out["symbol"] = _string(raw["symbol"])
        out["block"] = self.block_number()
        return out

    def inspect_controller(self, address: str) -> dict[str, Any]:
        """Classify who controls a contract: EOA, Safe multisig (threshold), timelock, or other contract."""
        res = self.batch([
            ("eth_getCode", [address, "latest"]),
            ("eth_call", [{"to": address, "data": SEL["getThreshold"]}, "latest"]),
            ("eth_call", [{"to": address, "data": SEL["getOwners"]}, "latest"]),
            ("eth_call", [{"to": address, "data": SEL["getMinDelay"]}, "latest"]),
        ])
        code, thr, owners, delay = res
        if code is None:
            return {"kind": "unknown"}
        if code in ("0x", "0x0"):
            return {"kind": "eoa"}
        threshold = _uint(thr)
        if threshold:
            return {"kind": "multisig", "threshold": threshold, "signers": _addr_array_len(owners)}
        min_delay = _uint(delay)
        if min_delay is not None and delay not in (None, "0x"):
            return {"kind": "timelock", "min_delay_s": min_delay}
        return {"kind": "contract"}

    def read_feed(self, address: str) -> dict[str, Any]:
        r = self.calls(address, ["description", "latestRoundData", "decimals"])
        out: dict[str, Any] = {"description": _string(r["description"]), "decimals": _uint(r["decimals"])}
        lrd = r["latestRoundData"]
        if lrd and len(lrd) >= 2 + 64 * 5:
            words = [lrd[2 + 64 * i: 2 + 64 * (i + 1)] for i in range(5)]
            answer = int(words[1], 16)
            if answer >= 2 ** 255:
                answer -= 2 ** 256
            updated = int(words[3], 16)
            out["answer"] = answer / 10 ** (out["decimals"] or 8)
            out["updated_at"] = updated
            out["age_s"] = int(time.time() - updated) if updated else None
        return out


def _word(h: str | None, i: int = 0) -> str | None:
    if not h or len(h) < 2 + 64 * (i + 1):
        return None
    return h[2 + 64 * i: 2 + 64 * (i + 1)]


def _uint(h: str | None) -> int | None:
    w = _word(h)
    return int(w, 16) if w else None


def _addr(h: str | None) -> str | None:
    w = _word(h)
    if not w or int(w[:24] or "0", 16) != 0:
        return None
    a = "0x" + w[24:]
    return None if a == ZERO else a


def _slot_addr(h: str | None) -> str | None:
    if not h or len(h) < 42:
        return None
    a = "0x" + h[-40:]
    return None if int(a, 16) == 0 else a


def _addr_array_len(h: str | None) -> int | None:
    w = _word(h, 1)
    return int(w, 16) if w else None


def _string(h: str | None) -> str | None:
    if not h or len(h) < 2 + 128:
        return None
    try:
        length = int(h[66:130], 16)
        raw = bytes.fromhex(h[130:130 + length * 2])
        return raw.decode("utf-8", "ignore").strip("\x00") or None
    except Exception:
        try:
            return bytes.fromhex(h[2:66]).rstrip(b"\x00").decode("utf-8", "ignore") or None
        except Exception:
            return None
