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
reports it too.  Counting is one indexed query (`Storage.count_transactions`)
rather than loading every block in the window.

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