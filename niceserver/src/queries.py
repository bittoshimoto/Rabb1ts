import binascii
import requests
from contextlib import contextmanager
from config import SingletonMeta, Config

import apsw

from nicefetcher import utils

import xxhash

def hash_string_and_number(input_bytes: bytes, input_num: int) -> bytes:
    # same XXH3-64 little-endian combinator you used in fetcher.py
    combined = input_bytes + input_num.to_bytes(8, "little")
    h = xxhash.xxh3_64_intdigest(combined)
    return h.to_bytes(8, "little")


def rowtracer(cursor, sql):
    """Converts fetched SQL data into dict-style"""
    return {
        name: (bool(value) if str(field_type) == "BOOL" else value)
        for (name, field_type), value in zip(cursor.getdescription(), sql)
    }


def apsw_connect(filename):
    """Connect to SQLite database with optimal settings"""
    db = apsw.Connection(filename)
    cursor = db.cursor()
    cursor.execute("PRAGMA page_size = 4096")
    cursor.execute("PRAGMA auto_vacuum = 0")
    cursor.execute("PRAGMA synchronous = NORMAL")
    cursor.execute("PRAGMA journal_size_limit = 6144000")
    cursor.execute("PRAGMA cache_size = 10000")
    cursor.execute("PRAGMA defer_foreign_keys = ON")
    cursor.execute("PRAGMA journal_mode = WAL")
    cursor.execute("PRAGMA locking_mode = NORMAL")

    db.setbusytimeout(5000)
    db.setrowtrace(rowtracer)
    cursor.close()
    return db


def get_shard_id(utxo_id):
    """Determine the shard ID based on the first byte of utxo_id"""
    return utxo_id[0] % 10


class APSWConnectionPool():
    def __init__(self, db_file):
        self.connections = []
        self.db_file = db_file
        self.closed = False
        self.pool_size = 10

    @contextmanager
    def connection(self):
        if self.connections:
            # Reusing connection
            db = self.connections.pop(0)
        else:
            # New db connection
            db = apsw_connect(self.db_file)
        try:
            yield db
        finally:
            if self.closed:
                db.close()
            elif len(self.connections) < self.pool_size:
                # Add connection to pool
                self.connections.append(db)
            else:
                # Too much connections in the pool: closing connection
                db.close()

    def close(self):
        self.closed = True
        while len(self.connections) > 0:
            db = self.connections.pop()
            db.close()


class ShardedConnectionPool:
    """Connection pool for multiple sharded databases"""

    def __init__(self, base_path):
        self.base_path = base_path
        self.pools = {}
        self.closed = False

    def get_shard_pool(self, shard_id):
        """Get or create a connection pool for a specific shard"""
        if shard_id not in self.pools:
            db_file = f"{self.base_path}/rb1ts_balances_{shard_id}.db"
            print(f"Opening shard {shard_id} at {db_file}")
            self.pools[shard_id] = APSWConnectionPool(db_file)
        return self.pools[shard_id]

    @contextmanager
    def shard_connection(self, shard_id):
        """Get a connection for a specific shard"""
        pool = self.get_shard_pool(shard_id)
        with pool.connection() as conn:
            yield conn

    def close(self):
        """Close all connection pools"""
        self.closed = True
        for pool in self.pools.values():
            pool.close()


def utxo_to_utxo_id(txid_hex: str, n: int) -> bytes:
    """
    Convert RPC hex TXID + vout into the same little-endian xxh3 key
    your parser used when writing to the DB.
    """
    # 1) Turn into bytes and reverse to little-endian
    be = bytes.fromhex(txid_hex)
    tx_bytes = be[::-1]

    # 2) Apply the XXH3‐64+LE routine
    return hash_string_and_number(tx_bytes, n)


class Rb1tsQueries:
    """
    Classe pour effectuer des requêtes sur les données RB1TS
    Remarque: Cette implémentation accède directement aux bases de données
    mais est compatible avec l'architecture où ces bases sont gérées par des processus séparés
    """

    def __init__(self, db_file=None):
        base_path = Config()["BALANCES_STORE"]
        if db_file is None:
            db_file = f"{base_path}/rb1ts_indexes.db"
        self.base_path = base_path
        self.pool = APSWConnectionPool(db_file)
        self.shard_pools = ShardedConnectionPool(base_path)

        # Cache for statistics
        self._stats_cache = None
        self._stats_cache_block_height = 0
        self._latest_nicehashes_cache = None
        self._latest_nicehashes_cache_block_height = 0
    
    def get_balance_by_address(self, address):
        """
        Fetch UTXOs for an address using B1T Explorer’s getrawtransaction API,
        convert them to utxo_id, and sum up their RB1TS balances.

        Args:
            address (str): B1T address

        Returns:
            dict: Balance information including total balance and UTXOs
        """
        total_balance = 0
        utxo_details = []

        # 1) First, fetch the list of txids involving this address
        try:
            list_url = f"https://b1texplorer.com/ext/getaddresstxs/{address}/0/100"
            resp = requests.get(list_url, timeout=10)
            resp.raise_for_status()
            txs = resp.json()
        except Exception as e:
            print(f"[ERROR] fetching tx list for {address}: {e}")
            return {"address": address, "total_balance": 0, "utxos": []}

        for entry in txs:
            txid = entry.get("txid")
            if not txid:
                continue

            # 2) Fetch the full raw transaction with decrypt=1
            try:
                raw_url = (
                    f"https://b1texplorer.com/api/getrawtransaction"
                    f"?txid={txid}&decrypt=1"
                )
                r2 = requests.get(raw_url, timeout=10)
                r2.raise_for_status()
                tx = r2.json()
            except Exception as e:
                print(f"[WARN] could not fetch raw tx {txid}: {e}")
                continue

            # 3) Scan its vouts for any outputs to our address
            for v in tx.get("vout", []):
                # match by scriptPubKey.addresses
                addrs = v.get("scriptPubKey", {}).get("addresses", [])
                if address not in addrs:
                    continue

                n = v.get("n")
                value = v.get("value", 0)
                # convert to satoshis
                sats = int(value * 1e8)

                # 4) map that (txid,n) → utxo_id
                utxo_id = utxo_to_utxo_id(txid, n)
                shard = get_shard_id(utxo_id)

                # 5) lookup in local shard DB
                with self.shard_pools.shard_connection(shard) as db:
                    row = db.cursor().execute(
                        "SELECT balance FROM balances WHERE utxo_id = ?",
                        (utxo_id,),
                    ).fetchone()

                bal = row["balance"] if row else 0
                if bal > 0:
                    total_balance += bal
                    human = f"{utils.inverse_hash(txid)}:{n}"
                    utxo_details.append({"utxo": human, "balance": bal})

        return {
            "address": address,
            "total_balance": total_balance,
            "utxos": utxo_details,
        }

    def get_latest_nicehashes(self, limit=50):
        """
        Return the most recent nicehashes with their rewards and block heights.

        Args:
            limit (int): Number of nicehashes to return

        Returns:
            list: List of dictionaries containing nicehash information
        """
        # Check if we can use cached result
        with self.pool.connection() as db:
            cursor = db.cursor()
            max_height = cursor.execute(
                "SELECT MAX(height) as max_height FROM blocks"
            ).fetchone()
            current_max_height = (
                max_height["max_height"]
                if max_height and max_height["max_height"] is not None
                else 0
            )

            # Use cache if available and still valid
            if (
                self._latest_nicehashes_cache is not None
                and self._latest_nicehashes_cache_block_height == current_max_height
            ):
                return self._latest_nicehashes_cache

            # Query the latest nicehashes
            nicehashes = []
            for row in cursor.execute(
                """
                SELECT height, txid, reward 
                FROM nicehashes 
                ORDER BY height DESC 
                LIMIT ?
                """,
                (limit,),
            ):
                nicehashes.append(
                    {
                        "height": row["height"],
                        "txid": row["txid"],
                        "reward": row["reward"],
                    }
                )

            # Update cache
            self._latest_nicehashes_cache = nicehashes
            self._latest_nicehashes_cache_block_height = current_max_height

            return nicehashes

    def get_stats(self):
        """
        Return various statistics about the system.

        Returns:
            dict: Statistics including RB1TS supply, nice hashes count, etc.
        """
        with self.pool.connection() as db:
            cursor = db.cursor()

            rows = cursor.execute("SELECT * FROM stats").fetchall()
            stats = {row["key"]: row["value"] for row in rows}
            stats["supply"] = int(stats["supply"])
            stats["supply_check"] = int(stats["supply_check"])
            stats["utxos_count"] = int(stats["utxos_count"])
            stats["nice_hashes_count"] = int(stats["nice_hashes_count"])
            stats["max_zero"] = int(stats["max_zero"])
            stats["last_parsed_block"] = int(stats["last_parsed_block"])

            return stats
