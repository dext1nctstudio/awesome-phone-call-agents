"""A loopback-only review console.

The console is where a biller reads what the plan said, sees the words that
support it, and signs off. It has no authentication, so it refuses to be
anything other than local: it will not bind a non-loopback address, it rejects a
request whose ``Host`` is not loopback, and it requires a header on writes that a
cross-site form post cannot set.

The page itself is a shell. Everything a reader sees is fetched from the JSON
endpoints below and written into the document as text rather than as markup, so
a value that came off a phone call cannot become part of the page. It cannot
place a call. Approving a claim is the only state it changes.
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from . import audit, engine, hold, policy, ui
from .models import ANSWERED, NEEDS_HUMAN, UNKNOWN, load_ledger

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
WRITE_HEADER = "X-Trunkline"

# 'self' is needed for the fetch calls that carry the data; nothing else is
# allowed, so the page can neither load nor reach anything off this origin.
CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
       "connect-src 'self'; form-action 'none'; base-uri 'none'")


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def _grounded(fields: Dict[str, Any]) -> bool:
    return bool(fields.get("_grounded"))


def _claim_row(ledger, claim) -> Dict[str, Any]:
    return {
        "id": claim.id,
        "claim_number": claim.claim_number,
        "payer": ledger.payer(claim.payer_id).name,
        "workflow": claim.workflow,
        "billed_amount": claim.billed_amount,
        "filing_deadline": claim.filing_deadline,
        "state": claim.state,
        "has_result": bool(claim.result),
        "grounded": _grounded(claim.result),
    }


def _claim_detail(ledger, claim) -> Dict[str, Any]:
    row = _claim_row(ledger, claim)
    row["result"] = {k: v for k, v in claim.result.items() if not k.startswith("_")}
    row["call_id"] = ""
    for record in reversed(ledger.calls):
        if claim.id in record.claim_ids:
            row["call_id"] = record.id
            break
    return row


def _call_row(ledger, record) -> Dict[str, Any]:
    payer = ledger.payer(record.payer_id)
    return {
        "id": record.id,
        "payer": payer.name,
        "phone": policy.mask_phone(payer.phone),
        "workflow": record.workflow,
        "outcome": record.outcome or record.status,
        "reached": record.outcome in ui.REACHED_OUTCOMES,
        "mode": record.mode,
        "reference_number": record.reference_number,
        "rep_name": record.rep_name,
        "hold_human": hold.format_duration(record.hold_seconds),
        "talk_human": hold.format_duration(record.talk_seconds),
        "total_human": hold.format_duration(record.total_seconds),
        "cost_estimate_usd": record.cost_estimate_usd,
        "claims": len(record.claim_ids),
    }


def _call_detail(ledger, record) -> Dict[str, Any]:
    row = _call_row(ledger, record)
    row["findings"] = list(record.findings)
    row["claims"] = []
    for claim_id in record.claim_ids:
        claim = ledger.claim(claim_id)
        fields = record.per_claim.get(claim_id, {}) or {}
        row["claims"].append({
            "claim_number": claim.claim_number,
            "state": claim.state,
            "grounded": _grounded(fields),
            "fields": {k: v for k, v in fields.items() if not k.startswith("_")},
        })

    # The hold gaps are recomputed here rather than stored, for the same reason
    # they are derived in the first place: the transcript offsets are the record.
    turns: List[Dict[str, Any]] = []
    previous: Optional[int] = None
    for turn in record.transcript:
        offset = int(turn.get("offset_seconds", 0))
        gap = ""
        if previous is not None and offset - previous >= hold.HOLD_GAP_THRESHOLD_SECONDS:
            gap = hold.format_duration(offset - previous)
        turns.append({
            "at": hold.format_duration(offset),
            "speaker": str(turn.get("speaker", "")),
            "text": str(turn.get("text", "")),
            "hold_before": gap,
        })
        previous = offset
    row["transcript"] = turns
    return row


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

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

        def _send(self, code: int, body: bytes,
                  content_type: str = "text/html; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: Any, code: int = 200) -> None:
            self._send(code, json.dumps(payload).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _text(self, code: int, message: str) -> None:
            self._send(code, message.encode("utf-8"), "text/plain; charset=utf-8")

        # -- reads ------------------------------------------------------
        def do_GET(self) -> None:
            if not self._host_ok():
                return self._text(403, "forbidden")
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            path = parsed.path

            if path == "/":
                return self._send(200, ui.shell())

            if not path.startswith("/api/"):
                return self._text(404, "not found")

            ledger = load_ledger(data_dir)

            if path == "/api/graph":
                payload = ui.graph(ledger)
                payload["practice"] = ledger.practices[0].name if ledger.practices else ""
                return self._json(payload)

            if path == "/api/claims":
                claims = ledger.claims
                if query.get("review"):
                    claims = [c for c in claims if c.state in (ANSWERED, NEEDS_HUMAN)]
                    claims = sorted(claims, key=lambda c: (c.state != NEEDS_HUMAN, c.claim_number))
                    return self._json([_claim_detail(ledger, c) for c in claims])
                claims = sorted(claims, key=lambda c: (-c.priority_score, c.claim_number))
                return self._json([_claim_row(ledger, c) for c in claims])

            if path == "/api/calls":
                calls = sorted(ledger.calls, key=lambda c: c.created_at, reverse=True)
                return self._json([_call_row(ledger, c) for c in calls])

            if path == "/api/call":
                try:
                    record = ledger.call((query.get("id") or [""])[0])
                except KeyError:
                    return self._text(404, "not found")
                return self._json(_call_detail(ledger, record))

            if path == "/api/claim":
                try:
                    claim = ledger.claim((query.get("id") or [""])[0])
                except KeyError:
                    return self._text(404, "not found")
                return self._json(_claim_detail(ledger, claim))

            self._text(404, "not found")

        # -- the one write ----------------------------------------------
        def do_POST(self) -> None:
            if not self._host_ok():
                return self._text(403, "forbidden")
            if self.headers.get(WRITE_HEADER) is None:
                return self._text(403, "missing write header")
            if urlparse(self.path).path != "/approve":
                return self._text(404, "not found")
            length = int(self.headers.get("Content-Length", "0"))
            form = parse_qs(self.rfile.read(length).decode("utf-8"))
            claim_id = (form.get("claim_id") or [""])[0]
            ledger = load_ledger(data_dir)
            ctx = engine.Context(data_dir=data_dir, ledger=ledger, actor="console")
            try:
                engine.approve(ctx, ledger.claim(claim_id), note="approved in console")
            except (KeyError, engine.EngineError) as error:
                return self._text(400, str(error))
            self._text(200, "approved")

    httpd = ThreadingHTTPServer((host, port), Handler)
    audit.record(data_dir, actor="console", action="console.started",
                 subject="%s:%d" % (host, port), detail={})
    print("Trunkline console on http://%s:%d  (loopback only, Ctrl-C to stop)" % (host, port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("")
    finally:
        httpd.server_close()
    return 0
