"""App-agnostisk Merkle-tre — gjenbrukbar forankrings-primitiv.

Batch mange hasher (f.eks. daglige rollup-hasher, eller bevis/avstemninger) i ett
tre, forankre ROTEN on-chain (én tx for tusenvis av hasher), og bevis hver enkelt
hash mot roten via en inklusjons-sti. Domeneuavhengig: Sporløs, beviset.no og
heltenig.no kan dele denne.

RFC-6962-stil for å unngå andre-preimage/duplikat-svakheter:
  blad = sha256(0x00 || data),  node = sha256(0x01 || venstre || høyre)
Odde noder promoteres oppover (ingen duplisering).
"""

from __future__ import annotations

import hashlib


def _sha(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def _leaf(hexstr: str) -> bytes:
    return _sha(b"\x00" + bytes.fromhex(hexstr))


def _node(left: bytes, right: bytes) -> bytes:
    return _sha(b"\x01" + left + right)


def _levels(leaf_hexes: list[str]) -> list[list[bytes]]:
    if not leaf_hexes:
        return [[_sha(b"")]]
    level = [_leaf(h) for h in leaf_hexes]
    levels = [level]
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level), 2):
            if i + 1 < len(level):
                nxt.append(_node(level[i], level[i + 1]))
            else:
                nxt.append(level[i])  # odde node promoteres opp
        level = nxt
        levels.append(level)
    return levels


def merkle_root(leaf_hexes: list[str]) -> str:
    """Roten (hex) av treet over leaf_hexes."""
    return _levels(leaf_hexes)[-1][0].hex()


def merkle_proof(leaf_hexes: list[str], index: int) -> list[dict]:
    """Inklusjons-sti for bladet på `index`: liste av {sibling, right}."""
    levels = _levels(leaf_hexes)
    proof, idx = [], index
    for lvl in levels[:-1]:
        if idx % 2 == 0:
            if idx + 1 < len(lvl):
                proof.append({"sibling": lvl[idx + 1].hex(), "right": True})
            # ellers promotert — ingen søsken på dette nivået
        else:
            proof.append({"sibling": lvl[idx - 1].hex(), "right": False})
        idx //= 2
    return proof


def verify_proof(leaf_hex: str, proof: list[dict], root_hex: str) -> bool:
    """Sant hvis bladet + stien gir den oppgitte roten."""
    h = _leaf(leaf_hex)
    for step in proof:
        sib = bytes.fromhex(step["sibling"])
        h = _node(h, sib) if step["right"] else _node(sib, h)
    return h.hex() == root_hex
