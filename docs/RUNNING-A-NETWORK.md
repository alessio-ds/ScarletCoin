# Running a real ScarletCoin network

This is the operator's guide: how to run a node other people can connect to, how
their nodes find yours automatically, and how to prove that everybody is on the
same chain.

* [What "the same network" means](#what-the-same-network-means)
* [Are you launching a network or joining one?](#are-you-launching-a-network-or-joining-one)
* [Launching a network](#launching-a-network)
* [Joining a network](#joining-a-network)
* [How nodes find each other](#how-nodes-find-each-other)
* [How nodes stay in sync](#how-nodes-stay-in-sync)
* [Checking that everyone agrees](#checking-that-everyone-agrees)
* [Running a node properly](#running-a-node-properly)
* [Publishing your explorer](#publishing-your-explorer)
* [Troubleshooting](#troubleshooting)
* [Starting your own separate chain](#starting-your-own-separate-chain)

## What "the same network" means

Two nodes are on the same network when they agree on four things, all of which
come from the code they run — none of them are configurable at run time:

| | Where it comes from | What happens if it differs |
|---|---|---|
| Magic bytes (`SCRL`, `SCRT`, `SCRR`) | `ChainParams.magic` | Every message is dropped as "for a different network"; the peers never talk |
| Genesis block | `ChainParams.genesis_*` | The node refuses to open a database from another chain, and no block will ever connect |
| Consensus rules (spacing, retarget, subsidy, maturity, limits) | `ChainParams` | Nodes reject each other's blocks and permanently fork |
| Protocol version | `protocol.PROTOCOL_VERSION` | Reserved for future changes; today all builds are version 1 |

So "keeping everyone in sync" is mostly a *distribution* problem: everyone runs
the same released version, and then the consensus rules do the rest. The only
thing an operator configures is **who to talk to**.

## Are you launching a network or joining one?

`mainnet` and `testnet` ship with an **empty seed list**:

```python
MAINNET = ChainParams(
    ...
    # Add the long-lived host names of your network's public nodes here, e.g.
    # seeds=("seed.example.org", "seed2.example.org:20333").
    seeds=(),
)
```

That is deliberate, not an oversight. ScarletCoin is a hobby chain: there is no
existing network to join, so the addresses of the first public nodes cannot be
baked in by me — they are yours. Whoever launches the network publishes one or
two host names, and from then on new nodes bootstrap from them automatically.

## Launching a network

### 1. Get a machine that can be reached

You need one host with a port other people can open a TCP connection to:

* a small VPS is the easy option — a public IPv4 address and nothing else needed;
* at home, forward TCP **20333** (mainnet) or **30333** (testnet) from your
  router to the machine, and use a dynamic-DNS name since your address changes.

Open the port in the firewall:

```sh
# firewalld (Fedora, RHEL)
sudo firewall-cmd --permanent --add-port=20333/tcp && sudo firewall-cmd --reload
# ufw (Debian, Ubuntu)
sudo ufw allow 20333/tcp
```

Do **not** open the RPC port (20332) to the internet — see
[Publishing your explorer](#publishing-your-explorer).

### 2. Keep the clock correct

Blocks carry timestamps, and a node rejects a block more than two hours ahead of
its own clock, or older than the median of the last eleven blocks. A badly wrong
clock makes a node reject everything, or mine blocks nobody else accepts.

```sh
timedatectl status          # want: "System clock synchronized: yes"
sudo systemctl enable --now systemd-timesyncd   # or chronyd / ntpd
```

### 3. Run the node as a service

```ini
# /etc/systemd/system/scarlet-node.service
[Unit]
Description=ScarletCoin node
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=scarlet
WorkingDirectory=/opt/scarletcoin
ExecStart=/usr/local/bin/uv run --project /opt/scarletcoin scarlet-node \
    --network mainnet \
    --datadir /var/lib/scarletcoin \
    --p2p-port 20333 \
    --rpc-host 127.0.0.1 --rpc-port 20332
Restart=always
RestartSec=5
StateDirectory=scarletcoin

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl enable --now scarlet-node
journalctl -u scarlet-node -f
```

You should see `listening for peers on 20333`. Verify from *outside* the machine
that the port is really reachable:

```sh
nc -vz your-host.example.org 20333
```

### 4. Publish a host name

Point a DNS record at the machine — `seed.example.org` → your IP. A name rather
than a raw address matters, because:

* you can move the machine without anyone reconfiguring anything;
* one name can hold **several** A/AAAA records, and a starting node tries all of
  them, so your seed keeps working while one host is down.

A dynamic-DNS hostname works exactly as well.

### 5. Mine the first blocks

A brand-new network is just the genesis block. Someone has to mine:

```sh
uv run scarlet-wallet --network mainnet create
uv run scarlet-miner --network mainnet S...your-address...
```

The first blocks come at the easiest allowed difficulty (`0x1e0fffff`, about a
million hashes each); after 60 blocks the difficulty starts tracking the real
hash rate towards one block per minute.

### 6. Tell everyone else

Two ways, and you can do both:

**Bake it into the build (recommended).** Add your names to `ChainParams.seeds`
in `src/scarletcoin/core/params.py`, commit, and tag a release:

```python
    seeds=("seed.example.org", "seed2.example.org"),
```

Now anyone who installs that version and runs `scarlet-node --network mainnet`
joins with **no configuration at all**.

**Tell people a name.** Until they upgrade, users pass it themselves:

```sh
scarlet-node --network mainnet --seed seed.example.org
```

Both put the seed in the node's address book. Everything else is learned by
gossip, and the address book is saved to `<datadir>/<network>/peers.json`, so
after the first successful start a node no longer depends on the seed at all.

## Joining a network

If the build has seeds, this is the whole thing:

```sh
uv sync
uv run scarlet-node --network mainnet
```

The node resolves the seeds, connects, asks for more addresses, downloads the
chain, and stays connected. If the build has no seeds, add one:

```sh
uv run scarlet-node --network mainnet --seed seed.example.org
# or a specific machine you were given:
uv run scarlet-node --network mainnet --addnode 203.0.113.7:20333
```

`--seed` expects a name whose records are all candidate peers; `--addnode` is one
specific peer. Neither is exclusive: the node keeps using its saved address book
and whatever it learns by gossip. Both may be repeated.

Watch the progress:

```sh
uv run scarlet-node info --network mainnet
uv run scarlet-node rpc --network mainnet getpeers
```

A node behind NAT that cannot accept inbound connections still works fine — it
just does not get gossiped to others. If you cannot open a port, say so
explicitly so no one waits for you: `--no-listen`.

## How nodes find each other

```
      seed.example.org                      the address book
   (one name, several A records)         <datadir>/<network>/peers.json
              │                                     ▲
              │ 1. resolve at start-up              │ 4. saved every 5 minutes
              ▼                                     │
     ┌──────────────────┐   2. version/verack   ┌───┴──────────────┐
     │  your new node   │◄─────────────────────►│   a public node  │
     └──────────────────┘                       └──────────────────┘
              │  3. getaddr → addr: "here are 250 peers I know"
              ▼
     connects to up to 8 of them, in parallel, forever
```

1. **Seeds.** Each seed name is resolved to every address it points at. The name
   is remembered as well, so it is retried later even if the records change.
2. **Handshake.** `version` / `verack` exchange the protocol version, the user
   agent, the chain height and a random nonce. The nonce is how a node notices it
   has dialled *itself*, or the same peer twice under two different names — the
   duplicate link is dropped so it does not waste a slot.
3. **Gossip.** Right after the handshake a node sends `getaddr`; the answer is up
   to 250 addresses. This is how nodes that only know a seed end up connected
   directly to each other.
4. **Persistence.** The address book is written to disk, so restarts do not need
   the seed. Addresses that keep failing are dropped after ten attempts, and ones
   not seen for a month are pruned (seed names are kept forever).

A node fills up to `--max-outbound` (8) outbound slots and accepts up to
`--max-inbound` (64) inbound ones. If the address book ever empties completely
and no peer is connected, the seeds are resolved again.

## How nodes stay in sync

Nothing here requires trust or coordination: every node validates everything
itself and follows the same rule — **the valid chain with the most cumulative
proof of work wins**.

* **Catching up.** After the handshake, a node that is behind sends `getblocks`
  with a *locator* (its tip, then progressively older ancestors, ending at the
  genesis hash). The peer answers with up to 500 block hashes from its active
  chain; the node fetches them 64 at a time until it is level.
* **Staying level.** New blocks and transactions are announced to every peer with
  `inv`, so a block reaches the whole network within a few hops.
* **Forks.** Two miners can find a block at the same height; nodes keep both.
  When the next block extends one of them, that branch has more work, and
  everyone rolls back the other one automatically — spent coins come back, and
  transactions from the abandoned block return to the mempool.
* **Out-of-order blocks.** A block whose parent is unknown is kept aside as an
  orphan for ten minutes and connected as soon as its parent arrives.
* **Missed announcements.** If a tip does not move for a while (five minutes, or
  ten block intervals, whichever is longer), the node re-asks every peer what
  follows its tip. This is what fixes the awkward case where two nodes sit on
  equal-height branches, since neither looks "ahead" of the other.
* **Bad peers.** An invalid block costs a peer 50 points and a protocol violation
  100; at 100 the peer is disconnected and its address banned for an hour. A
  hostile node cannot corrupt anybody's chain, only waste its own connection.

## Checking that everyone agrees

**Are we on the same chain at all?** Compare the fields that identify the
network. These must match exactly, forever:

```sh
uv run scarlet-node rpc --network mainnet getinfo
```

```json
{ "network": "mainnet", "genesis": "00000ca129aa591d…", "magic": "SCRL",
  "protocol_version": 1, "version": "2.0.0", "height": 1041, … }
```

**Do we have the same history?** Compare a block hash at a height that is old
enough to be settled — the tip can legitimately differ for a few seconds while a
new block propagates, but an older block cannot:

```sh
# on each node
uv run scarlet-node rpc --network mainnet getblockhash 1000
```

A tiny script that checks a whole set of nodes:

```sh
#!/bin/sh
# usage: ./check-sync.sh http://node-a:20332 http://node-b:20332 ...
for url in "$@"; do
  height=$(uv run scarlet-node rpc --network mainnet --rpc-url "$url" getblockcount)
  settled=$((height - 6))
  printf '%-32s height=%-7s genesis=%.16s tip=%.16s settled(%s)=%.16s\n' \
    "$url" "$height" \
    "$(uv run scarlet-node rpc --network mainnet --rpc-url "$url" getinfo | sed -n 's/.*"genesis": "\([^"]*\)".*/\1/p')" \
    "$(uv run scarlet-node rpc --network mainnet --rpc-url "$url" getbestblockhash)" \
    "$settled" \
    "$(uv run scarlet-node rpc --network mainnet --rpc-url "$url" getblockhash $settled)"
done
```

Same `genesis` and same `settled` hash across every node means the network is
healthy. What each result means:

| Symptom | Meaning |
|---|---|
| Same genesis, same settled hash, tips differ by one block | Normal: a block is still propagating |
| Same genesis, settled hashes differ | A real fork. Compare `getinfo.chainwork`; the lower one should reorganise. If it does not, its peers are unreachable or its clock is wrong |
| Different genesis or magic | Different chains. Someone is running a modified build |
| One node's height frozen while others advance | Check `getpeers` on it: probably zero peers |

Also useful: `getpeers` shows each peer's `start_height`, so you can see at a
glance whether your neighbours are ahead of you.

## Running a node properly

* **Keep the port reachable.** A node that cannot accept connections is a
  consumer of the network, not a contributor. Check with `nc -vz host 20333` from
  somewhere else.
* **Keep RPC private.** It binds to `127.0.0.1` by default and requires a bearer
  token, generated at start-up and written to `<datadir>/<network>/rpc.token`.
  Binding it to a public address without a token is refused outright.
* **Back up the wallet, not the chain.** `wallet.json` holds your keys and cannot
  be recovered from anywhere. The chain database can always be downloaded again.
* **Watch the log.** `journalctl -u scarlet-node -f`; `block … accepted at height
  N` lines are the heartbeat of a healthy node.
* **Storage.** Blocks, the UTXO set and the indexes live in one SQLite file in
  WAL mode. Put it on a real disk, not a network share.
* **Upgrades.** Because consensus rules live in the code, treat any change to
  `ChainParams` as a hard fork: everyone must upgrade together, or the network
  splits. Ordinary bug fixes are safe to roll out one node at a time.

## Publishing your explorer

The explorer and the JSON-RPC interface share one HTTP server. `GET` requests
(the explorer, `/api/info`) are public; `POST /rpc` requires the token. So
exposing the port publishes a read-only explorer, and RPC stays shut to anyone
without the token — but only if a token is set, which is the default.

For anything public, put it behind a reverse proxy with TLS:

```nginx
server {
    listen 443 ssl;
    server_name explorer.example.org;
    # ssl_certificate ...;

    location / {
        proxy_pass http://127.0.0.1:20332;
        proxy_set_header Host $host;
    }
    location /rpc {          # never expose the control interface
        return 404;
    }
}
```

For your own use, an SSH tunnel is simpler and safer:

```sh
ssh -L 20332:127.0.0.1:20332 you@your-host   # then open http://127.0.0.1:20332
```

## Troubleshooting

| Problem | Cause and fix |
|---|---|
| `peers 0`, nothing happens | No seed and no `--addnode`, or the seed is unreachable. Check with `nc -vz seed 20333`, then `scarlet-node rpc addpeer <host> <port>` to try one by hand |
| `cannot resolve seed …` | DNS problem, or a typo in the name |
| Nobody connects *to* you | Port not forwarded, firewall closed, or you started with `--no-listen`. Cloud hosts also need the port opened in their own security group |
| `this database belongs to a different network` | The datadir was created by another network or a modified build. Use a different `--datadir` |
| `message is for a different network` in the log | A peer from another chain (or a port scan). Harmless |
| Height stuck, peers connected | Give it the poll interval (up to five minutes) to re-ask; then check the log for rejected blocks, and check the clock |
| `timestamp … is too far in the future` | *Your* clock is behind. Fix NTP |
| Your mined blocks are rejected | Usually a stale template or a wrong-network payout address. The error from `submitblock` says which rule failed |
| Disk filling up | Nothing is pruned. Blocks are ~1 kB each on a quiet chain, but plan for growth |

## Starting your own separate chain

If you want a chain of your own rather than joining one, do not just change the
seeds — change the identity, so the two networks can never confuse each other:

1. edit `MAINNET` in `src/scarletcoin/core/params.py`: a new `magic`, a new
   `genesis_timestamp`, a new `genesis_message`, and your own `address_version`
   and `wif_version` if you want different-looking addresses;
2. re-mine the genesis block and paste the nonces it prints:

   ```sh
   uv run python tools/mine_genesis.py
   ```

3. run the test suite (`uv run pytest`) — it checks that every network's genesis
   is valid and that the three networks stay distinct;
4. delete any old datadir, start your seed node, and hand out its name.

Tune `target_spacing`, `retarget_interval`, `initial_subsidy`, `halving_interval`
and `coinbase_maturity` at the same time if you want different economics; they
are all in one dataclass, and `docs/PROTOCOL.md` explains exactly how each one is
used.
