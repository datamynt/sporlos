"""Forankrings-orkestrering: batch ikke-forankrede rollups → Merkle-rot → broadcast → lagre bevis.

broadcast_fn(root_hex) -> txid | None er pluggbar. bsv_broadcast (B2b) implementerer den
ekte on-chain-forankringen via merdata-mønsteret (krever funded BSV-lommebok). Returnerer
None til den er konfigurert, så hele pipelinen er trygt no-op uten wallet.

ÉN tx forankrer roten som dekker ALLE ikke-forankrede rollups (alle sites/dager) — skalerer
uendelig. Hver rollup beholder et uavhengig bevis (inklusjons-sti) mot den ankrede roten.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from app import store
from app.merkle import merkle_proof, merkle_root


_WOC = "https://api.whatsonchain.com/v1/bsv/main"
_ARC_ENDPOINTS = ["https://arc.gorillapool.io", "https://arc.taal.com"]


def build_anchor_tx(root_hex: str):
    """Bygg + signer en OP_RETURN-tx med Merkle-roten. Returnerer (tx, raw_hex) eller (None, grunn).
    Bruker WoC for UTXO + kilde-tx (engangs ved broadcast; IP-en vår, ikke besøkers)."""
    import httpx
    from bsv import PrivateKey, Transaction, TransactionInput, TransactionOutput, P2PKH
    from bsv.script.type import OpReturn

    wif = os.environ.get("SPORLOS_ANCHOR_WIF")
    if not wif:
        return None, "ingen wallet"
    priv = PrivateKey(wif)
    addr = priv.address()
    with httpx.Client(timeout=20) as c:
        utxos = c.get(f"{_WOC}/address/{addr}/unspent").json()
        if not utxos:
            return None, "ingen UTXO (ufunded?)"
        u = max(utxos, key=lambda x: x["value"])
        src = Transaction.from_hex(c.get(f"{_WOC}/tx/{u['tx_hash']}/hex").text)
    tx = Transaction()
    tx.add_input(
        TransactionInput(
            source_transaction=src,
            source_output_index=u["tx_pos"],
            unlocking_script_template=P2PKH().unlock(priv),
        )
    )
    tx.add_output(
        TransactionOutput(locking_script=OpReturn().lock([b"SPORLOS", bytes.fromhex(root_hex)]), satoshis=0)
    )
    tx.add_output(TransactionOutput(locking_script=P2PKH().lock(addr), change=True))
    tx.fee()
    tx.sign()
    return tx, tx.hex()


def bsv_broadcast(root_hex: str) -> str | None:
    """B2b: forankre roten on-chain via OP_RETURN + ARC. None hvis ikke konfigurert/feil."""
    import httpx

    tx, raw = build_anchor_tx(root_hex)
    if tx is None:
        return None  # ingen wallet/UTXO → trygt no-op
    txid = tx.txid()
    for ep in _ARC_ENDPOINTS:
        try:
            r = httpx.post(f"{ep}/v1/tx", json={"rawTx": raw}, timeout=30)
            if r.status_code in (200, 201):
                return r.json().get("txid") or txid
            if r.status_code == 409 or "already" in r.text.lower():
                return txid  # allerede kjent
        except Exception:
            continue
    return None


def anchor_pending(broadcast_fn=bsv_broadcast) -> dict:
    rows = store.pending_rollups()
    if not rows:
        return {"anchored": 0, "txid": None, "note": "ingenting å forankre"}
    leaves = [r["rollup_hash"] for r in rows]
    root = merkle_root(leaves)
    txid = broadcast_fn(root)
    if not txid:
        return {"anchored": 0, "txid": None, "root": root, "note": "ingen broadcaster konfigurert"}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    items = [
        {
            "site_id": r["site_id"],
            "day": str(r["day"])[:10],
            "proof": json.dumps(merkle_proof(leaves, i)),
            "root": root,
            "txid": txid,
            "anchored_at": now,
        }
        for i, r in enumerate(rows)
    ]
    store.record_anchor(items)
    return {"anchored": len(items), "txid": txid, "root": root}
