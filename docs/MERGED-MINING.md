# Merged Mining: Mine ScarletCoin with Bitcoin ASICs

ScarletCoin uses **merged mining (AuxPoW)** — Bitcoin SHA-256 ASICs can mine ScarletCoin **at zero additional hashing cost** as a by-product of normal Bitcoin mining.

## No special hardware needed

```
Your existing Antminer / Whatsminer / Avalon
        │
        │ standard Stratum V1
        ▼
  Merged-mining pool
        │
        +─── Bitcoin reward  ───►  Your BTC address
        │
        +─── ScarletCoin reward ─► Your SCT address
```

The ASIC firmware **does not change**. The same nonce that solves a Bitcoin share can also produce a ScarletCoin block — the pool handles all the ScarletCoin-specific work.

## For miners

### 1. Get a ScarletCoin address
```bash
scarletcoin wallet new
```

### 2. Point your ASIC at a merged-mining pool
```
URL: stratum+tcp://<pool-host>:3333
Worker: <your-sct-address>
Password: anything
```

### 3. Earn SCT alongside BTC
- Your hashrate earns both BTC (from the pool's Bitcoin parent chain) and SCT (from ScarletCoin)
- SCT blocks are found when a share hash meets the ScarletCoin target
- SCT payouts follow the pool's schedule after coinbase maturity (100 confirmations)

## For pool operators

See [`docs/POOL-OPERATIONS.md`](POOL-OPERATIONS.md) for the full setup guide.

A minimal pool deployment consists of:

1. **Bitcoin Core** — provides parent-chain block templates
2. **ScarletCoin node** — validates blocks and provides AuxPoW work
3. **Stratum bridge** (`pool/scarlet_pool/server.py`) — connects ASICs to both chains

```bash
# Terminal 1: Bitcoin Core (testnet/regtest for testing)
bitcoind -testnet -rpcuser=pool -rpcpassword=pool

# Terminal 2: ScarletCoin node
scarletcoin node testnet --rpc

# Terminal 3: Stratum bridge
python -m pool.scarlet_pool.server http://127.0.0.1:30332 <pool-address>
```

## Profitability

The expected SCT/day from a given hashrate:

```
SCT/day = hashrate / network_hashrate × 1440 × block_subsidy × (1 - pool_fee)
```

Where:
- `network_hashrate` = ScarletCoin's current difficulty expressed as H/s
- `1440` = blocks per day (60s spacing)
- `block_subsidy` = current subsidy (50 SCT, halving every 210,000 blocks)
- `pool_fee` = pool operator's fee percentage

Example with 1 TH/s at difficulty 1M and 0% pool fee:
```
SCT/day = 1,000,000,000,000 / 1,000,000 × 1,440 × 50 = ~72,000,000,000 scar/day = 720 SCT/day
```

Current network hashrate and difficulty are visible at:
- Explorer: https://scarletcoin.remotewire.net/
- RPC: `getnetworkstats`

## Architecture

```
                    Bitcoin parent chain
                           │
                    Bitcoin Core RPC
                           │
                    ┌──────┴──────┐
                    │  Pool Job   │
                    │  Manager    │
                    └──────┬──────┘
                           │
          ┌────────────────┼────────────────┐
          │                │                │
    ScarletCoin RPC   Stratum Server    Bitcoin RPC
   (createauxblock)   (port 3333)     (getblocktemplate)
          │                │
          │          ASIC miners
          │
    submitauxblock
    (when SCT target met)
```

The pool:
1. Calls `createauxblock` on ScarletCoin node → gets frozen candidate with target
2. Calls `getblocktemplate` on Bitcoin Core → gets parent block template
3. Builds a Bitcoin coinbase containing the ScarletCoin commitment (`fa be 6d 6d || ...`)
4. Computes the Bitcoin Merkle root and builds a Stratum job
5. ASICs hash the Bitcoin header normally
6. When a share's hash ≤ ScarletCoin target → assemble AuxPoW → `submitauxblock`
7. When a share's hash ≤ Bitcoin target → submit Bitcoin block normally

## Consensus

Full details in [`docs/AUXPOW.md`](AUXPOW.md).

Key points:
- ScarletCoin block hash is **always** the SHA-256d of its own 80-byte header
- The AuxPoW proof is a **separate payload** appended after transactions
- ScarletCoin chainwork is based on the **ScarletCoin target**, not Bitcoin difficulty
- Both native PoW and AuxPoW blocks are valid after activation

## FAQ

**Q: Do I need a separate ASIC for ScarletCoin?**  
A: No. Any Bitcoin SHA-256 ASIC works. The same nonces count for both chains.

**Q: Will merged mining slow down my Bitcoin mining?**  
A: No. The ASIC hashes exactly the same 80-byte Bitcoin header. Zero overhead.

**Q: What happens if I find a ScarletCoin block but not a Bitcoin block?**  
A: You earn SCT. The pool submits the AuxPoW proof to ScarletCoin. Your ASIC continues mining.

**Q: What if I find both?**  
A: You earn both BTC and SCT. The pool submits to both chains.

**Q: How are SCT rewards distributed?**  
A: The pool tracks shares, calculates each miner's contribution, and pays SCT to the address you provide when connecting.