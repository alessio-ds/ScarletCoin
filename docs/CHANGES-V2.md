# What changed in version 2

Version 1 (2022) was a Flask server that kept every balance in a text file, plus
a Qt wallet and miner that talked to it with concatenated strings. It was fun,
but it was not a cryptocurrency: there was no chain, no signatures, and one
machine decided everyone's balance. Version 2 is a rewrite from an empty
directory.

## The problems, and what replaced them

| Version 1 | Version 2 |
|---|---|
| **No blockchain.** Balances lived in `data/addresses/<address>` as `sha256:balance`, edited in place. | A real chain of blocks with headers, Merkle roots, cumulative work and reorganisations. Balances are derived from unspent outputs. |
| **No signatures.** The "private key" was `sha256(random)`, stored in the clear *on the server* and sent in the clear with every payment. Anyone who saw it — the operator, or anyone on the network — could spend the coins. | secp256k1 key pairs. Each output commits to `hash160(public key)`; spending it requires an ECDSA signature over a digest that covers the whole transaction, the input index and the value spent. Private keys never leave the wallet. |
| **Proof of work meant nothing.** The miner hashed `hex(random × random)` until four leading zeros appeared and mailed the pre-image to the server, which credited exactly 1 coin. The work was attached to no transaction and no block. | Miners hash real 80-byte block headers that commit to the transactions they include. Difficulty retargets, the reward halves, and fees go to whoever finds the block. |
| **Negative amounts minted coins.** `amount = int(...)` was checked against `>= balance` and `== 0`, never against negatives. Sending `-5` credited the sender and debited the recipient, without limit. | Amounts are validated at construction: negative or oversized values cannot exist in a `TxOutput`, and inputs must cover outputs. Property-style tests cover the arithmetic. |
| **Path traversal.** `open('data/addresses/' + address)` with an unvalidated address allowed reading and truncating arbitrary files; `refresh../../etc/passwd` is exactly the 16 characters the balance handler expected. | No user input reaches a file path. State lives in SQLite with typed, parameterised queries; addresses are parsed as Base58Check with a checksum and network byte before anything else happens. |
| **`debug=True` on `0.0.0.0`.** The Werkzeug debugger — a remote shell — was exposed to the internet, and several code paths raised unhandled exceptions on demand. | A standard-library HTTP server with no debugger, structured JSON-RPC errors, and bearer-token authentication generated automatically at start-up. Binding RPC to a public address without a token is refused. |
| **Shell injection in the clients.** `echo <server response> | clip` with `shell=True` gave a malicious or spoofed server command execution on the user's machine. | The Qt applications use `QApplication.clipboard()`. No subprocess is spawned anywhere. |
| **Race conditions.** Every balance change was a read, then a compute, then a whole-file overwrite, with no locking and a threaded server — a double spend was a matter of timing, and a crash mid-write corrupted an account permanently. | Connecting or disconnecting a block is one SQLite transaction: it either happens completely or not at all. Chain mutation is serialised behind a lock. |
| **A string-prefix "protocol".** The server dispatched on substring tests over the request body (`'mined' in data`, `len(data) == 64`) and sliced fields by hard-coded offsets. An address that happened to contain `refresh` broke it. | A framed binary protocol with magic bytes, a command name, a length and a checksum for peers, and JSON-RPC for tools. Canonical serialisation with explicit length limits, and readers that reject non-minimal encodings. |
| **One central server.** Wallets and miners pointed at `alessiosca.ddns.net`. If it was down, the currency was down. | Every node is a full node: it validates independently, gossips addresses, serves initial block download, and follows the heaviest chain. Nodes find each other and disagree safely. |
| **A "block explorer" made of static files.** The server wrote one HTML file per transaction and regenerated the home page every 300 seconds by string-concatenating two half-templates, unescaped. | The explorer renders from the chain on request, with every field HTML-escaped, at `/`, `/blocks`, `/block/…`, `/tx/…`, `/address/…`, `/mempool`, `/peers` and `/rich`. |
| **Documented staking that did not exist.** The README promised 50% APR paid every five minutes; no code implemented it. | Removed. The README now describes only what the code does, including its limitations. |
| **No tests, no CI, no dependency manifest, no docs.** | 285 tests covering crypto vectors, serialisation, consensus rules, reorganisations, the mempool, RPC, the explorer, two-node networking, the wallet, the miner, the command line tools and the Qt windows. `pyproject.toml` pins the dependencies, `ruff` lints and formats, and `docs/PROTOCOL.md` specifies the format precisely enough to reimplement. |
| **Italian and profanity in user-facing output.** A failed authentication answered `figlio di puttana`; identifiers were `manda`, `stronzium`, `diz_top`, `testo`, `nuovotesto`; a wallet field shipped with the placeholder `i just shit at my ass`. | Everything — code, comments, errors, interface, documentation — is in English, and error messages explain what went wrong and what to do about it. |

## Things that stayed

* The name, the ticker (SCT), the scarlet colour scheme, and the idea of shipping
  a node, a wallet and a miner together.
* Base58Check addresses, and mining as the way coins come into existence.
* The MIT licence.

## Compatibility

None. Version 2 shares no data format, no wire format and no address format with
version 1, and its chain starts from a new genesis block. Old "balances" were
entries in a text file on one server; there is nothing to migrate and no honest
way to migrate it.
