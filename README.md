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

**Version 2.3.4 · Mainnet is live and mined · MIT licensed**

<div align="center">

[![Download for Windows (64-bit)](https://img.shields.io/badge/Download-Windows_64--bit-0078D6?style=for-the-badge&logo=windows&logoColor=white)](https://github.com/alessio-ds/ScarletCoin/releases/download/v2.3.4/ScarletCoin-2.3.4-win64.zip)
[![Windows installer](https://img.shields.io/badge/Windows_Installer-.exe-0078D6?style=for-the-badge&logo=windows&logoColor=white)](https://github.com/alessio-ds/ScarletCoin/releases/download/v2.3.4/ScarletCoin-Setup-2.3.4.exe)

[![Download for Ubuntu](https://img.shields.io/badge/Download-Ubuntu-E95420?style=for-the-badge&logo=ubuntu&logoColor=white)](https://github.com/alessio-ds/ScarletCoin/releases/download/v2.3.4/ScarletCoin-2.3.4-linux-x86_64.tar.gz)
[![Download for Fedora](https://img.shields.io/badge/Download-Fedora-51A2DA?style=for-the-badge&logo=fedora&logoColor=white)](https://github.com/alessio-ds/ScarletCoin/releases/download/v2.3.4/ScarletCoin-2.3.4-linux-fc44-x86_64.tar.gz)

[![Download for macOS (Apple Silicon)](https://img.shields.io/badge/Download-macOS_Apple_Silicon-000000?style=for-the-badge&logo=apple&logoColor=white)](https://github.com/alessio-ds/ScarletCoin/releases/download/v2.3.4/ScarletCoin-2.3.4-macos-arm64.tar.gz)
[![Download for macOS (Intel)](https://img.shields.io/badge/Download-macOS_Intel-555555?style=for-the-badge&logo=apple&logoColor=white)](https://github.com/alessio-ds/ScarletCoin/releases/download/v2.3.4/ScarletCoin-2.3.4-macos-x86_64.tar.gz)

**No Python. No dependencies. Download, extract, run.**

Or use it straight from your browser, nothing to install:

[![Open the ScarletCoin Web Wallet](https://img.shields.io/badge/Open-Web_Wallet-e33a4e?style=for-the-badge&logo=github&logoColor=white)](https://alessio-ds.github.io/scarletcoin-web-wallet/)

</div>

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
| Difficulty retarget | **every block** from the observed hashrate, capped at **4× harder per block** |
| Chain selection | greatest cumulative proof of work |
| Block size limit | **1 MB** (1,000,000 bytes) |
| **Throughput (max TPS)** | **~80 tx/s sustained** — 1 MB of ~209-byte P2PKH transactions per 60 s block |
| Timestamp rules | median-time-past of 11 blocks; max 2 h ahead of local clock |
| Reorg protection | difficulty + published checkpoints |
| Networks | mainnet, testnet, regtest (trivial-PoW local network) |

### Difficulty adjustment

ScarletCoin retargets its proof-of-work difficulty **every block** (starting in
2.3.0), not once per retargeting period like Bitcoin. Mainnet adopted per-block
retargeting at height **10496**; blocks before that follow the periodic rule, so
a node re-validating the whole chain still accepts the pre-fork history. Each
block's target is computed directly from the hashrate observed over the trailing
time window: the chainwork mined in the last
`target_spacing · retarget_interval` seconds, divided by the time that work took:

```
work     = chainwork(tip) − chainwork(block at window start)
observed = work / elapsed                                    hashes per second
target   = 2^256 / (observed · target_spacing)               one block per target
```

Measuring the hashrate directly — instead of multiplying the previous target by
a time ratio — keeps the difficulty from drifting upward under the normal
variance of block times.

The adjustment is clamped **asymmetrically**, because the two failure modes are
not symmetric:

* **Harder** is capped at `max_adjustment_factor` (4×) per block, so a burst of
  hashrate — or a block with a deliberately low timestamp — cannot spike the
  difficulty and stall the chain.
* **Easier** is uncapped (down to `pow_limit`, difficulty 1), so a hashrate
  collapse eases immediately in a single block rather than crawling down 4× at
  a time.

On top of that, a block that lands more than `max_future_time` (2 h) after its
parent is treated as a stalled chain and resets straight to `pow_limit`. The
time-bounded window empties out a stall by itself: blocks mined before a long
gap are simply older than the window and drop out, so the difficulty measures
the miners that are active *now*, not the ones who left.

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

### Compiled releases (no Python needed)

Use the big buttons at the top of this page. Each archive is a
self-contained build made with PyInstaller — no Python, no dependencies, no
installation. What's inside:

| File | What it is |
|---|---|
| `scarlet-wallet-gui` | the desktop wallet |
| `scarlet-miner-gui` | the desktop miner |
| `scarlet-node` | the node, started in the background by the other two |

Extract the archive anywhere and double-click the wallet or the miner. If no
node is running yet, the first window to open starts a local node in the
background (same network and datadir) and stops it again when the last window
closes. On Linux and macOS, `chmod +x scarlet-*` after extracting. The Windows
installer `.exe` installs per user (no admin rights) and adds Start-menu
shortcuts for both applications.

### From source

100% Python, so it also runs anywhere Python does. Needs Python 3.10 or newer.
With [uv](https://docs.astral.sh/uv/):

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
| Retarget every | 1 block | 1 block | 20 blocks |
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
