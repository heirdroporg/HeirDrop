# 0. Before you install

What you need before you start, and how to try everything safely first. The
setup itself is in the numbered documents that follow:
[1-bitcoin-node.md](1-bitcoin-node.md),
[2-heirdrop-service.md](2-heirdrop-service.md),
[3-telegram-notifier.md](3-telegram-notifier.md),
[4-webapp.md](4-webapp.md),
[5-rehearsal.md](5-rehearsal.md) and
[6-go-live.md](6-go-live.md).

## Prerequisites

### The machine

- A Linux VPS with systemd and root access. **Debian 12** is the tested
  distribution, and the commands in these documents are written for it.
  Other systemd distributions with Python 3.11 or newer (for example Debian
  13 or Ubuntu 24.04) should work.
- For testing and the regtest rehearsal, a **Linux VM** on your own computer
  is enough. You need the VPS only for the mainnet dry run and going live.
- **Python 3.11 or newer**, standard library only. Nothing is installed with
  pip. Debian 12 ships Python 3.11.
- `git`, to put the code into `/opt/heirdrop`.
- Enough disk for a pruned Bitcoin Core node. Pruning keeps the block data
  small, but the UTXO set (chainstate) still needs its own space, and the
  first sync takes time.
- An SSH login for the owner (`dmowner` in these documents). The owner checks
  in through it.

### Bitcoin Core

- **Bitcoin Core 28 or newer**, on the same machine, run as its own `bitcoin`
  user. Version 28 is needed for `rpccookieperms`, which lets the service
  read the RPC cookie through the `bitcoin` group.
- Pruned is fine. Neither `txindex` nor a wallet is needed.
- Debian does not package it: download it from bitcoincore.org and verify
  the signatures. See [1-bitcoin-node.md](1-bitcoin-node.md).

### The web app

- To try it locally, nothing more: the app listens on loopback only and is
  reached through an SSH tunnel. See [4-webapp.md](4-webapp.md).
- A **picture** for the front page ships with the project
  (`doc/init/picture.png`). Your own works too: JPEG, PNG or WebP, at most
  5 MB.
- For going live: **nginx** and **certbot** from Debian's packages (nginx
  terminates TLS), a **DNS name** for the VPS and ports 80 and 443 open. See
  [6-go-live.md](6-go-live.md).

### Telegram

- A **Telegram bot**, made with BotFather, which gives you its token.
- Two Telegram accounts: one for the owner and one for the wife. Each must
  open the bot and press Start once. The family member gets no messages.

Telegram is optional for a rehearsal: without it, alerts only go to the
events log. A live setup needs it, and `validate-config` warns when
`dry_run = false` and Telegram is missing. See
[3-telegram-notifier.md](3-telegram-notifier.md).

## Two addresses and four transactions

The service never holds private keys. It needs two receiving addresses and
four transactions that the owner has signed in advance.

**The two addresses**, from two single-sig hardware wallets, each with its own
seed:

| name | whose | config key |
|------|-------|------------|
| WHW  | the wife's hardware wallet | `[addresses] whw` |
| FMHW | the family member's hardware wallet | `[addresses] fmhw` |

**The four transactions**, spending two coins (UTXOs) A and B:

| transaction | spends | pays |
|-------------|--------|------|
| W1 | coin A | WHW |
| W2 | coin B | FMHW |
| F1 | coin A | FMHW |
| F2 | coin B | FMHW |

W1 and F1 spend the same coin, as do W2 and F2, so only one pair can ever
confirm. The service does not check this pairing; section 2 of
[5-rehearsal.md](5-rehearsal.md) shows how to check it yourself. Each
transaction is a file of hex text in `/etc/heirdrop/txs/`, with its sha256
checksum in `[transactions]`.

The service checks them at start and once a day. A transaction that pays any
address other than its configured one (all outputs, change included), or
whose file does not match its checksum, holds the service. Spent inputs,
mempool rejection and a low fee rate alert the owner. The fee is fixed when
you sign, so see "Fee risk" in [2-heirdrop-service.md](2-heirdrop-service.md).

You do not need real hardware wallets or real coins to try it:
[5-rehearsal.md](5-rehearsal.md), section 1,
"Creating the regtest transactions", makes the two addresses and the four
transactions on regtest with a few `bitcoin-cli` commands. The real ones,
signed with your hardware wallet, are in section 2 of the same doc, "The
mainnet transactions".

## Try it first on regtest

Rehearse the whole thing before it guards real coins. Bitcoin Core's
**regtest** network is a private chain on your own machine: you mine blocks
on demand and the coins are worthless, so a real broadcast, confirmations
and even a reorg can be tried without risk.

[1-bitcoin-node.md](1-bitcoin-node.md) has what the node needs for this:
the regtest lines in `bitcoin.conf`, the regtest cookie path and RPC port
(18443). The fix for the `regtest` directory's mode is in
[2-heirdrop-service.md](2-heirdrop-service.md). The rehearsal itself, step by
step, is section 1 of [5-rehearsal.md](5-rehearsal.md). A Debian 12 VM is
enough for it.

## dry_run

`[safety] dry_run = true` is the default in the example config. The service
then does everything except the final `sendrawtransaction`: it validates the
transactions against the node, holds, sends alerts, decides, and writes a
broadcast marker flagged `dry_run`. Nothing reaches the bitcoin network.

Keep it on until you go live. It is the setting for the mainnet dry run
(section 2 of [5-rehearsal.md](5-rehearsal.md)),
with the real node and the real transactions.

**Reset the state directory before you turn it off.** Markers written with
`dry_run = true` are sent for real on the next tick once it is `false`. See
"Reset the state directory first" in [6-go-live.md](6-go-live.md).

## Shortened timings

The real timings take months: by default the owner deadline is day 74 and
the last decision day 120. For a rehearsal, shrink them. Every `[timing]`
value ending in `_days` or `_hours` accepts a decimal number, not only whole
numbers. The values to use are in [5-rehearsal.md](5-rehearsal.md), section
1. With them the whole 120-day story runs in under an hour. They are the
`[timing]` of the regtest rehearsal and of the mainnet dry run. Restore the
real values before going live ([6-go-live.md](6-go-live.md)).

Telegram messages still count real days, because alert ids count days. So in
a one-hour run each daily message (warning, grace) comes once, and the weekly
reminders never come.
