import sys
import time

from risk_engine.discovery import Discovery, PositionRequest

addr = sys.argv[1] if len(sys.argv) > 1 else "0xecfafdc7741323a945a163ed068b5a3c43483957"
kind = "market" if len(addr) == 66 else "token"  # Morpho market ids are 32-byte hashes
chain = int(sys.argv[2]) if len(sys.argv) > 2 else 1
t = time.time()
g = Discovery(PositionRequest(kind, addr, chain, 1_000_000)).run()
print(f"{len(g.nodes)} nodes, {len(g.edges)} edges in {time.time() - t:.1f}s")
for n in sorted(g.nodes.values(), key=lambda n: n.layer):
    print(f"  L{n.layer} {n.type:15} {n.label[:45]:45} {n.attrs.get('subtitle') or ''} rf={ {k: v for k, v in n.risk_factors.items() if v} }")
for e in g.edges.values():
    print(f"  {e.source[:40]:40} -{e.type}-> {e.target[:40]} w={e.weight:.2f}")
print("warnings:", g.nodes["position"].attrs["warnings"])
