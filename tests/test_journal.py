import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

journal = load("trader-journal", "journal")
ticket = load("trader-broker", "ticket")
check = load("trader-risk", "check")

J = os.path.join(FIX, "trader-journal")
# hand-made in Alpaca's own JSON shapes (get_orders with nested legs; account activities): an
# illustration of fills for the reel's TSLA plan, not the real outcome of that session
TSLA_ORDERS = os.path.join(J, "alpaca-orders-tsla-2026-10-19.json")
ACTIVITIES = os.path.join(J, "alpaca-activities-2026-10-20.json")
PLAN = os.path.join(FIX, "trader-budget", "plan-tsla.json")
ORDERS = os.path.join(FIX, "trader-risk", "orders-tsla.json")
CFG = os.path.join(ROOT, "trader-budget", "config.example.json")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = journal.main(argv)
    return code, out.getvalue(), err.getvalue()


def read(path):
    with open(path) as fh:
        return json.load(fh)


class Fills(unittest.TestCase):
    def test_alpaca_orders_with_nested_legs(self):
        fills = journal.normalize_fills(read(TSLA_ORDERS))
        self.assertEqual([(f["side"], f["qty"], f["price"], f["role"]) for f in fills],
                         [("buy", 8.0, 364.21, "market"), ("sell", 8.0, 358.27, "stop")])
        self.assertEqual(journal.ny_iso(fills[0]["utc"]), "2026-10-19T10:15:02-04:00")
        self.assertEqual(journal.ny_iso(fills[1]["utc"]), "2026-10-19T11:42:10-04:00")

    def test_the_canceled_take_profit_leg_is_not_a_fill(self):
        fills = journal.normalize_fills({"orders": read(TSLA_ORDERS)})
        self.assertNotIn(370.04, [f["price"] for f in fills])

    def test_account_activities_skip_non_fills(self):
        fills = journal.normalize_fills(read(ACTIVITIES))
        self.assertEqual(len(fills), 5)
        self.assertNotIn("KO", [f["symbol"] for f in fills])
        self.assertEqual([f["symbol"] for f in fills][:2], ["AAPL", "AAPL"])  # sorted by time

    def test_broker_log_fills_and_plain_lists(self):
        log = [{"at": "2026-10-19T10:16:00-04:00", "status": "filled", "symbol": "TSLA", "side": "buy",
                "qty": "8", "price": 364.2},
               {"at": "2026-10-19T10:15:00-04:00", "status": "approved", "symbol": "TSLA", "side": "buy"}]
        fills = journal.normalize_fills(log, "broker log")
        self.assertEqual(len(fills), 1)
        plain = journal.normalize_fills([{"symbol": "ko", "side": "buy", "qty": 3, "price": 70, "at": "2026-10-19 10:00"}])
        self.assertEqual((plain[0]["symbol"], journal.ny_hhmm(plain[0]["utc"])), ("KO", "10:00"))

    def test_duplicates_are_counted_once(self):
        doc = read(TSLA_ORDERS)
        self.assertEqual(len(journal.normalize_fills(doc + doc)), 2)

    def test_nanosecond_times(self):
        self.assertEqual(journal.ny_iso(journal.parse_time("2026-10-19T14:15:02.004182913Z")),
                         "2026-10-19T10:15:02-04:00")
        self.assertEqual(journal.ny_iso(journal.parse_time("2026-10-19T14:15:02.51Z")),
                         "2026-10-19T10:15:02-04:00")
        self.assertEqual(journal.ny_iso(journal.parse_time("2026-12-01T15:00:00Z")),
                         "2026-12-01T10:00:00-05:00")

    def test_bad_input(self):
        with self.assertRaises(journal.JournalError):
            journal.normalize_fills("nope")
        with self.assertRaises(journal.JournalError):
            journal.normalize_fills([{"symbol": "X", "side": "buy", "qty": 1, "price": 1, "status": "filled"}])


class Pairing(unittest.TestCase):
    def f(self, side, qty, price, hhmm):
        return {"symbol": "X", "side": side, "qty": float(qty), "price": float(price),
                "utc": journal.parse_time("2026-10-20 " + hhmm), "role": None, "order_id": None, "source": "t"}

    def test_scale_in_and_out_is_one_trade(self):
        fills = [self.f("buy", 5, 10, "10:00"), self.f("buy", 5, 10.2, "10:05"),
                 self.f("sell", 4, 11, "11:00"), self.f("sell", 6, 11.5, "12:00")]
        trades, orphans = journal.pair_trades(fills)
        self.assertEqual((len(trades), orphans), (1, []))

    def test_two_round_trips_and_an_open_one(self):
        fills = [self.f("buy", 1, 10, "10:00"), self.f("sell", 1, 11, "10:30"),
                 self.f("buy", 2, 10, "11:00"), self.f("sell", 2, 9, "11:30"),
                 self.f("buy", 3, 10, "13:00")]
        trades, _ = journal.pair_trades(fills)
        self.assertEqual([len(t["sells"]) for t in trades], [1, 1, 0])

    def test_a_sell_with_nothing_open_is_left_out(self):
        trades, orphans = journal.pair_trades([self.f("sell", 3, 10, "10:00")])
        self.assertEqual((trades, len(orphans)), ([], 1))
        trades, orphans = journal.pair_trades([self.f("buy", 2, 10, "10:00"), self.f("sell", 3, 11, "10:30")])
        self.assertEqual((len(trades), orphans[0]["qty"]), (1, 1.0))
        self.assertEqual(trades[0]["sells"][0]["qty"], 2.0)


class Flow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.j = os.path.join(self.d, "journal.json")
        self.plan = os.path.join(self.d, "plan.json")
        self.orders = os.path.join(self.d, "orders.json")
        self.state = os.path.join(self.d, "state", "broker.json")
        self.tm = os.path.join(self.d, "timemachine.json")
        self.cfg = os.path.join(self.d, "config.json")
        shutil.copy(PLAN, self.plan)
        shutil.copy(ORDERS, self.orders)
        shutil.copy(CFG, self.cfg)

    def tearDown(self):
        shutil.rmtree(self.d)

    def sync(self, *extra):
        return run(["sync", "--journal", self.j, "--plan", self.plan, "--orders", self.orders, "--state", self.state,
                    "--timemachine", self.tm, "--config", self.cfg, "--now", "2026-10-20 16:30"] + list(extra))

    def trade(self, tid):
        return [t for t in read(self.j)["trades"] if t["id"] == tid][0]

    def test_the_reel_trade_stopped_out_with_slippage(self):
        code, text, err = self.sync("--fills", TSLA_ORDERS)
        self.assertEqual(code, 0, err)
        self.assertIn("2026-10-19-TSLA-1", text)
        t = self.trade("2026-10-19-TSLA-1")
        self.assertEqual((t["setup"], t["status"], t["exit"], t["qty"]), ("orb", "closed", "stop", 8))
        self.assertEqual((t["entry_price"], t["exit_price"]), (364.21, 358.27))
        self.assertEqual(t["plan"], {"entry": 364.18, "stop": 358.32, "target": 370.04, "risk_per_share": 5.86,
                                     "entry_time": "10:15 AM", "flat_by": "15:55", "max_loss": 46.88})
        self.assertEqual(t["slippage"], {"entry_per_share": 0.03, "entry": 0.24, "exit": 0.4, "total": 0.64})
        self.assertEqual((t["gross_pnl"], t["pnl"], t["risk"], t["r"]), (-47.52, -47.52, 46.88, -1.014))
        self.assertEqual((t["result"], t["loss_vs_plan"], t["minutes_held"]), ("loss", 0.64, 87))
        self.assertEqual([f["order_id"][:8] for f in t["fills"]], ["61e69015", "b4d2e3f5"])

    def test_the_plan_survives_the_next_plan(self):
        self.sync("--fills", TSLA_ORDERS)
        os.remove(self.plan)
        os.remove(self.orders)
        self.sync("--fills", TSLA_ORDERS)
        t = self.trade("2026-10-19-TSLA-1")
        self.assertEqual((t["setup"], t["r"], t["plan"]["stop"]), ("orb", -1.014, 358.32))

    def test_tags_notes_and_hand_plans(self):
        code, text, _ = self.sync("--fills", ACTIVITIES)
        self.assertEqual(code, 0)
        self.assertIn("No plan found for 2026-10-20-AAPL-1, 2026-10-20-SOFI-1", text)
        self.assertEqual(self.trade("2026-10-20-AAPL-1")["setup"], "untagged")
        self.assertIsNone(self.trade("2026-10-20-AAPL-1")["r"])
        code, text, _ = run(["tag", "--journal", self.j, "--id", "2026-10-20-AAPL-1", "--setup", "level",
                             "--entry", "250.05", "--stop", "248.80", "--target", "252.50", "--note", "clean"])
        self.assertEqual(code, 0)
        t = self.trade("2026-10-20-AAPL-1")
        self.assertEqual((t["setup"], t["note"], t["exit"], t["pnl"], t["r"]), ("level", "clean", "target", 23.84, 1.907))
        self.assertEqual(t["plan"]["risk_per_share"], 1.25)
        self.assertEqual(t["slippage"], {"entry_per_share": 0.066, "entry": 0.66, "exit": 0.0, "total": 0.66})
        self.sync("--fills", ACTIVITIES)  # a re-sync keeps the tag, the note and the hand plan
        t = self.trade("2026-10-20-AAPL-1")
        self.assertEqual((t["setup"], t["note"], t["r"]), ("level", "clean", 1.907))

    def test_tag_refuses_a_stop_above_the_entry_and_a_bad_name(self):
        self.sync("--fills", ACTIVITIES)
        code, _, err = run(["tag", "--journal", self.j, "--id", "2026-10-20-AAPL-1", "--entry", "250", "--stop", "251"])
        self.assertEqual(code, 1)
        self.assertIn("not below the entry", err)
        code, _, err = run(["tag", "--journal", self.j, "--id", "2026-10-20-AAPL-1", "--setup", "a b; c"])
        self.assertEqual(code, 1)
        code, _, err = run(["tag", "--journal", self.j, "--id", "nope", "--setup", "orb"])
        self.assertIn("no trade nope", err)

    def test_flat_by_the_close(self):
        self.sync("--fills", ACTIVITIES)
        t = self.trade("2026-10-20-SOFI-1")
        self.assertEqual((t["exit"], t["pnl"], t["slippage"]["exit"]), ("flat", 17.0, None))

    def test_fees(self):
        self.sync("--fills", ACTIVITIES, "--fee-per-share", "0.005")
        t = self.trade("2026-10-20-SOFI-1")
        self.assertEqual((t["gross_pnl"], t["fees"], t["pnl"]), (17.0, 1.0, 16.0))

    def test_alpaca_fills_win_over_the_broker_log_for_the_same_day(self):
        os.makedirs(os.path.dirname(self.state))
        with open(self.state, "w") as fh:
            json.dump({"orders": [
                {"at": "2026-10-19T10:16:00-04:00", "date": "2026-10-19", "status": "filled", "symbol": "TSLA",
                 "side": "buy", "qty": "8", "price": 364.2},
                {"at": "2026-10-20T10:00:00-04:00", "date": "2026-10-20", "status": "filled", "symbol": "KO",
                 "side": "buy", "qty": "2", "price": 70.0},
                {"at": "2026-10-20T11:00:00-04:00", "date": "2026-10-20", "status": "filled", "symbol": "KO",
                 "side": "sell", "qty": "2", "price": 71.0}]}, fh)
        self.sync("--fills", TSLA_ORDERS)
        trades = read(self.j)["trades"]
        self.assertEqual([t["id"] for t in trades], ["2026-10-19-TSLA-1", "2026-10-20-KO-1"])
        self.assertEqual(trades[0]["qty"], 8)  # not 16
        self.assertEqual(trades[1]["exit"], "manual")
        self.assertEqual(trades[1]["fills"][0]["source"], "broker log")

    def test_the_full_pack_flow_from_the_broker_log(self):
        # risk -> yes -> placed with a fill -> the stop fills: all through trader-broker, then the journal
        os.makedirs(os.path.dirname(self.state))
        risk = os.path.join(self.d, "state", "risk.json")
        pf = os.path.join(self.d, "portfolio.json")
        none = os.path.join(self.d, "none")
        with redirect_stdout(io.StringIO()):
            check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", pf, "--prices", none,
                        "--state", self.state, "--now", "2026-10-19 10:15", "--out", risk])
            base = ["--orders", self.orders, "--config", self.cfg, "--risk", risk, "--state", self.state]
            self.assertEqual(ticket.main(["approve", "--index", "1", "--reply", "yes", "--now", "2026-10-19 10:15"]
                                         + base), 0)
            self.assertEqual(ticket.main(["result", "--index", "1", "--status", "placed", "--broker-id", "abc",
                                          "--filled-qty", "8", "--filled-price", "364.20", "--portfolio", pf,
                                          "--now", "2026-10-19 10:16"] + base), 0)
            self.assertEqual(ticket.main(["fill", "--symbol", "TSLA", "--side", "sell", "--qty", "8", "--price",
                                          "370.04", "--portfolio", pf, "--config", self.cfg, "--state", self.state,
                                          "--now", "2026-10-19 13:05"]), 0)
        code, _, err = self.sync()
        self.assertEqual(code, 0, err)
        t = self.trade("2026-10-19-TSLA-1")
        self.assertEqual((t["exit"], t["pnl"], t["r"], t["result"]), ("target", 46.72, 0.997, "win"))
        self.assertEqual(t["slippage"]["entry"], 0.16)
        self.assertEqual(t["slippage"]["exit"], 0.0)

    def test_timemachine_odds_ride_along(self):
        with open(self.tm, "w") as fh:
            json.dump({"plan": {"symbol": "TSLA", "date": "2026-10-19", "entry": 364.18, "stop": 358.32,
                                "target": 370.04, "setup": "orb"},
                       "mc": {"p_target": 0.294, "p_stop": 0.255, "p_neither": 0.451, "avg_R": 0.01,
                              "replays": 10000, "seed": 19},
                       "history": {"triggered": 33, "trades": [{"outcome": "target"}] * 13 + [{"outcome": "stop"}] * 15
                                   + [{"outcome": "close"}] * 5},
                       "verdict": "close to a coin flip"}, fh)
        self.sync("--fills", TSLA_ORDERS)
        tm = self.trade("2026-10-19-TSLA-1")["timemachine"]
        self.assertEqual((tm["p_target"], tm["history_triggered"], tm["history_target"], tm["history_stop"]),
                         (0.294, 33, 13, 15))
        code, text, _ = run(["report", "--journal", self.j])
        self.assertIn("Time Machine check (1 trade): it said target first 29 %, the target was hit 0 %; "
                      "it said stop first 26 %, the stop was hit 100 %.", " ".join(text.split()))

    def test_no_fills_is_an_error(self):
        code, _, err = self.sync()
        self.assertEqual(code, 1)
        self.assertIn("no fills to read", err)

    def test_an_open_trade_is_not_in_the_totals(self):
        fills = os.path.join(self.d, "fills.json")
        with open(fills, "w") as fh:
            json.dump([{"symbol": "NVDA", "side": "buy", "qty": 5, "price": 140, "at": "2026-10-21 10:00"},
                       {"symbol": "NVDA", "side": "sell", "qty": 2, "price": 141, "at": "2026-10-21 10:30"}], fh)
        self.sync("--fills", fills)
        t = self.trade("2026-10-21-NVDA-1")
        self.assertEqual((t["status"], t["open_qty"], t["realized_pnl"]), ("open", 3, 2.0))
        code, text, _ = run(["report", "--journal", self.j])
        self.assertEqual(code, 1)
        self.assertIn("No closed trades", text)
        self.assertIn("1 trade still open", text)

    def test_resync_drops_a_trade_that_is_no_longer_there(self):
        fills = os.path.join(self.d, "fills.json")
        two = [{"symbol": "KO", "side": "buy", "qty": 1, "price": 70, "at": "2026-10-21 10:00"},
               {"symbol": "KO", "side": "sell", "qty": 1, "price": 71, "at": "2026-10-21 10:30"},
               {"symbol": "KO", "side": "buy", "qty": 1, "price": 70, "at": "2026-10-21 11:00"},
               {"symbol": "KO", "side": "sell", "qty": 1, "price": 69, "at": "2026-10-21 11:30"}]
        with open(fills, "w") as fh:
            json.dump(two, fh)
        self.sync("--fills", fills)
        self.assertEqual(len(read(self.j)["trades"]), 2)
        with open(fills, "w") as fh:
            json.dump(two[:2], fh)
        self.sync("--fills", fills)
        self.assertEqual([t["id"] for t in read(self.j)["trades"]], ["2026-10-21-KO-1"])


class Report(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.j = os.path.join(self.d, "journal.json")
        x = os.path.join(self.d, "missing")
        base = ["sync", "--journal", self.j, "--plan", PLAN, "--orders", ORDERS, "--state", x, "--timemachine", x,
                "--config", CFG]
        self.assertEqual(run(base + ["--fills", TSLA_ORDERS, "--fills", ACTIVITIES])[0], 0)
        run(["tag", "--journal", self.j, "--id", "2026-10-20-AAPL-1", "--setup", "level",
             "--entry", "250.05", "--stop", "248.80", "--target", "252.50"])
        run(["tag", "--journal", self.j, "--id", "2026-10-20-SOFI-1", "--setup", "level", "--stop", "17.60",
             "--entry", "17.85"])

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_by_setup(self):
        code, text, _ = run(["report", "--journal", self.j, "--json"])
        self.assertEqual(code, 0)
        rep = json.loads(text)
        g = {r["group"]: r for r in rep["groups"]}
        self.assertEqual([r["group"] for r in rep["groups"]], ["level", "orb"])  # best first
        self.assertEqual((g["level"]["trades"], g["level"]["wins"], g["level"]["net_pnl"]), (2, 2, 40.84))
        self.assertEqual((g["level"]["win_rate"], g["level"]["profit_factor"]), (1.0, None))
        self.assertEqual(g["level"]["avg_r"], round((1.907 + 0.68) / 2, 3))
        self.assertEqual((g["orb"]["losses"], g["orb"]["net_pnl"], g["orb"]["avg_r"]), (1, -47.52, -1.014))
        tot = rep["total"]
        self.assertEqual((tot["trades"], tot["net_pnl"], tot["win_rate"], tot["profit_factor"]),
                         (3, -6.68, 0.6667, 0.86))
        self.assertEqual(tot["exits"], {"stop": 1, "target": 1, "flat": 1})
        self.assertEqual((tot["best"], tot["worst"], tot["slippage"]), (23.84, -47.52, 1.3))

    def test_text_table(self):
        code, text, _ = run(["report", "--journal", self.j])
        self.assertEqual(code, 0)
        self.assertIn("Trade journal, by setup", text)
        lines = [l.split() for l in text.splitlines()]
        self.assertIn(["level", "2", "2", "100", "%", "+$40.84", "+$20.42", "+1.29", "n/a", "$0.66"], lines)
        self.assertIn(["all", "3", "2", "67", "%", "-$6.68", "-$2.23", "+0.52", "0.86", "$1.30"], lines)
        self.assertIn("3 closed trades is a small sample", text)

    def test_by_exit_symbol_month_and_filters(self):
        rep = json.loads(run(["report", "--journal", self.j, "--by", "exit", "--json"])[1])
        self.assertEqual(sorted(r["group"] for r in rep["groups"]), ["flat", "stop", "target"])
        rep = json.loads(run(["report", "--journal", self.j, "--by", "month", "--json"])[1])
        self.assertEqual([r["group"] for r in rep["groups"]], ["2026-10"])
        rep = json.loads(run(["report", "--journal", self.j, "--from", "2026-10-20", "--json"])[1])
        self.assertEqual(rep["total"]["trades"], 2)
        rep = json.loads(run(["report", "--journal", self.j, "--symbol", "tsla", "--json"])[1])
        self.assertEqual(rep["total"]["net_pnl"], -47.52)
        code, _, err = run(["report", "--journal", self.j, "--from", "10/20"])
        self.assertEqual(code, 1)

    def test_show(self):
        code, text, _ = run(["show", "--journal", self.j])
        self.assertEqual(code, 0)
        self.assertEqual(len(text.strip().splitlines()), 3)
        code, text, _ = run(["show", "--journal", self.j, "--id", "2026-10-19-TSLA-1"])
        self.assertIn("SELL 8 @ 358.27  stop, slippage $0.40", text)
        self.assertIn("lost $0.64 more than the plan's max loss of $46.88", text)
        code, text, _ = run(["show", "--journal", self.j, "--setup", "level", "--json"])
        self.assertEqual([t["symbol"] for t in json.loads(text)], ["AAPL", "SOFI"])


if __name__ == "__main__":
    unittest.main()
