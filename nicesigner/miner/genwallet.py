#!/usr/bin/env python3
import json, time
import requests, base58, hashlib, hmac
import ecdsa

# ─── CONFIG ─────────────────────────────────────────────────────────────────────
RPC_URL      = "http://127.0.0.1:9876/"  # your B1T daemon RPC
RPC_USER     = "rpc"
RPC_PASSWORD = "rpc"
DUST_LIMIT   = 550                        # sats
FEE_RATE     = 1000                       # sats per kB (~0.01 B1T/kB)
# P2PKH version for B1T (0x19) and WIF version (0x9E)
ADDR_VERSION = 0x19
WIF_VERSION  = 0x9E
# ────────────────────────────────────────────────────────────────────────────────

def rpc(method, params=None):
    payload = {"jsonrpc":"1.0","id":"sender","method":method,"params":params or []}
    r = requests.post(RPC_URL, auth=(RPC_USER,RPC_PASSWORD),
                      headers={"Content-Type":"application/json"},
                      data=json.dumps(payload))
    r.raise_for_status()
    res = r.json()
    if res.get("error"):
        raise RuntimeError(res["error"])
    return res["result"]

def wif_to_privkey(wif: str) -> bytes:
    raw = base58.b58decode(wif)
    # raw = [version (1)] + privkey (32) + [0x01 if compressed] + checksum(4)
    if raw[0] != WIF_VERSION or len(raw) not in (38, 37):
        raise ValueError("Invalid WIF or wrong network byte")
    return raw[1:33]

def privkey_to_pubkey(privkey: bytes) -> bytes:
    sk = ecdsa.SigningKey.from_string(privkey, curve=ecdsa.SECP256k1)
    vk = sk.get_verifying_key().to_string()
    prefix = b"\x03" if (vk[32] & 1) else b"\x02"
    return prefix + vk[:32]

def pubkey_to_p2pkh(pubkey: bytes) -> str:
    h160 = hashlib.new("ripemd160", hashlib.sha256(pubkey).digest()).digest()
    ext  = bytes([ADDR_VERSION]) + h160
    chk  = hashlib.sha256(hashlib.sha256(ext).digest()).digest()[:4]
    return base58.b58encode(ext+chk).decode()

def list_utxos(addr):
    try: rpc("importaddress",[addr,"",False])
    except: pass
    return rpc("listunspent",[1,9999999,[addr]])

def estimate_fee(n_in, n_out):
    size = 180*n_in + 34*n_out + 10
    return (FEE_RATE*size + 999)//1000

def build_and_sign(private_wif, recipients, change_addr):
    # 1) key & own address
    priv = wif_to_privkey(private_wif)
    pub  = privkey_to_pubkey(priv)
    me   = pubkey_to_p2pkh(pub)
    # 2) gather UTXOs
    utxos = list_utxos(me)
    if not utxos:
        raise RuntimeError("No UTXOs to spend.")
    total_in = 0
    for u in utxos:
        sats = int(u["amount"]*1e8)
        total_in += sats
    # 3) compute required
    want = sum(r[1] for r in recipients)
    # one extra change output
    fee = estimate_fee(len(utxos), len(recipients)+1)
    if total_in < want + fee + DUST_LIMIT:
        raise RuntimeError(f"Need ≥{want+fee+DUST_LIMIT} sats, have {total_in}")
    change = total_in - want - fee
    # 4) make outputs map
    outs = { addr: sats/1e8 for addr,sats in recipients }
    outs[change_addr] = change/1e8
    # 5) RPC calls
    inputs = [ {"txid":u["txid"],"vout":u["vout"]} for u in utxos ]
    raw = rpc("createrawtransaction",[inputs, outs])
    signed = rpc("signrawtransaction",[raw, [], [private_wif]])
    if not signed.get("complete"):
        raise RuntimeError("Signing failed")
    return signed["hex"]

def main():
    print("\n== RB1TS Sender ==")
    wif = input("Enter your PRIVATE WIF (not stored): ").strip()
    total_rb = int(float(input("Total RB1TS to send: ").strip())*1e8)
    n = int(input("How many recipients? ").strip())
    recs = []
    sofar = 0
    for i in range(n):
        a = input(f" Recipient #{i+1} address: ").strip()
        sats = int(float(input(f"  → RB1TS sats to send to #{i+1}: ").strip())*1e8)
        recs.append((a, sats))
        sofar += sats
    if sofar != total_rb:
        raise RuntimeError(f"Sum of splits {sofar} ≠ total {total_rb}")
    change = input("Change address: ").strip()

    print("\nBuilding transaction…")
    txhex = build_and_sign(wif, recs, change)
    print("Raw TX hex:", txhex)
    if input("Broadcast? [y/N] ").lower().startswith("y"):
        txid = rpc("sendrawtransaction",[txhex])
        print("✅ TX broadcast:", txid)
    else:
        print("OK, not broadcast.")

if __name__=="__main__":
    main()
