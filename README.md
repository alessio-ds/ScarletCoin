# ScarletCoin (SCT)

A complete, working proof-of-work blockchain written 100% in Python. Node,
wallet, miner, explorer — every line of it, in Python.

ScarletCoin is a real cryptocurrency: a UTXO-based blockchain, a peer-to-peer
network that reaches consensus on its own, deterministic wallets with proper
keys and signatures, a CPU miner, and a live block explorer served by every
node. It runs the same consensus model as Bitcoin — proof of work, difficulty
retargeting, halvings, a hard supply cap — with faster blocks and a modern
feature set (BIP-39/32/44 wallets, P2SH multisig, replace-by-fee, encrypted
P2P links).

**Version 2.2.4 · Mainnet is live and mined · MIT licensed**

```
┌────────────────┐   getblocktemplate / submitblock   ┌───────────────┐
│  scarlet-miner │◄─────────── JSON-RPC ─────────────►│               │
└────────────────┘                                    │               │        ┌───────────────┐
                                                      │  scarlet-node │◄─ P2P ►│  other nodes  │
┌────────────────┐   balances, history, broadcast     │               │        └───────────────┘
│ scarlet-wallet │◄─────────── JSON-RPC ─────────────►│               │
└────────────────┘   (keys never leave the wallet)    └───────┬───────┘
                                                              │ HTTP
                                                     browser ─┘ block explorer
```

---

## Whitepaper

### Abstract

ScarletCoin is a peer-to-peer electronic cash system: a chain of blocks whose
validity is checked by every participant, so no participant has to be trusted.
Coins exist as unspent transaction outputs (UTXOs) locked to public-key hashes;
spending one means revealing the key and producing a valid ECDSA signature.
Miners compete to solve SHA-256d proof of work; the chain with the greatest
cumulative work wins, and any node that sees a heavier branch reorganises to it
automatically. The money supply is fixed by consensus rules that no party can
change: a geometric subsidy schedule converging to a 21,000,000 SCT cap.

### Monetary policy

| Parameter | Value |
|---|---|
| Ticker / unit | **SCT** · 1 SCT = 100,000,000 *scar* (smallest indivisible unit) |
| **Maximum supply** | **21,000,000 SCT** (21,000,000,000,000,000 scar) |
| Initial block subsidy | 50 SCT |
| **Halving** | every **210,000 blocks** (~4 years at 1 block/min) |
| Subsidy schedule | 50 → 25 → 12.5 → 6.25 → … → 0 scar after 33 halvings (~13 years) |
| Supply curve | strictly convergent: the sum of all subsidies + fees can never exceed the cap |
| Coinbase maturity | 100 confirmations before mined coins can be spent |
| Premine | none — the genesis coinbase pays a provably unspendable hash |

Because the subsidy halves on a fixed schedule, the total ever minted is
`210,000 × 50 × (1 + ½ + ¼ + … ) = 21,000,000` SCT — exactly. Fees are paid
from existing coins, so they never increase the supply.

### Consensus

| Parameter | Value |
|---|---|
| Consensus algorithm | Proof of work, **SHA-256d** (double SHA-256) |
| Block time | **60 seconds** (target) |
| Difficulty retarget | every **60 blocks** (~1 hour), capped at **4× per retarget** |
| Chain selection | greatest cumulative proof of work |
| Block size limit | **1 MB** (1,000,000 bytes) |
| **Throughput (max TPS)** | **~80 tx/s sustained** — 1 MB of ~209-byte P2PKH transactions per 60 s block |
| Timestamp rules | median-time-past of 11 blocks; max 2 h ahead of local clock |
| Reorg protection | difficulty + published checkpoints |
| Networks | mainnet, testnet, regtest (trivial-PoW local network) |

### Transactions & scripting

- **UTXO model** — coins are unspent outputs; no account balances stored anywhere.
- **P2PKH** — pay to public-key hash (standard addresses, prefix `S`).
- **P2SH** — pay to script hash (prefix `M`), enabling **multisig** and single-key redeem scripts.
- **Replace-by-fee** — raise a transaction's fee while it is still unconfirmed.
- **Deterministic transaction ids** — no malleability.
- **lock_time** — absolute block-height locks.

### Cryptography

| Where | What |
|---|---|
| Signatures | ECDSA on secp256k1, **RFC 6979 deterministic nonces**, canonical low-S |
| Block hashing | SHA-256d, Merkle-root commitment of all transactions |
| Wallet keys | **BIP-0039** recovery phrases → **BIP-0032** HD derivation → **BIP-0044** paths |
| Wallet at rest | AES-256-GCM, key derived from the password with **scrypt** |
| P2P links | **ChaCha20-Poly1305** AEAD after ECDH key exchange (HKDF), authenticated per message |
| Addresses | Base58Check with network prefixes and 32-bit checksums |

### Network

- Pure peer-to-peer gossip: handshake, address exchange, block/tx announcement.
- **Header-first sync** with parallel block download; orphan expiry, ping, and ban for peers that send invalid blocks.
- Encrypted, authenticated wire protocol — every message after the handshake is ChaCha20-Poly1305 sealed.
- Nodes discover each other automatically; `mainnet` bootstraps from
  `scarletcoin.remotewire.net` and needs zero configuration.
- JSON-RPC API with bearer-token auth, plus a read-only public endpoint mode.
- Block explorer with WebSocket live updates and a Prometheus `/metrics` endpoint.
- Pruning: drop old block bodies, keep every header, the UTXO set, and every balance.

### The client applications

| Program | What it does |
|---|---|
| `scarlet-node` | full node: validates everything from genesis, serves RPC + explorer |
| `scarlet-wallet` | BIP-39/32/44 HD wallet; signs locally, keys never leave the machine |
| `scarlet-miner` | CPU miner across multiple cores; pure-Python SHA-256d (~1 MH/s/core) |
| GUI apps | Qt desktop wallet and miner, ready-to-run releases for Windows & Linux |

---

## Install

Needs Python 3.10 or newer. With [uv](https://docs.astral.sh/uv/):

```sh
uv sync                # node, wallet and miner
uv sync --extra gui    # …and the Qt desktop applications
```

or with pip:

```sh
pip install -e ".[gui]"
```

## Quick start

```sh
# 1. join mainnet — no configuration, it finds the network by itself
uv run scarlet-node --network mainnet

# 2. create a wallet (prints your 12-word recovery phrase)
uv run scarlet-wallet --network mainnet create

# 3. mine to your address
uv run scarlet-miner --network mainnet SXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX

# 4. spend coins
uv run scarlet-wallet --network mainnet balance
uv run scarlet-wallet --network mainnet send SYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYY 12.5
uv run scarlet-wallet --network mainnet history
```

Open <http://127.0.0.1:20332> for the explorer. Want a private playground
instead? `regtest` gives you your own local chain with trivial proof of work —
same commands with `--network regtest`, and `scarlet-node rpc --network regtest
generate 5` mines instantly.

### Networks

| | mainnet | testnet | regtest |
|---|---|---|---|
| Address prefix | `S` | `t` | `t` |
| P2SH address prefix | `M` | `T` | `T` |
| P2P / RPC port | 20333 / 20332 | 30333 / 30332 | 40333 / 40332 |
| Target spacing | 60 s | 60 s | 10 s |
| Retarget every | 60 blocks | 60 blocks | 20 blocks |
| Coinbase maturity | 100 blocks | 20 blocks | 2 blocks |
| Easiest target | `0x1e0fffff` | `0x1e0fffff` | `0x207fffff` |

### Using a wallet with somebody else's node

The wallet and miner ask once whether to run a local node, use a public node, or
connect to an address of your choice — then remember the answer. Public nodes
answer balance queries and `sendrawtransaction` for anyone; the choice is
presented with each node's height, peer count and latency:

```sh
uv run scarlet-wallet --network mainnet --node local    info   # a node here
uv run scarlet-wallet --network mainnet --node public   info   # the best public one
uv run scarlet-wallet --network mainnet --node ask      info   # ask me again
```

Running your own node is the trustless option, and it's one command.

### The three programs

```sh
scarlet-node   [--network mainnet|testnet|regtest] [--datadir DIR]
               [--p2p-port N] [--seed HOST] [--addnode HOST:PORT]
               [--rpc-port N] [--rpc-token TOKEN] [--rpc-public] [--prune BLOCKS]
scarlet-node rpc  METHOD [PARAMS...]     # call a running node
scarlet-node info                        # chain status in plain text

scarlet-wallet create [--no-password]    # new wallet, encrypted by default
scarlet-wallet restore [PHRASE]          # rebuild from the recovery phrase
scarlet-wallet info | balance | addresses | unspent | history
scarlet-wallet send ADDRESS AMOUNT|all [--fee-rate N] [--dry-run]
scarlet-wallet export [ADDRESS] | import [WIF]

scarlet-miner ADDRESS [--workers N] [--max-rate HASHES_PER_SEC]
```

### Desktop releases

Ready-to-run builds for Windows and Linux are attached to every
[GitHub release](https://github.com/alessio-ds/ScarletCoin/releases) — wallet,
miner, and node executables, no Python required. Extract and double-click.

## Layout

```
src/scarletcoin/
  crypto/    hashes, Base58Check, secp256k1 keys and signatures, wallet encryption
  core/      consensus: serialisation, transactions, blocks, proof of work,
             the UTXO set, storage, the chain, the mempool, block templates
  net/       wire protocol, peers, address book, the node, JSON-RPC, explorer
  wallet/    key store, coin selection, transaction building, CLI
  miner/     the nonce search loop and the solo miner
  gui/       optional PyQt5 wallet and miner
tests/       437 tests: two-node networking, reorganisations, pruning, TPS load
docs/        protocol and consensus reference, network operator's guide
```

## Development

```sh
uv run pytest              # the whole suite
uv run ruff check src tests
uv run python tools/tps_test.py init --network mainnet   # measure real-world TPS
```

## Documentation

* [docs/PROTOCOL.md](docs/PROTOCOL.md) — consensus rules, serialisation formats,
  the peer-to-peer messages and the RPC methods.
* [docs/RUNNING-A-NETWORK.md](docs/RUNNING-A-NETWORK.md) — run a public node and
  let others discover it.
* [docs/CHANGES-V2.md](docs/CHANGES-V2.md) / [docs/CHANGES-V2.2.md](docs/CHANGES-V2.2.md) — release history.

## License

MIT — see [LICENSE](LICENSE).
