# What changed in version 2.2

Version 2.2 is a **consensus-breaking release** on top of the version 2 rewrite.
It adds pay-to-script-hash (multisig), replace-by-fee, hierarchical
deterministic wallets, deterministic signatures, link encryption and a native
mining backend. Because the transaction serialisation changed, the genesis block
was re-mined and every existing chain is reset: old databases are refused and
the three networks restart from new genesis blocks.

This document lists every change and the files it touches. The normative wire
format is [PROTOCOL.md](PROTOCOL.md); the honest list of remaining limitations
is in the [README](../README.md#honest-limitations).

* [Consensus changes](#consensus-changes)
* [Crypto and wallets](#crypto-and-wallets)
* [Networking](#networking)
* [Mining](#mining)
* [Node tooling](#node-tooling)
* [Testing](#testing)
* [New genesis and the reset](#new-genesis-and-the-reset)
* [Dependencies](#dependencies)
* [Compatibility](#compatibility)
* [Known limitation](#known-limitation)
* [Sibling projects](#sibling-projects)

## Consensus changes

These alter the transaction or block rules and required re-mining the genesis.

### New transaction serialisation (`core/transaction.py`)

* **Inputs** gained a `sequence` field (`uint32`) and a **witness stack**: a
  `varint` item count followed by `varbytes` items, replacing the fixed
  `public key + signature` pair. A P2PKH input's witness is
  `[public key, signature]`; a P2SH input's is `[redeem script, …arguments]`; a
  coinbase's is empty.
* **Outputs** gained a type byte (`uint8`, `0 = P2PKH`, `1 = P2SH`) before the
  value, and the 20-byte field is now a *hash* (public-key hash or script hash)
  instead of always a public-key hash.
* The signature hash was bumped to `ScarletCoin/sighash/2` and now also commits
  to a `script_code`: the type-and-hash of a P2PKH output, or the full redeem
  script of a P2SH output.

### Script language and P2SH (`core/script.py`, new)

A small, non-Turing-complete bytecode interpreter supports `OP_DUP`,
`OP_HASH160`, `OP_EQUAL`, `OP_EQUALVERIFY`, `OP_CHECKSIG`, `OP_CHECKMULTISIG`
and the data-push opcodes. `OP_CHECKMULTISIG` uses a clean stack layout (no
dummy element), `m` and `n` bounded to 1…16 and 1…15 keys, redeem scripts capped
at 520 bytes. `OP_HASH160` uses ScarletCoin's `hash256[:20]` convention.

### Replace-by-fee (`core/mempool.py`, `core/transaction.py`)

An input with `sequence < 0xfffffffe` marks its transaction replaceable. The
mempool now accepts a replacement that spends the same outputs when both sides
signal replaceability and the newcomer pays a strictly higher fee rate; a
non-signalling double spend is still refused. The mempool's coin view gained an
`ignored` set so a replacement can see the outputs it is about to double-spend.

### New address versions (`core/params.py`)

* P2SH addresses use a new `script_address_version`: mainnet `50` (prefix `M`),
  testnet and regtest `65` (prefix `T`). P2PKH prefixes are unchanged (`S`/`t`).
* `bip44_coin_type` was added (mainnet `0`, testnet/regtest `1`) for the wallet
  derivation path.

### Storage schema 3 (`core/storage.py`)

The UTXO table gained `type` and `payload` columns (replacing `pubkey_hash`),
and `SCHEMA_VERSION` is now `3`. The migration drops all tables and rebuilds
from scratch — this is the hard-fork reset, not an in-place migration.

### Checkpoints (`core/params.py`, `core/chain.py`)

`ChainParams` gained a `checkpoints: dict[height, hash]` map. A block whose hash
does not match the checkpoint at its height is rejected as invalid. No networks
ship checkpoints yet; the enforcement is in place and tested.

## Crypto and wallets

### Deterministic signatures (`crypto/keys.py`)

ECDSA signing now uses RFC 6979 deterministic nonces
(`deterministic_signing=True` on the `cryptography` `ECDSA` object), so signing
no longer depends on the system's random source for the nonce. Verified against
the RFC 6979 §A.2.5 secp256k1 vector in `tests/test_crypto.py`.

### Hierarchical deterministic wallets (`crypto/bip39.py`, `crypto/bip32.py`, new)

* `bip39.py` — mnemonic generation/validation and PBKDF2-HMAC-SHA512 seed
  derivation, with the official 2048-word English list bundled at
  `crypto/wordlist/english.txt`.
* `bip32.py` — BIP-0032 extended keys: master key derivation, hardened and
  non-hardened `CKDpriv`/`CKDpub`, `xprv`/`xpub` serialisation, and
  `m/…'` path parsing. Uses `ecdsa` for the elliptic-curve point arithmetic.
* Key fingerprints use `hash256(pubkey)[:4]` (ScarletCoin's `hash160`
  convention), which is the one deliberate deviation from BIP-0032.

### Wallet format 2 (`wallet/keystore.py`, `wallet/cli.py`, `gui/wallet_app.py`)

The wallet now stores an encrypted BIP-0039 seed and derives addresses along
`m/44'/coin_type'/0'/0/i`, instead of a list of independent WIF keys. Version 1
wallets still load and are written back as version 2 with their old keys kept as
imported keys. `scarlet-wallet create` prints the 12-word recovery phrase once,
and a new `scarlet-wallet restore PHRASE` command rebuilds a wallet from it.

## Networking

### Link encryption (`net/cipher.py`, new; `net/protocol.py`, `net/peer.py`, `net/node.py`)

After the `version` handshake, peers exchange ephemeral secp256k1 public keys
(carried in the `version` message), derive a shared key with ECDH + HKDF, and
encrypt every subsequent message with ChaCha20-Poly1305, one nonce counter per
direction. `PROTOCOL_VERSION` is now `2`; a peer that does not send an ephemeral
key (version 1) stays in the clear, so old and new nodes interoperate. The
envelope framing is unchanged for plaintext; encrypted links wrap only the
payload, and the checksum is taken over the ciphertext.

### Headers-first synchronisation (`net/protocol.py`, `core/storage.py`, `core/chain.py`, `net/node.py`)

Initial block download now happens in two phases. After the handshake a node
behind its peer sends `getheaders` and receives up to 2000 headers, which are
checked for proof of work, difficulty and their link to a known parent and
stored in a new `headers` table. Once the header chain is ahead, the missing
bodies are requested from any connected peer with `getdata`, in parallel, and
the process repeats until the chains agree. This removes the old one-peer,
full-block sequential download.

## Mining

### Native SHA-256 backend (`miner/_scan_nonces.c`, `miner/solver.py`)

A small C library implements the nonce scan (with the first-block midstate
optimisation). `solver.py` compiles it on first use and caches it under the
temporary directory, loading it through `ctypes`; when no C compiler is
available it silently falls back to the pure-Python loop. Both backends produce
identical results (asserted by a test). `tools/build_release.py` now bundles the
C source and the wordlist so PyInstaller builds carry both.

## Node tooling

* **`estimatefee` RPC** (`core/mempool.py`, `net/rpc.py`) — a rough fee-rate
  estimate as a percentile of the mempool's fee rates, floored at the relay
  minimum.
* **Prometheus metrics** (`net/rpc.py`) — `GET /metrics` exposes height, peers,
  mempool size, UTXO count, supply, difficulty and chain sizes in text format.
* **Live explorer updates** (`net/websocket.py`, new; `net/rpc.py`,
  `net/explorer.py`) — a WebSocket endpoint pushes `block`, `reorg` and `tx`
  events; the explorer reloads when a new block arrives. Enabled by default and
  reported in `getinfo` under `ws_port`.
* **Explorer favicon** — the browser wallet's icon is served at `/icon.svg` and
  `/favicon.ico`.
* **`sweep`** — already expressible as `scarlet-wallet send ADDRESS all` via
  `Wallet.send_everything`; no new command was needed.
* `validateaddress` now recognises P2SH addresses and reports the type.

## Testing

* `tests/test_bip39_bip32.py` — BIP-0039/0032 vectors, xprv/xpub round-trips,
  public-vs-private derivation agreement.
* `tests/test_script.py` — script engine semantics and end-to-end P2SH multisig
  spending through `check_transaction_inputs`.
* `tests/test_cipher.py` — encryption round-trip, tamper detection, version
  message with/without an ephemeral key.
* `tests/test_websocket.py` — WebSocket broadcast and node block-event push.
* `tests/test_properties.py` — `hypothesis` properties over target encoding,
  Base58, varbytes, amount formatting and coin selection.
* RBF, checkpoint and header-sync cases added to `tests/test_mempool.py` and
  `tests/test_chain.py`; `test_crypto.py` gained the RFC 6979 vector.
* The suite grew from 456 to 524 tests; `ruff` passes.

## New genesis and the reset

The transaction serialisation change altered the genesis coinbase, so all three
genesis blocks were re-mined with `tools/mine_genesis.py`:

| network | nonce | genesis hash |
|---|---|---|
| mainnet | 816 317 | `000006d229b8401ae0890255bd8a2dc65891f0018653eb0a1082d4f61cbf8027` |
| testnet | 154 650 | `000001806517072288ef3287e8c1203081318765df5a039ab37bdd2d6aad05c1` |
| regtest | 5 | `7812fcab2949f16309257b4813b1b3d478a06b938fd040a4784965b04885f518` |

Old databases are incompatible: the storage migration drops every table and the
chain restarts from the new genesis. The reference public node
(`scarletcoin.remotewire.net`) must have its database deleted and be restarted.

## Dependencies

* Added `ecdsa` (pure Python elliptic-curve arithmetic, used by BIP-0032 public
  derivation — the `cryptography` API does not expose point addition).
* Added `websockets` (pure Python, for the explorer's live-update endpoint).
* Added `hypothesis` to the development group.
* Version bumped to `2.2.0` in both `pyproject.toml` and
  `src/scarletcoin/__init__.py`.

## Compatibility

None. Like version 2 before it, version 2.2 shares no serialisation or wire format
with earlier releases, and its chains start from new genesis blocks. Private
keys remain valid secp256k1 keys, and version 1 wallet files are still readable
(they upgrade to format 2 on save).

## Known limitation

### Native secp256k1 for signature verification

`coincurve` and `python-secp256k1` do not build on Python 3.14 in this
environment (packaging/build failures), so the planned swap from `cryptography`
to libsecp256k1 for the ~1 300 tx/s ingestion ceiling was left out. `ecdsa` was
used where the extra curve primitives were actually needed (BIP-0032).

## Sibling projects

The browser wallet (`scarletcoin-web-wallet`) was updated in step: its
transaction serialisation, signature hash and coinbase building now match the
new format (sequence numbers, typed outputs, witness stacks, `sighash/2`), and
its golden fixtures were regenerated so the two implementations still agree
byte-for-byte. The browser wallet keeps the version-1 (WIF list) wallet file,
which the desktop wallet still imports as imported keys.
