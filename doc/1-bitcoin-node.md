# 1. The Bitcoin node

heirdrop needs a Bitcoin Core node on the same machine. It talks to it over
JSON-RPC only. It never touches the node's data and needs neither `txindex`
nor a wallet, so a pruned node is fine.

## Requirements

- Linux with systemd, Python 3.11 or newer
- Bitcoin Core 27 or newer, on the same machine. Pruned is fine. `txindex`
  and a wallet are **not** required.

### RPC calls relied on

`getblockchaininfo`, `getblockheader`, `getblockhash`, `decoderawtransaction`,
`gettxout`, `testmempoolaccept`, `getmempoolinfo`, `getmempoolentry`,
`sendrawtransaction`. All of them work on a pruned node without `txindex`.

Confirmations are tracked with `gettxout` on our own output until the first
confirmation, then by the recorded block hash with `getblockheader`. A spent
output or a reorg is therefore handled without an index. The block is located
from the tip named in the `gettxout` answer (`bestblock`), not from the tip
seen earlier in the tick.

## Setting it up

Run `bitcoind` as its own system user, `bitcoin`. Never run it as root or as
the heirdrop user. The service only gets RPC access: it reads the cookie file
through the `bitcoin` group and can neither read nor write the node's data.
The rehearsal (see [5-rehearsal-and-go-live.md](5-rehearsal-and-go-live.md))
and the VPS use the same layout, so the rehearsal proves the real setup.

Debian does not package Bitcoin Core. Download version 27 or newer from
bitcoincore.org, verify the signatures, and unpack `bitcoind` and
`bitcoin-cli` into `/usr/local/bin`. Then, as root, with the heirdrop code
already in `/opt/heirdrop` (see [2-heirdrop-service.md](2-heirdrop-service.md)):

```sh
useradd --system --create-home --home /var/lib/bitcoind --shell /usr/sbin/nologin bitcoin
mkdir -p /etc/bitcoin
cp /opt/heirdrop/doc/init/bitcoin.conf /etc/bitcoin/bitcoin.conf
nano /etc/bitcoin/bitcoin.conf      # regtest for a rehearsal, mainnet options for real
chown -R bitcoin:bitcoin /etc/bitcoin /var/lib/bitcoind
chmod 750 /etc/bitcoin /var/lib/bitcoind
chmod 640 /etc/bitcoin/bitcoin.conf
cp /opt/heirdrop/doc/init/bitcoind.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now bitcoind
```

[`init/bitcoin.conf`](init/bitcoin.conf) has `server=1` and
`rpccookieperms=group` (the cookie becomes readable by the `bitcoin` group).
It also has the two regtest lines for a rehearsal, and the mainnet options
`prune` and `disablewallet` commented out.

The cookie is then at `/var/lib/bitcoind/regtest/.cookie` on regtest and
`/var/lib/bitcoind/.cookie` on mainnet. The RPC port is 18443 on regtest and
8332 on mainnet. Put those two into `[bitcoind]` of the heirdrop config.

On regtest bitcoind creates the `regtest` subdirectory itself, possibly with
mode 0700, which keeps the `bitcoin` group away from the cookie. After the
first start, check it and fix it:

```sh
ls -ld /var/lib/bitcoind/regtest /var/lib/bitcoind/regtest/.cookie   # want drwxr-x--- and -rw-r-----
chmod 750 /var/lib/bitcoind/regtest
```

Every `bitcoin-cli` call runs as the `bitcoin` user. Define this once in your
root shell and use it in all the commands of these documents:

```sh
bcli() { sudo -u bitcoin bitcoin-cli -conf=/etc/bitcoin/bitcoin.conf -datadir=/var/lib/bitcoind "$@"; }
bcli getblockchaininfo
```

## bitcoind access

With the setup above the service reads the cookie file. `rpccookieperms=group`
makes it readable by the `bitcoin` group, and the heirdrop unit joins that
group through `SupplementaryGroups=bitcoin`. Set `cookie_file` to the cookie's
path and `rpc_url` to the right port. No secret ends up in the config.

If you would rather not share the cookie, create an `rpcauth` entry for a user
`heirdrop` in `bitcoin.conf`. Then put `rpc_user`/`rpc_password` in the
heirdrop config instead of `cookie_file`.

**The node is trusted with the transactions.** Validation sends all four raw
transactions to it (`decoderawtransaction`, `testmempoolaccept`) at start and
once a day, long before any decision. The service does not check who answers
on the RPC port. Whoever controls bitcoind receives the transactions and
could broadcast either pair early. The same goes for anyone who manages to
listen on its port while it is down and its cookie file is still there
(after a kill or a power loss). This is an accepted risk:

- keep bitcoind on this machine;
- keep other local users off it;
- prefer the cookie over `rpc_user`/`rpc_password`, because a stale cookie is
  worthless to a listener while a password keeps working.

Once the service runs, check with `journalctl -u heirdrop`: the first ticks
log `cold_start`, then a `validation` line with `ok` for all four
transactions. `status` must show no `node_*` holds.

The unit file [`init/bitcoind.service`](init/bitcoind.service) is less
hardened than heirdrop's own unit (see "Known and left for later" in
[AGENTS.md](../AGENTS.md)).
