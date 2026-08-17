# ScarletCoin

A small but complete proof-of-work cryptocurrency, written in Python: a real
blockchain, peer-to-peer nodes that reach consensus on their own, a wallet with
proper keys and signatures, a miner, and a block explorer served by every node.

This is version 2.2 — a consensus-breaking release on top of the version 2
rewrite. It adds pay-to-script-hash (multisig), replace-by-fee, hierarchical
deterministic (BIP-0039/0032) wallets, deterministic RFC 6979 signatures, and an
optional native mining backend. The transaction serialisation changed and the
genesis was re-mined, so the chain restarts. The original ScarletCoin (2022) was
a central Flask server that kept balances in text files; none of that is left.
See [What changed from v1](docs/CHANGES-V2.md).

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
  hash (P2PKH) or a script hash (P2SH). Spending one means revealing the public
  key and signing with secp256k1 (ECDSA, RFC 6979 deterministic nonces,
  canonical low-`s`, deterministic transaction ids), or satisfying a redeem
  script. Multisig is built on P2SH.
* **Real money rules.** 50 SCT per block, halving every 210 000 blocks, 21 000 000
  SCT maximum. Difficulty retargets every 60 blocks towards one block per minute.
  Mined coins mature for 100 blocks before they can be spent.
* **A peer-to-peer network.** Nodes hand-shake, gossip addresses, announce blocks
  and transactions, serve initial block download, expire orphans, ping idle
  peers, and ban peers that send invalid blocks. No node is special.
* **A wallet that owns its keys.** A BIP-0039 recovery phrase derives every key
  (BIP-0032/0044); the seed lives in a JSON file encrypted with AES-256-GCM
  behind an scrypt-derived key. Signing happens locally; the node only ever sees
  finished transactions.
* **Replace-by-fee.** A transaction can signal replaceability so its sender can
  raise the fee while it is still unconfirmed.
* **A miner.** Asks a node for work, searches the nonce space across CPU cores
  (with an optional compiled SHA-256 backend and a pure-Python fallback),
  submits solved blocks, and collects fees along with the subsidy.
* **A block explorer** on the node's HTTP port: blocks, transactions,
  addresses, the mempool, peers and a rich list, live-updating over a WebSocket
  endpoint, plus a Prometheus `/metrics` endpoint.

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

A wallet needs a node, and there are two honest answers to *whose*. The first time
you start the wallet or the miner they ask, rather than guessing:

```
  1) Run a node on this machine
     validates everything itself, needs disk space and time to catch up
     nothing stored here yet, so the whole chain has to be downloaded
  2) Connect to a public node
     ready at once, and you trust somebody else's copy of the chain
  3) Enter a node address yourself
```

The answer is saved in `<datadir>/<network>/node.json` and shared by every tool, so
nobody is asked twice. To skip the question:

```sh
uv run scarlet-wallet --network mainnet --node local    info   # a node here
uv run scarlet-wallet --network mainnet --node public   info   # the best public one
uv run scarlet-wallet --network mainnet --node ask      info   # ask me again
uv run scarlet-wallet --network mainnet --node https://scarletcoin.remotewire.net info
```

`--node public` does not need a hard-coded list to stay correct. It starts from the
addresses built into the release (`https://scarletcoin.remotewire.net`), plus any
you have added, probes them all at once, and then asks whichever answered for the
public nodes *it* knows (`getpublicnodes`). Each candidate is reported with its
height, its peer count and how quickly it replied, so a node that has fallen behind
is visible before you trust it:

```
Public mainnet nodes:
   1)  scarletcoin.remotewire.net  height 41207  ·  8 peers  ·  6.71 MB  ·  38 ms
   2)  node.example.org            height 41190  ·  5 peers  ·  6.70 MB  ·  102 ms
   0)  enter a different address
```

The desktop applications show the same list under **Node ▸ Choose a public node…**,
with a *Refresh* button and an *Add a node…* button. A public node answers reads and
`sendrawtransaction` for anybody; peer management, pruning and shutdown always need
its token, and mining does too unless its operator passed `--rpc-public-mining`.
Running your own node is still better: then you trust nobody for your balance.

### How big is the chain?

Every node reports how much room its chain takes up, so nobody has to guess before
downloading it:

```sh
uv run scarlet-node rpc --network mainnet getchainsize
uv run scarlet-node size --network mainnet    # works with no node running
```

`getinfo` carries the same figures (`chain_bytes`/`chain_size`,
`disk_bytes`/`disk_size`, `average_block_bytes`), the explorer's front page shows
them as the **Chain weight** card, and the wallet's status bar puts the chain size
next to the height. Two numbers, because they answer different questions:
`chain_size` is the serialised active chain — what every node on the network has to
carry — and `disk_size` is what this node's database actually costs, indexes and
UTXO set included.

### Pruning

A node can throw away the bodies of old blocks and keep only their headers:

```sh
uv run scarlet-node --network mainnet --prune 5000   # trim continuously while running
uv run scarlet-node prune --network mainnet --keep 5000   # trim once, then reclaim the space
```

The desktop applications offer it on the screen that appears before a node starts,
next to the size of the chain that is already there.

What is kept: every header, the whole UTXO set, and therefore every balance. A
pruned node validates new blocks exactly as strictly as a full one. What is lost,
irreversibly: showing or serving those old blocks and the transactions in them, and
reorganising past the horizon. Never fewer than 2880 blocks (two days at one block a
minute) are kept whole, whatever you ask for.

### Running a public node

A node needs **two** ports, and they are not interchangeable:

| Port | What | Who reaches it |
|---|---|---|
| 20333 | the peer-to-peer protocol, raw TCP | other nodes, directly — an HTTP proxy cannot carry it |
| 20332 | JSON-RPC and the explorer, HTTP | localhost; publish through a reverse proxy, and add `--rpc-public` if you want other people's wallets to use it |

Tell clients where you are, and who else you know, so your node can be discovered:

```sh
uv run scarlet-node --network mainnet --rpc-public \
    --rpc-advertise https://scarletcoin.example.net \
    --public-peer https://scarletcoin.remotewire.net
```

Add `--rpc-public-mining` if you are also willing to hand out block templates to
strangers; it costs a template per request, which is why it is a separate decision.

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
[PyInstaller](https://pyinstaller.org/) when a tag like `v2.1.3` is pushed, and
uploads the archives (plus the installer on Windows) to the release. To build
locally:

```sh
python tools/build_release.py
# on Windows, compile the installer as well:
iscc /DMyAppVersion=2.1.3 packaging/windows/scarletcoin.iss
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
             [--rpc-public] [--rpc-public-mining]
             [--rpc-advertise URL] [--public-peer URL]
             [--prune BLOCKS]
scarlet-node rpc  METHOD [PARAMS...]     # call a running node
scarlet-node info                        # its status, in plain text
scarlet-node size                        # how much disk the chain uses
scarlet-node prune [--keep BLOCKS]       # drop old block bodies
```

RPC is protected by a bearer token. Unless you pass `--rpc-token`, the node
generates one and writes it to `<datadir>/<network>/rpc.token`; the wallet, the
miner and `scarlet-node rpc` read it automatically. Binding the RPC port to a
non-loopback address without a token is refused.

`scarlet-node size` and `scarlet-node prune` work whether or not a node is running:
`size` opens the database read-only, and `prune` goes through the running node's RPC
when one answers and edits the file in place when none does.

### `scarlet-wallet`

```sh
scarlet-wallet create [--no-password]   # new wallet, encrypted by default
scarlet-wallet restore [PHRASE]         # rebuild a wallet from its recovery phrase
scarlet-wallet info | balance | addresses | unspent | history
scarlet-wallet new [LABEL]              # a fresh receiving address
scarlet-wallet send ADDRESS AMOUNT|all [--fee-rate N] [--dry-run] [--yes]
scarlet-wallet export [ADDRESS] | import [WIF] | label ADDRESS LABEL
scarlet-wallet password [--remove]

# which node to talk to (asked once, then remembered)
scarlet-wallet --node local|public|ask|URL ...
scarlet-wallet --forget-node ...        # choose again
```

`create` prints a 12-word recovery phrase that must be written down; `restore`
recreates the wallet (and its addresses) from it. The wallet file defaults to
`<datadir>/<network>/wallet.json`. Amounts are decimal SCT (`12.5`), never
floats internally: 1 SCT is 100 000 000 *scar*.

### `scarlet-miner`

```sh
scarlet-miner ADDRESS [--workers N] [--refresh SECONDS] [--blocks N] [--quiet]
              [--max-rate HASHES_PER_SEC] [--node local|public|ask|URL]
```

Mining runs in worker processes, so it actually uses several cores. Pure-Python
SHA-256 reaches roughly 1 MH/s per core — enough for `regtest` and `testnet`.

Mining needs a node that will hand out work. Your own always will; a public one
only if its operator passed `--rpc-public-mining`. The miner checks before it starts
and says which it is, rather than looping on a rejected `getblocktemplate`.

## Networks

| | mainnet | testnet | regtest |
|---|---|---|---|
| Address prefix | `S` | `t` | `t` |
| P2SH address prefix | `M` | `T` | `T` |
| P2P / RPC port | 20333 / 20332 | 30333 / 30332 | 40333 / 40332 |
| Target spacing | 60 s | 60 s | 10 s |
| Retarget every | 60 blocks | 60 blocks | 20 blocks |
| Coinbase maturity | 100 blocks | 20 blocks | 2 blocks |
| Easiest target | `0x1e0fffff` | `0x1e0fffff` | `0x207fffff` |

`mainnet` bootstraps from `scarletcoin.remotewire.net` (with the reference node's
address as a fallback for when DNS is unavailable), and wallets that do not want a
node of their own start from `https://scarletcoin.remotewire.net`. `testnet` has no
seeds: point nodes at each other with `--seed` or `--addnode`, or add your own to
`ChainParams.seeds` and `ChainParams.public_nodes`. See
[docs/RUNNING-A-NETWORK.md](docs/RUNNING-A-NETWORK.md).

## Layout

```
src/scarletcoin/
  crypto/    hashes, Base58Check, secp256k1 keys and signatures, wallet encryption
  core/      consensus: serialisation, transactions, blocks, proof of work,
             the UTXO set, storage, the chain, the mempool, block templates
  net/       wire protocol, peers, address book, the node, JSON-RPC, explorer,
             the public-node directory and the local-or-public chooser
  wallet/    key store, coin selection, transaction building, CLI
  miner/     the nonce search loop and the solo miner
  gui/       optional PyQt5 wallet and miner
tests/       437 tests, including two-node networking, reorganisations and pruning
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
* [docs/CHANGES-V2.2.md](docs/CHANGES-V2.2.md) — the version 2.2 upgrade, and what is
  still to do.

## Honest limitations

It is a hobby chain, and it says so:

* proof of work is Python by default, so the hash rate is tiny; a compiled
  backend helps on source installs, but a real network would still be trivial
  to out-mine;
* the script language is deliberately small: P2SH redeem scripts support
  multisig and single-key spending, but there are no contracts and no time
  locks beyond a block-height `lock_time`;
* no compact block relay, no headers-first sync, no SPV proofs, and no
  encryption or authentication on the peer-to-peer link;
* pruning drops old block bodies but there is no way back: a pruned node cannot
  help a new one sync, and cannot show the transactions it forgot;
* the wallet trusts the node it is configured to talk to — a public node most of
  all, which is why running your own is still the honest option;
* no staking. The staking table in the old README described something that was
  never implemented, and paying interest out of thin air is not a thing this
  code will do.

## License

MIT — see [LICENSE](LICENSE).
