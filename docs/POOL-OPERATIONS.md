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
   │ localhost RPC           │ P2P gossip
   │ (createauxblock,        │
   │  submitauxblock)        │
   ▼                         ▼
                   scarletcoin.remotewire.com
                   (explorer on :80/:443)
```

The bridge runs as a separate process, talks to the ScarletCoin node over
localhost RPC, and exposes a Stratum V1 TCP port for ASIC miners.

## Prerequisites

- **Python 3.10+** and **uv** installed
- **ScarletCoin node** already running with RPC enabled
- **Port 3333** open to the internet (TCP)
- (Optional) **Bitcoin Core** for real BTC+SCT dual mining

## Server Setup

### 1. Clone and sync

```bash
cd /home/scarletcoin
git pull origin main
uv sync
```

### 2. Enable mining RPC on the node

`createauxblock` and `submitauxblock` are `MINING_METHODS` — they require
the RPC token unless you start the node with `--rpc-public-mining`.

**Option A (safer):** Use the node's existing RPC token.
```bash
# The token lives in the node's config or is auto-generated.
# Check the node's startup logs for "RPC token: ..."
cat /home/scarletcoin/.scarletcoin/mainnet/rpc-token
```

**Option B (convenient):** Make mining public.
```bash
scarletcoin node mainnet --rpc --rpc-public-mining
```
This lets anyone call `createauxblock`/`submitauxblock` without a token.
Only do this if the RPC port (20332) is NOT exposed to the internet.

### 3. Test the bridge manually

```bash
uv run python -m pool.scarlet_pool.server \
    --scarlet-url http://127.0.0.1:20332 \
    --scarlet-token YOUR_RPC_TOKEN \
    --payout-address Sxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \
    --chain-id 1 \
    --port 3333
```

Verify it starts without errors:
```
INFO Stratum server listening on 0.0.0.0:3333
```

### 4. Install the systemd unit

```bash
sudo cp packaging/scarletcoin-stratum.service /etc/systemd/system/
sudo nano /etc/systemd/system/scarletcoin-stratum.service
# Edit the ExecStart line with your actual RPC token and payout address

sudo systemctl daemon-reload
sudo systemctl enable --now scarletcoin-stratum
sudo systemctl status scarletcoin-stratum
```

### 5. Open the firewall

```bash
# ufw
sudo ufw allow 3333/tcp

# or firewalld
sudo firewall-cmd --add-port=3333/tcp --permanent
sudo firewall-cmd --reload
```

### 6. Verify it's working

From any machine with netcat or a Stratum client:

```bash
# Test TCP connectivity
nc -v scarletcoin.remotewire.com 3333

# Test Stratum subscribe (type this, press Enter)
{"id": 1, "method": "mining.subscribe", "params": ["cpuminer/test"]}
# Should respond with subscription details + set_difficulty notification
```

## Miner instructions

Give miners this info:

```
URL:    stratum+tcp://scarletcoin.remotewire.com:3333
Worker: Sxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx  (your SCT payout address)
Pass:   x  (ignored)
```

Any Bitcoin ASIC (Antminer, Whatsminer, Avalon) or CPU miner that speaks
Stratum V1 can connect.

## Bitcoin dual-mining (BTC + SCT)

The current reference bridge uses `SimulatedParentChain` — it generates fake
solveable parent headers so miners earn SCT only. To add real BTC dual-mining:

1. Run **Bitcoin Core** on the same server:
   ```bash
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

- **Prometheus metrics** at `https://scarletcoin.remotewire.com/metrics`:
  - `scarletcoin_auxpow_blocks_total`
  - `scarletcoin_auxpow_rejections_total`
  - `scarletcoin_auxpow_submissions_total`
  - `scarletcoin_auxpow_templates_created_total`

- **Bridge logs:** `sudo journalctl -u scarletcoin-stratum -f`

- **Explorer:** AuxPoW blocks show "Proof: AuxPoW (merged-mined)" with full
  parent Bitcoin header details.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Bridge exits immediately | Wrong RPC token | Check `--scarlet-token` matches node |
| "AuxPoW is not configured" | Wrong chain-id | Use `--chain-id 1` for mainnet |
| Miners connect but get no jobs | RPC connection lost | Check `--scarlet-url` is reachable |
| SCT blocks not accepted | Stale candidate | Bridge refreshes every 30s; check logs |
| "no AuxPoW candidate with that hash" | Tip advanced | Normal under high hashpower; bridge retries |

## Security notes

- **Never expose the RPC port (20332) to the internet** without a token
- The bridge's `--scarlet-token` should be a dedicated token, not your wallet passphrase
- Run the bridge as a non-root user (`scarletcoin`)
- The systemd unit uses `ProtectSystem=strict` and `NoNewPrivileges=yes`