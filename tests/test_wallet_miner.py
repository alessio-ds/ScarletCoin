"""Tests for the wallet, the keystore, the miner and the command line tools."""

from __future__ import annotations

import json

import pytest

from scarletcoin.core.params import REGTEST
from scarletcoin.crypto.keys import Address, PrivateKey
from scarletcoin.miner.miner import Miner, MiningError
from scarletcoin.miner.solver import scan_nonces, solve_block
from scarletcoin.net.client import RpcClientError
from scarletcoin.units import format_amount
from scarletcoin.wallet.builder import InsufficientFundsError
from scarletcoin.wallet.keystore import Keystore, WalletError, WalletLocked
from scarletcoin.wallet.wallet import Wallet
from tests.helpers import mine_block


class TestKeystore:
    def test_create_and_load(self, tmp_path):
        path = tmp_path / "wallet.json"
        keystore = Keystore.create(path, "regtest")
        address = keystore.default_address()
        assert Address.is_valid(address, expected_version=REGTEST.address_version)

        reloaded = Keystore.load(path)
        assert reloaded.default_address() == address
        assert not reloaded.encrypted
        assert len(reloaded.keys) == 1

    def test_refuses_to_overwrite(self, tmp_path):
        path = tmp_path / "wallet.json"
        Keystore.create(path, "regtest")
        with pytest.raises(WalletError, match="already exists"):
            Keystore.create(path, "regtest")

    def test_encrypted_wallets_need_the_password(self, tmp_path):
        path = tmp_path / "wallet.json"
        keystore = Keystore.create(path, "regtest", password="hunter2")
        address = keystore.default_address()

        locked = Keystore.load(path)
        assert locked.encrypted and locked.locked
        assert locked.address_strings() == [address]  # addresses stay readable
        with pytest.raises(WalletLocked):
            locked.keys  # noqa: B018 - the property raises
        with pytest.raises(WalletError, match="wrong password"):
            locked.unlock("wrong")
        locked.unlock("hunter2")
        assert not locked.locked
        assert locked.default_address() == address

    def test_adding_a_password_later(self, tmp_path):
        path = tmp_path / "wallet.json"
        keystore = Keystore.create(path, "regtest")
        keystore.set_password("hunter2")
        assert Keystore.load(path).locked
        keystore.set_password(None)
        assert not Keystore.load(path).encrypted

    def test_encrypted_file_does_not_contain_the_key(self, tmp_path):
        path = tmp_path / "wallet.json"
        keystore = Keystore.create(path, "regtest", password="hunter2")
        wif = keystore.export_wif(keystore.default_address())
        assert wif not in path.read_text()
        document = json.loads(path.read_text())
        assert document["crypto"]["cipher"] == "aes-256-gcm"
        assert "keys" not in document

    def test_import_and_export(self, tmp_path):
        keystore = Keystore.create(tmp_path / "wallet.json", "regtest")
        key = PrivateKey.generate()
        address = keystore.import_wif(key.to_wif(REGTEST.wif_version), "cold storage")
        assert str(address) == str(key.address(REGTEST.address_version))
        assert keystore.export_wif(str(address)) == key.to_wif(REGTEST.wif_version)
        with pytest.raises(WalletError, match="already in this wallet"):
            keystore.import_wif(key.to_wif(REGTEST.wif_version))

    def test_importing_a_key_from_another_network_is_refused(self, tmp_path):
        keystore = Keystore.create(tmp_path / "wallet.json", "regtest")
        with pytest.raises(WalletError):
            keystore.import_wif(PrivateKey.generate().to_wif(191))

    def test_exporting_an_unknown_address_is_refused(self, tmp_path):
        keystore = Keystore.create(tmp_path / "wallet.json", "regtest")
        with pytest.raises(WalletError, match="not in this wallet"):
            keystore.export_wif(str(PrivateKey.generate().address(REGTEST.address_version)))

    def test_labels(self, tmp_path):
        keystore = Keystore.create(tmp_path / "wallet.json", "regtest")
        address = keystore.default_address()
        keystore.set_label(address, "savings")
        keystore.save()
        assert Keystore.load(tmp_path / "wallet.json").addresses()[0].label == "savings"

    def test_a_missing_file_is_reported(self, tmp_path):
        with pytest.raises(WalletError, match="no wallet at"):
            Keystore.load(tmp_path / "nothing.json")

    def test_a_corrupt_file_is_reported(self, tmp_path):
        path = tmp_path / "wallet.json"
        path.write_text("{}")
        with pytest.raises(WalletError, match="not a version"):
            Keystore.load(path)

    def test_new_keys_are_appended(self, tmp_path):
        keystore = Keystore.create(tmp_path / "wallet.json", "regtest")
        second = keystore.new_key("second")
        keystore.save()
        assert str(second) in keystore.address_strings()
        assert len(Keystore.load(tmp_path / "wallet.json").addresses()) == 2

    def test_restoring_a_mnemonic_rebuilds_the_wallet(self, tmp_path):
        mnemonic = (
            "abandon abandon abandon abandon abandon abandon abandon abandon abandon"
            " abandon abandon about"
        )
        first = Keystore.create(tmp_path / "one.json", "regtest", mnemonic=mnemonic)
        second = Keystore.create(tmp_path / "two.json", "regtest", mnemonic=mnemonic)
        assert first.default_address() == second.default_address()
        first.new_key("extra")
        second.new_key("extra")
        assert first.addresses()[1].address == second.addresses()[1].address

    def test_invalid_mnemonic_is_refused(self, tmp_path):
        with pytest.raises(WalletError, match="checksum"):
            Keystore.create(tmp_path / "wallet.json", "regtest", mnemonic="abandon " * 12)

    def test_version_1_wallet_still_loads(self, tmp_path):
        key = PrivateKey.generate()
        document = {
            "version": 1,
            "network": "regtest",
            "encrypted": False,
            "addresses": [
                {
                    "address": str(key.address(REGTEST.address_version)),
                    "label": "legacy",
                    "created": 1,
                }
            ],
            "keys": [{"wif": key.to_wif(REGTEST.wif_version), "label": "legacy", "created": 1}],
        }
        path = tmp_path / "wallet.json"
        path.write_text(json.dumps(document))
        keystore = Keystore.load(path)
        assert keystore.default_address() == str(key.address(REGTEST.address_version))
        assert keystore.export_wif(keystore.default_address()) == key.to_wif(REGTEST.wif_version)
        keystore.save()
        assert json.loads(path.read_text())["version"] == 2
        reloaded = Keystore.load(path)
        assert reloaded.export_wif(reloaded.default_address()) == key.to_wif(REGTEST.wif_version)


class TestWallet:
    def test_balance_and_history(self, rpc, wallet):
        _, _, client = rpc
        address = wallet.keystore.default_address()
        client.call("generate", 4, address)

        balance = wallet.balance()
        assert balance.confirmed == REGTEST.subsidy(0) * 4
        # Regtest coinbases mature after two blocks, so only the newest is immature.
        assert balance.spendable == REGTEST.subsidy(0) * 3
        assert balance.immature == REGTEST.subsidy(0)
        history = wallet.history()
        assert len(history) == 4
        assert all(item["net"] > 0 for item in history)

    def test_sending_coins(self, rpc, wallet, other_key):
        _, _, client = rpc
        client.call("generate", 4, wallet.keystore.default_address())
        destination = str(other_key.address(REGTEST.address_version))

        result = wallet.send(destination, 10 * 10**8)
        assert result.txid in [item["txid"] for item in client.call("getmempool")["transactions"]]
        assert result.fee > 0
        assert result.change > 0

        client.call("generate", 1)  # mined by someone else
        assert client.getbalance(destination)["balance"] == 10 * 10**8
        assert wallet.balance().confirmed == REGTEST.subsidy(0) * 4 - 10 * 10**8 - result.fee

    def test_sending_everything(self, rpc, wallet, other_key):
        _, _, client = rpc
        client.call("generate", 4, wallet.keystore.default_address())
        destination = str(other_key.address(REGTEST.address_version))
        spendable = wallet.balance().spendable

        results = wallet.send_everything(destination)
        assert isinstance(results, list) and len(results) == 1
        result = results[0]
        assert result.change == 0
        client.call("generate", 1)
        assert client.getbalance(destination)["balance"] == spendable - result.fee

    def test_wallet_ignores_coins_spent_by_mempool_transactions(self, rpc, wallet, other_key):
        _, _, client = rpc
        client.call("generate", 4, wallet.keystore.default_address())
        destination = str(other_key.address(REGTEST.address_version))

        first = wallet.send(destination, 10 * 10**8)
        assert first.change > 0
        pooled_outpoint = first.transaction.inputs[0].prevout

        # The coin backing this send is pooled but unconfirmed; the wallet must
        # not offer it again or the node would refuse the next transaction as a
        # double spend.
        coins = wallet.coins(spendable_only=True)
        assert all(outpoint != pooled_outpoint for outpoint, _coin in coins)

        second = wallet.send(destination, 10 * 10**8)
        assert second.transaction.inputs[0].prevout != pooled_outpoint

    def test_sending_more_than_the_balance(self, rpc, wallet, other_key):
        _, _, client = rpc
        client.call("generate", 4, wallet.keystore.default_address())
        with pytest.raises(InsufficientFundsError):
            wallet.send(str(other_key.address(REGTEST.address_version)), 10**14)

    def test_sending_to_a_bad_address(self, rpc, wallet):
        _, _, client = rpc
        client.call("generate", 4, wallet.keystore.default_address())
        with pytest.raises(WalletError):
            wallet.send("not-an-address", 10**8)
        with pytest.raises(WalletError):
            wallet.send(str(PrivateKey.generate().address(63)), 10**8)  # mainnet address

    def test_a_locked_wallet_cannot_spend(self, tmp_path, rpc, other_key):
        _, _, client = rpc
        keystore = Keystore.create(tmp_path / "locked.json", "regtest", password="hunter2")
        client.call("generate", 4, keystore.default_address())
        locked = Wallet(Keystore.load(tmp_path / "locked.json"), client)
        assert locked.balance().confirmed > 0  # watching works
        with pytest.raises(WalletLocked):
            locked.send(str(other_key.address(REGTEST.address_version)), 10**8)

    def test_new_addresses_are_used_for_receiving(self, rpc, wallet):
        _, _, client = rpc
        second = wallet.new_address("second")
        client.call("generate", 3, second)
        rows = {address: value for address, _, value in wallet.balances_by_address()}
        assert rows[second] == REGTEST.subsidy(0) * 3
        assert rows[wallet.keystore.default_address()] == 0

    def test_a_missing_node_is_reported(self, tmp_path):
        from scarletcoin.net.client import RpcClient

        keystore = Keystore.create(tmp_path / "wallet.json", "regtest")
        offline = Wallet(keystore, RpcClient("http://127.0.0.1:1", timeout=1))
        with pytest.raises(RpcClientError):
            offline.balance()
        assert "error" in offline.node_info()


class TestSolver:
    def test_scan_finds_an_easy_nonce(self):
        header = REGTEST.genesis_block.header.serialize()
        result = scan_nonces(header, 2**256 - 1, start=0, count=1)
        assert result.found
        assert result.nonce == 0
        assert result.hashes == 1
        assert result.hash_rate > 0

    def test_scan_reports_an_exhausted_range(self):
        header = REGTEST.genesis_block.header.serialize()
        result = scan_nonces(header, 0, start=0, count=32)
        assert not result.found
        assert result.hashes == 32

    def test_scan_validates_its_arguments(self):
        with pytest.raises(ValueError, match="80 bytes"):
            scan_nonces(b"short", 1)
        with pytest.raises(ValueError, match="negative"):
            scan_nonces(REGTEST.genesis_block.header.serialize(), 1, start=-1)

    def test_solve_block_produces_a_valid_block(self, chain, key):
        from scarletcoin.core.template import create_block_template

        template = create_block_template(chain)
        candidate = template.build_block(pubkey_hash=key.public_key().hash160(), nonce=0)
        solved = solve_block(candidate)
        assert solved is not None
        solved.check_sanity(pow_limit=REGTEST.pow_limit, max_block_size=REGTEST.max_block_size)
        assert solved.transactions == candidate.transactions

    def test_solve_block_can_be_stopped(self, chain, key):
        from scarletcoin.core.template import create_block_template

        template = create_block_template(chain)
        candidate = template.build_block(pubkey_hash=key.public_key().hash160())
        assert solve_block(candidate, should_stop=lambda: True) is None


class TestMiner:
    def test_mining_blocks(self, rpc, key):
        _, _, client = rpc
        address = str(key.address(REGTEST.address_version))
        miner = Miner(client, address, workers=1, refresh_seconds=5)
        stats = miner.run(max_blocks=3)
        assert stats.blocks_accepted == 3
        assert stats.blocks_rejected == 0
        assert stats.hashes > 0
        assert client.getblockcount() == 3
        assert client.getbalance(address)["balance"] == REGTEST.subsidy(0) * 3
        assert "hash_rate" in stats.to_dict()

    def test_mining_includes_mempool_transactions(self, rpc, wallet, key, other_key):
        _, _, client = rpc
        client.call("generate", 4, wallet.keystore.default_address())
        result = wallet.send(str(other_key.address(REGTEST.address_version)), 10**8)

        miner = Miner(client, str(key.address(REGTEST.address_version)), workers=1)
        miner.run(max_blocks=1)
        transaction = client.call("gettransaction", result.txid)
        assert transaction["confirmations"] == 1
        # the miner collected the fee on top of the subsidy
        block = client.call("getblock", client.getblockcount())
        reward = sum(output["value"] for output in block["transactions"][0]["outputs"])
        assert reward == REGTEST.subsidy(block["height"]) + result.fee

    def test_a_bad_payout_address_is_refused(self, rpc):
        _, _, client = rpc
        miner = Miner(client, "not-an-address", workers=1)
        with pytest.raises(MiningError, match="not a valid regtest address"):
            miner.run(max_blocks=1)

    def test_mining_with_several_workers(self, rpc, key):
        _, _, client = rpc
        miner = Miner(client, str(key.address(REGTEST.address_version)), workers=2)
        stats = miner.run(max_blocks=1)
        assert stats.blocks_accepted == 1
        assert client.getblockcount() == 1

    def test_a_zero_or_negative_rate_cap_is_ignored(self, rpc, key):
        from scarletcoin.miner.miner import Miner

        _, _, client = rpc
        address = str(key.address(REGTEST.address_version))
        assert Miner(client, address, workers=1, max_rate=0).max_rate is None
        assert Miner(client, address, workers=1, max_rate=-5).max_rate is None
        assert Miner(client, address, workers=1, max_rate=500).max_rate == 500
        # and a capped miner still mines fine
        stats = Miner(client, address, workers=1, max_rate=500).run(max_blocks=1)
        assert stats.blocks_accepted == 1

    def test_a_rate_cap_actually_slows_the_loop(self, rpc, key, monkeypatch):
        import time

        import scarletcoin.miner.miner as module
        from scarletcoin.miner.solver import ScanResult

        _, _, client = rpc
        address = str(key.address(REGTEST.address_version))

        def quick_scan(header, target, *, start=0, count=1 << 20):
            if start >= 2 * 1 << 16:  # a solution on the third call
                return ScanResult(start, count, 0.001)
            return ScanResult(None, count, 0.001)

        monkeypatch.setattr(module, "scan_nonces", quick_scan)
        monkeypatch.setattr(module.Miner, "_tune_chunk", lambda self, seconds: None)
        miner = module.Miner(client, address, workers=1, max_rate=500, refresh_seconds=60)
        miner._chunk = 1 << 16
        started = time.time()
        miner.run(max_blocks=1)
        elapsed = time.time() - started
        # 3 x 65536 hashes at a 500 H/s cap means ~0.39 s of idle time.
        assert elapsed >= 0.3

    def test_an_unreachable_node_is_reported(self, key):
        from scarletcoin.net.client import RpcClient

        miner = Miner(
            RpcClient("http://127.0.0.1:1", timeout=1),
            str(key.address(REGTEST.address_version)),
            workers=1,
        )
        events: list[str] = []

        def on_event(kind: str, payload: dict) -> None:
            events.append(kind)
            miner.stop()  # give up after the first failure

        miner.on_event = on_event
        miner.run()
        assert events == ["error"]


class TestWalletCli:
    def _run(self, arguments: list[str], server_url: str, wallet_path, *extra: str) -> int:
        from scarletcoin.wallet.cli import main

        return main(
            [
                "--wallet",
                str(wallet_path),
                "--network",
                "regtest",
                "--rpc-url",
                server_url,
                "--rpc-token",
                "test-token",
                *arguments,
                *extra,
            ]
        )

    def test_create_balance_send_and_history(self, rpc, tmp_path, capsys, other_key):
        _, server, client = rpc
        path = tmp_path / "cli-wallet.json"

        assert self._run(["create", "--no-password"], server.url, path) == 0
        output = capsys.readouterr().out
        assert "created" in output
        address = Keystore.load(path).default_address()

        client.call("generate", 4, address)

        assert self._run(["balance"], server.url, path) == 0
        assert format_amount(REGTEST.subsidy(0) * 3) in capsys.readouterr().out

        assert self._run(["addresses"], server.url, path) == 0
        assert address in capsys.readouterr().out

        assert self._run(["info"], server.url, path) == 0
        info = capsys.readouterr().out
        assert "encrypted  no" in info
        assert "height 4" in info

        destination = str(other_key.address(REGTEST.address_version))
        assert self._run(["send", destination, "5", "--yes"], server.url, path) == 0
        sent = capsys.readouterr().out
        assert "broadcast" in sent

        client.call("generate", 1)
        assert self._run(["history", "--limit", "10"], server.url, path) == 0
        history = capsys.readouterr().out
        assert "height" in history

        assert self._run(["unspent"], server.url, path) == 0
        assert "coinbase" in capsys.readouterr().out

    def test_dry_run_does_not_broadcast(self, rpc, tmp_path, capsys, other_key):
        _, server, client = rpc
        path = tmp_path / "cli-wallet.json"
        self._run(["create", "--no-password"], server.url, path)
        capsys.readouterr()
        client.call("generate", 4, Keystore.load(path).default_address())

        destination = str(other_key.address(REGTEST.address_version))
        assert self._run(["send", destination, "1", "--dry-run"], server.url, path) == 0
        assert "dry run" in capsys.readouterr().out
        assert client.call("getmempool")["count"] == 0

    def test_new_address_and_labels(self, rpc, tmp_path, capsys):
        _, server, _ = rpc
        path = tmp_path / "cli-wallet.json"
        self._run(["create", "--no-password"], server.url, path)
        capsys.readouterr()

        assert self._run(["new", "savings"], server.url, path) == 0
        address = capsys.readouterr().out.strip()
        assert Address.is_valid(address, expected_version=REGTEST.address_version)

        assert self._run(["label", address, "renamed"], server.url, path) == 0
        assert "renamed" in capsys.readouterr().out

        assert self._run(["export", address], server.url, path) == 0
        exported = capsys.readouterr().out
        assert "private key" in exported

    def test_import_a_key(self, rpc, tmp_path, capsys):
        _, server, _ = rpc
        path = tmp_path / "cli-wallet.json"
        self._run(["create", "--no-password"], server.url, path)
        capsys.readouterr()
        key = PrivateKey.generate()
        assert self._run(["import", key.to_wif(REGTEST.wif_version)], server.url, path) == 0
        assert str(key.address(REGTEST.address_version)) in capsys.readouterr().out

    def test_errors_exit_with_a_message(self, rpc, tmp_path, capsys):
        _, server, _ = rpc
        path = tmp_path / "missing.json"
        with pytest.raises(SystemExit):
            self._run(["balance"], server.url, path)
        assert "no wallet at" in capsys.readouterr().err

    def test_a_wallet_from_another_network_is_refused(self, rpc, tmp_path, capsys):
        _, server, _ = rpc
        path = tmp_path / "mainnet.json"
        Keystore.create(path, "mainnet")
        with pytest.raises(SystemExit):
            self._run(["balance"], server.url, path)
        assert "mainnet" in capsys.readouterr().err


class TestNodeCli:
    def test_rpc_subcommand(self, rpc, capsys, tmp_path):
        from scarletcoin.net.cli import main

        _, server, client = rpc
        client.call("generate", 2)
        code = main(
            [
                "rpc",
                "--network",
                "regtest",
                "--datadir",
                str(tmp_path),
                "--rpc-url",
                server.url,
                "--rpc-token",
                "test-token",
                "getblockcount",
            ]
        )
        assert code == 0
        assert capsys.readouterr().out.strip() == "2"

    def test_info_subcommand(self, rpc, capsys, tmp_path):
        from scarletcoin.net.cli import main

        _, server, _ = rpc
        code = main(
            [
                "info",
                "--network",
                "regtest",
                "--datadir",
                str(tmp_path),
                "--rpc-url",
                server.url,
                "--rpc-token",
                "test-token",
            ]
        )
        assert code == 0
        assert "network" in capsys.readouterr().out

    def test_rpc_parameters_are_parsed_as_json(self, rpc, capsys, tmp_path):
        from scarletcoin.net.cli import main

        _, server, client = rpc
        client.call("generate", 3)
        main(
            [
                "rpc",
                "--network",
                "regtest",
                "--datadir",
                str(tmp_path),
                "--rpc-url",
                server.url,
                "--rpc-token",
                "test-token",
                "getblockheader",
                "2",
            ]
        )
        assert json.loads(capsys.readouterr().out)["height"] == 2

    def test_an_unreachable_node_exits_with_an_error(self, capsys, tmp_path):
        from scarletcoin.net.cli import main

        with pytest.raises(SystemExit):
            main(
                [
                    "info",
                    "--network",
                    "regtest",
                    "--datadir",
                    str(tmp_path),
                    "--rpc-url",
                    "http://127.0.0.1:1",
                    "--timeout",
                    "1",
                ]
            )
        assert "cannot reach the node" in capsys.readouterr().err


class TestMinerCli:
    def test_mining_from_the_command_line(self, rpc, capsys, tmp_path, key):
        from scarletcoin.miner.cli import main

        _, server, client = rpc
        address = str(key.address(REGTEST.address_version))
        code = main(
            [
                address,
                "--network",
                "regtest",
                "--datadir",
                str(tmp_path),
                "--rpc-url",
                server.url,
                "--rpc-token",
                "test-token",
                "--workers",
                "1",
                "--blocks",
                "2",
                "--quiet",
            ]
        )
        assert code == 0
        assert client.getblockcount() == 2
        assert "blocks accepted" in capsys.readouterr().out

    def test_a_node_that_is_not_running(self, capsys, tmp_path, key):
        from scarletcoin.miner.cli import main

        with pytest.raises(SystemExit):
            main(
                [
                    str(key.address(REGTEST.address_version)),
                    "--network",
                    "regtest",
                    "--datadir",
                    str(tmp_path),
                    "--rpc-url",
                    "http://127.0.0.1:1",
                    "--timeout",
                    "1",
                ]
            )
        assert "cannot reach the node" in capsys.readouterr().err


class TestBlockTemplate:
    def test_round_trip_through_json(self, rpc, wallet, other_key):
        from scarletcoin.core.template import BlockTemplate

        _, _, client = rpc
        client.call("generate", 4, wallet.keystore.default_address())
        wallet.send(str(other_key.address(REGTEST.address_version)), 10**8)
        data = client.getblocktemplate()
        template = BlockTemplate.from_dict(data)
        assert template.to_dict() == {key: data[key] for key in template.to_dict()}
        assert len(template.transactions) == 1
        assert template.coinbase_value > REGTEST.subsidy(template.height)

    def test_a_template_never_produces_a_stale_timestamp(self, chain, key):
        from scarletcoin.core.template import create_block_template

        template = create_block_template(chain, timestamp=0)
        block = template.build_block(pubkey_hash=key.public_key().hash160(), timestamp=0)
        assert block.header.timestamp > template.min_time
        assert mine_block(chain, key).header.timestamp > chain.tip.timestamp - 1
