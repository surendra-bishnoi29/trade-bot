# Factor catalogue: what moves a stock price

Status: draft for review, 2026-11-01. This is the research team's checklist. Every factor here becomes either a numeric feature in the Feature Store or a structured event produced by an LLM analyst, always stamped with when it was knowable.

## How to read this

- **Horizon**: I = matters mainly for the intraday book, S = swing book, B = both.
- **Update**: how often the value changes: tick, 1m, daily, weekly, monthly, quarterly, or event (when something happens).
- **Source**: MKT market data vendor; FUND fundamentals and estimates vendor; NEWS news vendor; EDGAR SEC filings; FRED and other official statistics; FINRA; CFTC; CBOE; SOCIAL social APIs; ALT alternative data vendor; LLM means an LLM analyst produces the value from text; derived = computed from other factors.
- **Owner**: the agent responsible. QA Quant Analyst, FA Fundamental Analyst, NSA News and Sentiment Analyst, FTA Filing and Transcript Analyst, MS Macro Strategist, SS Sector Specialist, EA Events Analyst, RO Risk Officer, ALT alternative data collector (later phase).

## How factors enter the system

1. Structured data (prices, financials, macro series) arrives as events, the Feature Store computes numeric features point-in-time, and the quant models consume them.
2. Unstructured data (news, filings, transcripts, social posts) is read by LLM analysts, which emit structured, scored events. Those become numeric features too (sentiment, event type, impact) and are cited as evidence in analyst reports.
3. Scheduled events (earnings, macro releases, expiries) become calendar features such as days until and days since, and define no-trade windows.
4. The head trader sees the analysts' reports and the forecast ensemble, not raw features.

Honest expectations: individually, most of these factors carry weak and unstable signal. The value is in combining independent families, respecting regime, sizing by uncertainty, and refusing to trade when evidence is thin. Costs eat small edges, so every evaluation is after costs.

## A. Price and volume (technical)

### Trend

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Moving averages (SMA and EMA 10, 20, 50, 200) and distance to each | Close relative to each average, in percent | B | 1m, daily | MKT | QA |
| Moving-average crossovers | 20/50 and 50/200 cross state and days since the cross | S | daily | derived | QA |
| ADX and directional movement | Trend strength (14 periods) | B | 1m, daily | derived | QA |
| Regression slope and fit of log price | Trend quality over 20 and 60 bars | B | 1m, daily | derived | QA |
| Donchian channel position | Position inside the 20 and 55 bar high-low range | B | 1m, daily | derived | QA |

### Momentum

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Multi-horizon returns | 1, 5, 21, 63, 126 and 252 day total return | S | daily | MKT | QA |
| 12-1 momentum | Twelve-month return skipping the most recent month | S | daily | derived | QA |
| RSI (14) and RSI divergence | Overbought and oversold; price versus RSI divergence | B | 1m, daily | derived | QA |
| MACD (12, 26, 9) line, signal, histogram | Trend momentum and its change | B | 1m, daily | derived | QA |
| Stochastic, CCI, Williams %R, rate of change | Oscillator family | B | 1m, daily | derived | QA |
| Momentum acceleration | Change in the 21-day return over the last 5 days | S | daily | derived | QA |

### Mean reversion

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Z-score versus 20-day mean | Standardized distance from the mean | B | 1m, daily | derived | QA |
| Bollinger %B and bandwidth | Position inside the bands; band squeeze | B | 1m, daily | derived | QA |
| Short-term reversal | Prior 1-week and 1-month return, which tends to reverse | S | daily | derived | QA |
| VWAP distance | Price minus session VWAP, in ATR units | I | 1m | derived | QA |

### Volatility

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Realized volatility (10, 21, 63 day) | Standard deviation of log returns, annualized | B | daily | derived | QA |
| Parkinson and Garman-Klass volatility | Range-based estimators | B | daily | derived | QA |
| ATR (14) | Average true range, daily and intraday | B | 1m, daily | derived | QA |
| GARCH(1,1) forecast | Conditional volatility forecast | S | daily | derived | QA |
| Volatility regime percentile | Current volatility against its own one-year distribution | B | daily | derived | QA |
| Implied volatility, IV rank, IV percentile | From the options chain | B | 1m, daily | MKT (options) | QA |
| Implied minus realized spread | Variance risk premium per name | S | daily | derived | QA |
| Implied move into earnings | Straddle-implied expected move | S | daily | MKT (options) | QA |

### Volume and liquidity

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Average daily volume (20) and dollar volume | Liquidity base | B | daily | MKT | QA |
| Relative volume | Today's volume against the 20-day average, adjusted for time of day | B | 1m, daily | derived | QA |
| Volume z-score and volume spikes | Abnormal activity | B | 1m, daily | derived | QA |
| On-balance volume, accumulation/distribution, Chaikin money flow, money flow index | Volume-price flow indicators | B | 1m, daily | derived | QA |
| VWAP and anchored VWAP | Session and event-anchored volume-weighted price | I | 1m | derived | QA |
| Turnover ratio | Volume over shares outstanding | S | daily | derived | QA |
| Amihud illiquidity | Absolute return per dollar of volume | S | daily | derived | QA |
| Bid-ask spread (quoted and effective) | Trading cost and stress signal | B | tick, 1m | MKT | QA |

### Levels and ranges

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| 52-week high and low distance | Percent from each; new-high and new-low flags | S | daily | derived | QA |
| All-time high distance | | S | daily | derived | QA |
| Prior day high, low, close | Key reference levels | I | daily | derived | QA |
| Pre-market high and low | | I | 1m | MKT | QA |
| Opening range (5, 15, 30 minutes) | High and low of the opening window; breakout flags | I | 1m | derived | QA |
| Gap size and gap type | Open against prior close; gap-and-go versus gap-fill statistics | B | daily | derived | QA |
| Daily range against ATR | Range expansion or compression | B | daily | derived | QA |
| Pivot points and round numbers | Classic pivots; proximity to round price levels | I | daily | derived | QA |
| Support and resistance clusters | Price levels with repeated reversals or volume nodes (volume profile) | B | daily | derived | QA |
| Candlestick patterns | Engulfing, doji, hammer, inside bar and similar | B | 1m, daily | derived | QA |

### Microstructure (intraday)

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Order book imbalance | Bid size against ask size at the top levels | I | tick | MKT (quotes) | QA |
| Trade imbalance and cumulative delta | Buy-initiated minus sell-initiated volume (tick rule or quote rule) | I | tick, 1m | MKT (trades) | QA |
| Large prints and block trades | Trades above a size threshold; dark-pool prints where available | I | tick | MKT | QA |
| Trade and quote intensity | Trades per minute, quote updates per minute | I | 1m | derived | QA |
| Spread widening and depth thinning | Liquidity stress | I | tick | derived | QA |
| Time-of-day profiles | Expected volume and volatility by minute of the day (the U-shape) | I | 1m | derived | QA |
| Market-on-close imbalance | Published MOC imbalance near 15:50 | I | event | MKT | QA |
| Trading halts (LULD) | Halt state and reopen | I | event | MKT | RO |

## B. Company fundamentals

### Valuation

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| P/E trailing and forward, PEG | | S | daily (price), quarterly (earnings) | FUND | FA |
| P/B, P/S, EV/EBITDA, EV/Sales, EV/FCF | | S | daily, quarterly | FUND | FA |
| Earnings yield, FCF yield, dividend yield, shareholder yield | Shareholder yield is dividends plus net buybacks | S | daily, quarterly | FUND | FA |
| Relative valuation | Multiple against the sector median and against the name's own five-year history (z-scores) | S | daily | derived | FA |

### Profitability

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Gross, operating and net margin, and their trends | Level and year-over-year change | S | quarterly | FUND | FA |
| ROE, ROA, ROIC, and ROIC minus cost of capital | | S | quarterly | FUND | FA |
| Asset turnover, operating leverage | | S | quarterly | FUND | FA |

### Growth

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Revenue, EPS and FCF growth (YoY, QoQ, three-year CAGR) | | S | quarterly | FUND | FA |
| Growth acceleration | Change in the growth rate | S | quarterly | derived | FA |
| Forward growth estimates (FY1, FY2) | Consensus | S | daily | FUND | FA |

### Quality and safety

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Accruals ratio, cash conversion (operating cash flow over net income) | Earnings quality | S | quarterly | FUND | FA |
| Piotroski F-score, Altman Z-score, Beneish M-score | Composite quality, distress and manipulation screens | S | quarterly | derived | FA |
| Debt/equity, net debt/EBITDA, interest coverage, current and quick ratio | Balance sheet strength | S | quarterly | FUND | FA |
| Share count change | Dilution or buyback | S | quarterly | FUND | FA |
| Earnings variability, goodwill over assets, R&D intensity, capex over sales | | S | quarterly | FUND | FA |
| Debt maturity wall | Debt due in the next 12 to 24 months | S | quarterly | FUND, EDGAR | FA |

### Earnings dynamics

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| EPS and revenue surprise | Actual against consensus, in percent and standardized (SUE) | S | event (quarterly) | FUND | FA |
| Beat and miss history | Streaks and consistency | S | quarterly | derived | FA |
| Guidance against consensus | Raised, maintained, lowered; magnitude | S | event | FUND, NEWS, LLM | FA, NSA |
| Earnings-day reaction and gap | The market's verdict | B | event | derived | QA |
| Post-earnings-announcement drift window | Days since the report and direction of the surprise | S | daily | derived | FA |
| Pre-announcement drift | Return in the 10 days before the report | S | daily | derived | QA |

### Analysts and estimates

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Consensus EPS and revenue estimates; revision percent over 1 and 3 months | Estimate momentum | S | daily | FUND | FA |
| Revision breadth | Count of upward against downward revisions | S | daily | FUND | FA |
| Rating changes | Upgrades and downgrades, with the firm | B | event | FUND, NEWS | FA, NSA |
| Consensus rating and target-price upside | Target against price | S | daily | FUND | FA |
| Estimate dispersion and analyst count | Uncertainty and coverage | S | daily | FUND | FA |

### Ownership and positioning

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Institutional ownership and its quarterly change | 13F aggregates | S | quarterly | EDGAR, FUND | FA |
| Hedge fund ownership and crowding | 13F-based crowding score | S | quarterly | FUND | FA |
| Insider transactions | Form 4 net buying; cluster buys; officers against directors | S | event | EDGAR | FA |
| Insider ownership percent | | S | quarterly | FUND | FA |
| Short interest, days to cover, change | FINRA twice monthly; daily estimates from a paid vendor | B | twice monthly, daily | FINRA, FUND | FA |
| Cost to borrow and utilization | Squeeze pressure | B | daily | FUND (Ortex or S3) | FA |
| Free float and float turnover | | B | daily | FUND | FA |
| Index membership and weight | S&P 500, Nasdaq 100, Russell | S | event | FUND | EA |

### Capital allocation and structure

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Buyback announcements and execution pace | | S | event, quarterly | NEWS, EDGAR | FA |
| Dividend initiation, increase, cut, special dividend | | S | event | FUND | FA, EA |
| Equity issuance, secondary offerings, convertibles | | B | event | EDGAR, NEWS | EA |
| Debt issuance and refinancing | | S | event | EDGAR, NEWS | FA |
| Market cap, enterprise value, stock age, share class, ADR status | Size and structure | S | daily | FUND | FA |

## C. Corporate events and calendar

### Scheduled

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Earnings date and time (before open, after close) | Days until and days since | B | daily | FUND | EA |
| Dividend ex-date and pay date | | B | daily | FUND | EA |
| Splits and reverse splits | | B | event | FUND | EA |
| Shareholder meeting, investor day, conference presentations | | S | daily | FUND, NEWS | EA |
| Product launch and event dates | | S | daily | NEWS | EA |
| FDA decision dates (PDUFA) and trial readouts | Biotech and pharma | S | daily | FUND, NEWS | EA |
| Index rebalance dates | S&P quarterly, Russell reconstitution | B | daily | FUND | EA |
| Options expiration | Monthly, quarterly (quad witching), weekly | B | daily | derived | EA |
| IPO lock-up expiry | | S | daily | FUND | EA |
| 13F and other filing deadlines | | S | daily | derived | EA |

### Unscheduled

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Mergers and acquisitions | Acquirer, target, terms, premium | B | event | NEWS, EDGAR, LLM | NSA, EA |
| Spin-offs and divestitures | | S | event | NEWS, EDGAR | EA |
| Activist stakes (13D) | | S | event | EDGAR | EA |
| Management changes (CEO, CFO) | | B | event | NEWS, EDGAR (8-K) | NSA, EA |
| Guidance updates and pre-announcements | | B | event | NEWS, LLM | NSA |
| Restatements, auditor changes, going-concern language | | S | event | EDGAR, LLM | FTA |
| Lawsuits, settlements, regulatory actions (SEC, DOJ, FTC) | | B | event | NEWS, EDGAR, LLM | NSA |
| Credit rating changes | | S | event | NEWS | NSA, FA |
| Bankruptcy and delisting notices | | B | event | NEWS, EDGAR | EA |
| Large contracts, partnerships, product recalls, data breaches, labor actions | | B | event | NEWS, LLM | NSA |

## D. News and sentiment

### News, per article and per symbol

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Relevance | Is the article about this company; primary subject or passing mention | B | event | NEWS, LLM | NSA |
| Novelty | New information or a rehash of the last 48 hours | B | event | derived (embeddings) | NSA |
| Polarity and confidence | Minus one to plus one, with the model's confidence | B | event | LLM | NSA |
| Event type | Classification into the taxonomy of section C plus macro | B | event | LLM | NSA |
| Expected impact and horizon | Material or not; intraday, days, weeks | B | event | LLM | NSA |
| Source credibility weight | Wire services and major outlets against blogs and promotional sources | B | event | derived | NSA |
| Headline against body sentiment gap | Clickbait detection | B | event | LLM | NSA |
| Press release or third-party coverage | Company-authored or independent | B | event | NEWS | NSA |
| Breaking-news flag and shock score | Sudden news plus abnormal volume and price move | I | event | derived | NSA, QA |
| News volume and abnormal news volume | Articles per day against baseline | B | daily | derived | NSA |
| Sentiment momentum and dispersion | Change in average sentiment over 1 and 5 days; disagreement across sources | S | daily | derived | NSA |
| Sentiment-price divergence | Sentiment improving while price falls, and the reverse | S | daily | derived | NSA |

### Filings and transcripts

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| 10-K and 10-Q risk-factor changes | Diff against the prior filing; newly added risks | S | event | EDGAR, LLM | FTA |
| MD&A tone and language change | Uncertainty, negativity and litigiousness lexicons plus an LLM reading | S | event | EDGAR, LLM | FTA |
| 8-K item types | Which items were filed (2.02 results, 5.02 officer changes, 8.01 other events) | B | event | EDGAR | EA, FTA |
| Earnings call tone | Prepared remarks against Q&A; evasiveness, hedging, confidence | S | event | transcripts, LLM | FTA |
| Analyst Q&A surprise | Questions management struggled with; topics absent from the release | S | event | transcripts, LLM | FTA |
| Forward-looking statement density and specificity | | S | event | LLM | FTA |

### Social and attention

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Mention volume and z-score | Reddit, StockTwits, X | B | 1m to daily | SOCIAL | NSA |
| Bullish against bearish ratio | Platform labels plus LLM | B | daily | SOCIAL, LLM | NSA |
| Influencer and large-account mentions | | I | event | SOCIAL | NSA |
| Bot and spam filtering | Weight by account quality | B | daily | derived | NSA |
| Google Trends search interest | | S | weekly, daily | ALT | NSA |
| Wikipedia page views | Attention proxy | S | daily | ALT | NSA |
| Composite sentiment score | Weighted blend of news, filings and social, with credibility weights | B | daily | derived | NSA |

## E. Sector and industry

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| GICS sector and industry | Classification | B | static | FUND | SS |
| Sector ETF returns (1, 5, 21 day) and relative strength against SPY | XLK, XLF, XLE and the other sector ETFs | B | 1m, daily | MKT | SS |
| Stock return relative to its sector | Idiosyncratic component | B | daily | derived | SS |
| Sector momentum rank and rotation phase | Which sectors lead; cyclical against defensive leadership | S | daily | derived | SS |
| Peer group return and dispersion | Named peer set per stock | B | daily | derived | SS |
| Peer lead-lag | Reaction after a peer reports or guides | B | event | derived | SS |
| Sector earnings-season aggregate | Beat rate and guidance tone across the sector so far | S | daily | FUND | SS |
| Sector short interest and fund flows | | S | weekly | FINRA, FUND | SS |
| Industry drivers | Oil and gas prices (energy); yield curve and credit (banks); semiconductor index and memory prices (chips); mortgage rates and housing starts (homebuilders); jet fuel and travel data (airlines); retail sales and card spend (retail); drug approvals (pharma); ad spend (media); freight indices (transport); metal prices (miners) | B | daily | FRED, MKT, ALT | SS, MS |
| Supply chain exposure | Key customers and suppliers; their news and results | S | event | FUND, NEWS | SS |
| Input cost and currency exposure | Commodity inputs; revenue by geography | S | quarterly | FUND | SS |
| Sector regulatory environment | Pending rules, antitrust, tariffs by sector | S | event | NEWS, LLM | SS, NSA |

## F. Macroeconomic

### Rates and central bank

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Fed funds rate and the expected path | From fed funds futures (market-implied cuts and hikes) | B | 1m, daily | MKT, CME | MS |
| Treasury yields (2, 10, 30 year) and daily changes | | B | 1m, daily | FRED, MKT | MS |
| Curve slope (2s10s, 3m10y) and inversion state | | S | daily | derived | MS |
| Real yields (TIPS) and term premium | | S | daily | FRED | MS |
| MOVE index | Bond market volatility | B | daily | MKT | MS |
| FOMC dates; statement and minutes tone; dot plot shifts; speaker tone | LLM-read central bank communication | B | event | FRED, NEWS, LLM | MS |

### Inflation

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| CPI, core CPI, PPI, PCE (month over month, year over year) and surprise against consensus | | B | monthly (event) | FRED, calendar | MS |
| Breakeven inflation (5 and 10 year), 5y5y forward | | S | daily | FRED | MS |
| Inflation expectations surveys | University of Michigan, New York Fed | S | monthly | FRED | MS |

### Growth and activity

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| GDP (advance and revisions), GDPNow | | S | quarterly, weekly | FRED, Atlanta Fed | MS |
| ISM manufacturing and services PMI and sub-indices (new orders, prices paid, employment) | | B | monthly (event) | calendar, FRED | MS |
| Industrial production, retail sales, durable goods | | B | monthly (event) | FRED | MS |
| Housing starts, permits, existing and new home sales | | S | monthly | FRED | MS |
| Consumer confidence (Conference Board, Michigan) | | S | monthly | FRED | MS |
| Leading economic index | | S | monthly | FRED | MS |
| Economic surprise index | Rolling aggregate of releases against consensus | S | daily | derived or vendor | MS |

### Labor

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Nonfarm payrolls and revisions, unemployment rate, participation, average hourly earnings | | B | monthly (event) | FRED | MS |
| Initial and continuing jobless claims | | B | weekly (event) | FRED | MS |
| JOLTS openings and quits, ADP | | S | monthly | FRED | MS |

### Money, credit and conditions

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Credit spreads (investment grade and high yield OAS) and CDS indices | Risk appetite and stress | B | daily | FRED, MKT | MS |
| Financial conditions indices (Chicago Fed NFCI and others) | | S | weekly | FRED | MS |
| Bank lending standards (SLOOS) | | S | quarterly | FRED | MS |
| Fed balance sheet, reserves, reverse repo, Treasury general account | Liquidity plumbing | S | weekly | FRED | MS |
| M2 growth | | S | monthly | FRED | MS |

### Currency and commodities

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Dollar index and major pairs (EUR, JPY, CNY) | | B | 1m, daily | MKT | MS |
| Crude oil (WTI, Brent), natural gas, gasoline | | B | 1m, daily | MKT | MS |
| Gold, silver, copper | Copper as a growth proxy | B | daily | MKT | MS |
| Agricultural and broad commodity indices | | S | daily | MKT | MS |

### Fiscal, policy and global

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Budget deficit, Treasury issuance and refunding, debt ceiling events | | S | event | Treasury, NEWS | MS |
| Tax, tariff and trade policy changes | | B | event | NEWS, LLM | MS, NSA |
| Government shutdown risk and fiscal deadlines | | S | event | NEWS | MS |
| ECB, BoJ, BoE, PBoC policy and global PMIs | | S | event | calendar, NEWS | MS |
| China credit impulse and activity data | | S | monthly | vendor | MS |

### Calendar

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Scheduled release times and consensus | For every release above | B | daily | calendar vendor | EA, MS |
| Days until the next FOMC, CPI, payrolls | | B | daily | derived | EA |
| Release surprise magnitude | Actual minus consensus, standardized by history | B | event | derived | MS |

## G. Market regime, positioning and flows

### Index and style

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| S&P 500, Nasdaq 100, Russell 2000, Dow returns and trend against 50 and 200 day averages | | B | 1m, daily | MKT | MS |
| Index drawdown from peak | | B | daily | derived | MS |
| Equal-weight against cap-weight, growth against value, small against large, high beta against low vol, cyclicals against defensives | Style and breadth of leadership | S | daily | MKT (ETF pairs) | MS |

### Volatility

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| VIX level and percentile | | B | 1m, daily | MKT | MS |
| VIX term structure (VIX/VIX3M, futures curve); contango or backwardation | | B | daily | MKT, CBOE | MS |
| VVIX, SKEW index | Volatility of volatility; tail demand | S | daily | CBOE | MS |
| Variance risk premium | Implied minus realized at the index level | S | daily | derived | MS |
| Implied correlation index | | S | daily | CBOE | MS |

### Breadth

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Advance-decline line and ratio; up against down volume | | B | 1m, daily | MKT | MS |
| Percent of stocks above their 50 and 200 day averages | | S | daily | derived | MS |
| New highs minus new lows | | S | daily | derived | MS |
| McClellan oscillator and summation index | | S | daily | derived | MS |
| Breadth thrusts | Zweig and similar | S | daily | derived | MS |
| Intraday internals: TICK, ADD, VOLD, TRIN | | I | tick, 1m | MKT | MS, QA |

### Options positioning

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Put-call ratios (equity, index, total) | | B | 1m, daily | CBOE, MKT | MS |
| Dealer gamma exposure, gamma flip level, call and put walls | Index and single name | B | 1m, daily | MKT (options), vendor | MS, QA |
| 0DTE volume share | | I | daily | CBOE | MS |
| Unusual options activity and sweeps per name | | B | event | MKT (options), vendor | QA |
| Single-name skew and term structure | | S | daily | MKT (options) | QA |
| Max pain and open interest by strike into expiry | | B | daily | MKT (options) | QA |

### Flows and positioning

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| ETF flows (SPY, QQQ, sectors) | | S | daily | vendor | MS |
| Mutual fund and money market flows | | S | weekly | ICI | MS |
| Margin debt | | S | monthly | FINRA | MS |
| CFTC commitments of traders | Speculative positioning in index futures | S | weekly | CFTC | MS |
| Retail flow estimates | | B | daily | vendor | MS |
| Systematic and CTA positioning estimates | Vol-control and trend-follower exposure | S | daily, weekly | vendor | MS |
| Buyback blackout windows and corporate buyback pace | | S | daily | derived, vendor | MS |
| Aggregate insider buy-sell ratio | Market-wide | S | weekly | EDGAR | MS |
| Hedge fund gross and net leverage | Prime broker reports | S | weekly | vendor | MS |
| Sentiment surveys: AAII, NAAIM, Investors Intelligence, fund manager survey cash levels | Contrarian indicators | S | weekly, monthly | surveys | MS |
| Composite fear and greed index | | S | daily | derived | MS |

### Seasonality and calendar effects

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Day of week, turn of month, month of year | Known calendar anomalies | B | daily | derived | QA |
| Options expiration week and quad witching | | B | daily | derived | EA |
| Pre-FOMC drift, CPI day, payrolls day | Event-day behavior | B | daily | derived | QA |
| Earnings season intensity | Share of the index reporting this week | S | weekly | FUND | EA |
| Quarter-end window dressing, tax-loss selling season, January effect | | S | daily | derived | QA |
| Holiday and half-day sessions | | B | daily | exchange calendar | EA |
| Index rebalance and reconstitution days | | B | daily | FUND | EA |

### Cross-asset

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Bond-equity correlation (rolling 60 day) | Hedging regime | S | daily | derived | MS |
| Dollar-equity, oil-equity and gold-equity correlations | | S | daily | derived | MS |
| Credit-equity divergence (HYG against SPY) | | S | daily | derived | MS |
| Bitcoin as a risk-appetite proxy | | B | 1m, daily | MKT | MS |
| Growth-stock duration sensitivity | Response of long-duration names to yield moves | S | daily | derived | MS |
| Risk-on, risk-off composite | Blend of the above | B | daily | derived | MS |

## H. Alternative data (phase 6 or later)

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Web traffic, app downloads, daily active users | SimilarWeb, Sensor Tower | S | weekly | ALT | ALT |
| Credit and debit card transaction panels | Consumer spend by merchant | S | weekly | ALT | ALT |
| Foot traffic and geolocation | Placer.ai and similar | S | weekly | ALT | ALT |
| Satellite imagery | Parking lots, oil storage, construction | S | weekly | ALT | ALT |
| Job postings, headcount, employee reviews | Hiring trends; Glassdoor | S | weekly | ALT | ALT |
| Government contracts, lobbying, congressional trading | USAspending, Quiver | S | event | ALT | ALT |
| Patents, FDA databases, clinical trial registries | | S | event | ALT | ALT |
| Shipping and vessel tracking, freight indices, import bills of lading | | S | daily, weekly | ALT | ALT |
| Product pricing and availability scrapes, app store reviews, Amazon rank | | S | daily | ALT | ALT |
| Weather and climate | Agriculture, energy, retail, insurance | B | daily | NOAA | ALT |
| Electricity consumption, hotel RevPAR, box office, used-car prices, semiconductor channel inventory | Sector-specific trackers | S | weekly | ALT | ALT |
| Corporate jet tracking | Merger hints | S | event | ALT | ALT |
| ESG scores and controversies | | S | monthly | ALT | ALT |

## I. Geopolitical and exogenous shocks

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Elections and policy uncertainty | US and major economies; economic policy uncertainty index | S | event, monthly | NEWS, ALT | MS |
| Geopolitical risk index | Caldara-Iacoviello GPR | S | daily, monthly | ALT | MS |
| Wars, conflicts, terrorism | | B | event | NEWS, LLM | NSA, MS |
| Sanctions, export controls (chips, rare earths), tariffs | | B | event | NEWS, LLM | NSA, MS |
| OPEC decisions and energy supply shocks | | B | event | NEWS | MS |
| Shipping chokepoint disruptions | Suez, Hormuz, Panama | S | event | NEWS | MS |
| Pandemics and health emergencies | | S | event | NEWS | MS |
| Natural disasters | Hurricanes, earthquakes, wildfires; sector exposure | B | event | NEWS, NOAA | MS, SS |
| Cyberattacks on infrastructure or large companies | | B | event | NEWS | NSA |
| Court rulings and antitrust actions | | B | event | NEWS | NSA |
| Sovereign events | Debt downgrades, debt ceiling, central bank independence | S | event | NEWS | MS |

## J. Behavioral and stock-level positioning

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Retail interest proxies | Social mentions, popularity rankings, fractional-share flows | B | daily | SOCIAL, vendor | NSA, QA |
| Short-squeeze setup score | High short interest, high borrow cost, low float, rising price, high volume | B | daily | derived | QA |
| Gamma-squeeze setup score | Heavy near-the-money call open interest relative to float | B | daily | derived | QA |
| Momentum and factor crowding | How many systematic strategies are in the same trade | S | weekly | derived, vendor | QA |
| Analyst herding | Low estimate dispersion with skewed ratings | S | daily | derived | FA |
| Lottery-like features | Maximum daily return in the past month; skewness | S | daily | derived | QA |
| 52-week high anchoring | Proximity to the high and breakout behavior | S | daily | derived | QA |
| Capital gains overhang | Estimated unrealized gains of holders (disposition effect) | S | daily | derived | QA |
| Attention spikes | Abnormal volume with abnormal news or social volume | B | daily | derived | QA |
| Meme-stock classifier | Retail-driven moves detached from fundamentals | B | daily | derived, LLM | NSA |
| Post-earnings under- and over-reaction classification | | S | event | derived | QA |
| Round-number and stop-cluster levels | Where stops are likely stacked | I | daily | derived | QA |
| IPO and SPAC hype indicators | Recency of listing, lock-up, promotional coverage | S | daily | FUND, NEWS | EA |

## K. Factor exposures and risk model

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Market beta (rolling 60 and 250 day), downside beta | | B | daily | derived | QA, RO |
| Fama-French five factors plus momentum loadings | Market, size, value, profitability, investment, momentum | S | daily | derived (Ken French data) | QA |
| Low volatility and betting-against-beta exposure | | S | daily | derived | QA |
| Quality-minus-junk exposure | | S | daily | derived | QA |
| Liquidity factor exposure | | S | daily | derived | QA |
| Idiosyncratic volatility and skewness | Residual after the factor model | S | daily | derived | QA |
| Residual momentum | Momentum of the idiosyncratic component | S | daily | derived | QA |
| Factor momentum and factor valuation spreads | Which factors are working and how expensive they are | S | daily | derived | QA |
| Style scores | Growth, value, quality, momentum, low vol, size composites | S | daily | derived | QA |
| Industry and country exposures | | S | static, quarterly | FUND | RO |
| Portfolio factor exposure and crowding | Aggregate exposures of each book and the whole desk | B | daily | derived | RO |

## L. Intraday set

The intraday essentials from the sections above, collected into the list the Intraday Strategy actually reads.

| Factor | Definition or computation | Horizon | Update | Source | Owner |
|---|---|---|---|---|---|
| Pre-market gap percent and pre-market volume against normal | | I | 1m | MKT | QA |
| Overnight futures move (ES, NQ) and international sessions (Nikkei, Europe) | | I | 1m | MKT | MS |
| Overnight news count, sentiment and shock score for watchlist names | | I | event | LLM | NSA |
| Scheduled releases in the next 60 minutes | No-trade windows | I | daily | calendar | EA |
| Opening range high and low (5, 15, 30 minutes) and breakout state | | I | 1m | derived | QA |
| VWAP, anchored VWAP, VWAP bands and distance in ATR units | | I | 1m | derived | QA |
| Relative volume by time of day | | I | 1m | derived | QA |
| Cumulative delta and order book imbalance | | I | tick | MKT | QA |
| Intraday momentum and oscillators (1, 5, 15 minute) | | I | 1m | derived | QA |
| Intraday ATR and range expansion | | I | 1m | derived | QA |
| Distance to prior day high, low and close, pre-market high and low, round numbers | | I | 1m | derived | QA |
| Market internals: TICK, ADD, VOLD, TRIN; SPY and QQQ intraday trend; sector ETF relative strength | | I | 1m | MKT | MS, QA |
| VIX, 10-year yield and dollar intraday changes | | I | 1m | MKT | MS |
| Index gamma levels: call and put walls, gamma flip | Expected pinning or acceleration | I | 1m, daily | MKT (options), vendor | QA |
| News shock state per symbol | Fresh material news in the last N minutes, with polarity | I | event | LLM | NSA |
| Unusual options sweeps and large block prints | | I | event | MKT | QA |
| Halt state, spread, depth | Tradability | I | tick | MKT | RO |
| Time of day | Open, mid-morning, lunch, power hour; minutes to close | I | 1m | derived | QA |
| Market-on-close imbalance | | I | event | MKT | QA |
| Realized beta to SPY intraday | | I | 1m | derived | QA |

## Build order

v1 (roadmap phases 2 to 4) implements the subset with the best evidence-to-effort ratio:

- **A**: multi-horizon returns, moving averages and distances, RSI, MACD, realized volatility, ATR, relative volume, 52-week distance, gaps, opening range, VWAP, cumulative delta if trade data is available.
- **B**: valuation multiples, growth, margins, quality screens, earnings surprise and revisions, short interest, insider transactions.
- **C**: earnings and dividend calendar, index events, expirations; unscheduled events through the news taxonomy.
- **D**: news relevance, novelty, polarity, event type, impact, news volume; 8-K types; earnings call tone.
- **E**: sector ETF relative strength, stock against sector, peer dispersion, three or four industry drivers.
- **F**: fed funds path, 2 and 10 year yields, curve, CPI, payrolls, ISM, claims, credit spreads, dollar, oil, release calendar with surprises.
- **G**: index trend, VIX and term structure, breadth basics, put-call, seasonality flags, intraday internals.
- **K**: beta, factor loadings, idiosyncratic volatility, portfolio exposure.
- **L**: the full intraday set.

Later: all of H, the positioning vendors in G, social beyond Reddit and StockTwits, options analytics beyond implied volatility and put-call.

## Pitfalls this catalogue is designed around

- Use first-reported fundamentals and estimates with publication timestamps; restated history leaks the future.
- Earnings dates change; store the date as it was known at each point in time.
- Splits and dividends: adjust prices consistently and store the adjustment history.
- News timestamps: use the vendor's publication time, not our fetch time; drop articles whose publication time is missing.
- Survivorship: keep delisted and acquired names in historical universes.
- Many "daily" series are released with a lag (FINRA short interest, 13F holdings) and must be aligned to their release date, not their as-of date.
