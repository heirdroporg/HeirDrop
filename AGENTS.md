# AGENTS.md

Notes for anyone, human or AI agent, who reviews or changes this code. They
cover what the project is, the rules the code follows, and the decisions
behind it. Many of these look like bugs or omissions at first sight but were
chosen on purpose. Read this before reporting a finding or proposing a
change.

For what the project does and how to set it up, see [README.md](README.md)
and [doc/](doc/).

## What it is

heirdrop is a dead man's switch daemon for a family's Bitcoin. A Linux VPS
runs `bitcoind` and holds four presigned transactions (W1, W2, F1, F2). If
the owner stops checking in, the service decides once between the W pair
and the F pair and broadcasts it. W1 and F1 spend the same coins, as do W2
and F2, so only one pair can ever confirm.

Funds are at stake and the service may run untouched for years. The guiding
principles are:

- prefer simple, restart-proof code;
- when unsure, **hold and alert instead of broadcasting**.

Three people are involved:

- **the owner**, who checks in over SSH;
- **the wife**, who checks in through the web app once the owner is overdue,
  and receives W1;
- **the family member**, who receives W2, or F1 and F2.

## Rules the code follows

- Python 3.11+, standard library only. No third-party packages, ever.
- One process, one thread: a tick loop plus a socket listener. No locks.
- Plain files in the state directory are the only source of truth. No
  derived state is stored: `logic.derive()` recomputes everything from the
  files and the clock on each tick. UTC timestamps go inside files; mtimes
  are never used.
- Files are replaced atomically (`statefiles.write_atomic`). One-time
  markers use `statefiles.create_exclusive` (temp file plus hard link).
- Only the service writes to its state directory.
- Every alert has a deterministic id (kind, the deadline it belongs to, day
  index) and is created exclusively, so restarts never duplicate alerts.
- Alerts go through the `Notifier` protocol in `heirdrop/notifier/`:
  Telegram when `[telegram]` is configured, else the events-log backend. The
  bot token never appears in logs, alert files, socket answers or error
  texts.
- Neither txindex nor a wallet is assumed on bitcoind. The RPC calls used
  are listed in `node.py`'s docstring, and that list is kept accurate.
- Hold rather than broadcast on any doubt. Hold reasons are plain strings
  passed into `derive()` as `faults`.

## Layout

### Service (`heirdrop/`)

- `clock.py`: injectable clock, UTC parse/format.
- `config.py`: TOML load, defaults, validation; unknown keys refused. The
  config file must be root-owned with mode up to 0640, or 0600 otherwise.
- `statefiles.py`: atomic primitives, `StateStore`, consistency checks.
- `logic.py`: pure derivation of deadlines, phase, decision due, alerts and
  views.
- `node.py`: the JSON-RPC client. `txcheck.py`: transaction validation.
- `notifier/`:
  - `__init__.py`: the protocol, `NotifyError`, `build`;
  - `eventslog.py`;
  - `telegram.py`: `sendMessage` over `http.client`;
  - `messages.py`: the texts per kind;
  - `queue.py`: pure; sendable/missed, interleave, health summary.
- `service.py`: the tick: gaps and pauses, node checks, decision, broadcast,
  tracking.
- `server.py`: Unix socket server (peer uid to role), request handlers, and
  the `request()` client.
- `cli.py` and `__main__.py`: `python3 -m heirdrop
  run|validate-config|checkin|status|health|notify-test|package|document`.

### Web app (`webapp/`)

It imports nothing from `heirdrop`; a test checks this.

- `config.py`: strict TOML, unknown keys refused, the same permission rule.
- `passwords.py`: 16-digit passwords, scrypt hashes.
- `auth.py`: signed session cookies, CSRF tokens, lockout counters.
- `files.py`: UTC timestamps, atomic JSON files.
- `client.py`: the socket client; `Unavailable`/`Refused`.
- `pages.py`: HTML, pure.
- `app.py`: routing from `Request` to `Response`, no HTTP.
- `server.py`: the `http.server` adapter, one request per connection.
- `cli.py` and `__main__.py`: `python3 -m webapp run|new-password
  wife|family [--choose]|new-key|validate-config`.

### Other files

- `doc/`: docs 0 to 6: install, node, service, notifier, web app,
  rehearsal (regtest, then the mainnet dry run, with how to sign and check
  the real transactions) and going live (switching on, TLS, keeping it safe
  and running).
- `doc/init/`: `bitcoin.conf`, the systemd units (`bitcoind.service`,
  `heirdrop.service`, `heirdrop-web.service`), `heirdrop-web-nginx.conf`,
  the two example configs and `picture.png`, the shipped front-page picture.
- `tests/`: unittest. `fakenode.py` is a fake bitcoind, `faketelegram.py` a
  fake Telegram, and `env.py` a full test environment.

## Design decisions

### Clock, grace and the wife window

- Grace views, and the window in which a wife check-in selects W
  (`w_selected_at`), open at the owner deadline. The grace *end* is computed
  from the time Telegram accepted the first wife grace alert
  (`accepted_at`), capped by `max_alert_wait`.
- Downtime is recorded in an append-only pause ledger (`pauses.jsonl`).
  All durations are "running time", so pauses also extend grace and the
  wife window. A pause entry may carry `hold_until`. The cold-start hold is
  derived from it, so a restart during a hold cannot skip it.
- Overlapping pause entries count once (`logic.advance` merges them). A tick
  that fails after recording a pause leaves `last_tick` behind, so later
  ticks record the same downtime again; the ledger keeps those lines.
- A last line of `pauses.jsonl` without a newline is a crashed append. The
  reader ignores it, and `record_pause` drops it before appending (logged as
  `pauses_repaired`). This is safe because that tick never wrote
  `last_tick`, so the gap is recorded again. A complete bad line is still a
  state error.
- Gap detection runs every tick, not only at start, as a defence against
  clock jumps.
- The service is unarmed until the first owner check-in (`armed.json`).

### The decision

- The decision is one pure rule. If there is no decision yet and grace has
  ended:
  - **W**, if a wife check-in exists at or after the owner deadline;
  - else **F**, once the wife window has ended.

  A late wife check-in during a hold still yields W.
- The wife has no clock of her own: no `wife_period_days`, no wife warning
  or overdue alerts. She enters at the owner deadline: her first alert comes
  then, and only a check-in at or after it counts. An earlier check-in is
  stored and ignored.
- The wife view's `deadline` is `wife_window_end` from the owner deadline
  until the decision, else null. The owner view has no wife deadline.
- The wife view mirrors the owner's `grace` and `wife_window` states. It
  has an extra state, `closed`, after F until she checks in
  (`false_triggered`). The family view is `inheritance` after either
  decision.

### Broadcast and tracking

- The broadcast marker is written after a successful send; a rebroadcast is
  idempotent. Dry-run markers are resent for real if `dry_run` is later
  turned off, which is why the docs say to wipe the state before going
  live.
- Confirmations work on a pruned node without txindex:
  - `gettxout` on our own output until the first confirmation;
  - then the block hash is recorded and confirmations come from
    `getblockheader`.

  The block height comes from the `bestblock` in the `gettxout` answer.
- Spent inputs and node rejection alert the owner but do not hold: those
  are exactly the cases in which the heirs need the raw transactions.
- Validation passes `max_fee_rate` to `testmempoolaccept`, as the broadcast
  does to `sendrawtransaction`, so both judge the fee the same way.
- After `final` the transactions are no longer tracked, so a later reorg
  goes unnoticed. That is the meaning of final.

### Node answers are untrusted in shape

- A missing field or a wrong type (`service.BAD_ANSWER`) counts as
  `node_unreachable`, and an aborted validation run holds the same way.
- A failure while broadcasting or tracking is logged (`node_error`) and
  retried next tick.
- No node answer may stop a tick before `write_last_tick`.
- `RpcClient` bounds each answer in size and total time. An HTTP error
  status without an RPC error, or an answer without `result`, is
  `NodeUnreachable`: a missing result must not read as `null` ("not found"
  for `gettxout`).
- The first tick goes through `safe_tick` like every other. A malformed
  alert file is a `state_files` hold, also at start.

### Process and socket

- `run` holds an exclusive `flock` on `<state_dir>/lock` for the life of the
  process and refuses euid 0. The lock is taken in `cli.cmd_run`, not in
  `Service`, because tests build several services on one directory.
- The main loop is `Server.serve_forever`: `selectors` on the listening
  socket, with the time to the next tick as timeout. SIGTERM and SIGINT
  arrive through a wakeup fd.
- The socket carries one JSON request and one response per connection.
  - The owner uid may check in as owner and read everything, including
    packages at any time.
  - The webapp uid checks in as wife and gets the wife and family views. It
    gets packages and documents only in the matching inheritance state: the
    wife W1 after W, the family W2 or F1+F2 after a decision.
  - Root and unknown uids are refused.
- The socket mode is 0666; the uid check is the only authorization. An
  unknown uid is refused before anything is read (journal line only), and
  `RECV_TIMEOUT` bounds the whole request line, not each recv.
- Packages include the raw hex even during holds. If no validation ran in
  this process, the hex is read from the file (sha256-checked,
  `txcheck.read_tx_file`).
- `Service.checkin` catches a failing tick and answers ok with a `warning`,
  because the check-in is already on disk.
- Internal errors answer a bare `internal error`; the exception goes to
  `events.jsonl`.

### Configuration and permissions

- `/etc/heirdrop` is root-owned: `root:heirdrop`, directories 0750, files
  0640. `config.permission_problem` accepts that layout, or a file owned by
  another user with mode 0600. The service can read its config and
  transactions but cannot change them.
- The systemd units do not use `ConfigurationDirectory=`, which would chown
  the hand-made `/etc/heirdrop` (and `/etc/heirdrop-web`) to the service
  user.
- `validate-config` makes no network call and reports a configured
  interface user that does not exist.

### Telegram notifier

- The owner and the wife are notified separately through one bot, with
  English texts and no txids, addresses or amounts. The notifier is
  outbound only: no `getUpdates`, no commands or check-ins through
  Telegram. The family member gets nothing.
- "Notified" means `sendMessage` answered `ok: true`. The alert field is
  `accepted_at`; old files with `delivered_at` are still read. The grace
  clock anchor is the `accepted_at` of the first wife grace alert.
- The wife's schedule: her first alert at the owner deadline, then daily
  through grace and the wife window. There is no all-clear message to her
  when the owner checks in.
- The owner's schedule:
  - daily in the warning window, from the owner deadline, and while a hold
    lasts;
  - a weekly reminder before the warning window only (`owner-weekly`,
    counted back from the deadline, phase normal);
  - a message on every state change: armed, check-ins (owner and wife, with
    their effect), wife window opened, hold and fault cleared, wife chat
    broken.

  "Cleared" alerts are derived from existing alert ids.
- **Quiet after the decision.** Once a decision exists, only `decision`,
  `broadcast`, `broadcast_failed`, `rebroadcast_failed`, `false_triggered`
  and `final` are raised or sent (`logic.AFTER_DECISION`). After `final` the
  two failure kinds end too (`logic.AFTER_FINAL`). A hold still blocks the
  broadcast. Any other alert still pending at the decision counts as missed
  (`queue.sendable(alerts, logic.allowed_kinds(facts))`), so a stale "please
  check in" never follows the decision. A `state_files` tick reads no facts
  and is not quieted.
- **Delivery** happens every tick, with no backoff, permanent failures
  included:
  - at most `MAX_SENDS_PER_TICK` sends;
  - owner and wife take turns, wife first;
  - a failed recipient is skipped for the rest of the tick;
  - a transport failure (network, timeout, 5xx, 429) stops sending for the
    tick;
  - 400/401/403/404 are permanent: shown in `health`, still retried.
- **After an outage** only the newest alert of a series is sent; the older
  ones are "missed", judged against all alerts, accepted ones included. A
  series is:
  - `(to, kind)` for warning, weekly and grace alerts;
  - the id without its last part for the other repeating kinds;
  - the alert itself otherwise.
- The token is in its own file (`bot_token_file`), with the same permission
  rule as the config, and must match `digits:letters`. `notify-test
  owner|wife` (owner only) sends through the running service and writes no
  alert file.
- `health` has `notifications` (the backend, and per recipient
  `last_notified_on`, `last_kind`, `last_id`, `pending`), `pending_alerts`
  and `missed_alerts`.

### Web app

- It lives in `webapp/` and is deployed with the same `/opt/heirdrop`
  checkout. It runs as `dmweb` (`webapp_user`) under the same rules as the
  service: stdlib only, one process, one thread.
- It imports nothing from `heirdrop`. The socket protocol is the boundary,
  so the permission rule and the socket client are small deliberate copies.
- The socket is the only source of truth: every request asks it again, and
  nothing about the service's state is stored. The app's own files are its
  config in `/etc/heirdrop-web` (root:dmweb 0750/0640, also the session key
  and the picture) and the lockout counters in `/var/lib/heirdrop-web`.
- Any doubt (socket down, an answer of the wrong shape, an unknown state)
  shows "Not reachable right now. Try again later."; details go only to the
  journal.
- Public HTTPS goes through nginx and certbot (Debian packages, certbot in
  standalone mode). The app listens on loopback only.
- The front page shows only the owner's picture, with no text and a neutral
  `<title>`. A transparent link over `login_area` (percent of the picture)
  opens `/login`. Nothing before the login names the site's purpose. After
  the login the browser tab says `WIFE` or `FAMILY` (`app.TITLES`), on every
  page including "not reachable".
- The login form has a dropdown `choose…` / `WFE` (wife) / `FML` (family)
  instead of a name field (`auth.CHOICES`). A wrong password says "Not
  accepted. Try again later." and keeps the choice. A lock says "Too many
  attempts. Try again after HH:MM UTC.", disables the OK button, and reloads
  `/login` by meta refresh when it ends. There is no warning before a lock.
- There are two accounts, `wife` and `family`, with 16-digit passwords and
  no TOTP; the two passwords must differ.
- Lockout is counted per account since the last success:
  - 10 minutes at the 3rd and 6th failure;
  - 1 hour at the 9th and every 3rd after that.

  Any value not in the dropdown shares the `unknown` counter and never logs
  in. Nothing is counted while locked. Unreadable or unwritable counters
  refuse the login.
- The session cookie is stateless and signed, 8 hours by default, and
  shared by all tabs of a browser, so the last login wins. Every form has a
  CSRF token. Logout clears the browser cookie only.
- The wife sees one date ("check in by", the wife view's `deadline`). Holds
  are never shown, only "may be delayed" once her date has passed. There is
  no button in `normal`, and "check in anyway" in `closed`. The confirm
  page states the current effect and the form carries that state; if the
  state has changed, the page shows the new text instead of checking in.
- The family member sees "Nothing to do." in `standby` and `alert` alike,
  and the package after a decision.
- A package shows hex (text box and `.txt`), txid, receiving address and
  status, and no amounts. Downloads are numbered (`/tx/<n>.txt`,
  `/doc/<n>`), only in `inheritance`, and only from the person's own
  package.
- The design is minimal: English, UTC times, no JavaScript, and one
  stylesheet allowed by its hash in the CSP. A rehearsal banner shows when
  `dry_run` is on, plus one optional `contact` line.

## Accepted risks and known limitations

These were looked at and accepted. None of them can cause a wrong or early
broadcast. Please do not report them as new findings unless you see a way
they could.

### Trust and access

- **The node is trusted with all four raw transactions before a decision.**
  Validation sends them daily, and the RPC port is not authenticated, so
  whoever controls bitcoind, or listens on its port while it is down, could
  broadcast a pair early. Documented in doc/1-bitcoin-node.md. Two
  alternatives were considered and not chosen: a privileged RPC port for
  bitcoind, and decoding locally and sending nothing before a decision.
- If `sendrawtransaction` returns a txid that differs from the decoded one,
  the returned one is adopted. The node is trusted, and both come from it
  anyway.
- A config rewrite cannot redirect funds: the transactions are presigned
  and the signing keys are not on the machine.

### Socket and web app

- A client that does not read a large answer can block the loop for up to
  60 s (`SEND_TIMEOUT`). This needs an answer larger than the socket buffer,
  so in practice only the owner with a document, or the web app after a
  decision.
- Web app: logout only clears the browser cookie, so a copied cookie stays
  valid until it expires.
- Web app: the login page shows that two accounts exist, and a stranger can
  keep one locked by guessing, an hour at a time. The wife still has grace
  and the wife window, which last days.
- Web app: one thread, so a slow request holds the others. nginx buffers
  requests and answers.
- Web app: there is no notification to the owner about logins.

### Notifier

- A message can be sent twice after a crash between Telegram's answer and
  the update of the alert file (`service._send`).
- An owner chat that refuses messages cannot be reported to anyone; it
  shows in `health` and the journal only.
- A hold that clears and returns twice on one day is reported once.
- A tick waits for each send attempt, and the DNS lookup has no timeout of
  its own.

### Files and code

- State file modes depend on the unit's `UMask=0077`, and
  `StateStore.prepare` does not check an existing directory.
- `events.jsonl`, `pauses.jsonl` and `alerts/` grow without bound, and all
  alert files are read on each tick.
- `doc/init/bitcoind.service` is much less hardened than the heirdrop unit.
- There is no sanitizing in `alert_path`, and temp names in `_write_temp`
  are predictable. Only the service writes there.
- `Service.run_forever` is dead code; the loop is `Server.serve_forever`.

### Out of scope unless asked

Inbound Telegram, other languages, pruning `alerts/`, and backoff.

## Testing

```sh
python3 -m unittest discover -s tests -t .
```

- Service tests use a 1-day tick interval and a 72 h cold-start gap
  threshold, so a 120-day timeline is 120 ticks. Tests must tick daily
  (`env.run_days`); skipping days is itself a "pause" and shifts deadlines.
- Peer credentials on the socket are Linux `SO_PEERCRED`. macOS needs the
  `LOCAL_PEERCRED` fallback, and tests inject a fake credential resolver.
  systemd can only be exercised on Linux.
- Web app tests (`tests/test_webapp_*.py`) need no service. `FakeService` in
  `test_webapp_app.py` answers like the socket, `test_webapp_server.py` runs
  the real `HTTPServer` on a loopback port in a thread, and the socket
  client is tested against a real Unix socket. Test passwords must pass
  `passwords.weakness` (no simple sequences). scrypt makes each login check
  take about 0.05 s; the whole suite takes about 35 s.
- `RpcClient` (`tests/test_node.py`) and Telegram's `http_post` are tested
  against a one-shot server in a child process, not a thread. `fakenode.py`
  applies bitcoind's `maxfeerate` (0.10 BTC/kvB when none is given, 0 = no
  cap).
- Never let a result depend on the order of `glob`/`iterdir`: macOS lists
  alphabetically, Linux does not. The alert directory is read sorted for
  this reason.
- Some permission tests behave differently as root (a root-owned file may be
  group-readable). Use `helpers.owned_by_non_root`, and patch `os.geteuid`
  where `run` would refuse root before the step under test.
- Run the suite on both macOS and Linux before a deployment.

## Operational notes

- Commands run from `/opt/heirdrop`: `python3 -m heirdrop` and `python3 -m
  webapp` find their packages through the current directory.
- On regtest, bitcoind creates its datadir 0700; `chmod 750` is needed so
  the `bitcoin` group can read the cookie. Regtest wallets need
  `load_on_startup`.
- `cp` gives new files mode 0644, which the config loaders refuse. Fix the
  modes after every copy into `/etc/heirdrop` or `/etc/heirdrop-web`.
- Backups include the state directory, taken from a disk snapshot or with
  the service stopped. After a loss, restore the newest copy and have the
  owner check in. Never restore a state from before going live (its dry-run
  markers would be sent for real), and never run an armed setup on an empty
  state directory: it is unarmed and waits for the owner's first check-in,
  so after a decision nothing would happen.
- The service does not check how the transactions pair up (W1 and F1 on one
  coin, W2 and F2 on the other); doc 5 has the manual check.

## Working on this code

- Keep changes small and explain them; when unsure, hold rather than
  broadcast.
- Keep the docs in `doc/` and this file in step with the code, in the same
  commit.
- Any new RPC call goes into `node.py`'s docstring and
  doc/1-bitcoin-node.md.
- Any text a visitor of the web app or a recipient of a Telegram message
  sees must not name the site's purpose before the login, and must never
  carry txids, addresses or amounts in Telegram messages.
