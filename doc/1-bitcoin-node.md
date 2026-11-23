# 1. The Bitcoin node

heirdrop needs a Bitcoin Core node on the same machine. It talks to it over
JSON-RPC only. It never touches the node's data and needs neither `txindex`
nor a wallet, so a pruned node is fine.

## Requirements

See [0-install.md](0-install.md) for all prerequisites, including the
Bitcoin Core version.

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

### Getting the code

All commands in these documents run as `root`, unless stated otherwise. Put
the heirdrop code into `/opt/heirdrop` first; the setup below copies files
from it.

```sh
apt install git
git clone https://github.com/heirdroporg/HeirDrop.git /opt/heirdrop
chown -R root:root /opt/heirdrop
chmod -R u=rwX,go=rX /opt/heirdrop      # the service user must be able to read it
```

### Downloading Bitcoin Core

Debian does not package Bitcoin Core. Download it from
https://bitcoincore.org/en/download/ and check its signatures before you run
it. Set `VER` to the current release shown there. `ARCH` picks the build for
this machine's processor (`x86_64` or `aarch64`).

```sh
apt install wget gnupg
VER=<current version, e.g. 32.1>
ARCH=$(uname -m)        # x86_64 or aarch64
cd /tmp
wget https://bitcoincore.org/bin/bitcoin-core-$VER/bitcoin-$VER-$ARCH-linux-gnu.tar.gz
wget https://bitcoincore.org/bin/bitcoin-core-$VER/SHA256SUMS
wget https://bitcoincore.org/bin/bitcoin-core-$VER/SHA256SUMS.asc

# 1. the archive matches the list of checksums: want "OK"
sha256sum --ignore-missing --check SHA256SUMS

# 2. the list is signed by the Bitcoin Core builders: want several "Good signature"
git clone https://github.com/bitcoin-core/guix.sigs
gpg --import guix.sigs/builder-keys/*
gpg --verify SHA256SUMS.asc SHA256SUMS

# 3. unpack and install the two programs
tar -xzf bitcoin-$VER-$ARCH-linux-gnu.tar.gz
install -m 0755 -t /usr/local/bin bitcoin-$VER/bin/bitcoind bitcoin-$VER/bin/bitcoin-cli
bitcoind -version | head -1
```

Step 2 also prints warnings that the keys are not certified. That is
normal. What matters is several "Good signature" lines from different
builders. Do not continue if step 1 does not say `OK`.

### Setting up the bitcoin user and config

bitcoind should run under its own system user, `bitcoin`, and not as root or
any other user, the heirdrop user included. The service only gets RPC
access: it reads the cookie file through the `bitcoin` group and can neither
read nor write the node's data. The rehearsal (see
[5-rehearsal-and-go-live.md](5-rehearsal-and-go-live.md)) and the VPS use the
same layout, so the rehearsal proves the real setup.

[`init/bitcoin.conf`](init/bitcoin.conf) has `server=1` and
`rpccookieperms=group` (the cookie becomes readable by the `bitcoin` group).
It also has the two regtest lines for a rehearsal, and the mainnet options
`prune` and `disablewallet` commented out. Keep the regtest lines for a
rehearsal; for mainnet remove them and uncomment the mainnet options.

```sh
useradd --system --create-home --home /var/lib/bitcoind --shell /usr/sbin/nologin bitcoin
mkdir -p /etc/bitcoin
cp /opt/heirdrop/doc/init/bitcoin.conf /etc/bitcoin/bitcoin.conf
nano /etc/bitcoin/bitcoin.conf
chown -R bitcoin:bitcoin /etc/bitcoin /var/lib/bitcoind
chmod 750 /etc/bitcoin /var/lib/bitcoind
chmod 640 /etc/bitcoin/bitcoin.conf
```

### Starting bitcoind

```sh
cp /opt/heirdrop/doc/init/bitcoind.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now bitcoind
```

The cookie is then at `/var/lib/bitcoind/regtest/.cookie` on regtest and
`/var/lib/bitcoind/.cookie` on mainnet. The RPC port is 18443 on regtest and
8332 on mainnet. Put those two into `[bitcoind]` of the heirdrop config later
([2-heirdrop-service.md](2-heirdrop-service.md)).

Every `bitcoin-cli` call runs as the `bitcoin` user. Define this once in your
root shell and use it in all the commands of these documents:

```sh
bcli() { sudo -u bitcoin bitcoin-cli -conf=/etc/bitcoin/bitcoin.conf -datadir=/var/lib/bitcoind "$@"; }
bcli getblockchaininfo
```

If `sudo` is not installed (a minimal Debian may lack it), use
`runuser -u bitcoin --` in place of `sudo -u bitcoin`.

### Regtest: the cookie directory's mode

On regtest bitcoind creates the `regtest` subdirectory itself, possibly with
mode 0700, which keeps the `bitcoin` group away from the cookie. After the
first start, check it and fix it:

```sh
ls -ld /var/lib/bitcoind/regtest /var/lib/bitcoind/regtest/.cookie   # want drwxr-x--- and -rw-r-----
chmod 750 /var/lib/bitcoind/regtest
```

## Watching bitcoind

```sh
systemctl status bitcoind          # running, and since when
journalctl -fu bitcoind            # its log, live (Ctrl-C to stop)
bcli getblockchaininfo             # sync progress
```

In `getblockchaininfo`, the node is synced when `initialblockdownload` is
`false` and `blocks` equals `headers`. `verificationprogress` approaches 1
during the first sync. Until then the heirdrop service holds with
`node_not_synced`. That is expected on a fresh mainnet node, which takes
hours to days to sync. bitcoind also writes the same log to
`/var/lib/bitcoind/debug.log` (`regtest/debug.log` on regtest).

## bitcoind access

With the setup above the service reads the cookie file. `rpccookieperms=group`
makes it readable by the `bitcoin` group, and the heirdrop unit joins that
group through `SupplementaryGroups=bitcoin`. Set `cookie_file` to the cookie's
path and `rpc_url` to the right port. No secret ends up in the config.

If you would rather not share the cookie, create an `rpcauth` entry for a user
`heirdrop` in `bitcoin.conf`. The script `share/rpcauth/rpcauth.py` in
Bitcoin Core's source makes one: `python3 rpcauth.py heirdrop` prints the
`rpcauth=` line for `bitcoin.conf` and the password. Then put
`rpc_user`/`rpc_password` in the heirdrop config instead of `cookie_file`.

**The node is trusted with the transactions.** Validation sends all four raw
transactions to it (`decoderawtransaction`, `testmempoolaccept`) at start and
once a day, long before any decision. The service does not check who answers
on the RPC port. Whoever controls bitcoind receives the transactions and
could broadcast either pair early. The same goes for anyone who manages to
listen on its port while it is down and its cookie file is still there
(after a kill or a power loss). This is an accepted risk:

- keep bitcoind on the same machine as the service;
- keep other local users off it;
- prefer the cookie over `rpc_user`/`rpc_password`. bitcoind deletes the
  cookie when it stops cleanly, and without a cookie the service sends
  nothing at all. With a password the service keeps sending to whoever
  listens on the port, and the password stays valid for them.

The unit file [`init/bitcoind.service`](init/bitcoind.service) is less
hardened than heirdrop's own unit (see "Accepted risks and known
limitations" in [AGENTS.md](../AGENTS.md)).
