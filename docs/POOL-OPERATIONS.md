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

### 2. Find the RPC token

The node auto-generates a token on first start:

```sh
cat /var/lib/scarletcoin/mainnet/rpc.token
```

If that file doesn't exist, look in the node's startup log:

```sh
grep -i token /var/log/scarletcoin/node.log | tail -1
```

### 3. Test the bridge manually

```sh
su -s /bin/sh scarlet -c \
  'cd /opt/scarletcoin && /opt/scarletcoin/.venv/bin/python -m pool.scarlet_pool.server \
    --scarlet-url http://127.0.0.1:20332 \
    --scarlet-token YOUR_TOKEN_HERE \
    --payout-address Sxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \
    --chain-id 1'
```

It should print:
```
Stratum server starting on 0.0.0.0:3333
ScarletCoin node: http://127.0.0.1:20332
Payout address: Sxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
Chain ID: 1
```

Press Ctrl-C once you've confirmed it starts.  If it exits with
`"AuxPoW is not configured"` you used the wrong `--chain-id` (mainnet = 1).

### 4. Install as an OpenRC service

```sh
# Copy the init script
cp /opt/scarletcoin/packaging/scarletcoin-stratum.openrc /etc/init.d/scarletcoin-stratum
chmod +x /etc/init.d/scarletcoin-stratum

# Create the config file with your real values
cat > /etc/conf.d/scarletcoin-stratum <<'EOF'
payout_address="Sxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
scarlet_token="YOUR_TOKEN_HERE"
scarlet_url="http://127.0.0.1:20332"
chain_id="1"
port="3333"
host="0.0.0.0"
EOF

# Enable and start
rc-update add scarletcoin-stratum default
rc-service scarletcoin-stratum start
```

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

## Miner instructions

Give miners this info:

```
URL:    stratum+tcp://scarletcoin.remotewire.net:3333
Worker: Sxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx  (your SCT payout address)
Pass:   x  (ignored)
```

Any Bitcoin ASIC (Antminer, Whatsminer, Avalon) or CPU miner that speaks
Stratum V1 can connect.

## Bitcoin dual-mining (BTC + SCT)

The current reference bridge uses `SimulatedParentChain` — it generates fake
solveable parent headers so miners earn SCT only. To add real BTC dual-mining:

1. Run **Bitcoin Core** on the same server:
   ```sh
   bitcoind -rpcuser=pool -rpcpassword=securepassword
   ```

2. Replace `SimulatedParentChain` with a `BitcoinCoreClient` that wraps
   `bitcoind` RPC (not yet implemented — see `pool/scarlet_pool/jobs.py`
   `ParentChainClient` protocol).

3. The bridge will then:
   - Fetch real Bitcoin block templates
   - Build real Bitcoin coinbases (with SCT commitment)
   - Submit valid Bitcoin blocks back to `bitcoind`
   - Earn real BTC block rewards alongside SCT

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