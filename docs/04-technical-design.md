# Technical design: processing, time-bounded storage, LLM context and cost

Status: draft for review, 2026-11-02, memory section added 2026-11-03. Companion to [01-architecture.md](01-architecture.md). HLD is the high-level design, LLD the low-level design. Whiteboard version: `docs/technical-design-sketch.html`.

## 1. HLD: the processing pipeline

Every piece of information, whether a price bar, a filing or a headline, passes through the same seven stages. Nothing skips a stage, which is what makes replay and audit possible.

| Stage | What happens | Owner | Output |
|---|---|---|---|
| 1 Ingest | A collector polls or streams a source, normalizes to our schema, validates, deduplicates, and stamps two times: `occurred_at` (the source's own time) and `ingested_at` (the desk's clock) | collectors | one event per fact on the bus |
| 2 Persist | A persister consumer writes every event to the append-only `events` table; inserts are idempotent on `event_id` and on natural keys | persister | durable, replayable history |
| 3 Derive | The Feature Store turns bars and reference data into numeric features, point in time. The text pipeline turns articles and filings into structured, scored events | feature store, news pipeline | `features.*.ready`, `news.article.scored`, `news.state`, `filing.analyzed` |
| 4 Research | Quant models forecast from features. LLM analysts read compact evidence packs and emit structured reports | quant, analysts | `forecast.*`, `research.report` |
| 5 Decide | The head trader synthesizes reports, forecasts, theses and the portfolio into order intents. The intraday strategy decides from features and the playbook | head trader, strategies | `thesis.*`, `order.intent` |
| 6 Control and execute | Risk officer checks, execution sends, back office reconciles | risk, execution, back office | `risk.*`, `order.*`, `portfolio.*` |
| 7 Learn | The reviewer grades outcomes, scores analysts, checks calibration and retrains | reviewer | `review.*`, credibility weights, model promotions |

Two paths with different latency budgets share the pipeline:

- **Hot path** (intraday): bar, features, strategy, risk, execution. In memory, no LLM call, under one second.
- **Cold path** (swing and research): minutes are fine, LLM calls allowed, anything not needed within the hour goes to the Batch API overnight.

## 2. LLD: time-bounded storage

### 2.1 The bitemporal rule

Every stored fact carries two times. **Valid time** is when the fact was true in the world: `occurred_at` for events, `as_of` for features, `period_end` for a financial statement. **Knowledge time** is when the desk could have known it: `ingested_at` for live data, `filed_at` for filings, `released_at` for macro releases, `snapshot_at` for consensus estimates. Every read that feeds a decision or a backtest is "as of knowledge time T". That single rule removes lookahead bias from the whole system, because a restatement, a revised earnings date or a late-arriving article simply has a later knowledge time and is invisible at T.

For backfilled history (articles or filings we fetch months after they were published), knowledge time is not our backfill time. It is the source's publication time plus a modelled ingestion delay (two minutes for a news API, same day for a filing), so backtests see history the way the live desk would have seen it.

### 2.2 Tables

PostgreSQL with TimescaleDB. Hypertables are partitioned by time; the partition column is listed.

| Table | Key columns | Hypertable on | Notes |
|---|---|---|---|
| `events` | event_id (ULID), type, schema_version, occurred_at, ingested_at, symbol, book, correlation_id, causation_id, source, payload jsonb | occurred_at | Append-only audit trail. Indexes on (symbol, occurred_at), (type, occurred_at), (correlation_id). Never updated or deleted; corrections are new events |
| `bars` | symbol, interval, ts (bar start), open, high, low, close, volume, vwap, trade_count, source, ingested_at | ts | Primary key (symbol, interval, ts). Continuous aggregates build 5m, 15m, 1h and 1d from 1m. Only completed bars are stored |
| `features` | symbol, as_of (bar close), feature_set, version, values jsonb | as_of | Computed only from inputs whose knowledge time is at or before as_of. Versioned so a feature change never silently rewrites history |
| `fundamentals_facts` | cik, concept, unit, period_start, period_end, value, form, accession, filed_at | filed_at | From EDGAR XBRL company facts. First-reported values are kept; a restatement is a new row with its own filed_at |
| `estimates_snapshots` | symbol, snapshot_at, fiscal_period, eps_mean, revenue_mean, n_analysts, rating_mean, target_mean | snapshot_at | Daily snapshot of free-tier consensus. Revision features are deltas between snapshots |
| `calendar_events` | symbol, kind, scheduled_at, known_at, details jsonb | known_at | Earnings dates as they were known at each point in time; a date change is a new row |
| `macro_series` | series_id, period, value, released_at, vintage | released_at | ALFRED vintages from FRED, so a revised CPI print does not leak backwards |
| `news_articles` | article_id, published_at, ingested_at, knowledge_at, source, url, title, body_ref, symbols[], content_hash, embedding vector | published_at | Body text on disk, later S3. Embedding for deduplication and retrieval through pgvector |
| `news_scores` | article_id, model, prompt_version, relevance, polarity, confidence, event_type, impact, horizon, scored_at | scored_at | One row per model and prompt version. Re-scoring adds rows, never overwrites |
| `news_state` | symbol, as_of, score_1h, score_1d, count_1d, max_impact_1d, last_high_impact_at | as_of | Rolling aggregates the strategies and evidence packs read instead of raw articles |
| `forecasts` | model_id, trained_through, symbol, as_of, horizon, expected_return, q05, q25, q50, q75, q95, p_up, attributions jsonb | as_of | |
| `research_reports` | report_id, analyst, symbol, as_of, horizon, stance, conviction, summary, evidence_ids[], risks, invalidation, numbers jsonb, llm_call_id | as_of | |
| `theses`, `thesis_updates` | thesis_id, symbol, book, direction, status, created_at, closed_at, narrative, evidence_ids[], expected_return, horizon_days, target, invalidation jsonb, size_rationale; updates: at, change, decision, evidence_ids[] | created_at | The trader's memory |
| `order_intents`, `risk_decisions`, `orders`, `fills`, `positions`, `pnl_daily` | ids, symbol, book, quantities, prices, rule results jsonb, timestamps | ts | Every risk rule result is stored with its numbers |
| `llm_calls` | call_id, agent, model, prompt_version, prompt_hash, input_tokens, cache_read_tokens, cache_write_tokens, output_tokens, cost_usd, latency_ms, stop_reason, output_ref | created_at | Every call and its cost |
| `llm_cache` | prompt_hash, model, prompt_version, output jsonb, created_at | | Identical prompts, including every replay, are served from here at no cost |
| `analyst_scores`, `credibility_weights` | analyst, regime, horizon, window_end, brier, hit_rate, ic, n; weight, as_of | window_end | Read into the daily prompt layer |

### 2.3 Tiers and retention

| Tier | Holds | Window | Why |
|---|---|---|---|
| Redis Streams | live events, one stream per topic, MAXLEN trimmed | about 24 hours | fan-out, consumer groups, hot replay |
| PostgreSQL uncompressed | everything above | last 30 days | fast operational queries |
| TimescaleDB compressed chunks | the same rows, roughly ten times smaller | forever | the audit trail and backtest source |
| Parquet lake, DuckDB | nightly export partitioned by date and symbol | forever | model training and research never touch the operational database |

### 2.4 Time conventions

- UTC in storage; US Eastern only when reading the exchange calendar or rendering for people.
- A bar's `ts` is its start. A bar exists only when it is complete, meaning `ts + interval` is at or before the clock. Features are stamped `as_of` = bar close.
- Session boundaries, holidays and early closes come from `exchange_calendars`.
- One injected `Clock` object: `LiveClock` in production, `SimClock` in replay. No agent ever calls the system clock directly.

### 2.5 Point-in-time reads

Every read helper takes T from the Clock. Two examples:

```sql
-- the latest feature row known at T
SELECT DISTINCT ON (symbol) *
FROM features
WHERE symbol = $1 AND feature_set = $2 AND as_of <= $T
ORDER BY symbol, as_of DESC;

-- the last eight quarters of revenue as they were known at T
SELECT DISTINCT ON (period_end) period_end, value, filed_at
FROM fundamentals_facts
WHERE cik = $1 AND concept = 'Revenues' AND filed_at <= $T
ORDER BY period_end DESC, filed_at DESC
LIMIT 8;
```

### 2.6 Idempotency and delivery

ULID event ids; natural unique keys such as (symbol, interval, ts) with `ON CONFLICT DO NOTHING`; consumer groups with explicit acknowledgement; pending entries reclaimed with `XAUTOCLAIM` after a crash; a dead-letter stream after three failed deliveries; every handler is idempotent because replay can redeliver anything.

## 3. LLD: the bus and the replayer

- One Redis stream per topic, named `desk:<topic>`. Producers `XADD` with a MAXLEN. Each agent is a consumer group and reads with `XREADGROUP`, so several instances of one agent share the work and every agent sees every event.
- The persister is one more consumer group. It writes each event to PostgreSQL; the `events` table, not Redis, is the system of record.
- The Replayer reads `events` ordered by `occurred_at`, publishes them onto the same streams, and advances a `SimClock`. Agents run unchanged. The execution adapter is swapped for the simulated broker. LLM calls are served from `llm_cache`, so a replay of a month costs nothing in tokens unless a prompt changed.

## 4. LLD: how the LLM gets its context

### 4.1 Principles

1. **Never raw data.** An LLM call receives an evidence pack: a compact, deterministic summary with the numbers already computed and every item cited by id. No bars, no full articles, no tables of features.
2. **Stateless calls.** There is no conversation history. State lives in the database. Each call gets exactly the slice it needs and nothing else, so context never grows over time.
3. **Three prompt layers with cache breakpoints** (section 4.2), ordered from most stable to most volatile, so the expensive part of every prompt is read from cache.
4. **Structured outputs.** Every response is a pydantic schema through `client.messages.parse`. Outputs are short, bounded with `max_tokens`, and free text is allowed only in a summary or narrative field.
5. **Model tiering with escalation.** Haiku 5.5 does the high-volume classification. Opus 5.5 does synthesis and decisions. Haiku escalates to Opus when its confidence is low or the estimated impact is high.
6. **Batch API** for anything not needed within the hour: backfills, transcripts, post-mortems, overnight news. Half price.
7. **Output cache** keyed by model, prompt version and prompt hash. Identical prompts, including replays and duplicate news, cost nothing.
8. **Effort per route.** Low for classification, medium for reports, high for head-trader decisions.

### 4.2 The three layers

| Layer | Content | Size | Changes | Caching |
|---|---|---|---|---|
| 1 Static | role and rules, output schema, factor glossary, house style | 2 to 4K tokens | only with a prompt version | cached; breakpoint after it |
| 2 Daily | regime brief, macro calendar, credibility weights, risk budgets, universe | 1 to 2K | once per day | cached across every call that day; breakpoint after it |
| 3 Volatile | the evidence pack for this symbol and moment | 1 to 3K | every call | not cached |

Prompt caching matches on the exact prefix, so nothing volatile is allowed in layers 1 and 2: no timestamps, no per-call ids, and JSON is serialized with sorted keys.

### 4.3 An evidence pack (fundamental analyst, swing, one symbol)

```
SYMBOL AAPL  AS_OF 2026-11-02T21:05Z  HORIZON weeks  BOOK swing
POSITION none   OPEN_THESIS none
PRICE 268.40  RET 1d +0.8% 5d +2.1% 21d -3.4% 63d +9.0%   VOL21 22% (pct 41)   RVOL 1.1
VALUATION  pe_fwd 29.1 (sector median 24.0, own 5y z +0.6)  ev_ebitda 21.8  fcf_yield 3.4%
QUALITY    roic 41%  net_debt_ebitda 0.3  accruals -1.2%  piotroski 7
GROWTH     rev_yoy +6.2%  eps_yoy +9.1%  3y_cagr rev +5.0%
ESTIMATES  fy1_eps 8.12 (rev 1m +0.9%, breadth +7/-2)  rating 4.1/5  target 292 (+8.8%)
EARNINGS   next 2027-01-28 (known 2026-10-30)  last surprise eps +3.1% rev +1.0%  reaction +2.4%
OWNERSHIP  inst 61% (q/q +0.4)  short 0.8% float  insiders 90d: 2 sells (routine, 10b5-1)
EVENTS (ids)  ev_01J9X... 8-K 2.02 results 2026-10-30
NEWS_STATE 1d score +0.21 count 14 max_impact medium   top: ev_01J9Y... "services growth" (0.6, +)
ANALYST_SCORES self: brier 0.21 hit 0.56 (regime: low-vol uptrend)
```

About 600 tokens. The analyst returns a `ResearchReport` of about 400 tokens that cites `ev_...` ids rather than restating the facts.

### 4.4 Call anatomy per route

Volumes assume the phase 3 to 5 scale: a core of 30 swing names, a daily intraday watchlist of 20, free-tier news volume.

| Route | Model | Effort | Input, cached | Input, new | Output | Calls per day |
|---|---|---|---|---|---|---|
| News scoring | Haiku 5.5 | low | 1.5K | 0.8K | 150 | about 600 |
| News escalation | Opus 5.5 | medium | 3K | 1.5K | 300 | about 30 |
| Analyst report | Opus 5.5 | medium | 5K | 2K | 500 | about 100 |
| Head trader decision | Opus 5.5 | high | 8K | 4K | 800 plus thinking | about 55 |
| Skeptic challenge | Opus 5.5 | high | 8K | 3K | 600 plus thinking | about 35 |
| Analyst deep dive | Opus 5.5 | medium | 5K | 2K plus up to 3K of tool results | 600 | about 10 |
| Intraday playbook | Opus 5.5 | high | 8K | 6K | 1.5K | 1 |
| Post-mortem | Opus 5.5, batch | medium | 5K | 2K | 400 | about 10 |
| Filing or transcript | Opus 5.5, batch | medium | 3K | 15K | 1K | about 2 |
| Memory consolidation | Haiku 5.5, batch | low | 2K | 4K | 800 | about 40 |
| Knowledge page rebuild | Opus 5.5, batch | medium | 3K | 4K | 800 | about 15 |

### 4.5 Why context stays small over time

- Old news becomes numbers in `news_state`; the pack carries at most the five highest-impact recent items by id.
- A thesis carries its last three updates verbatim and a one-line summary of older ones.
- The journal is a database, never a prompt. Retrieval through pgvector pulls at most a handful of relevant past post-mortems when the head trader asks for precedent.
- Each pack section has a hard token cap; truncation keeps the most recent and highest-impact items.
- Token counts are measured with `count_tokens` in tests, and every call's usage is logged.

### 4.6 Deep-dive mode

A deep dive is the standard call turned into a short agent loop. The analyst gets its tools, each a read-only function over the point-in-time read helpers of section 2.5, taking T from the Clock. The loop runs through the SDK tool runner with a hard cap of five tool calls, each result capped at about 800 tokens, and ends with the same structured report. Tool results are cached by tool, arguments and T, so a replayed deep dive makes no new reads and no new LLM calls. The head trader requests a deep dive for a name when conviction is high or the reports disagree, and the request carries the question to answer, so the loop has a goal rather than a licence to browse.

## 5. Cost model

Assumptions, to be re-checked at signup: Anthropic list prices as of late 2026. Opus 5.5 at $4 per million input tokens, $20 per million output, cache reads $0.20 per million. Haiku 5.5 at $0.10 and $0.50, cache reads assumed at ten percent of input. Cache writes assumed at 1.25 times input. Batch API half price. Thinking tokens bill as output.

| Route | Engineered design, per day | Naive design, per day |
|---|---|---|
| News scoring | $0.10 (Haiku, cached prefix) | $7.20 (Opus, whole articles) |
| News escalation | $0.40 | not separated |
| Analyst reports | $1.90 (evidence packs, cached layers) | $10.00 (raw data in prompts) |
| Head trader decisions | $4.20 (8K cached, 4K new, thinking) | $15.00 (40K-token prompts, prose) |
| Skeptic challenges | $1.90 (same cached layers) | not present |
| Analyst deep dives | $0.80 (bounded tool budget) | not present |
| Playbook | $0.10 | $0.50 |
| Post-mortems | $0.15 (batch) | $1.00 |
| Filings and transcripts | $0.10 (batch) | $0.60 |
| Agent memory (extraction, consolidation, pages) | $1.50 (batch, local retrieval) | not present |
| **Total** | **about $12 per day, $250 to $350 per month** | **about $34 per day, $750 to $1,000 per month** |

Other costs: market data $0 until phase 5, then Alpaca Algo Trader Plus at about $99 per month; one small cloud VM at $20 to $40 per month; replays about $0 thanks to the output cache. The head trader is the dominant line, so its call budget is the first alert we set.

The levers, in the order they pay off: evidence packs and structured outputs; prompt caching with stable prefixes; Haiku for bulk work; Batch API overnight; the output cache; effort tuning per route; per-route daily call budgets with alerts.

## 6. Hot path latency budget

| Step | Where | Budget |
|---|---|---|
| Bar arrives on the Alpaca WebSocket, normalized, published | collector | 5 ms |
| Feature update from in-memory ring buffers, incremental indicators | feature store | 10 ms |
| Setup evaluation against the playbook | intraday strategy | 5 ms |
| Risk rules from in-memory limits and cached positions | risk officer | 10 ms |
| Order submission | execution, alpaca-py | 100 to 300 ms |
| **Bar close to order sent** | | **about 150 to 350 ms, budget 1 s** |

News state updates arrive asynchronously; the strategy reads the latest value on the next bar.

## 7. Agent memory: what we take from Hindsight, and what we do differently

Decision of 2026-11-03: the desk's memory is built natively on our PostgreSQL, borrowing Hindsight's architecture where it fits and keeping the two clocks everywhere. We do not run Hindsight as a service: its recall cannot be restricted to what was known at a point in time, its extraction is not deterministic, and our memory volume (hundreds of items a day, not millions) does not need its scale machinery. A thin `Memory` interface keeps that decision reversible.

### 7.1 Borrowed and added

| Idea | From Hindsight | What we add |
|---|---|---|
| Typed memories: world facts and experiences | yes | Structured facts never become memories. They stay in the bitemporal tables and reach the LLM through evidence packs |
| Occurrence time and mention time on every memory | yes | The same two clocks as everything else, `occurred_at` and `known_at`, and every read is as of `known_at` |
| Entities and links between memories | yes | Entities are our symbols, companies, people, sectors and macro series. Causal links come free from `causation_id` on events |
| Observations: consolidated beliefs with evidence, proof count, refined rather than overwritten | yes | Every refinement is a new version with its own `known_at`, so a replay reads the belief the desk actually held at the time |
| Knowledge pages: standing answers rebuilt only when their scope changed, read as a row | yes | Pages carry `built_from_known_at`; prompt layer 2 reads them at boot |
| Four-way retrieval, reciprocal rank fusion, token-budget packing | yes | The as-of filter runs before ranking, never after |
| Scoped consolidation, idempotent document ids, local embedding and reranker models | yes | Consolidation runs on the Batch API overnight |
| Disposition for reflection, such as skepticism | yes | Our skeptic is a separate agent with its own prompt |
| Trust levels on memories | no | Human directive, observed outcome, analyst inference, in that order of weight |

### 7.2 Memory types

| Type | What it holds | Written by | Example |
|---|---|---|---|
| World memory | A durable qualitative fact about a company, sector, market or source | analysts, head trader, humans | "AAPL guides conservatively; the stock trades on services growth more than on hardware units" |
| Experience memory | What the desk did and what happened | reviewer, head trader | "Faded a news-driven gap in a low-float name in a high-VIX regime; stopped out in 40 minutes; third such loss" |
| Observation | A belief consolidated from several memories, with evidence ids, a proof count and a version chain | consolidation job | "Fading news-driven gaps in low-float names loses in high-VIX regimes, 5 of 6 cases since September" |
| Knowledge page | A standing answer to a fixed question, rebuilt when its scope changes | page builder | "What do we know about trading AAPL around earnings?" |
| Directive | A rule a human gave the desk. Highest trust, never consolidated away | humans | "Never short a name with more than 20 percent of its float sold short" |

Structured facts (bars, fundamentals, estimates, calendar, macro) are not memories. They are ground truth in the tables of section 2.

### 7.3 Tables

| Table | Key columns | Notes |
|---|---|---|
| `memories` | memory_id, bank, type (world, experience, directive), text, symbol, occurred_at, known_at, trust, source_event_ids[], document_id, tags[], embedding vector, tsv tsvector | One bank per learning agent plus a shared desk bank. `document_id` makes re-writes idempotent |
| `memory_entities` | memory_id, entity_id | The entities table reuses symbols, companies, people, sectors and macro series |
| `memory_links` | from_id, to_id, kind (entity, semantic, causal, temporal), weight | Causal links copied from `causation_id` chains in `events` |
| `observations` | observation_id, bank, scope, version, supersedes, text, proof_count, known_at, status | Versions chain through `supersedes`; the version in force at T is the latest with `known_at` at or before T |
| `observation_evidence` | observation_id, memory_id, quote, stance (supports, weakens, extends) | Exact quotes, as Hindsight does |
| `knowledge_pages` | page_id, bank, question, scope, text, version, built_from_known_at, stale | Read into prompt layer 2 |
| `memory_recalls` | recall_id, agent, query, as_of, returned_ids[], tokens, latency_ms | Every recall is logged, so memory's value can be measured |

### 7.4 Operations

| Operation | What it does | LLM |
|---|---|---|
| `remember(bank, text, occurred_at, source_event_ids, trust, document_id)` | Stores a memory. Structured inputs such as a post-mortem record are stored verbatim. Free text goes through an extraction into one or more memories with entities and times | Haiku 5.5, free text only |
| `recall(bank, query, as_of, budget_tokens)` | As-of filter, four retrieval arms, fusion, rerank, pack to budget | none |
| `consolidate(scope)` | Nightly, per scope (symbol, setup, regime, agent). New memories plus the scope's current observations go to the model, which returns refined observations with evidence ids. A contradiction produces a new version that records the change. Near-duplicates merge above cosine 0.97 | Haiku 5.5 on the Batch API; Opus 5.5 for core-symbol scopes |
| `rebuild_pages(scope)` | Rebuilds only the pages whose scope changed since `built_from_known_at` | Opus 5.5 on the Batch API |
| `reflect(bank, question, as_of)` | Weekly, in the review ritual: a bounded agent loop over pages, observations and memories to answer a question such as "why did we lose money on Thursdays?" | Opus 5.5 |

### 7.5 Retrieval

1. Filter: `known_at <= T`, the bank, and optional symbol and tags. Nothing after this step can leak the future.
2. Four arms in parallel over the filtered set: semantic (pgvector HNSW over `bge-small-en-v1.5` embeddings), keyword (`tsvector`), graph (memories sharing entities with the query's entities, plus causal neighbours, two hops), temporal (memories whose `occurred_at` falls in the query's window).
3. Reciprocal rank fusion with k = 60 and equal weights.
4. Rerank the top 100 with a local cross-encoder (`ms-marco-MiniLM-L-6-v2`), multiplied by small boosts for recency, proof count and trust.
5. Pack to the budget by score, 800 tokens for a decision-time recall, whole items only.

Recall runs on the cold path only. Expected latency is 100 to 400 ms on a small VM.

### 7.6 Who writes, who reads

| Agent | Writes | Reads |
|---|---|---|
| Performance reviewer | Experience memories from every closed thesis and intraday session; triggers consolidation | Observations, when writing post-mortems |
| Analysts | World memories observed while reporting, limited to one or two per report | Their own page at boot (layer 2); a recall of precedent for the symbol (layer 3) |
| Head trader | Rationale and lessons at thesis close | Desk pages at boot; a recall of precedent at decision time; the symbol page when forming a thesis |
| Skeptic | Nothing | The same recall as the head trader, so it can cite past failures |
| Humans | Directives | Everything, through the journal |
| Risk officer | Nothing | Nothing. Memory never reaches a risk rule |

### 7.7 Replay and evaluation

Because observations and pages are versioned by `known_at`, a replay at simulated time T reads exactly the beliefs the live desk held at T, with no rebuild. For a backtest of a new strategy over a period the desk never traded, memory starts empty and fills from the replayed journal in simulated time. Every backtest and every paper month runs twice, with memory and without, and the difference is reported. If memory does not improve after-cost results, it stays in evidence-only mode and does not feed conviction.

### 7.8 Cost

About 100 remembers a day, of which perhaps 20 need extraction; 40 consolidation scopes a night on the Batch API; 10 to 20 page rebuilds a day. Roughly $1.50 a day, with the page rebuilds for the 30 core names on Opus as the largest part. Recall and the local models cost CPU only.
