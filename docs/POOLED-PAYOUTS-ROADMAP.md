# Pooled payouts roadmap: paying many miners for shared work

**Status: not started.** Fix A ([below](#what-fix-a-already-did)) makes every
miner paid its own address, which is enough while a miner can expect to find
blocks on its own. This document is the plan for the next step: proportional
payouts for a pool where individual miners are too small to find blocks alone.

## What Fix A already did

Each Stratum session now authorises with a payout address in its worker name
(`ADDRESS` or `ADDRESS.rig1`), and the pool builds that miner its **own**
`createauxblock` candidate, so the block it finds pays it directly. Verified on
regtest: two miners on two addresses produced two blocks, each paying its
finder.

That is **solo mining through a bridge**. It is the right shape while:

```text
miner hashrate  x  seconds per block  >  chain difficulty work
```

holds — i.e. a miner finds a block in a reasonable time by itself. On mainnet at
difficulty ~7 that is ~7.3M hashes, so even a modest CPU miner qualifies.

## When Fix A stops being enough

Two situations:

1. **Difficulty rises.** If merged mining ever brings real hashrate, difficulty
   climbs to match the *largest* miner. A small miner then goes hours or days
   without a block and earns nothing, even though it contributed.
2. **Variance.** Even at equal hashrate, solo mining is a lottery. A miner with
   1% of the pool's hashrate should earn 1% of rewards, but solo it earns 0% for
   long stretches and then 100% once.

Both are solved by the same thing: **credit shares, pay proportionally**.

## The model

```text
miner --shares--> pool credits --> block found --> reward split by shares
                                                     |
                                                     v
                                          on-chain payout from pool wallet
```

### 1. Worker identity

Already in place: the worker name carries the payout address. Keep it. Reject a
worker with no address exactly as Fix A does — never silently pay the operator.

### 2. Share accounting

Credit each accepted share weighted by its difficulty, so a share that took ten
times the work counts ten times as much.

- Store in the chain datadir (SQLite), not in memory: a pool restart must not
  erase what miners are owed.
- Key by payout address; the rig label is a display detail.
- Record: address, share difficulty, timestamp, and the block height it was
  credited against.

Suggested tables:

```sql
CREATE TABLE shares (
    id            INTEGER PRIMARY KEY,
    address       TEXT    NOT NULL,
    difficulty    REAL    NOT NULL,
    credited_at   INTEGER NOT NULL,   -- unix seconds
    round_id      INTEGER NOT NULL
);
CREATE INDEX shares_by_round ON shares(round_id);

CREATE TABLE rounds (
    id            INTEGER PRIMARY KEY,
    started_at    INTEGER NOT NULL,
    closed_at     INTEGER,            -- set when a block lands
    block_hash    TEXT,               -- NULL until found
    reward        INTEGER             -- satoshis, once known
);

CREATE TABLE balances (
    address       TEXT PRIMARY KEY,
    owed          INTEGER NOT NULL DEFAULT 0,   -- satoshis
    paid          INTEGER NOT NULL DEFAULT 0,
    last_paid_at  INTEGER
);
```

### 3. Choosing the distribution scheme

Pick one deliberately; they differ in who carries variance.

| Scheme | Miner gets | Pool carries | Notes |
| --- | --- | --- | --- |
| **PPS** (pay-per-share) | a fixed rate per share, immediately | all variance + orphan risk | needs a large reserve; a pool can go bankrupt on a bad streak |
| **PPLNS** (pay-per-last-N-shares) | a share of each block, over the last N shares | little | the usual choice; discourages pool hopping if N is large |
| **Proportional** | a share of each block, since the last block | little | simplest; vulnerable to pool hopping |

**Recommendation: PPLNS.** It is simple, it does not need the pool to hold a
reserve, and it is what most merged-mining pools use. Avoid PPS until the pool
has capital to insure against variance.

### 4. Payouts

- Rewards arrive as **coinbase outputs**, which are immature for
  `coinbase_maturity` blocks (2 on regtest; check mainnet's value in
  `core/params.py`). Paying before maturity risks paying coins that a reorg
  removes. Wait for maturity.
- Batch: one transaction with many outputs, paid when an address's `owed`
  exceeds a threshold (e.g. 1 SCT) or on a schedule.
- Fee: keep the payout transaction's fee below the sum of what is owed, or the
  pool pays to give money away.
- After sending, move `owed` to `paid`, and store the txid for reconciliation.

### 5. Custody — the part to decide first

Fix B requires the pool to **hold a key** for the coinbase address, sign payout
transactions, and therefore custody other people's money. That is a real change
in trust and in operational risk:

- a hot wallet is now on the server;
- a bug is now a loss of funds, not a rejected share;
- the operator becomes responsible for other people's balances.

Alternatives worth considering before accepting custody:

- **Keep Fix A only.** Works today, no custody. Accept that small miners get
  variance.
- **Per-round coinbase splitting.** Give each miner a coinbase paying the
  *previous* round's proportions. Removes custody, but is complex, and a miner
  who joins mid-round may be paid for work it did not do.
- **Run a real pool.** Full custody, but the only model that scales past a
  handful of miners.

## Failure modes to design against

1. **Reorgs.** A block that is later orphaned must have its credits rolled back,
   or the pool pays out coins it never had. Tie credits to block hashes so a
   disconnect can reverse them.
2. **Restarts.** In-memory accounting loses money owed. Persist every accepted
   share before acknowledging it.
3. **Duplicate shares.** Two submissions of the same (job, extranonce2, nonce)
   must count once. Keep a per-round set of seen share ids.
4. **Difficulty drift.** Credit measured difficulty, not a share count, or a
   miner who joined when difficulty was low gets overpaid.
5. **Rounding.** Integer satoshi division leaves dust. Track a remainder per
   address rather than discarding it.
6. **A miner with a wrong address.** Validated at authorize; the pool cannot
   recover coins sent to an unspendable address, so reject, do not guess.

## Testing

- Unit: share→credit arithmetic; PPLNS window selection; batched payout
  construction; reorg rollback; duplicate suppression.
- Integration: two miners with unequal hashrate on regtest, confirm over many
  blocks that each is paid roughly in proportion to shares, and that the sum of
  payouts plus fees never exceeds the block rewards received.
- Property: no sequence of shares and blocks ever allows total credits to exceed
  total rewards.

## Open questions

1. PPLNS window size (in shares or in time?).
2. Payout threshold and schedule — a minimum keeps fees sane but delays small
   miners.
3. Whether the pool takes a fee, and how it is accounted.
4. Who holds the key, and whether it is a hot wallet or signed offline.
5. Whether to expose balances through an API or a web page, so a miner can see
   what it is owed without trusting the operator blindly.