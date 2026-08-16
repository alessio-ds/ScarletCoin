"""Tests for the TPS load-test tool in ``tools/tps_test.py``."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from scarletcoin.wallet.keystore import Keystore

_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"


def _load_tool():
    spec = importlib.util.spec_from_file_location("_tps_test_tool", _TOOLS_DIR / "tps_test.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _funded_wallet(rpc, tmp_path) -> Keystore:
    _, _, client = rpc
    keystore = Keystore.create(tmp_path / "wallet.json", "regtest")
    client.call("generate", 4, keystore.default_address())
    return keystore


def test_split_and_burst_on_regtest(rpc, tmp_path):
    """Split a funded balance into UTXOs, then burst-spend them all."""
    _, _, client = rpc
    tool = _load_tool()
    keystore = _funded_wallet(rpc, tmp_path)

    split = tool.split_utxos(keystore, client, utxos=50, confirm=True, confirm_timeout=30.0)
    assert split["confirmed"] is True
    assert split["utxos"] >= 50

    result = tool.run_burst(keystore, client, txs=50, workers=4, watch=5.0)
    assert result["attempted"] == 50
    assert result["accepted"] == 50
    assert result["tps"] > 0
    assert result["watch"]["mempool_left"] == 0


def test_split_without_funds_fails(rpc, tmp_path):
    tool = _load_tool()
    _, _, client = rpc
    keystore = Keystore.create(tmp_path / "wallet.json", "regtest")
    with pytest.raises(tool.TpsError, match="no spendable coins"):
        tool.split_utxos(keystore, client, utxos=10, confirm=False)


def test_split_rejects_too_many_outputs(rpc, tmp_path):
    tool = _load_tool()
    _, _, client = rpc
    keystore = _funded_wallet(rpc, tmp_path)
    with pytest.raises(tool.TpsError, match="relays at most"):
        tool.split_utxos(keystore, client, utxos=10_000_000, confirm=False)


def test_run_without_utxos_fails(rpc, tmp_path):
    tool = _load_tool()
    _, _, client = rpc
    keystore = Keystore.create(tmp_path / "wallet.json", "regtest")
    with pytest.raises(tool.TpsError, match="no spendable coins"):
        tool.run_burst(keystore, client, txs=10, workers=2)
