# Running a ScarletCoin Merged-Mining Pool

This guide covers deploying the Stratum bridge on `scarletcoin.remotewire.com`
(or any server running a ScarletCoin node) so that Bitcoin ASIC miners can
mine SCT.

## Architecture

```
Internet
   │
   │ :3333 (Stratum)        :20333 (P2P)
   ▼                         ▼
┌──────────────┐    ┌─────────────────┐
│ Stratum      │    │ ScarletCoin     │
│ bridge       │───▶│ node            │
│ (port 3333)  │    │ (port 20332 RPC)│
└──────────────┘    └─────────────────┘
   │                         │
   │ localhost RPC           │ :443 (Caddy)
   │ (createauxblock,        │
   │  submitauxblock)        ▼
   ▼                 scarletcoin.remotewire.net
                     (explorer / public RPC)
```

The bridge runs as a separate OpenRC service, talks to the ScarletCoin node
over localhost RPC, and exposes a Stratum V1 TCP port for ASIC miners.

The node itself is already behind Caddy (HTTPS) for the explorer and
read-only public RPC; the bridge goes **directly to localhost:20332** with the
node's RPC token since `createauxblock`/`submitauxblock` are mining methods.

## Deployment on scarletcoin.remotewire.net (Alpine Linux)

The reference node is an Alpine server at `45.126.126.139`.  The full node
setup is documented in [RUNNING-A-NETWORK.md](RUNNING-A-NETWORK.md); this
section adds the Stratum bridge on top of that existing installation.

### 1. Pull the latest code

```sh
cd /opt/scarletcoin
git pull origin main
chmod -R a+rX /opt/scarletcoin
# The virtualenv is already built against Alpine's Python.
# If new dependencies were added (none were this release), re-run:
#   UV_PYTHON_DOWNLOADS=never uv sync --python /usr/bin/python3
```

### 2. Check the mining RPC is available

`createauxblock` and `submitauxblock` are mining methods. The reference node
already runs with `--rpc-public-mining`, so they are reachable on localhost
without a token. Confirm it:

```sh
curl -s -X POST http://127.0.0.1:20332/rpc \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"createauxblock","params":["<your-sct-address>"]}'
```

A JSON object with `hash`, `target`, `chainid` and `nonce` means you are good.
If you get an authorization error instead, the node needs `--rpc-public-mining`
in `/etc/init.d/scarlet-node`, or the bridge needs `--scarlet-token` (the token
is in `/var/lib/scarletcoin/mainnet/rpc.token`).

### 3. Test the bridge manually

```sh
su -s /bin/sh scarlet -c \
  'cd /opt/scarletcoin && /opt/scarletcoin/.venv/bin/python -m pool.scarlet_pool.server \
    --scarlet-url http://127.0.0.1:20332 \
    --payout-address <your-sct-address> \
    --chain-id 1'
```

You should see the listening line and a first job:

```
Stratum server listening on 0.0.0.0:3333
```

Press Ctrl-C once you have confirmed it starts. If it exits with a
`chain id` error, the node is not on the network you asked for. If it exits
with `AuxPoW is not configured`, the node predates AuxPoW.

### 4. Install as an OpenRC service

```sh
# Copy the init script
cp /opt/scarletcoin/packaging/scarletcoin-stratum.openrc /etc/init.d/scarletcoin-stratum
chmod +x /etc/init.d/scarletcoin-stratum

# Create the config file with your real values
cat > /etc/conf.d/scarletcoin-stratum <<'EOF'
payout_address="<your-sct-address>"
scarlet_url="http://127.0.0.1:20332"
chain_id="1"
port="3333"
host="0.0.0.0"
EOF

# Enable and start
rc-update add scarletcoin-stratum default
rc-service scarletcoin-stratum start
```

Leave `scarlet_token` unset while the node runs `--rpc-public-mining`, and leave
`share_difficulty` unset so the share rate tracks the chain — see
[MERGED-MINING.md](MERGED-MINING.md).

### 5. Open the Stratum port

```sh
iptables -A INPUT -p tcp --dport 3333 -j ACCEPT
rc-service iptables save
```

If your VPS provider has its own firewall / security group, open TCP 3333 there too.

### 6. Verify

```sh
# Check the service is running
rc-service scarletcoin-stratum status
tail -f /var/log/scarletcoin/stratum.log

# From your local machine, test Stratum connectivity
echo '{"id":1,"method":"mining.subscribe","params":["cpuminer/test"]}' \
  | nc -w3 scarletcoin.remotewire.net 3333
```

You should get back a JSON response with subscription details and a
`mining.set_difficulty` notification.

### Prove it end to end

`tools/stratum_probe.py` does what an ASIC does — subscribe, authorize, build
the coinbase, fold the Merkle branch, grind a nonce against the real block
target, submit — and reports whether the chain advanced:

```sh
python tools/stratum_probe.py \
    --host scarletcoin.remotewire.net --port 3333 \
    --payout-address <your-sct-address>
```

It exits 0 only when a block was accepted. Run it from a machine with several
cores: it needs roughly `2^256 / target` hashes, which on this chain is a few
seconds of pure Python, but the tip can move while it grinds, so it retries
across jobs. Each attempt is reported, including the pool's refusal.

To confirm a block really was merged-mined rather than found natively:

```sh
curl -s -X POST http://127.0.0.1:20332/rpc -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"getblock","params":["<block-hash>"]}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["proof_type"])'
```

`auxpow` means the proof of work came from a parent header, and the SCT header
nonce will be 0.

## Miner instructions

Give miners this info:

```
URL:    stratum+tcp://scarletcoin.remotewire.net:3333
Worker: Sxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx  (your SCT payout address)
Pass:   x  (ignored)
```

Any Bitcoin ASIC (Antminer, Whatsminer, Avalon) or CPU miner that speaks
Stratum V1 can connect.

## This bridge mines SCT only

`SimulatedParentChain` generates the parent header the ASIC hashes. That is not
a shortcut — it is what the consensus rules require: ScarletCoin validates the
parent coinbase as one of its **own** transactions, with the commitment in that
transaction's `coinbase_data` field, so a coinbase taken from a real `bitcoind`
would not parse. No BTC is mined and no BTC reward exists.

The ASIC cannot tell the difference, so this is exactly what you want for
mining ScarletCoin with Bitcoin hardware. Supporting a genuine Bitcoin parent
would mean adding a Bitcoin-format coinbase parser to the consensus validation
path — a consensus change, not a configuration option.

## Monitoring

- **Prometheus metrics** at `https://scarletcoin.remotewire.net/metrics`:
  - `scarletcoin_auxpow_blocks_total`
  - `scarletcoin_auxpow_rejections_total`
  - `scarletcoin_auxpow_submissions_total`
  - `scarletcoin_auxpow_templates_created_total`

- **Bridge logs:** `tail -f /var/log/scarletcoin/stratum.log`

- **Explorer:** AuxPoW blocks show "Proof: AuxPoW (merged-mined)" with full
  parent Bitcoin header details.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Bridge exits immediately | Wrong RPC token | Check `/var/lib/scarletcoin/mainnet/rpc.token` |
| "AuxPoW is not configured" | Wrong chain-id | Use `chain_id="1"` for mainnet |
| "payout_address is still placeholder" | Not configured | Edit `/etc/conf.d/scarletcoin-stratum` |
| Miners connect but get no jobs | RPC connection lost | Check `scarlet_url` is reachable from localhost |
| Port 3333 closed | Firewall | `iptables -A INPUT -p tcp --dport 3333 -j ACCEPT` |

## Security notes

- **The RPC port (20332) is NOT exposed to the internet** — only Caddy (443) and localhost can reach it.  This is already the setup on the reference server.
- The bridge talks to the node directly on `127.0.0.1:20332` with the RPC token — it does not go through Caddy.
- `createauxblock` and `submitauxblock` require the token because they are MINING_METHODS, even though the node runs `--rpc-public`.
- The bridge runs as the unprivileged `scarlet` user.
- The OpenRC service uses `supervise-daemon` — it restarts automatically if it ever dies.