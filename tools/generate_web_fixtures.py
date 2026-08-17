"""Generate golden fixtures for the ScarletCoin web wallet's test suite.

Run from the ScarletCoin checkout (needs the ``scarletcoin`` package):

    uv run python tools/generate_web_fixtures.py

Writes ``test/fixtures/golden.json`` in the sibling ``scarletcoin-web-wallet``
repository. The web wallet's tests read this file and must reproduce every value
byte-for-byte, which is what keeps the two implementations from drifting apart.
"""

from __future__ import annotations

import json
from pathlib import Path

from scarletcoin.core.params import COIN, MAINNET
from scarletcoin.core.transaction import OutPoint
from scarletcoin.core.utxo import Coin
from scarletcoin.crypto.hashing import hash256, sha256
from scarletcoin.crypto.keys import Address, PrivateKey
from scarletcoin.wallet.builder import build_sweep_transaction, build_transaction

OUT = (
    Path(__file__).resolve().parents[1]
    / ".."
    / "scarletcoin-web-wallet"
    / "test"
    / "fixtures"
    / "golden.json"
)


def secret(n: int) -> PrivateKey:
    return PrivateKey.from_bytes(bytes(range(n, n + 32)))


def main() -> None:
    key1 = secret(1)
    key2 = secret(33)
    key3 = secret(65)

    pub1 = key1.public_key()
    pub2 = key2.public_key()
    pub3 = key3.public_key()

    digest = hash256(b"ScarletCoin golden fixture")

    coin1 = (OutPoint(bytes(range(1, 33)), 0), Coin(50 * COIN, pub1.hash160(), 100, False))
    coin2 = (OutPoint(bytes(range(33, 65)), 1), Coin(25 * COIN, pub2.hash160(), 101, False))

    destination = Address(MAINNET.address_version, pub3.hash160())

    built = build_transaction(
        spendable_coins=[coin1, coin2],
        keys={pub1.hash160(): key1, pub2.hash160(): key2},
        outputs=[(destination, 30 * COIN)],
        change_hash=pub1.hash160(),
        fee_per_kb=1000,
        params=MAINNET,
    )

    sweep = build_sweep_transaction(
        spendable_coins=[coin1, coin2],
        keys={pub1.hash160(): key1, pub2.hash160(): key2},
        destination=destination,
        fee_per_kb=1000,
        params=MAINNET,
    )

    fixture = {
        "address_version": MAINNET.address_version,
        "wif_version": MAINNET.wif_version,
        "coin": COIN,
        "min_relay_fee_per_kb": MAINNET.min_relay_fee_per_kb,
        "sha256_abc": sha256(b"abc").hex(),
        "hash256_abc": hash256(b"abc").hex(),
        "keys": [
            {
                "secret": key1.to_bytes().hex(),
                "pubkey": pub1.to_bytes().hex(),
                "address": str(key1.address(MAINNET.address_version)),
                "wif": key1.to_wif(MAINNET.wif_version),
            },
            {
                "secret": key2.to_bytes().hex(),
                "pubkey": pub2.to_bytes().hex(),
                "address": str(key2.address(MAINNET.address_version)),
                "wif": key2.to_wif(MAINNET.wif_version),
            },
            {
                "secret": key3.to_bytes().hex(),
                "pubkey": pub3.to_bytes().hex(),
                "address": str(key3.address(MAINNET.address_version)),
                "wif": key3.to_wif(MAINNET.wif_version),
            },
        ],
        "sign": {
            "secret": key1.to_bytes().hex(),
            "digest": digest.hex(),
            "signature": key1.sign(digest).hex(),
        },
        "transaction": {
            "fee": built.fee,
            "change": built.change,
            "total_input": built.total_input,
            "txid": built.transaction.txid_hex(),
            "body": built.transaction.serialize_body().hex(),
            "raw": built.transaction.serialize().hex(),
            "size": built.transaction.size(),
        },
        "sweep": {
            "fee": sweep.fee,
            "change": sweep.change,
            "total_input": sweep.total_input,
            "txid": sweep.transaction.txid_hex(),
            "body": sweep.transaction.serialize_body().hex(),
            "raw": sweep.transaction.serialize().hex(),
            "size": sweep.transaction.size(),
        },
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fixture, indent=2) + "\n", "utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
