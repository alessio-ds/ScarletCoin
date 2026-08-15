# ScarletCoin

A small but complete proof-of-work cryptocurrency, written in Python: a real
blockchain, peer-to-peer nodes that reach consensus on their own, a wallet with
proper keys and signatures, a miner, and a block explorer served by every node.

This is version 2 — a full rewrite. The original ScarletCoin (2022) was a
central Flask server that kept balances in text files; there was no chain, no
signatures, and the "private key" was a password stored in the clear on the
server. None of that is left. See [What changed from v1](docs/CHANGES-V2.md).

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

## What it does

* **A real chain.** Blocks contain a header (previous hash, Merkle root,
  timestamp, compact difficulty target, nonce) and transactions. The chain with
  the most cumulative proof of work wins; nodes reorganise when a heavier branch
  appears.
* **UTXO transactions.** Coins are unspent outputs, each locked to a public-key
  hash. Spending one means revealing the public key and signing the transaction
  with secp256k1 (ECDSA, canonical low-`s`, deterministic transaction ids).
* **Real money rules.** 50 SCT per block, halving every 210 000 blocks, 21 000 000
  SCT maximum. Difficulty retargets every 60 blocks towards one block per minute.
  Mined coins mature for 100 blocks before they can be spent.
* **A peer-to-peer network.** Nodes hand-shake, gossip addresses, announce blocks
  and transactions, serve initial block download, expire orphans, ping idle
  peers, and ban peers that send invalid blocks. No node is special.
* **A wallet that owns its keys.** Keys live in a JSON file encrypted with
  AES-256-GCM behind an scrypt-derived key. Signing happens locally; the node
  only ever sees finished transactions.
* **A miner.** Asks a node for work, searches the nonce space across CPU cores,
  submits solved blocks, and collects fees along with the subsidy.
* **A block explorer** on the node's HTTP port: blocks, transactions,
  addresses, the mempool, peers and a rich list.

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

On Alpine (or anywhere the code runs as a dedicated service user), build the
environment against the system Python so an unprivileged user can execute it:

```sh
UV_PYTHON_DOWNLOADS=never uv sync --python /usr/bin/python3
```

## Quick start on a private network

`regtest` is a local network whose proof of work is trivial, which makes it
perfect for trying things out. Run each command in its own terminal.

```sh
# 1. a node (prints the explorer URL, writes an RPC token under the data dir)
uv run scarlet-node --network regtest

# 2. a wallet
uv run scarlet-wallet --network regtest create
uv run scarlet-wallet --network regtest addresses

# 3. mine to the address you just created
uv run scarlet-miner --network regtest tXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX

# 4. spend some coins
uv run scarlet-wallet --network regtest balance
uv run scarlet-wallet --network regtest send tYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYY 12.5
uv run scarlet-wallet --network regtest history
```

Open <http://127.0.0.1:40332> for the explorer. On `regtest` the node also
offers a shortcut that mines instantly:

```sh
uv run scarlet-node rpc --network regtest generate 5
```

### Joining the network

`mainnet` ships with a published seed, so joining takes no configuration at all:

```sh
uv run scarlet-node --network mainnet
```

The node resolves `scarletcoin.remotewire.net`, connects, asks its peers for more
addresses, downloads the chain, and saves what it learned to
`<datadir>/mainnet/peers.json` — after the first start it no longer needs the seed.
To add peers of your own:

```sh
uv run scarlet-node --network mainnet --seed seed.example.org       # a name, all its records
uv run scarlet-node --network mainnet --addnode 203.0.113.7:20333   # one specific peer
```

### Using a wallet with somebody else's node

A wallet needs a node. Run one yourself — the wallet defaults to
`http://127.0.0.1:20332` and finds its token automatically — or point it at a node
that was started with `--rpc-public`:

```sh
uv run scarlet-wallet     --network mainnet --rpc-url https://scarletcoin.remotewire.net info
uv run scarlet-wallet-gui --network mainnet --rpc-url https://scarletcoin.remotewire.net
```

The desktop wallet remembers the address, has a **Node ▸ Connection…** dialog with
a *Test* button, and asks for a node if none answers instead of showing an empty
window. A public node answers reads and `sendrawtransaction` for anybody; mining,
peer management and shutdown always need its token. Running your own node is still
better: then you trust nobody for your balance.

### Running a public node

A node needs **two** ports, and they are not interchangeable:

| Port | What | Who reaches it |
|---|---|---|
| 20333 | the peer-to-peer protocol, raw TCP | other nodes, directly — an HTTP proxy cannot carry it |
| 20332 | JSON-RPC and the explorer, HTTP | localhost; publish through a reverse proxy, and add `--rpc-public` if you want other people's wallets to use it |

[docs/RUNNING-A-NETWORK.md](docs/RUNNING-A-NETWORK.md) has the full guide,
including a complete worked example for the reference node (Alpine Linux, OpenRC
services, Caddy with automatic HTTPS in front of the explorer, firewall rules, and
a DDNS name), plus how to prove that a set of nodes really is on one chain.

### Two nodes talking to each other

```sh
uv run scarlet-node --network regtest --datadir /tmp/a --p2p-port 41001 --rpc-port 41002
uv run scarlet-node --network regtest --datadir /tmp/b --p2p-port 41003 --rpc-port 41004 \
    --addnode 127.0.0.1:41001
```

Mine on one and watch the other follow:

```sh
uv run scarlet-node rpc --network regtest --rpc-url http://127.0.0.1:41002 generate 3
uv run scarlet-node info --network regtest --rpc-url http://127.0.0.1:41004
```

### The desktop applications

```sh
uv run scarlet-wallet-gui --network regtest
uv run scarlet-miner-gui  --network regtest
```

If no node is already running on this machine, the wallet and the miner start a
local node themselves (same network and datadir), wait for it to come up, and
stop it again when the window closes. Disable that with `--no-start-node`; to
start a node by hand from inside the window use *Node > Start a local node*.
The miner needs a node it owns for its mining token, so a public node will not
do.

## Desktop release

Ready-to-run builds for Windows and Linux are attached to every
[GitHub release](https://github.com/alessio-ds/ScarletCoin/releases). Each
archive contains three executables and nothing else — no Python is required:

| File | What it is |
|---|---|
| `scarlet-wallet-gui` | the desktop wallet |
| `scarlet-miner-gui` | the desktop miner |
| `scarlet-node` | the node, started in the background by the other two |

Extract the archive anywhere and double-click the wallet or the miner. If no
node is running yet, the first window to open starts a local node in the
background (same network and datadir) and stops it again when the last window
closes.

* **Windows** — `ScarletCoin-<version>-win64.zip`, or the
  `ScarletCoin-Setup-<version>.exe` installer, which installs per user (no
  admin rights) and adds Start-menu shortcuts for both applications.
* **Linux** — `ScarletCoin-<version>-linux-x86_64.tar.gz` (single directory of
  executables; `chmod +x scarlet-*` after extracting). A desktop with the usual
  Qt libraries is assumed; on Debian/Ubuntu `libxcb-cursor0` may need to be
  installed.

The `Release` workflow builds everything with
[PyInstaller](https://pyinstaller.org/) when a tag like `v2.0.0` is pushed, and
uploads the archives (plus the installer on Windows) to the release. To build
locally:

```sh
python tools/build_release.py
# on Windows, compile the installer as well:
iscc /DMyAppVersion=2.0.0 packaging/windows/scarletcoin.iss
```

## The three programs

### `scarlet-node`

Validates and stores the chain, talks to peers, and serves JSON-RPC plus the
explorer.

```sh
scarlet-node [--network mainnet|testnet|regtest] [--datadir DIR]
             [--p2p-port N] [--no-listen]
             [--seed HOST[:PORT]] [--addnode HOST[:PORT]] [--no-seeds]
             [--rpc-host ADDR] [--rpc-port N] [--rpc-token TOKEN] [--no-rpc]
scarlet-node rpc  METHOD [PARAMS...]     # call a running node
scarlet-node info                        # its status, in plain text
```

RPC is protected by a bearer token. Unless you pass `--rpc-token`, the node
generates one and writes it to `<datadir>/<network>/rpc.token`; the wallet, the
miner and `scarlet-node rpc` read it automatically. Binding the RPC port to a
non-loopback address without a token is refused.

### `scarlet-wallet`

```sh
scarlet-wallet create [--no-password]   # new wallet file, encrypted by default
scarlet-wallet info | balance | addresses | unspent | history
scarlet-wallet new [LABEL]              # a fresh receiving address
scarlet-wallet send ADDRESS AMOUNT|all [--fee-rate N] [--dry-run] [--yes]
scarlet-wallet export [ADDRESS] | import [WIF] | label ADDRESS LABEL
scarlet-wallet password [--remove]
```

The wallet file defaults to `<datadir>/<network>/wallet.json`. Amounts are
decimal SCT (`12.5`), never floats internally: 1 SCT is 100 000 000 *scar*.

### `scarlet-miner`

```sh
scarlet-miner ADDRESS [--workers N] [--refresh SECONDS] [--blocks N] [--quiet]
```

Mining runs in worker processes, so it actually uses several cores. Pure-Python
SHA-256 reaches roughly 1 MH/s per core — enough for `regtest` and `testnet`.

## Networks

| | mainnet | testnet | regtest |
|---|---|---|---|
| Address prefix | `S` | `t` | `t` |
| P2P / RPC port | 20333 / 20332 | 30333 / 30332 | 40333 / 40332 |
| Target spacing | 60 s | 60 s | 10 s |
| Retarget every | 60 blocks | 60 blocks | 20 blocks |
| Coinbase maturity | 100 blocks | 20 blocks | 2 blocks |
| Easiest target | `0x1e0fffff` | `0x1e0fffff` | `0x207fffff` |

`mainnet` bootstraps from `scarletcoin.remotewire.net` (with the reference node's
address as a fallback for when DNS is unavailable). `testnet` has no seeds: point
nodes at each other with `--seed` or `--addnode`, or add your own to
`ChainParams.seeds`. See [docs/RUNNING-A-NETWORK.md](docs/RUNNING-A-NETWORK.md).

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
tests/       285 tests, including two-node networking and reorganisations
docs/        protocol and consensus reference, and what changed since v1
```

## Development

```sh
uv run pytest              # the whole suite, about ten seconds
uv run ruff check src tests
uv run ruff format src tests
uv run python tools/mine_genesis.py   # only if the genesis definition changes
```

## Documentation

* [docs/RUNNING-A-NETWORK.md](docs/RUNNING-A-NETWORK.md) — how to run a public
  node, let others connect to it automatically, and verify that everyone is on
  the same chain.
* [docs/PROTOCOL.md](docs/PROTOCOL.md) — consensus rules, serialisation formats,
  the peer-to-peer messages and the RPC methods.
* [docs/CHANGES-V2.md](docs/CHANGES-V2.md) — what the rewrite fixed, and why.

## Honest limitations

It is a hobby chain, and it says so:

* proof of work is pure Python, so the hash rate is tiny; a real network would
  be trivial to out-mine;
* there is no script language — outputs pay a public-key hash and nothing else,
  so no multisig, no time locks beyond a block-height `lock_time`, no contracts;
* no replace-by-fee, no compact block relay, no headers-first sync, no pruning,
  no SPV proofs, and no encryption or authentication on the peer-to-peer link;
* the wallet trusts the node it is configured to talk to;
* no staking. The staking table in the old README described something that was
  never implemented, and paying interest out of thin air is not a thing this
  code will do.

## License

MIT — see [LICENSE](LICENSE).
