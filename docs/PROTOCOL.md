# ScarletCoin protocol and consensus reference (v3 — Anonymous)

ScarletCoin 3.0 replaces transparent transactions with linkable ring
signatures and stealth addresses. Every transaction is private by default:
the sender, the recipient and the link between payments are all hidden.
Amounts remain visible so the monetary supply stays auditable.

Byte order is little-endian unless stated otherwise.

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

`hash160` and the transparent-address scheme are retired; all outputs now
commit to a 33-byte compressed secp256k1 one-time public key.

## Keys and addresses

ScarletCoin 3.0 uses a dual-key stealth-address scheme derived from
CryptoNote. Every address is a pair of curve points:

* View key `(a, A)` — `a·G = A`. The view key can *recognise* outputs that
  belong to the address but cannot spend them.
* Spend key `(b, B)` — `b·G = B`. The spend key, together with the view
  key, can spend a recognised output.

A sender picks a random ephemeral scalar `r` and produces a one-time public
key `P` that only the recipient's wallet can recognise:

```
R    = r·G
P    = H_s(r·A)·G  +  B
```

`H_s` is `hash256` reduced modulo the curve order. The transaction carries
`R` (33 bytes, `tx_public_key`) once, and each output carries its own `P`.

The recipient, who holds the private view key `a`, scans every transaction:

```
P' = H_s(a·R)·G  +  B
```

If `P' == P`, the output is theirs. The private spending key of that
one-time output is:

```
x_spend = H_s(a·R)  +  b
```

**Keys:**
* Private view key: 32 random bytes, interpreted as a secp256k1 scalar.
* Private spend key: 32 random bytes, interpreted as a secp256k1 scalar.
* Public view key `A` and public spend key `B`: compressed 33-byte SEC1
  encoding (`0x02` / `0x03` prefix).

**Address:** `Base58Check(stealth_version || A || B)` — two 33-byte
compressed keys, plus a 4-byte checksum. The version byte distinguishes
networks (see [Network parameters](#network-parameters)).

## Serialisation primitives

* `uintN` — unsigned little-endian integer of N bits.
* `varint` — compact size: `< 0xfd` in one byte; `0xfd` + `uint16`; `0xfe` +
  `uint32`; `0xff` + `uint64`. Encodings must be minimal; readers reject
  non-canonical ones.
* `varbytes` — `varint` length followed by that many bytes.
* `hash32` — 32 raw bytes.

## Transactions

Every transaction is version 2. A transaction is serialised as a **body**
followed by the **witnesses**:

```
body:
    uint32   version  (=2)
    varint   input count
      per input:
        varint   ring size
          per ring member:
            [33]    one-time public key
        [33]    key image
    varint   output count
      per output:
        uint64  value in scar
        [33]    one-time public key
    uint32   lock_time
    [33]     tx_public_key (R)        (zero-filled for none)
    varbytes coinbase data           (empty unless this is a coinbase)

witnesses (one per input, same order):
    varbytes ring signature           (CLSAG/MLSAG; empty in a coinbase)
```

**Transaction id** = `hash256(body)`. Signatures are therefore *not* covered by
the id: a transaction's identity is fixed the moment its inputs and outputs are
decided.

**Signature hash** for input `i`:

```
hash256( varbytes("ScarletCoin/sighash/2") || body || uint32(i) )
```

It commits to every input (ring members + key images), every output, the
lock time, the ephemeral key `R` and the extra data, plus which input is
being signed — so a signature cannot be replayed on another input or
another transaction.

**Ring signatures** use an LSAG (Linkable Spontaneous Anonymous Group)
construction on secp256k1. The signature proves that the signer knows the
discrete log of *one* public key in the ring, without revealing which one.
The key image `K = x_s · H_p(P_s)` — where `H_p` is hash-to-point — makes
the signature *linkable*: spending the same output twice produces the same
key image, which the network rejects.

The serialized signature is:

* `varint` ring size `n`
* `c_0`              — 32 bytes
* `r_0 … r_{n-1}`    — n × 32 bytes
* `K`                — 33 bytes (compressed key image)

Ring size must be between 2 and 32 inclusive, with a target of 16 for
adequate privacy. Decoy outputs are sampled with a recency-weighted gamma
distribution over outputs of the same value, falling back to uniform
random when not enough candidates exist.

**Coinbase.** The first transaction of a block has exactly one input whose
ring is empty and whose key image is zeroed. Its `coinbase data` starts
with the block height as a `uint32`, followed by up to 96 free bytes
(extra nonce, messages). Committing to the height makes every coinbase
unique.

**Transaction version 1** (transparent pay-to-pubkey-hash) is retired.
Any block containing a version-1 transaction is rejected by this build.

## Blocks

The header is exactly 80 bytes (unchanged from v2):

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

**Merkle root.** Leaves are transaction ids in block order. Duplicate
transaction ids are invalid and remove the ambiguity of the odd-level
duplication rule.

## Proof of work

Unchanged from v2. `bits` is Bitcoin's compact encoding, and a block is
valid when the block hash, as a little-endian integer, is ≤ `target`.
Retargeting adjusts difficulty every `retarget_interval` blocks towards
the target spacing.

## Money supply

```
subsidy(height) = 50 · COIN >> (height / halving_interval)      (0 after 64 halvings)
```

With `halving_interval = 210 000` the total ever created is just under
21 000 000 SCT. A coinbase may pay *at most* `subsidy(height)` plus the
fees of the other transactions in the block; paying less destroys the
difference.

## Validation rules

Validation happens in three stages:

**1. Sanity** (no context needed)

* At least one transaction, and the first one is a coinbase;
* Exactly one coinbase;
* Serialised size at most `max_block_size` (1 MB);
* The header hash meets the header's own target, which is not easier than
  `pow_limit`;
* No duplicate transaction ids;
* The Merkle root matches the transactions;
* Every transaction is well formed: at least one input and one output, no
  output negative or above `MAX_MONEY`, no output value sum above
  `MAX_MONEY`, no duplicate ring members, ring size in `[2, 32]`,
  coinbase data only in a coinbase, coinbase input has an empty ring and
  no pre-computed signature.

**2. Context** (needs the parent block only)

* `bits` equals the value the retargeting rule requires;
* `timestamp` is strictly greater than the median of the last 11 blocks;
* `timestamp` is at most two hours ahead of the validating node's clock;
* The height in the coinbase data equals the parent's height plus one.

**3. Connection** (needs the output set at the parent)

* **Every ring member must exist** in the output set;
* **All ring members of a given input must have the same value** — this
  makes the input's value unambiguous without RingCT;
* Every ring member must satisfy the coinbase-maturity rule (the validator
  cannot tell which member is real, so every member is checked);
* The key image must not already exist in the key-image set;
* The ring signature must verify against the transaction's signature hash;
* input value sum ≥ output value sum; the difference is the fee;
* Every transaction is final: `lock_time == 0` or `lock_time ≤ height`;
* The coinbase pays at most subsidy plus fees.

**Equal-value rule.** Because the validator does not know which ring member
is being spent, the input value must be derivable without ambiguity: all
ring members must reference outputs of identical value. The input value is
that common value. This is the honest cost of leaving amounts visible; it
is why Monero moved to RingCT.

## Chain selection

The active chain is the stored branch with the greatest cumulative work.
Reorganisations are atomic database transactions: if a block on the new
branch is invalid, everything rolls back.

**Undo data.** Outputs are never removed from the output set when spent
(the validator cannot determine which ring member was real). Only key
images are stored. Disconnecting a block removes the outputs it created
(by iterating its transactions) and the key images it recorded.

## Network parameters

| | mainnet | testnet | regtest |
|---|---|---|---|
| Magic | `SCRL` | `SCRT` | `SCRR` |
| Stealth address version | 83 | 128 | 128 |
| P2P / RPC port | 20333 / 20332 | 30333 / 30332 | 40333 / 40332 |
| `target_spacing` | 60 s | 60 s | 10 s |
| `retarget_interval` | 60 | 60 | 20 |
| `pow_limit_bits` | `0x1e0fffff` | `0x1e0fffff` | `0x207fffff` |
| `coinbase_maturity` | 100 | 20 | 2 |
| `halving_interval` | 210 000 | 210 000 | 210 000 |
| `max_block_size` | 1 000 000 | 1 000 000 | 1 000 000 |
| `min_relay_fee_per_kb` | 1 000 scar | 1 000 scar | 1 000 scar |
| Ring size target | 16 | 16 | 16 |
| Genesis hash | `00000dbfd8f9ec1b6eb641fc62b1a72f365fb78a66215ce3cde4dfc9a12f200b` | `00000e92a8619b3be43c2f0fe5b9114c9c30085d22e0e53a4c9ba030f6513515` | `405434a980b6e2cc564d14dd9af542b7cb466a57b2db5f20bc9674d8b74cec49` |

## Peer-to-peer protocol

Every message is framed the same way:

```
[4]   magic          network identifier
[12]  command        ASCII, zero padded
[4]   payload length uint32, at most 2 MiB
[4]   checksum       hash256(payload)[:4]
[...] payload
```

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

Unchanged from v2. Unknown commands are ignored, so the protocol can
grow without breaking older nodes.

**Dandelion stem relay.** A transaction that originates at this node
(via RPC, not from a peer) is announced to a single random outbound peer
instead of being broadcast. That peer cannot distinguish the origin from
an ordinary forwarder. After the next mempool re-announcement the
transaction is broadcast normally. This is a simplified single-hop Dandelion
stem phase that hides the origin of a transaction.

## JSON-RPC interface

`POST /` or `POST /rpc`, JSON-RPC 2.0. When a token is configured, requests
must carry `Authorization: Bearer <token>`.

Public methods (answered without a token on a `--rpc-public` node):
`getinfo`, `getblockcount`, `getbestblockhash`, `getdifficulty`,
`getsupply`, `getchainsize`, `getnetworkstats`, `getpublicnodes`,
`getblockhash`, `getblock`, `getblockheader`, `getrawblock`,
`gettransaction`, `getrawtransaction`, `sendrawtransaction`,
`getmempool`, `getoutputs`, `getkeyimages`.

Mining methods (`--rpc-public-mining`): `getblocktemplate`, `submitblock`.

Transparent-address methods (`getbalance`, `getutxos`,
`getaddresshistory`, `getrichlist`, `validateaddress`) are retired.

The block explorer is served at `/`, `/blocks`, `/block/<hash-or-height>`,
`/tx/<txid>`, `/mempool` and `/peers`. Address pages and the rich list are
removed: balances can only be shown by a wallet that holds the view key.

## Wallet file format

```json
{
  "version": 2,
  "network": "mainnet",
  "encrypted": true,
  "addresses": [{"address": "stealth:...", "label": "main", "created": 1700000000}],
  "crypto": {
    "kdf": "scrypt",
    "kdf_params": {"n": 65536, "r": 8, "p": 1, "salt": "<hex>"},
    "cipher": "aes-256-gcm",
    "nonce": "<hex>",
    "ciphertext": "<hex>"
  }
}
```

The plaintext inside `crypto` is a JSON list of `{"view_wif", "spend_wif",
"label", "created"}`. Each entry is a dual-key pair (view key + spend key).
The associated data tag is `scarletcoin-wallet-v2:<network>`.