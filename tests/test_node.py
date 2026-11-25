import base64
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from heirdrop.node import NodeUnreachable, RpcClient, RpcError

# One connection, one canned answer: argv[1] is the status, argv[2] the body.
# The request is printed after the port, so the test can check what was sent.
SERVER = r"""
import json, socket, sys
status, body = int(sys.argv[1]), sys.argv[2].encode()
s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1)
print(s.getsockname()[1], flush=True)
c, _ = s.accept()
data = b""
while b"\r\n\r\n" not in data:
    data += c.recv(65536)
head, rest = data.split(b"\r\n\r\n", 1)
length = int([l.split(b":")[1] for l in head.split(b"\r\n") if l.lower().startswith(b"content-length")][0])
while len(rest) < length:
    rest += c.recv(65536)
auth = [l.split(b" ", 2)[2] for l in head.split(b"\r\n") if l.lower().startswith(b"authorization")][0]
c.sendall(b"HTTP/1.1 %d X\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % (status, len(body)) + body)
c.close()
print(json.dumps({"auth": auth.decode(), "body": json.loads(rest)}), flush=True)
"""


class RpcClientTests(unittest.TestCase):
    """RpcClient against a real socket in a child process (no threads in tests)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cookie = Path(self.tmp.name) / ".cookie"
        self.cookie.write_text("__cookie__:secret\n")

    def client(self, status, answer):
        body = answer if isinstance(answer, str) else json.dumps(answer)
        p = subprocess.Popen([sys.executable, "-c", SERVER, str(status), body], stdout=subprocess.PIPE, text=True)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        self.child = p
        port = int(p.stdout.readline())
        return RpcClient(f"http://127.0.0.1:{port}/", cookie_file=self.cookie, timeout=5)

    def seen(self):
        return json.loads(self.child.stdout.readline())

    def test_result_and_request(self):
        rpc = self.client(200, {"jsonrpc": "2.0", "id": "heirdrop", "result": {"blocks": 5}})
        self.assertEqual(rpc.call("getblockchaininfo"), {"blocks": 5})
        seen = self.seen()
        self.assertEqual(base64.b64decode(seen["auth"]).decode(), "__cookie__:secret")
        self.assertEqual((seen["body"]["method"], seen["body"]["params"]), ("getblockchaininfo", []))

    def test_null_result_is_none(self):
        rpc = self.client(200, {"jsonrpc": "2.0", "id": "heirdrop", "result": None})
        self.assertIsNone(rpc.call("gettxout", "ab" * 32, 0, True))

    def test_rpc_error_with_any_status(self):
        for status in (200, 500):                    # JSON-RPC 2.0 and the older 1.0 style
            with self.subTest(status=status):
                rpc = self.client(status, {"result": None, "error": {"code": -5, "message": "not found"}})
                with self.assertRaises(RpcError) as cm:
                    rpc.call("getmempoolentry", "ab" * 32)
                self.assertEqual((cm.exception.code, cm.exception.message), (-5, "not found"))

    def test_error_status_without_rpc_error(self):
        for answer in ({}, {"result": None}):
            with self.subTest(answer=answer):
                rpc = self.client(503, answer)
                with self.assertRaisesRegex(NodeUnreachable, "HTTP 503 without an RPC error"):
                    rpc.call("gettxout", "ab" * 32, 0, True)

    def test_answer_without_result(self):
        rpc = self.client(200, {"jsonrpc": "2.0", "id": "heirdrop"})
        with self.assertRaisesRegex(NodeUnreachable, "without a result"):
            rpc.call("gettxout", "ab" * 32, 0, True)

    def test_authentication_rejected(self):
        rpc = self.client(401, "")
        with self.assertRaisesRegex(NodeUnreachable, "authentication rejected"):
            rpc.call("getblockchaininfo")

    def test_cookie_that_is_not_text(self):
        self.cookie.write_bytes(b"__cookie__:\xff\xfe\n")
        rpc = RpcClient("http://127.0.0.1:1/", cookie_file=self.cookie, timeout=5)
        with self.assertRaises(NodeUnreachable) as cm:
            rpc.call("getblockchaininfo")
        self.assertIn("is not text", str(cm.exception))
        self.assertNotIn("0xff", str(cm.exception))
        self.assertIsNone(cm.exception.__cause__)

    def test_missing_cookie(self):
        self.cookie.unlink()
        rpc = RpcClient("http://127.0.0.1:1/", cookie_file=self.cookie, timeout=5)
        with self.assertRaisesRegex(NodeUnreachable, "cannot read cookie file"):
            rpc.call("getblockchaininfo")


if __name__ == "__main__":
    unittest.main()
