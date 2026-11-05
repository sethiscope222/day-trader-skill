#!/usr/bin/env python3
"""Trade Journal for trader-journal. Standard library only.

Turns what actually happened into a record of every trade: the setup, the
plan (entry, stop, target), every fill, the slippage against the plan, the
P&L in dollars and in R, and why it ended (stop, target, flat by the close,
or by hand). Then it reports the results by setup, so you can see which
setups make you money and which ones only feel like they do.

  sync     reads the fills (Alpaca's own orders or account activities saved
           as JSON with --fills, else the fills trader-broker logged in
           trader/state/broker.json), pairs buys with sells into trades, and
           attaches the plan from trader/plan.json, trader/orders.json, the
           broker log and trader/timemachine.json. Writes trader/journal.json.
           Safe to run again: a trade keeps its id, its tag and its note.
  show     lists the trades (or one, with every fill).
  tag      sets a trade's setup by hand, and a note.
  report   results by setup (or by symbol, or by how the trade ended).

    python3 journal.py sync
    python3 journal.py sync --fills trader/alpaca-fills.json
    python3 journal.py show
    python3 journal.py show --id 2026-10-19-TSLA-1
    python3 journal.py tag --id 2026-10-19-TSLA-1 --setup orb --note "chased the entry"
    python3 journal.py report
    python3 journal.py report --by exit --from 2026-10-01 --json

It never talks to a broker. Long trades only, like the rest of the pack.
Dates and times are New York time.
"""

import argparse
import datetime
import json
import os
import re
import sys

TOL = 0.02          # a fill within 2 cents of the stop or target counts as hitting it
EPS = 1e-9


class JournalError(Exception):
    pass


# ---------------------------------------------------------------- time (New York, no tz files)

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    """Hours from UTC for New York: -4 in daylight time, -5 otherwise (US rule since 2007)."""
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))
    naive = utc.replace(tzinfo=None)
    return -4 if start <= naive < end else -5


def to_eastern(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=eastern_offset(utc))


def eastern_to_utc(local):
    for off in (-4, -5):
        utc = local - datetime.timedelta(hours=off)
        if eastern_offset(utc) == off:
            return utc
    return local + datetime.timedelta(hours=5)


def parse_time(text):
    """An ISO time -> naive UTC. A time without an offset is New York time.
    Takes Alpaca's "2026-10-19T14:15:03.123456789Z" (nanoseconds are cut to micro)."""
    if text is None or text == "":
        return None
    t = str(text).strip().replace(" ", "T", 1) if "T" not in str(text) else str(text).strip()
    t = t.replace("Z", "+00:00")
    m = re.match(r"^(.*?T[\d:]+)\.(\d+)(.*)$", t)
    if m:  # Python 3.8 takes exactly 3 or 6 digits of a second
        t = "%s.%s%s" % (m.group(1), (m.group(2) + "000000")[:6], m.group(3))
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise JournalError("cannot read the time %r" % text)
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def ny_iso(utc):
    return "%s%+03d:00" % (to_eastern(utc).replace(microsecond=0).isoformat(), eastern_offset(utc))


def ny_date(utc):
    return to_eastern(utc).date().isoformat()


def ny_hhmm(utc):
    return to_eastern(utc).strftime("%H:%M")


def clock_minutes(text):
    """ "15:55", "3:55 PM", "10:15 AM" -> minutes after midnight, or None."""
    if not text:
        return None
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*$", str(text))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    ap = (m.group(3) or "").lower()
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return h * 60 + mi


# ---------------------------------------------------------------- files

def read_json(path, what, required=False):
    if not path or not os.path.exists(path):
        if required:
            raise JournalError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise JournalError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _f(x):
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(str(x).replace(",", "").replace("$", ""))
    except ValueError:
        return None
    return v


def _r2(x):
    return None if x is None else round(x + 0.0, 2)


def _money(v):
    if v is None:
        return "n/a"
    s = "-" if v < -0.004 else ""
    return "%s$%s" % (s, "{:,.2f}".format(abs(v)))


def _smoney(v):
    if v is None:
        return "n/a"
    return ("+" if v > 0.004 else "") + _money(v)


def _qty(v):
    return str(int(v)) if abs(v - round(v)) < EPS else ("%.6f" % v).rstrip("0").rstrip(".")


# ---------------------------------------------------------------- fills

def _role_of_order(o):
    """What an Alpaca order was for, from its type: a stop leg, a limit (the target), or market."""
    t = str(o.get("order_type") or o.get("type") or "").lower()
    if t in ("stop", "stop_limit", "trailing_stop"):
        return "stop"
    if t == "limit":
        return "limit"
    if t == "market":
        return "market"
    return None


def _flatten_orders(items):
    for o in items:
        if not isinstance(o, dict):
            continue
        yield o
        for leg in o.get("legs") or []:
            if isinstance(leg, dict):
                for x in _flatten_orders([leg]):
                    yield x


def normalize_fills(doc, source="alpaca"):
    """Fills from any of: Alpaca orders (get_orders, place_stock_order's response, with nested legs),
    Alpaca account activities (activity_type FILL), trader-broker's log, or a plain list of
    {"symbol", "side", "qty", "price", "at"}. -> [{"symbol","side","qty","price","utc","role","order_id","source"}]"""
    if doc is None:
        return []
    if isinstance(doc, dict):
        items = None
        for k in ("fills", "activities", "orders", "data", "items"):
            if isinstance(doc.get(k), list):
                items = doc[k]
                break
        if items is None:
            items = [doc]
    elif isinstance(doc, list):
        items = doc
    else:
        raise JournalError("the fills file should hold a JSON list or an object with \"orders\" or \"activities\"")
    out = []
    for o in _flatten_orders(items):
        if "activity_type" in o:
            if str(o.get("activity_type")).upper() != "FILL":
                continue
            qty, price, when = _f(o.get("qty")), _f(o.get("price")), o.get("transaction_time")
            role, oid = None, o.get("order_id")
        elif "filled_avg_price" in o or "filled_qty" in o:
            status = str(o.get("status") or "").lower()
            qty, price = _f(o.get("filled_qty")), _f(o.get("filled_avg_price"))
            when = o.get("filled_at") or o.get("updated_at")
            if status in ("canceled", "expired", "rejected") and not qty:
                continue
            role, oid = _role_of_order(o), o.get("id")
        elif o.get("status") == "filled" or ("price" in o and "side" in o):
            if "status" in o and o.get("status") != "filled":
                continue
            qty, price, when = _f(o.get("qty")), _f(o.get("price")), o.get("at") or o.get("time")
            role, oid = o.get("role"), o.get("order_id") or o.get("broker_order_id")
        else:
            continue
        if not qty or not price or qty <= 0 or price <= 0:
            continue
        side = str(o.get("side") or "").lower()
        if side.startswith("buy"):
            side = "buy"
        elif side.startswith("sell"):
            side = "sell"
        else:
            continue
        utc = parse_time(when)
        if utc is None:
            raise JournalError("a %s fill of %s has no time" % (source, o.get("symbol")))
        out.append({"symbol": str(o.get("symbol") or "").upper(), "side": side, "qty": qty, "price": price,
                    "utc": utc, "role": role, "order_id": oid, "source": source})
    seen, uniq = set(), []
    for x in sorted(out, key=lambda x: (x["utc"], x["side"] != "buy")):
        key = (x["symbol"], x["side"], round(x["qty"], 6), round(x["price"], 4), x["utc"], x["order_id"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(x)
    return uniq


def pair_trades(fills):
    """Fills of one symbol on one day, in time order -> trades: a trade opens on a buy from flat and
    closes when the position is flat again. A sell with nothing open is left out (and reported)."""
    trades, cur, pos, orphans = [], None, 0.0, []
    for x in fills:
        if x["side"] == "buy":
            if cur is None:
                cur = {"buys": [], "sells": []}
            cur["buys"].append(x)
            pos += x["qty"]
        else:
            if cur is None or pos <= EPS:
                orphans.append(x)
                continue
            take = min(x["qty"], pos)
            if take + EPS < x["qty"]:
                orphans.append(dict(x, qty=x["qty"] - take))
                x = dict(x, qty=take)
            cur["sells"].append(x)
            pos -= take
            if pos <= EPS:
                trades.append(cur)
                cur, pos = None, 0.0
    if cur is not None:
        trades.append(cur)
    return trades, orphans


# ---------------------------------------------------------------- the plan

PLAN_KEYS = ("entry", "stop", "target", "risk_per_share", "entry_time", "flat_by", "setup", "max_loss",
             "stop_label", "target_label", "target_r")


def plans_from_sources(plan_doc, orders_doc, broker_state, tm_doc):
    """{(date, SYMBOL): {plan fields, "tm": {...}}} from what the other skills wrote."""
    plans = {}

    def put(key, fields, overwrite):
        cur = plans.setdefault(key, {})
        for k, v in fields.items():
            if v is None:
                continue
            if overwrite or k not in cur:
                cur[k] = v

    # the broker log: one approved entry per symbol and day carries its stop and max loss
    for r in (broker_state or {}).get("orders", []):
        if r.get("status") in ("approved", "placed") and r.get("kind", "entry") == "entry" and r.get("side") == "buy":
            day = str(r.get("date") or str(r.get("at", ""))[:10])
            put((day, str(r.get("symbol", "")).upper()), {"stop": _f(r.get("stop")), "max_loss": _f(r.get("max_loss"))},
                False)
    # trader/orders.json: each order's plan (the budget manager writes it), dated by created_at
    if isinstance(orders_doc, dict):
        made = parse_time(orders_doc.get("created_at")) if orders_doc.get("created_at") else None
        for o in orders_doc.get("orders", []):
            p = o.get("plan") if isinstance(o.get("plan"), dict) else None
            if not p or str(o.get("side", "")).lower() != "buy":
                continue
            day = p.get("date") or (ny_date(made) if made else None)
            if not day:
                continue
            f = {k: p.get(k) for k in PLAN_KEYS}
            f["stop"] = f.get("stop") if f.get("stop") is not None else _f(o.get("stop_loss_stop_price"))
            f["target"] = f.get("target") if f.get("target") is not None else _f(o.get("take_profit_limit_price"))
            put((day, str(o.get("symbol", "")).upper()), f, True)
    # trader/plan.json: the exit planner's plan, the fullest source
    if isinstance(plan_doc, dict) and plan_doc.get("symbol") and plan_doc.get("date") \
            and str(plan_doc.get("side", "buy")).lower() == "buy":
        put((plan_doc["date"], str(plan_doc["symbol"]).upper()), {k: plan_doc.get(k) for k in PLAN_KEYS}, True)
    # trader/timemachine.json: the odds it gave this plan
    if isinstance(tm_doc, dict) and isinstance(tm_doc.get("plan"), dict) and isinstance(tm_doc.get("mc"), dict):
        tp, mc = tm_doc["plan"], tm_doc["mc"]
        if tp.get("symbol") and tp.get("date"):
            key = (tp["date"], str(tp["symbol"]).upper())
            put(key, {k: tp.get(k) for k in PLAN_KEYS}, False)
            tm = {k: mc.get(k) for k in ("p_target", "p_stop", "p_neither", "avg_R", "replays", "seed")}
            if isinstance(tm_doc.get("history"), dict) and tm_doc["history"].get("triggered") is not None:
                h = tm_doc["history"]
                tm["history_triggered"] = h.get("triggered")
                trs = h.get("trades") or []
                tm["history_target"] = sum(1 for t in trs if t.get("outcome") == "target")
                tm["history_stop"] = sum(1 for t in trs if t.get("outcome") == "stop")
            tm["verdict"] = tm_doc.get("verdict")
            plans.setdefault(key, {})["tm"] = {k: v for k, v in tm.items() if v is not None}
    for p in plans.values():
        if p.get("risk_per_share") is None and p.get("entry") is not None and p.get("stop") is not None:
            p["risk_per_share"] = round(float(p["entry"]) - float(p["stop"]), 4)
    return plans


# ---------------------------------------------------------------- one trade

def exit_reason(fill, plan, flat_by):
    stop, target = _f(plan.get("stop")), _f(plan.get("target"))
    role = fill.get("role")
    if role == "stop":
        return "stop"
    if role == "limit" and target is not None and abs(fill["price"] - target) <= max(TOL, 0.001 * target):
        return "target"
    if role not in ("market",):
        if stop is not None and fill["price"] <= stop + TOL:
            return "stop"
        if target is not None and fill["price"] >= target - TOL:
            return "target"
    fb = clock_minutes(flat_by)
    t = to_eastern(fill["utc"])
    if fb is not None and t.hour * 60 + t.minute >= fb - 1:
        return "flat"
    return "manual"


def build_trade(tid, symbol, day, legs, plan, fee_per_share, flat_by_default):
    buys, sells = legs["buys"], legs["sells"]
    qty_in = sum(x["qty"] for x in buys)
    qty_out = sum(x["qty"] for x in sells)
    cost_in = sum(x["qty"] * x["price"] for x in buys)
    avg_in = cost_in / qty_in
    avg_out = (sum(x["qty"] * x["price"] for x in sells) / qty_out) if qty_out > EPS else None
    closed = qty_out + EPS >= qty_in
    flat_by = plan.get("flat_by") or flat_by_default
    entry_p, stop, target = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    rps = _f(plan.get("risk_per_share"))
    if rps is None and entry_p is not None and stop is not None:
        rps = entry_p - stop

    fills = []
    for x in buys:
        fills.append({"at": ny_iso(x["utc"]), "side": "buy", "qty": x["qty"], "price": x["price"],
                      "role": x.get("role"), "order_id": x.get("order_id"), "source": x["source"]})
    exit_slip, reasons = 0.0, []
    exit_slip_known = False
    for x in sells:
        why = exit_reason(x, plan, flat_by)
        reasons.append(why)
        slip = None
        if why == "stop" and stop is not None:
            slip = (stop - x["price"]) * x["qty"]
        elif why == "target" and target is not None:
            slip = (target - x["price"]) * x["qty"]
        if slip is not None:
            exit_slip += slip
            exit_slip_known = True
        fills.append({"at": ny_iso(x["utc"]), "side": "sell", "qty": x["qty"], "price": x["price"],
                      "exit": why, "slippage": _r2(slip), "role": x.get("role"), "order_id": x.get("order_id"),
                      "source": x["source"]})

    entry_slip_ps = (avg_in - entry_p) if entry_p is not None else None
    entry_slip = entry_slip_ps * qty_in if entry_slip_ps is not None else None
    slip_total = None
    if entry_slip is not None or exit_slip_known:
        slip_total = (entry_slip or 0.0) + (exit_slip if exit_slip_known else 0.0)

    t = {"id": tid, "date": day, "symbol": symbol, "side": "long", "setup": plan.get("setup") or "untagged",
         "status": "closed" if closed else "open",
         "qty": round(qty_in, 6), "entry_price": round(avg_in, 4),
         "entry_at": ny_iso(buys[0]["utc"]),
         "exit_price": round(avg_out, 4) if avg_out is not None else None,
         "exit_at": ny_iso(sells[-1]["utc"]) if sells else None,
         "exit": (reasons[-1] if len(set(reasons)) == 1 else "mixed") if closed and reasons else None,
         "plan": {k: plan.get(k) for k in ("entry", "stop", "target", "risk_per_share", "entry_time", "flat_by",
                                           "max_loss") if plan.get(k) is not None},
         "fills": fills, "fee_per_share": fee_per_share,
         "slippage": {"entry_per_share": None if entry_slip_ps is None else round(entry_slip_ps, 4), "entry": _r2(entry_slip),
                      "exit": _r2(exit_slip) if exit_slip_known else None, "total": _r2(slip_total)}}
    if plan.get("tm"):
        t["timemachine"] = plan["tm"]
    risk = rps * qty_in if rps is not None and rps > 0 else None
    t["risk"] = _r2(risk)
    if closed:
        gross = sum(x["qty"] * x["price"] for x in sells) - cost_in
        fees = fee_per_share * (qty_in + qty_out)
        net = gross - fees
        t.update({"gross_pnl": _r2(gross), "fees": _r2(fees), "pnl": _r2(net),
                  "r": round(net / risk, 3) if risk else None,
                  "result": "win" if net > 0.004 else ("loss" if net < -0.004 else "scratch")})
        if risk and plan.get("max_loss") is not None:
            t["loss_vs_plan"] = _r2(-net - float(plan["max_loss"])) if net < 0 else None
        hold = (sells[-1]["utc"] - buys[0]["utc"]).total_seconds() / 60.0
        t["minutes_held"] = int(round(hold))
    else:
        t["open_qty"] = round(qty_in - qty_out, 6)
        if sells:
            t["realized_pnl"] = _r2(sum(x["qty"] * (x["price"] - avg_in) for x in sells))
    return t


def sync(fills, plans, journal, fee_per_share=0.0, flat_by_default="15:55", only_date=None):
    """All fills -> trades, merged into the journal document. Returns (journal, summary)."""
    old = {t["id"]: t for t in journal.get("trades", [])}
    groups = {}
    for x in fills:
        if not x["symbol"]:
            continue
        groups.setdefault((ny_date(x["utc"]), x["symbol"]), []).append(x)
    new_ids, orphans, touched = [], [], set()
    for (day, sym) in sorted(groups):
        if only_date and day != only_date:
            continue
        touched.add((day, sym))
        trades, orph = pair_trades(sorted(groups[(day, sym)], key=lambda x: (x["utc"], x["side"] != "buy")))
        orphans.extend(orph)
        for i, legs in enumerate(trades, 1):
            tid = "%s-%s-%d" % (day, sym, i)
            prev = old.get(tid, {})
            plan = dict(prev.get("plan") or {})
            if prev.get("setup") and prev.get("setup") != "untagged":
                plan.setdefault("setup", prev["setup"])
            if prev.get("timemachine"):
                plan.setdefault("tm", prev["timemachine"])
            fresh = plans.get((day, sym), {})
            for k, v in fresh.items():
                if v is not None:
                    plan[k] = v
            t = build_trade(tid, sym, day, legs, plan, fee_per_share, flat_by_default)
            if prev.get("tagged"):
                t["setup"], t["tagged"] = prev["setup"], True
            if prev.get("note"):
                t["note"] = prev["note"]
            if tid not in old:
                new_ids.append(tid)
            old[tid] = t
    # trades of a day and symbol that was re-read but now has fewer trades are dropped
    keep = []
    for tid, t in old.items():
        if (t["date"], t["symbol"]) in touched:
            n = int(tid.rsplit("-", 1)[1])
            trades_now = len(pair_trades(sorted(groups[(t["date"], t["symbol"])],
                                                key=lambda x: (x["utc"], x["side"] != "buy")))[0])
            if n > trades_now:
                continue
        keep.append(t)
    keep.sort(key=lambda t: (t["date"], t["entry_at"], t["symbol"]))
    journal["trades"] = keep
    return journal, {"new": new_ids, "orphans": orphans, "days": sorted(touched)}


# ---------------------------------------------------------------- the report

def _group_key(t, by):
    if by == "symbol":
        return t["symbol"]
    if by == "exit":
        return t.get("exit") or "open"
    if by == "month":
        return t["date"][:7]
    return t.get("setup") or "untagged"


def stats(trades):
    closed = [t for t in trades if t["status"] == "closed"]
    wins = [t for t in closed if t.get("result") == "win"]
    losses = [t for t in closed if t.get("result") == "loss"]
    won = sum(t["pnl"] for t in wins)
    lost = -sum(t["pnl"] for t in losses)
    net = sum(t["pnl"] for t in closed)
    rs = [t["r"] for t in closed if t.get("r") is not None]
    slips = [t["slippage"]["total"] for t in closed if t["slippage"].get("total") is not None]
    eslips = [t["slippage"]["entry_per_share"] for t in closed if t["slippage"].get("entry_per_share") is not None]
    exits = {}
    for t in closed:
        exits[t.get("exit") or "?"] = exits.get(t.get("exit") or "?", 0) + 1
    s = {"trades": len(closed), "open": len(trades) - len(closed), "wins": len(wins), "losses": len(losses),
         "scratches": len(closed) - len(wins) - len(losses),
         "win_rate": round(len(wins) / len(closed), 4) if closed else None,
         "net_pnl": _r2(net), "gross_pnl": _r2(sum(t["gross_pnl"] for t in closed)),
         "fees": _r2(sum(t.get("fees") or 0 for t in closed)),
         "avg_pnl": _r2(net / len(closed)) if closed else None,
         "avg_win": _r2(won / len(wins)) if wins else None,
         "avg_loss": _r2(-lost / len(losses)) if losses else None,
         "profit_factor": round(won / lost, 2) if lost > 0 else None,
         "avg_r": round(sum(rs) / len(rs), 3) if rs else None, "total_r": round(sum(rs), 3) if rs else None,
         "r_trades": len(rs),
         "slippage": _r2(sum(slips)) if slips else None,
         "avg_entry_slippage_per_share": round(sum(eslips) / len(eslips), 4) if eslips else None,
         "best": _r2(max(t["pnl"] for t in closed)) if closed else None,
         "worst": _r2(min(t["pnl"] for t in closed)) if closed else None,
         "exits": exits}
    tm = [t for t in closed if t.get("timemachine", {}).get("p_target") is not None]
    if tm:
        s["timemachine"] = {"trades": len(tm),
                            "said_target_first": round(sum(t["timemachine"]["p_target"] for t in tm) / len(tm), 4),
                            "hit_target": round(sum(1 for t in tm if t.get("exit") == "target") / len(tm), 4),
                            "said_stop_first": round(sum(t["timemachine"].get("p_stop", 0) for t in tm) / len(tm), 4),
                            "hit_stop": round(sum(1 for t in tm if t.get("exit") == "stop") / len(tm), 4)}
    return s


def select(trades, date_from=None, date_to=None, symbol=None, setup=None):
    out = []
    for t in trades:
        if date_from and t["date"] < date_from:
            continue
        if date_to and t["date"] > date_to:
            continue
        if symbol and t["symbol"] != symbol.upper():
            continue
        if setup and t.get("setup") != setup:
            continue
        out.append(t)
    return out


def report(trades, by="setup"):
    groups = {}
    for t in trades:
        groups.setdefault(_group_key(t, by), []).append(t)
    rows = [dict(stats(ts), group=k) for k, ts in groups.items()]
    rows.sort(key=lambda r: (-(r["net_pnl"] or 0), r["group"]))
    return {"by": by, "groups": rows, "total": stats(trades)}


def _pct(x):
    return "n/a" if x is None else "%.0f %%" % (100 * x)


def render_report(rep, label):
    lines = ["Trade journal, by %s%s" % (rep["by"], label), ""]
    head = "%-14s %6s %5s %8s %11s %10s %7s %6s %10s" % (rep["by"], "trades", "wins", "win rate", "net P&L",
                                                         "avg/trade", "avg R", "PF", "slippage")
    lines.append(head)
    lines.append("-" * len(head))

    def row(name, s):
        pf = "n/a" if s["profit_factor"] is None else ("%.2f" % s["profit_factor"])
        ar = "n/a" if s["avg_r"] is None else ("%+.2f" % s["avg_r"])
        return "%-14s %6d %5d %8s %11s %10s %7s %6s %10s" % (
            name[:14], s["trades"], s["wins"], _pct(s["win_rate"]), _smoney(s["net_pnl"]), _smoney(s["avg_pnl"]),
            ar, pf, _money(s["slippage"]) if s["slippage"] is not None else "n/a")

    for r in rep["groups"]:
        lines.append(row(r["group"], r))
    lines.append("-" * len(head))
    tot = rep["total"]
    lines.append(row("all", tot))
    lines.append("")
    if tot["trades"]:
        ex = ", ".join("%s %d" % (k, v) for k, v in sorted(tot["exits"].items(), key=lambda kv: -kv[1]))
        lines.append("How they ended: %s." % ex)
        if tot["avg_entry_slippage_per_share"] is not None:
            lines.append("Entry fills averaged %s a share %s the planned entry." % (
                _money(abs(tot["avg_entry_slippage_per_share"])),
                "above" if tot["avg_entry_slippage_per_share"] >= 0 else "below"))
        if tot["slippage"] is not None:
            lines.append("Slippage against the plan cost %s in all (positive is a cost; a stop that fills past "
                         "its price shows up here)." % _money(tot["slippage"]))
        tm = tot.get("timemachine")
        if tm:
            lines.append("Time Machine check (%d trade%s): it said target first %s, the target was hit %s; "
                         "it said stop first %s, the stop was hit %s."
                         % (tm["trades"], "" if tm["trades"] == 1 else "s", _pct(tm["said_target_first"]),
                            _pct(tm["hit_target"]), _pct(tm["said_stop_first"]), _pct(tm["hit_stop"])))
        if tot["trades"] < 20:
            lines.append("%d closed trade%s is a small sample: one more win or loss moves these numbers a lot."
                         % (tot["trades"], "" if tot["trades"] == 1 else "s"))
    else:
        lines.append("No closed trades in this range.")
    if tot["open"]:
        lines.append("%d trade%s still open, not counted above." % (tot["open"], "" if tot["open"] == 1 else "s"))
    return "\n".join(lines)


def render_trade_line(t):
    if t["status"] == "closed":
        r = "" if t.get("r") is None else " (%+.2fR)" % t["r"]
        return "%-22s %-9s %4s %s @ %.2f -> %.2f  %-7s %s%s" % (
            t["id"], t.get("setup"), _qty(t["qty"]), t["symbol"], t["entry_price"], t["exit_price"],
            t.get("exit") or "", _smoney(t["pnl"]), r)
    return "%-22s %-9s %4s %s @ %.2f  OPEN (%s shares left)" % (
        t["id"], t.get("setup"), _qty(t["qty"]), t["symbol"], t["entry_price"], _qty(t["open_qty"]))


def render_trade(t):
    lines = [render_trade_line(t), ""]
    p = t.get("plan") or {}
    if p:
        lines.append("  plan: entry %s, stop %s, target %s, risk %s a share%s" % (
            p.get("entry", "n/a"), p.get("stop", "n/a"), p.get("target", "n/a"), p.get("risk_per_share", "n/a"),
            (", flat by %s" % p["flat_by"]) if p.get("flat_by") else ""))
    else:
        lines.append("  plan: none found (tag it: journal.py tag --id %s --setup NAME)" % t["id"])
    for f in t["fills"]:
        extra = ""
        if f["side"] == "sell":
            extra = "  %s" % f.get("exit")
            if f.get("slippage") is not None:
                extra += ", slippage %s" % _money(f["slippage"])
        lines.append("  %s  %-4s %s @ %.2f%s" % (f["at"], f["side"].upper(), _qty(f["qty"]), f["price"], extra))
    s = t["slippage"]
    if s.get("entry_per_share") is not None:
        lines.append("  entry slippage %s a share (%s)" % (_smoney(s["entry_per_share"]), _money(s["entry"])))
    if t["status"] == "closed":
        lines.append("  P&L %s gross, %s fees, %s net%s" % (_smoney(t["gross_pnl"]), _money(t["fees"]),
                                                            _smoney(t["pnl"]),
                                                            "" if t.get("r") is None else ", %+.2fR" % t["r"]))
        if t.get("loss_vs_plan") is not None and t["loss_vs_plan"] > 0.004:
            lines.append("  lost %s more than the plan's max loss of %s" % (_money(t["loss_vs_plan"]),
                                                                         _money(t["plan"]["max_loss"])))
    tm = t.get("timemachine")
    if tm and tm.get("p_target") is not None:
        lines.append("  Time Machine said: target first %s, stop first %s" % (_pct(tm["p_target"]),
                                                                            _pct(tm.get("p_stop"))))
    if t.get("note"):
        lines.append("  note: %s" % t["note"])
    return "\n".join(lines)


# ---------------------------------------------------------------- commands

def load_journal(path):
    doc = read_json(path, "journal") or {}
    doc.setdefault("trades", [])
    return doc


def cmd_sync(a):
    cfg = read_json(a.config, "config") or {}
    fee = a.fee_per_share if a.fee_per_share is not None else float(cfg.get("fee_per_share", 0) or 0)
    broker = read_json(a.state, "broker log") or {}
    fills = []
    if a.fills:
        for path in a.fills:
            fills.extend(normalize_fills(read_json(path, "fills file", required=True), source="alpaca"))
        have = {(ny_date(x["utc"]), x["symbol"]) for x in fills}
        for x in normalize_fills(broker.get("orders", []), source="broker log"):
            if (ny_date(x["utc"]), x["symbol"]) not in have:
                fills.append(x)
    else:
        fills = normalize_fills(broker.get("orders", []), source="broker log")
    if not fills:
        raise JournalError("no fills to read: none in %s%s. Record fills with trader-broker (ticket.py fill), "
                           "or save Alpaca's filled orders as JSON and pass --fills FILE"
                           % (a.state, "" if not a.fills else " or " + ", ".join(a.fills)))
    plans = plans_from_sources(read_json(a.plan, "plan"), read_json(a.orders, "orders"), broker,
                               read_json(a.timemachine, "time machine"))
    journal = load_journal(a.journal)
    journal, summary = sync(fills, plans, journal, fee, cfg.get("flat_by") or "15:55", a.date)
    if not summary["days"]:
        print("No fills on %s." % a.date)
        return 1
    journal["created_by"] = "trader-journal"
    now = parse_time(a.now) if a.now else \
        datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    journal["updated_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    write_json(a.journal, journal)
    touched = set(summary["days"])
    these = [t for t in journal["trades"] if (t["date"], t["symbol"]) in touched]
    print("Journal: %d trade%s from %d day/symbol pair%s, %d new. Saved %s." % (
        len(these), "" if len(these) == 1 else "s", len(touched), "" if len(touched) == 1 else "s",
        len(summary["new"]), a.journal))
    for t in these:
        print("  " + render_trade_line(t))
    untagged = [t["id"] for t in these if t.get("setup") == "untagged"]
    if untagged:
        print("No plan found for %s. Tag the setup: journal.py tag --id ID --setup NAME" % ", ".join(untagged))
    for x in summary["orphans"]:
        print("Left out: SELL %s %s at %.2f on %s, with no buy before it that day (a position from an earlier "
              "day, or a short)." % (_qty(x["qty"]), x["symbol"], x["price"], ny_iso(x["utc"])))
    return 0


def cmd_show(a):
    journal = load_journal(a.journal)
    trades = journal["trades"]
    if a.id:
        t = [x for x in trades if x["id"] == a.id]
        if not t:
            raise JournalError("no trade %s in %s" % (a.id, a.journal))
        print(json.dumps(t[0], indent=2) if a.json else render_trade(t[0]))
        return 0
    trades = select(trades, a.date_from or a.date, a.date_to or a.date, a.symbol, a.setup)
    if a.json:
        print(json.dumps(trades, indent=2))
        return 0
    if not trades:
        print("No trades in %s%s." % (a.journal, "" if not (a.date or a.date_from or a.symbol or a.setup)
                                      else " for that filter"))
        return 1
    for t in trades:
        print(render_trade_line(t))
    return 0


def rebuild(t, plan_changes, flat_by_default="15:55"):
    """Rebuild a saved trade from its own fills, with plan fields changed by hand."""
    legs = {"buys": [], "sells": []}
    for f in t["fills"]:
        x = {"symbol": t["symbol"], "side": f["side"], "qty": float(f["qty"]), "price": float(f["price"]),
             "utc": parse_time(f["at"]), "role": f.get("role"), "order_id": f.get("order_id"),
             "source": f.get("source", "journal")}
        legs["buys" if f["side"] == "buy" else "sells"].append(x)
    plan = dict(t.get("plan") or {})
    plan.update({k: v for k, v in plan_changes.items() if v is not None})
    if any(k in plan_changes for k in ("entry", "stop")) and plan_changes.get("risk_per_share") is None \
            and plan.get("entry") is not None and plan.get("stop") is not None:
        plan["risk_per_share"] = round(float(plan["entry"]) - float(plan["stop"]), 4)
    plan["setup"] = t.get("setup")
    if t.get("timemachine"):
        plan["tm"] = t["timemachine"]
    new = build_trade(t["id"], t["symbol"], t["date"], legs, plan, float(t.get("fee_per_share") or 0),
                      flat_by_default)
    for k in ("tagged", "note"):
        if t.get(k) is not None:
            new[k] = t[k]
    return new


def cmd_tag(a):
    journal = load_journal(a.journal)
    for i, t in enumerate(journal["trades"]):
        if t["id"] == a.id:
            if a.setup:
                if not re.match(r"^[A-Za-z0-9_.-]{1,30}$", a.setup):
                    raise JournalError("--setup wants a short name like orb, level, vwap-bounce")
                t["setup"], t["tagged"] = a.setup, True
            if a.note is not None:
                t["note"] = a.note
            changes = {"entry": a.entry, "stop": a.stop, "target": a.target}
            if any(v is not None for v in changes.values()):
                e = changes["entry"] if changes["entry"] is not None else (t.get("plan") or {}).get("entry")
                st = changes["stop"] if changes["stop"] is not None else (t.get("plan") or {}).get("stop")
                if e is not None and st is not None and float(st) >= float(e):
                    raise JournalError("the stop %s is not below the entry %s (long trades only)" % (st, e))
                t = rebuild(t, {k: v for k, v in changes.items() if v is not None})
                journal["trades"][i] = t
            write_json(a.journal, journal)
            print("Tagged %s: setup %s%s." % (a.id, t["setup"], (", note \"%s\"" % t["note"]) if t.get("note") else ""))
            return 0
    raise JournalError("no trade %s in %s" % (a.id, a.journal))


def cmd_report(a):
    journal = load_journal(a.journal)
    for d in (a.date_from, a.date_to):
        if d and not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
            raise JournalError("dates look like 2026-10-19")
    trades = select(journal["trades"], a.date_from, a.date_to, a.symbol, a.setup)
    rep = report(trades, a.by)
    if a.json:
        print(json.dumps(rep, indent=2))
        return 0
    label = ""
    if a.date_from or a.date_to:
        label = ", %s to %s" % (a.date_from or "start", a.date_to or "today")
    print(render_report(rep, label))
    return 0 if rep["total"]["trades"] else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Trade journal: fills -> trades, P&L and slippage by setup.")
    sub = ap.add_subparsers(dest="cmd")

    def jpath(p):
        p.add_argument("--journal", default="trader/journal.json")

    p = sub.add_parser("sync", help="read the fills and the plans, write trader/journal.json")
    jpath(p)
    p.add_argument("--fills", action="append", metavar="FILE",
                   help="Alpaca orders or account activities saved as JSON (repeatable)")
    p.add_argument("--state", default="trader/state/broker.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--orders", default="trader/orders.json")
    p.add_argument("--timemachine", default="trader/timemachine.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--fee-per-share", type=float, help="fees per share bought or sold (default: config, else 0)")
    p.add_argument("--date", help="only this New York day, YYYY-MM-DD")
    p.add_argument("--now")
    p = sub.add_parser("show", help="list the trades, or one with --id")
    jpath(p)
    p.add_argument("--id")
    p.add_argument("--date")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    p.add_argument("--symbol")
    p.add_argument("--setup")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("tag", help="set a trade's setup and note")
    jpath(p)
    p.add_argument("--id", required=True)
    p.add_argument("--setup")
    p.add_argument("--note")
    p.add_argument("--entry", type=float, help="the planned entry, for a trade with no plan on file")
    p.add_argument("--stop", type=float, help="the planned stop (gives the trade its R)")
    p.add_argument("--target", type=float, help="the planned target")
    p = sub.add_parser("report", help="results by setup, symbol, exit or month")
    jpath(p)
    p.add_argument("--by", default="setup", choices=["setup", "symbol", "exit", "month"])
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    p.add_argument("--symbol")
    p.add_argument("--setup")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"sync": cmd_sync, "show": cmd_show, "tag": cmd_tag, "report": cmd_report}[a.cmd](a)
    except JournalError as e:
        print("journal.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
