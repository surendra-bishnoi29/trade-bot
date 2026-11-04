# Roadmap

Status: draft for review, 2026-11-01. Durations assume one or two people working on it most days; they stretch with a smaller team and compress with a larger one. Every phase ends with a review of its deliverables before the next starts.

## Phase 0: Design (now)

Deliverables: the four documents in this folder, reviewed and agreed.

Exit: every assumption in [00-decisions.md](00-decisions.md) is confirmed or changed, and the open questions needed for phase 1 are answered.

## Phase 1: Foundation (2 to 3 weeks)

Goal: the desk's skeleton runs end to end on a paper account with a scripted signal, and a historical day can be replayed through the same code.

Deliverables:
- Repository scaffold per the architecture's layout; configuration; the autonomy level switch.
- Event envelope and payload schemas; Redis Streams bus and in-memory bus; consumer groups; dead-letter handling.
- Head trader skeleton: scheduler on the exchange calendar, swing cycle state machine, correlation ids, timeouts, partial-data policy. No LLM yet.
- Market data collector: historical daily and minute bars for the core universe; real-time minute bars during the session.
- PostgreSQL and TimescaleDB schema; append-only event store; Parquet export.
- Execution trader with the Alpaca paper adapter; back office with reconciliation.
- Risk officer with the per-order and per-position rules.
- Journal with the audit chain; a plain daily brief.
- Replayer and simulated broker.
- Docker Compose; metrics and alerts for failures and stale data.

Exit criteria:
- A scripted signal produces a paper order whose journal entry links intent, risk decision, submission and fill.
- One historical trading day replays through the bus with identical agent code and produces simulated fills.
- Stale data or a broker disconnect engages the kill switch and alerts.

## Phase 2: Quantitative research (3 to 4 weeks)

Goal: honest baselines for both books, with a backtester we trust.

Deliverables:
- Feature Store with point-in-time joins; the v1 technical and intraday features.
- Swing forecaster: LightGBM on the v1 feature set predicting 1, 5 and 20 day return distributions; calibration layer; walk-forward training.
- Intraday baselines: opening-range breakout, VWAP reversion and momentum continuation setups, parameterized and evaluated by time of day and regime.
- Forecast Ensemble and the Swing Strategy rules (entry on conviction, invalidation monitoring, sizing by uncertainty).
- Cost and slippage model calibrated to paper fills.
- Evaluation reports: Sharpe after costs, drawdown, hit rate, calibration, turnover, factor exposures; per-regime breakdowns.

Exit criteria:
- Walk-forward results are reported after costs and reproduce exactly on re-run.
- An independent lookahead audit (a feature-by-feature check of timestamps) passes.
- We know which baselines carry signal and which do not. A negative result is an acceptable outcome; it tells the head trader when to do nothing.

## Phase 3: The research team (3 to 4 weeks)

Goal: the LLM analysts produce structured, evaluated reports that measurably improve on the quant baseline.

Deliverables:
- News collector (Benzinga and Polygon news, EDGAR 8-K feed, RSS fallbacks) with deduplication.
- News and Sentiment Analyst: Haiku scoring with structured outputs, Opus escalation, `news.state` for the intraday book, Batch API for overnight bulk work.
- Filing and Transcript Analyst: risk-factor diffs, call tone.
- Macro Strategist: FRED and calendar collectors, regime classifier, release surprise handling, narrative report.
- Fundamental Analyst: fundamentals and estimates collector, scoring, synthesis.
- Sector Specialist and Events Analyst.
- Research report schema and the analyst evaluation harness: stance and conviction scored against realized outcomes on a labeled historical set.
- Prompt registry with versioning; LLM output cache; cost logging.

Exit criteria:
- Each analyst's reports are scored on history and the scores are reported, including those no better than chance.
- Adding the sentiment and event features to the swing forecaster improves out-of-sample results, or we document that it does not and keep them as evidence for the head trader only.
- Daily LLM spend is measured and within the agreed envelope.

## Phase 4: The head trader and the risk officer (2 to 3 weeks)

Goal: the full decision loop with theses, playbooks, risk and learning.

Deliverables:
- Thesis object and lifecycle; event-triggered re-evaluation.
- Head trader decision prompts (Opus) with structured outputs, credibility weights, and the daily brief.
- Intraday playbook generation and the Intraday Strategy wired to it.
- Risk officer completed: per-book, portfolio, drawdown ladder, kill switch, pattern-day-trader awareness; capital allocation between books.
- Performance Reviewer: trade post-mortems, analyst credibility scoring, forecast calibration tracking, model promotion gate.
- Replay-based evaluation of head-trader decisions against a rules-only baseline.

Exit criteria:
- A full month of history replays through the complete desk with cached LLM outputs, producing a journal we can read trade by trade.
- Every order in the replay traces to a thesis, reports and events.
- The risk officer rejects every injected limit violation in a test suite.

## Phase 5: Autonomous paper trading (at least 3 months)

Goal: run the desk every trading day on the paper account and learn from it.

Deliverables:
- Cloud deployment with monitoring, alerting and the daily brief delivered to the chosen channel. *Done 2026-11-04: three services on the VM, external health check every five minutes to Telegram, nightly backups with a monthly restore drill.*
- Weekly review ritual: performance, post-mortems, analyst scores, incidents, prompt and model changes. *Done 2026-11-04: the reviewer writes it every Friday (`desk weekly`); the graduation checklist below is `desk graduation` and the Reviews tab.*
- Weekly retraining with the promotion gate; monthly prompt review. *Retraining done (Saturday timer); the prompt review is a human ritual.*

Proposed graduation criteria to level 2 (live with approval gate), to confirm:
- At least 3 months and 60 trading days of uninterrupted paper operation.
- Positive after-cost performance in the swing book with drawdown inside the limit; the intraday book either positive after costs or switched off.
- Forecast calibration within tolerance; no unexplained journal gaps; zero risk-rule bypasses; incident count trending down.
- Paper fills reconciled against the broker every day without manual fixes.

## Phase 6: Live with approval gate, then within limits

Small capital, level 2 first. Compare live fills with paper assumptions and recalibrate the cost model. Move to level 3 only after a further agreed period at level 2 with no interventions needed. Alternative data (section H of the factor catalogue) and additional books (options, other markets) are considered only after this point.

## Working agreement

- Design before code: each phase opens with a short design note for anything not already covered here, and closes with a review.
- Negative results are reported as plainly as positive ones.
- Nothing touches live keys without an explicit, logged decision.
