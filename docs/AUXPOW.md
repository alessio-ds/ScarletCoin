# AuxPoW — Bitcoin Merged Mining for ScarletCoin

ScarletCoin supports **Namecoin-style AuxPoW** so that existing Bitcoin SHA-256 ASIC miners can also produce ScarletCoin blocks **with zero additional hashing**.

## How it works

```
          SHA-256d ASIC
               │
               │ normal Bitcoin Stratum job
               ▼
       Bitcoin mining pool
               │
       +-------+-------+
       │               │
       ▼               ▼
  BTC block       SCT AuxPoW proof
  (if hash ≤     (if same hash ≤
   BTC target)    SCT target)
```

The ASIC continues doing exactly what it always does — hashing an 80-byte Bitcoin-style header. The pool embeds a ScarletCoin **commitment** into the Bitcoin coinbase before giving the header to the ASIC. When the ASIC finds a nonce whose hash meets the ScarletCoin target, the pool assembles an **AuxPoW proof** and submits it to a ScarletCoin node.

## Commitment format

The commitment lives in the **parent Bitcoin coinbase's scriptSig**, following the Namecoin convention:

```
fa be 6d 6d          merged-mining magic marker (4 bytes)
⟨aux_merkle_root⟩    ScarletCoin commitment root (32 bytes)
⟨tree_size⟩          auxiliary tree size, uint32 LE (4 bytes)
⟨nonce⟩              commitment nonce, uint32 LE (4 bytes)
```

For a single auxiliary chain (only ScarletCoin) the tree has one leaf, so:
- `tree_size = 1`
- `aux_merkle_branch = []` (empty)
- `aux_chain_index = 0` (always 0 for height-0 tree)

## Consensus validation

A ScarletCoin node validates an AuxPoW block by proving seven things:

1. **Structural** — all branches ≤ 30 levels, all hashes 32 bytes, parent header is 80 bytes
2. **Aux block hash** — the ScarletCoin block hash itself (the 80-byte SHA-256d header)
3. **Aux Merkle root** — the block hash, passed through the auxiliary Merkle branch, reaches `aux_merkle_root`
4. **Commitment present** — exactly one `fa be 6d 6d` marker in the parent coinbase, with correct root
5. **Deterministic index** — the `aux_chain_index` matches `get_expected_index(nonce, chain_id, tree_height)`
6. **Coinbase Merkle proof** — the parent coinbase's hash, passed through the coinbase Merkle branch, reaches the parent block's `merkle_root`
7. **Parent PoW** — `SHA256d(parent_header) ≤ ScarletCoin target`

The key rule: **the parent Bitcoin block hash IS the proof of work for ScarletCoin.** The ScarletCoin header's own `nonce` field is irrelevant for AuxPoW blocks.

## Chain IDs

| Network | `auxpow_chain_id` |
|---------|-------------------|
| mainnet | 1 |
| testnet | 2 |
| regtest | 3 |

## Activation

AuxPoW is activated by a **consensus height** (`auxpow_activation_height`). Before that height, AuxPoW blocks are rejected. After activation, both native PoW and AuxPoW blocks are accepted.

Current mainnet: **not yet activated** (activation height = `None`).  
Current testnet: **not yet activated** (activation height = `None`).  
Current regtest: **activated from genesis** (activation height = 0).

## RPC

### `createauxblock <address>`

Creates a frozen AuxPoW candidate. Returns:

```json
{
  "hash": "<scarlet-block-hash>",
  "chainid": 1,
  "target": "<64-char-target>",
  "bits": "0x...",
  "height": 12345,
  "previousblock": "<hash>",
  "coinbasevalue": 5000000000,
  "coinbasehash": "<hash>",
  "tree_size": 1,
  "nonce": 1234567890
}
```

### `submitauxblock <hash> <auxpow_hex>`

Submits a complete AuxPoW proof. Returns the block submission result with `status: "connected"` on success.

## Block serialization

A ScarletCoin block wire format appends the AuxPoW after transactions:

```
[80-byte header]
[transaction count + transactions]
[marker: 0x00 = native, 0x01 = AuxPoW]
[if marker=0x01: varbytes(AuxPoW)]
```

The **ScarletCoin block hash is always the SHA-256d of the 80-byte header only** — the AuxPoW payload does not change the block hash.

## Deterministic index formula

From the Namecoin reference implementation:

```python
rand = nonce & 0xFFFFFFFF
rand = rand * 1103515245 + 12345
rand += chain_id
rand = rand * 1103515245 + 12345
index = rand % (1 << aux_tree_height)
```

For `aux_tree_height = 0` (single chain), the result is always 0.

## Security

- Maximum Merkle branch depth: 30 levels
- Coinbase data must contain exactly one commitment marker — duplicate markers cause rejection
- Tree size must be a power of two
- Nonce hash must not exceed the ScarletCoin target
- The parent coinbase MUST be a coinbase transaction (null outpoint)
- All consensus rules (UTXO, signatures, subsidy, timestamp, difficulty) still apply

## References

- Namecoin AuxPoW implementation: https://github.com/namecoin/namecoin-core/blob/master/src/auxpow.cpp
- Namecoin merged-mining documentation: https://github.com/vinced/namecoin/blob/master/doc/README_merged-mining.md
- ScarletCoin repository: https://github.com/alessio-ds/ScarletCoin