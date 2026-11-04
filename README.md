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

Layout, as in the architecture document: `src/desk/core` (clock, event envelope, payload schemas, settings), `src/desk/bus` (in-memory and Redis Streams buses), `src/desk/storage` (schema, event store, persister), `src/desk/agents` (the base agent), `tests`, `scripts`.
