# 🐇 RABB1TS

> **RABB1TS** are on‐chain tokens on the B1T network—minted whenever you craft a TXID that begins with five (or more) zeroes!

---

## 📖 Table of Contents

1. [What Are RABB1TS?](#what-are-rabb1ts)
2. [How It Works](#how-it-works)
3. [Mining Guide](#mining-guide)
4. [Requirements](#requirements)
5. [Checking & Sending](#checking--sending)
6. [License](#license)

---

## ❓ What Are RABB1TS?

* **Tokens on the B1T chain**
* Minted when a TXID starts with `00000…`
* **Reward:** 1 RABB1T per valid TXID
* Assigned to the **TXID** itself (vs. sats, like Ordinals)

---

## ⚙️ How It Works

1. You craft a **single-input/output** transaction.
2. Tweak nonce/inputs until the resulting **TXID** begins with `00000…`.
3. Our indexer spots your “zeroed” TXID, **mints** you a RABB1T token, and **tracks** your balance.

---

## 🚀 Mining Guide

1. **Fund** one UTXO in your B1T wallet (keep its WIF key!).

2. **Clone** and run the miner:

   ```bash
   git clone https://github.com/bittoshimoto/Rabb1ts.git
   cd Rabb1ts/nicesigner/miner
   python3 miner.py
   ```

3. **Configure** when prompted:

   * **Address**
   * **WIF**
   * **RPC URL**, **User**, **Pass**
   * **Target zeros** (default: 5)
   * **CPU cores** to dedicate

4. Miner threads spin until a `00000…` TXID is found, then:

   * It **signs** and **broadcasts** the TX
   * Shows your **new TXID** (and mints your RABB1T!)

---

## 📋 Requirements

* A **funded UTXO** on B1T
* **RPC** access to a B1T node
* **Python 3**
* Libraries:

  * `nicesigner`
  * `bitcoinutils`
  * `requests`

Install dependencies with:

```bash
pip install nicesigner bitcoinutils requests
```

---

## 🔍 Checking & Sending

* **Website:** [https://rabb1t.org/](https://rabb1t.org/)
* **View Balances:** [https://rabb1t.org/balances](https://rabb1t.org/balances)
* **Send RABB1TS:** [https://rabb1t.org/send](https://rabb1t.org/send)

---

## 📄 License

Distributed under the [MIT License](LICENSE).
Feel free to open issues or submit pull requests!

Copyright: Ouziel Slama & Bit developers

---
