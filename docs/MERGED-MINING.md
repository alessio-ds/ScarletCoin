# Mining ScarletCoin with Bitcoin ASICs

ScarletCoin speaks **Stratum V1**, the same protocol every Bitcoin ASIC already
uses, so a stock Antminer / Whatsminer / Avalon can mine SCT **with no firmware
change**. The miner hashes 80-byte SHA-256d headers exactly as it always does;
the pool wraps that work in a ScarletCoin **AuxPoW** proof and submits the
blocks.

```
Your existing Antminer / Whatsminer / Avalon
        │
        │ standard Stratum V1
        ▼
  Merged-mining pool  ──── createauxblock / submitauxblock ────►  ScarletCoin node
        │
        └─── SCT block reward ──►  the pool's payout address
```

## What this does and does not do

**It works today:** an ASIC pointed at the pool mines **ScarletCoin**, and SCT
block rewards go to the pool's payout address. This is the useful part, and it
needs no ASIC changes.

**It does not produce real Bitcoin blocks.** ScarletCoin's consensus validates
the parent coinbase as one of *its own* transactions (the commitment lives in
that transaction's `coinbase_data` field — see
[AUXPOW.md](AUXPOW.md)), so the pool synthesises the parent header rather than
taking one from Bitcoin. The ASIC cannot tell the difference, but no BTC is
mined and no BTC reward exists. Genuine BTC + SCT merged mining would require a
Bitcoin-format coinbase parser in consensus; that is a future change, not
something a configuration switch can turn on.

## For miners

```
URL:      stratum+tcp://<pool-host>:3333
Worker:   <your-sct-address>
Password: anything (ignored)
```

The worker name is passed to the node as the payout address, so use a real SCT
address. The pool operator sets the address that actually receives the block
reward; check with them before pointing hardware at a pool.

## For pool operators

The full setup guide is [POOL-OPERATIONS.md](POOL-OPERATIONS.md). A deployment
needs two things:

1. a **ScarletCoin node** with `--rpc-public-mining` (or an RPC token), and
2. the **Stratum bridge**, `python -m pool.scarlet_pool.server`.

```sh
scarletcoin node mainnet --rpc --rpc-public-mining
python -m pool.scarlet_pool.server \
    --scarlet-url http://127.0.0.1:20332 \
    --payout-address S... \
    --chain-id 1 \
    --share-difficulty 1
```

### Share difficulty

`--share-difficulty` is **not** the chain difficulty. It only controls how often
a miner submits a share, i.e. how often the pool is allowed to submit a block.
It is expressed in Bitcoin difficulty-1 units, so `1` means "a share must beat
Bitcoin's difficulty-1 target".

ScarletCoin's own difficulty is currently far below 1, which means every share
that beats the share target *also* beats the ScarletCoin target — each accepted
share becomes a block. Raise `--share-difficulty` to throttle submissions from a
fast ASIC; lower it if a small miner never finds anything. Because the chain
retargets per block, its difficulty climbs toward whatever hashrate is pointed
at it, and once it passes the share difficulty the pool stops throttling and
simply accepts everything that is a block.

### Job flow

1. `createauxblock` on the ScarletCoin node → a frozen candidate whose block
   hash is committed into the parent coinbase.
2. The pool builds the parent coinbase with the AuxPoW commitment in its
   `coinbase_data` field and splits it into `coinbase1` / `coinbase2` around the
   two Stratum extranonces.
3. `mining.notify` hands the ASIC the coinbase halves, the Merkle branch, and an
   easy parent header. `prevhash` and every branch entry are in internal byte
   order, as stock firmware expects.
4. The ASIC hashes headers and submits nonces.
5. When a share beats the ScarletCoin target the pool assembles the AuxPoW proof
   and calls `submitauxblock`.

### Architecture

```
                Pool Job Manager
                       │
        ┌──────────────┼───────────────┐
        │              │               │
  ScarletCoin RPC  Stratum Server   parent chain
  createauxblock   (port 3333)      (simulated today,
  submitauxblock        │            bitcoind later)
        │          ASIC miners
        │
   ScarletCoin node
```

## Economics

Block rewards go to the pool's payout address, so the SCT/day for a given
hashrate is:

```
SCT/day = hashrate / network_hashrate × 1440 × block_subsidy
```

`1440` is the number of 60-second blocks in a day and the subsidy starts at
50 SCT, halving every 210,000 blocks. At the time of writing the network
difficulty is very low, so even a single CPU miner produces most blocks — an
ASIC will dominate it. Treat any profitability estimate with suspicion until the
difficulty has settled at the hashrate actually pointed at the chain.

## FAQ

**Q: Do I need special firmware or a modified ASIC?**
A: No. Stock Stratum V1 firmware works.

**Q: Do I earn BTC as well?**
A: No. The pool mines ScarletCoin only; the parent header is synthetic. See
"What this does and does not do" above.

**Q: Will this slow down my Bitcoin mining?**
A: If you point the ASIC at this pool it is not mining Bitcoin at all. If a pool
later adds a real Bitcoin parent, merged mining costs no extra hashing.

**Q: The miner connects but every share is rejected.**
A: Check the pool's `--chain-id` matches the node's network (1 = mainnet). The
pool refuses to start against the wrong chain rather than silently building
invalid proofs.

**Q: Nothing is found for a long time.**
A: `--share-difficulty` is probably too high for the miner's hashrate. Lower it.

**Q: How are rewards split between miners?**
A: They are not, yet. Every block reward goes to the single pool payout address;
there is no per-miner accounting or payout layer in this version.
