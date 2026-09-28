# ScarletCoin 2.7.0

This release makes the built-in block explorer answer the question a visitor
actually arrives with — *is this chain carrying any traffic?* — and fixes a
hashrate chart that had quietly stopped being readable.

## The explorer reports transactions per second

The Network card on the overview used to show the node's peer count.  That is
a property of one node's dialling luck, not of the chain, and on a small
network it is nearly always the same number.  It now shows the chain's
**transactions per second**, measured over the same window of recent blocks as
the block pace and the hashrate, with the sample underneath ("`N` tx in `M`
blocks") so a single quiet block cannot read as chain-wide load.  The peer
count did not disappear; it still has its own page.

The measurement lives in `Blockchain.network_stats()` as two new fields,
`transactions` and `transactions_per_second`, so `getnetworkstats` over RPC
reports it too.  Counting is one query (`Storage.count_transactions`) rather
than loading every block in the window.

## The hashrate chart has a logarithmic axis

The hashrate history chart scaled its y-axis linearly to the peak sample.  A
real hashrate series spans orders of magnitude — native blocks near 10^5 H/s,
merged-mined blocks near 10^6, and a pre-fork difficulty jump can read as 10^8
— so one large sample flattened everything else into the baseline.  Clipping
to a percentile would have hidden real recent blocks instead.  The axis is now
logarithmic, labelled with powers of ten, so every sample stays at its true
value and the whole range is readable at once.

## Upgrading

No consensus change, no chain change, no wallet-file change.  The new RPC
fields are additive, and a client that does not know them ignores them.

# ScarletCoin 2.7.1

Three fixes found by running a transaction generator against the live mainnet
node.

## A rate-capped miner now actually idles

`Miner._mine_template` worked out how long it had to idle to honour
`--max-rate`, then passed `min(idle, 1.0)` to `Event.wait`.  A miner asked for
500 H/s hashed for about a second and rested for a second — hundreds of times
over its cap — and held roughly half of the machine's only core.  It also
idled *before* submitting a block it had just found, so a solution could wait
out the cap and go stale.  The idle is now the full amount (an `Event.wait`,
so Ctrl-C stays immediate) and a solution is submitted first.

## Unspent-output lists can be bounded

`getutxos` and `getutxosmulti` gained `limit` and `offset`.  A client that only
needs a working pool no longer has to pull an address's entire unspent set:
with tens of thousands of coins that set is megabytes of JSON and seconds of
node CPU per call.  `RpcClient` exposes the same arguments and leaves them off
when no limit is requested, so an older node is called exactly as before.

## The TPS count has an index

`tx_location(height)` is now indexed, so the overview's transactions-per-second
card no longer scans every indexed transaction.

## Upgrading

No consensus change, no chain change, no wallet-file change.  Existing
databases pick the new index up on the next start; building it is a one-off
cost, not a migration.

# ScarletCoin 2.7.2

A follow-up to 2.7.1's miner fix, found by working through what the idle
actually does on a solo-mined chain.

A rate cap has to bound the *round*, not only the idle after it.  A solution
found inside a round is submitted at once (deliberately — 2.7.1 made that so a
block cannot go stale), which means that round never idles.  With rounds tuned
to about a second of full-speed hashing, every round on an easy chain found a
solution, so the idle never ran and the cap did nothing again, just in a
different way.  `Miner._tune_chunk` now clamps a capped miner's round to at
most `max_rate × ROUND_SECONDS` hashes (never below the existing minimum), so
the idle is paid on every round that does not find a block and the average
rate settles at the cap.  On a machine that hashes far faster than the cap,
that also drops the mining CPU from a full core to a fraction of one.

## Upgrading

No consensus change, no chain change, no wallet-file change.
# ScarletCoin 2.7.3

## The block cache no longer keeps the whole recent chain resident

`Storage.get_block` caches deserialised blocks in memory.  The budget was
128 MB, but it counted the *serialised* size while holding Python `Block`
objects, which measure roughly 3-4x their serialised bytes on a chain full of
small transactions.  A node whose explorer had walked the recent chain
therefore sat at 400 MB or more.  Measured on the live mainnet node: RSS fell
from 285 MB to 30 MB on restart, and grew about 0.34 MB for every recent block
cached.

The budget is now 16 MB serialised (about 55 MB resident) and the entry cap
256.  A block that falls out of the cache is re-read from SQLite in about a
millisecond, which is a good trade on the small hosts this node runs on.

## Upgrading

No consensus change, no chain change, no wallet-file change.  The smaller
cache is purely in memory; nothing on disk changes.

# ScarletCoin 2.7.4

## A sweep signs the same key once, not once per coin

`builder._sign_inputs` derived the public key and the script code for every
input, even though a sweep almost always spends hundreds of coins paying the
same address.  Deriving a public key is an EC point multiplication, so a
1000-input sweep paid for one per input; caching both by payload roughly
halves the time a large sweep takes (4.4 s to 2.6 s for 1000 inputs here).

This matters most when consolidating a wallet that has accumulated thousands
of small coins, which is exactly what the fake-traffic generator does.

## Upgrading

No consensus change, no chain change, no wallet-file change.

# ScarletCoin 2.7.5

The chain database was about three times the size of the chain itself.  None
of that extra was consensus data: it was derived indexes and undo records,
kept for every block since genesis.  Measured on the live mainnet node
(283 MB database, 95 MB of active chain): `blocks` 108 MB, `address_history`
40 MB, `undo` 39 MB, `address_history_txid` 34 MB, `tx_location` 25 MB,
`tx_location_height` 13 MB.

## Undo is bounded

Undo exists so a block can be disconnected during a reorganisation.  It was
kept for every block, but a block `UNDO_KEEP_BLOCKS` (now 2,000) behind the tip
can never be reorganised away.  The node now drops the undo record for the
block that falls out of that horizon as each block connects, and the schema-5
migration deletes the backlog.  A reorg deeper than 2,000 blocks can no longer
be rolled back, which is far deeper than this chain has ever seen.

## The transactions-per-second index is gone

`tx_location_height` existed only to count transactions in a height window.
`blocks` now carries a `tx_count` for each block, written when the block is
stored and read through the existing `blocks_chain` index, so the TPS card is
just as cheap without a second copy of every height.

## The address-history delete index is gone

`address_history_txid` cost 34 MB so that disconnecting a block could delete
its history rows by transaction id.  Disconnects and prunes now batch every
transaction in a block into one `DELETE ... IN (...)`, which scans the table
once per block instead of once per transaction, so the index is not needed.

## `vacuum`

SQLite reuses deleted space but never shrinks the file on its own, so a new
(token-only) `vacuum` RPC compacts the database and reports the bytes
reclaimed.  Run it after the schema-5 migration to actually get the disk back.

## Upgrading

No consensus change, no chain change, no wallet-file change.  Existing
databases are upgraded in place on first start: the migration adds
`blocks.tx_count`, drops the two indexes and prunes old undo.  It can take a
few seconds; follow it with `vacuum` to reclaim the freed pages.
