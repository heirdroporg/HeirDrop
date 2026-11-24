# 5. Rehearsal

Rehearse everything before trusting it with real coins, in two stages:

1. on a Linux VM with regtest and shrunk timings;
2. on the VPS against mainnet, still with `dry_run = true`.

Going live comes after both: see [6-go-live.md](6-go-live.md).

Start on a VM with bitcoind on the regtest network. Install, in this order:

1. the node ([1-bitcoin-node.md](1-bitcoin-node.md), with `regtest=1`);
2. the service ([2-heirdrop-service.md](2-heirdrop-service.md)), with the
   shortened `[timing]` and the regtest transactions from section 1 below;
3. the web app ([4-webapp.md](4-webapp.md));
4. optionally the notifier ([3-telegram-notifier.md](3-telegram-notifier.md)).
   Without it, alerts only go to the events log.

Then rehearse steps 1 to 7 of section 1. Steps 8 and 9 try the notifier and
the web app on their own.

## 1. Rehearsal on a Linux VM with regtest

Shrunk timings make the whole 120-day story run in under an hour. Use this
`[timing]` in the heirdrop config:

```toml
[timing]
owner_period_days = 0.01            # 14.4 minutes
warning_window_days = 0.003         # 4.3 minutes
owner_grace_days = 0.004            # 5.8 minutes
wife_window_days = 0.004            # 5.8 minutes
cold_start_hold_hours = 0.05        # 3 minutes
cold_start_gap_threshold_hours = 0.02
tick_interval_seconds = 10
confirmations_final = 2
max_clock_skew_hours = 4
max_alert_wait_hours = 0.01
```

### Creating the regtest transactions

The rehearsal uses two funding coins, not four: W1 and F1 spend coin A, W2
and F2 spend coin B. Once one pair confirms, the other can never be
broadcast. It also uses two destinations, not four: W1 pays the wife's
address, while W2, F1 and F2 pay the family address. The service refuses any
transaction paying anything else. Coinbase outputs need 100 blocks of
maturity, hence the 101 blocks.

Run everything in one root shell: `bcli` uses sudo, and the functions and
variables must stay in the same session. `bitcoin.conf` must have `regtest=1`
and `fallbackfee=0.0001`. To make fresh coins later (steps 7 to 9), run it
again but skip its steps 1 and 2: the wallets already exist, so `createwallet`
would fail, and the coinbase is already mature. If the running service
validates between step 8 and the wipe that follows, it holds with a checksum
mismatch; the wipe restarts it with the new checksums and clears that.

```sh
bcli() { sudo -u bitcoin bitcoin-cli -conf=/etc/bitcoin/bitcoin.conf -datadir=/var/lib/bitcoind "$@"; }

# 1. two wallets: signer holds the coins, heirs receives them; load_on_startup
#    keeps them loaded across bitcoind restarts (otherwise: bcli loadwallet signer true)
bcli -named createwallet wallet_name=signer load_on_startup=true
bcli -named createwallet wallet_name=heirs load_on_startup=true

# 2. mine 101 blocks so the first coinbase is mature and spendable
bcli -rpcwallet=signer -generate 101

# 3. two funding coins, one per transaction pair, paid in ONE transaction:
#    two separate payments may spend each other's output (both addresses
#    belong to this wallet), and then one coin is gone
A=$(bcli -rpcwallet=signer getnewaddress)
B=$(bcli -rpcwallet=signer getnewaddress)
bcli -rpcwallet=signer sendmany "" "{\"$A\":1,\"$B\":1}"
bcli -rpcwallet=signer -generate 1

# 4. find txid and vout of each coin; stops with a message if one is missing
utxo() { bcli -rpcwallet=signer listunspent 1 9999 "[\"$1\"]" |
         python3 -c 'import json,sys
d=sys.stdin.read().strip()
u=json.loads(d) if d else sys.exit("listunspent failed, see the error above")
print(u[0]["txid"], u[0]["vout"]) if u else sys.exit("no unspent coin at " + sys.argv[1])' "$1"; }
read TXA VOUTA < <(utxo "$A")
read TXB VOUTB < <(utxo "$B")
if [ -n "$TXA" ] && [ -n "$VOUTA" ] && [ -n "$TXB" ] && [ -n "$VOUTB" ]; then
  echo "A: $TXA:$VOUTA  B: $TXB:$VOUTB"
else
  echo "STOP: a coin was not found. Do not continue; redo step 3."
fi

# 5. the two destinations: the wife's and the family member's "hardware wallets"
WHW=$(bcli -rpcwallet=heirs getnewaddress)
FMHW=$(bcli -rpcwallet=heirs getnewaddress)
echo "whw = \"$WHW\""; echo "fmhw = \"$FMHW\""

# 6. build and sign the four transactions; sign refuses a missing argument
sign() {  # sign <txid> <vout> <address>
  if [ -z "$1" ] || [ -z "$2" ] || [ -z "$3" ]; then
    echo "sign: missing txid, vout or address" >&2; return 1
  fi
  raw=$(bcli createrawtransaction "[{\"txid\":\"$1\",\"vout\":$2}]" "{\"$3\":0.9999}")
  bcli -rpcwallet=signer signrawtransactionwithwallet "$raw" |
    python3 -c 'import json,sys; r=json.load(sys.stdin); assert r["complete"], r; print(r["hex"])'
}
sign "$TXA" "$VOUTA" "$WHW"  > w1.hex   # W1: coin A -> wife
sign "$TXB" "$VOUTB" "$FMHW" > w2.hex   # W2: coin B -> family
sign "$TXA" "$VOUTA" "$FMHW" > f1.hex   # F1: coin A -> family (conflicts with W1)
sign "$TXB" "$VOUTB" "$FMHW" > f2.hex   # F2: coin B -> family (conflicts with W2)

# 7. sanity check, one at a time: each file must be non-empty and report
#    "allowed": true; do not go on to step 8 otherwise
for f in w1 w2 f1 f2; do
  if [ -s $f.hex ]; then bcli testmempoolaccept "[\"$(cat $f.hex)\"]"; else echo "STOP: $f.hex is empty"; fi
done

# 8. install for the service
cp w1.hex w2.hex f1.hex f2.hex /etc/heirdrop/txs/
chown root:heirdrop /etc/heirdrop/txs/*.hex
chmod 640 /etc/heirdrop/txs/*.hex
sha256sum /etc/heirdrop/txs/{w1,w2,f1,f2}.hex   # paste into [transactions]
echo "whw = \"$WHW\""; echo "fmhw = \"$FMHW\""       # paste into [addresses]
```

Put the two addresses and the four checksums printed by step 8 into
`[addresses]` and `[transactions]`. Do not mine the transactions yourself: the
service broadcasts them in rehearsal step 6, and until then the coins must stay
unspent or `health` reports spent inputs.

Then rehearse. Watch `journalctl -fu heirdrop` in one shell, and in another
watch the status as the owner, redrawn every 20 seconds:

```sh
cd /opt/heirdrop
watch -d -n 20 python3 -m heirdrop status
```

or just the essentials on one line:

```sh
watch -d -n 20 "python3 -m heirdrop status --json | python3 -c '
import json,sys; v=json.load(sys.stdin)[\"views\"][\"owner\"]
print(v[\"state\"], v[\"holds\"], \"left:\", v[\"time_left_seconds\"], \"decision:\", v[\"decision\"])'"
```

### Steps 1 to 7: the service

1. **Start.** Start the unit if it is not running yet. Expect `cold_start` and
   a 3-minute hold. Mine a block if `status` shows `node_not_synced`: regtest
   considers itself syncing until it sees a fresh block.
2. **Owner check-in.** Check in as owner. The state goes `normal`, then
   `warning`, then `grace` at the owner deadline; the family view goes to
   `alert`.
3. **The F story.** Let grace end without a wife check-in. The state goes to
   `wife_window`. After it, the journal shows `decision` F and two
   `dry_run_broadcast` lines, and the family `package` shows F1 and F2 with
   hex.
4. **The W story.** To start again, wipe the state directory by running
   `systemctl stop heirdrop && rm -rf /var/lib/heirdrop/* && systemctl start
   heirdrop`, then check in as owner. This time check in as the wife during
   grace (as root: `sudo -u dmweb python3 -m heirdrop checkin wife`); W is
   decided at the end of grace. In another run, check in as owner before grace
   ends: the clock restarts and nothing is decided.
5. **Kills.** Run `systemctl kill -s KILL heirdrop` at various points,
   especially right after `decision` appears. The restart must hold, then
   continue exactly where it was.
6. **Real broadcast.** Set `dry_run = false`. To start again with it, wipe the
   state directory by running `systemctl stop heirdrop && rm -rf
   /var/lib/heirdrop/* && systemctl start heirdrop`, then run the F story
   again. This time `broadcast` lines appear. `bcli -rpcwallet=heirs
   getbalance` grows once you mine blocks (`bcli -rpcwallet=signer -generate 1`
   each). After `confirmations_final` blocks `final` is logged. From then on
   the service no longer tracks the transactions: `health` keeps the last
   confirmation count and a later reorg is not noticed. That is what final
   means.
7. **The reorg.** This needs a run of its own, because the reorg has to
   happen before `final`:
   - raise `confirmations_final` to 6;
   - use fresh coins and transactions (the old ones are spent);
   - to start again, wipe the state directory by running `systemctl stop
     heirdrop && rm -rf /var/lib/heirdrop/* && systemctl start heirdrop`, then
     run the F story to the broadcast;
   - mine one block, wait for the `confirmed` line and take its block hash;
   - run `bcli invalidateblock <hash>`.

   Within a tick or two the journal shows `reorg`. The transactions are back
   in the mempool, so `health` shows 0 confirmations (or `rebroadcast` if the
   node dropped them). Mine again to see `confirmed` in a new block and
   finally `final`. If `status` shows a `node_not_synced` hold after
   `invalidateblock` (bitcoind may still report the old `headers` height),
   mine past the old height to clear it. Never use `invalidateblock` on a
   mainnet node.

Regtest blocks carry the time they were mined. Mine one at least every
`max_clock_skew_hours`, or the service holds with `clock_skew`. The simplest
way is a mainnet-like miner in a second root shell for the whole rehearsal,
one block every ten minutes. It also brings the confirmations in step 6:

```sh
bcli() { sudo -u bitcoin bitcoin-cli -conf=/etc/bitcoin/bitcoin.conf -datadir=/var/lib/bitcoind "$@"; }
while true; do
  bcli -rpcwallet=signer -generate 1 >/dev/null && echo "$(date -u +%H:%M:%S) block $(bcli getblockcount)"
  sleep 600
done
```

Stop it with Ctrl-C. For a faster step 6, mine by hand with
`bcli -rpcwallet=signer -generate 1` whenever you want a confirmation.

### Step 8: Telegram with a real bot

This is a run of its own, after the steps above, with the shrunk timings and
`dry_run = true`: if it is still `false` from step 6 or 7, set it back to
`true`. The coins of steps 6 and 7 are spent, so `health` reports spent
inputs; make fresh transactions with "Creating the regtest transactions"
above to avoid that. You need two Telegram accounts on two phones (owner and
wife), or one phone with a second account. Use a separate bot for the
rehearsal and keep the real one for the VPS.

With shrunk timings a "day" of the schedule is still a real day, because
alert ids count days. So in a one-hour run each daily message (warning,
grace) comes once, and the weekly reminders never come (the whole period is
14 minutes).

1. **Setup.** If the notifier is not set up yet, follow "Setting up the bot" in
   [3-telegram-notifier.md](3-telegram-notifier.md): token file, Start on both
   accounts, chat ids, `[telegram]`, `validate-config`. To start clean, wipe
   the state directory by running `systemctl stop heirdrop && rm -rf
   /var/lib/heirdrop/* && systemctl start heirdrop`.
2. **Test messages.** As the owner, run `python3 -m heirdrop notify-test
   owner` and `... notify-test wife`. Both phones show the test text and the
   answer has a `message_id`. `last_notified_on` in `health` does not move
   for a test: the owner's already shows the cold start messages, and the
   wife's stays `None`. Then try a wrong chat id: set `wife_chat_id` to a
   made-up number, restart, and `notify-test wife` must answer `not accepted:
   HTTP 400: ...`. Put it back.
3. **The F story with messages.** Check in as owner, and let it run to the
   decision as in step 3 above.

   Expected on the owner's phone, in this order:
   - cold start hold;
   - not armed;
   - armed;
   - hold ended;
   - possibly a node hold and its "cleared" (regtest reports
     `node_not_synced` until a fresh block);
   - warning;
   - deadline passed;
   - wife window open;
   - decision F;
   - two dry-run broadcasts.

   On the wife's phone: nothing until the owner deadline, then "the owner has
   not checked in", then the wife-window text, then "the family transfer has
   been started". During the run `health` shows `last_notified_on` moving for
   both, and the journal has an `alert_accepted <id>` line per message.
4. **Wife check-in.** To start again, wipe the state directory by running
   `systemctl stop heirdrop && rm -rf /var/lib/heirdrop/* && systemctl start
   heirdrop`, then check in as owner, and as the wife during grace (`sudo -u
   dmweb python3 -m heirdrop checkin wife`). The owner's phone gets "your wife
   checked in ... pair W is decided at ...", and the wife's next message says
   there is nothing more to do. At the end of grace both get the W decision.
5. **Telegram unreachable.** While the service is in `warning` or `grace`,
   block the service's HTTPS traffic (bitcoind on its own port is not
   affected):

   ```sh
   nft add table inet hdtest
   nft add chain inet hdtest out '{ type filter hook output priority 0; }'
   nft add rule inet hdtest out meta skuid heirdrop tcp dport 443 reject
   ```

   The journal shows `alert_failed <id> network: ...`, and the ticks go on:
   one attempt per tick, each taking at most about `timeout_seconds`.
   `health` lists the alert under `pending_alerts` with `attempts` growing.
   Remove the block with `nft delete table inet hdtest`; within a tick the
   pending messages arrive, the newest of each kind only. With `reject` the
   failure is immediate; to see the timeout instead, use `drop` in the rule.
6. **The wife blocks the bot.** In the wife's Telegram, open the bot and
   choose Block (or Stop and block). At her next message:
   - the journal shows `alert_failed ... HTTP 403: Forbidden: bot was blocked
     by the user`;
   - `health` shows the alert with `permanent: true`;
   - the owner's phone gets "Telegram does not accept messages to your wife".

   Unblock and press Start: the next tick delivers her newest message.
7. **Restart while sending.** Run `systemctl kill -s KILL heirdrop` while
   messages are pending (with the block from step 5 in place), then remove
   the block. After the restart no message may arrive twice, apart from the
   rare kill exactly between Telegram's answer and the alert file update.
8. **No token anywhere.** With the token's secret part (after the colon) in
   `$S`:

   ```sh
   S=$(cut -d: -f2 /etc/heirdrop/telegram.token)
   journalctl -u heirdrop | grep -c -- "$S"
   grep -rc -- "$S" /var/lib/heirdrop | grep -v ':0$'
   ```

   The first prints 0, the second prints nothing.

### Step 9: the web app

On the VM, with the shrunk timings and `dry_run = true`, after the steps
above: if it is still `false` from step 6 or 7, set it back to `true`. The
coins of steps 6 and 7 are spent, so `health` reports spent inputs; make
fresh transactions with "Creating the regtest transactions" above to avoid
that.

1. **Setup.** As root:

   ```sh
   grep webapp_user /etc/heirdrop/heirdrop.toml && id dmweb
   ```

   The web app's unit runs as `dmweb`, so `webapp_user` must be `dmweb`.

   Create test documents, readable by the service through its group:

   ```sh
   echo "for the wife" > /tmp/wife.txt; echo "for the family" > /tmp/family.txt
   install -m 640 -o root -g heirdrop /tmp/wife.txt /var/lib/heirdrop-docs/wife/
   install -m 640 -o root -g heirdrop /tmp/family.txt /var/lib/heirdrop-docs/family/
   ```

   With the web app set up as in [4-webapp.md](4-webapp.md), its
   `validate-config` says `config ok`:

   ```sh
   cd /opt/heirdrop
   sudo -u dmweb python3 -m webapp validate-config
   ```

   and its journal shows `listening on 127.0.0.1:8090`. To start clean, wipe
   the state directory by running `systemctl stop heirdrop && rm -rf
   /var/lib/heirdrop/* && systemctl start heirdrop`, then wait out the cold
   start hold and check in as owner. The web app needs no restart for a wipe.
2. **Reach it.** From your computer, run `ssh -L 8090:127.0.0.1:8090 <your
   login>@<the VPS's or VM's IP address>`, then open http://localhost:8090 in
   Firefox or Chrome (Safari may refuse the `Secure` cookie on plain http).
   Only the picture shows, and the hidden area opens the login. Run directly,
   `curl http://<the VPS's or VM's IP address>:8090` must fail. A page after
   the login that says "Not reachable" right away means the web app cannot
   reach the socket; `journalctl -u heirdrop-web` has the `service_unavailable`
   reason.
3. **Lockout.** Enter three wrong passwords with `WFE`. The first two say
   "Not accepted", and the third "Too many attempts. Try again after ..."
   with the OK button greyed out. The journal has `login_failed wife
   failures=3 locked until ...`. `FML` is not locked. The right password with
   `WFE` is now refused (`login_locked`). Leave the page open: it reloads
   itself when the lock ends, and the login works again. To end the lock at
   once, `rm /var/lib/heirdrop-web/lockout-wife.json`.
4. **The W story.** To start again, wipe the state directory by running
   `systemctl stop heirdrop && rm -rf /var/lib/heirdrop/* && systemctl start
   heirdrop`, then check in as owner, and log in as the wife (`WFE`): the page
   says "Nothing to do." After the owner deadline it says "Please check in by
   ...". Check in through the confirm page: the page says the check-in is
   recorded, and (with the notifier) the owner's phone gets the Telegram
   message. At the end of grace her page shows the transfer, W1 with "Not
   sent: rehearsal.", the `.txt` download and her document. Logged in as
   `FML`, the page shows the transfer, W2 and the family document.
5. **The F story.** To start again, wipe the state directory by running
   `systemctl stop heirdrop && rm -rf /var/lib/heirdrop/* && systemctl start
   heirdrop`, then check in as owner and run without a wife check-in. The
   family page stays "Nothing to do." past the owner deadline, then shows F1
   and F2. The wife's page says the family transfer has started. "Check in
   anyway" leads to "contact the family member", and `status` shows
   `false_triggered`.
6. **Service down.** After `systemctl stop heirdrop`, every page after the
   login says "Not reachable right now" and the journal has
   `service_unavailable`. Start it again: the pages come back without a
   restart of the web app.
7. **No secrets.** `journalctl -u heirdrop-web | grep -c <one of the
   passwords, digits only>` prints 0.

## 2. Rehearsal on the VPS against mainnet

Run steps 1 to 5, 8 and 9 of section 1 with the real transactions, the real
node, shrunk timings and `dry_run = true`. Leave out step 6 (a real broadcast)
and step 7 (`invalidateblock`, never on mainnet). Validation runs against
mainnet for real:

- the checksums;
- the outputs;
- the inputs being unspent;
- mempool acceptance;
- the fee rate against the node's current minimum.

A complete dry F story and a dry W story each take under an hour.
