# ScarletCoin throughput benchmark

This report measures where ScarletCoin actually breaks. It is the result of a
load test run against a real node on the author's machine — every number below
is measured, not extrapolated. The goal was to answer one question: **is there a
hard throughput limit, and if so, where does it come from?**

* [Environment](#environment)
* [Methodology](#methodology)
* [Results](#results)
* [Where the limits come from](#where-the-limits-come-from)
* [Hard limits hit during the test](#hard-limits-hit-during-the-test)
* [Verdict](#verdict)
* [How to reproduce](#how-to-reproduce)
* [Headroom](#headroom)

## Environment

| | |
|---|---|---|
| Machine | single consumer desktop, x86-64, Linux |
| Runtime | CPython 3.14, ScarletCoin v2.2.4 |
| Network under test | `regtest` (instant mining, 10 s target spacing) |
| Node | one full node, `--log-level error` to keep logging off the hot path |
| Client | one wallet process speaking JSON-RPC over localhost |
| Tooling | `tools/tps_test.py` plus a multiprocessing signing probe |

All crypto (secp256k1 signing/verification, SHA-256) is the reference pure
Python/`hashlib` implementation. The node and the client share one machine, so
the numbers are what one machine can do — which is the realistic single-node
deployment for a hobby chain.

## Methodology

1. **Fund** the wallet by mining blocks on `regtest`, then wait out coinbase
   maturity.
2. **Split** the whole balance into N equal UTXOs back to the wallet's own
   address with one transaction, and wait for it to be confirmed.
3. **Sign** one sweep transaction per UTXO (1-in/1-out, `fee_per_kb = 1000`,
    ~180 B each), measuring the signing rate.
4. **Broadcast** them in parallel over JSON-RPC, measuring how many the node
   accepts per second, the acceptance latency, and the resulting mempool size.
5. **Mine** blocks until the mempool drains, recording how many transactions and
   how many bytes each block carried.

Runs were executed at 8 and 16 sender threads, and signing was measured both
with threads and with a multiprocessing pool. One run burst 16 000 transactions;
a larger burst than one split can produce is impossible by construction (see
[Hard limits hit during the test](#hard-limits-hit-during-the-test)).

## Results

| Layer | Measured ceiling | Bottleneck |
|---|---|---|
| Signing, threads (GIL-bound) | **~600 tx/s** | pure-Python ECDSA, single process |
| Signing, multiprocessing (8 workers) | **~2 860 tx/s** | none — 5× faster than threads |
| Node ingestion (`sendrawtransaction`) | **~1 250-1 300 tx/s** | 8 and 16 senders give the same figure → the node is the limit |
| Block fill | **5 551 tx = 999 176 B** | hard 1 000 000 B consensus limit |
| Confirmation into blocks (regtest, instant mining) | ~930 tx/s | includes one RPC round trip per mined block |
| Sustained mainnet throughput (design) | **~92 tx/s** | 5 551 tx per 60 s block |

The 16 000-transaction burst was accepted **16 000/16 000** at ~1 250 tx/s with a
p95 acceptance latency of ~14 ms and the mempool at 2 880 000 B of its 5 000 000
B cap. The burst then drained in 3 blocks (5 551 + 5 551 + 4 901 tx), each block
stopping just under the 1 MB limit.

## Where the limits come from

Every throughput layer converges on the same underlying fact: the hot path is
GIL-bound. Signature verification, transaction validation, and block
connection all run inside one Python process, so ingestion and block
processing plateau at roughly the same value (~1 250 tx/s on this machine)
regardless of how many clients hammer the node.

The sustained chain rate is set by consensus, not by hardware:

* a block is at most **1 000 000 B**;
* the smallest possible transaction (1-in/1-out) is ~180 B;
* so a block carries at most ~5 551 transactions;
* mainnet targets one block every **60 s**, giving **~92 tx/s sustained**;
* regtest targets 10 s spacing, giving ~555 tx/s if blocks arrive on time.

The node's mempool is capped at **5 000 000 B** (~27 700 of those transactions),
which is the shock absorber between ingestion speed and confirmation speed.

## Hard limits hit during the test

Two limits were hit for real, and one proved unreachable:

1. **Relay size.** A second split spending 16 000 inputs would exceed the relay
   limit. The node rejects any transaction larger than half a block
   (500 000 B) with `transaction is too large to be relayed`. This caps one
   split at ~16 700 outputs, and — because re-splitting would have to spend the
   first split — a freshly funded wallet can never manufacture more UTXOs than
   that on a stock node.
2. **Mempool capacity.** Because of the previous point, one split yields at most
   ~2 880 000 B of sweep transactions, which always fits the 5 MB mempool. The
   mempool cap is therefore unreachable from a single wallet and bursts are
   always accepted in full.
3. **Block size** was hit every mining round: the template stopped at 999 176 B,
   i.e. 5 551 transactions, exactly the consensus limit.

## Verdict

The true sustained limit is the **block size**: 1 MB → ~5 551 transactions per
block → **~92 tx/s on mainnet**. That is a design property, not a defect.

The practical ceiling below it is the **node**: ~1 250 tx/s ingestion and block
connection on this machine, because validation is pure-Python and GIL-bound. The
client is only the bottleneck when signing in a single process (~600 tx/s);
with multiprocessing signing (~2 860 tx/s) the wallet stops being the limit and
the node takes over.

## How to reproduce

```sh
uv run python tools/tps_test.py init --network regtest
# fund the wallet, then:
uv run python tools/tps_test.py split --utxos 16000 --network regtest
uv run python tools/tps_test.py run --txs 16000 --workers 16 --network regtest --watch 120
```

On `regtest` the split and the drain auto-mine. To measure the node's
ingestion ceiling separately from signing, feed it pre-signed transactions from
multiple processes (see the signing numbers above). Run the node with
`--log-level warning` or `error`, since it logs one INFO line per accepted
transaction.

## Headroom

The measured ceilings are not fixed; they are what the current implementation
achieves on one machine. Each layer can be lifted independently:

* **Chain**: larger blocks or a shorter target spacing raise the sustained rate
  directly (it is 1 MB / 60 s by design).
* **Node**: native secp256k1, or moving verification out of the GIL
  (multiprocessing), raises the ~1 250 tx/s ingestion ceiling.
* **Client**: multiprocessing signing already removes the wallet as a
  bottleneck; ~2 860 tx/s is a floor for how much load one machine can generate.

*Last run: August 2026 against ScarletCoin v2.2.4. The modest 6 B/tx size
increase (174 B → 180 B, from the v2.2.0 transaction format changes — P2SH,
RBF, HD wallets) costs ~3 tx/s sustained, a 3% regression that is purely a
consensus constant and not a performance defect.*
