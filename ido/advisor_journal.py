"""Owner-scoped, append-only advisory inputs/outputs with a later outcome link."""

import json


def init_advisors(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS advisor_rounds (
            id INTEGER PRIMARY KEY, account TEXT NOT NULL, provider TEXT NOT NULL,
            assessed INTEGER NOT NULL, input TEXT NOT NULL, report TEXT NOT NULL,
            closed INTEGER, outcome TEXT
        );
        CREATE INDEX IF NOT EXISTS advisor_account ON advisor_rounds(account,provider,id);
        CREATE INDEX IF NOT EXISTS advisor_closed ON advisor_rounds(account,provider,closed) WHERE closed IS NOT NULL;
    """)


def record_round(db, sid, provider, report, inputs, executed):
    stored = dict(report, executed=executed)
    row = db.execute("INSERT INTO advisor_rounds(account,provider,assessed,input,report) VALUES(?,?,?,?,?)",
                     (sid, provider, report["time"], json.dumps(inputs), json.dumps(stored)))
    return row.lastrowid


def record_outcome(db, sid, trade):
    if not trade.get("advisorRoundId"):
        return
    entry, exit_time = trade.get("entryQuoteAt"), trade.get("exitQuoteAt")
    outcome = dict(tradeId=trade["id"], closedAt=trade["closedAt"], exitQuoteAt=exit_time,
                   exit=trade["exit"], pnl=trade["pnl"], reason=trade["reason"],
                   comparable=entry is not None and exit_time is not None and exit_time > entry)
    db.execute("UPDATE advisor_rounds SET closed=?,outcome=? WHERE id=? AND account=? AND closed IS NULL",
               (outcome["closedAt"], json.dumps(outcome), trade["advisorRoundId"], sid))


def latest_round(db, sid, provider):
    row = db.execute("SELECT id, report FROM advisor_rounds WHERE account=? AND provider=? ORDER BY id DESC LIMIT 1",
                     (sid, provider)).fetchone()
    return dict(json.loads(row["report"]), id=row["id"]) if row else None


def journal(db, sid, provider, now, requested=None):
    recent = []
    rows = list(db.execute("""SELECT id,input,report,outcome FROM advisor_rounds
                              WHERE account=? AND provider=? ORDER BY id DESC LIMIT 20""", (sid, provider)))
    if requested is not None and not any(r["id"] == requested for r in rows):
        row = db.execute("""SELECT id,input,report,outcome FROM advisor_rounds
                            WHERE account=? AND provider=? AND id=?""", (sid, provider, requested)).fetchone()
        if row:
            rows.insert(0, row)
    for row in rows:
        inputs = json.loads(row["input"])
        risk = inputs["risk"]
        context = (f"Hoofdmodel: {inputs['model'] or 'niet beoordeeld'} · koers: {inputs['quote'] or '—'} · "
                   f"actueel: {'ja' if risk['fresh'] else 'nee'} · capaciteit: {'ja' if risk['capacity'] else 'nee'} · "
                   f"dagruimte: {'ja' if risk['daily_room'] else 'nee'} · geldige omvang: {'ja' if risk['size_ok'] else 'nee'}")
        recent.append(dict(round=dict(json.loads(row["report"]), id=row["id"]), candles=inputs["candles"],
                           context=context, outcome=json.loads(row["outcome"]) if row["outcome"] else None))
    totals = {}
    for row in db.execute("""SELECT report,outcome FROM advisor_rounds
                             WHERE account=? AND provider=? AND closed>=? ORDER BY closed""", (sid, provider, now - 365*86400)):
        report = json.loads(row["report"])
        outcome = json.loads(row["outcome"])
        if not outcome["comparable"]:
            continue
        for vote in report["votes"]:
            if vote["vote"] != report["recommendation"]:
                continue
            key = vote["agent"], vote["version"], vote["confidence"]
            stats = totals.setdefault(key, dict(agent=key[0], version=key[1], confidence=key[2], trades=0, wins=0, netPnl=0))
            stats["trades"] += 1
            stats["wins"] += outcome["pnl"] > 0
            stats["netPnl"] += outcome["pnl"]
    for stats in totals.values():
        stats["netPnl"] = round(stats["netPnl"], 2)
    return dict(provider=provider, rounds=recent, stats=list(totals.values()))
