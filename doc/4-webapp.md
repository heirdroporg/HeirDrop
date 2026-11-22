# 4. The web app

The wife and the family member use a small web site on the VPS (`webapp/`,
run as `dmweb`). It talks to the service only through the socket, as
`webapp_user`, and asks it again on every page; it keeps nothing of the
service's state. Python standard library only, no JavaScript, English. nginx
terminates TLS in front of it; the app itself listens on loopback only.

## What each person sees

The site opens with a picture and nothing else. Tapping one hidden area of
the picture (`login_area`) opens the login: a dropdown with `WFE` (the wife)
and `FML` (the family member), and a 16-digit password. Tell each person
which label is theirs.

- After a wrong password the page says "Not accepted. Try again later." and
  keeps the choice.
- When the account is locked it says "Too many attempts. Try again after
  14:32 UTC.". The OK button is greyed out, and the page reloads itself when
  the lock ends.
- Before the login the browser tab shows the neutral `title` from the config;
  after it, `WIFE` or `FAMILY`.

A browser holds one login at a time: there is one cookie for the site, shared
by all its tabs. Logging in as `FML` in a second tab turns the first tab into
the family member's page at its next reload. To try both accounts side by
side, use a private window or a second browser for one of them.

After the login:

| wife's state | her page |
|--------------|----------|
| `normal` | Nothing to do. |
| `grace`, `wife_window` | Please check in by (end of the wife window), and a button. A confirm page says what the check-in does now: in grace it selects W unless the owner checks in first; in the wife window it decides W at once. After it: "your check-in is recorded" with the time |
| `inheritance` | the transfer to her has started; W1 and her documents |
| `closed` | the family transfer has started; "check in anyway" (changes nothing, records that she was reachable) |
| `false_triggered` | her late check-in is recorded; contact the family member |

The family member sees "Nothing to do." until a decision; the owner deadline
is not shown. After it they see W2, or F1 and F2, and the family documents.

Each transaction shows:

- its hex, also as a `.txt` download;
- its txid and receiving address;
- whether it is sent, confirmed or final;
- a short note on broadcasting it elsewhere.

Holds are never shown. If the wife's date passes without a decision, she
reads that the transfer may be delayed. With `dry_run = true` every page
after the login says "Rehearsal: nothing is real." If the service does not
answer, or answers something unexpected, the page says "Not reachable right
now. Try again later." Times are in UTC. An optional `contact` line from the
config is shown under every page after the login and on the "not reachable"
page.

The family member gets no Telegram message: someone has to tell them when to
look.

## What protects it

- **Passwords and lockout.** The 16-digit passwords are stored as scrypt
  hashes in the config. Lockout is counted per account since its last
  successful login: 10 minutes after the 3rd and 6th wrong guess, an hour
  after the 9th and every 3rd after that. The counters are
  `lockout-wife.json` and `lockout-family.json`. A value that is not in the
  dropdown, only possible with a hand-made request, never logs in and is
  counted in `lockout-unknown.json`.
- **Locked means locked.** A locked account refuses even the right password,
  and the page shows when the lock ends. The counters are files in
  `/var/lib/heirdrop-web`. If they cannot be read or written, the login is
  refused with the plain "Not accepted" answer.
- **Accepted with the dropdown.** Anyone who finds the login sees that there
  are two accounts and their labels. They can lock one out for 10 minutes or
  an hour by guessing wrong; her check-in windows last days.
- **The picture hides the login from a casual visitor only.** The link is in
  the page source.
- **Sessions.** They are signed cookies (`Secure`, `HttpOnly`,
  `SameSite=Strict`) that last `session_hours`, and every form carries a
  CSRF token. Logout clears the cookie in the browser; a copied cookie stays
  valid until it expires.
- **Network.** nginx terminates TLS and refuses every request that does not
  name your host, so a scan of the VPS's address finds nothing. The web app
  listens on loopback only, and its unit allows it no other network.
- **What an account gives.** Whoever gets into the wife's account can check
  in as her; after the owner deadline that selects or decides W. Whoever gets
  into either account can read its package and documents after a decision.
  Keep secrets out of the documents.
- **The host name is public.** It and the certificate appear in certificate
  transparency logs. Pick a neutral name: no "bitcoin", "inheritance" or
  family names.

## Setting it up (Debian 12, as root)

You need:

- a DNS name for the VPS (an `A`/`AAAA` record);
- ports 80 and 443 open;
- a picture (JPEG, PNG or WebP, at most 5 MB).

`dmweb` exists from the service installation
([2-heirdrop-service.md](2-heirdrop-service.md)).

```sh
cd /opt/heirdrop
mkdir -p /etc/heirdrop-web
cp doc/init/heirdrop-web.example.toml /etc/heirdrop-web/web.toml
cp /path/to/picture.jpg /etc/heirdrop-web/picture.jpg
python3 -m webapp new-key /etc/heirdrop-web/session.key
python3 -m webapp new-password wife --config /etc/heirdrop-web/web.toml
```

`new-password` makes a random password and shows it once; write it down for
the person. With `--choose` it asks you for one twice without showing it (16
digits, at least 4 different, not a simple sequence like 0123...). It prints
the `[accounts.wife]` lines: put them into `web.toml` in place of the
placeholder.

Then do the same for the family member. `new-password` compares the new
password with the wife's in `web.toml` and refuses the same one:

```sh
python3 -m webapp new-password family --config /etc/heirdrop-web/web.toml
nano /etc/heirdrop-web/web.toml        # both password lines, title, contact, login_area
chown -R root:dmweb /etc/heirdrop-web
chmod 750 /etc/heirdrop-web
chmod 640 /etc/heirdrop-web/*
sudo -u dmweb python3 -m webapp validate-config
cp doc/init/heirdrop-web.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now heirdrop-web
journalctl -u heirdrop-web -f
```

The `chown`/`chmod` lines come after the last `cp` and edit on purpose. `cp`
gives a new file mode 0644, and the app then refuses to start with
`/etc/heirdrop-web/web.toml is accessible by others` (the file holds the
password hashes). nginx shows only a 502; the reason is in `journalctl -u
heirdrop-web`. After any later copy into `/etc/heirdrop-web`, run the three
lines again, then check and restart:

```sh
ls -la /etc/heirdrop-web       # drwxr-x--- root dmweb; files -rw-r----- root dmweb
sudo -u dmweb python3 -m webapp validate-config    # from /opt/heirdrop: config ok
systemctl restart heirdrop-web
```

Running `validate-config` as `dmweb` also shows that the web app user can
read the config, the session key and the picture. The unit
([`init/heirdrop-web.service`](init/heirdrop-web.service)) loads
`/etc/heirdrop-web/web.toml`, so the file must have exactly that name.

`login_area` is `[x, y, width, height]` in percent of the picture, measured
from its top left corner. It scales with the picture, so the same spot works
on a phone and a desktop. Make it large enough to hit with a thumb.

TLS with nginx and Let's Encrypt (replace `web.example.org` with your name):

```sh
apt install nginx certbot
certbot certonly --standalone -d web.example.org \
  --pre-hook "systemctl stop nginx" --post-hook "systemctl start nginx"
cp /opt/heirdrop/doc/init/heirdrop-web-nginx.conf /etc/nginx/conf.d/heirdrop-web.conf
nano /etc/nginx/conf.d/heirdrop-web.conf    # the host name, 4 places
rm /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
```

certbot renews on its own (`systemctl list-timers | grep certbot`). Through
the saved hooks it stops nginx for a few seconds each time.

## Changing it later

Every change to `/etc/heirdrop-web` needs `systemctl restart heirdrop-web`.

- New password: run `new-password`, replace the line, restart.
- Unlock an account at once: `rm /var/lib/heirdrop-web/lockout-wife.json`
  (or `-family`, `-unknown`).
- Log everyone out: `rm /etc/heirdrop-web/session.key`, run `new-key`, then
  the `chown`/`chmod` it prints, then restart.
- Picture, `login_area`, `title`, `contact`: edit, restart.

## Journal lines

`journalctl -u heirdrop-web` has one `request <method> <path> <status>
<address>` line per request (no query strings, no form fields). It also has:

| line | meaning |
|------|---------|
| `login_ok` | a successful login |
| `login_failed <account> failures=N` | a wrong password |
| `login_locked` | a login attempt on a locked account |
| `login_state_error` | the lockout files are unreadable or unwritable |
| `csrf_refused` | a form without a valid CSRF token |
| `checkin wife at ... state ...` | the wife checked in |
| `checkin_state_changed` | the state moved while she read the confirm page; she is shown the new text, nothing is checked in |
| `download` | a transaction or document was downloaded |
| `service_unavailable <reason>` | the socket did not answer, or answered something unexpected |
| `internal_error` | an unexpected failure in the app |

Passwords, cookies and the session key never appear.
