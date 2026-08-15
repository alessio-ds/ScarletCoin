# ScarletCoin protocol and consensus reference

Everything here is normative: two implementations that agree on this document
will agree on the same chain. Byte order is little-endian unless stated
otherwise.

* [Units](#units)
* [Hashes](#hashes)
* [Keys and addresses](#keys-and-addresses)
* [Serialisation primitives](#serialisation-primitives)
* [Transactions](#transactions)
* [Blocks](#blocks)
* [Proof of work](#proof-of-work)
* [Money supply](#money-supply)
* [Validation rules](#validation-rules)
* [Chain selection](#chain-selection)
* [Network parameters](#network-parameters)
* [Peer-to-peer protocol](#peer-to-peer-protocol)
* [JSON-RPC interface](#json-rpc-interface)
* [Wallet file format](#wallet-file-format)

## Units

The smallest unit is the **scar**. One SCT is 100 000 000 scar. Amounts are
always integers; no code path uses floating point for money.

## Hashes

| Name | Definition | Used for |
|---|---|---|
| `sha256` | SHA-256 | building block |
| `hash256` | `sha256(sha256(x))` | block hashes, transaction ids, Merkle trees, Base58Check checksums |
| `hash160` | `hash256(x)[:20]` | public-key hashes in outputs and addresses |

`hash160` deliberately does not use RIPEMD-160: it is missing from default
OpenSSL 3 builds, and truncating a double SHA-256 gives the same 160-bit
digest length with one primitive instead of two.

Hashes are handled internally in their raw byte order and displayed
**reversed** (big-endian hex), which is the convention users expect from block
explorers.

## Keys and addresses

* Private key: 32 bytes, interpreted as an integer in `[1, n)` on secp256k1.
* Public key: always the 33-byte compressed SEC1 encoding (`0x02`/`0x03` prefix).
* Signature: 64 bytes, `r || s`, both big-endian. Only canonical signatures are
  valid: `1 ≤ r < n`, `1 ≤ s ≤ n/2`. High-`s` signatures are rejected, which
  removes signature malleability.
* Address: `Base58Check(version || hash160(compressed_public_key))`, where
  `Base58Check(payload) = Base58(payload || hash256(payload)[:4])`.
* Private key export ("WIF"): `Base58Check(wif_version || secret || 0x01)`.
  The trailing byte marks the compressed public key, as in Bitcoin.

## Serialisation primitives

* `uintN` — unsigned little-endian integer of N bits.
* `varint` — compact size: `< 0xfd` in one byte; `0xfd` + `uint16`; `0xfe` +
  `uint32`; `0xff` + `uint64`. Encodings must be minimal; readers reject
  non-canonical ones.
* `varbytes` — `varint` length followed by that many bytes.
* `hash32` — 32 raw bytes.

## Transactions

A transaction is serialised as a **body** followed by the **witnesses**:

```
body:
    uint32   version
    varint   input count
      per input:
        hash32  previous transaction id
        uint32  previous output index
    varint   output count
      per output:
        uint64  value in scar
        [20]    public-key hash
    uint32   lock_time
    varbytes coinbase data           (empty unless this is a coinbase)

witnesses (one pair per input, same order):
    varbytes public key   (33 bytes, or empty in a coinbase)
    varbytes signature    (64 bytes, or empty in a coinbase)
```

**Transaction id** = `hash256(body)`. Signatures are therefore *not* covered by
the id: a transaction's identity is fixed the moment its inputs and outputs are
decided.

**Signature hash** for input `i` spending an output worth `v`:

```
hash256( varbytes("ScarletCoin/sighash/1") || body || uint32(i) || uint64(v) )
```

It commits to every input and output, to which input is being signed, and to the
value being spent, so a signature cannot be replayed elsewhere.

**Coinbase.** The first transaction of a block has exactly one input whose
previous outpoint is `(0x00…00, 0xffffffff)` and whose witness is empty. Its
`coinbase data` starts with the block height as a `uint32`, followed by up to 96
free bytes (extra nonce, messages). Committing to the height makes every
coinbase unique, so no two blocks can share a transaction id.

## Blocks

The header is exactly 80 bytes:

```
uint32  version
hash32  previous block hash
hash32  Merkle root
uint32  timestamp (seconds since the Unix epoch)
uint32  bits (compact target)
uint32  nonce
```

`block hash = hash256(header)`.

A block is the header followed by a `varint` transaction count and the
transactions.

**Merkle root.** Leaves are transaction ids in block order. At each level, if the
number of nodes is odd the last one is duplicated, then adjacent pairs are
combined with `hash256(left || right)`. Blocks containing duplicate transaction
ids are invalid, which removes the ambiguity this duplication would otherwise
allow.

## Proof of work

`bits` is Bitcoin's compact encoding: the top byte is an exponent, the low three
bytes a mantissa, and

```
target = mantissa · 256^(exponent − 3)
```

A block is valid when the block hash, read as a **little-endian** integer, is
less than or equal to `target`, and `target` is not easier than the network's
`pow_limit`. A block's work is `2^256 / (target + 1)`; a chain's work is the sum
over its blocks.

**Retargeting.** At every height that is a multiple of `retarget_interval`:

```
first    = ancestor at (height − retarget_interval)
observed = parent.timestamp − first.timestamp        clamped to
           [target_timespan / 4, target_timespan · 4]
target   = min(parent_target · observed / target_timespan, pow_limit)
```

where `target_timespan = target_spacing · retarget_interval`. At every other
height the target is the parent's.

## Money supply

```
subsidy(height) = 50 · COIN >> (height / halving_interval)      (0 after 64 halvings)
```

With `halving_interval = 210 000` the total ever created is just under
21 000 000 SCT. A coinbase may pay *at most* `subsidy(height)` plus the fees of
the other transactions in the block; paying less is allowed and destroys the
difference.

## Validation rules

Validation happens in three stages, so that a block can be checked as far as
possible before it is stored.

**1. Sanity** (no context needed)

* at least one transaction, and the first one is a coinbase;
* exactly one coinbase;
* serialised size at most `max_block_size` (1 MB);
* the header hash meets the header's own target, which is not easier than `pow_limit`;
* no duplicate transaction ids;
* the Merkle root matches the transactions;
* every transaction is well formed: at least one input and one output, no output
  negative or above the money supply, no output value sum above it, no duplicate
  inputs, no null outpoint outside a coinbase, coinbase data only in a coinbase,
  witnesses exactly 33 and 64 bytes long.

**2. Context** (needs the parent block only)

* `bits` equals the value the retargeting rule requires;
* `timestamp` is strictly greater than the median of the last 11 blocks;
* `timestamp` is at most two hours ahead of the validating node's clock;
* the height in the coinbase data equals the parent's height plus one.

**3. Connection** (needs the UTXO set at the parent)

* every input spends an output that exists and is unspent, including outputs
  created earlier *in the same block*;
* a coinbase output may only be spent once `coinbase_maturity` blocks have been
  built on top of the block that created it;
* the revealed public key hashes to the output's `hash160`;
* the signature is valid and canonical;
* input value sum ≥ output value sum; the difference is the fee;
* every transaction is final: `lock_time == 0` or `lock_time ≤ height`;
* the coinbase pays at most subsidy plus fees.

A node that already knows a block returns "duplicate"; a block whose parent is
unknown is an "orphan", kept aside for a while and retried when its parent
arrives.

## Chain selection

The active chain is the stored branch with the greatest cumulative work. When a
side branch overtakes the tip, the node walks back to the fork point,
disconnects the blocks it is abandoning (restoring the coins they spent from
per-block undo data), then connects the new branch block by block. The whole
switch happens inside one database transaction: if a block on the new branch
turns out to be invalid, everything is rolled back, the offending block and its
descendants are marked unusable, and the previous chain stays active.
Transactions from disconnected blocks go back to the mempool; those that are no
longer valid are dropped.

## Network parameters

| | mainnet | testnet | regtest |
|---|---|---|---|
| Magic | `SCRL` | `SCRT` | `SCRR` |
| Address version | 63 (`S…`) | 127 (`t…`) | 127 (`t…`) |
| WIF version | 191 | 239 | 239 |
| P2P / RPC port | 20333 / 20332 | 30333 / 30332 | 40333 / 40332 |
| `target_spacing` | 60 s | 60 s | 10 s |
| `retarget_interval` | 60 | 60 | 20 |
| `pow_limit_bits` | `0x1e0fffff` | `0x1e0fffff` | `0x207fffff` |
| `coinbase_maturity` | 100 | 20 | 2 |
| `halving_interval` | 210 000 | 210 000 | 210 000 |
| `max_block_size` | 1 000 000 | 1 000 000 | 1 000 000 |
| `min_relay_fee_per_kb` | 1 000 scar | 1 000 scar | 1 000 scar |
| Genesis hash | `00000ca129aa591dffe251f7815ed4a2ae87db9005945842bd7297a869c9ba1f` | `00000761a924bc06afd611e52b7d4414e780f1a1bd0d9c1203992186a7cd41a5` | `3094ff7a10619c989c07fd4807368bf0b638ea0a62f5f8eb763c5485aa4a3d50` |

## Peer-to-peer protocol

Every message is framed the same way:

```
[4]   magic          network identifier
[12]  command        ASCII, zero padded
[4]   payload length uint32, at most 2 MiB
[4]   checksum       hash256(payload)[:4]
[…]   payload
```

Unknown commands are ignored, so the protocol can grow without breaking older
nodes.

| Command | Payload | Purpose |
|---|---|---|
| `version` | `uint32` version, `varstr` user agent, `uint32` height, `uint64` nonce, `uint16` listening port, `uint64` timestamp | opens the handshake |
| `verack` | — | accepts a `version` |
| `ping` / `pong` | `uint64` nonce | liveness and latency |
| `getaddr` | — | asks for known peers |
| `addr` | `varint` count, then `varstr` host, `uint16` port, `uint32` last seen | gossips peers |
| `getblocks` | `varint` count + `hash32` locator hashes, `hash32` stop hash | asks what follows a locator |
| `inv` | `varint` count, then `uint8` type (1 tx, 2 block) + `hash32` | announces objects |
| `getdata` | same as `inv` | requests objects |
| `notfound` | same as `inv` | reports objects not held |
| `block` | a serialised block | delivers a block |
| `tx` | a serialised transaction | delivers a transaction |
| `mempool` | — | asks for an announcement of the pool |

**Handshake.** The connecting side sends `version`; the other answers with its
own `version`; both then send `verack`. A node that sees its own nonce
disconnects (it dialled itself). Only `version` and `verack` are accepted before
the handshake completes.

**Synchronising.** After the handshake, a node behind its peer sends `getblocks`
with a *locator*: the tip, then progressively sparser ancestors, ending at the
genesis hash. The peer replies with an `inv` of up to 500 hashes on its active
chain after the newest locator entry it recognises. Blocks are fetched with
`getdata`, at most 64 in flight per peer, and the process repeats until the
chains agree.

**Relaying.** Newly accepted blocks and mempool transactions are announced with
`inv` to every peer except the one they came from, skipping peers already known
to have them.

**Misbehaviour.** Invalid blocks cost a peer 50 points and protocol violations
100; at 100 points the peer is disconnected and its address is banned for an
hour. Peers silent for 60 seconds get a `ping`; silent for 180 seconds they are
dropped.

## JSON-RPC interface

`POST /` or `POST /rpc`, JSON-RPC 2.0, single requests or batches. When a token
is configured, requests must carry `Authorization: Bearer <token>`.

| Method | Parameters | Returns |
|---|---|---|
| `getinfo` | — | node, chain and network status |
| `getblockcount` | — | active chain height |
| `getbestblockhash` | — | tip hash |
| `getdifficulty` | — | current difficulty |
| `getsupply` | — | circulating supply and UTXO count |
| `getblockhash` | `height` | block hash |
| `getblock` | `hash_or_height`, `verbose=true` | block with transactions |
| `getblockheader` | `hash_or_height` | header fields |
| `getrawblock` | `hash_or_height` | hex-encoded block |
| `gettransaction` | `txid` | decoded transaction with confirmations |
| `getrawtransaction` | `txid` | hex-encoded transaction |
| `sendrawtransaction` | `hex` | txid, after validation and relay |
| `getmempool` | — | pool contents with fees |
| `validateaddress` | `address` | validity and public-key hash |
| `getbalance` | `address` | confirmed, spendable and immature balance |
| `getutxos` | `address` | unspent outputs |
| `getaddresshistory` | `address`, `limit=100` | transactions touching the address |
| `getrichlist` | `limit=10` | largest balances |
| `getblocktemplate` | — | work for a miner |
| `submitblock` | `hex` | acceptance status and height |
| `getpeers` | — | connected peers |
| `addpeer` | `host`, `port` | connect to a peer |
| `getaddresses` | — | the address book |
| `stop` | — | shut the node down |
| `generate` | `count=1`, `address=None` | mine blocks immediately (**regtest only**) |

The same HTTP server answers `GET /api/info` with the `getinfo` document, and
serves the HTML explorer at `/`, `/blocks`, `/block/<hash-or-height>`,
`/tx/<txid>`, `/address/<address>`, `/mempool`, `/peers`, `/rich` and
`/search?q=…`.

A **block template** gives the miner everything except the coinbase:

```json
{
  "height": 12, "previous_block": "…", "bits": "0x207fffff", "target": "…",
  "min_time": 1700000011, "current_time": 1700000042,
  "coinbase_value": 5000000475, "version": 1,
  "transactions": ["<hex>", "…"]
}
```

The miner builds its own coinbase paying `coinbase_value` to its address,
computes the Merkle root over `[coinbase] + transactions`, and searches for a
nonce. That way the node never handles the miner's keys and the miner can roll
its extra nonce without asking for new work.

## Wallet file format

```json
{
  "version": 1,
  "network": "mainnet",
  "encrypted": true,
  "addresses": [{"address": "S…", "label": "main", "created": 1700000000}],
  "crypto": {
    "kdf": "scrypt",
    "kdf_params": {"n": 65536, "r": 8, "p": 1, "salt": "<hex>"},
    "cipher": "aes-256-gcm",
    "nonce": "<hex>",
    "ciphertext": "<hex>"
  }
}
```

The plaintext inside `crypto` is a JSON list of `{"wif", "label", "created"}`.
The additional authenticated data is `scarletcoin-wallet-v1:<network>`, so a
wallet file cannot be replayed onto another network. Unencrypted wallets carry
the same list in a top-level `keys` field instead. Addresses stay readable
either way, which lets the wallet show balances while locked.
