# ScarletCoin Merged Mining

Tools for Bitcoin-compatible SHA-256d merged mining (AuxPoW).

## Overview

ScarletCoin supports Namecoin-style AuxPoW, allowing Bitcoin SHA-256 miners
to simultaneously mine both Bitcoin and ScarletCoin with zero extra hashing.

```
ASIC SHA-256d
      |
      v
Bitcoin mining job (80-byte header)
      |
      +-- hash <= BTC target -> submit BTC block
      |
      +-- hash <= SCT target -> submit SCT AuxPoW proof
```

The ASIC performs the same hashing work it always does. The pool/proxy:
1. Requests ScarletCoin work via `createauxblock`
2. Embeds the ScarletCoin commitment in the Bitcoin coinbase
3. Watches for shares/blocks that meet the ScarletCoin target
4. Assembles and submits AuxPoW proofs via `submitauxblock`

## Files

- `__init__.py` – Reference merged-mining coordinator (standalone demo)
- Bitcoin pool integrations should live in this directory.

## Quick Start (regtest)

Terminal 1 — start a ScarletCoin regtest node:
```
scarletcoin node regtest --rpc
```

Terminal 2 — run the coordinator:
```
python -m tools.merged_mining.coordinator <your-address>
```

This will mine regtest blocks using simulated parent PoW.

## RPC Reference

### `createauxblock <address>`

Creates a frozen AuxPoW candidate and returns commitment information:

```json
{
  "hash": "<scarlet-aux-block-hash>",
  "chainid": 3,
  "target": "<64-char target>",
  "bits": "0x...",
  "height": 123,
  "previousblock": "<hash>",
  "coinbasevalue": 5000000000,
  "tree_size": 1,
  "nonce": 1234567890
}
```

### `submitauxblock <hash> <auxpow_hex>`

Submits a complete AuxPoW proof for a previously-created candidate.

## Pool Integration

A Bitcoin pool integrating ScarletCoin merged mining must:

1. Call `createauxblock` periodically to get fresh ScarletCoin work
2. Include the ScarletCoin commitment in the Bitcoin coinbase (after the
   merged-mining magic marker `fa be 6d 6d`)
3. When a share/block meets the ScarletCoin target (in addition to whatever 
   pool share target is in use), assemble the AuxPoW proof:
   - The parent Bitcoin coinbase transaction
   - The coinbase's Merkle branch in the Bitcoin block
   - The parent Bitcoin block header
4. Call `submitauxblock` with the assembled proof
5. Attribute SCT rewards to miners via internal pool accounting