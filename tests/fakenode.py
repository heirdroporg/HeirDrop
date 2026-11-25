"""An in-memory stand-in for bitcoind covering the RPC calls the service uses."""
from __future__ import annotations

from datetime import timedelta

from heirdrop.node import NodeUnreachable, RpcError


class FakeNode:
    def __init__(self, clock, height: int = 800000):
        self.clock = clock
        self.height = height
        self.headers_ahead = 0
        self.ibd = False
        self.reachable = True
        self.tip_time_offset = timedelta(minutes=-10)
        self.tip_time_fixed = None
        self.min_fee = 0.00001
        self.fail_send: Exception | None = None
        self.txs: dict[str, dict] = {}        # hex -> decoded
        self.txid_hex: dict[str, str] = {}
        self.utxos: dict[tuple, float] = {}   # (txid, n) -> value
        self.utxo_height: dict[tuple, int | None] = {}
        self.spent_by: dict[tuple, str] = {}  # (txid, n) -> spender txid or "external"
        self.mempool: set[str] = set()
        self.chain: dict[str, int] = {}       # txid -> height
        self.reorged: set[str] = set()
        self.reject: dict[str, str] = {}
        self.sent: list[str] = []
        self.calls: list[str] = []

    # ---- test setup helpers ------------------------------------------------
    def add_utxo(self, txid, n, value, height=700000):
        self.utxos[(txid, n)] = value
        self.utxo_height[(txid, n)] = height

    def register_tx(self, hexstr, txid, inputs, outputs, vsize=200):
        self.txs[hexstr] = {
            "txid": txid, "vsize": vsize,
            "vin": [{"txid": t, "vout": n} for t, n in inputs],
            "vout": [{"value": v, "n": i, "scriptPubKey": {"address": a}} for i, (a, v) in enumerate(outputs)],
        }
        self.txid_hex[txid] = hexstr

    def mine(self, n=1):
        for _ in range(n):
            self.height += 1
            for txid in list(self.mempool):
                self.chain[txid] = self.height
                self.mempool.discard(txid)
                for key, h in self.utxo_height.items():
                    if key[0] == txid and h is None:
                        self.utxo_height[key] = self.height

    def evict(self, txid):
        self.mempool.discard(txid)
        for key, spender in list(self.spent_by.items()):
            if spender == txid:
                del self.spent_by[key]
        for key in [k for k in self.utxos if k[0] == txid]:
            del self.utxos[key]
            del self.utxo_height[key]

    def spend_externally(self, key):
        self.spent_by[key] = "external"
        self.chain["external"] = self.height

    # ---- RPC --------------------------------------------------------------
    def _tip_time(self):
        if self.tip_time_fixed is not None:
            return self.tip_time_fixed.timestamp()
        return (self.clock.now() + self.tip_time_offset).timestamp()

    def call(self, method, *params):
        self.calls.append(method)
        if not self.reachable:
            raise NodeUnreachable("connection refused")
        return getattr(self, "rpc_" + method)(*params)

    def rpc_getblockchaininfo(self):
        return {"blocks": self.height, "headers": self.height + self.headers_ahead,
                "initialblockdownload": self.ibd, "time": self._tip_time(),
                "bestblockhash": f"hash{self.height}"}

    def rpc_getblockhash(self, height):
        return f"hash{height}"

    def rpc_getblockheader(self, blockhash, verbose=True):
        h = int(blockhash[4:])
        confs = -1 if blockhash in self.reorged else self.height - h + 1
        return {"confirmations": confs, "height": h, "time": self._tip_time()}

    def rpc_decoderawtransaction(self, hexstr):
        if hexstr not in self.txs:
            raise RpcError(-22, "TX decode failed")
        return self.txs[hexstr]

    def _spender_in(self, key, include_mempool):
        spender = self.spent_by.get(key)
        if spender is None:
            return False
        if spender in self.chain:
            return True
        return include_mempool and spender in self.mempool

    def rpc_gettxout(self, txid, n, include_mempool=True):
        key = (txid, n)
        if key not in self.utxos or self._spender_in(key, include_mempool):
            return None
        h = self.utxo_height[key]
        confs = 0 if h is None else self.height - h + 1
        return {"value": self.utxos[key], "confirmations": confs, "bestblock": f"hash{self.height}"}

    def _fee(self, decoded):
        ins = sum(self.utxos.get((v["txid"], v["vout"]), 0) for v in decoded["vin"])
        return round(ins - sum(o["value"] for o in decoded["vout"]), 8)

    def _check_inputs(self, decoded, txid):
        for v in decoded["vin"]:
            key = (v["txid"], v["vout"])
            if key not in self.utxos or self._spender_in(key, False):
                return -25, "bad-txns-inputs-missingorspent"
            spender = self.spent_by.get(key)
            if spender and spender != txid and spender in self.mempool:
                return -26, "txn-mempool-conflict"
        return None

    def _over_fee_cap(self, decoded, maxfeerate):
        """bitcoind's maxfeerate in BTC/kvB: 0.10 when not given, 0 = no cap."""
        cap = 0.10 if maxfeerate is None else maxfeerate
        return cap > 0 and self._fee(decoded) * 1000 / decoded["vsize"] > cap

    def rpc_testmempoolaccept(self, hexes, maxfeerate=None):
        out = []
        for hexstr in hexes:
            d = self.rpc_decoderawtransaction(hexstr)
            txid = d["txid"]
            problem = None
            if txid in self.chain:
                problem = "txn-already-known"
            elif txid in self.reject:
                problem = self.reject[txid]
            else:
                bad = self._check_inputs(d, txid)
                if bad and txid not in self.mempool:
                    problem = bad[1]
                elif not bad and self._over_fee_cap(d, maxfeerate):
                    problem = "max-fee-exceeded"
            if problem:
                out.append({"txid": txid, "allowed": False, "reject-reason": problem})
            else:
                out.append({"txid": txid, "allowed": True, "vsize": d["vsize"], "fees": {"base": self._fee(d)}})
        return out

    def rpc_getmempoolinfo(self):
        return {"mempoolminfee": self.min_fee}

    def rpc_getmempoolentry(self, txid):
        if txid in self.mempool:
            return {"vsize": 200}
        raise RpcError(-5, "Transaction not in mempool")

    def rpc_sendrawtransaction(self, hexstr, maxfeerate=None):
        d = self.rpc_decoderawtransaction(hexstr)
        txid = d["txid"]
        self.sent.append(txid)
        if self.fail_send is not None:
            raise self.fail_send
        if txid in self.chain:
            raise RpcError(-27, "Transaction already in block chain")
        if txid in self.mempool:
            return txid
        if txid in self.reject:
            raise RpcError(-26, self.reject[txid])
        bad = self._check_inputs(d, txid)
        if bad:
            raise RpcError(*bad)
        if self._over_fee_cap(d, maxfeerate):
            raise RpcError(-25, "Fee exceeds maximum configured by user (e.g. -maxtxfee, maxfeerate)")
        self.mempool.add(txid)
        for v in d["vin"]:
            self.spent_by[(v["txid"], v["vout"])] = txid
        for o in d["vout"]:
            self.utxos[(txid, o["n"])] = o["value"]
            self.utxo_height[(txid, o["n"])] = None
        return txid
