# Running a real ScarletCoin network

This is the operator's guide: how to run a node other people can connect to, how
their nodes find yours automatically, and how to prove that everybody is on the
same chain.

* [What "the same network" means](#what-the-same-network-means)
* [Which ports do what](#which-ports-do-what)
* [Are you launching a network or joining one?](#are-you-launching-a-network-or-joining-one)
* [The ScarletCoin mainnet seed](#the-scarletcoin-mainnet-seed)
* [Worked example: Alpine Linux, Caddy and a DDNS name](#worked-example-alpine-linux-caddy-and-a-ddns-name)
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

## Which ports do what

A public node listens on two ports that have nothing to do with each other, and
mixing them up is the most common way to end up with a node nobody can reach.

| Port | Protocol | Who connects | Exposure |
|---|---|---|---|
| 20333 (mainnet), 30333 (testnet) | **raw TCP**, the peer-to-peer protocol | other nodes | must be reachable from the internet, **directly** |
| 20332 (mainnet), 30332 (testnet) | HTTP: JSON-RPC **and** the explorer | you, and explorer visitors | bind to localhost; publish only through a reverse proxy |

The peer-to-peer port carries a binary framed protocol, not HTTP. An HTTP
reverse proxy (Caddy, nginx, Cloudflare's orange cloud) **cannot** carry it. Peers
open a plain TCP connection to port 20333 and speak `version`/`verack`; anything
that terminates HTTP will simply close it.

So: forward or open TCP 20333 at the firewall, and use your web server only for
the explorer on 20332.

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

## The ScarletCoin mainnet seed

`mainnet` ships with one published seed:

```python
    seeds=("scarletcoin.remotewire.net", "45.126.126.139"),
    public_nodes=("https://scarletcoin.remotewire.net",),
```

`seeds` are for nodes (raw TCP, port 20333) and `public_nodes` for wallets and
miners (HTTPS, the RPC port behind a proxy). The two lists are separate because the
two protocols are: an HTTP proxy can serve the second and cannot carry the first.

The name is what makes the network movable — repoint the record and every node
follows. The literal address next to it is a fallback for when DNS is broken,
filtered on someone's network, or answered by a proxy that cannot carry the
peer-to-peer protocol. Both are marked as seeds in the address book, which means
they are never pruned, so a node can always find its way back after a long time
offline.

A node only needs a seed **once**. After the first successful start it has a
gossiped address book in `<datadir>/<network>/peers.json` and no longer depends on
the seed being up.

## Worked example: Alpine Linux, Caddy and a DDNS name

This is the exact recipe for the reference node: an Alpine server at
`45.126.126.139`, reachable as `scarletcoin.remotewire.net`, with Caddy already
installed and serving the explorer over HTTPS.

The finished layout:

```
                        the internet
                             │
        ┌────────────────────┴─────────────────────┐
        │ TCP 20333                     TCP 443/80 │
        │ (raw peer-to-peer,            (HTTPS)    │
        │  no proxy possible)                      │
        ▼                                          ▼
  ┌───────────────┐                          ┌───────────┐
  │  scarlet-node │◄── 127.0.0.1:20332 ──────│   Caddy   │
  │   (OpenRC)    │      HTTP, explorer      │  (OpenRC) │
  └───────┬───────┘                          └───────────┘
          │
   /var/lib/scarletcoin/mainnet/{chain.sqlite3,peers.json,rpc.token}
```

### 1. Point the name at the server, with no proxy in between

`scarletcoin.remotewire.net` must be a plain `A` record for `45.126.126.139`. If
your DNS provider offers HTTP proxying (Cloudflare's orange cloud, for example),
**turn it off** for this record: a proxy would break port 20333 and answer with
its own address instead of yours.

Check from somewhere that is not the server:

```sh
dig +short scarletcoin.remotewire.net      # must print 45.126.126.139
```

At the time of writing this name answered `138.199.60.12`, so the record still
needs to be updated (or is going through a proxy). Until `dig` prints your
address, seeding by name will not work — the literal address in `seeds` is what
keeps the network reachable in the meantime.

If the address is dynamic, add whatever update client your provider wants, on a
timer:

```sh
apk add curl
printf '*/5 * * * * curl -fsS "https://your-provider/update?host=scarletcoin&token=…" >/dev/null\n' \
  >> /etc/crontabs/root
rc-service crond restart
```

If you would rather keep `scarletcoin.remotewire.net` behind a proxy for the web
side, publish a second, unproxied name for the peer-to-peer side (say
`seed.remotewire.net`), and put *that* one in `ChainParams.seeds`.

### 2. Install on Alpine

Alpine uses musl, so wheels matter. `cryptography` publishes musl wheels for
x86-64, which is all this project needs to compile nothing:

```sh
apk add --no-cache python3 git uv ca-certificates
# no uv package on your Alpine release? then:
#   apk add --no-cache python3 py3-pip git ca-certificates
#   and replace the uv commands below with pip in a venv

adduser -D -H -h /var/lib/scarletcoin -s /sbin/nologin scarlet
install -d -o scarlet -g scarlet -m 0750 /var/lib/scarletcoin /var/log/scarletcoin

git clone https://github.com/alessio-ds/ScarletCoin /opt/scarletcoin
cd /opt/scarletcoin

# Build the virtual environment against Alpine's own Python. Without this, uv
# may download a Python of its own into /root/.local/share/uv/, which is mode
# 700 -- the service user could not execute it, and the node would fail to start
# with "failed to exec ...: Permission denied".
UV_PYTHON_DOWNLOADS=never uv sync --python /usr/bin/python3

# The service runs as an unprivileged user, so everything it executes must be
# readable and traversable by it.
chmod 755 /opt /opt/scarletcoin
chmod -R a+rX /opt/scarletcoin

# Check as the service user, not as root: this is the exact call OpenRC makes.
su -s /bin/sh scarlet -c '/opt/scarletcoin/.venv/bin/scarlet-node --version'
```

That last command must print `scarletcoin 2.0.0`. If it does not, fix it now —
the service will fail in exactly the same way, and `rc-service ... start` reports
`[ ok ]` regardless, because it only means "the supervisor was launched".

If your architecture has no musl wheel for `cryptography`, use Alpine's own build
instead of compiling Rust:

```sh
apk add --no-cache py3-cryptography
python3 -m venv --system-site-packages /opt/scarletcoin/.venv
/opt/scarletcoin/.venv/bin/pip install --no-deps -e /opt/scarletcoin
python3 -c "import cryptography; print(cryptography.__version__)"   # want >= 41
```

Keep the clock right — a node rejects blocks more than two hours ahead of its own
clock:

```sh
apk add --no-cache chrony
rc-update add chronyd default && rc-service chronyd start
```

### 3. Open the peer-to-peer port

```sh
apk add --no-cache iptables ip6tables
iptables -A INPUT -p tcp --dport 20333 -j ACCEPT   # peers
iptables -A INPUT -p tcp --dport 80    -j ACCEPT   # Caddy: ACME challenge
iptables -A INPUT -p tcp --dport 443   -j ACCEPT   # Caddy: explorer
rc-service iptables save && rc-update add iptables default
```

Note what is *not* here: 20332 stays closed. If your provider has its own
firewall or security group, open the same three ports there too.

### 4. Run the node under OpenRC

Before installing the service, make sure the service user can run the binary —
step 2 ends with exactly that check.

```sh
cat > /etc/init.d/scarlet-node <<'EOF'
#!/sbin/openrc-run

name="scarlet-node"
description="ScarletCoin node"

: ${network:=mainnet}
: ${datadir:=/var/lib/scarletcoin}

command="/opt/scarletcoin/.venv/bin/scarlet-node"
# --no-seeds: this node *is* the seed, so it has nothing to bootstrap from.
# --rpc-advertise: tells wallets and other public nodes where to find this one,
# so `--node public` in a fresh wallet can discover it.
command_args="--network ${network} --datadir ${datadir}
    --p2p-port 20333 --rpc-host 127.0.0.1 --rpc-port 20332 --rpc-public --no-seeds
    --rpc-advertise https://scarletcoin.remotewire.net"
command_user="scarlet:scarlet"

supervisor="supervise-daemon"
respawn_delay=5
respawn_max=0
output_log="/var/log/scarletcoin/node.log"
error_log="/var/log/scarletcoin/node.log"

depend() {
    need net
    after firewall chronyd
}

start_pre() {
    checkpath -d -o scarlet:scarlet -m 0750 "${datadir}" /var/log/scarletcoin
}
EOF
chmod +x /etc/init.d/scarlet-node

rc-update add scarlet-node default
rc-service scarlet-node start
tail -f /var/log/scarletcoin/node.log
```

You are looking for these three lines:

```
starting mainnet node at height 0 (/var/lib/scarletcoin/mainnet)
listening for peers on 20333
RPC and explorer listening on http://127.0.0.1:20332
```

The node also prints how much room its chain takes up as it starts
(`chain: height 41207, 6.71 MB of blocks (9.84 MB on disk)`), and
`scarlet-node size` answers the same question at any time without touching the
running node.

`supervise-daemon` restarts the node if it ever dies, and `respawn_max=0` means it
keeps trying forever. Rotate the log so it cannot fill the disk:

```sh
apk add --no-cache logrotate
cat > /etc/logrotate.d/scarletcoin <<'EOF'
/var/log/scarletcoin/*.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}
EOF
```

### 5. Put the explorer behind Caddy

Caddy proxies **only** the HTTP port, and the control interface is blocked so no
one can even try a token against it:

The node is started with `--rpc-public`, so `POST /rpc` answers the read-only and
broadcast methods for anybody and keeps everything else behind the token. That is
what lets someone else's wallet use your node, so Caddy passes `/rpc` through:

```caddyfile
# /etc/caddy/Caddyfile
scarletcoin.remotewire.net {
	encode zstd gzip

	# Wallets and explorers. The node itself decides what an anonymous caller
	# may do: reads and sendrawtransaction yes, mining and control no.
	reverse_proxy 127.0.0.1:20332

	header {
		Strict-Transport-Security "max-age=31536000"
		X-Content-Type-Options nosniff
		Referrer-Policy no-referrer
		-Server
	}

	log {
		output file /var/log/caddy/scarletcoin.log
	}
}
```

If you would rather keep the node entirely to yourself, drop `--rpc-public` from
the service and block the endpoint at the proxy instead:

```caddyfile
	@rpc path /rpc /rpc/*
	handle @rpc {
		respond "the RPC interface is not public" 404
	}
	handle {
		reverse_proxy 127.0.0.1:20332
	}
```

```sh
# Caddy usually runs as its own user, so the log directory has to exist and be
# writable by it, or start-up fails with "permission denied".
install -d -m 0755 /var/log/caddy
chown -R caddy:caddy /var/log/caddy 2>/dev/null || true

caddy validate --config /etc/caddy/Caddyfile
rc-service caddy restart
rc-update add caddy default
```

Editing an existing `Caddyfile`? Back it up first — `cp -a /etc/caddy /etc/caddy.bak-$(date +%F-%H%M)` — and add the site as a new block rather than rewriting the file.

Caddy gets a certificate automatically, which needs port 80 reachable and the DNS
record already pointing at the server. Every explorer link is root-relative, so
proxying at the root path needs no extra rewriting.

### 6. Give the chain some blocks

A fresh network is only its genesis block until somebody mines. Create the wallet
**on your own machine**, never on the server, and give the server only the
address:

```sh
# on your laptop
uv run scarlet-wallet --network mainnet create
uv run scarlet-wallet --network mainnet addresses
```

```sh
# on the server, mining to that address; no keys involved
cat > /etc/init.d/scarlet-miner <<'EOF'
#!/sbin/openrc-run

name="scarlet-miner"
description="ScarletCoin miner"

: ${address:=S_your_address_here}
: ${workers:=1}
: ${max_rate:=}

command="/opt/scarletcoin/.venv/bin/scarlet-miner"
command_args="${address} --network mainnet --datadir /var/lib/scarletcoin
    --workers ${workers} --quiet ${max_rate:+--max-rate ${max_rate}}"
command_user="scarlet:scarlet"

supervisor="supervise-daemon"
respawn_delay=10
respawn_max=0
output_log="/var/log/scarletcoin/miner.log"
error_log="/var/log/scarletcoin/miner.log"

depend() {
    need net scarlet-node
}
EOF
chmod +x /etc/init.d/scarlet-miner
rc-update add scarlet-miner default
rc-service scarlet-miner start
```

The miner reads the node's token from `/var/lib/scarletcoin/mainnet/rpc.token`,
which is why it runs as the same user. Keep `workers` to one or two on a small
VPS — the point is to keep the chain moving, and difficulty adapts to whatever
hash rate shows up. Even one worker burns a full core; to leave the machine
responsive, cap the rate with `max_rate`, for example `max_rate=500` keeps the
miner under half a per cent of a core. The node also uses a core; on a VPS with
two vCPUs that is the whole machine, so do not mine and expect to run anything
else there.

### 7. Check it from outside

Run all of these from a *different* machine:

```sh
dig +short scarletcoin.remotewire.net              # 45.126.126.139
nc -vz scarletcoin.remotewire.net 20333            # open  (peers)
nc -vz scarletcoin.remotewire.net 20332            # refused/filtered (correct!)
curl -sI https://scarletcoin.remotewire.net | head -1
curl -s  https://scarletcoin.remotewire.net/api/info

# and the real test: a node somewhere else, with no configuration at all
uv run scarlet-node --network mainnet --datadir /tmp/probe
uv run scarlet-node info --network mainnet --datadir /tmp/probe
```

The probe should report `peers 1` or more and the same `genesis` as the server.
That is the whole point: users install the release and run one command.

### 8. Day-to-day operation

```sh
rc-service scarlet-node status
tail -f /var/log/scarletcoin/node.log

# the control interface, from the server itself
cd /opt/scarletcoin
.venv/bin/scarlet-node info --network mainnet --datadir /var/lib/scarletcoin
.venv/bin/scarlet-node rpc  --network mainnet --datadir /var/lib/scarletcoin getpeers

# or from your laptop, over SSH, without exposing anything
ssh -L 20332:127.0.0.1:20332 root@45.126.126.139
```

Upgrades:

```sh
cd /opt/scarletcoin && git pull && uv sync && rc-service scarlet-node restart
```

The chain database survives restarts and upgrades. Back up
`/var/lib/scarletcoin/mainnet/peers.json` if you like, but nothing there is
irreplaceable — wallets are the only thing that cannot be re-downloaded, and
yours is not on this machine.

## Launching a network

### 1. Get a machine that can be reached

You need one host with a port other people can open a TCP connection to:

* a small VPS is the easy option — a public IPv4 address and nothing else needed;
* at home, forward TCP **20333** (mainnet) or **30333** (testnet) from your
  router to the machine, and use a dynamic-DNS name since your address changes.

Open the port in the firewall (see the
[Alpine worked example](#worked-example-alpine-linux-caddy-and-a-ddns-name) for
`iptables`):

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
    # And, for wallets that do not want to run a node at all:
    public_nodes=("https://seed.example.org",),
```

`seeds` are peer-to-peer addresses for nodes; `public_nodes` are HTTPS endpoints
for wallets and miners, and only need to get one client started — after that they
find the rest through `getpublicnodes`.

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
* **Addressing.** The listener is IPv4 (`0.0.0.0`); outbound connections happily
  use IPv6 when a peer advertises one. If you need to *accept* IPv6 connections,
  that is a one-line change to the socket family in `net/node.py`.
* **Upgrades.** Because consensus rules live in the code, treat any change to
  `ChainParams` as a hard fork: everyone must upgrade together, or the network
  splits. Ordinary bug fixes are safe to roll out one node at a time.

## Publishing your explorer

The explorer and the JSON-RPC interface share one HTTP server. `GET` requests
(the explorer, `/api/info`) are public; `POST /rpc` requires the token. So
exposing the port publishes a read-only explorer, and RPC stays shut to anyone
without the token — but only if a token is set, which is the default.

For anything public, put it behind a reverse proxy with TLS. A full Caddy
configuration, including blocking `/rpc`, is in the
[Alpine worked example](#worked-example-alpine-linux-caddy-and-a-ddns-name);
the nginx equivalent is:

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

## Letting other people's wallets use your node

A wallet needs *a* node. Two ways, and it is worth being clear about the
trade-off.

### The proper way: everyone runs a node

```sh
uv run scarlet-node --network mainnet      # joins through the seed, no config
uv run scarlet-wallet-gui --network mainnet
```

The wallet defaults to `http://127.0.0.1:20332` and picks up the local node's
token automatically, so this needs no configuration at all. The wallet then
trusts nobody: balances come from a chain the user validated themselves.

If a user has no node yet, the wallet and the miner offer to start one and show,
before they commit to it, how big the chain already on disk is — with the option to
prune it, to make it public, and to let others mine through it. Nothing is
downloaded behind their back.

### The convenient way: point wallets at a public node

Start the node with `--rpc-public` (the service in step 4 does). Anonymous callers
may then use exactly the calls a wallet and an explorer need:

```
getinfo  getblockcount  getbestblockhash  getdifficulty  getsupply  getchainsize
getnetworkstats  getpublicnodes
getblockhash  getblock  getblockheader  getrawblock
gettransaction  getrawtransaction  getmempool
validateaddress  getbalance  getutxos  getaddresshistory  getrichlist
sendrawtransaction
```

Everything else — `getpeers`, `getaddresses`, `addpeer`, `prune`, `stop`,
`generate` — still requires the bearer token, and an unauthenticated attempt is
refused with JSON-RPC error `-32001`.

`getblocktemplate` and `submitblock` sit in between. They are private by default,
so mining through a public node needs its token; add `--rpc-public-mining` if you
are willing to hand out block templates to strangers. It is a separate flag because
it costs you a template per request, and because a miner and a wallet are asking for
very different things.

### Being findable

A wallet with no configuration at all starts from the addresses compiled into its
release, so a node that is not on that list has to be found some other way.
`getpublicnodes` is that other way:

```sh
scarlet-node --network mainnet --rpc-public \
    --rpc-advertise https://scarletcoin.example.net \
    --public-peer https://scarletcoin.remotewire.net
```

* `--rpc-advertise` is the address *you* are reachable at. Without it your node
  works fine but can never tell anybody where it is, and no other public node can
  pass it on.
* `--public-peer` is another public node you are willing to vouch for; repeat it
  for as many as you like.

A wallet running `--node public` probes everything it knows about at once, then asks
whichever nodes answered for their lists and probes those too. One reciprocal
`--public-peer` between two operators is enough to make both discoverable to every
wallet in the network.

Users point their wallet at it with no token, or simply let it ask:

```sh
uv run scarlet-wallet     --network mainnet --node public info
uv run scarlet-wallet-gui --network mainnet --node https://scarletcoin.remotewire.net
```

The chosen node is remembered in `<datadir>/<network>/node.json`, which the command
line tools and the desktop applications share, so it is typed once. The graphical
wallet also offers **Node ▸ Choose a public node…** (a live list with heights and
latencies) and **Node ▸ Connection…** with a *Test* button, and if no node answers
at start-up it asks which one to use instead of opening a window full of zeroes.

What you are accepting by running this:

* **Privacy.** You see which addresses your users ask about. They are trusting you
  for balances, too — a lying node can hide or invent a payment, which is why
  running their own is strictly better.
* **Abuse.** `sendrawtransaction` is validated exactly like a transaction from any
  peer (signatures, fees, mempool limits), so it cannot be used to inject
  nonsense, but it can be called in a loop. There is no rate limiting in the node:
  put Caddy's `rate_limit` in front of it if that ever matters.
* **Nothing else.** No key material exists on the node, and no public method can
  change the chain, the peer set or the process.

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
| Disk filling up | Check with `scarlet-node size`. Blocks are ~1 kB each on a quiet chain, but plan for growth; `scarlet-node prune --keep 5000` drops old bodies, and `--prune 5000` keeps doing it. A pruned node can no longer help a new one sync, so do not prune the seed |
| `dig` returns an address that is not your server | The record is wrong, or your DNS provider is proxying it. A proxy cannot carry the peer-to-peer protocol: use a plain A record, or publish a second unproxied name for peers |
| Explorer works over HTTPS but no peer ever connects | You proxied the wrong thing. Caddy serves 20332; peers need TCP 20333 open directly |
| Caddy cannot get a certificate | Port 80 must be reachable and the DNS record must already point at the server |
| On Alpine: `cryptography` tries to compile Rust | No musl wheel for your architecture. Use `apk add py3-cryptography` with a `--system-site-packages` venv |
| The miner cannot authenticate | It must run as the user that owns `<datadir>/<network>/rpc.token`, or be given `--rpc-token` |
| A remote wallet gets `401 unauthorised` | The node was not started with `--rpc-public`. Add it, or give that wallet the token |
| A remote wallet gets `-32001 … needs the node's RPC token` | The method is not in the public set on purpose (peers, pruning, control). Use a local node for those |
| A remote miner cannot get work | `getblocktemplate` is private unless the operator passed `--rpc-public-mining`. Give the miner the token, run it beside its own node, or add that flag |
| A wallet cannot find any public node | Its release only knows the addresses compiled into it. Pass `--node <URL>` once, set `SCARLETCOIN_PUBLIC_NODES`, or ask an operator to list yours with `--public-peer` |
| `has been pruned by this node` from `getblock` | That node keeps only recent bodies. Ask one that keeps the whole chain |
| Test nodes on your laptop keep joining the real network | Start them with `--no-seeds`, or they will find the seed and sync (and relay anything they mine) |
| The seed node logs `connected` / `disconnected` once a second, peer numbers climbing | It is dialling its own published address. Fixed in the code (the address is remembered after the first attempt); also give the seed node `--no-seeds`, since it has nothing to bootstrap from. `getinfo.own_addresses` lists the addresses it knows are itself |
| Caddy: `setting up custom log … permission denied` | `/var/log/caddy` is missing or not writable by Caddy's user: `install -d -m 0755 /var/log/caddy && chown -R caddy:caddy /var/log/caddy` |
| `rc-service caddy restart` ends with `ERROR: caddy failed to stop` | The configuration check failed, so nothing was restarted. Fix the error it printed and try again; the old configuration is still what is running |
| `supervise-daemon: failed to exec …/scarlet-node: Permission denied` | The service user cannot execute the launcher **or its interpreter**. Check `readlink -f .venv/bin/python3`: if it points inside `/root/.local/share/uv/`, uv used a Python only root can read — rebuild with `UV_PYTHON_DOWNLOADS=never uv sync --python /usr/bin/python3`. Otherwise it is directory permissions: `chmod 755 /opt /opt/scarletcoin && chmod -R a+rX /opt/scarletcoin`. Confirm with `su -s /bin/sh scarlet -c '/opt/scarletcoin/.venv/bin/scarlet-node --version'` |
| `rc-service scarlet-node start` says `[ ok ]` but nothing runs | OpenRC only reports that the supervisor started. The real error is in `/var/log/scarletcoin/node.log` |
| `failed to exec` and `/opt` is a separate mount | Check `mount | grep /opt` for `noexec`; if so, install somewhere else |

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
