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
