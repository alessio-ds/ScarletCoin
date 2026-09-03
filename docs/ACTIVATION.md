# AuxPoW Activation Plan

## Current state

AuxPoW consensus is **implemented but not yet activated** on mainnet or testnet.

| Network | `auxpow_chain_id` | `auxpow_activation_height` | Status |
|---------|-------------------|---------------------------|--------|
| mainnet | 1 | `None` | **Not activated** |
| testnet | 2 | `None` | **Not activated** |
| regtest | 3 | `0` | Active from genesis |

## Activation mechanism

AuxPoW uses a **fixed block height** for activation. This is the simplest mechanism suitable for a small network.

The consensus parameter is:

```python
ChainParams.auxpow_activation_height: int | None
```

- `None` → AuxPoW is never accepted
- `0` → AuxPoW is accepted from genesis (regtest only)
- `N` (positive) → AuxPoW is accepted starting at height `N`

## Pre-activation behavior

At heights **below** `auxpow_activation_height`:
- **AuxPoW blocks are rejected** as invalid
- **Native PoW blocks** are required and validated as before
- The `createauxblock` RPC is available but blocks submitted via `submitauxblock` will be rejected
- Existing native miners continue working unchanged

## Post-activation behavior

At height `auxpow_activation_height` and above:
- **Both native PoW and AuxPoW blocks are valid**
- Native miners can continue mining as before
- Merged-mining pools can submit AuxPoW blocks
- The chain with the most cumulative work wins (AuxPoW does not change chain selection)

## Why both native and AuxPoW?

Keeping both valid after activation:
- Allows a **gradual transition** — miners don't need to switch immediately
- Prevents a situation where a pool outage stops all block production
- Native solo mining remains possible for hobbyists
- Can be changed to AuxPoW-only in a future hard fork if desired

## Activation process

1. **Phase 1: Testnet validation** (current)
   - Deploy AuxPoW on testnet with an activation height
   - Run a reference pool against testnet for at least 2 weeks
   - Verify: block acceptance, reorg handling, difficulty retargeting under merged-mining hashrate

2. **Phase 2: Mainnet activation**
   - Announce activation height at least 2 weeks in advance
   - Set `auxpow_activation_height` to a block height ~10,000 blocks (~1 week) in the future
   - Release a new node version with the activation height set
   - Node operators upgrade before the activation height

3. **Phase 3: Post-activation monitoring**
   - Monitor the ratio of native vs AuxPoW blocks
   - Watch for difficulty spikes from merged-mining hashrate
   - Ensure reorg depth stays within safe bounds

## Recommended activation heights

For reference (to be finalized):

| Network | Recommended Height | Approximate Date | Rationale |
|---------|-------------------|------------------|-----------|
| testnet | ~20,000 | TBD | Allow at least 2 weeks of pre-activation testing |
| mainnet | ~115,000 | TBD | Current height ~105,000; gives ~1 week for node upgrades |

## Risks

1. **Difficulty spike** — if a large Bitcoin pool enables merged mining, the ScarletCoin difficulty could increase 100x–1000x nearly instantly. The per-block observed-hashrate retargeting algorithm will adjust quickly (within the retarget window), but the first few blocks after a big miner joins will be found very fast.

2. **Miner exit** — if a large pool stops mining ScarletCoin, the hashrate drops. The per-block retargeting handles this without a death spiral because it measures actual hashrate from the trailing time window, not from a fixed block count.

3. **Reorg risk** — merged mining does not increase reorg risk because ScarletCoin chainwork is based on the ScarletCoin target, not Bitcoin difficulty. An attacker must still provide work meeting the ScarletCoin target to reorganise the chain.

## Testing

Before mainnet activation, verify:

- [ ] Testnet AuxPoW blocks accepted for ≥ 10,000 blocks
- [ ] Native PoW blocks still accepted post-activation
- [ ] Reorgs involving AuxPoW blocks handled correctly
- [ ] Difficulty adjusts correctly under 10×, 100×, 1000× hashrate changes
- [ ] `createauxblock` / `submitauxblock` RPC stable under continuous load
- [ ] Explorer correctly displays AuxPoW blocks
- [ ] Prometheus metrics report AuxPoW activity