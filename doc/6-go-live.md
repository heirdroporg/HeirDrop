# 6. Going live

## Prerequisites

You need:

- a DNS name for the VPS (an `A`/`AAAA` record);
- ports 80 and 443 open;
- the four mainnet transactions, signed and checked in the mainnet dry run
  ([5-rehearsal.md](5-rehearsal.md), section 2);
- the VPS hardened, and a plan for keeping it safe and running ("Keeping it
  safe and running" below).

## Reset the state directory first

`dry_run = true` does everything except the final `sendrawtransaction`:
validation against the node, holds, alerts, the decision, and a
`broadcast_<name>.json` marker flagged `dry_run`. If the setting is later
turned off with such markers present, the service sends them for real on the
next tick. So the state directory must be emptied before `dry_run` is turned
off. Steps 1 to 3 below do that.

## Switching it on

1. Stop the service and empty the state directory (not the documents):

   ```sh
   systemctl stop heirdrop
   rm -rf /var/lib/heirdrop/*
   ```

2. Restore the real `[timing]`, set `dry_run = false`, and use the real bot's
   token file and chat ids in `[telegram]`. Then check the config; it warns
   if Telegram is missing:

   ```sh
   cd /opt/heirdrop
   sudo -u heirdrop python3 -m heirdrop validate-config --config /etc/heirdrop/heirdrop.toml
   ```

3. `systemctl start heirdrop`, then wait out the cold-start hold.
4. Check in as owner. `status` must show `normal` with no holds, and
   `health` must show every transaction `ok`.
5. Run `notify-test owner` and `notify-test wife`, and confirm with the wife
   that the message arrived.
6. Set up the web app ([4-webapp.md](4-webapp.md)):
   - make real passwords for this, and give them to the wife and the family
     member on paper, with the address and where to tap;
   - set up TLS with the real host name ("TLS with nginx and Let's Encrypt"
     below);
   - have each of them log in once on their own phone and see "Nothing to
     do." with no rehearsal banner.

## TLS with nginx and Let's Encrypt

Replace `web.example.org` with your host name:

```sh
apt install nginx certbot
certbot certonly --standalone -d web.example.org \
  --pre-hook "systemctl stop nginx" --post-hook "systemctl start nginx"
cp /opt/heirdrop/doc/init/heirdrop-web-nginx.conf /etc/nginx/conf.d/heirdrop-web.conf
nano /etc/nginx/conf.d/heirdrop-web.conf    # the host name, 4 places
rm /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
curl -s https://web.example.org/ | head -c 200            # the front page
curl -sk https://<the VPS's IP address>/ ; echo "exit $?"  # refused: not our host name
```

The first `curl` shows the front page, which also shows that the web app's
unit (`IPAddressAllow=localhost`) lets nginx in. The second fails with a TLS
error: a scan of the VPS's address finds nothing.

certbot renews on its own (`systemctl list-timers | grep certbot`). Through
the saved hooks it stops nginx for a few seconds each time.

## Keeping it safe and running

The service and the web app run under hardened systemd units: a read-only
system, no privileges, no capabilities. The machine around them is up to you,
and most of it is ordinary Linux operations.

**Hardening the VPS (before you switch on).** SSH with a key only, no password
and no root login; a firewall open only for 22, 80 and 443; unattended
security updates. The Securing Debian Manual and your provider's "initial
server setup" guide cover the details.

**Backups.** The signing keys are not on the server. If you lose the four hex
files, you can sign them again with the keys. Back up:

- `/etc/heirdrop`: the config, the Telegram token and the four transactions
  in `txs/`;
- `/etc/heirdrop-web`: the web app's config with the password hashes, the
  session key and the picture;
- `/var/lib/heirdrop-docs`: the documents for the heirs;
- `/var/lib/heirdrop`: the state. Take it from a disk snapshot or with the
  service stopped, so the files belong together.

These copies hold secrets (the bot token, the session key, the signed
transactions and the heirs' documents), so encrypt them. A provider snapshot
gives all of it to the provider.

After a loss, restore the newest copy of the state, then check in as the
owner at once. An older copy has an older check-in and so an earlier deadline,
and a restart after more than six hours down begins with a cold-start hold. Never restore a state from
before going live: its dry-run markers would be sent for real. And never
start with an empty state directory once the service is armed: an empty state
is unarmed and waits for the owner's first check-in, so after a decision
nothing would ever happen again. Test one restore.

**Monitoring.** Holds and problems reach the owner through Telegram. What no
message can report is a VPS or a service that has died: Telegram just goes
quiet. So add an outside check that reaches you, for example an uptime
service that loads the web app's front page. That shows the VPS and the web
app are up, not the service itself, so also look at the service now and then:

```sh
systemctl status heirdrop heirdrop-web
cd /opt/heirdrop
python3 -m heirdrop health
```

**Upgrades.** Review the new commits and run the test suite on a copy first
(`python3 -m unittest discover -s tests -t .`). Then note the current commit
and upgrade:

```sh
cd /opt/heirdrop
git rev-parse HEAD                  # note it, for a rollback
git pull
sudo -u heirdrop python3 -m heirdrop validate-config --config /etc/heirdrop/heirdrop.toml
sudo -u dmweb python3 -m webapp validate-config
systemctl restart heirdrop heirdrop-web
journalctl -fu heirdrop
```

Upgrade Bitcoin Core the way [1-bitcoin-node.md](1-bitcoin-node.md) installs
it, with the signatures checked. While bitcoind restarts, the service holds
and then carries on.

**Rollback.** In `/opt/heirdrop`, `git checkout <the noted commit>`, run both
`validate-config` commands again and restart both units. The state directory
stays as it is. If the newer code wrote state the older code cannot read, the
service holds (`state_files`) and alerts the owner; then roll forward again
instead of touching the state.

### Go-live checklist

- [ ] Mainnet dry run done (doc 5, section 2), with the pairing check and
  every transaction `ok` in `health`.
- [ ] VPS hardened (SSH with a key only, firewall for 22, 80 and 443, updates
  on).
- [ ] Backups encrypted, and one restore tested.
- [ ] Documents for the heirs in place.
- [ ] Real `[timing]` restored, `dry_run = false`, the real bot's token and
  chat ids.
- [ ] State wiped: `systemctl stop heirdrop && rm -rf /var/lib/heirdrop/*`.
- [ ] `validate-config` clean for the service and the web app.
- [ ] Cold-start hold over; checked in as owner; `status` shows `normal` with
  no holds and `health` every transaction `ok`.
- [ ] Owner and wife each received a `notify-test` message.
- [ ] Web app behind nginx with a Let's Encrypt certificate; the wife and the
  family member each logged in once and saw "Nothing to do." with no
  rehearsal banner.
- [ ] Outside monitoring in place, and you know how to roll back.
