#!/usr/bin/env python3
import os
import json
import time
import math
import requests
from bitcoinutils.transactions import Transaction
from bitcoinutils.setup import setup

from multiprocessing import Process, Event, Value, cpu_count
from ctypes import c_ulonglong

CONFIG_FILE = "config.json"

def load_or_create_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    # else: prompt and save
    cfg = {
        "ADDRESS":      input("Address: ").strip(),
        "PRIVATE_WIF":  input("Private WIF: ").strip(),
        "RPC_URL":      input("RPC URL (e.g. http://127.0.0.1:9876/): ").strip(),
        "RPC_USER":     input("RPC User: ").strip(),
        "RPC_PASSWORD": input("RPC Password: ").strip(),
        "TARGET":       int(input("Target leading zeros: ") or "5"),
        "CORES":        min(int(input("Cores to use: ") or str(cpu_count())), cpu_count())
    }
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    print(f"\nSaved configuration to {CONFIG_FILE}\n")
    return cfg

# ─── CONFIG LOAD ───────────────────────────────────────────────────────────────
cfg = load_or_create_config()
ADDRESS      = cfg["ADDRESS"]
PRIVATE_WIF  = cfg["PRIVATE_WIF"]
RPC_URL      = cfg["RPC_URL"]
RPC_USER     = cfg["RPC_USER"]
RPC_PASSWORD = cfg["RPC_PASSWORD"]
TARGET       = cfg["TARGET"]
CORES        = cfg["CORES"]

# Fee rate: 0.01 B1T per kB → sats/byte
MIN_FEE_PER_KB    = 0.01
RATE_SAT_PER_BYTE = (MIN_FEE_PER_KB * 1e8) / 1000  # ≈1000 sats/byte
TX_SIZE_EST       = 148 + 34 + 10
FEE               = int(math.ceil(RATE_SAT_PER_BYTE * TX_SIZE_EST))
DUST              = 550

print(f"Fee={FEE} tos ({MIN_FEE_PER_KB} B1T/kB × {TX_SIZE_EST} B)")
print(f"→ Using {CORES} processes, target={TARGET} leading zeros\n")

setup("mainnet")

BLOCKBOOK_API = "https://blockbook.b1tcore.org/api/v2/utxo/"

def rpc(method, params=None):
    payload = {"jsonrpc":"1.0","id":"miner","method":method,"params":params or []}
    r = requests.post(RPC_URL, auth=(RPC_USER, RPC_PASSWORD), json=payload)
    r.raise_for_status()
    resp = r.json()
    if resp.get('error'):
        raise RuntimeError(resp['error'])
    return resp['result']

def fetch_utxo():
    resp = requests.get(f"{BLOCKBOOK_API}{ADDRESS}?confirmed=true")
    resp.raise_for_status()
    utxos = resp.json()
    candidates = []
    for u in utxos:
        sats = int(u["value"])
        if sats <= DUST + FEE:
            continue
        info = rpc("gettxout", [u["txid"], u["vout"], True])
        # if the UTXO isn't yet in our node's view (unconfirmed/spent), retry
        if info is None:
            print(f"[fetch_utxo] UTXO {u['txid']}:{u['vout']} not yet confirmed; retrying in 60s…")
            time.sleep(60)
            return fetch_utxo()
        script_hex = info["scriptPubKey"]["hex"]
        candidates.append((u["txid"], u["vout"], sats, script_hex))
    if not candidates:
        raise RuntimeError(f"No UTXOs > {DUST+FEE} sats; fund {ADDRESS} and retry.")
    return max(candidates, key=lambda x: x[2])

# shared between processes:
found_event    = Event()
total_attempts = Value(c_ulonglong, 0)
found_result   = Value('b', False)

def miner_thread(idx, txid, vout, sats, script_hex):
    sequence = idx
    prefix   = '0' * TARGET
    while not found_event.is_set():
        raw = rpc("createrawtransaction", [[{"txid":txid,"vout":vout,"sequence":sequence}],
                                           {ADDRESS:(sats-FEE)/1e8}, 0])
        signed = rpc("signrawtransaction", [raw, [{"txid":txid,"vout":vout,
                                                   "scriptPubKey":script_hex,
                                                   "amount":sats/1e8}],
                                              [PRIVATE_WIF]])
        with total_attempts.get_lock():
            total_attempts.value += 1

        if signed.get('complete'):
            hex_signed = signed['hex']
            tid = Transaction.from_raw(hex_signed).get_txid()
            if tid.startswith(prefix):
                # stash both hex+sequence in a small file
                with open("FOUND.json","w") as f:
                    json.dump({"hex":hex_signed,"seq":sequence}, f)
                found_result.value = True
                found_event.set()
                return

        sequence += CORES

def main():
    txid, vout, sats, script_hex = fetch_utxo()
    print(f"Mining from UTXO: {txid} vout={vout} sats={sats}\n")

    procs = []
    for i in range(CORES):
        p = Process(target=miner_thread,
                    args=(i, txid, vout, sats, script_hex),
                    daemon=True)
        p.start()
        procs.append(p)

    last = 0
    try:
        while not found_event.is_set():
            time.sleep(1)
            now = total_attempts.value
            rate = now - last
            last = now
            print(f"Hashrate: {rate:,} txid/s, total attempts: {now:,}", end="\r")
    except KeyboardInterrupt:
        print("\nAborted by user")
        found_event.set()

    for p in procs:
        p.join()

    if found_result.value:
        result = json.load(open("FOUND.json"))
        print(f"\n🥕 Carrots set sail through the zeros! (sequence={result['seq']})")
        sent = rpc("sendrawtransaction", [result["hex"]])
        print(f"🐇 RABB1TS uncovered in a sea of zeros! TXID: {sent}")
    else:
        print("\nNo result.")

if __name__ == '__main__':
    main()
