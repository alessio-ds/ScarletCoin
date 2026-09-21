# ScarletCoin 2.6.0

This release is about making a wallet that holds a lot of small coins usable
again.  Sending from such a wallet — and "send everything" in particular — used
to take minutes, could time out against a public node, and could stall the node
itself for the duration.

## The signature digest is linear

`Transaction.signature_hash()` commits to the whole transaction body, and the
body does not depend on which input is being signed.  It was rebuilt and
re-hashed once per input, so signing and verifying a transaction with *n*
inputs cost *O(n²)*.  A sweep of a few thousand 50-SCT coinbase outputs built
half-megabyte transactions and spent most of a minute inside that loop; on the
live mainnet node a single such transaction drove the process to a standstill
for minutes.

`SignatureHasher` serialises the body once and carries the SHA-256 state
forward, so each input hashes only its own index, value and script code.
Digests are byte-for-byte identical to the old implementation — this is not a
consensus change and existing signatures stay valid.

## The mempool stops re-verifying what it already checked

`Mempool._revalidate()` clears the pool and re-adds every transaction after
every block.  Re-adding ran the full ECDSA checks again, so each new block
re-verified every unconfirmed transaction and held the mempool lock while it
did.  The pool now remembers which transaction ids it has verified
(`Mempool._verified`) and a revalidation only confirms that the coins still
exist and are mature — a signature cannot change while the transaction itself
is unchanged.

`Mempool.is_spent()` no longer takes the main lock.  Verifying a large
transaction can hold that lock for seconds, and a spendability hint must not
queue behind it: the RPC reads that back the wallet (`getutxos`,
`getbalances`) used to hang for exactly that long.

## One round trip for a whole wallet

* New `getutxosmulti` RPC method: the unspent outputs of several addresses in
  one call.  `Wallet.coins()` uses it instead of one `getutxos` per address.
* RPC responses are serialised compactly.  Indenting a UTXO list with
  thousands of entries is mostly whitespace on the wallet's hot path.

## Bounded, resilient broadcasts

* `build_sweep_transactions` caps how many inputs one sweep transaction
  carries (`DEFAULT_MAX_SWEEP_INPUTS`, currently 1,000) rather than filling it
  to the relay byte limit.  No single broadcast is expensive enough to outlive
  a reverse proxy's read timeout.
* `RpcClient.broadcast()` recovers from a proxy 502 or a client timeout: the
  node may have accepted the transaction while it was still verifying, so the
  client asks the node whether it knows the transaction before reporting the
  broadcast lost.  A genuine rejection is never retried.
* The desktop wallet's data client uses a generous timeout, separate from the
  short timeout used for liveness probes.

## Upgrading

No consensus change, no chain change, no wallet-file change.  Existing nodes,
wallets and signatures keep working.  A node that serves `getutxosmulti` is
compatible with an older wallet, and a newer wallet falls back to per-address
`getutxos` only if the node does not offer it — this release adds the method on
both sides at once.
