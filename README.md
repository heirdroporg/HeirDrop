# heirdrop

A dead man's switch for a family's bitcoin, written as a small, careful
Linux daemon.

The owner signs four bitcoin transactions in advance and gives them to a
service on a VPS. As long as the owner checks in regularly, nothing happens.
If the owner stops checking in, the service gives the wife a chance to
respond. It then decides, once and for all, which pair of transactions to
broadcast, and hands the heirs everything they need to finish the job
themselves.

No private keys are ever on the server. The service can only broadcast
transactions that were signed beforehand and that pay the two addresses
named in its config.

## The idea in one table

| pair | transaction | pays |
|------|-------------|------|
| W    | W1          | the wife's hardware wallet (WHW) |
| W    | W2          | the family member's hardware wallet (FMHW) |
| F    | F1, F2      | FMHW |

W1 and F1 spend the same coins, as do W2 and F2, so only one pair can ever
confirm. Pair W is chosen when the wife responds after the owner went silent.
Pair F is chosen when she does not, and then everything goes to the family
member.

## How it plays out (default timings)

Day 0 is the owner's last check-in.

| day | what happens |
|-----|--------------|
| 4, 11, ... 53 | weekly reminders to the owner |
| 60 | daily warnings to the owner |
| 74 | **owner deadline.** The wife is alerted for the first time, daily from now on |
| 74 to 90 | owner grace: an owner check-in still cancels everything. A wife check-in now selects W |
| 90 | grace ends. If the wife checked in, **W** is decided; otherwise her window opens |
| 90 to 120 | wife window: a wife check-in decides **W** at once |
| 120 | nobody checked in: **F** is decided |

After a decision the transactions are broadcast and tracked until they are
final. An owner check-in at any time before the decision restarts the clock.
After a decision nothing can change it. All durations count *running time*,
so if the server is down for a day, every deadline moves by a day.

The grace clock actually starts when Telegram accepts the first alert to the
wife, not at the deadline itself, so a slow notification path cannot eat
into her time. It waits at most `max_alert_wait_hours` (24 by default) for
that, so a broken one cannot stop the clock forever.

## What it is made of

```
 owner (SSH) ──┐
               │  Unix socket, identity = kernel peer uid
 web app ──────┼──────────────►  heirdrop service  ──── JSON-RPC ───►  bitcoind
 (wife, family)│                       │                              (pruned ok, no
               │                       │ Telegram Bot API             wallet, no txindex)
  nginx + TLS ─┘                       ▼
                               owner's and wife's phones
```

| part | what it does | read |
|------|--------------|------|
| Bitcoin node | Bitcoin Core on the same machine, used only through RPC | [doc/1-bitcoin-node.md](doc/1-bitcoin-node.md) |
| heirdrop service | the daemon: clock, decision, broadcast, tracking, the socket | [doc/2-heirdrop-service.md](doc/2-heirdrop-service.md) |
| Telegram notifier | messages to the owner and the wife, send-only | [doc/3-telegram-notifier.md](doc/3-telegram-notifier.md) |
| web app | the wife's check-in and the heirs' packages, behind nginx | [doc/4-webapp.md](doc/4-webapp.md) |
| rehearsal and go-live | regtest rehearsal, mainnet dry run, going live | [doc/5-rehearsal-and-go-live.md](doc/5-rehearsal-and-go-live.md) |

The config files and systemd units to copy are in [doc/init/](doc/init/):

- `bitcoin.conf` and `bitcoind.service`;
- `heirdrop.example.toml` and `heirdrop.service`;
- `heirdrop-web.example.toml`, `heirdrop-web.service` and
  `heirdrop-web-nginx.conf`.

## Built to wait for years

Funds are at stake and the service may run untouched for a long time, so it
is built to be boring:

- **Python 3.11+, standard library only.** Nothing to update, nothing to be
  compromised through a dependency.
- **One process, one thread.** A tick loop plus a socket listener, no locks.
- **Plain files are the only state.** Each tick recomputes everything from
  those files and the clock. Files are replaced atomically, and one-time
  markers (the decision, each alert) are created exclusively. A crash or a
  restart at any point neither repeats nor skips anything.
- **Hold when unsure.** The service will not decide or broadcast in any of
  these cases:
  - right after a cold start;
  - after a clock jump;
  - while the node is unreachable or not synced;
  - while the system clock and the chain disagree;
  - while a transaction file fails its checksum or pays a wrong address;
  - while its own files contradict each other.

  It alerts instead.
- **The heirs never depend on this machine.** The package each heir gets
  after the decision contains the raw signed transactions, so they can
  broadcast from any wallet or node if the server never manages to.
- **Least privilege.** The service runs as its own user under a hardened
  systemd unit. It can read its config and transactions but not change
  them, and it knows callers only by their uid.

The design decisions, and the trade-offs that were accepted on purpose, are
collected in [AGENTS.md](AGENTS.md).

## Repository layout

```
heirdrop/          the service (python3 -m heirdrop ...)
  notifier/        Telegram and events-log backends, message texts, send queue
webapp/            the web app (python3 -m webapp ...), imports nothing from heirdrop
doc/               setup and operation, one file per part
  init/            config files and systemd units to copy
tests/             unittest suite, with a fake bitcoind, fake Telegram and fake clock
AGENTS.md          design and implementation notes for reviewers and contributors
LICENSE            MIT
```

## Running the tests

```sh
python3 -m unittest discover -s tests -t .
```

The suite runs on Linux and macOS. It uses a fake clock, a fake `bitcoind`
and a fake Telegram, and replays the full 120-day timeline one tick per day.
The web app's tests use a fake service socket and real HTTP on a loopback
port. The full suite takes about 35 seconds; scrypt makes each login check
slow on purpose.

## Status

Complete, and rehearsed on Debian 12 against a regtest `bitcoind`:

- all states;
- both decisions;
- real broadcasts;
- confirmation tracking through a reorg;
- Telegram with a real bot.

The web app runs there behind nginx. A dry run against mainnet and going live
are the remaining steps ([doc/5-rehearsal-and-go-live.md](doc/5-rehearsal-and-go-live.md)).

## License

MIT, see [LICENSE](LICENSE).
