---
name: trader-journal
description: >-
  Trade Journal. Records every trade you actually took and tells you which
  setups make you money. Reads the fills (Alpaca's own filled orders or
  account activities, or the fills trader-broker logged), pairs each buy
  with its sells into one trade, and attaches the plan the other skills
  wrote: the setup, the planned entry, stop and target, the max loss and the
  Time Machine's odds. For each trade it records every fill, the slippage
  against the plan (entry fill vs the signal, stop fill vs the stop), the
  P&L in dollars and in R, how long it was held, and how it ended: stop,
  target, flat by the close, or by hand. Then it reports win rate, net P&L,
  average R, profit factor and slippage by setup, symbol, exit or month,
  and checks the Time Machine's odds against what really happened. Writes
  trader/journal.json. Use when the user says "journal my trades", "log
  this trade", "how did I do today", "what's my P&L", "which setup works",
  "am I making money", "win rate", "how much is slippage costing me", "my
  stats", or at the end of every trading day, after /trader-broker.
argument-hint: "[sync | report | show | tag]"
---

# trader-journal

The Trade Journal is where a day trader finds out the truth. Every trade the
pack plans is a guess; the journal is the record of how the guess went. It
reads the fills, not the plans, so it counts what you really paid and what you
really got, and it keeps the plan next to them so you can see the gap. Most
people who day trade lose money. A journal does not change that by itself. It
shows you, setup by setup, whether you are one of them, so you can stop
trading the setups that lose.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/journal.py" sync                                  # fills from trader/state/broker.json
python3 "${CLAUDE_SKILL_DIR}/journal.py" sync --fills trader/alpaca-fills.json # Alpaca's own fills (preferred)
python3 "${CLAUDE_SKILL_DIR}/journal.py" show                                  # one line per trade
python3 "${CLAUDE_SKILL_DIR}/journal.py" show --id 2026-10-19-TSLA-1           # every fill, slippage, P&L
python3 "${CLAUDE_SKILL_DIR}/journal.py" tag --id 2026-10-20-AAPL-1 --setup level --stop 248.80
python3 "${CLAUDE_SKILL_DIR}/journal.py" report                                # by setup
python3 "${CLAUDE_SKILL_DIR}/journal.py" report --by exit --from 2026-10-01    # or symbol, month
```

Other flags: `--json` on show and report, `--date 2026-10-19` on sync (one
day only), `--fee-per-share 0.0` (or `"fee_per_share"` in trader/config.json),
`--setup` and `--symbol` filters on show and report.

## What it reads and writes

| file | what |
| --- | --- |
| `--fills FILE` | Alpaca's filled orders (`get_orders`, with nested legs) or account activities (`activity_type` FILL), saved as JSON. Repeatable. |
| `trader/state/broker.json` | the fills trader-broker logged (`ticket.py fill`, `result --filled-price`), and each entry's stop and max loss |
| `trader/plan.json` | the exit planner's plan: setup, entry, stop, target, flat-by |
| `trader/orders.json` | each order's plan, from the budget manager |
| `trader/timemachine.json` | the odds the Time Machine gave the plan |
| `trader/config.json` | `flat_by`, and `fee_per_share` if you pay fees |
| `trader/journal.json` | the journal it writes: one record per trade |

When `--fills` covers a day and a symbol, those fills are used and the broker
log's fills for that day and symbol are ignored, so nothing is counted twice.
Alpaca's fills are the better source: they carry the exact fill price and
time, and the order type tells a stop-loss fill from a take-profit fill.

## Steps: at the end of the day (or right after a trade closes)

1. Get the fills. With Alpaca's MCP server, call `get_orders` with status
   closed, nested true, for today (read only, Always allow is fine). Save the
   JSON it returns to `trader/alpaca-fills.json`. If the user is not on
   Alpaca, the fills trader-broker logged are enough.
2. `journal.py sync --fills trader/alpaca-fills.json`. Run it before the next
   trade's plan overwrites `trader/plan.json`: the plan is copied into the
   journal at sync time, and a later sync keeps it.
3. For any trade it prints as untagged (no plan on file, for example a trade
   placed outside the pack), ask the user what the setup was and where the
   stop was, then `journal.py tag --id ID --setup NAME --stop PRICE`. A trade
   with no stop has no R; its dollars still count.
4. `journal.py report`. Show the table, then say in one or two plain
   sentences which setup is paying and which is not. Quote the numbers; do
   not round a loss into a win.

## How it builds a trade

- A trade opens on a buy from flat and closes when the position is flat again,
  per symbol, per New York day. Partial fills and scaling in or out are one
  trade. A second round trip in the same stock the same day is trade 2.
- A sell with no buy before it that day (a position held overnight, or a
  short) is left out and reported, not guessed at.
- Ids look like `2026-10-19-TSLA-1` and stay the same on every sync, so a tag
  and a note survive.

## The numbers

| field | what |
| --- | --- |
| P&L | sells minus buys, minus fees. Net P&L is what the report sums. |
| R | net P&L divided by the planned risk (planned entry minus stop, times shares). -1R is a clean stop. |
| entry slippage | the average entry fill minus the planned entry, per share and in dollars. Positive is a cost. |
| exit slippage | for a stop exit, the stop minus the fill; for a target exit, the target minus the fill. Positive is a cost. A flat or manual exit has no planned price, so none. |
| exit | stop, target, flat (at or after flat-by), manual, or mixed |
| loss vs plan | how much a loss went past the plan's max loss: the gap and slippage the budget manager could not see |
| win rate, profit factor | wins over closed trades; dollars won over dollars lost |
| Time Machine check | the average odds it gave (target first, stop first) next to how often each really happened |

## Example: the reel's trade

```
2026-10-19-TSLA-1      orb          8 TSLA @ 364.21 -> 358.27  stop    -$47.52 (-1.01R)

  plan: entry 364.18, stop 358.32, target 370.04, risk 5.86 a share, flat by 15:55
  2026-10-19T10:15:02-04:00  BUY  8 @ 364.21
  2026-10-19T11:42:10-04:00  SELL 8 @ 358.27  stop, slippage $0.40
  entry slippage +$0.03 a share ($0.24)
  P&L -$47.52 gross, $0.00 fees, -$47.52 net, -1.01R
  lost $0.64 more than the plan's max loss of $46.88
```

(The fills here are an illustration in Alpaca's format, not the real outcome
of that session.) The plan said $46.88 at risk; slippage made it $47.52.
Across many trades, that gap is the number to watch.

## What it never does

- Never places, changes or cancels an order. It only reads.
- Never asks for, reads or stores broker keys.
- Never invents a fill, a stop or a setup. A missing plan stays untagged
  until the user says what it was.
- Never counts an open position's P&L in the totals.

## Safety

- Education and tooling, not financial advice. Past results, good or bad, do
  not predict the next trade.
- A small sample says little: under 20 trades, the report says so.
- The journal reads what the broker reports. Check anything that matters
  against your broker's statements, which are the record for taxes.
- Day-trading gains are short-term gains, and frequent trading makes wash
  sales likely. Nothing here is tax advice.
- Nothing in this pack places an order except trader-broker, and only after
  trader-risk passes it and the user says yes.
