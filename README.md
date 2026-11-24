# HeirDrop

A self-hosted dead man's switch for your Bitcoin stack. If you stop checking
in, it broadcasts transactions you signed in advance, paying the addresses
you chose.

Suppose you want your heirs to inherit your stack when you are no longer
available, but you don't want to give them or any third party (a lawyer, a
relative, a custodian) control of your coins while you are alive. You set up
two single-sig hardware wallets, each with its own seed. One goes to your
wife and the other to a family member, say your mother. Your stack sits in
two UTXOs under your control. If you become unavailable, a predetermined
amount should go to your wife and the rest to your mother. If your wife is
gone too, everything should go to your mother.

So you presign two pairs of transactions. In pair **W** (your wife is
present), W1 sends one UTXO to your wife and W2 sends the other to your
mother. In pair **F** (neither of you is present), F1 and F2 send both UTXOs
to your mother.

The owner signs four bitcoin transactions in advance and gives them to a
service on a VPS. As long as the owner checks in regularly, nothing happens.
If the owner stops checking in, the service gives the wife a chance to
respond. It then decides, once and for all, which pair of transactions to
broadcast, and broadcasts it. Each heir also gets their own signed
transactions, plus any documents the owner left for them (instructions, a letter),
so they can finish the job from any wallet or node if the broadcast fails or
the transactions get stuck.

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

## How each person takes part

The owner checks in over SSH. The service reminds the owner, and later the
wife, through a Telegram bot when a check-in is due. The wife and the family
member each have their own password-protected page in the web app. The wife
checks in there, and after the decision each of them collects their
transactions and documents there. The family member gets no Telegram
messages.

## How it plays out (default timings)

Day 0 is the owner's last check-in.

| day | what happens |
|-----|--------------|
| 4, 11, ... 53 | weekly reminders to the owner |
| 60 to 74 | daily warnings to the owner |
| 74 | **owner deadline.** The wife is alerted for the first time; she and the owner are alerted daily from now on |
| 74 to 90 | owner grace: an owner check-in still restarts the clock. A wife check-in now selects W |
| 90 | grace ends. If the wife checked in, **W** is decided; otherwise her window opens |
| 90 to 120 | wife window: a wife check-in decides **W** at once; an owner check-in still restarts the clock |
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
 owner (SSH) ───────────────────────────┐
                                        │  Unix socket, identity = kernel peer uid
 wife, family ─► nginx + TLS ─► web app ┴───────►  heirdrop service  ──── JSON-RPC ───►  bitcoind
                                                          │                              (pruned ok, no
                                                          │ Telegram Bot API             wallet, no txindex)
                                                          ▼
                                              owner's and wife's phones
```

| part | what it does | read |
|------|--------------|------|
| before you install | prerequisites, the two addresses and four transactions, regtest, `dry_run` and shrunk timings | [doc/0-install.md](doc/0-install.md) |
| bitcoin node | Bitcoin Core on the same machine, used only through RPC | [doc/1-bitcoin-node.md](doc/1-bitcoin-node.md) |
| heirdrop service | the daemon: clock, decision, broadcast, tracking, the socket | [doc/2-heirdrop-service.md](doc/2-heirdrop-service.md) |
| Telegram notifier | messages to the owner and the wife, send-only | [doc/3-telegram-notifier.md](doc/3-telegram-notifier.md) |
| web app | the wife's check-in and the heirs' packages, behind nginx | [doc/4-webapp.md](doc/4-webapp.md) |
| rehearsal | step by step on a regtest VM, then a dry run against mainnet | [doc/5-rehearsal.md](doc/5-rehearsal.md) |
| going live | prerequisites, switching it on, TLS with nginx and Let's Encrypt | [doc/6-go-live.md](doc/6-go-live.md) |

The config files and systemd units to copy are in [doc/init/](doc/init/):

- `bitcoin.conf` and `bitcoind.service`;
- `heirdrop.example.toml` and `heirdrop.service`;
- `heirdrop-web.example.toml`, `heirdrop-web.service` and
  `heirdrop-web-nginx.conf`.

## Built to wait for years

Funds are at stake and the service may run untouched for a long time, so it
is built to be boring:

- **Python 3.11+, standard library only.** No third-party dependencies to
  update or to be compromised through.
- **One process, one thread.** A tick loop plus a socket listener, no locks.
- **Plain files are the only state.** Each tick recomputes everything from
  those files and the clock. Files are replaced atomically, and one-time
  markers (the decision, each alert) are created exclusively. A crash or a
  restart at any point neither repeats nor skips a decision or a broadcast
  (at worst a Telegram message is sent twice).
- **Hold when unsure.** The service will not decide or broadcast in any of
  these cases:
  - before the owner's first check-in;
  - right after a cold start;
  - after a clock jump;
  - while the node is unreachable or not synced;
  - while the system clock and the chain disagree;
  - while a check-in time lies in the future;
  - while a transaction file fails its checksum or pays a wrong address;
  - while its own files contradict each other.

  It alerts instead.
- **Once they have their package, the heirs no longer depend on this
  machine.** The package each heir gets after the decision contains the raw
  signed transactions, so they can broadcast from any wallet or node if the
  server never manages to.
- **Least privilege.** The service runs as its own user under a hardened
  systemd unit. It can read its config and transactions but not change
  them, and it knows callers only by their uid.

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
port. The scrypt password hash makes each login check slow on purpose.

## License

MIT, see [LICENSE](LICENSE).
