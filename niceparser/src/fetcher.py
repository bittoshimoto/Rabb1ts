# niceparser/src/fetcher.py

import threading
import queue
import json
import time
import requests
import xxhash

from config import Config

def hash_string_and_number(input_bytes: bytes, input_num: int) -> bytes:
    # concatenate bytes + 8-byte little-endian number
    combined = input_bytes + input_num.to_bytes(8, "little")
    # xxh3_64 returns an integer
    h = xxhash.xxh3_64_intdigest(combined)
    # pack into 8-byte little endian
    return h.to_bytes(8, "little")

class BitcoindRPCError(Exception):
    pass

def rpc_call(method, params):
    """
    Call your daemon via JSON-RPC, return the 'result' or raise.
    """
    cfg = Config()
    payload = {
        "method":  method,
        "params":  params,
        "jsonrpc": "2.0",
        "id":      0,
    }
    r = requests.post(
        cfg["BACKEND_URL"],
        data=json.dumps(payload),
        headers={"content-type": "application/json"},
        auth=("rpc", "rpc"),
        timeout=cfg["REQUESTS_TIMEOUT"],
        verify=not cfg["BACKEND_SSL_NO_VERIFY"],
    )
    r.raise_for_status()
    resp = r.json()
    if resp.get("error"):
        raise BitcoindRPCError(f"RPC error {resp['error']}")
    return resp["result"]

def deserialize_block(block_hex, block_index):
    """
    Pure-Python stand-in for indexer.Deserializer.parse_block:
    """
    return {
        "hex":          block_hex,
        "height":       block_index,
        "transactions": []
    }

def get_block_rpc(block_height):
    """
    Fetch & decode a block at `block_height`, falling back from verbosity=2
    to raw-hex + deserializer if needed.
    """
    bh = rpc_call("getblockhash", [block_height])

    try:
        # Try JSON first (verbosity=2 strips AuxPoW envelope)
        blk = rpc_call("getblock", [bh, 2])
        transactions = blk.pop("tx", [])
    except BitcoindRPCError:
        # Fallback: get raw hex and deserialize via your Python wrapper
        raw_hex = rpc_call("getblock", [bh, 0])
        decoded = deserialize_block(raw_hex, block_height)
        # decoded includes 'transactions' as decoded["tx"]
        blk = decoded
        transactions = blk.pop("transactions", [])

    # inject the fields your parser expects
    blk["block_hash"]   = bh
    blk["height"]       = block_height
    blk["transactions"] = transactions

    return blk

class RSFetcher:
    def __init__(self, start_height=0):
        self.prefeteched_block = queue.Queue(maxsize=10)
        self.prefetched_count = 0
        self.stopped_event = threading.Event()
        self.start_height = start_height
        self.next_height  = start_height
        self.executors = []

        executor = threading.Thread(
            target=self.prefetch_block, args=(self.stopped_event,),
            daemon=True
        )
        executor.start()
        self.executors.append(executor)

    def get_next_block(self, timeout=1.0):
        """
        Get the next block from the queue.
        """
        if self.stopped_event.is_set():
            return None
        try:
            return self.prefeteched_block.get(timeout=timeout)
        except queue.Empty:
            return None

    def prefetch_block(self, stopped_event):
        """
        Thread function that prefetches blocks, waiting for new heights
        instead of hammering RPC when ahead of tip.
        """
        while not stopped_event.is_set():
            # 0) Don’t fetch past the current tip
            try:
                tip = rpc_call("getblockcount", [])
            except Exception as e:
                print(f"Error fetching tip: {e}")
                if stopped_event.wait(timeout=1):
                    break
                continue

            if self.next_height > tip:
                # Wait until a new block arrives
                if stopped_event.wait(timeout=5):
                    break
                continue

            # 1) Throttle if queue nearly full
            if self.prefeteched_block.qsize() >= 8:
                if stopped_event.wait(timeout=0.2):
                    break
                continue

            # 2) Fetch the block
            height = self.next_height
            try:
                block = get_block_rpc(height)
            except Exception as e:
                print(f"Error fetching block {height}: {e}")
                if stopped_event.wait(timeout=0.2):
                    break
                continue

            # 3) Extract and rename transactions
            txs = block.pop("transactions", [])
            normalized = []
            for tx in txs:
                # Precompute LE tx_id bytes
                txid_hex = tx.get("txid", "")
                try:
                    be = bytes.fromhex(txid_hex)
                    tx_bytes = be[::-1]            # big-endian → little-endian
                except Exception:
                    tx_bytes = b""

                # Normalize vouts (unchanged)
                vouts = []
                for v in tx.get("vout", []):
                    is_op = v.get("scriptPubKey", {}).get("type") == "nulldata"
                    out_entry = {
                        "n":            v.get("n"),
                        "value":        v.get("value"),
                        "is_op_return": is_op,
                    }
                    out_entry["utxo_id"] = hash_string_and_number(tx_bytes, out_entry["n"])
                    vouts.append(out_entry)

                # Compute zero_count (unchanged)
                zero_count = 0
                for c in txid_hex:
                    if c == "0":
                        zero_count += 1
                    else:
                        break

                # Normalize vins with LE utxo_id
                vins = []
                for vin in tx.get("vin", []):
                    prev_txid_hex = vin.get("txid", "")
                    prev_vout     = vin.get("vout", 0)
                    try:
                        be_prev = bytes.fromhex(prev_txid_hex)
                        prev_txid_bytes = be_prev[::-1]   # big-endian → little-endian
                    except Exception:
                        prev_txid_bytes = b""
                    vin_id = hash_string_and_number(prev_txid_bytes, prev_vout)
                    vin["utxo_id"] = vin_id
                    vins.append(vin)

                # Assemble normalized tx dict
                tx["tx_id"]      = tx_bytes
                tx["vout"]       = vouts
                tx["vin"]        = vins
                tx["zero_count"] = zero_count

                normalized.append(tx)

            # 4) Put normalized back on block
            block["tx"] = normalized

            # 5) Compute max_zero_count
            block["max_zero_count"] = max(
                (tx.get("zero_count", 0) for tx in normalized), default=0
            )

            # 6) Enqueue with retries
            for _ in range(5):
                if stopped_event.is_set():
                    break
                try:
                    self.prefeteched_block.put(block, timeout=0.5)
                    break
                except queue.Full:
                    if stopped_event.wait(timeout=0.2):
                        break

            self.next_height += 1

    def stop(self):
        """
        Stop all threads and clean up.
        """
        print("Arrêt du fetcher en cours...")
        self.stopped_event.set()

        print(f"Attente de la fin des {len(self.executors)} threads...")
        start = time.time()
        for i, t in enumerate(self.executors):
            t.join(timeout=max(0, 5 - (time.time() - start)))
            if t.is_alive():
                print(f"Le thread {i} ne s'est pas terminé proprement")

        # drain queue
        try:
            while True:
                self.prefeteched_block.get_nowait()
        except queue.Empty:
            pass

        print("Fetcher arrêté")
