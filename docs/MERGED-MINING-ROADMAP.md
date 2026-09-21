# Merged-mining roadmap: earning real BTC from the same hashing

**Status: not started. Deliberately deferred.** This document records what real
Bitcoin merged mining would take, so the decision can be made later without
re-deriving it. See [Recommendation](#recommendation) for why it is not being
built now.

## What exists today

ScarletCoin blocks are wrapped in a genuine Namecoin-*style* AuxPoW proof, and
merged mining is active on mainnet from height 47,000. What works:

- Any stock SHA-256d ASIC or CPU miner can mine SCT through the Stratum bridge
  with no firmware change. Verified with an unmodified `minerd`.
- The commitment format (`fa be 6d 6d ‖ aux_root ‖ tree_size ‖ nonce`) and the
  deterministic chain-index formula are Namecoin's.
- `createauxblock` / `submitauxblock` are exposed and match modern Namecoin's
  RPC surface.
- Node, explorer, wallet, miner, and bridge all run in production.

What does **not** work: no BTC is produced. The parent chain is
`SimulatedParentChain` (`pool/scarlet_pool/server.py:59`), which synthesises
80-byte headers rather than reading them from a `bitcoind`.

## Where a solo miner's reward goes

There is **no consensus rule** constraining which address a coinbase pays. A
solo miner names its own address on the command line
(`scarlet-miner <address>`, `src/scarletcoin/miner/cli.py:45`) and is paid
directly.

A *pool* is different: the bridge calls `createauxblock --payout-address`
(`pool/scarlet_pool/server.py:483`, `pool/scarlet_pool/jobs.py:221`), so every
block the bridge finds pays the single address in its config. That is a pool
policy, not a chain rule, and it means **connecting to the bridge mines for the
bridge's address, not yours.**

## Recommendation

**Do not build this yet.** Merged mining does not create hashrate by itself; it
only lets a Bitcoin pool that has *already decided to adopt SCT* redirect
existing work. No pool has adopted it, so building it now yields zero new
security while adding consensus code and permanent issuance concentration.

The chain is currently secured by one CPU (~170 kH/s), which a single laptop
out-hashes 60–300×. That is a real weakness — but it costs an attacker nothing
because there is nothing on the chain worth taking. Security is worth paying for
in proportion to what is at stake.

An earlier argument in favour — "the hard fork is free now, expensive later" —
was overweighted. Monero hard-forks every six months; on a chain with a handful
of node operators a hard fork is routine coordination, not a crisis.

Revisit when SCT has users, trading, or anything worth stealing. The one good
reason to build it sooner is if the *engineering itself* is the goal.

## The two wire-format gaps

Becoming byte-compatible with `CAuxPow` is the difference between "a pool adds a
config line" and "every pool writes SCT-specific code". There are two gaps, not
one.

### Gap 1 — parent coinbase encoding

The parent coinbase is parsed and written as a **ScarletCoin** transaction, with
the commitment in `coinbase_data`:

- `src/scarletcoin/core/auxpow.py:435` — `Transaction.deserialize(coinbase_bytes)`
- `src/scarletcoin/core/auxpow.py:398` — `writer.varbytes(self.coinbase_tx.serialize())`
- `src/scarletcoin/core/auxpow.py:529` — commitment read from `coinbase_data`

A real Bitcoin coinbase is a Bitcoin transaction with no `coinbase_data` field;
the commitment belongs in its `scriptSig`. A pool cannot produce our format from
a `bitcoind` template.

### Gap 2 — AuxPoW blob field order

`AuxPoW.serialize` (`src/scarletcoin/core/auxpow.py:370`) writes:

```text
varint  coinbase_merkle_branch_count
[32-byte hash] * count
uint32  coinbase_index
varint  aux_merkle_branch_count
[32-byte hash] * count
uint32  aux_chain_index
varbytes parent coinbase transaction
[80-byte parent header]
```

Namecoin's `CAuxPow` is believed to serialize the **coinbase transaction first**,
then the branch/index pairs, then the parent header. **This has not been verified
against upstream** — confirm against `namecoin-core/src/auxpow.h` before acting
on it. If correct, a Namecoin-compatible pool emits a differently-ordered blob
that this node rejects.

The ScarletCoin block's own `0x01` AuxPoW marker
(`docs/AUXPOW.md:122`) is ours alone, but does not hinder a pool: pools submit
via RPC rather than constructing blocks.

## Plan

Rough sizes assume familiarity with the codebase.

### Phase 1 — Bitcoin parent-coinbase codec (~1 day)

A minimal Bitcoin transaction reader/writer: version, varint counts, 36-byte
outpoints, varint-prefixed `scriptSig`, sequence, `value:u64` +
varint-prefixed `scriptPubKey`, locktime, plus the segwit marker/flag and the
coinbase witness reserved value. Round-trip against real mainnet coinbase hex.

### Phase 2 — Commitment in `scriptSig` (small)

A `parse_auxpow_commitment` variant that scans the coinbase input script.
**Keep the existing `coinbase_data` path** — mainnet blocks 47001, 47003, 47007…
are already stored in the ScarletCoin format and must keep validating.

### Phase 3 — Format disambiguation (moderate)

`AuxPoW.read` knows the coinbase's exact byte length, so it can attempt the
Bitcoin codec and require it to consume every byte, falling back to the
ScarletCoin codec. Reject the (astronomically unlikely) case where both succeed.

Aligning the blob order with `CAuxPow` also happens here, if that is the goal.

### Phase 4 — `BitcoinCoreClient` (~1 day)

The second `ParentChainClient` implementation
(`pool/scarlet_pool/jobs.py:60`), over bitcoind JSON-RPC: `getblocktemplate` →
`ParentTemplate`, `submitblock` for solved parents. Config:
`--parent-url/--parent-user/--parent-password`.

### Phase 5 — Parent coinbase construction in the pool (~2 days)

The largest piece. The pool must build the Bitcoin coinbase itself to embed the
commitment, which means recomputing the parent merkle root from the template's
transaction list — bitcoind's branches are invalid once the coinbase changes.
Also BIP34 height in the scriptSig, and, when the template has witness
transactions, a correct witness commitment
(`hash256(witness_merkle_root ‖ witness_reserved_value)`) and segwit
serialization for the coinbase input.

`pool/scarlet_pool/coinbase.py` already has the extranonce splitting and
`coinbase_merkle_branch(txids)`; this adds a Bitcoin-format builder alongside the
existing one.

### Phase 6 — Verification against real bitcoind (~1–2 days)

`bitcoind -regtest`: the pool mines a merged parent block, `submitblock` it to
bitcoind, confirm **both** BTC and SCT land. Then a real miner over Stratum
against the merged pool. Everything before this is scaffolding.

### Phase 7 — Activation

A hard fork: widening the accepted parent coinbase formats makes previously
invalid blocks valid, so old nodes reject blocks new nodes accept. Free today
(zero peers), routine later with coordination.

## Prerequisite: per-miner accounting

Merged mining is only meaningful to a miner if SCT reaches *them*. The bridge
currently pays one address, so a second participant cannot be paid at all. Share
accounting and per-worker payouts are a prerequisite for adoption and are useful
immediately, independently of any of the above.

## Decisions to make before starting

1. Byte-compatibility with `CAuxPow`, or an SCT-specific format? Compatibility is
   what makes existing pools able to adopt SCT cheaply.
2. Verify Gap 2 against `namecoin-core/src/auxpow.h`.
3. Height-gated activation, or ship-and-activate?
4. How to prevent one adopting pool from capturing all future issuance.