"""HTML of every page. Pure functions: data in, a string out.

Rules: no JavaScript, one stylesheet (inline, allowed by its hash in the
CSP), every dynamic value escaped. English, plain words. Times in UTC.
Nothing before login names what this site is about.
"""
from __future__ import annotations

import base64
import hashlib
import math
from datetime import datetime, timedelta
from html import escape

from . import files

BASE_CSS = """
*{box-sizing:border-box}
body{margin:0;font:18px/1.5 system-ui,sans-serif;color:#111;background:#fff}
main{max-width:40rem;margin:0 auto;padding:1.5rem 1rem}
h1{font-size:1.4rem;margin:0 0 1rem}
p{margin:0 0 1rem}
.banner{background:#fde68a;padding:.5rem 1rem;text-align:center;font-weight:600}
.note{color:#555;font-size:.95rem}
.box{border:1px solid #ccc;border-radius:.4rem;padding:1rem;margin:0 0 1rem}
textarea{width:100%;height:7rem;font:13px/1.3 ui-monospace,monospace;word-break:break-all}
code{word-break:break-all;font-size:.9rem}
label{display:block;margin:0 0 .25rem}
input,select{font:inherit;width:100%;padding:.5rem;margin:0 0 1rem}
button{font:inherit;padding:.6rem 1.4rem;border:0;border-radius:.4rem;background:#1f4f8f;color:#fff}
button.quiet{background:#666}
button:disabled{background:#aaa}
form.inline{display:inline}
footer{max-width:40rem;margin:0 auto;padding:0 1rem 2rem;color:#555;font-size:.95rem}
.front{margin:0;background:#000}
.pic{position:relative;line-height:0}
.pic img{width:100%;height:auto;display:block}
.pic a{position:absolute;display:block}
"""


def stylesheet(login_area: tuple[float, float, float, float]) -> str:
    x, y, w, h = login_area
    return BASE_CSS + f".pic a{{left:{x:g}%;top:{y:g}%;width:{w:g}%;height:{h:g}%}}\n"


def csp(css: str) -> str:
    digest = base64.b64encode(hashlib.sha256(css.encode()).digest()).decode()
    return ("default-src 'none'; img-src 'self'; style-src 'sha256-" + digest + "'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")


def when(dt: datetime) -> str:
    return dt.astimezone(files.UTC).strftime("%d %B %Y, %H:%M UTC")


def _doc(title: str, css: str, body: str, body_class: str = "", head: str = "") -> str:
    cls = f' class="{body_class}"' if body_class else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="robots" content="noindex,nofollow">{head}'
            f"<title>{escape(title)}</title><style>{css}</style></head><body{cls}>{body}</body></html>")


# ---- before login ------------------------------------------------------------

def front(title: str, css: str) -> str:
    """Only the picture; one area of it links to the login."""
    return _doc(title, css, '<div class="pic"><img src="/p" alt=""><a href="/login" tabindex="-1"></a></div>',
                "front")


def login(title: str, css: str, choices: tuple, refused: bool = False, chosen: str | None = None,
          locked_until: datetime | None = None, now: datetime | None = None) -> str:
    """The login form. `choices` are the dropdown labels, `chosen` the one to
    keep selected after an attempt. With `locked_until` (and `now`) the
    account is locked: the time it ends, the OK button disabled, and a reload
    of /login once it has ended (a meta refresh: no JavaScript)."""
    head, msg, disabled = "", "<p>Not accepted. Try again later.</p>" if refused else "", ""
    if locked_until is not None and now is not None and locked_until > now:
        # rounded up to the minute, so "after" holds; a lock lasts an hour at most
        shown = locked_until.astimezone(files.UTC)
        if shown.second or shown.microsecond:
            shown = shown.replace(second=0, microsecond=0) + timedelta(minutes=1)
        msg = f"<p>Too many attempts. Try again after {shown:%H:%M} UTC.</p>"
        head = f'<meta http-equiv="refresh" content="{math.ceil((locked_until - now).total_seconds()) + 1};url=/login">'
        disabled = " disabled"
    options = "".join(f'<option value="{escape(c)}"{" selected" if c == chosen else ""}>{escape(c)}</option>'
                      for c in choices)
    return _doc(title, css, (
        f'<main>{msg}<form method="post" action="/login">'
        '<label for="u">Name</label>'
        f'<select id="u" name="account" required><option value="">choose…</option>{options}</select>'
        '<label for="p">Password</label>'
        '<input id="p" name="password" type="password" inputmode="numeric" autocomplete="current-password" required>'
        f'<button type="submit"{disabled}>OK</button></form></main>'), head=head)


# ---- after login ---------------------------------------------------------------

def page(title: str, css: str, content: str, *, csrf: str, dry_run: bool, contact: str) -> str:
    banner = '<div class="banner">Rehearsal: nothing is real.</div>' if dry_run else ""
    foot = f"<p>{escape(contact)}</p>" if contact else ""
    logout = (f'<form class="inline" method="post" action="/logout"><input type="hidden" name="csrf" '
              f'value="{escape(csrf)}"><button class="quiet" type="submit">Log out</button></form>')
    return _doc(title, css, f"{banner}<main>{content}</main><footer>{foot}{logout}</footer>")


def unavailable(title: str, css: str, contact: str) -> str:
    foot = f"<p>{escape(contact)}</p>" if contact else ""
    return _doc(title, css, f"<main><p>Not reachable right now. Try again later.</p>{foot}</main>")


def _button(label: str, href: str) -> str:
    return f'<form method="get" action="{href}"><button type="submit">{escape(label)}</button></form>'


DELAYED = '<p class="note">The transfer may be delayed; there is nothing you need to do.</p>'


def wife_home(view: dict, now: datetime, package_html: str = "") -> str:
    """Her page by her view's state (see the table in doc/4-webapp.md)."""
    state = view["state"]
    deadline = files.parse_ts(view["deadline"]) if view.get("deadline") else None
    checked = files.parse_ts(view["checked_in_at"]) if view.get("checked_in_at") else None
    delayed = DELAYED if deadline is not None and now >= deadline else ""
    if state == "normal":
        return "<p>Nothing to do.</p>"
    if state in ("grace", "wife_window"):
        if checked is not None:
            return (f"<p>Your check-in is recorded ({when(checked)}).</p>"
                    f"<p>Nothing more to do. You will get a message when it is decided.</p>{delayed}")
        by = f" by {when(deadline)}" if deadline else ""
        return f"<p>Please check in{by}.</p>{_button('Check in', '/checkin')}{delayed}"
    if state == "inheritance":
        return f"<h1>The transfer to you has started.</h1>{package_html}"
    if state == "closed":
        return ("<p>The family transfer has started.</p>"
                f"{_button('Check in anyway', '/checkin')}")
    if state == "false_triggered":
        return ("<p>Your check-in after the family transfer is recorded.</p>"
                "<p>Please contact the family member.</p>")
    raise ValueError(f"unknown wife state {state!r}")


CHECKIN_EFFECT = {
    "grace": ("If you check in now, the transfer goes to you. It is decided at the end of the waiting "
              "time, unless the owner checks in first."),
    "wife_window": "If you check in now, the transfer to you is decided at once. This cannot be undone.",
    "closed": ("The family transfer has already started. Checking in changes nothing; it only records "
               "that you were reachable."),
}


def checkin_confirm(state: str, csrf: str) -> str:
    return (f"<p>{CHECKIN_EFFECT[state]}</p>"
            f'<form method="post" action="/checkin"><input type="hidden" name="csrf" value="{escape(csrf)}">'
            f'<input type="hidden" name="state" value="{escape(state)}">'
            '<button type="submit">Check in</button></form>'
            '<p><a href="/">Back</a></p>')


def family_home(view: dict, package_html: str = "") -> str:
    if view["state"] == "inheritance":
        return f"<h1>The transfer has started.</h1>{package_html}"
    if view["state"] in ("standby", "alert"):  # told apart nowhere: as little as possible
        return "<p>Nothing to do.</p>"
    raise ValueError(f"unknown family state {view['state']!r}")


def _tx_status(tx: dict, final: bool) -> str:
    if tx.get("broadcast_at") is None:
        return "Not sent yet."
    if tx.get("dry_run"):
        return "Not sent: rehearsal."
    if final:
        return "Confirmed. Final."
    n = tx.get("confirmations")
    if not n:
        return "Sent, waiting for confirmation."
    return "Sent, 1 confirmation." if n == 1 else f"Sent, {int(n)} confirmations."


def package(pkg: dict) -> str:
    final = pkg.get("final_at") is not None
    txs = pkg.get("transactions") or []
    parts = []
    for i, tx in enumerate(txs, 1):
        head = f"Transaction {i} of {len(txs)}" if len(txs) > 1 else "Transaction"
        hexstr = tx.get("hex")
        if hexstr:
            raw = (f'<textarea readonly aria-label="transaction text">{escape(hexstr)}</textarea>'
                   f'<p><a href="/tx/{i}.txt">Download as a text file</a></p>')
        else:
            raw = "<p>The transaction text is not available. Please contact the family member.</p>"
        txid = f"<p>Transaction id: <code>{escape(tx['txid'])}</code></p>" if tx.get("txid") else ""
        pays = f"<p>Pays to: <code>{escape(tx['pays'])}</code></p>" if tx.get("pays") else ""
        parts.append(f'<div class="box"><p><strong>{head}</strong>. {_tx_status(tx, final)}</p>'
                     f"{raw}{txid}{pays}</div>")
    if txs:
        parts.append('<p class="note">If a transaction is not confirmed after a few days, any Bitcoin node or '
                     "transaction broadcast service can send it: give it the text above, then look up the "
                     "transaction id.</p>")
    docs = pkg.get("documents") or []
    if docs:
        items = "".join(f'<li><a href="/doc/{i}">{escape(d["name"])}</a></li>' for i, d in enumerate(docs, 1))
        parts.append(f"<h1>Documents</h1><ul>{items}</ul>")
    return "".join(parts)
