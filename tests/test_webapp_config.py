import ast
import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path

from tests.helpers import owned_by_non_root
from webapp import cli, config, passwords

ROOT = Path(__file__).resolve().parent.parent
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
KEY = "ab" * 32
# One hash for all tests: scrypt is deliberately slow.
PASSWORD = "7305918264017352"
HASH = passwords.hash_password(PASSWORD)
HASH2 = passwords.hash_password("9081726354453627")


class PasswordTests(unittest.TestCase):
    def test_new_passwords_are_16_digits(self):
        seen = {passwords.new_password() for _ in range(50)}
        self.assertEqual(len(seen), 50)
        for p in seen:
            self.assertRegex(p, r"^\d{16}$")
        self.assertEqual(passwords.grouped("1234567890123456"), "1234 5678 9012 3456")

    def test_verify_accepts_spaces_and_dashes(self):
        self.assertTrue(passwords.verify(PASSWORD, HASH))
        self.assertTrue(passwords.verify("7305 9182 6401 7352", HASH))
        self.assertTrue(passwords.verify(" 7305-9182-6401-7352\n", HASH))

    def test_verify_refuses(self):
        for typed in ("7305918264017353", "730591826401735", "73059182640173520", "", "abcd918264017352",
                      "７３０５918264017352"):  # full-width digits are not ASCII
            self.assertFalse(passwords.verify(typed, HASH), typed)
        self.assertFalse(passwords.verify(PASSWORD, "scrypt$nonsense"))
        self.assertFalse(passwords.verify(PASSWORD, HASH.replace("$32768$", "$1000$")))

    def test_salted(self):
        self.assertNotEqual(passwords.hash_password(PASSWORD), HASH)
        self.assertIsNone(passwords.check_hash_format(HASH))

    def test_hash_format_problems(self):
        n, r, p, salt, digest = HASH.split("$")[1:]
        for bad in ("plain", "scrypt$1$2$3", f"scrypt$1024${r}${p}${salt}${digest}",
                    f"scrypt${n}${r}${p}${salt[:8]}${digest}", f"scrypt${n}${r}${p}${salt}$AAAA"):
            self.assertIsNotNone(passwords.check_hash_format(bad), bad)


class ConfigCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.picture = self.dir / "picture.jpg"
        self.picture.write_bytes(JPEG)
        self.key = self.dir / "session.key"
        self.key.write_text(KEY + "\n")
        self.state = self.dir / "state"
        self.state.mkdir()
        for f in (self.picture, self.key):
            os.chmod(f, 0o600)
            owned_by_non_root(f)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir)

    def text(self, web_extra="", accounts=None):
        accounts = accounts if accounts is not None else (
            f'[accounts.wife]\npassword = "{HASH}"\n\n[accounts.family]\npassword = "{HASH2}"\n')
        return (f'[web]\nstate_dir = "{self.state}"\nsession_key_file = "{self.key}"\n'
                f'picture = "{self.picture}"\nsocket = "{self.dir / "none.sock"}"\n{web_extra}\n\n{accounts}')

    def write(self, text, mode=0o600):
        path = self.dir / "web.toml"
        path.write_text(text)
        os.chmod(path, mode)
        owned_by_non_root(path)
        return path

    def load(self, web_extra="", accounts=None):
        return config.load(self.write(self.text(web_extra, accounts)))

    def refused(self, web_extra="", accounts=None, match=""):
        with self.assertRaises(config.ConfigError) as cm:
            self.load(web_extra, accounts)
        self.assertIn(match, str(cm.exception))
        return str(cm.exception)


class ConfigTests(ConfigCase):
    def test_defaults(self):
        cfg = self.load()
        self.assertEqual((cfg.host, cfg.port, cfg.session_hours, cfg.title, cfg.contact),
                         ("127.0.0.1", 8090, 8, "Family", ""))
        self.assertEqual(cfg.login_area, (70.0, 75.0, 20.0, 15.0))
        self.assertEqual(cfg.picture_type, "image/jpeg")
        self.assertEqual(cfg.session_key, bytes.fromhex(KEY))
        self.assertEqual(set(cfg.password_hashes), {"wife", "family"})
        self.assertNotIn(HASH, repr(cfg))
        self.assertNotIn(KEY, repr(cfg))

    def test_example_config_parses_apart_from_placeholders(self):
        text = (ROOT / "doc" / "init" / "heirdrop-web.example.toml").read_text().replace("scrypt$...", HASH, 1).replace("scrypt$...", HASH2)
        text = text.replace("/etc/heirdrop-web/picture.jpg", str(self.picture))
        import tomllib
        cfg = config.parse(tomllib.loads(text))
        self.assertEqual((cfg.port, cfg.title), (8090, "Family"))

    def test_values(self):
        cfg = self.load('listen = "[::1]:9000"\nsession_hours = 2\ntitle = " Garden "\n'
                        'contact = "Call Anna"\nlogin_area = [0, 0, 100, 100]')
        self.assertEqual((cfg.host, cfg.port, cfg.session_hours, cfg.title, cfg.contact, cfg.login_area),
                         ("::1", 9000, 2, "Garden", "Call Anna", (0.0, 0.0, 100.0, 100.0)))

    def test_listen_must_be_loopback(self):
        for value in ("0.0.0.0:8090", "192.168.1.5:8090", "example.org:80", "127.0.0.1", "127.0.0.1:0"):
            self.refused(f'listen = "{value}"', match="listen")

    def test_bad_values(self):
        self.refused("session_hours = 0", match="session_hours")
        self.refused("session_hours = true", match="session_hours")
        self.refused('session_hours = "8"', match="session_hours")
        self.refused('title = ""', match="title")
        self.refused(f'title = "{"x" * 61}"', match="title")
        self.refused(f'contact = "{"x" * 301}"', match="contact")
        for area in ("[1, 2, 3]", "[90, 0, 20, 10]", "[0, 0, 0, 10]", "[-1, 0, 10, 10]", '["a", 0, 1, 1]',
                     "[true, 0, 1, 1]"):
            self.refused(f"login_area = {area}", match="login_area")

    def test_unknown_keys_are_refused(self):
        self.refused('sesion_hours = 8', match="sesion_hours")
        self.refused(accounts=f'[accounts.wife]\npassword = "{HASH}"\n[accounts.family]\npassword = "{HASH}"\n'
                     f'[accounts.owner]\npassword = "{HASH}"\n', match="owner")
        self.refused(accounts=f'[accounts.wife]\npassword = "{HASH}"\npasword = "x"\n'
                     f'[accounts.family]\npassword = "{HASH}"\n', match="pasword")

    def test_accounts(self):
        self.refused(accounts=f'[accounts.wife]\npassword = "{HASH}"\n', match="accounts.family")
        self.refused(accounts=f'[accounts.wife]\npassword = "1234567890123456"\n'
                     f'[accounts.family]\npassword = "{HASH}"\n', match="accounts.wife")
        self.refused(accounts=f'[accounts.wife]\npassword = 5\n'
                     f'[accounts.family]\npassword = "{HASH}"\n', match="accounts.wife")
        self.refused(accounts="", match="accounts")

    def test_picture(self):
        png = self.dir / "p.png"
        png.write_bytes(PNG)
        os.chmod(png, 0o600)
        owned_by_non_root(png)
        self.assertEqual(config.picture_problem(png), ("image/png", None))
        self.picture.write_bytes(PNG)            # .jpg holding a PNG
        self.refused(match="does not look like")
        self.picture.write_bytes(b"\xff\xd8\xff" + b"\x00" * config.MAX_PICTURE_BYTES)
        self.refused(match="at most")
        self.picture.unlink()
        self.refused(match="not found")
        gif = self.dir / "p.gif"
        self.assertIn(".webp", config.picture_problem(gif)[1])

    def test_session_key(self):
        self.key.write_text("abc\n")
        self.refused(match="64 hex digits")
        self.key.write_text("zz" * 32)
        self.refused(match="64 hex digits")
        self.key.unlink()
        self.refused(match="new-key")

    def test_permissions(self):
        path = self.write(self.text(), mode=0o644)
        with self.assertRaises(config.ConfigError) as cm:
            config.load(path)
        self.assertIn("readable by group or others", str(cm.exception))
        config.load(path, check_perms=False)
        os.chmod(path, 0o600)
        for f in (self.key, self.picture):
            os.chmod(f, 0o640)
            with self.assertRaises(config.ConfigError):
                config.load(path)
            os.chmod(f, 0o600)
        config.load(path)

    def test_permission_rule(self):
        self.assertIsNone(config.permission_problem(0o100640, 0))
        self.assertIsNone(config.permission_problem(0o100600, 1000))
        self.assertIsNotNone(config.permission_problem(0o100644, 0))
        self.assertIsNotNone(config.permission_problem(0o100660, 0))
        self.assertIsNotNone(config.permission_problem(0o100640, 1000))

    def test_bad_toml(self):
        path = self.write("[web\n")
        with self.assertRaises(config.ConfigError):
            config.load(path)


class CliTests(ConfigCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def new_password(self, *argv, typed=()):
        answers = iter(typed)
        args = cli.build_parser().parse_args(["new-password", *argv])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.cmd_new_password(args, prompt=lambda _: next(answers))
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def stored(out):
        return [ln for ln in out.splitlines() if ln.startswith('password = "')][0].split('"')[1]

    def test_new_password(self):
        code, out, _ = self.new_password("wife", "--config", str(self.dir / "none.toml"))
        self.assertEqual(code, 0)
        shown = out.split("password: ", 1)[1].splitlines()[0]
        self.assertRegex(shown, r"^\d{4} \d{4} \d{4} \d{4}$")
        self.assertIn("[accounts.wife]", out)
        self.assertIn("no other account", out)
        self.assertTrue(passwords.verify(shown, self.stored(out)))

    def test_chosen_password(self):
        chosen = "2468 1357 9024 6813"
        code, out, err = self.new_password("family", "--choose", "--config", str(self.dir / "none.toml"),
                                           typed=(chosen, "2468-1357-9024-6813"))
        self.assertEqual(code, 0, err)
        self.assertNotIn("2468", out)                       # never echoed
        self.assertIn("[accounts.family]", out)
        self.assertTrue(passwords.verify(chosen, self.stored(out)))

    def test_chosen_password_refused(self):
        none = str(self.dir / "none.toml")
        for typed, why in (((("1234", "1234")), "exactly 16 digits"),
                           (("1111 2222 1111 2222", "x"), "different digits"),
                           (("0123 4567 8901 2345", "x"), "sequence"),
                           (("9876 5432 1098 7654", "x"), "sequence"),
                           (("2468 1357 9024 6813", "2468 1357 9024 6814"), "differ")):
            code, out, err = self.new_password("wife", "--choose", "--config", none, typed=typed)
            self.assertEqual(code, 1, typed)
            self.assertIn(why, err)
            self.assertNotIn("password =", out)

    def test_accounts_need_different_passwords(self):
        path = self.write(self.text(accounts=f'[accounts.family]\npassword = "{HASH}"\n'))
        code, out, err = self.new_password("wife", "--choose", "--config", str(path),
                                           typed=(PASSWORD.replace("", " ").strip(), PASSWORD))
        self.assertEqual(code, 1)
        self.assertIn("other account", err)
        self.assertNotIn("password =", out)
        code, out, err = self.new_password("wife", "--choose", "--config", str(path),
                                           typed=("2468 1357 9024 6813",) * 2)
        self.assertEqual(code, 0, err)
        self.assertNotIn("no other account", out)
        # the same hash line pasted twice is refused by the config
        self.refused(accounts=f'[accounts.wife]\npassword = "{HASH}"\n[accounts.family]\npassword = "{HASH}"\n',
                     match="same password")

    def test_unreadable_config_is_not_skipped(self):
        path = self.write("[accounts\n")
        code, out, err = self.new_password("wife", "--config", str(path))
        self.assertEqual(code, 1)
        self.assertIn("cannot read", err)
        self.assertNotIn("password", out)
        path.write_text("accounts = 5\n")
        self.assertEqual(self.new_password("wife", "--config", str(path))[0], 0)  # no other account in it

    def test_new_key(self):
        path = self.dir / "new.key"
        code, out, _ = self.run_cli("new-key", str(path))
        self.assertEqual(code, 0)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(len(config.read_key(path)), 32)
        self.assertNotIn(path.read_text().strip(), out)
        before = path.read_text()
        code, _, err = self.run_cli("new-key", str(path))
        self.assertEqual(code, 1)
        self.assertIn("exists", err)
        self.assertEqual(path.read_text(), before)

    def test_validate_config(self):
        path = self.write(self.text())
        code, out, _ = self.run_cli("validate-config", "--config", str(path))
        self.assertEqual(code, 0, out)
        self.assertIn("config ok", out)
        self.assertIn("not present", out)                # no service socket: a warning only
        for secret in (HASH, KEY, HASH.split("$")[4]):
            self.assertNotIn(secret, out)
        self.state.rmdir()
        code, out, _ = self.run_cli("validate-config", "--config", str(path))
        self.assertEqual(code, 0)
        self.assertIn("missing: systemd creates it", out)
        os.chmod(path, 0o644)
        code, _, err = self.run_cli("validate-config", "--config", str(path))
        self.assertEqual(code, 1)
        self.assertIn("config error", err)


class BoundaryTests(unittest.TestCase):
    def test_webapp_imports_nothing_from_heirdrop(self):
        for path in sorted((ROOT / "webapp").glob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]
                for name in names:
                    self.assertFalse(name == "heirdrop" or name.startswith("heirdrop."), f"{path.name}: {name}")


if __name__ == "__main__":
    unittest.main()
