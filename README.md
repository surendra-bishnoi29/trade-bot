# bot-trading: an autonomous AI trading desk

**Status:** phase 1, foundation. Slice 1 (event core, buses, event store) and slice 2 (a runnable desk: data agent, watcher, news agent, risk officer, execution, back office, head trader with a console) are implemented and tested against the Alpaca paper account. Slice 3 adds the autonomous swing cycle: scorecard, head-trader plan, skeptic challenge, theses, orders, a thesis monitor and an automatic kill switch. `uv run desk run` starts everything with the console and the schedule; `uv run desk cycle --dry-run` prints a plan without trading; `uv run desk cycle` trades it; `uv run desk ask "..."` asks one question; `uv run desk theses` lists theses; `uv run desk backfill` loads history. In the console, `/plan`, `/cycle`, `/status`, `/theses`, `/kill` and `/resume`. See Getting started below.

## Vision

An autonomous head-trader agent runs a team of specialist research agents. The team gathers data and researches every factor that moves a stock, the head trader forms a thesis and decides, an independent risk officer approves or vetoes, and an execution agent buys and sells automatically. The desk trades US equities in two books, swing and intraday, starting in fully autonomous paper trading and graduating to live trading on measured results.

## Documents

| Document | Purpose |
|---|---|
| [docs/00-decisions.md](docs/00-decisions.md) | Decisions made, assumptions to confirm, open questions |
| [docs/01-architecture.md](docs/01-architecture.md) | The desk: agents, event bus, head-trader loops, thesis, risk, data, LLM policy |
| [docs/02-factors.md](docs/02-factors.md) | Catalogue of factors that move stock prices, with horizon, frequency, source and owning agent |
| [docs/03-roadmap.md](docs/03-roadmap.md) | Phased plan with exit criteria for each phase |
| [docs/04-technical-design.md](docs/04-technical-design.md) | HLD and LLD: processing pipeline, bitemporal storage, bus mechanics, LLM context layers, cost model, hot-path latency |
| [docs/technical-design-sketch.html](docs/technical-design-sketch.html) | Whiteboard version of the technical design, published at https://claude.ai/artifact/Ma36bdRmT9ZSWRgRabiQ1K |
| [docs/trading-desk-sketch.html](docs/trading-desk-sketch.html) | Whiteboard-style visual overview of all of the above, published at https://claude.ai/artifact/RdXZPHWqwV7d49rn321LXJ |

## How we work

Design is discussed and agreed in these documents before any code is written. Each phase in the roadmap ends with a review.

## Getting started (phase 1)

Requirements on this machine: Python 3.12 via `uv`, PostgreSQL 14 with pgvector, Redis 8. Both services come from Homebrew; no Docker.

```bash
uv sync --extra dev                 # create .venv and install everything
cp .env.example .env                # then put your keys in .env (never in a chat)
createdb desk && createdb desk_test # once
uv run pytest -q                    # unit tests; Redis and PostgreSQL tests skip unless the URLs below are set
DESK_TEST_REDIS_URL=redis://localhost:6379/0 DESK_TEST_DATABASE_URL=postgresql://localhost:5432/desk_test uv run pytest -q
uv run python scripts/check_anthropic.py   # one tiny call to confirm the Anthropic key works
```

Layout: `src/desk` is the bot (core, bus, storage, data, agents, the `desk` command), `services/api` is the interface backend (`desk-api`), `web/` is the React interface, `deploy/` holds the systemd units, the Caddyfile and the deploy scripts, `docs/` the design.

## Running the desk on the server

The desk runs on a Linux VM as three systemd services: `desk-bot`, `desk-api` and Caddy (architecture section 19). From this machine:

```bash
deploy/make-env.sh            # once: stage the server's .env (this machine's keys plus server overrides)
deploy/deploy.sh              # sync the code, install, build the interface, restart bot and api
deploy/deploy.sh --no-bot     # same, but leave the running bot alone
ssh oracle sudo journalctl -u desk-bot -f        # the bot's log
ssh oracle sudo -u desk /opt/desk/app/.venv/bin/desk-api users add NAME --role operator   # interface accounts
```

The interface is at the server's address; operators can run cycles and use the kill switch, viewers can watch and ask the head trader. Telegram notifications need `DESK_TELEGRAM_BOT_TOKEN` and `DESK_TELEGRAM_CHAT_ID` in the server's `.env` (`scripts/telegram_chat_id.py` finds the chat id).

## The risk officer's limits

Every order passes the deterministic risk officer; every rule and its numbers are on the trade card. Defaults, all overridable in `.env`:

| Setting | Default | Meaning |
|---|---|---|
| `DESK_POSITION_PCT` | 0.02 | budget per new swing position, of equity |
| `DESK_MAX_OPEN_POSITIONS` | 10 | open names in the swing book |
| `DESK_MAX_GROSS_PCT` | 1.0 | account gross exposure, of equity |
| `DESK_MAX_SECTOR_PCT` | 0.30 | one sector's gross exposure, of equity |
| `DESK_MAX_DAILY_LOSS_PCT` | 0.02 | loss on the day (against the previous close's equity) after which only exits are allowed |
| `DESK_DRAWDOWN_DERISK_PCT` | 0.03 | from the equity peak: new positions at half size |
| `DESK_DRAWDOWN_HALT_PCT` | 0.05 | no new positions |
| `DESK_DRAWDOWN_KILL_PCT` | 0.08 | the kill switch engages by itself; `/resume` releases it |
| `DESK_INTRADAY_MAX_GROSS_PCT` | 0.5 | the intraday book's gross exposure |
| `DESK_INTRADAY_ALLOW_SHORT` | true | shorts in the intraday book only |
| `DESK_INTRADAY_MAX_SHORT_PCT` | 0.25 | the intraday book's gross short exposure |

The health strip shows the drawdown, the day's P&L and the posture (full size, half size, no new risk).

## Replaying a past session

```bash
uv run desk replay --day 2026-11-03                 # the same agents, a simulated broker, a scripted head trader
uv run desk replay --day 2026-11-03 --llm           # the real head trader; model outputs are cached, a rerun is free
uv run desk replay --day 2026-11-03 --feed-gap 15:00-15:15   # withhold bars: the kill switch must engage
```

The replayer feeds the day's minute bars to the agents under a driven clock, the simulated broker fills at the next bar with slippage and commission always on, theses live in memory, and the journal is read as of the simulated time and never written. The report lists cycles, fills, theses, costs and drawdown.

## Training the forecaster (phase 2)

```bash
uv run desk backfill --daily-sessions 1300   # five years of daily bars: a model needs more than one
uv run desk features audit                 # recompute, correlation and shuffle checks: no feature may know the future
uv run desk train --horizon 5              # features -> audit -> walk-forward -> after-cost backtest -> registry
uv run desk models                         # every trained model, its out-of-sample record and who is champion
```

`desk train` refuses a frame that fails the lookahead audit, and a model becomes champion only through the promotion gate (300 or more out-of-sample predictions, positive rank correlation, hit rate at least 50%, no regression against the incumbent). With a champion in the registry the forecaster agent publishes a `forecast` report per name after every quant screen, with the model's own record in the text, and the scorecard gives the expected return a third of its weight. Without one the desk runs exactly as before. Model files live under `data/models` (git-ignored); retrain weekly after the close on Friday.

## The funnel (the main screen)

One picture of every instrument the desk watches, read left to right: the day's fan (one line per instrument from the prior close), then five columns that narrow it (Moving, Confirmed, Shortlist, Cleared, Picks), with lines joining each instrument through the columns it survives. Hover a card and that instrument lights up everywhere; click to pin its detail: candles, the book it belongs to and why, the sentiment evidence with the agent that measured each reading, the desk's decision trail.

- **Engine:** `desk.funnel.build` (deterministic, no model calls) turns bars, watcher alerts, news tone and the desk's own journal into a score from −100 to +100, five agent checks, a setup tag, a book label with reasons, and levels. `desk.funnel.markets` defines the markets: US stocks (traded), oil and gold through their funds, and crypto (both watch-only, kept fresh by the `market_feeds` agent; `DESK_WATCH_MARKETS=false` turns that off).
- **Agents read the same thing:** every plan's material carries the funnel as text, and the head trader has a `get_funnel` tool for questions.
- **API:** `/api/funnel?market=stocks&at=<time>`, `/api/funnel/detail`, `/api/funnel/markets`. `at` replays any moment of a stored session; nothing after that moment is read.
- **Components** (`web/src/funnel/`): `FanChart`, `BreadthStrip`, `StageCard`, `PickCard`, `MiniChart`, `LevelBar`, `FocusPanel`, `SentimentChip`, composed by `FunnelBoard`. A new market is one entry in `markets()`; the board needs no change.
- **Local development:** `createdb desk_dev`, run `DESK_DATABASE_URL=postgresql://localhost:5432/desk_dev uv run desk-api serve --port 8090` and `DESK_API=http://127.0.0.1:8090 npx vite` in `web/`.

## The lab desk (branch `predictor`)

A second desk on a second paper account that trades the user's rule instead of the hourly cycle: one eye per instrument, every minute, on stocks, oil, gold and crypto; a prediction on every trigger; lines on every position. See decision D17.

- **Sentinel** (`desk.lab.sentinel`): rebuilds the funnel for each open market every minute and raises `sentinel.trigger` when an instrument's side flips, its strength surges, unusual activity or a change of news tone appears, the market turns, or a strong name has not been reviewed for `DESK_SENTINEL_REVIEW_MINUTES`. No model calls; a quiet period of ten minutes per instrument.
- **Predictor** (`desk.lab.predictor`, prompts `prompts/predictor/`): answers a trigger with a structured call (long, short or none, and always the side it leans to; confidence; horizon; maximum loss, first profit and step in percent; reason) checked by a skeptic. Acts at or above `DESK_PREDICTOR_MIN_CONFIDENCE` (0.55: its own odds of reaching the first profit line before the maximum loss, 0.5 being a coin toss), unless the skeptic says blocking; a skeptic's concern is recorded and raises the bar by `DESK_PREDICTOR_CONCERN_MARGIN` (0). Under `DESK_PREDICTOR_MAX_CALLS_PER_HOUR` (120, a third kept for triggers that carry news), with the first profit line at least `DESK_LAB_MIN_FIRST_PROFIT_PCT`, three round-trip costs, and never nearer than the maximum loss. A pass is graded on paper by its lean. Sizes `DESK_LAB_POSITION_PCT` of equity (fractional for crypto). Reads its own record on the instrument first; every call is journaled as `prediction.call`, acted on or not.
- **Lines** (`desk.lab.lines`): on the fill, a base at the price and a max-loss line below it (above for a short). The price is measured in steps of `step_pct` from the base (the predictor's, held between `DESK_LAB_MIN_STEP_PCT` 0.15% and `DESK_LAB_MAX_STEP_PCT` 0.3% for stocks and funds, five times that for crypto): one step in the trade's favour moves the max-loss line up to the base (the trade can no longer lose); every further step raises the min-profit line one step, so it always sits one step below the best price seen. The position closes when the price comes back to the min-profit line (or to the base before the line has risen), at the max-loss line, at the horizon, or on the broker's own stop. Before its own exit the engine cancels the broker's stop and waits for the broker to confirm it is gone (following a replaced stop to its replacement); an exit is never sent while the stop may still be alive, since both filling would turn the position round. A position the broker shows with no line on it for three minutes is flattened (`call_id` `flatten`), once, and again after a quarter of an hour if it is still there (an OTO stop leg rests at Alpaca for stocks and ETFs and is replaced as the lines move; crypto has no brackets, so the engine's bar watch is the stop). Each exit scores the call (`prediction.score`) with the return after costs; the funnel shows the record per instrument, the open lines and the latest call.
- **The Predictor screen** (`web/src/lab/`, shown in the rail on the lab desk): the four markets (US equities, oil and energy, gold and metals, crypto) with today's counts; every agent in the order work flows through them with what each did in the last hour; every call, newest first; and for the selected call the whole path: why the sentinel looked, what the predictor was shown (each reading with the agent that measured it, the market, the levels, the headlines, its own record), how it reasoned (for, against, what settled it, what would prove it wrong), its lines drawn to scale, the skeptic's case, the bar, the risk officer, the fill, what the lines did, and the result. Every right/wrong count (the scoreboard, the market tiles) lists the calls behind it on hover, with date, time, side and result, and a click filters the call list to them. A day picker at the top shows any earlier day on the same screen, from its graded report. API: `/api/lab/live`.
- **Lines, minute by minute** (its own page, `/lines`, in the rail; `web/src/lab/LinesPage.tsx`, `TradeChart.tsx`; `/api/lab/trades?days=7` lists the positions, `/api/lab/trade/{call_id}` gives one with its minute bars and every move of its lines): one candlestick chart per position with the base, the max-loss line and the min-profit line drawn as they stood at every minute, markers where the max-loss line moved up to the base and where the min-profit line was raised, and a time runner (drag, play, click the chart or click a move) that reads the price and all three lines at any minute: the lines are drawn solid up to the runner and faint beyond it, so they take their new values as time plays. A crosshair under the mouse shows the time and the price at the cursor and that minute's bar and lines. An open position refreshes every ten seconds. The screens live on URLs (`/`, `/predictor`, `/lines`, `/desk`); Caddy and the api both serve `index.html` for them.
- **The right/wrong report** (`desk.lab.report`, `/api/lab/report?day=YYYY-MM-DD`, the screen's second tab): one UTC day, graded. A traded call is right or wrong by what the position returned after costs. A call that named a direction but was not traded is graded on paper: the same lines are run over the real minute bars that followed. A pass has no direction to grade; the report counts the passes that were followed within the hour by a move large enough to trade. Totals by market, by direction, by exit reason and by confidence band, and the findings in sentences. The stock, oil and gold numbers are final at the New York close (21:00 UTC); crypto at midnight UTC.
- **Markets:** `DESK_LAB_MARKETS=stocks,commodities,crypto`. Commodities through their funds, shorts allowed, `DESK_COMMODITIES_MAX_GROSS_PCT`; crypto long only, always open, `DESK_CRYPTO_MAX_GROSS_PCT`. The risk officer applies every portfolio rule to all three.
- **Local run without a second account:** `DESK_BROKER=sim` replaces Alpaca trading with the in-process simulated broker (fills at the next live minute bar, 0.25% a side on crypto, shorts allowed); data still comes from Alpaca, and the journal, the funnel and the scores look the same. Crypto trades on weekends, so the lab can be watched on a Saturday.
- **On the VM:** put the second paper account's two keys in `.env.lab` (git-ignored, so you create it yourself; two lines, `ALPACA_API_KEY=` and `ALPACA_SECRET_KEY=`, the same names as in `.env`), run `bash deploy/lab-env.sh` (it builds the lab's environment in three layers, `.env`, then `.env.vm`, then `.env.lab`, so the Claude key comes from the main desk's files unless `.env.lab` names its own; the main desk's broker keys and Telegram never carry over; it refuses blank keys and the main desk's keys), then `scp deploy/lab-install.sh oracle:/tmp/ && ssh oracle "sudo bash /tmp/lab-install.sh"`. The lab is an independent instance: `/opt/desk/lab` on branch `predictor`, database `desk_lab`, redis db 1, api on 8081, interface on port 8090 (not opened to the internet: `ssh -N -L 8090:localhost:8090 oracle`, then http://localhost:8090), units `desk-lab-bot` and `desk-lab-api`. The lab's database starts with no interface login; the installer prints the command to create one. Later updates: `sudo bash /opt/desk/lab/deploy/lab-install.sh` on the VM. To change the lab's keys or settings later: edit `.env.lab`, run `bash deploy/lab-env.sh`, then that same update command; a freshly staged file replaces the lab's `.env`. Live stock bars: Alpaca's free plan allows one stream per login, so the lab polls stock bars over REST every 20 seconds by default and leaves the stream to the main desk; a lab account on a different Alpaca login has its own stream, switched on with `DESK_LIVE_STREAM=true` in `.env.lab`. Nothing it does can touch the main desk's book.
- **What decides:** two weeks on paper. The lab graduates when its calls hit above 55% after costs with at least 60 scored calls; below that the prediction, not the lines, is what has to change.

## HTTPS

The interface runs on plain HTTP at the VM's address until a domain exists. When one points at the VM:

```bash
sudo bash /opt/desk/app/deploy/enable-https.sh desk.example.com
```

Caddy obtains and renews the certificate by itself and redirects HTTP to HTTPS; later deploys keep the domain. Port 443 is open on the host firewall and must be open in the Oracle security list too (same place as port 80).

## Operations (phase 5)

On the server, systemd timers do the routine work and report to Telegram:

| Timer | When | What |
|---|---|---|
| `desk-healthcheck` | every 5 minutes | services, Redis, disk, the last heartbeat, the price feed during the session; one message per problem per hour, one when it recovers |
| `desk-backup` | 22:30 UTC daily | `pg_dump` of the journal, 14 days kept |
| `desk-drill` | first Sunday monthly | restores the newest backup into a scratch database, counts what came back, drops it |
| `desk-train` | Saturday 02:00 UTC | retrains the forecaster through the promotion gate |

```bash
uv run desk weekly        # the weekly review (performance, post-mortems, analyst scores, incidents, models and prompts)
uv run desk graduation    # the checklist for moving from paper to live, each criterion with its number
```

The reviewer writes the weekly review every Friday after the close; it and the graduation checklist are in the Reviews tab. `bash deploy/healthcheck.sh` and `bash deploy/restore-drill.sh` run by hand too.

## The intraday book (phase 4)

Off by default: on the last 59 sessions none of the three baselines beats costs (`desk intraday-eval`), so the book waits for a strategy that does. When one passes, switch it on in `.env`:

```
DESK_INTRADAY_ENABLED=true
DESK_INTRADAY_STRATEGY=vwap_reversion     # or opening_range, momentum
DESK_INTRADAY_POSITION_PCT=0.01           # of equity per name
DESK_INTRADAY_WATCHLIST_SIZE=10
DESK_INTRADAY_MAX_POSITIONS=5
```

Forty-five minutes before the open the book publishes its playbook (the names that moved, traded and made news yesterday) as a `playbook` report. During the session it runs the chosen strategy on live minute bars for those names, one position per name per day, every order through the risk officer under the intraday book's limits (gross 50%, shorts allowed up to 25%, nothing queued for the next open), no entries in the last half hour, everything flat ten minutes before the close. Each round trip is an `intraday.trade` event with its P&L and reason, on Telegram and in the activity feed. After a restart during the session the book recovers its positions from the day's fills.

## The performance reviewer (phase 4)

```bash
uv run desk review                   # post-mortems of the theses closed in the last 30 days, and the analyst scoreboard
uv run desk compare --days 5         # replay the last five sessions with the rules-only baseline (costs on, SPY as the tape)
uv run desk compare --days 5 --llm   # the same sessions with the real head trader and skeptic (model outputs cached)
```

After the post-close cycle the reviewer grades every thesis closed since the last review (realized against expected, how it exited, the best and worst point during the hold) and asks the model for one lesson per trade in a single call (`DESK_REVIEW_NARRATIVE=false` turns that off). Every Friday it scores each analyst's calls against what prices did over the next five sessions and publishes credibility weights; the head trader reads them in every plan. Both appear in the Reviews tab and on Telegram.

## Costs and the intraday baselines (phase 2)

```bash
uv run desk costs                                  # slippage of every fill against its arrival price; the cost model it supports
uv run desk backfill --minute-sessions 60          # three months of minute bars for the universe
uv run desk intraday-eval --sessions 60            # opening range, VWAP reversion, momentum, after costs, vs holding from the open
```

The cost model assumes 5 bps a side for the core names (10 bps round trip; the training gate uses twice that) until 20 fills say otherwise. The intraday baselines are the yardstick the intraday book must beat: one trade per name and session, no lookahead, flat by the close.

## Running the interface locally

```bash
uv run desk-api users add me --role operator   # once, against the local database
uv run desk-api serve                          # api on 127.0.0.1:8080
cd web && npm install && npm run dev           # interface on http://localhost:5173, proxied to the api
```
