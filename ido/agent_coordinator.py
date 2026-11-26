"""Backend-only discussion protocol. No advisor can call the execution engine."""

import json
import uuid
from dataclasses import asdict
from hashlib import sha256

from advisors import DIRECTIONAL, assess

WINDOW = 12                 # seconds per remote phase; no network I/O in engine transactions
PEERS = (*DIRECTIONAL, "risk", "skeptic")


def init_mailbox(db):
    db.execute("""CREATE TABLE IF NOT EXISTS advisor_mailbox (
        id TEXT PRIMARY KEY, account TEXT NOT NULL UNIQUE, provider TEXT NOT NULL,
        phase TEXT NOT NULL, deadline INTEGER NOT NULL, payload TEXT NOT NULL
    )""")


def event(job, time, kind, agent, text, target=None, position=None):
    job["events"].append(dict(sequence=len(job["events"])+1, time=time, kind=kind,
                              agent=agent, text=text, target=target, position=position))


def prepare(provider, bars, proposed, risk, now, quote, remote=False, context=None):
    snapshot = dict(symbol="XAU/USD", provider=provider, asOf=now, quoteTime=quote["time"],
                    price=quote["price"] if quote["fresh"] else None,
                    candles=[asdict(b) for b in bars])
    snapshot["id"] = sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    job = dict(snapshot=snapshot, events=[], phase="analysis", started=now, remote=remote,
               unavailable=False, veto=False)
    event(job, now, "snapshot", "coordinator",
          f"Gedeelde XAU/USD-snapshot: {len(bars)} gesloten minuutcandles."
          if quote["fresh"] else "Koers onbeschikbaar of verouderd. Geen bruikbare momentopname.")
    for agent in PEERS:
        event(job, now, "analyzing", agent, "Ontving de snapshot voor onafhankelijke analyse.")
    job["report"] = assess(bars, proposed, risk, now, quote["time"], context)
    for vote in job["report"]["votes"]:
        event(job, now, "analysis", vote["agent"], vote["evidence"], position=vote["vote"])
    if remote:
        event(job, now, "analyzing", "remote", "Analyseopdracht staat klaar; wacht op de externe worker.")
    else:
        discussion(job, now)
    return job


def discussion(job, now):
    job["phase"] = "review" if job["remote"] and not job["unavailable"] else "ready"
    event(job, now, "reviewing", "coordinator", "Onafhankelijke analyses afgesloten. Adviseurs kunnen nu elkaars bevindingen beoordelen.")
    votes = {v["agent"]: v for v in job["report"]["votes"]}
    proposed = job["report"]["proposed"]
    if job["remote"] and job.get("external"):
        votes["remote"] = job["external"]
    objections = set()
    for agent in DIRECTIONAL:
        own = votes[agent]
        for other in (*DIRECTIONAL, "remote"):
            peer = votes.get(other)
            if agent == other or not peer:
                continue
            if {own["vote"], peer["vote"]} == {"BUY", "SELL"}:
                event(job, now, "objection", agent, f"{own['vote']} botst met {peer['vote']}. Eigen bewijs: {own['evidence']}", other)
                objections.add(agent)
        if agent not in objections:
            target = DIRECTIONAL[(DIRECTIONAL.index(agent)+1) % len(DIRECTIONAL)]
            peer = votes[target]
            kind = "objection" if own["vote"] == "WAIT" and peer["vote"] in ("BUY", "SELL") else "review"
            text = (f"Ik bevestig {peer['vote']} niet. Eigen toets: {own['evidence']}" if kind == "objection" else
                    f"Ik behoud {own['vote']} na vergelijking met {peer['vote']}; geen tegengestelde richting.")
            event(job, now, kind, agent, text, target)
    risk, skeptic = votes["risk"], votes["skeptic"]
    direction = next((v for v in votes.values() if v["agent"] in DIRECTIONAL and v["vote"] == proposed and proposed != "WAIT"), None)
    event(job, now, "objection" if risk["vote"] == "VETO" else "review", "risk",
          (f"Getoetst tegen richtingadvies {direction['vote']}. " if direction else "") +
          ("Geen meerderheid kan dit veto opheffen. " if risk["vote"] == "VETO" else "Grenzen blijven gelden ongeacht de stemmen. ")+risk["evidence"],
          direction["agent"] if direction else "coordinator",
          risk["vote"])
    weak = next((v for v in votes.values() if v["agent"] in DIRECTIONAL and v["vote"] == "WAIT"), None)
    event(job, now, "objection" if skeptic["vote"] == "FLAG" or (weak and proposed != "WAIT") else "review",
          "skeptic", skeptic["evidence"] + (f" Ook {weak['agent']} geeft geen richting." if weak and proposed != "WAIT" else ""),
          weak["agent"] if weak and proposed != "WAIT" else "coordinator")
    if job["phase"] == "review":
        event(job, now, "reviewing", "remote", "Beoordelingsopdracht staat klaar; wacht op peerfeedback.")


def conclusion(job, now, reason=None, can_execute=False):
    report = job["report"]
    if reason is None and (job["unavailable"] or job["veto"]) and report["recommendation"] != "WAIT":
        reason = ("Externe adviseur niet beschikbaar; geen nieuwe positie." if job["unavailable"] else
                  "De externe reviewer betwistte een bevinding; geen nieuwe positie.")
    if reason:
        report.update(recommendation="WAIT", reason=reason)
    executable = can_execute and report["recommendation"] in ("BUY", "SELL")
    if report["recommendation"] != "WAIT" and not executable:
        report.update(recommendation="WAIT", reason="De engine kon de instap niet veilig bevestigen.")
    event(job, now, "decision", "coordinator", report["reason"], position=report["recommendation"])
    report.update(executed=executable, snapshotId=job["snapshot"]["id"], events=job["events"],
                  decidedAt=now, discussionComplete=job["phase"] == "ready" and not job["unavailable"])
    return report


def start_remote(db, sid, provider, job, inputs, now):
    key = uuid.uuid4().hex
    job["inputs"] = inputs
    cursor = db.execute("""INSERT OR IGNORE INTO advisor_mailbox
        SELECT ?,?,?, 'analysis',?,? WHERE (SELECT count(*) FROM advisor_mailbox) < 32""",
        (key, sid, provider, now+WINDOW, json.dumps(job)))
    return cursor.rowcount == 1


def pending(db, sid, provider):
    return db.execute("SELECT * FROM advisor_mailbox WHERE account=? AND provider=?", (sid, provider)).fetchone()


def remove(db, row):
    db.execute("DELETE FROM advisor_mailbox WHERE id=?", (row["id"],))


def expire(row, now):
    job = json.loads(row["payload"])
    if row["phase"] != "ready":
        job["unavailable"] = True
        event(job, now, "unavailable", "remote", "Geen geldige reactie vóór de deadline; er wordt niets ingevuld.")
        if row["phase"] == "analysis":
            discussion(job, now)
    return job


def task(db, now):
    row = db.execute("""SELECT * FROM advisor_mailbox WHERE phase IN ('analysis','review')
                        AND deadline>? ORDER BY deadline LIMIT 1""", (now,)).fetchone()
    if not row:
        return None
    job = json.loads(row["payload"])
    # Risk/budget, local order proposal, private identifiers and broker credentials never leave this service.
    peers = ([{k: v[k] for k in ("agent", "vote", "confidence", "evidence")}
              for v in job["report"]["votes"] if v["agent"] in DIRECTIONAL] if row["phase"] == "review" else [])
    return dict(id=row["id"], phase=row["phase"], deadline=row["deadline"], snapshot=job["snapshot"], peers=peers)


def submit(db, data, now):
    keys = {"id","snapshotId","phase","position","text","target"}
    if (set(data) != keys or not all(isinstance(data[k], str) for k in keys-{"target"}) or
            len(data["id"]) != 32 or len(data["snapshotId"]) != 64 or
            not 1 <= len(data["text"].strip()) <= 320 or not data["text"].isprintable()):
        raise ValueError
    phase = data["phase"]
    if phase == "analysis":
        if data["position"] not in ("BUY", "SELL", "WAIT") or data["target"] is not None:
            raise ValueError
    elif phase == "review":
        if data["position"] not in ("REVIEW", "CHALLENGE") or data["target"] not in DIRECTIONAL:
            raise ValueError
    else:
        raise ValueError
    row = db.execute("SELECT * FROM advisor_mailbox WHERE id=?", (data["id"],)).fetchone()
    if not row or row["phase"] != phase or row["deadline"] <= now:
        return False
    job = json.loads(row["payload"])
    if job["snapshot"]["id"] != data["snapshotId"]:
        return False
    if phase == "analysis":
        job["external"] = dict(vote=data["position"], evidence=data["text"])
        event(job, now, "analysis", "remote", data["text"], position=data["position"])
        discussion(job, now)
    else:
        job["phase"] = "ready"
        job["veto"] = data["position"] == "CHALLENGE"
        event(job, now, "objection" if job["veto"] else "review", "remote", data["text"], data["target"])
    db.execute("UPDATE advisor_mailbox SET phase=?, deadline=?, payload=? WHERE id=?",
               (job["phase"], now+WINDOW, json.dumps(job), row["id"]))
    return True


def activity(db, sid, provider, last):
    row = pending(db, sid, provider)
    if row:
        job = json.loads(row["payload"])
        return dict(roundId=None, snapshotId=job["snapshot"]["id"], phase=row["phase"], events=job["events"])
    return dict(roundId=last["id"] if last and last.get("snapshotId") else None,
                snapshotId=last.get("snapshotId") if last else None,
                phase="complete" if last and last.get("events") else "idle",
                events=last.get("events", []) if last else [])
