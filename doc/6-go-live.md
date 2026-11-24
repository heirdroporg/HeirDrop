# 6. Going live

## Prerequisites

You need:

- a DNS name for the VPS (an `A`/`AAAA` record);
- ports 80 and 443 open.

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
