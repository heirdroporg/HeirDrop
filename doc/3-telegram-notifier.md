# 3. The Telegram notifier

The service tells the owner and the wife what is happening through a Telegram
bot. It only sends: it never reads messages, so nobody can check in or change
anything through Telegram. Without a `[telegram]` section alerts are only
written to `events.jsonl`, so holds and deadlines are silent. The family
member gets no Telegram messages.

## What is sent

To the owner, on every change and as reminders:

| when | message |
|------|---------|
| cold start or long downtime | hold enforced until a time; later: hold ended |
| any other hold (node, clock, state files, transaction files) | hold enforced, once a day while it lasts; later: hold cleared |
| not armed yet | once a day until the first check-in |
| first check-in | armed, with the next deadline |
| each later owner check-in | recorded, with the next deadline |
| a wife check-in | recorded, and what it does (nothing before the deadline, or selects W) |
| before the warning window | weekly, counted back from the deadline (days 4, 11, ... 53) |
| warning window | daily (days 60 to 73) |
| from the owner deadline | daily, until the decision |
| wife window opens | once |
| decision, broadcast, final | once each |
| transaction problem, failed (re)broadcast | once a day while it lasts |
| the wife's chat refuses messages | once a day while it lasts |

Once a decision is recorded the service goes quiet. The owner then gets only:

- the decision;
- each broadcast;
- a failed broadcast or rebroadcast (once a day while it lasts);
- `final`.

After `final` nothing more is sent. Holds, faults, transaction problems,
check-ins and a refusing wife chat are no longer reported. They still show in
`heirdrop health` and the journal, and a hold still stops the broadcast.

To the wife: nothing before the owner deadline. Then a message once a day
until the decision (the text changes in the wife window and after she has
checked in), then the decision, and `final`. In the tick that records the
decision the daily message is skipped, so she does not read "please check in"
and "the transfer has started" a moment apart. A late check-in after decision
F gets her one more message. When the owner checks in during grace, her
messages simply stop.

Texts are English plain text and never contain txids, addresses or amounts;
those stay behind `heirdrop health`.

## Accepted, retried, missed

"Notified" means that Telegram's `sendMessage` answered `ok: true` with a
message id. Telegram reports nothing about delivery to the phone or reading,
so the alert file records `accepted_at`, not a delivery time. The first wife
grace alert's `accepted_at` starts the grace clock.

An alert Telegram did not accept stays pending and is retried every tick,
with no backoff. The permanent failures are retried as well, because they can
fix themselves once someone acts:

- a 403, because the wife blocked the bot or never pressed Start;
- a 400, for a wrong chat id;
- a 401, for a revoked token.

These limits apply per tick, so a broken network or Telegram cannot stall the
service:

- at most 4 send attempts per tick, owner and wife taking turns;
- after a failure for one recipient, the rest of that tick skips them;
- after a network error, a timeout, an HTTP 5xx or a 429 the tick sends
  nothing more.

After an outage only the newest alert of a repeating kind is sent: ten missed
daily warnings become one, the latest. The older ones are counted as
`missed_alerts` in `health` and never sent. The same goes, once a decision is
recorded, for every pending alert the quiet rule above does not allow. A
"please check in" that could not be sent before the decision never arrives
after it. If the wife's first grace alert is missed this way, the grace clock
starts after `max_alert_wait_hours`, as for a late alert.

A wife chat that refuses messages (403 or 400) raises a daily alert to the
owner. An owner chat that refuses messages cannot tell anyone. It shows only
in `health` (`pending_alerts` with `permanent: true`) and in the journal
(`alert_failed`), so run `notify-test owner` now and then.

## Setting up the bot

1. In Telegram, talk to `@BotFather`: `/newbot`, then pick a name and a user
   name. It answers with the token. Then `/setjoingroups`, choose the bot,
   and `Disable`, so it cannot be added to groups. Keep the token out of
   chats, mails and shell history.
2. The owner and the wife each open the bot in Telegram and press **Start**.
   A bot cannot write to anyone who has not.
3. Store the token on the VPS, as root, in its own file. Open an empty file
   and paste the token in an editor, so it does not end up in the shell
   history:

   ```sh
   install -m 640 -o root -g heirdrop /dev/null /etc/heirdrop/telegram.token
   nano /etc/heirdrop/telegram.token          # one line: 123456789:AA...
   ```

   The config's permission rule applies here too: `root:heirdrop` 0640, or
   owned by `heirdrop` with 0600.
4. Find the two chat ids, once, by hand. This reads the bot's recent updates
   (the Start messages from step 2, which Telegram keeps for 24 hours):

   ```sh
   python3 - <<'EOF'
   import json, urllib.request
   token = open("/etc/heirdrop/telegram.token").read().strip()
   with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates") as r:
       for u in json.load(r)["result"]:
           m = u.get("message") or {}
           print(m.get("chat", {}).get("id"), m.get("from", {}).get("first_name"), m.get("text"))
   EOF
   ```

   If nothing is printed, press Start (or send any message) again and rerun.
   The service itself never calls `getUpdates`.
5. Add the section to `/etc/heirdrop/heirdrop.toml`:

   ```toml
   [telegram]
   bot_token_file = "/etc/heirdrop/telegram.token"
   owner_chat_id = 111111111
   wife_chat_id = 222222222
   # timeout_seconds = 10       # per message, whole request
   ```

6. `validate-config` checks the section and the token file without any
   network call. It warns when `dry_run = false` and Telegram is missing.
   Restart the unit, then send a test message to each person, as the owner:

   ```sh
   sudo -u heirdrop python3 -m heirdrop validate-config --config /etc/heirdrop/heirdrop.toml
   systemctl restart heirdrop
   python3 -m heirdrop notify-test owner
   python3 -m heirdrop notify-test wife
   ```

   `notify-test` sends at once and answers with the message id or Telegram's
   error. It writes no alert file, so it does not change `last_notified_on`.

The token never appears in the journal, the events log, alert files or a
socket answer; error texts carry only the HTTP status and Telegram's
description. Whoever can read the token file can send messages as the bot,
which could be used to mislead the wife, so the file is as sensitive as the
config.

The systemd unit needs no change for Telegram. It allows IPv4 and IPv6
sockets and leaves `/etc/resolv.conf` and the CA certificates readable.

## Upgrading from a version without the notifier

Alert files written before the notifier have `delivered_at` instead of
`accepted_at`. They are read as accepted and rewritten with the new name when
next touched. The first tick after the upgrade catches up on messages that did
not exist before: armed, the latest owner and wife check-ins, and one "hold
cleared" per hold reason found in old alert files. Harmless, but expect a
handful of messages at once.
