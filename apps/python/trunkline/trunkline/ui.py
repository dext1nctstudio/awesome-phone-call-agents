"""The console's presentation layer: the pipeline graph, the stylesheet, the client.

The console is a reviewer's tool, so the thing worth showing is not a list of
rows but the path a claim takes and where it is stopped. ``graph`` describes
that path as nodes and edges and hangs the ledger's live counts on it, and the
client draws it. Everything here is static text apart from ``graph``; the
browser fetches the data separately as JSON and renders it with ``textContent``
so no ledger value is ever parsed as markup.
"""
from __future__ import annotations

from typing import Any, Dict, List

from . import hold
from .models import (
    ANSWERED,
    CLOSED,
    NEEDS_HUMAN,
    PENDING_CALL,
    PENDING_RECONCILIATION,
    QUEUED,
    Ledger,
)

REACHED_OUTCOMES = ("answered", "partial", "not_on_file")

# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------
# Coordinates are laid out by hand rather than by a layout algorithm. The shape
# is the argument: one straight spine down the middle for the path a claim takes
# when everything works, and one column to the right for every way it can stop.
# A reader should be able to see, without reading a word, that the right-hand
# column is where the guardrails put things.

NODE_W = 258
NODE_H = 62
SPINE_X = 40
STOP_X = 430

_NODES: List[Dict[str, Any]] = [
    {"id": "intake", "kind": "start", "x": SPINE_X, "y": 0,
     "label": "Backlog import", "sub": "CSV, 835 ERA, EHR export",
     "detail": "Claims arrive with a payer, a workflow, a billed amount and a timely-filing "
               "deadline. The patient is a pointer: identifiers never enter the ledger, they go "
               "to the vault."},
    {"id": "score", "kind": "step", "x": SPINE_X, "y": 112,
     "label": "Priority scoring", "sub": "value x deadline pressure",
     "detail": "Priority is the claim's value multiplied by how close its filing deadline is. A "
               "claim inside 14 days preempts everything; past the deadline it is suppressed, "
               "because a call can no longer change the outcome."},
    {"id": "bundle", "kind": "step", "x": SPINE_X, "y": 224,
     "label": "Bundle by payer", "sub": "one hold, not N",
     "detail": "Claims for the same payer and workflow ride one call, up to that payer's own "
               "cap. Reaching a representative is the expensive part, so it is paid once instead "
               "of once per claim."},
    {"id": "gate", "kind": "decision", "x": SPINE_X, "y": 336,
     "label": "May we call?", "sub": "thirteen named holds",
     "detail": "The calling window in the payer's own timezone, the hold budget, the per-run call "
               "cap, the cost cap, the filing deadline, an unreconciled earlier call, a missing "
               "vault record, the kill switch, and the four authorization checks. Any one of them "
               "stops the call and says which."},
    {"id": "held", "kind": "stop", "x": STOP_X, "y": 336,
     "label": "Held", "sub": "with a named reason",
     "detail": "No call is placed. The claim stays queued and the reason is recorded, so a held "
               "claim is never a silent failure."},
    {"id": "plan", "kind": "step", "x": SPINE_X, "y": 448,
     "label": "Build the call plan", "sub": "task, schema, envelope",
     "detail": "The task text, the closed result schema, and one narrowed disclosure per patient. "
               "Each workflow may speak only the identifiers its envelope names: eligibility gets "
               "a member id and a date of birth and no name, because it does not need one."},
    {"id": "preflight", "kind": "decision", "x": SPINE_X, "y": 560,
     "label": "Disclosure pre-flight", "sub": "would this task leak?",
     "detail": "The planner reads its own finished task text back and refuses to return it if a "
               "never-disclose value appears in it. The check runs before the request is built, "
               "not after the call."},
    {"id": "refused", "kind": "stop", "x": STOP_X, "y": 560,
     "label": "Refused", "sub": "task never sent",
     "detail": "The plan is thrown away rather than dialled. This is the last check that runs "
               "entirely on our side of the wire."},
    {"id": "call", "kind": "call", "x": SPINE_X, "y": 672,
     "label": "CALL-E places the call", "sub": "menu, queue, hold, rep",
     "detail": "The agent works the phone menu with the keypad, waits in the queue without "
               "speaking, and talks to the representative. Hold is derived afterwards from the "
               "gaps between transcript turns, so it is measured rather than estimated."},
    {"id": "reached", "kind": "decision", "x": SPINE_X, "y": 790,
     "label": "Did anyone answer?", "sub": "outcome from the call",
     "detail": "hold_timeout, ivr_dead_end and unreached mean nobody was reached. Nothing is "
               "recorded from those calls, because there is nothing to record."},
    {"id": "requeue", "kind": "stop", "x": STOP_X, "y": 790,
     "label": "Requeue", "sub": "try a different window",
     "detail": "The claims go back to the queue tagged with the window that failed, so the next "
               "attempt is scheduled somewhere else in the day."},
    {"id": "schema", "kind": "decision", "x": SPINE_X, "y": 902,
     "label": "Schema check", "sub": "closed, fields required",
     "detail": "Validated locally rather than trusted from the response: no extra fields, every "
               "field present, correct type, enum values inside the enum. A failure marks the "
               "whole call unusable and sends every claim on it to a person."},
    {"id": "ground", "kind": "decision", "x": SPINE_X, "y": 1014,
     "label": "Is the quote real?", "sub": "found in the transcript",
     "detail": "Every answer carries the representative's own words. The quote is searched for in "
               "the transcript; one shorter than six words is refused however well it matches, "
               "because a single common word is a coincidence and not evidence."},
    {"id": "review", "kind": "human", "x": STOP_X, "y": 1014,
     "label": "Human review", "sub": "fields reset to unknown",
     "detail": "A claim whose quote cannot be found loses its substantive fields and goes to a "
               "person. The other claims on the same call keep their answers: one bad extraction "
               "does not discard a good one."},
    {"id": "answered", "kind": "step", "x": SPINE_X, "y": 1126,
     "label": "Answered", "sub": "every field has a quote",
     "detail": "Recorded with the reference number, the representative's first name, the hold "
               "receipt and the transcript, scrubbed of patient identifiers before it is written."},
    {"id": "approve", "kind": "human", "x": SPINE_X, "y": 1238,
     "label": "A person approves", "sub": "nothing leaves by itself",
     "detail": "Trunkline gathers and structures. It never files an appeal, never agrees to an "
               "amount, and never closes a claim by itself. This gate is not a limitation to be "
               "removed later."},
    {"id": "closed", "kind": "end", "x": SPINE_X, "y": 1350,
     "label": "Closed and exportable", "sub": "no patient identifiers",
     "detail": "The export carries claim numbers, plan answers and evidence quotes. It carries no "
               "patient identifiers; vault-check walks every file Trunkline wrote to prove it."},
]

_EDGES: List[Dict[str, Any]] = [
    {"from": "intake", "to": "score"},
    {"from": "score", "to": "bundle"},
    {"from": "bundle", "to": "gate"},
    {"from": "gate", "to": "held", "label": "suppressed", "tone": "stop"},
    {"from": "gate", "to": "plan", "label": "clear"},
    {"from": "plan", "to": "preflight"},
    {"from": "preflight", "to": "refused", "label": "would leak", "tone": "stop"},
    {"from": "preflight", "to": "call", "label": "clean"},
    {"from": "call", "to": "reached"},
    {"from": "reached", "to": "requeue", "label": "nobody", "tone": "stop"},
    {"from": "reached", "to": "schema", "label": "reached"},
    {"from": "schema", "to": "review", "label": "fails", "tone": "stop"},
    {"from": "schema", "to": "ground", "label": "passes"},
    {"from": "ground", "to": "review", "label": "no quote", "tone": "stop"},
    {"from": "ground", "to": "answered", "label": "grounded"},
    {"from": "review", "to": "approve", "tone": "muted"},
    {"from": "answered", "to": "approve"},
    {"from": "approve", "to": "closed"},
    {"from": "requeue", "to": "bundle", "kind": "loop", "label": "next window", "tone": "muted"},
]


def _counts(ledger: Ledger) -> Dict[str, int]:
    by_state: Dict[str, int] = {}
    for claim in ledger.claims:
        by_state[claim.state] = by_state.get(claim.state, 0) + 1

    reached = [c for c in ledger.calls if c.outcome in REACHED_OUTCOMES]
    stopped = [c for c in ledger.calls if c.outcome and c.outcome not in REACHED_OUTCOMES
               and c.outcome != "unusable"]
    unusable = [c for c in ledger.calls if c.outcome == "unusable"]

    ungrounded = 0
    grounded = 0
    for record in ledger.calls:
        for fields in record.per_claim.values():
            if not isinstance(fields, dict):
                continue
            if fields.get("_grounded"):
                grounded += 1
            else:
                ungrounded += 1

    return {
        "intake": len(ledger.claims),
        "score": len(ledger.claims),
        "bundle": len(ledger.calls),
        "gate": len(ledger.calls),
        "held": 0,
        "plan": len(ledger.calls),
        "preflight": len(ledger.calls),
        "refused": 0,
        "call": len(ledger.calls),
        "reached": len(reached),
        "requeue": len(stopped),
        "schema": len(unusable),
        "ground": grounded,
        "review": by_state.get(NEEDS_HUMAN, 0),
        "answered": by_state.get(ANSWERED, 0) + by_state.get(CLOSED, 0),
        "approve": by_state.get(ANSWERED, 0),
        "closed": by_state.get(CLOSED, 0),
        "_queued": by_state.get(QUEUED, 0),
        "_in_flight": by_state.get(PENDING_CALL, 0) + by_state.get(PENDING_RECONCILIATION, 0),
        "_ungrounded": ungrounded,
    }


def graph(ledger: Ledger) -> Dict[str, Any]:
    """The pipeline, with the ledger's live counts hung on it."""
    counts = _counts(ledger)
    nodes = []
    for node in _NODES:
        entry = dict(node)
        entry["count"] = counts.get(node["id"], 0)
        entry["w"] = NODE_W
        entry["h"] = NODE_H
        nodes.append(entry)
    return {
        "nodes": nodes,
        "edges": _EDGES,
        "totals": {
            "claims": len(ledger.claims),
            "calls": len(ledger.calls),
            "hold_seconds": sum(c.hold_seconds for c in ledger.calls),
            "hold_human": hold.format_duration(sum(c.hold_seconds for c in ledger.calls)),
            "queued": counts["_queued"],
            "in_flight": counts["_in_flight"],
            "review": counts["review"],
            "awaiting_approval": counts["approve"],
            "closed": counts["closed"],
            "ungrounded": counts["_ungrounded"],
        },
    }


STYLE = """
:root{
  --bg:#f6f7f9; --panel:#ffffff; --ink:#12151a; --muted:#61697a; --faint:#8b93a3;
  --line:#e4e7ec; --line-strong:#cfd4dd;
  --accent:#3b5bdb; --accent-soft:#e8ecfd;
  --ok:#0f7b52; --ok-soft:#e3f5ec;
  --warn:#8a5a00; --warn-soft:#fdf1dc;
  --stop:#a33232; --stop-soft:#fbe9e9;
  --human:#6741d9; --human-soft:#efeafc;
  --shadow:0 1px 2px rgba(16,20,30,.05),0 4px 14px rgba(16,20,30,.05);
  --radius:12px;
}
:root[data-theme="dark"], :root:not([data-theme="light"]){}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#0e1014; --panel:#161a21; --ink:#e9ecf2; --muted:#98a1b2; --faint:#727c8f;
    --line:#242a34; --line-strong:#333b48;
    --accent:#7d97ff; --accent-soft:#1c2440;
    --ok:#4ecb8f; --ok-soft:#12281f;
    --warn:#e2b85f; --warn-soft:#2a2213;
    --stop:#f08b8b; --stop-soft:#2e1919;
    --human:#a98bff; --human-soft:#221b3a;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 6px 20px rgba(0,0,0,.3);
  }
}
:root[data-theme="dark"]{
  --bg:#0e1014; --panel:#161a21; --ink:#e9ecf2; --muted:#98a1b2; --faint:#727c8f;
  --line:#242a34; --line-strong:#333b48;
  --accent:#7d97ff; --accent-soft:#1c2440;
  --ok:#4ecb8f; --ok-soft:#12281f;
  --warn:#e2b85f; --warn-soft:#2a2213;
  --stop:#f08b8b; --stop-soft:#2e1919;
  --human:#a98bff; --human-soft:#221b3a;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 6px 20px rgba(0,0,0,.3);
}
*{box-sizing:border-box;}
html,body{height:100%;}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;}
a{color:var(--accent);text-decoration:none;}
a:hover{text-decoration:underline;}
button{font:inherit;color:inherit;}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;}
.num{font-variant-numeric:tabular-nums;}

/* ---------- frame ---------- */
.app{display:grid;grid-template-columns:232px 1fr;grid-template-rows:56px 1fr;height:100vh;}
header{grid-column:1/-1;display:flex;align-items:center;gap:16px;padding:0 18px;
  background:var(--panel);border-bottom:1px solid var(--line);}
.brand{display:flex;align-items:baseline;gap:9px;font-weight:650;letter-spacing:-.02em;font-size:16px;}
.brand em{font-style:normal;color:var(--muted);font-weight:450;font-size:12.5px;}
.hstats{margin-left:auto;display:flex;gap:20px;align-items:center;}
.hstat{display:flex;flex-direction:column;line-height:1.25;}
.hstat b{font-size:14px;font-weight:650;}
.hstat span{font-size:10.5px;color:var(--faint);text-transform:uppercase;letter-spacing:.06em;}
.themebtn{background:none;border:1px solid var(--line);border-radius:8px;width:32px;height:32px;
  cursor:pointer;color:var(--muted);}
.themebtn:hover{border-color:var(--line-strong);color:var(--ink);}

nav{background:var(--panel);border-right:1px solid var(--line);padding:14px 10px;overflow:auto;}
nav h6{margin:16px 8px 6px;font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--faint);font-weight:650;}
nav h6:first-child{margin-top:2px;}
.navitem{display:flex;align-items:center;gap:10px;width:100%;padding:8px 10px;border:0;
  background:none;border-radius:9px;cursor:pointer;text-align:left;color:var(--muted);font-weight:500;}
.navitem:hover{background:var(--bg);color:var(--ink);}
.navitem[aria-current="true"]{background:var(--accent-soft);color:var(--accent);font-weight:600;}
.navitem .pill{margin-left:auto;font-size:11px;padding:1px 7px;border-radius:99px;
  background:var(--bg);color:var(--muted);font-variant-numeric:tabular-nums;}
.navitem[aria-current="true"] .pill{background:var(--panel);color:var(--accent);}
.navitem svg{width:16px;height:16px;flex:none;}
.navnote{margin:18px 8px 0;padding-top:14px;border-top:1px solid var(--line);
  font-size:11.5px;color:var(--faint);line-height:1.5;}

main{overflow:auto;position:relative;}
.pad{padding:22px 26px 60px;max-width:1080px;}
.pagehead{margin:0 0 4px;font-size:19px;font-weight:650;letter-spacing:-.02em;}
.pagesub{margin:0 0 20px;color:var(--muted);}

/* ---------- diagram ---------- */
.canvaswrap{position:absolute;inset:0;overflow:hidden;
  background-image:radial-gradient(var(--line-strong) 1px,transparent 1px);
  background-size:22px 22px;}
.canvaswrap.grab{cursor:grab;} .canvaswrap.grabbing{cursor:grabbing;}
#stage{position:absolute;top:0;left:0;transform-origin:0 0;}
#edges{position:absolute;top:0;left:0;overflow:visible;pointer-events:none;}
.node{position:absolute;background:var(--panel);border:1px solid var(--line);
  border-radius:var(--radius);box-shadow:var(--shadow);padding:10px 12px;cursor:pointer;
  display:flex;gap:10px;align-items:flex-start;transition:border-color .12s,box-shadow .12s;}
.node:hover{border-color:var(--line-strong);}
.node.sel{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft),var(--shadow);}
.node .ico{width:26px;height:26px;border-radius:7px;display:grid;place-items:center;flex:none;
  background:var(--bg);color:var(--muted);}
.node .ico svg{width:15px;height:15px;}
.node .lab{min-width:0;flex:1;}
.node .lab b{display:block;font-size:13px;font-weight:600;letter-spacing:-.01em;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.node .lab span{display:block;font-size:11.5px;color:var(--faint);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.node .cnt{font-size:11.5px;font-weight:650;padding:1px 7px;border-radius:99px;flex:none;
  background:var(--bg);color:var(--muted);font-variant-numeric:tabular-nums;}
.node[data-kind="decision"] .ico{background:var(--human-soft);color:var(--human);}
.node[data-kind="call"]{border-color:var(--accent);}
.node[data-kind="call"] .ico{background:var(--accent-soft);color:var(--accent);}
.node[data-kind="call"] .cnt{background:var(--accent-soft);color:var(--accent);}
.node[data-kind="human"] .ico{background:var(--ok-soft);color:var(--ok);}
.node[data-kind="human"] .cnt{background:var(--ok-soft);color:var(--ok);}
.node[data-kind="stop"]{border-style:dashed;}
.node[data-kind="stop"] .ico{background:var(--stop-soft);color:var(--stop);}
.node[data-kind="stop"] .cnt{background:var(--stop-soft);color:var(--stop);}
.node[data-kind="start"] .ico,.node[data-kind="end"] .ico{background:var(--ink);color:var(--panel);}
.edge{fill:none;stroke:var(--line-strong);stroke-width:1.6;}
.edge.stop{stroke-dasharray:5 4;}
.edge.muted{stroke-dasharray:3 5;opacity:.75;}
.elabel{font-size:11px;fill:var(--faint);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;}
.elabelbg{fill:var(--bg);}
.zoom{position:absolute;left:18px;bottom:18px;display:flex;flex-direction:column;
  background:var(--panel);border:1px solid var(--line);border-radius:10px;box-shadow:var(--shadow);
  overflow:hidden;z-index:3;}
.zoom button{width:34px;height:34px;border:0;background:none;cursor:pointer;color:var(--muted);
  display:grid;place-items:center;}
.zoom button+button{border-top:1px solid var(--line);}
.zoom button:hover{background:var(--bg);color:var(--ink);}
.legend{position:absolute;left:18px;top:18px;z-index:3;background:var(--panel);
  border:1px solid var(--line);border-radius:10px;box-shadow:var(--shadow);padding:10px 12px;
  display:flex;gap:14px;font-size:11.5px;color:var(--muted);}
.legend i{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:5px;
  vertical-align:middle;}

/* ---------- inspector ---------- */
.inspector{position:absolute;right:0;top:0;bottom:0;width:352px;background:var(--panel);
  border-left:1px solid var(--line);padding:20px 22px;overflow:auto;z-index:4;
  box-shadow:-8px 0 24px rgba(16,20,30,.06);}
.inspector[hidden]{display:none!important;}
.inspector .close{position:absolute;right:14px;top:14px;border:0;background:none;cursor:pointer;
  color:var(--faint);font-size:18px;line-height:1;}
.inspector h3{margin:0 0 2px;font-size:16px;letter-spacing:-.02em;}
.inspector .kind{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--faint);
  font-weight:650;}
.inspector p{color:var(--muted);}

/* ---------- generic ---------- */
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow);padding:16px 18px;margin:0 0 14px;}
.card h4{margin:0 0 12px;font-size:13px;font-weight:650;}
table{border-collapse:collapse;width:100%;font-size:13px;}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top;}
thead th{color:var(--faint);font-weight:650;font-size:10.5px;text-transform:uppercase;
  letter-spacing:.06em;}
tbody tr:last-child td{border-bottom:0;}
tbody tr.clickable{cursor:pointer;}
tbody tr.clickable:hover{background:var(--bg);}
td.r,th.r{text-align:right;font-variant-numeric:tabular-nums;}
.badge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:11px;font-weight:600;
  background:var(--bg);color:var(--muted);}
.badge.ok{background:var(--ok-soft);color:var(--ok);}
.badge.warn{background:var(--warn-soft);color:var(--warn);}
.badge.bad{background:var(--stop-soft);color:var(--stop);}
.badge.accent{background:var(--accent-soft);color:var(--accent);}
.btn{border:1px solid var(--line);background:var(--panel);border-radius:9px;padding:8px 14px;
  cursor:pointer;font-weight:600;}
.btn:hover{border-color:var(--line-strong);}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff;}
.btn.primary:hover{filter:brightness(1.07);}
.quote{border-left:3px solid var(--line-strong);padding:5px 0 5px 12px;color:var(--muted);
  font-style:italic;margin:10px 0 0;}
.turns{border:1px solid var(--line);border-radius:10px;overflow:hidden;}
.turn{display:flex;gap:12px;padding:7px 12px;border-bottom:1px solid var(--line);}
.turn:last-child{border-bottom:0;}
.turn .t{color:var(--faint);min-width:56px;font-variant-numeric:tabular-nums;font-size:12px;}
.turn.bot{background:var(--bg);}
.holdgap{display:flex;align-items:center;gap:10px;padding:8px 12px;background:var(--warn-soft);
  color:var(--warn);font-weight:600;font-size:12.5px;border-bottom:1px solid var(--line);}
.holdgap:before{content:"";flex:none;width:6px;height:6px;border-radius:99px;background:currentColor;}
.kv{display:grid;grid-template-columns:minmax(120px,auto) 1fr;gap:7px 16px;font-size:13px;}
.kv dt{color:var(--faint);}
.kv dd{margin:0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;
  word-break:break-word;}
.empty{color:var(--faint);padding:22px;text-align:center;}
.note{color:var(--faint);font-size:12px;margin-top:20px;line-height:1.6;}
.crumb{border:0;background:none;color:var(--accent);cursor:pointer;padding:0;margin-bottom:12px;
  font-weight:500;}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:14px 16px;box-shadow:var(--shadow);}
.stat b{display:block;font-size:22px;font-weight:650;letter-spacing:-.02em;
  font-variant-numeric:tabular-nums;}
.stat span{font-size:11.5px;color:var(--faint);}
@media (max-width:900px){
  .app{grid-template-columns:1fr;}
  nav{display:none;}
  .inspector{width:100%;}
}
"""


ICONS = {
    "start": "<path d='M5 3v18l14-9z'/>",
    "step": "<rect x='3' y='5' width='18' height='14' rx='2'/><path d='M3 10h18'/>",
    "decision": "<path d='M12 3l9 9-9 9-9-9z'/>",
    "call": "<path d='M4 4h4l2 5-2.5 1.5a12 12 0 006 6L15 14l5 2v4a2 2 0 01-2 2A16 16 0 012 6a2 2 0 012-2z'/>",
    "human": "<circle cx='12' cy='8' r='3.5'/><path d='M5 20a7 7 0 0114 0'/>",
    "stop": "<circle cx='12' cy='12' r='9'/><path d='M8 12h8'/>",
    "end": "<circle cx='12' cy='12' r='9'/><path d='M8.5 12.5l2.5 2.5 4.5-5'/>",
}

SCRIPT = r"""
'use strict';
var S={view:'workflow',graph:null,sel:null,detail:null};
var ICONS=__ICONS__;

function api(p){return fetch(p,{headers:{'Accept':'application/json'}}).then(function(r){
  if(!r.ok)throw new Error(r.status);return r.json();});}
function el(t,c,x){var e=document.createElement(t);if(c)e.className=c;
  if(x!==undefined&&x!==null)e.textContent=String(x);return e;}
function svg(t,a){var e=document.createElementNS('http://www.w3.org/2000/svg',t);
  for(var k in a)e.setAttribute(k,a[k]);return e;}
function icon(kind){var s=svg('svg',{viewBox:'0 0 24 24',fill:'none',stroke:'currentColor',
  'stroke-width':'1.8','stroke-linecap':'round','stroke-linejoin':'round'});
  s.innerHTML=ICONS[kind]||ICONS.step;return s;}
function money(n){return '$'+Number(n||0).toFixed(2);}
function clear(n){while(n.firstChild)n.removeChild(n.firstChild);}

/* ---------------- theme ---------------- */
function initTheme(){
  var saved=null;try{saved=localStorage.getItem('tl-theme');}catch(e){}
  if(saved)document.documentElement.setAttribute('data-theme',saved);
  document.getElementById('theme').addEventListener('click',function(){
    var cur=document.documentElement.getAttribute('data-theme');
    var next=cur==='dark'?'light':'dark';
    document.documentElement.setAttribute('data-theme',next);
    try{localStorage.setItem('tl-theme',next);}catch(e){}
  });
}

/* ---------------- routing ---------------- */
function go(view,detail){S.view=view;S.detail=detail||null;
  var h='#/'+view+(detail?'/'+encodeURIComponent(detail):'');
  if(location.hash!==h)location.hash=h; else render();}
function fromHash(){
  var p=(location.hash||'#/workflow').replace(/^#\//,'').split('/');
  S.view=p[0]||'workflow';S.detail=p[1]?decodeURIComponent(p[1]):null;render();}

/* ---------------- chrome ---------------- */
var NAV=[['workflow','Workflow','decision'],['queue','Work queue','step'],
         ['calls','Calls','call'],['review','Needs a person','human']];
function paintNav(){
  var t=S.graph?S.graph.totals:{};
  var counts={workflow:null,queue:t.queued,calls:t.calls,review:t.review};
  var nav=document.getElementById('nav');clear(nav);
  var h=el('h6',null,'Pipeline');nav.appendChild(h);
  NAV.forEach(function(row){
    var b=el('button','navitem');b.type='button';
    b.setAttribute('aria-current',S.view===row[0]?'true':'false');
    b.appendChild(icon(row[2]));b.appendChild(el('span',null,row[1]));
    var n=counts[row[0]];
    if(n!==null&&n!==undefined)b.appendChild(el('span','pill',n));
    b.addEventListener('click',function(){go(row[0]);});
    nav.appendChild(b);
  });
  var note=el('div','navnote');
  note.appendChild(el('div',null,'Loopback only. No authentication, and it cannot place a call.'));
  nav.appendChild(note);
}
function paintHeader(){
  var t=S.graph?S.graph.totals:{};
  var box=document.getElementById('hstats');clear(box);
  [[t.hold_human||'0m 00s','hold absorbed'],[t.calls||0,'calls'],
   [t.awaiting_approval||0,'awaiting approval']].forEach(function(p){
    var d=el('div','hstat');d.appendChild(el('b',null,p[0]));d.appendChild(el('span',null,p[1]));
    box.appendChild(d);});
}

/* ---------------- workflow diagram ---------------- */
var VIEW={x:0,y:0,k:1};
function nodeById(id){for(var i=0;i<S.graph.nodes.length;i++)
  if(S.graph.nodes[i].id===id)return S.graph.nodes[i];return null;}

function drawWorkflow(main){
  var wrap=el('div','canvaswrap grab');wrap.id='cw';
  var stage=el('div');stage.id='stage';
  var edges=svg('svg',{id:'edges'});
  stage.appendChild(edges);

  var maxX=0,maxY=0;
  S.graph.nodes.forEach(function(n){
    maxX=Math.max(maxX,n.x+n.w);maxY=Math.max(maxY,n.y+n.h);
    var d=el('div','node');d.dataset.kind=n.kind;d.dataset.id=n.id;
    d.style.left=n.x+'px';d.style.top=n.y+'px';
    d.style.width=n.w+'px';d.style.minHeight=n.h+'px';
    var ic=el('div','ico');ic.appendChild(icon(n.kind));d.appendChild(ic);
    var lab=el('div','lab');lab.appendChild(el('b',null,n.label));
    lab.appendChild(el('span',null,n.sub));d.appendChild(lab);
    if(n.count)d.appendChild(el('div','cnt',n.count));
    d.addEventListener('click',function(ev){ev.stopPropagation();select(n.id);});
    stage.appendChild(d);
  });
  edges.setAttribute('width',maxX+160);edges.setAttribute('height',maxY+80);
  var defs=svg('defs');
  var m=svg('marker',{id:'arrow',viewBox:'0 0 10 10',refX:'9',refY:'5',
    markerWidth:'7',markerHeight:'7',orient:'auto-start-reverse'});
  m.appendChild(svg('path',{d:'M0 0L10 5L0 10z',fill:'var(--line-strong)'}));
  defs.appendChild(m);edges.appendChild(defs);

  S.graph.edges.forEach(function(e){
    var a=nodeById(e.from),b=nodeById(e.to);if(!a||!b)return;
    var cls='edge'+(e.tone?' '+e.tone:'');
    var d,lx,ly,anchor='start';
    if(e.kind==='loop'){
      var ax=a.x+a.w,ay=a.y+a.h/2,bx=b.x+b.w,by=b.y+b.h/2,out=maxX+110;
      d='M'+ax+' '+ay+' C'+out+' '+ay+','+out+' '+by+','+bx+' '+by;
      lx=out-26;ly=(ay+by)/2;
    }else if(a.x===b.x){
      var x=a.x+a.w/2,y1=a.y+a.h,y2=b.y;
      d='M'+x+' '+y1+' L'+x+' '+y2;lx=x+9;ly=(y1+y2)/2+4;
    }else{
      var sx=a.x+a.w,sy=a.y+a.h/2,tx=b.x,ty=b.y+b.h/2;
      var mid=(sx+tx)/2;
      d='M'+sx+' '+sy+' C'+mid+' '+sy+','+mid+' '+ty+','+tx+' '+ty;
      lx=mid;ly=(sy+ty)/2-9;anchor='middle';
    }
    edges.appendChild(svg('path',{d:d,class:cls,'marker-end':'url(#arrow)'}));
    if(e.label){
      var g=svg('g');
      var bg=svg('rect',{class:'elabelbg',rx:'4'});
      var tx2=svg('text',{class:'elabel',x:lx,y:ly,'text-anchor':
        e.kind==='loop'?'middle':anchor});
      tx2.textContent=e.label;g.appendChild(bg);g.appendChild(tx2);edges.appendChild(g);
      setTimeout(function(){var bb=tx2.getBBox();
        bg.setAttribute('x',bb.x-4);bg.setAttribute('y',bb.y-2);
        bg.setAttribute('width',bb.width+8);bg.setAttribute('height',bb.height+4);},0);
    }
  });

  wrap.appendChild(stage);

  var legend=el('div','legend');
  [['var(--accent)','the call'],['var(--human)','a decision'],
   ['var(--ok)','a person'],['var(--stop)','stopped here']].forEach(function(p){
    var s=el('span');var i=el('i');i.style.background=p[0];
    s.appendChild(i);s.appendChild(document.createTextNode(p[1]));legend.appendChild(s);});
  wrap.appendChild(legend);

  var zoom=el('div','zoom');
  [['+',function(){zoomBy(1.2);}],['−',function(){zoomBy(1/1.2);}],
   ['⤢',function(){fit();}]].forEach(function(p){
    var b=el('button',null,p[0]);b.type='button';b.addEventListener('click',p[1]);
    zoom.appendChild(b);});
  wrap.appendChild(zoom);

  var insp=el('aside','inspector');insp.id='insp';insp.hidden=true;wrap.appendChild(insp);
  wrap.addEventListener('click',function(){select(null);});
  main.appendChild(wrap);

  wrap.addEventListener('wheel',function(ev){
    ev.preventDefault();
    if(ev.ctrlKey||ev.metaKey){zoomBy(ev.deltaY<0?1.1:1/1.1);return;}
    VIEW.x-=ev.deltaX;VIEW.y-=ev.deltaY;applyView();},{passive:false});
  var drag=null;
  wrap.addEventListener('pointerdown',function(ev){
    if(ev.target.closest('.node,.inspector,.zoom,.legend'))return;
    drag={x:ev.clientX-VIEW.x,y:ev.clientY-VIEW.y};wrap.classList.add('grabbing');
    wrap.setPointerCapture(ev.pointerId);});
  wrap.addEventListener('pointermove',function(ev){
    if(!drag)return;VIEW.x=ev.clientX-drag.x;VIEW.y=ev.clientY-drag.y;applyView();});
  wrap.addEventListener('pointerup',function(){drag=null;wrap.classList.remove('grabbing');});
  S.bounds={w:maxX,h:maxY};
  fitWidth();
}
function applyView(){var s=document.getElementById('stage');
  if(s)s.style.transform='translate('+VIEW.x+'px,'+VIEW.y+'px) scale('+VIEW.k+')';}
function zoomBy(f){VIEW.k=Math.min(2,Math.max(.35,VIEW.k*f));applyView();}
function metrics(){
  var w=document.getElementById('cw');if(!w||!S.bounds)return null;
  return {w:w,aw:w.clientWidth-(S.sel?372:0),ah:w.clientHeight};}
/* On load, scale to the width and leave the flow readable; the reader pans down. */
function fitWidth(){
  var m=metrics();if(!m)return;
  VIEW.k=Math.max(.62,Math.min(1,(m.aw-120)/S.bounds.w));
  VIEW.x=(m.aw-S.bounds.w*VIEW.k)/2;VIEW.y=84;applyView();}
/* The button: show the whole pipeline at once, however small that has to be. */
function fit(){
  var m=metrics();if(!m)return;
  VIEW.k=Math.max(.3,Math.min(1,Math.min((m.aw-80)/S.bounds.w,(m.ah-80)/S.bounds.h)));
  VIEW.x=(m.aw-S.bounds.w*VIEW.k)/2;VIEW.y=(m.ah-S.bounds.h*VIEW.k)/2;applyView();}

function select(id){
  S.sel=id;
  var nodes=document.querySelectorAll('.node');
  for(var i=0;i<nodes.length;i++)
    nodes[i].classList.toggle('sel',nodes[i].dataset.id===id);
  var insp=document.getElementById('insp');if(!insp)return;
  clear(insp);
  if(!id){insp.hidden=true;return;}
  var n=nodeById(id);insp.hidden=false;
  var x=el('button','close','×');x.type='button';
  x.addEventListener('click',function(ev){ev.stopPropagation();select(null);});
  insp.appendChild(x);
  var kindName={start:'entry',step:'step',decision:'decision',call:'the call',
    human:'human gate',stop:'stopped',end:'exit'}[n.kind]||n.kind;
  insp.appendChild(el('div','kind',kindName));
  insp.appendChild(el('h3',null,n.label));
  insp.appendChild(el('p',null,n.detail));
  var box=el('div','card');
  box.appendChild(el('h4',null,'Right now'));
  var dl=el('dl','kv');
  dl.appendChild(el('dt',null,'at this stage'));dl.appendChild(el('dd',null,n.count));
  if(id==='ground'&&S.graph.totals.ungrounded)
    {dl.appendChild(el('dt',null,'reset to unknown'));
     dl.appendChild(el('dd',null,S.graph.totals.ungrounded));}
  if(id==='call'){dl.appendChild(el('dt',null,'hold absorbed'));
     dl.appendChild(el('dd',null,S.graph.totals.hold_human));}
  box.appendChild(dl);insp.appendChild(box);
  var jump={queue:'queue',review:'review',call:'calls',answered:'review',approve:'review'}[id];
  if(jump){var b=el('button','btn','Open '+jump);b.type='button';
    b.addEventListener('click',function(ev){ev.stopPropagation();go(jump);});insp.appendChild(b);}
}

/* ---------------- tables ---------------- */
function stateBadge(s){
  var m={answered:'ok',closed:'ok',needs_human:'warn',queued:'',pending_call:'accent',
    pending_reconciliation:'warn'};
  var b=el('span','badge '+(m[s]||''),String(s).replace(/_/g,' '));return b;}

function viewQueue(main){
  var pad=el('div','pad');
  pad.appendChild(el('h1','pagehead','Work queue'));
  pad.appendChild(el('p','pagesub','Every claim Trunkline is holding, highest priority first.'));
  api('/api/claims').then(function(rows){
    var card=el('div','card');
    if(!rows.length){card.appendChild(el('div','empty','Nothing queued.'));}
    else{
      var t=el('table'),th=el('thead'),tr=el('tr');
      ['Claim','Payer','Workflow','Billed','Deadline','State','Evidence'].forEach(function(h,i){
        tr.appendChild(el('th',i===3?'r':null,h));});
      th.appendChild(tr);t.appendChild(th);
      var tb=el('tbody');
      rows.forEach(function(r){
        var row=el('tr','clickable');
        var c=el('td');c.appendChild(el('span','mono',r.claim_number));row.appendChild(c);
        row.appendChild(el('td',null,r.payer));
        row.appendChild(el('td',null,r.workflow.replace(/_/g,' ')));
        row.appendChild(el('td','r',money(r.billed_amount)));
        row.appendChild(el('td',null,r.filing_deadline));
        var st=el('td');st.appendChild(stateBadge(r.state));row.appendChild(st);
        var ev=el('td');
        if(r.has_result)ev.appendChild(el('span','badge '+(r.grounded?'ok':'bad'),
          r.grounded?'grounded':'no quote'));
        row.appendChild(ev);
        row.addEventListener('click',function(){go('claim',r.id);});
        tb.appendChild(row);});
      t.appendChild(tb);card.appendChild(t);}
    pad.appendChild(card);
  });
  main.appendChild(pad);
}

function viewReview(main){
  var pad=el('div','pad');
  pad.appendChild(el('h1','pagehead','Needs a person'));
  pad.appendChild(el('p','pagesub',
    'Nothing leaves Trunkline on its own. Approve an answer to close and export it.'));
  api('/api/claims?review=1').then(function(rows){
    if(!rows.length){var c=el('div','card');c.appendChild(el('div','empty','Nothing to review.'));
      pad.appendChild(c);return;}
    rows.forEach(function(r){
      var card=el('div','card');
      var head=el('div');head.style.display='flex';head.style.alignItems='center';
      head.style.gap='10px';head.style.marginBottom='10px';
      var strong=el('strong','mono',r.claim_number);head.appendChild(strong);
      head.appendChild(stateBadge(r.state));
      if(r.has_result)head.appendChild(el('span','badge '+(r.grounded?'ok':'bad'),
        r.grounded?'evidence found':'no supporting quote'));
      var sp=el('span');sp.style.marginLeft='auto';
      sp.appendChild(el('span','badge',r.payer));head.appendChild(sp);
      card.appendChild(head);
      var dl=el('dl','kv');
      Object.keys(r.result||{}).sort().forEach(function(k){
        if(k.charAt(0)==='_'||k==='claim_number'||k==='evidence_quote')return;
        dl.appendChild(el('dt',null,k.replace(/_/g,' ')));
        dl.appendChild(el('dd',null,r.result[k]));});
      card.appendChild(dl);
      if(r.result&&r.result.evidence_quote&&r.result.evidence_quote!=='unknown')
        card.appendChild(el('p','quote','“'+r.result.evidence_quote+'”'));
      var bar=el('div');bar.style.marginTop='14px';bar.style.display='flex';bar.style.gap='9px';
      var open=el('button','btn','Open the call');open.type='button';
      open.addEventListener('click',function(){go('claim',r.id);});bar.appendChild(open);
      if(r.state==='answered'){
        var ok=el('button','btn primary','Approve');ok.type='button';
        ok.addEventListener('click',function(){approve(r.id,ok);});bar.appendChild(ok);}
      card.appendChild(bar);
      pad.appendChild(card);});
  });
  main.appendChild(pad);
}

function approve(id,btn){
  btn.disabled=true;btn.textContent='Approving…';
  fetch('/approve',{method:'POST',headers:{
    'Content-Type':'application/x-www-form-urlencoded','X-Trunkline':'1'},
    body:'claim_id='+encodeURIComponent(id)})
  .then(function(r){if(!r.ok)throw new Error();return boot();})
  .then(function(){render();})
  .catch(function(){btn.disabled=false;btn.textContent='Approve failed, retry';});
}

function viewCalls(main){
  var pad=el('div','pad');
  pad.appendChild(el('h1','pagehead','Calls'));
  pad.appendChild(el('p','pagesub',
    'Hold is derived from the gaps between transcript turns, so it is measured, not estimated.'));
  api('/api/calls').then(function(rows){
    if(!rows.length){var c=el('div','card');c.appendChild(el('div','empty','No calls yet.'));
      pad.appendChild(c);return;}
    rows.forEach(function(r){
      var card=el('div','card');card.style.cursor='pointer';
      var head=el('div');head.style.display='flex';head.style.gap='10px';
      head.style.alignItems='center';head.style.flexWrap='wrap';
      head.appendChild(el('strong',null,r.payer));
      head.appendChild(el('span','badge',r.workflow.replace(/_/g,' ')));
      head.appendChild(el('span','badge '+(r.reached?'ok':'bad'),r.outcome));
      head.appendChild(el('span','mono',r.phone));
      var sp=el('span');sp.style.marginLeft='auto';
      sp.appendChild(el('span','badge',r.mode+' mode'));head.appendChild(sp);
      card.appendChild(head);
      var line=el('div');line.style.marginTop='10px';line.style.display='flex';
      line.style.gap='18px';line.style.flexWrap='wrap';line.style.alignItems='baseline';
      var big=el('b',null,r.hold_human+' on hold');big.style.fontSize='15px';
      line.appendChild(big);
      line.appendChild(el('span','mono','of '+r.total_human+' on the call'));
      line.appendChild(el('span','mono','talk '+r.talk_human));
      line.appendChild(el('span','mono',money(r.cost_estimate_usd)));
      line.appendChild(el('span','mono','ref '+r.reference_number));
      line.appendChild(el('span','mono',r.claims+' claim(s)'));
      card.appendChild(line);
      card.addEventListener('click',function(){go('call',r.id);});
      pad.appendChild(card);});
  });
  main.appendChild(pad);
}

function backBtn(pad,view,text){
  var b=el('button','crumb','← '+text);b.type='button';
  b.addEventListener('click',function(){go(view);});pad.appendChild(b);}

function viewCall(main,id){
  var pad=el('div','pad');
  backBtn(pad,'calls','All calls');
  api('/api/call?id='+encodeURIComponent(id)).then(function(r){
    pad.appendChild(el('h1','pagehead',r.payer+' · '+r.workflow.replace(/_/g,' ')));
    pad.appendChild(el('p','pagesub',r.phone+' · reference '+r.reference_number
      +' · representative '+r.rep_name+' · '+r.mode+' mode'));
    var s=el('div','grid2');
    [[r.hold_human,'on hold'],[r.talk_human,'talking'],[r.total_human,'call length'],
     [money(r.cost_estimate_usd),'estimated cost']].forEach(function(p){
      var d=el('div','stat');d.appendChild(el('b',null,p[0]));
      d.appendChild(el('span',null,p[1]));s.appendChild(d);});
    pad.appendChild(s);
    if(r.findings&&r.findings.length){
      var f=el('div','card');f.style.marginTop='14px';
      f.appendChild(el('h4',null,'Findings'));
      var ul=el('ul');ul.style.margin='0';ul.style.paddingLeft='18px';
      r.findings.forEach(function(x){var li=el('li',null,x);li.style.color='var(--stop)';
        ul.appendChild(li);});
      f.appendChild(ul);pad.appendChild(f);}
    var wrap=el('div');wrap.style.marginTop='14px';
    r.claims.forEach(function(c){
      var card=el('div','card');
      var head=el('div');head.style.display='flex';head.style.gap='10px';
      head.style.alignItems='center';head.style.marginBottom='10px';
      head.appendChild(el('strong','mono',c.claim_number));
      head.appendChild(el('span','badge '+(c.grounded?'ok':'bad'),
        c.grounded?'evidence found in transcript':'no supporting quote'));
      head.appendChild(stateBadge(c.state));
      card.appendChild(head);
      var dl=el('dl','kv');
      Object.keys(c.fields).sort().forEach(function(k){
        if(k.charAt(0)==='_'||k==='claim_number'||k==='evidence_quote')return;
        dl.appendChild(el('dt',null,k.replace(/_/g,' ')));
        dl.appendChild(el('dd',null,c.fields[k]));});
      card.appendChild(dl);
      if(c.fields.evidence_quote&&c.fields.evidence_quote!=='unknown')
        card.appendChild(el('p','quote','“'+c.fields.evidence_quote+'”'));
      wrap.appendChild(card);});
    pad.appendChild(wrap);
    pad.appendChild(el('h4',null,'Transcript'));
    var t=el('div','turns');
    r.transcript.forEach(function(turn){
      if(turn.hold_before)
        t.appendChild(el('div','holdgap','on hold for '+turn.hold_before));
      var d=el('div','turn'+(turn.speaker==='bot'?' bot':''));
      d.appendChild(el('span','t',turn.at));
      d.appendChild(el('span',null,turn.text));
      t.appendChild(d);});
    pad.appendChild(t);
    pad.appendChild(el('p','note',
      'Patient identifiers were removed before this transcript was stored. Claim numbers survive: '
      +'the redactor matches short identifiers exactly rather than loosely.'));
  });
  main.appendChild(pad);
}

function viewClaim(main,id){
  var pad=el('div','pad');
  backBtn(pad,'queue','Work queue');
  api('/api/claim?id='+encodeURIComponent(id)).then(function(r){
    pad.appendChild(el('h1','pagehead',r.claim_number));
    pad.appendChild(el('p','pagesub',r.payer+' · '+r.workflow.replace(/_/g,' ')
      +' · '+money(r.billed_amount)+' billed · deadline '+r.filing_deadline));
    var card=el('div','card');
    var head=el('div');head.style.display='flex';head.style.gap='10px';
    head.style.marginBottom='12px';
    head.appendChild(stateBadge(r.state));
    if(r.has_result)head.appendChild(el('span','badge '+(r.grounded?'ok':'bad'),
      r.grounded?'evidence found':'no supporting quote'));
    card.appendChild(head);
    var dl=el('dl','kv');
    Object.keys(r.result||{}).sort().forEach(function(k){
      if(k.charAt(0)==='_'||k==='claim_number'||k==='evidence_quote')return;
      dl.appendChild(el('dt',null,k.replace(/_/g,' ')));
      dl.appendChild(el('dd',null,r.result[k]));});
    card.appendChild(dl);
    if(r.result&&r.result.evidence_quote&&r.result.evidence_quote!=='unknown')
      card.appendChild(el('p','quote','“'+r.result.evidence_quote+'”'));
    pad.appendChild(card);
    var bar=el('div');bar.style.display='flex';bar.style.gap='9px';
    if(r.call_id){var b=el('button','btn','Open the call');b.type='button';
      b.addEventListener('click',function(){go('call',r.call_id);});bar.appendChild(b);}
    if(r.state==='answered'){var ok=el('button','btn primary','Approve this answer');
      ok.type='button';ok.addEventListener('click',function(){approve(r.id,ok);});
      bar.appendChild(ok);}
    pad.appendChild(bar);
  });
  main.appendChild(pad);
}

/* ---------------- render ---------------- */
function render(){
  paintNav();paintHeader();
  var main=document.getElementById('main');clear(main);
  if(S.view==='workflow')drawWorkflow(main);
  else if(S.view==='queue')viewQueue(main);
  else if(S.view==='calls')viewCalls(main);
  else if(S.view==='review')viewReview(main);
  else if(S.view==='call')viewCall(main,S.detail);
  else if(S.view==='claim')viewClaim(main,S.detail);
  else{S.view='workflow';drawWorkflow(main);}
}
function boot(){return api('/api/graph').then(function(g){
  S.graph=g;
  var t=document.getElementById('practice');
  if(t)t.textContent=g.practice||'';
  return g;});}
window.addEventListener('hashchange',fromHash);
window.addEventListener('resize',function(){if(S.view==='workflow')fitWidth();});
initTheme();
boot().then(fromHash).catch(function(){
  document.getElementById('main').appendChild(
    el('div','pad','Could not read the ledger.'));});
"""


def script() -> str:
    import json as _json
    return SCRIPT.replace("__ICONS__", _json.dumps(ICONS))


def shell() -> bytes:
    """The page skeleton. Every value in it is fetched as JSON and set as text."""
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Trunkline</title><style>" + STYLE + "</style></head><body>"
        "<div class='app'>"
        "<header>"
        "<div class='brand'>Trunkline<em id='practice'></em></div>"
        "<div class='hstats' id='hstats'></div>"
        "<button class='themebtn' id='theme' type='button' title='Light or dark'>&#9681;</button>"
        "</header>"
        "<nav id='nav'></nav>"
        "<main id='main'></main>"
        "</div>"
        "<script>" + script() + "</script>"
        "</body></html>"
    ).encode("utf-8")
