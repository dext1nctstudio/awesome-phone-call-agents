"""A loopback-only review console.

The console is where a biller reads what the plan said, sees the words that
support it, and signs off. It has no authentication, so it refuses to be
anything other than local: it will not bind a non-loopback address, it rejects a
request whose ``Host`` is not loopback, and it requires a header on writes that a
cross-site form post cannot set.

It cannot place a call. Approving a claim is the only state it changes.
"""
from __future__ import annotations

import html
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from . import audit, engine, hold, policy
from .models import ANSWERED, CLOSED, NEEDS_HUMAN, UNKNOWN, load_ledger

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
WRITE_HEADER = "X-Trunkline"

STYLE = """
:root { color-scheme: light dark; --fg:#16181d; --bg:#fbfaf8; --muted:#5d6470;
        --line:#e2e0db; --card:#ffffff; --ok:#1a7f52; --warn:#8a5a00; --bad:#9a2c2c; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e8e6e1; --bg:#15171b; --muted:#9aa1ad; --line:#2b2f36; --card:#1c1f25;
          --ok:#5fd39b; --warn:#e0b153; --bad:#f08b8b; } }
* { box-sizing:border-box; }
body { margin:0; padding:24px 20px 64px; background:var(--bg); color:var(--fg);
       font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
.wrap { max-width:1000px; margin:0 auto; }
h1 { font-size:20px; margin:0 0 4px; letter-spacing:-0.01em; }
h2 { font-size:15px; margin:28px 0 10px; }
.sub { color:var(--muted); margin:0 0 20px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:16px 18px; margin:0 0 14px; }
.row { display:flex; flex-wrap:wrap; gap:10px 22px; align-items:baseline; }
.mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12.5px; }
.badge { display:inline-block; padding:2px 8px; border-radius:999px; font-size:11.5px;
         font-weight:600; border:1px solid var(--line); }
.ok { color:var(--ok); } .warn { color:var(--warn); } .bad { color:var(--bad); }
.hold { font-size:15px; font-weight:600; }
table { border-collapse:collapse; width:100%; font-size:13px; }
th,td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:600; font-size:11.5px; text-transform:uppercase;
     letter-spacing:0.04em; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
.quote { border-left:3px solid var(--line); padding:4px 0 4px 12px; color:var(--muted);
         margin:8px 0 0; font-style:italic; }
.turns { max-height:320px; overflow:auto; border:1px solid var(--line); border-radius:8px;
         padding:10px 12px; background:var(--bg); }
.turn { display:flex; gap:10px; padding:3px 0; }
.turn .t { color:var(--muted); min-width:58px; font-variant-numeric:tabular-nums; }
.gap { color:var(--warn); font-weight:600; padding:5px 0 5px 68px; }
button { font:inherit; padding:6px 14px; border-radius:7px; border:1px solid var(--line);
         background:var(--card); color:var(--fg); cursor:pointer; }
button:hover { border-color:var(--muted); }
a { color:inherit; }
.note { color:var(--muted); font-size:12.5px; margin-top:22px; }
@media (max-width:560px){ body{padding:16px 14px 48px;} .row{gap:6px 14px;} }
"""


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _page(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>%s</title><style>%s</style></head><body><div class='wrap'>%s</div></body></html>"
        % (_esc(title), STYLE, body)
    ).encode("utf-8")


def _state_class(state: str) -> str:
    return {ANSWERED: "ok", NEEDS_HUMAN: "warn", CLOSED: "ok"}.get(state, "")


def render_index(data_dir: str) -> bytes:
    ledger = load_ledger(data_dir)
    calls = sorted(ledger.calls, key=lambda c: c.created_at, reverse=True)
    total_hold = sum(c.hold_seconds for c in calls)

    parts: List[str] = [
        "<h1>Trunkline review console</h1>",
        "<p class='sub'>%s &middot; local only, no authentication, cannot place calls</p>"
        % _esc(ledger.practices[0].name if ledger.practices else "no practice"),
    ]

    parts.append(
        "<div class='card'><div class='row'>"
        "<div><span class='badge'>%d calls</span></div>"
        "<div class='hold'>%s of hold absorbed by the agent</div>"
        "</div></div>" % (len(calls), _esc(hold.format_duration(total_hold)))
    )

    review = [c for c in ledger.claims if c.state in (ANSWERED, NEEDS_HUMAN)]
    parts.append("<h2>Waiting on a person (%d)</h2>" % len(review))
    if not review:
        parts.append("<div class='card'>Nothing to review.</div>")
    else:
        rows = []
        for claim in sorted(review, key=lambda c: (c.state != NEEDS_HUMAN, c.claim_number)):
            payer = ledger.payer(claim.payer_id)
            grounded = claim.result.get("_grounded", False)
            rows.append(
                "<tr><td class='mono'>%s</td><td>%s</td><td>%s</td>"
                "<td class='num'>$%.2f</td><td class='%s'>%s</td>"
                "<td class='%s'>%s</td><td><a href='/claim?id=%s'>open</a></td></tr>"
                % (_esc(claim.claim_number), _esc(payer.name), _esc(claim.workflow),
                   claim.billed_amount, _state_class(claim.state), _esc(claim.state),
                   "ok" if grounded else "bad", "grounded" if grounded else "no evidence",
                   _esc(claim.id))
            )
        parts.append(
            "<div class='card'><table><tr><th>Claim</th><th>Payer</th><th>Workflow</th>"
            "<th class='num'>Billed</th><th>State</th><th>Evidence</th><th></th></tr>%s</table></div>"
            % "".join(rows)
        )

    parts.append("<h2>Calls</h2>")
    if not calls:
        parts.append("<div class='card'>No calls yet.</div>")
    for record in calls[:20]:
        payer = ledger.payer(record.payer_id)
        parts.append(
            "<div class='card'><div class='row'>"
            "<strong>%s</strong><span class='mono'>%s</span>"
            "<span class='badge %s'>%s</span><span>%s</span>"
            "<span class='mono'>ref %s</span><span class='mono'>%s mode</span>"
            "<a href='/call?id=%s'>detail</a></div>"
            "<div class='hold' style='margin-top:8px'>%s</div></div>"
            % (_esc(payer.name), _esc(record.workflow),
               "ok" if record.outcome in ("answered", "not_on_file") else "warn",
               _esc(record.outcome or record.status), _esc(policy.mask_phone(payer.phone)),
               _esc(record.reference_number), _esc(record.mode), _esc(record.id),
               _esc(hold.format_duration(record.hold_seconds) + " on hold of "
                    + hold.format_duration(record.total_seconds)))
        )

    parts.append(
        "<p class='note'>Phone numbers are masked. Transcripts are stored with patient "
        "identifiers already removed; verify with <span class='mono'>trunkline vault-check</span>.</p>"
    )
    return _page("Trunkline review", "".join(parts))


def render_call(data_dir: str, call_id: str) -> Optional[bytes]:
    ledger = load_ledger(data_dir)
    try:
        record = ledger.call(call_id)
    except KeyError:
        return None
    payer = ledger.payer(record.payer_id)

    parts = [
        "<p><a href='/'>&larr; back</a></p>",
        "<h1>%s &middot; %s</h1>" % (_esc(payer.name), _esc(record.workflow)),
        "<p class='sub'>%s &middot; reference %s &middot; representative %s &middot; %s mode</p>"
        % (_esc(policy.mask_phone(payer.phone)), _esc(record.reference_number),
           _esc(record.rep_name), _esc(record.mode)),
        "<div class='card'><div class='hold'>%s</div>"
        "<div class='row' style='margin-top:8px'>"
        "<span>talking %s</span><span>total %s</span>"
        "<span>estimated cost $%.2f</span><span class='badge'>%s</span></div></div>"
        % (_esc(hold.HoldProfile(record.total_seconds, record.hold_seconds, record.talk_seconds,
                                []).receipt()),
           _esc(hold.format_duration(record.talk_seconds)),
           _esc(hold.format_duration(record.total_seconds)),
           record.cost_estimate_usd, _esc(record.outcome)),
    ]

    if record.findings:
        parts.append("<div class='card'><strong class='warn'>Findings</strong><ul>%s</ul></div>"
                     % "".join("<li>%s</li>" % _esc(f) for f in record.findings))

    for claim_id in record.claim_ids:
        claim = ledger.claim(claim_id)
        fields = record.per_claim.get(claim_id, {})
        grounded = fields.get("_grounded", False)
        rows = "".join(
            "<tr><th>%s</th><td class='mono'>%s</td></tr>" % (_esc(k.replace("_", " ")), _esc(v))
            for k, v in sorted(fields.items())
            if not k.startswith("_") and k not in ("claim_number", "evidence_quote")
        )
        quote = fields.get("evidence_quote", UNKNOWN)
        quote_html = ("<p class='quote'>&ldquo;%s&rdquo;</p>" % _esc(quote)) if quote and quote != UNKNOWN else ""
        parts.append(
            "<div class='card'><div class='row'><strong class='mono'>%s</strong>"
            "<span class='badge %s'>%s</span><span class='badge %s'>%s</span></div>"
            "<table style='margin-top:10px'>%s</table>%s</div>"
            % (_esc(claim.claim_number), "ok" if grounded else "bad",
               "evidence found in transcript" if grounded else "no supporting quote",
               _state_class(claim.state), _esc(claim.state), rows, quote_html)
        )

    parts.append("<h2>Transcript</h2><div class='card'><div class='turns'>")
    previous: Optional[int] = None
    for turn in record.transcript:
        offset = int(turn.get("offset_seconds", 0))
        if previous is not None and offset - previous >= hold.HOLD_GAP_THRESHOLD_SECONDS:
            parts.append("<div class='gap'>on hold for %s</div>"
                         % _esc(hold.format_duration(offset - previous)))
        parts.append("<div class='turn'><span class='t'>%s</span><span>%s</span></div>"
                     % (_esc(hold.format_duration(offset)), _esc(turn.get("text", ""))))
        previous = offset
    parts.append("</div></div>")
    return _page("Call %s" % record.id, "".join(parts))


def render_claim(data_dir: str, claim_id: str) -> Optional[bytes]:
    ledger = load_ledger(data_dir)
    try:
        claim = ledger.claim(claim_id)
    except KeyError:
        return None
    payer = ledger.payer(claim.payer_id)
    rows = "".join(
        "<tr><th>%s</th><td class='mono'>%s</td></tr>" % (_esc(k.replace("_", " ")), _esc(v))
        for k, v in sorted(claim.result.items()) if not k.startswith("_")
    )
    approve = ""
    if claim.state == ANSWERED:
        approve = (
            "<form method='post' action='/approve' id='f'>"
            "<input type='hidden' name='claim_id' value='%s'>"
            "<button type='submit'>Approve this answer</button></form>"
            "<script>document.getElementById('f').addEventListener('submit',function(e){"
            "e.preventDefault();fetch('/approve',{method:'POST',"
            "headers:{'Content-Type':'application/x-www-form-urlencoded','%s':'1'},"
            "body:'claim_id=%s'}).then(function(){location.href='/';});});</script>"
            % (_esc(claim.id), WRITE_HEADER, _esc(claim.id))
        )
    body = (
        "<p><a href='/'>&larr; back</a></p>"
        "<h1 class='mono'>%s</h1>"
        "<p class='sub'>%s &middot; %s &middot; $%.2f billed &middot; deadline %s</p>"
        "<div class='card'><table>%s</table></div>%s"
        % (_esc(claim.claim_number), _esc(payer.name), _esc(claim.workflow),
           claim.billed_amount, _esc(claim.filing_deadline), rows, approve)
    )
    return _page("Claim %s" % claim.claim_number, body)


def serve(data_dir: str, host: str = "127.0.0.1", port: int = 8770) -> int:
    if host not in LOOPBACK_HOSTS:
        print("The console has no authentication and refuses to bind %s. Use 127.0.0.1." % host)
        return 2
    if not os.path.exists(os.path.join(data_dir, "ledger.json")):
        print("No ledger at %s. Run `trunkline --data %s init-demo` first." % (data_dir, data_dir))
        return 1

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _host_ok(self) -> bool:
            hostname = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            return hostname in LOOPBACK_HOSTS

        def _send(self, code: int, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain; charset=utf-8")
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path == "/":
                return self._send(200, render_index(data_dir))
            if parsed.path == "/call":
                page = render_call(data_dir, (query.get("id") or [""])[0])
                return self._send(200, page) if page else self._send(404, b"not found", "text/plain; charset=utf-8")
            if parsed.path == "/claim":
                page = render_claim(data_dir, (query.get("id") or [""])[0])
                return self._send(200, page) if page else self._send(404, b"not found", "text/plain; charset=utf-8")
            if parsed.path == "/api/ledger":
                ledger = load_ledger(data_dir)
                summary: Dict[str, Any] = {
                    "calls": len(ledger.calls),
                    "hold_seconds": ledger.hold_seconds_used,
                    "claims": {state: len([c for c in ledger.claims if c.state == state])
                               for state in {c.state for c in ledger.claims}},
                }
                return self._send(200, json.dumps(summary, indent=2).encode("utf-8"),
                                  "application/json; charset=utf-8")
            self._send(404, b"not found", "text/plain; charset=utf-8")

        def do_POST(self) -> None:
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain; charset=utf-8")
            if self.headers.get(WRITE_HEADER) is None:
                return self._send(403, b"missing write header", "text/plain; charset=utf-8")
            if urlparse(self.path).path != "/approve":
                return self._send(404, b"not found", "text/plain; charset=utf-8")
            length = int(self.headers.get("Content-Length", "0"))
            form = parse_qs(self.rfile.read(length).decode("utf-8"))
            claim_id = (form.get("claim_id") or [""])[0]
            ledger = load_ledger(data_dir)
            ctx = engine.Context(data_dir=data_dir, ledger=ledger, actor="console")
            try:
                engine.approve(ctx, ledger.claim(claim_id), note="approved in console")
            except (KeyError, engine.EngineError) as error:
                return self._send(400, str(error).encode("utf-8"), "text/plain; charset=utf-8")
            self._send(200, b"approved", "text/plain; charset=utf-8")

    httpd = ThreadingHTTPServer((host, port), Handler)
    audit.record(data_dir, actor="console", action="console.started", subject="%s:%d" % (host, port), detail={})
    print("Trunkline console on http://%s:%d  (loopback only, Ctrl-C to stop)" % (host, port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("")
    finally:
        httpd.server_close()
    return 0
