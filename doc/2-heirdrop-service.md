# 2. The heirdrop service

The daemon itself (`heirdrop/`). It runs as the `heirdrop` system user under
systemd, keeps its state in `/var/lib/heirdrop`, and answers on a Unix socket
at `/run/heirdrop/heirdrop.sock`. The node must be set up first
([1-bitcoin-node.md](1-bitcoin-node.md)).

## Installation

Three system users are involved besides `bitcoin`: the service (`heirdrop`),
the web app (`dmweb`) and the owner's SSH login. `dmowner` is only the name
these documents use for that login. The service needs nothing from it except
its name: it recognises the owner by the user id of whoever connects to its
socket. So if you already log in over SSH as yourself, skip creating
`dmowner` and put your own login name into `owner_user` in the config. Any
login works except root, which the service refuses. As root:

```sh
# users: the service and the web app
useradd --system --home /var/lib/heirdrop --shell /usr/sbin/nologin heirdrop
useradd --system --shell /usr/sbin/nologin dmweb      # the web app user (doc/4-webapp.md)
usermod -aG bitcoin heirdrop                          # to read the RPC cookie

# only if the owner needs a new login (see above):
# useradd --create-home --shell /bin/bash dmowner

# config and transactions: owned by root, read by the service through its group
mkdir -p /etc/heirdrop/txs
cp /opt/heirdrop/doc/init/heirdrop.example.toml /etc/heirdrop/heirdrop.toml
chown -R root:heirdrop /etc/heirdrop
chmod 750 /etc/heirdrop /etc/heirdrop/txs
chmod 640 /etc/heirdrop/heirdrop.toml

# documents served to the heirs (optional), read-only for the service
mkdir -p /var/lib/heirdrop-docs/wife /var/lib/heirdrop-docs/family
chown -R root:heirdrop /var/lib/heirdrop-docs
chmod -R 750 /var/lib/heirdrop-docs
```

The service can read its configuration and transactions but not change them.
Nothing running as `heirdrop` can turn off `dry_run`, swap the interface
users or replace a checksum; edit the files as root. The config is accepted
when root owns it and its mode is at most 0640. Read access cannot be taken
away: a process running as `heirdrop` can still read the four transactions.

### The four transactions

The service needs four signed raw transactions, W1, W2, F1 and F2, as hex
text, one per file. W1 pays WHW; W2, F1 and F2 pay FMHW. The table and the
rules they must follow are in [0-install.md](0-install.md).

- **For a rehearsal**, make them on regtest now, with "Creating the regtest
  transactions" in [5-rehearsal.md](5-rehearsal.md), section 1. Its last
  step installs them here with the right owner and mode and prints the
  checksums. Then continue here.
- **For mainnet**, the owner signs them in advance with their own wallet and
  exports each one as raw hex. How to do that depends on the wallet and is
  not covered in these documents.

**Put the four files in place and get their checksums for the config** (the
rehearsal helper has already done this). Each file's sha256 goes into
`[transactions]`:

```sh
cp w1.hex w2.hex f1.hex f2.hex /etc/heirdrop/txs/
chown root:heirdrop /etc/heirdrop/txs/*.hex
chmod 640 /etc/heirdrop/txs/*.hex          # cp makes them 0644, readable by every user
sha256sum /etc/heirdrop/txs/{w1,w2,f1,f2}.hex   # paste into [transactions]
```

### The configuration

Edit `/etc/heirdrop/heirdrop.toml`
(commented example: [`init/heirdrop.example.toml`](init/heirdrop.example.toml)):

- the two addresses and the four checksums;
- `owner_user` and `webapp_user`;
- the `[bitcoind]` access ([1-bitcoin-node.md](1-bitcoin-node.md); the
  example's comments give the regtest values);
- for a rehearsal, the shortened `[timing]` ([5-rehearsal.md](5-rehearsal.md),
  section 1);
- leave `dry_run = true` for now.

| section | what it holds |
|---------|---------------|
| `[timing]` | owner period, warning window, grace, wife window, cold-start hold, tick interval, confirmations, clock skew, alert wait |
| `[bitcoind]` | `rpc_url` and `cookie_file` (or `rpc_user`/`rpc_password`) |
| `[transactions]` | the four hex files with their sha256 |
| `[addresses]` | `whw` (only W1 may pay it) and `fmhw` (W2, F1, F2) |
| `[paths]` | state directory, socket, the two document directories |
| `[interface]` | `owner_user`, `webapp_user` |
| `[telegram]` | optional, see [3-telegram-notifier.md](3-telegram-notifier.md) |
| `[safety]` | `dry_run`, `max_document_bytes`, `max_fee_rate` |

### Validating the config

Run this after every change to the config or the transaction files, before
(re)starting the service. Run it from `/opt/heirdrop`, because
`python3 -m heirdrop` finds the package through the current directory. It
runs as `heirdrop`, so it reads the files the way the service does:

```sh
cd /opt/heirdrop
sudo -u heirdrop python3 -m heirdrop validate-config --config /etc/heirdrop/heirdrop.toml
```

It prints `config ok` with the main settings, then one line per check, and
exits non-zero if something is wrong. It checks:

- the config's syntax, values and permissions (root-owned, at most 0640);
- that `owner_user` and `webapp_user` exist and are not root;
- each transaction file: present, and matching its sha256 (`ok … -> pays
  <address>`, `MISSING` or `CHECKSUM MISMATCH`);
- the two document directories (`(exists)` or `(MISSING)`; missing ones
  are only reported);
- the Telegram token file, if `[telegram]` is set. Without it, a warning
  when `dry_run = false`.

It makes no network call, so it does not check the node or Telegram, and it
does not decode the transactions. Those checks happen in the running
service: see `health` and `notify-test` under "Operating it".

### Regtest: the cookie directory's mode

On regtest bitcoind creates the `regtest` subdirectory itself, possibly with
mode 0700, which keeps the `bitcoin` group away from the cookie. After
bitcoind's first start, check it and fix it:

```sh
ls -ld /var/lib/bitcoind/regtest /var/lib/bitcoind/regtest/.cookie   # want drwxr-x--- and -rw-r-----
chmod 750 /var/lib/bitcoind/regtest
```

### Starting the service

Once `validate-config` is clean:

```sh
cp /opt/heirdrop/doc/init/heirdrop.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now heirdrop
journalctl -fu heirdrop
```

On the first ticks the journal shows `cold_start`, then a `validation` line
with `ok` for all four transactions. `status` (run as the owner, see "Owner"
under "Operating it") must show no `node_*` holds. On regtest, mine a block
first if it does (see the end of "Owner"). `unarmed` and `cold_start_hold`
are expected at this point.

The unit ([`init/heirdrop.service`](init/heirdrop.service)) runs as
`heirdrop`. It creates `/var/lib/heirdrop` (state, mode 0700) and
`/run/heirdrop` (socket), restarts on any exit, and is sandboxed read-only
apart from those two directories. It does not use `ConfigurationDirectory=`,
which would hand `/etc/heirdrop` to the service user.

## Operating it

### Owner

Log in over SSH as the owner user and run, from `/opt/heirdrop`:

```sh
python3 -m heirdrop checkin             # records the owner check-in; the first one arms the service
python3 -m heirdrop status              # all three views, deadlines, holds
python3 -m heirdrop health              # node, transaction checks, tracking, notifications
python3 -m heirdrop notify-test owner   # a test message through Telegram (or wife)
python3 -m heirdrop package wife        # either package, any time, to verify the setup
```

`health` shows under `notifications` when each person was last notified
(`last_notified_on`, the time Telegram accepted the message). Under
`pending_alerts` it shows what has not gone through yet, with the number of
attempts, the last attempt and the last error.

Never run these through `sudo`: the service identifies the caller by uid.
Put an alias in the owner's shell if you like. Set `HEIRDROP_SOCKET` if the
socket is not at `/run/heirdrop/heirdrop.sock`.

There is no disarm command. To stop the switch, run `systemctl stop
heirdrop`, and `systemctl disable heirdrop` so it does not return at boot.
While it is stopped nothing is broadcast. When it starts again it holds for
`cold_start_hold_hours`, and all deadlines have moved by the downtime.

To follow the service as it changes, let `watch` rerun a command and
highlight what changed (`-d`). Run it as the owner from `/opt/heirdrop`:

```sh
cd /opt/heirdrop
watch -d -n 20 python3 -m heirdrop status    # state, deadlines, holds
watch -d -n 20 python3 -m heirdrop health    # node, transactions, notifications
```

`-n 20` redraws every 20 seconds. The service changes only once per tick
(`tick_interval_seconds`: 60 by default, 10 in the rehearsal). Stop with
Ctrl-C.

On regtest no blocks arrive by themselves, so mine one now and then, as root
(with `bcli` from [1-bitcoin-node.md](1-bitcoin-node.md)):

```sh
bcli -rpcwallet=signer -generate 1
```

The service compares the system clock with the time of the newest block and
holds with `clock_skew` when they differ by more than
`max_clock_skew_hours`. bitcoind also reports itself as syncing while its
newest block is about a day old, which shows as `node_not_synced`. A fresh
block clears both. On mainnet, blocks come every ten minutes on their own.

### Wife and family

The web app ([4-webapp.md](4-webapp.md)) runs as `webapp_user`. It is the
only way the wife checks in and the only reader for the family. To try the
web app's requests without the web app, run them as its user, as root from
`/opt/heirdrop`:

```sh
sudo -u dmweb python3 -m heirdrop checkin wife     # the wife's check-in
sudo -u dmweb python3 -m heirdrop status           # wife and family views only
sudo -u dmweb python3 -m heirdrop package family   # after a decision
```

Only that uid may check in as the wife. The owner's login is refused for it,
and root is refused for everything. What the wife's check-in does depends on
the moment:

| when | effect |
|------|--------|
| before the owner deadline | none (stored, ignored) |
| during grace | selects pair W (`w_selected_at` in the owner view), decided at the end of grace unless the owner checks in first |
| during the wife window | decides pair W at once |
| after decision F | her view switches to `false_triggered` and an alert is queued; nothing is undone |

From the owner deadline until the decision her view's `deadline` is the end
of the wife window; otherwise it is null. Her view's `checked_in_at` is her
check-in once it counts (at or after the owner deadline), else null, so the
web app can confirm it.

### States shown

| view   | states |
|--------|--------|
| owner  | `unarmed`, `normal`, `warning`, `grace`, `wife_window`, `decided`, `final` |
| wife   | `normal`, `grace`, `wife_window`, `inheritance` (after W), `closed` (after F), `false_triggered` (checked in after F) |
| family | `standby`, `alert` (owner deadline passed), `inheritance` (after either decision) |

Every view also carries `period`, `period_end`, `time_left_seconds` and
`hold_remaining_seconds`.

## Hold conditions

The service records a decision and broadcasts only when none of these hold:

- cold start: for `cold_start_hold_hours` after the first tick or after a gap
  longer than `cold_start_gap_threshold_hours` (a restart during a hold
  resumes it);
- the system clock went backwards relative to the last tick;
- a check-in file carries a timestamp from the future;
- `bitcoind` is unreachable, in initial block download, or behind its
  headers. An answer with a missing field or a wrong type counts as
  unreachable, and so does a validation run that could not finish;
- the system clock and the tip block time differ by more than
  `max_clock_skew_hours`;
- the state directory is unreadable or its files contradict each other;
- a transaction file fails its sha256 checksum or does not decode;
- a transaction pays any address other than the configured WHW (W1) or FMHW
  (W2, F1, F2).

Spent inputs, mempool rejection and a low fee rate alert the owner but do not
hold, because those are exactly the conditions under which the heirs need the
raw transactions in their hands.

## Local interface

A Unix domain socket at `[paths] socket`. Connect, send one JSON object on one
line, read one JSON object on one line, close. The caller is identified by the
kernel's peer credentials; nothing in the request is trusted for identity.

| caller      | allowed |
|-------------|---------|
| owner_user  | `status` (all views), `checkin` (as owner), `health`, `notify_test`, `package` and `document` for either role at any time |
| webapp_user | `status` (wife and family views), `checkin` (as wife), `package` and `document` for `wife` (only after W) and `family` (only after a decision) |
| anyone else | refused |

Requests and responses. Each answer has `"ok": true` on success, or
`{"ok": false, "error": "..."}` otherwise:

```
{"request": "status"}
  -> {"ok": true, "now": "...Z", "dry_run": true, "views": {"wife": {...}, "family": {...}}}
     as webapp_user; the owner's answer also has "owner" in "views"
{"request": "checkin"}                       role defaults to the caller's own
{"request": "checkin", "role": "wife"}
  -> {"ok": true, "role": "wife", "at": "...Z", "after_decision": false, "state": "normal", "dry_run": true}
     plus "warning": "..." if the check-in was recorded but the tick after it failed;
     "state" is the wife view's state for the wife, the owner's phase for the owner
{"request": "package", "role": "family"}
  -> {"ok": true, "role": "family", "state": "inheritance", "decision": {"choice": "F", "at": "...Z"},
      "final_at": null,
      "transactions": [{"name": "F1", "hex": "...", "txid": "...", "broadcast_at": "...Z",
                        "dry_run": false, "confirmations": 3, "pays": "bc1q..."}, ...],
      "documents": [{"name": "letter.pdf", "size": 12345}]}
{"request": "document", "role": "family", "name": "letter.pdf"}
  -> {"ok": true, "role": "family", "name": "letter.pdf", "size": 12345, "base64": "..."}
{"request": "health"}
  -> {"ok": true, "state_error": null, "holds": [], "node": {...}, "transactions": {...},
      "last_validation": "...Z", "last_tick": "...Z", "tracking": {...},
      "notifications": {"backend": "telegram",
                        "owner": {"last_notified_on": "...Z", "last_kind": "grace", "last_id": "...", "pending": 0},
                        "wife":  {"last_notified_on": null, "last_kind": null, "last_id": null, "pending": 1}},
      "pending_alerts": [{"id": "...", "to": "wife", "kind": "grace", "created_at": "...Z", "attempts": 3,
                          "last_attempt_at": "...Z", "last_error": "HTTP 403: Forbidden: bot was blocked by the user",
                          "permanent": true}],
      "missed_alerts": 2, "dry_run": true}
{"request": "notify_test", "to": "wife"}          owner only
  -> {"ok": true, "to": "wife", "accepted_at": "...Z", "receipt": {"message_id": 42}}
     or {"ok": false, "error": "not accepted: HTTP 403: ..."}
```

Documents are plain files in `documents_wife` / `documents_family`. The wife
package lists only W1, the family package W2 or F1+F2. From Python:

```python
from heirdrop.server import request
request("/run/heirdrop/heirdrop.sock", {"request": "status"})
```

### Interface decisions worth knowing

- The package carries the raw hex even while the service is holding, so an
  heir can broadcast from any wallet or node if this machine never does.
- The owner may fetch either package and any document at any time, to verify
  the setup. The heirs get theirs only after the decision.
- The socket file is mode 0666. Anyone may connect; the peer uid check is the
  only authorization, so there is no group to keep in sync.
- One request per connection: open, send one line, read one line, close.
- An unexpected failure answers `"error": "internal error"` and nothing more;
  the exception is in `events.jsonl` as `request_error`.
- The whole request line must arrive within 5 seconds. A caller whose uid has
  no role is refused before anything is read, and the refusal goes to the
  journal only (`refused uid N`), not to `events.jsonl`.
- The owner cannot check in as the wife. The wife's path is the web app user;
  for a rehearsal use `sudo -u dmweb python3 -m heirdrop checkin wife`.

## State directory

Plain files, written only by the service, with UTC timestamps inside. Nothing
derived is stored: every tick recomputes the situation from these files and
the clock.

| file | meaning |
|------|---------|
| `armed.json` | first owner check-in (created once) |
| `checkin_owner.json`, `checkin_wife.json` | latest check-in per role |
| `last_tick.json` | end of the last tick; gaps become pauses |
| `pauses.jsonl` | append-only downtime ledger, each line `from`, `to`, optional `hold_until`. Overlapping lines count once. An incomplete last line (crash during an append) is ignored and dropped at the next append |
| `decision.json` | the one decision, `choice` W or F and `at`; created exclusively, never rewritten |
| `broadcast_<name>.json` | txid, time, `dry_run`, and the block hash once confirmed |
| `final.json` | both transactions reached `confirmations_final` |
| `alerts/<id>.json` | one file per alert, created exclusively (restarts never duplicate): text, `created_at`, `accepted_at`, `attempts`, `last_attempt_at`, `last_error`, `permanent`, `receipt` (Telegram's message id) |
| `events.jsonl` | append-only log of everything the service did, including an `alert` line for every accepted message and a `notify_test` line per test |
| `lock` | empty; `run` holds an exclusive lock on it, so a second service on the same directory exits at once |

Which alerts exist is derived from the state each tick; the alert files are
the queue. The notifier (`heirdrop/notifier/`) renders the texts, picks what
is worth sending and sends it through Telegram. Without a `[telegram]`
section it writes them only to `events.jsonl`.

## Fee risk

The presigned transactions carry a fixed fee. If mainnet fee levels rise far
above it for weeks, `sendrawtransaction` may still accept them but they may
never confirm. Or the node may reject them as below its mempool minimum.

The service alerts the owner (`tx_problem` with a fee margin warning)
whenever the fee rate drops under twice the node's `mempoolminfee`. After a
broadcast it rebroadcasts whenever a transaction leaves the mempool. Two
mitigations are on the owner:

- re-sign the four transactions with a higher fee whenever the warning fires,
  replace the files and checksums, run `validate-config`, and restart the
  unit;
- make sure the heirs' wallets can do child-pays-for-parent on W1/W2/F1/F2;
  the raw hex in the package lets them do so from any wallet.
