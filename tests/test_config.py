import os
import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from heirdrop import config
from tests.helpers import owned_by_non_root

EXAMPLE = Path(__file__).resolve().parent.parent / "doc" / "init" / "heirdrop.example.toml"


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "heirdrop.toml"
        shutil.copy(EXAMPLE, self.path)
        os.chmod(self.path, 0o600)

    def tearDown(self):
        self.tmp.cleanup()

    def test_example_loads_with_defaults(self):
        cfg = config.load(self.path)
        self.assertEqual(cfg.timing.owner_period, timedelta(days=74))
        self.assertEqual(cfg.timing.wife_window, timedelta(days=30))
        self.assertEqual(cfg.timing.cold_start_hold, timedelta(hours=24))
        self.assertEqual(cfg.timing.tick_interval, timedelta(seconds=60))
        self.assertEqual(cfg.timing.confirmations_final, 6)
        self.assertTrue(cfg.dry_run)
        self.assertEqual(set(cfg.transactions), {"W1", "W2", "F1", "F2"})
        self.assertEqual(cfg.rpc_cookie_file, Path("/var/lib/bitcoind/.cookie"))
        self.assertEqual(cfg.documents["wife"], Path("/var/lib/heirdrop-docs/wife"))

    def test_group_readable_refused(self):
        os.chmod(self.path, 0o640)
        owned_by_non_root(self.path)
        with self.assertRaisesRegex(config.ConfigError, "readable by group"):
            config.load(self.path)

    def test_permission_rules(self):
        ok, bad = self.assertIsNone, self.assertIsNotNone
        for mode in (0o600, 0o400):
            ok(config.permission_problem(mode, 1000))
            ok(config.permission_problem(mode, 0))
        for mode in (0o640, 0o440):              # group-readable: only when root owns it
            ok(config.permission_problem(mode, 0))
            bad(config.permission_problem(mode, 1000))
        for mode in (0o660, 0o620, 0o644, 0o604, 0o602, 0o641, 0o666):
            bad(config.permission_problem(mode, 0), oct(mode))
            bad(config.permission_problem(mode, 1000), oct(mode))

    def test_fractional_days_allowed(self):
        text = self.path.read_text().replace("owner_period_days = 74", "owner_period_days = 0.01")
        text = text.replace("warning_window_days = 14", "warning_window_days = 0.002")
        self.path.write_text(text)
        self.assertEqual(config.load(self.path).timing.owner_period, timedelta(days=0.01))

    def test_missing_section(self):
        self.path.write_text("[timing]\n")
        with self.assertRaisesRegex(config.ConfigError, r"missing \[bitcoind\]"):
            config.load(self.path)

    def test_bad_sha(self):
        text = self.path.read_text().replace("0" * 64, "zz", 1)
        self.path.write_text(text)
        with self.assertRaisesRegex(config.ConfigError, "sha256"):
            config.load(self.path)

    def test_both_auth_methods_refused(self):
        text = self.path.read_text().replace('# rpc_user = "heirdrop"', 'rpc_user = "heirdrop"')
        text = text.replace('# rpc_password = "secret"', 'rpc_password = "secret"')
        self.path.write_text(text)
        with self.assertRaisesRegex(config.ConfigError, "either cookie_file"):
            config.load(self.path)

    def test_same_addresses_refused(self):
        text = self.path.read_text().replace("bc1qfamilyhardwarewalletaddress", "bc1qwifehardwarewalletaddress")
        self.path.write_text(text)
        with self.assertRaisesRegex(config.ConfigError, "must differ"):
            config.load(self.path)

    def test_negative_duration_refused(self):
        text = self.path.read_text().replace("wife_window_days = 30", "wife_window_days = -1")
        self.path.write_text(text)
        with self.assertRaisesRegex(config.ConfigError, "negative"):
            config.load(self.path)

    def edited(self, old, new):
        text = self.path.read_text()
        self.assertIn(old, text)
        self.path.write_text(text.replace(old, new, 1))

    def test_unknown_keys_and_sections_refused(self):
        original = self.path.read_text()
        telegram = ('[telegram]\nbot_token_file = "/x"\nowner_chat_id = 1\nwife_chat_id = 2\n')
        cases = [
            ("dry_run = true", "dryrun = false", r"unknown key\(s\) in \[safety\]: dryrun"),
            ("owner_grace_days = 16", "owner_grace_day = 30", r"in \[timing\]: owner_grace_day"),
            ("max_fee_rate = 0", "max_feerate = 0", r"in \[safety\]: max_feerate"),
            ('rpc_url = "', 'rpcurl = "', r"in \[bitcoind\]: rpcurl"),
            ('whw = "', 'whw_address = "', r"in \[addresses\]: whw_address"),
            ('socket = "', 'sockets = "', r"in \[paths\]: sockets"),
            ('owner_user = "', 'owner = "', r"in \[interface\]: owner"),
            ('W1 = { file', 'W3 = { file', r"in \[transactions\]: W3"),
            ('W1 = { file', 'W1 = { amount = 1, file', r"in \[transactions\] W1: amount"),
            ("[timing]", "[telegran]\n[timing]", r"unknown section\(s\): telegran"),
            ("[timing]", telegram + "chat_id = 3\n[timing]", r"in \[telegram\]: chat_id"),
        ]
        for old, new, error in cases:
            with self.subTest(new=new):
                self.path.write_text(original)
                self.edited(old, new)
                with self.assertRaisesRegex(config.ConfigError, error):
                    config.load(self.path)

    def test_numbers_must_be_finite_and_in_range(self):
        original = self.path.read_text()
        cases = [
            ("owner_period_days = 74", "owner_period_days = inf", "finite"),
            ("wife_window_days = 30", "wife_window_days = nan", "finite"),
            ("tick_interval_seconds = 60", "tick_interval_seconds = -inf", "finite"),
            ("max_fee_rate = 0", "max_fee_rate = nan", "finite"),
            ("owner_period_days = 74", "owner_period_days = 1e300", "too large"),
            ("max_document_bytes = 10000000", "max_document_bytes = -1", "must not be negative"),
        ]
        for old, new, error in cases:
            with self.subTest(new=new):
                self.path.write_text(original)
                self.edited(old, new)
                with self.assertRaisesRegex(config.ConfigError, error):
                    config.load(self.path)

    def test_urls_need_a_host_and_a_valid_port(self):
        original = self.path.read_text()
        cases = [
            ('"http://127.0.0.1:8332"', '"https://"', "no host name"),
            ('"http://127.0.0.1:8332"', '"http://:8332"', "no host name"),
            ('"http://127.0.0.1:8332"', '"http://127.0.0.1:port"', "bad port"),
            ('"http://127.0.0.1:8332"', '"http://127.0.0.1:99999"', "bad port"),
            ('"http://127.0.0.1:8332"', '"ftp://127.0.0.1"', "must start with"),
        ]
        for old, new, error in cases:
            with self.subTest(new=new):
                self.path.write_text(original)
                self.edited(old, new)
                with self.assertRaisesRegex(config.ConfigError, error):
                    config.load(self.path)
        self.path.write_text(original)
        self.edited('"http://127.0.0.1:8332"', '"http://[::1]:18443"')
        self.assertEqual(config.load(self.path).rpc_url, "http://[::1]:18443")

    def test_telegram_api_url_needs_a_host(self):
        data = config.tomllib.loads(self.path.read_text())
        data["telegram"] = {"bot_token_file": "/x", "owner_chat_id": 1, "wife_chat_id": 2,
                            "api_url": "https://"}
        with self.assertRaisesRegex(config.ConfigError, r"\[telegram\] api_url has no host name"):
            config.parse(data)
        data["telegram"]["api_url"] = "http://127.0.0.1:8081"
        self.assertEqual(config.parse(data).telegram.api_url, "http://127.0.0.1:8081")


if __name__ == "__main__":
    unittest.main()
