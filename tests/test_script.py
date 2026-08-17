"""Tests for the P2SH script engine and pay-to-script-hash transactions."""

from __future__ import annotations

import pytest

from scarletcoin.core.params import REGTEST
from scarletcoin.core.script import (
    MAX_PUBKEYS,
    ScriptError,
    decode_ops,
    evaluate_script,
    multisig_redeem,
    p2pkh_redeem,
    push_data,
)
from scarletcoin.core.transaction import (
    OutPoint,
    Transaction,
    TxInput,
    TxOutput,
)
from scarletcoin.core.utxo import Coin, CoinOverlay
from scarletcoin.core.validation import ValidationError, check_transaction_inputs
from scarletcoin.crypto.hashing import hash256
from scarletcoin.crypto.keys import PrivateKey


class TestScript:
    def test_multisig_2_of_3(self):
        keys = [PrivateKey.generate() for _ in range(3)]
        pubkeys = [key.public_key().to_bytes() for key in keys]
        script = multisig_redeem(pubkeys, 2)
        digest = b"\x42" * 32
        sigs = [key.sign(digest) for key in keys]
        assert evaluate_script(script, [sigs[0], sigs[1]], digest)
        assert evaluate_script(script, [sigs[1], sigs[2]], digest)
        assert not evaluate_script(script, [sigs[0]], digest)

    def test_multisig_wrong_key_fails(self):
        keys = [PrivateKey.generate() for _ in range(3)]
        stranger = PrivateKey.generate()
        pubkeys = [key.public_key().to_bytes() for key in keys]
        script = multisig_redeem(pubkeys, 2)
        digest = b"\x42" * 32
        sigs = [keys[0].sign(digest), stranger.sign(digest)]
        assert not evaluate_script(script, sigs, digest)

    def test_threshold_must_fit_the_keys(self):
        keys = [PrivateKey.generate() for _ in range(2)]
        pubkeys = [key.public_key().to_bytes() for key in keys]
        with pytest.raises(ScriptError, match="threshold"):
            multisig_redeem(pubkeys, 3)

    def test_too_many_keys_is_refused(self):
        pubkeys = [PrivateKey.generate().public_key().to_bytes() for _ in range(MAX_PUBKEYS + 1)]
        with pytest.raises(ScriptError, match="public keys"):
            multisig_redeem(pubkeys, 1)

    def test_p2pkh_redeem(self):
        key = PrivateKey.generate()
        script = p2pkh_redeem(key.public_key().hash160())
        digest = b"\x24" * 32
        assert evaluate_script(script, [key.sign(digest), key.public_key().to_bytes()], digest)

    def test_p2pkh_redeem_wrong_hash_fails(self):
        key = PrivateKey.generate()
        script = p2pkh_redeem(bytes(20))
        digest = b"\x24" * 32
        assert not evaluate_script(script, [key.sign(digest), key.public_key().to_bytes()], digest)

    def test_unknown_opcode_is_refused(self):
        with pytest.raises(ScriptError, match="unknown script opcode"):
            decode_ops(b"\xff")

    def test_truncated_push_is_refused(self):
        with pytest.raises(ScriptError, match="data push"):
            decode_ops(b"\x0a\x01\x02")

    def test_push_data_round_trip(self):
        for length in (1, 75, 76, 255, 256, 500):
            data = b"\x07" * length
            script = push_data(data)
            assert decode_ops(script)[0][1] == data

    def test_oversized_script_is_refused(self):
        with pytest.raises(ScriptError, match="limit"):
            decode_ops(b"\x00" * 521)


class TestP2shTransactions:
    def _p2sh_coin(self, redeem_script: bytes, value: int = 1000) -> tuple[OutPoint, Coin]:
        return OutPoint(b"\x11" * 32, 0), Coin(value, 1, hash256(redeem_script)[:20], 1, False)

    def test_spending_a_multisig_p2sh_output(self):
        keys = [PrivateKey.generate() for _ in range(3)]
        pubkeys = [key.public_key().to_bytes() for key in keys]
        script = multisig_redeem(pubkeys, 2)
        outpoint, coin = self._p2sh_coin(script)

        overlay = CoinOverlay(None)
        overlay.add(outpoint, coin)

        destination = PrivateKey.generate().public_key().hash160()
        unsigned = Transaction(
            inputs=(TxInput(outpoint),),
            outputs=(TxOutput.p2pkh(900, destination),),
        )
        digest = unsigned.signature_hash(0, coin.value, script)
        spend = unsigned.signed_with({0: (script, keys[0].sign(digest), keys[2].sign(digest))})

        fee = check_transaction_inputs(spend, overlay, height=2, params=REGTEST)
        assert fee == 100

    def test_a_wrong_redeem_script_is_rejected(self):
        keys = [PrivateKey.generate() for _ in range(3)]
        pubkeys = [key.public_key().to_bytes() for key in keys]
        script = multisig_redeem(pubkeys, 2)
        outpoint, coin = self._p2sh_coin(script)

        overlay = CoinOverlay(None)
        overlay.add(outpoint, coin)

        unsigned = Transaction(
            inputs=(TxInput(outpoint),),
            outputs=(TxOutput.p2pkh(900, bytes(20)),),
        )
        digest = unsigned.signature_hash(0, coin.value, script)
        other = multisig_redeem(
            [PrivateKey.generate().public_key().to_bytes() for _ in range(2)], 1
        )
        spend = unsigned.signed_with({0: (other, keys[0].sign(digest))})
        with pytest.raises(ValidationError, match="invalid signature"):
            check_transaction_inputs(spend, overlay, height=2, params=REGTEST)

    def test_p2sh_output_is_round_tripped(self):
        script = multisig_redeem([PrivateKey.generate().public_key().to_bytes()], 1)
        output = TxOutput.p2sh(500, hash256(script)[:20])
        assert output.is_p2sh
        transaction = Transaction(inputs=(TxInput(OutPoint(b"\x22" * 32, 1)),), outputs=(output,))
        assert Transaction.deserialize(transaction.serialize()) == transaction

    def test_p2sh_address_prefixes_differ(self):
        script = multisig_redeem([PrivateKey.generate().public_key().to_bytes()], 1)
        address = REGTEST.script_address_version, hash256(script)[:20]
        from scarletcoin.crypto.keys import Address

        assert str(Address(*address)).startswith("T")
        assert str(Address(REGTEST.address_version, hash256(script)[:20])).startswith("t")
