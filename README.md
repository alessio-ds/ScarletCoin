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

### Joining or starting a public network

`mainnet` and `testnet` ship with an empty seed list, because this is a hobby
chain: there is no network to join until somebody starts one, and the addresses
of its first public nodes are theirs, not mine.

To **join** a network somebody runs, point your node at one of its published host
names — everything after that is automatic (the seed's records are all tried, the
node asks for more addresses, and the address book is saved for next time):

```sh
uv run scarlet-node --network mainnet --seed seed.example.org
uv run scarlet-node --network mainnet --addnode 203.0.113.7:20333   # one specific peer
```

To **run** a network other people can join: open TCP 20333, publish a DNS name
for your node, and add that name to `ChainParams.seeds` in
`src/scarletcoin/core/params.py` so every install of that version connects with no
configuration at all.

[docs/RUNNING-A-NETWORK.md](docs/RUNNING-A-NETWORK.md) covers the whole thing:
systemd units, firewalls, seeds and gossip, clock requirements, publishing the
explorer safely, and how to prove that a set of nodes really is on one chain.

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

`mainnet` and `testnet` ship with no seed nodes: this is a hobby chain, so a
network exists only if somebody starts one. Use `--seed` or `--addnode` to point
nodes at each other, or bake your seeds into `ChainParams.seeds`. See
[docs/RUNNING-A-NETWORK.md](docs/RUNNING-A-NETWORK.md).

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
