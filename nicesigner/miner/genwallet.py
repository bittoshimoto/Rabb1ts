#!/usr/bin/env python3
import hashlib
import hmac
from mnemonic import Mnemonic
import ecdsa
import base58

# --- CONFIGURE YOUR COIN PARAMETERS HERE ---
# BIP-44 coin type = 3141
COIN_TYPE = 3141
# P2PKH version byte for your coin (25 decimal → 0x19)
ADDR_VERSION = 0x19
# WIF secret-key version byte (158 decimal → 0x9E)
WIF_VERSION  = 0x9E

# BIP-44 path components (all hardened except the last two)
PATH = [
    44   | 0x80000000,   #  44'
    COIN_TYPE | 0x80000000,   # 3141'
    0    | 0x80000000,   #   0'
    0,                   #   0
    0,                   #   0
]


def mnemonic_to_seed(mnemonic: str, passphrase: str = "") -> bytes:
    """Derive BIP-39 seed from the mnemonic."""
    return hashlib.pbkdf2_hmac(
        "sha512",
        mnemonic.encode("utf-8"),
        ("mnemonic" + passphrase).encode("utf-8"),
        2048,
        dklen=64,
    )


def derive_bip32_master_key(seed: bytes):
    """BIP-32 master (privkey, chain code)."""
    I = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
    return I[:32], I[32:]


def derive_child(privkey: bytes, chain_code: bytes, index: int):
    """Derive one child key at `index` (handles hardened automatically)."""
    hardened = index & 0x80000000 != 0
    if hardened:
        data = b"\x00" + privkey + index.to_bytes(4, "big")
    else:
        sk = ecdsa.SigningKey.from_string(privkey, curve=ecdsa.SECP256k1)
        vk = sk.get_verifying_key().to_string()
        # compressed pubkey: 0x02/0x03 + X
        prefix = b"\x03" if vk[32] & 1 else b"\x02"
        data = prefix + vk[:32] + index.to_bytes(4, "big")

    I = hmac.new(chain_code, data, hashlib.sha512).digest()
    IL, IR = I[:32], I[32:]
    # child_priv = (IL + parent_priv) mod n
    n = ecdsa.SECP256k1.order
    child_int = (int.from_bytes(IL, "big") + int.from_bytes(privkey, "big")) % n
    return child_int.to_bytes(32, "big"), IR


def privkey_to_wif(privkey: bytes) -> str:
    """Encode a 32-byte privkey into WIF with your custom prefix."""
    ext = bytes([WIF_VERSION]) + privkey + b"\x01"  # compressed flag
    chk = hashlib.sha256(hashlib.sha256(ext).digest()).digest()[:4]
    return base58.b58encode(ext + chk).decode()


def pubkey_from_privkey(privkey: bytes) -> bytes:
    """Get compressed pubkey bytes from privkey."""
    sk = ecdsa.SigningKey.from_string(privkey, curve=ecdsa.SECP256k1)
    vk = sk.get_verifying_key().to_string()
    prefix = b"\x03" if vk[32] & 1 else b"\x02"
    return prefix + vk[:32]


def pubkey_to_p2pkh_address(pubkey: bytes) -> str:
    """Compute P2PKH address with your coin’s version byte."""
    h160 = hashlib.new("ripemd160", hashlib.sha256(pubkey).digest()).digest()
    ext  = bytes([ADDR_VERSION]) + h160
    chk  = hashlib.sha256(hashlib.sha256(ext).digest()).digest()[:4]
    return base58.b58encode(ext + chk).decode()


if __name__ == "__main__":
    # 1) generate mnemonic
    mnemo   = Mnemonic("english")
    mnemonic= mnemo.generate(128)
    print("mnemonic:", mnemonic)

    # 2) seed → master
    seed        = mnemonic_to_seed(mnemonic)
    master_priv, master_cc = derive_bip32_master_key(seed)

    # 3) walk down the path
    priv, cc = master_priv, master_cc
    for idx in PATH:
        priv, cc = derive_child(priv, cc, idx)

    # 4) WIF & address
    wif     = privkey_to_wif(priv)
    pubkey  = pubkey_from_privkey(priv)
    address = pubkey_to_p2pkh_address(pubkey)

    print("derivation_path: " + 
          "m/" + "/".join(
             (str(idx & 0x7FFFFFFF) + ("'" if idx & 0x80000000 else ""))
             for idx in PATH
          ))
    print("address:", address)
    print("private WIF:", wif)