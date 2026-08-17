# What changed in version 3 (Anonymous)

Version 2 was a transparent Bitcoin-like UTXO chain: every transaction revealed
the sender's public key, the recipient's address and the exact amount moved.
Version 3 adds **stealth addresses and linkable ring signatures** so that every
transaction is private by default. There is no "transparent mode" — anonymity is
always enforced.

## What changed

| Version 2 | Version 3 |
|---|---|
| **Transparent UTXOs.** Every output committed to `hash160(pubkey)` — a static 20-byte tag that identified the recipient and linked all payments to the same address. | **Stealth addresses (dual-key).** Every output commits to a one-time public key `P` that only the recipient can recognise. The sender picks a random ephemeral key `R` carried in the transaction. An address is two curve points: a view key `A` and a spend key `B`. Even if you send to the same address twice, the outputs look unrelated on-chain. |
| **ECDSA signatures.** Spending meant revealing the full public key and signing with ECDSA. Every input was trivially linked to its output. | **Linkable ring signatures (LSAG).** An input contains a ring of `N` one-time public keys. The signature proves you know the private key of *one* of them, without revealing which. A key image `K = x · H_p(P)` prevents double-spends: spending the same output twice yields the same `K`, which the network rejects. |
| **Address balances on the node.** `getbalance <address>`, `getaddresshistory`, the rich list — the node could show anyone's balance. | **Wallet-side scanning.** The node no longer knows which output belongs to whom. Balances and history can only be computed by a wallet that holds the view key. The RPC methods for transparent addresses are retired. |
| **Single-key wallet.** Each key was one secp256k1 private key exported as WIF. | **Dual-key wallet.** Each key pair is a `view_key : spend_key` string. The wallet scans every new block to find outputs it can spend. Decoy outputs are selected from the chain for each ring. |
| **Bitcoin-like transaction format.** Inputs referenced outpoints; outputs contained `(value, pubkey_hash)`. | **Ring-based format.** Inputs carry a ring of one-time keys and a key image. Outputs carry `(value, one_time_pubkey)`. The body is `version, inputs, outputs, lock_time, tx_public_key, extra`. |
| **Chain undo data.** Every block stored the coins it spent so a reorganisation could restore them. | **Append-only outputs.** Outputs are never removed on spend (the validator cannot tell which ring member was real). Only key images are tracked. Reorgs remove the outputs they created and the key images they recorded. |
| **Block explorer with address pages.** `/address/<addr>`, `/rich` showed balances. | **Explorer shows one-time keys.** Address pages and the rich list are gone. The explorer still shows blocks, transactions, the mempool and peers. Sanity over surveillance. |

## New crypto primitives

| Primitive | Curve | Construction |
|---|---|---|
| Stealth addresses | secp256k1 | Dual-key (CryptoNote): `P = H_s(r·A)·G + B` |
| Hash-to-point | secp256k1 | Try-and-increment: hash a counter, treat as x, check quadratic residue |
| Linkable ring signatures | secp256k1 | LSAG (Liu–Wei–Wong): `(c_0, r_0…r_{n-1}, K)` |
| Schnorr (helper) | secp256k1 | Standard non-interactive Schnorr, deterministic nonces |

Implements these in pure Python using the `ecdsa` library for curve arithmetic
and `cryptography` (OpenSSL) for the existing ECDSA signing path. One new
dependency: `ecdsa ≥ 0.18` (pure Python, secp256k1 point arithmetic).

## New features

* **Tor proxy support.** The wallet, miner and node can route their RPC traffic
  through a SOCKS5 proxy (default `127.0.0.1:9050`). The CLI accepts `--proxy
  HOST:PORT`; the GUI has a checkbox and host/port fields. The P2P socket can
  also be proxied through Tor.
* **Dandelion stem relay.** A locally-originated transaction is announced to a
  single random peer instead of being broadcast, so the first relay cannot tell
  which node created it. After the next mempool re-announce the transaction is
  broadcast normally.
* **Standard denominations.** The wallet automatically splits change outputs
  into powers-of-ten denominations (1, 10, 100 scar…) so that enough outputs of
  the same value exist on chain for large ring sizes.

## Consensus constraints of visible amounts

Because amounts are not hidden (no RingCT), the validator must know an input's
value without knowing which ring member is real. The consensus requires **all
ring members of an input to have the same value**. The input's value is that
common value.

The anonymity set of an input is therefore limited to outputs of the same value.
The network uses a target ring size of 16, falling back to 2 when not enough
same-value outputs exist.

This is the same model Monero used before RingCT (2017). It preserves supply
auditability at the cost of this constraint.

## Pruning and scalability

Outputs are **never removed on spend**, so the output set grows with the chain's
history. A pruned node removes block bodies but keeps the full output set and
key-image index. For a young chain this is not a concern; in future the ring's
decoy pool could be limited to recent outputs.

## Wallet compatibility

Version 2 wallet files use a single-key format (`wif`). Version 3 uses a
dual-key format (`view_wif:spend_wif`). There is no migration path from old
transparent wallets: keys that owned transparent UTXOs are meaningless on the
anonymous chain. The genesis starts fresh.

## Things that stayed

* The name, the ticker (SCT), the PoW consensus, the retargeting rules, the
  block header format and the money supply schedule.
* The P2P wire protocol framing (magic + command + payload + checksum).
* The JSON-RPC interface for block/transaction queries and mining.
* The MIT licence.
* `scarlet-node`, `scarlet-wallet`, `scarlet-miner` — same commands, new
  internals.
* Mostly pure Python, with the same minimal dependency footprint.

## Compatibility

None. Version 3 shares no address format, no transaction format and no wallet
format with version 2. Its genesis block is new. Version 2's transparent
addresses and UTXOs are retired entirely.