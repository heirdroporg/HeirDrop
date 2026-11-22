"""Validation of the four presigned transactions."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .config import Config, TxSpec
from .node import RpcError


@dataclass
class TxReport:
    name: str
    txid: str | None = None
    checksum_ok: bool = False
    decoded: bool | None = None
    address_ok: bool | None = None
    inputs_unspent: bool | None = None
    accepted: bool | None = None
    reject_reason: str | None = None
    fee_rate_sat_vb: float | None = None
    mempool_min_sat_vb: float | None = None
    problems: list[str] = field(default_factory=list)

    def hold_reasons(self) -> list[str]:
        out = []
        if not self.checksum_ok:
            out.append(f"tx_checksum:{self.name}")
        if self.decoded is False:
            out.append(f"tx_decode:{self.name}")
        if self.address_ok is False:
            out.append(f"tx_address:{self.name}")
        return out

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def read_tx_file(spec: TxSpec) -> str | None:
    """Return the hex text if the file exists and matches its SHA-256."""
    try:
        data = spec.file.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(data).hexdigest() != spec.sha256:
        return None
    return data.decode("ascii", "replace").strip()


def load_all(cfg: Config) -> dict[str, str | None]:
    return {name: read_tx_file(spec) for name, spec in cfg.transactions.items()}


def expected_address(cfg: Config, name: str) -> str:
    return cfg.whw_address if name == "W1" else cfg.fmhw_address


def validate(cfg: Config, node, hexes: dict[str, str | None],
             skip_node_checks: set[str] = frozenset()) -> dict[str, TxReport]:
    """Check every transaction. Raises NodeUnreachable if the node fails mid-way.

    skip_node_checks names transactions already broadcast: their inputs are
    expected to be spent, so only checksum and outputs are checked.
    """
    try:
        min_fee_btc_kvb = float(node.call("getmempoolinfo").get("mempoolminfee", 0))
    except RpcError:
        min_fee_btc_kvb = 0.0
    min_sat_vb = min_fee_btc_kvb * 1e8 / 1000

    reports = {}
    for name, hexstr in hexes.items():
        r = TxReport(name=name)
        reports[name] = r
        if hexstr is None:
            r.problems.append("file missing or checksum mismatch")
            continue
        r.checksum_ok = True
        try:
            decoded = node.call("decoderawtransaction", hexstr)
        except RpcError as e:
            r.decoded = False
            r.problems.append(f"does not decode: {e.message}")
            continue
        r.decoded = True
        r.txid = decoded.get("txid")

        want = expected_address(cfg, name).lower()
        bad = []
        for out in decoded.get("vout", []):
            addr = (out.get("scriptPubKey") or {}).get("address")
            if not addr or addr.lower() != want:
                bad.append(addr or "<no address>")
        r.address_ok = not bad
        if bad:
            r.problems.append(f"pays unexpected address(es): {', '.join(bad)}")
        if name in skip_node_checks:
            continue

        spent = []
        for vin in decoded.get("vin", []):
            if "txid" not in vin:
                spent.append("coinbase input")
                continue
            if node.call("gettxout", vin["txid"], vin["vout"], True) is None:
                spent.append(f"{vin['txid']}:{vin['vout']}")
        r.inputs_unspent = not spent
        if spent:
            r.problems.append(f"inputs spent or unknown: {', '.join(spent)}")

        result = node.call("testmempoolaccept", [hexstr])
        entry = result[0] if result else {}
        r.accepted = bool(entry.get("allowed"))
        if not r.accepted:
            r.reject_reason = entry.get("reject-reason") or "unknown"
            r.problems.append(f"node would not accept it: {r.reject_reason}")
        fees = entry.get("fees") or {}
        vsize = entry.get("vsize") or decoded.get("vsize")
        if "base" in fees and vsize:
            r.fee_rate_sat_vb = round(float(fees["base"]) * 1e8 / float(vsize), 2)
            r.mempool_min_sat_vb = round(min_sat_vb, 2)
            if r.fee_rate_sat_vb < 2 * min_sat_vb:
                r.problems.append(
                    f"fee rate {r.fee_rate_sat_vb} sat/vB is close to the mempool minimum {r.mempool_min_sat_vb}"
                )
    return reports
