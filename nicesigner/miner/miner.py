#!/usr/bin/env python3
import json, time, math
import requests, base58
from bitcoinutils.transactions import Transaction
from bitcoinutils.setup        import setup

#
# ─── CONFIG ────────────────────────────────────────────────────────────────────
#

RPC_URL      = "http://127.0.0.1:9876/"
RPC_USER     = "rpc"
RPC_PASSWORD = "rpc"

ADDRESS      = ""
PRIVATE_WIF  = ""


TARGET        = 4      # leading zeroes required
TOTAL_THREADS = 6     # locktime tweaks per round
DUST          = 550    # sats minimum output

# Fee rate: 0.01 B1T per kB → sats/byte
MIN_FEE_PER_KB     = 0.01
RATE_SAT_PER_BYTE  = (MIN_FEE_PER_KB * 1e8) / 1000  # =1000 sats/byte

# Estimate TX size: 148B input + 34B output + 10B overhead
TX_SIZE_EST        = 148 + 34 + 10  # ≈192 bytes

# Compute base fee (rounded up)
FEE = int(math.ceil(RATE_SAT_PER_BYTE * TX_SIZE_EST))
print(f"Using fee={FEE} sats ({MIN_FEE_PER_KB} B1T/kB × {TX_SIZE_EST} B)")

#
# ─── END CONFIG ────────────────────────────────────────────────────────────────────
#

setup("mainnet")


def rpc(method, params=None):
    payload = {"jsonrpc":"1.0","id":"miner","method":method,"params":params or []}
    r = requests.post(RPC_URL, auth=(RPC_USER,RPC_PASSWORD),
                      headers={"Content-Type":"application/json"},
                      data=json.dumps(payload))
    try:
        r.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"RPC HTTP {e.response.status_code}: {e.response.text}")
    resp = r.json()
    if resp.get("error"):
        raise RuntimeError(f"RPC error: {resp['error']}")
    return resp["result"]


def fetch_utxo_rpc():
    """
    Import our address as watch-only and listunspent via RPC.
    Returns the largest UTXO > dust+fee as (txid, vout, sats, scriptPubKeyHex).
    """
    # watch-only import (idempotent)
    try:
        rpc("importaddress", [ADDRESS, "", False])
    except RuntimeError:
        pass

    utxos = rpc("listunspent", [0, 9999999, [ADDRESS]])
    candidates = []
    for u in utxos:
        sats = int(u["amount"] * 1e8)
        if sats > DUST + FEE:
            candidates.append((
                u["txid"], 
                u["vout"], 
                sats, 
                u["scriptPubKey"]
            ))

    if not candidates:
        raise RuntimeError(f"No UTXOs > {DUST+FEE} sats; fund {ADDRESS} and retry.")

    # return the largest
    return max(candidates, key=lambda x: x[2])


def build_and_find(txid, vout, sats, script_hex):
    round_idx = 0
    while True:
        for tweak in range(TOTAL_THREADS):
            locktime = round_idx * TOTAL_THREADS + tweak

            # create
            raw = rpc("createrawtransaction", [
                [{"txid":txid,"vout":vout,"sequence":0}],
                {ADDRESS: (sats - FEE)/1e8},
                locktime
            ])
            # sign
            signed = rpc("signrawtransaction", [
                raw,
                [{"txid":txid,"vout":vout,"scriptPubKey":script_hex,"amount":sats/1e8}],
                [PRIVATE_WIF]
            ])
            if not signed.get("complete"):
                continue

            hex_signed = signed["hex"]
            tid = Transaction.from_raw(hex_signed).get_txid()
            if tid.startswith("0"*TARGET):
                return hex_signed, locktime

        round_idx += 1


def send_with_bump(hex_signed, txid, vout, sats, script_hex, locktime):
    """
    Try sendrawtransaction; on "insufficient priority", bump fee 50% and retry.
    """
    global FEE
    try:
        return rpc("sendrawtransaction", [hex_signed])
    except RuntimeError as e:
        err = str(e)
        if "insufficient priority" in err:
            old = FEE
            FEE = int(math.ceil(FEE * 1.5))
            print(f"↗️  Priority too low; bumping fee {old}->{FEE} sats and retrying…")
            # rebuild & re-sign
            raw2 = rpc("createrawtransaction", [
                [{"txid":txid,"vout":vout,"sequence":0}],
                {ADDRESS: (sats - FEE)/1e8},
                locktime
            ])
            signed2 = rpc("signrawtransaction", [
                raw2,
                [{"txid":txid,"vout":vout,"scriptPubKey":script_hex,"amount":sats/1e8}],
                [PRIVATE_WIF]
            ])
            if not signed2.get("complete"):
                raise RuntimeError("Re-sign after fee bump failed")
            return rpc("sendrawtransaction", [signed2["hex"]])
        raise


def backup(batch, txid, locktime, sats, raw):
    fn = f"{batch}-{txid}.json"
    with open(fn,"w") as f:
        json.dump({
            "txid": txid,
            "locktime": locktime,
            "value": sats,
            "raw": raw
        }, f, indent=2)
    print(f"[backup] {fn}")


def mine_one(batch_name="mybatch"):
    # 1) pick the freshest UTXO from RPC
    txid, vout, sats, script_hex = fetch_utxo_rpc()
    print("Mining single tx from UTXO:", txid, vout, sats, "sats")

    # 2) hunt for a matching locktime
    hex_signed, locktime = build_and_find(txid, vout, sats, script_hex)
    print(f"✅ Found nice TX (locktime={locktime})")

    # 3) broadcast, retry on priority, accept already-spent
    while True:
        try:
            sent = send_with_bump(hex_signed, txid, vout, sats, script_hex, locktime)
            break
        except Exception as e:
            msg = str(e)
            if "bad-txns-inputs-spent" in msg:
                sent = Transaction.from_raw(hex_signed).get_txid()
                print(f"⚠️ Inputs already spent; assuming TX {sent} is live.")
                break
            print(f"⚠️ Broadcast error: {e}\n→ retrying in 2 minutes…")
            time.sleep(120)

    # 4) done
    assert sent == Transaction.from_raw(hex_signed).get_txid()
    backup(batch_name, sent, locktime, sats - FEE, hex_signed)
    print(f"🎉 Broadcast TX {sent}; exiting.")


if __name__ == "__main__":
    import sys
    mine_one(sys.argv[1] if len(sys.argv)>1 else "mybatch")
