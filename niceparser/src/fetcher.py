import threading
import queue
import json
import time
import hashlib

import requests

from nicefetcher import indexer

from config import Config


class RSFetcher:
    def __init__(self, start_height=0):
        self.fetcher = indexer.Indexer(
            {
                "rpc_address": Config()["BACKEND_URL"],
                "rpc_user": "rpc",
                "rpc_password": "rpc",
                "db_dir": Config()["FETCHER_DB"],
                "start_height": start_height,
                "log_file": "/tmp/fetcher.log",
                "only_write_in_reorg_window": True,
                "batch_size": Config()["BATCH_SIZE"],
                "address_version": Config()["ADDRESS_VERSION"],
                "p2sh_address_version": Config()["P2SH_ADDRESS_VERSION"],   
            }
        )
        self.fetcher.start()
        self.prefeteched_block = queue.Queue(maxsize=10)
        self.prefetched_count = 0
        self.stopped_event = threading.Event()
        self.executors = []
        for _i in range(1):
            executor = threading.Thread(
                target=self.prefetch_block, args=(self.stopped_event,)
            )
            executor.daemon = True
            executor.start()
            self.executors.append(executor)

    def get_next_block(self, timeout=1.0):
        """
        Get the next block from the queue
        
        Args:
            timeout (float): Timeout in seconds to wait for a block
            
        Returns:
            dict or None: Block data or None if no block is available or if stopped
        """
        if self.stopped_event.is_set():
            return None  # Return None immediately if stopped
            
        try:
            return self.prefeteched_block.get(timeout=timeout)
        except queue.Empty:
            return None  # Return None if no block is available within timeout

    def prefetch_block(self, stopped_event):
        """
        Thread function that prefetches blocks
        
        Args:
            stopped_event (threading.Event): Event to signal thread to stop
        """
        while not stopped_event.is_set():
            # Check the queue size and skip if full
            if self.prefeteched_block.qsize() >= 8:  # Marge de sécurité par rapport à maxsize=10
                # Add sleep to prevent CPU spinning
                if stopped_event.wait(timeout=0.2):  # Vérifie l'arrêt toutes les 0.2 secondes
                    break  # Exit immediately if stopped
                continue

            # Get a block (non-blocking)
            try:
                block = self.fetcher.get_block_non_blocking()
            except Exception as e:
                if stopped_event.wait(timeout=0.2):
                    break
                continue
                
            # If no block available, wait and continue
            if block is None:
                if stopped_event.wait(timeout=0.2):
                    break
                continue

            block["tx"] = block.pop("transactions")
            
            # Try to add to queue with timeout
            attempt_count = 0
            while not stopped_event.is_set() and attempt_count < 5:  # Limite les tentatives
                try:
                    # Réduire le timeout pour être plus réactif à l'arrêt
                    success = self.prefeteched_block.put(block, timeout=0.5)
                    break
                except queue.Full:
                    attempt_count += 1
                    if stopped_event.wait(timeout=0.2):
                        break

    def stop(self):
        """Stop all threads and resources"""
        print("Arrêt du fetcher en cours...")
        
        # Signal all threads to stop
        self.stopped_event.set()
        
        # Stop the fetcher
        try:
            print("Arrêt de l'indexer sous-jacent...")
            self.fetcher.stop()
        except Exception as e:
            print(f"Erreur lors de l'arrêt de l'indexer: {e}")
        
        # Wait for executor threads to finish (with timeout)
        print(f"Attente de la fin des {len(self.executors)} threads...")
        max_wait = 5  # Attendre au maximum 5 secondes
        start_time = time.time()
        
        for i, thread in enumerate(self.executors):
            remaining_time = max(0, max_wait - (time.time() - start_time))
            if thread.is_alive():
                thread.join(timeout=remaining_time)
                if thread.is_alive():
                    print(f"Le thread {i} ne s'est pas terminé proprement")
        
        # Vider la queue pour éviter tout blocage
        try:
            while True:
                self.prefeteched_block.get_nowait()
        except queue.Empty:
            pass
            
        print("Fetcher arrêté")


class BitcoindRPCError(Exception):
    pass


def rpc_call(method, params):
    try:
        payload = {
            "method": method,
            "params": params,
            "jsonrpc": "2.0",
            "id": 0,
        }
        response = requests.post(
            Config()["BACKEND_URL"],
            data=json.dumps(payload),
            headers={"content-type": "application/json"},
            verify=(not Config()["BACKEND_SSL_NO_VERIFY"]),
            timeout=Config()["REQUESTS_TIMEOUT"],
        ).json()
        if "error" in response and response["error"]:
            raise BitcoindRPCError(f"Error calling {method}: {response['error']}")
        return response["result"]
    except (
        requests.exceptions.RequestException,
        json.decoder.JSONDecodeError,
        KeyError,
    ) as e:
        raise BitcoindRPCError(f"Error calling {method}: {str(e)}") from e


def deserialize_block(block_hex, block_index):
    deserializer = indexer.Deserializer(
        {
            "rpc_address": "",
            "rpc_user": "",
            "rpc_password": "",
            "network": Config().network_name,
            "db_dir": "",
            "log_file": "",
            "prefix": b"prefix",
        }
    )
    decoded_block = deserializer.parse_block(block_hex, block_index)
    decoded_block["tx"] = decoded_block.pop("transactions")
    return decoded_block


def adapt_auxpow_block(block_json):
    """
    Adapt AuxPoW block JSON from bitd to the unified block structure expected by the parser.
    This includes:
    - Adding 'is_op_return' to each vout (True if output is OP_RETURN)
    - Calculating and adding 'zero_count' to each transaction (number of leading zeros in txid)
    - Adding 'max_zero_count' to the block (max zero_count in block)
    - Renaming 'txid' to 'tx_id' and 'hash' to 'tx_hash' (and converting tx_id to bytes)
    - Generating 'utxo_id' for each vin and vout (8 bytes: first 8 bytes of SHA256(txid_bytes + n_bytes))
    """
    block = {
        "height": block_json["height"],
        "block_hash": block_json["hash"],
        "tx": block_json["tx"],
        "auxpow": block_json["auxpow"],
        "time": block_json.get("time"),
        "previousblockhash": block_json.get("previousblockhash"),
        "nextblockhash": block_json.get("nextblockhash"),
    }

    # Add 'is_op_return' to each vout
    for tx in block["tx"]:
        for vout in tx["vout"]:
            script_hex = vout.get("scriptPubKey", {}).get("hex", "")
            script_asm = vout.get("scriptPubKey", {}).get("asm", "")
            vout["is_op_return"] = script_hex.startswith("6a") or script_asm.startswith("OP_RETURN")

    # Calculate and add 'zero_count' to each tx, and 'max_zero_count' to block
    def count_leading_zeros(hex_str):
        count = 0
        for c in hex_str:
            if c == '0':
                count += 1
            else:
                break
        return count
    max_zero_count = 0
    for tx in block["tx"]:
        txid = tx.get("txid") or tx.get("tx_id")
        if txid is not None:
            zero_count = count_leading_zeros(txid)
            tx["zero_count"] = zero_count
            if zero_count > max_zero_count:
                max_zero_count = zero_count
        else:
            tx["zero_count"] = 0
    block["max_zero_count"] = max_zero_count

    # Rename 'txid' to 'tx_id' (as bytes) and 'hash' to 'tx_hash' for parser compatibility
    for tx in block["tx"]:
        if "txid" in tx:
            tx["tx_id"] = bytes.fromhex(tx.pop("txid"))
        elif "tx_id" in tx and isinstance(tx["tx_id"], str):
            tx["tx_id"] = bytes.fromhex(tx["tx_id"])
        if "hash" in tx:
            tx["tx_hash"] = tx.pop("hash")

    # Generate 'utxo_id' for each vin and vout (8 bytes: first 8 bytes of SHA256(txid_bytes + n_bytes))
    def make_utxo_id(txid_bytes, n):
        n_bytes = n.to_bytes(4, "little")
        h = hashlib.sha256(txid_bytes + n_bytes).digest()
        return h[:8]
    for tx in block["tx"]:
        for vout in tx["vout"]:
            vout["utxo_id"] = make_utxo_id(tx["tx_id"], vout["n"])
        for vin in tx["vin"]:
            if "coinbase" in vin:
                vin["utxo_id"] = b"coinbase"
            else:
                prev_txid_bytes = bytes.fromhex(vin["txid"])
                vin["utxo_id"] = make_utxo_id(prev_txid_bytes, vin["vout"])
    return block


def get_block_rpc(block_height):
    """
    Fetch a block by height, handling both normal and AuxPoW blocks.
    Returns None if the requested height is out of range (end of chain).
    """
    try:
        block_hash = rpc_call("getblockhash", [block_height])
    except BitcoindRPCError as e:
        if "Block height out of range" in str(e):
            # End of chain reached, stop fetching
            return None
        else:
            raise
    if block_height >= 1074:
        block_json = rpc_call("getblock", [block_hash, 2])
        if "auxpow" in block_json:
            return adapt_auxpow_block(block_json)
        else:
            raw_block = rpc_call("getblock", [block_hash, 0])
            decoded_block = deserialize_block(raw_block, block_height)
            return decoded_block
    else:
        raw_block = rpc_call("getblock", [block_hash, 0])
        decoded_block = deserialize_block(raw_block, block_height)
        return decoded_block


class PurePythonFetcher:
    def __init__(self, start_height=0):
        self.current_height = start_height

    def get_next_block(self, timeout=1.0):
        """
        Fetch the next block using get_block_rpc, handling AuxPoW and normal blocks.
        """
        block = get_block_rpc(self.current_height + 1)
        if block is not None:
            self.current_height += 1
        return block

# Example usage (replace RSFetcher with PurePythonFetcher for blocks >= 1074):
#
# if start_height < 1074:
#     fetcher = RSFetcher(start_height)
# else:
#     print("[DEBUG] Switching to PurePythonFetcher for AuxPoW blocks")
#     fetcher = PurePythonFetcher(start_height)
#
# Then use fetcher.get_next_block() in your main loop.
#
# You can implement logic in your main parse loop to switch fetchers at block 1074.