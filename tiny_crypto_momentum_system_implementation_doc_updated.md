# Tiny-Crypto Momentum Event Trading System

## Purpose
This document defines the updated mechanics, architecture, implementation approach, and operating procedures for an automated trading system designed to detect, rank, and trade abnormal short-term momentum events in small-cap cryptocurrencies on Binance.

This version incorporates the earlier system design plus the later refinements discussed for:
- exchange-wide pump radar scanning
- multi-layer promotion from broad watch to deep inspection
- tiny-coin specific behavior
- more realistic venue and liquidity constraints
- separate early-build-up, ignition, and exhaustion logic
- stronger risk controls around slippage, false pumps, and overextension

This document is implementation-focused and intentionally excludes code samples.

---

## 1. System Objective

The objective is to build a real-time automated system that:

- monitors a broad but filtered Binance universe efficiently
- identifies tiny coins that are becoming abnormal within seconds
- distinguishes between early build-up, ignition, overextension, and exhaustion
- ranks symbols continuously and promotes only the most relevant candidates to deeper analysis
- enters trades only when both signal quality and execution quality are acceptable
- exits quickly when price, flow, or liquidity invalidates the thesis
- limits portfolio-level and symbol-level risk aggressively
- supports long entries during ignition and selective short entries during exhaustion where the venue permits shorting

The system should be built as an event-driven, multi-layer architecture that separates:
- exchange-wide radar scanning
- high-resolution symbol inspection
- feature computation
- scoring and ranking
- execution decisioning
- order management
- logging, analytics, and supervision

---

## 2. Strategy Philosophy

### 2.1 Core thesis
Tiny-coin moves are driven primarily by short-term changes in liquidity, order flow, and participation, not by slow lagging indicators.

The system should detect moments where:
- buying pressure is building
- sell-side resistance is thinning or being removed
- participation is accelerating abnormally
- spread and depth conditions remain tradeable
- price is still early enough for entry, or extended enough for controlled reversal logic

### 2.2 Separate horizons and roles
The strategy should be implemented as three related but distinct layers.

#### A. Early Radar Layer
Detects tiny coins that may become dangerous within roughly 30 seconds to 15 minutes.

#### B. Ignition Long Engine
Detects the exact moment a promising candidate transitions from build-up into tradable upside ignition.

#### C. Exhaustion Short Engine
Detects when a move is already extended, unstable, and beginning to fail, so that a controlled short may be possible only on venues that support it.

These layers must remain conceptually separate. The system should not treat all abnormal activity as a direct trade entry.

### 2.3 Operating principle
The core operating principle is:

**Detect many, inspect few, trade fewer.**

The system should scan broadly, promote selectively, and execute only where:
- the signal is strong
- liquidity is sufficient
- slippage is acceptable
- risk budget is available
- the setup is still valid at the moment of execution

---

## 3. High-Level System Architecture

The system should be organized into the following major components.

### 3.1 Universe Manager
Responsible for maintaining the monitored and tradeable symbol universe.

Responsibilities:
- fetch and refresh exchange metadata and symbol filters
- determine which symbols are tradeable on spot, margin, and futures
- maintain separate long-eligible and short-eligible lists
- apply liquidity and activity filters
- refresh the universe slowly enough to preserve useful rolling state

### 3.2 Exchange-Wide Radar Service
Responsible for cheap broad monitoring of the exchange.

Responsibilities:
- ingest lightweight exchange-wide streams where appropriate
- maintain fast rolling statistics for a large universe
- compute broad abnormality metrics such as short-horizon returns, volume bursts, spread changes, and quote activity
- rank and promote symbols into deeper watchlists

This layer should be optimized for breadth, not microstructure precision.

### 3.3 High-Resolution Market Data Service
Responsible for detailed symbol-level monitoring of shortlisted names.

Responsibilities:
- subscribe to per-symbol depth, aggregate trades, and best bid/ask updates for promoted symbols
- normalize incoming messages into internal event types
- maintain reliable reconnect and cleanup logic
- stamp and sequence messages consistently

### 3.4 State and Feature Engine
Responsible for maintaining live per-symbol state and calculating both lightweight and deep microstructure features.

Responsibilities:
- maintain rolling state for price, spread, volume, depth, and flow
- maintain order-book snapshots for promoted symbols
- compute features on a fixed cadence, ideally once per second for most logic
- expose current feature state to the scoring engine

### 3.5 Signal and Scoring Engine
Responsible for converting features into ranked states and trade candidates.

Responsibilities:
- compute separate radar, long, and short scores
- apply hard filters before scoring or execution
- manage symbol state transitions such as suspicious, danger watch, high-resolution monitor, trade candidate, and cooldown
- apply persistence and confirmation rules

### 3.6 Execution Engine
Responsible for deciding whether and how to place orders.

Responsibilities:
- validate venue permissions and symbol constraints
- enforce slippage-aware sizing and concurrency limits
- place, manage, and cancel orders
- manage live positions and exits
- avoid chasing late or unstable moves

### 3.7 Risk Engine
Responsible for symbol-level and portfolio-level protection.

Responsibilities:
- cap per-trade risk and portfolio risk
- limit notional exposure in weak-liquidity names
- enforce kill switches for drawdown, stale data, and repeated execution failures
- block trading when liquidity conditions degrade suddenly

### 3.8 Logging, Replay, and Monitoring
Responsible for observability and iterative improvement.

Responsibilities:
- log radar promotions, feature snapshots, watch states, entries, exits, and rejects
- support replay of abnormal events and trade decisions
- measure alert quality, slippage, and post-alert outcomes
- power operator dashboards and health alerts

---

## 4. Exchange and Venue Considerations

### 4.1 Spot, Margin, and Futures must be treated separately
The implementation must not assume that every Binance spot symbol can be shorted. The system must explicitly discover and maintain venue-specific tradability.

Maintain at minimum:
- monitored universe
- long-tradable universe
- short-tradable universe

### 4.2 Symbol metadata must be cached and validated
For every tradeable symbol, store:
- quantity precision
- price precision
- tick size
- step size
- minimum quantity
- minimum notional
- allowed order types
- venue permissions
- leverage eligibility where relevant

No order should be constructed without passing through a symbol validation layer.

### 4.3 Production constraints
The design must account for:
- WebSocket stream limits
- reconnect handling
- exchange-side disconnect patterns
- the fact that deep book monitoring should be reserved only for shortlisted symbols

---

## 5. Universe Design

### 5.1 Universe construction goals
The universe should be large enough to catch rare tiny-coin opportunities, but small enough to maintain stable monitoring and execution.

### 5.2 Universe categories
Maintain at least four sets:
- broad monitored universe
- radar-eligible universe
- long-eligible universe
- short-eligible universe

### 5.3 Filtering criteria
Use prefilters such as:
- quote volume floor over the last 24 hours
- maximum acceptable spread from recent data
- minimum top-of-book and top-N book depth
- minimum recent trade activity
- exclusion of stablecoin bases and leveraged token products

### 5.4 Refresh cadence
Universe refresh should be slow enough to preserve useful rolling state. A cadence such as every 5 to 10 minutes is generally preferable to very frequent full refreshes.

---

## 6. Data Feeds and Event Model

### 6.1 Broad radar feeds
For the broad scanner, ingest the lightest practical streams to monitor a large number of symbols efficiently.

The radar layer should maintain:
- short-horizon price movement
- short-horizon volume acceleration
- spread changes where available
- quote activity / update frequency where available

### 6.2 High-resolution symbol feeds
For promoted symbols, ingest:
- partial order-book depth stream
- aggregate trades stream
- best bid/ask stream
- liquidation-related futures signals where supported and relevant

### 6.3 Internal event normalization
All incoming market data should be normalized into event types such as:
- lightweight market stat update
- order book update
- aggregate trade
- best bid/ask update
- liquidation snapshot

Each event should include:
- symbol
- exchange event time where available
- local receive time
- parsed payload
- source stream identifier

### 6.4 Clock handling
The system should maintain both:
- exchange event time
- local processing time

This supports latency diagnostics and more reliable sequencing.

---

## 7. State Model and Lifecycle

### 7.1 Per-symbol state
Each symbol should maintain a live state object containing:
- current eligibility and venue flags
- current lifecycle state
- rolling price history
- rolling spread history
- rolling volume history
- rolling flow history
- order-book state if in deep-monitor mode
- feature values
- watch metadata
- cooldown metadata

### 7.2 Symbol lifecycle states
Symbols should move through explicit states such as:
- Normal
- Suspicious
- Danger Watch
- High-Resolution Monitor
- Long Watch
- Short Watch
- Order Pending
- Live Position
- Cooldown

This state model is central to avoiding noisy repeated triggers.

### 7.3 Warmup handling
A symbol should not be scored by a layer until the relevant rolling windows have enough history.

---

## 8. Feature Framework

The feature framework should be split into two groups: radar features and deep microstructure features.

### 8.1 Radar-layer features
These are cheap, broad, and used to rank many symbols.

#### 8.1.1 Short-horizon return and acceleration
Measure 10-second, 30-second, and 60-second returns, plus changes in velocity.

Purpose:
- identify symbols becoming abnormal quickly

#### 8.1.2 Volume burst vs baseline
Compare recent notional volume against the symbol’s own baseline.

Purpose:
- detect abnormal participation before or during build-up

#### 8.1.3 Spread compression or expansion
Track spread relative to the symbol’s recent baseline.

Purpose:
- identify whether a name is becoming easier to move or more unstable

#### 8.1.4 Quote/update activity
Track how frequently the symbol is updating.

Purpose:
- identify unusual market activity even before price clearly moves

#### 8.1.5 Early build-up score
Combine flat-to-small price range with increasing participation and improving tradability.

Purpose:
- identify coins that may pump in the next several minutes rather than the next several seconds

### 8.2 Deep microstructure features
These are used only for shortlisted names.

#### 8.2.1 Order Book Imbalance
Measure buy-side notional vs sell-side notional over the top levels of the book.

Purpose:
- estimate directional pressure

Implementation guidance:
- compute using notional rather than raw quantity
- standardize with rolling baselines

#### 8.2.2 Flow Imbalance
Measure aggressive buy vs sell flow over a short rolling horizon.

Purpose:
- confirm whether participants are actively lifting offers or hitting bids

Implementation guidance:
- aggregate over a small rolling horizon such as 3 to 5 seconds
- use a bounded imbalance metric

#### 8.2.3 Liquidity Vacuum / Ask Depletion
Measure how much ask-side liquidity exists just above current price relative to a baseline.

Purpose:
- identify when resistance is unusually weak

Implementation guidance:
- ensure the chosen depth resolution actually covers the measurement band
- use a consistent sign convention so thinner asks strengthen bullish interpretation

#### 8.2.4 Rehydration Rate
Measure how quickly ask liquidity reappears after being depleted.

Purpose:
- distinguish true vacuum from hidden or persistent resistance

#### 8.2.5 Spread Stability
Track whether spread remains controlled or becomes unstable during a candidate setup.

Purpose:
- avoid entries into unstable books

#### 8.2.6 Volume Acceleration
Compare immediate recent notional volume against a recent baseline.

Purpose:
- confirm that participation is actually increasing

#### 8.2.7 Micro Return / Extension
Measure short-horizon price movement.

Purpose:
- keep long entries early
- require sufficient prior extension for short entries

#### 8.2.8 Book Depth Quality
Measure top-of-book and top-N notional depth.

Purpose:
- reject symbols whose apparent momentum is not tradeable

#### 8.2.9 Optional advanced features
Future versions may add:
- ask-pull delta over recent seconds
- bid-stack delta over recent seconds
- liquidity migration toward price
- trade-size distribution shift
- cluster momentum across similar symbols
- futures liquidation assist for larger or futures-listed names

---

## 9. Radar Layer Mechanics

### 9.1 Objective
The radar layer exists to scan broadly and cheaply, then promote only the most abnormal symbols into deeper monitoring.

### 9.2 Radar score
The radar score should combine lightweight features such as:
- return acceleration
- volume burst vs baseline
- spread compression or instability
- quote activity surge
- early build-up characteristics

### 9.3 Radar outputs
Each symbol should be labeled as one of:
- building pressure
- ignition risk increasing
- already extended
- illiquid trap
- cooling off

### 9.4 Promotion rules
Only the top slice of symbols should be promoted into high-resolution monitoring. Promotion should require both abnormality and basic tradability.

### 9.5 Cooldown rules
Symbols recently promoted and invalidated should not immediately re-promote unless their state materially changes.

---

## 10. Early Build-Up Detection Layer

### 10.1 Objective
Detect names that may pump within roughly 5 to 15 minutes by identifying accumulation or preparation behavior.

### 10.2 Characteristic pattern
This layer should look for conditions such as:
- price remaining in a relatively tight recent range
- volume increasing against that flat range
- spread becoming more favorable
- activity rising without full ignition yet

### 10.3 Suggested build-up signals
Examples include:
- price-volume divergence
- spread compression from baseline
- volume distribution shift toward larger prints
- gradual liquidity improvement or migration closer to price
- cluster activity among related tiny-cap names

### 10.4 Output
This layer should not place trades directly. It should produce a ranked pre-pump watchlist for deeper monitoring.

---

## 11. Ignition Long Engine Mechanics

### 11.1 Objective
Enter long positions during early ignition phases before the move becomes too crowded or overextended.

### 11.2 Hard filters before scoring
A symbol must pass minimum checks such as:
- acceptable spread
- acceptable top-N depth
- valid recent trade activity
- warmed-up windows
- not already too extended

### 11.3 Composite long score
The long score should combine:
- order-book imbalance
- flow imbalance
- ask depletion / vacuum
- rehydration behavior
- volume acceleration
- spread stability

### 11.4 Persistence filter
A single spike should not be enough. The score should exceed threshold across multiple recent evaluations.

### 11.5 Watch state
When score is high enough, move the symbol into a long watch state instead of entering immediately.

The watch state should store:
- watch start time
- price at watch start
- high and low since watch start
- confirmation counter
- reason for watch

### 11.6 Confirmation logic
A long should only be entered when one or more of the following occurs while quality remains high:
- price breaks a recent local high
- aggressive buy flow remains strong for multiple ticks
- ask depletion persists and spread remains controlled

### 11.7 Entry validation
At the moment of entry, re-check:
- score still valid
- depth still sufficient
- spread still acceptable
- expected slippage acceptable
- portfolio risk budget available
- price not too far beyond trigger

### 11.8 Long invalidation conditions before entry
Cancel the watch if:
- score collapses
- ask liquidity refills strongly
- flow flips negative
- price stalls too long
- spread widens abnormally

---

## 12. Exhaustion Short Engine Mechanics

### 12.1 Objective
Enter short positions only after a move is clearly extended and beginning to fail.

### 12.2 Eligibility
A symbol must be confirmed short-tradeable on the selected venue.

### 12.3 Precondition: extension must already exist
The short engine should activate only after:
- a substantial recent upside move
- extreme short-term participation
- signs of instability or blow-off behavior

### 12.4 Composite short score
The short score may combine:
- weakening buy flow after extension
- widening spread
- refilling asks above price
- thinning bids below price
- failure to hold highs
- local low break confirmation

### 12.5 Watch state and confirmation
The short watch should track:
- watch start time
- high since watch start
- low since watch start
- reversal confirmation count

### 12.6 Entry rules
Only short when:
- extension threshold was met
- reversal evidence persists
- price confirms by breaking a local low or failing a reclaim
- execution quality is still acceptable

### 12.7 Short invalidation conditions before entry
Cancel the short watch if:
- buy flow re-accelerates
- failed highs are reclaimed
- spread becomes too unstable
- venue conditions are no longer acceptable

---

## 13. Execution Design

### 13.1 Execution principles
Execution must prioritize:
- controlled entry
- strict symbol-filter compliance
- low-impact sizing
- rapid cancellation when invalidated
- immediate protection after fills

### 13.2 Slippage-first sizing
Position size should be capped by liquidity, not just conviction.

The execution engine should estimate the notional available in the first few levels of the book and avoid consuming too much visible liquidity.

### 13.3 Entry style
The engine should support:
- marketable limit orders for thin books
- staged entry where the first clip is small
- optional clipping for larger desired notionals
- cancellation if the entry does not fill quickly enough

### 13.4 Flash invalidation handling
If a symbol’s immediate book support collapses abruptly, the engine should support fast defensive exit logic regardless of whether the original stop has been reached.

### 13.5 Fill handling
After a fill, the engine should:
- record actual average entry price
- place protective exit logic if supported by the venue
- transition position state from pending to live

---

## 14. Exit Logic

### 14.1 Exit philosophy
Tiny-coin trades are fragile. Good trades usually work quickly. Slow or conflicting trades should be closed quickly.

### 14.2 Hard stop
Every position needs a protective stop defined at entry.

### 14.3 Flow invalidation exit
Exit immediately if microstructure invalidates the thesis.

Examples:
- long trade: flow turns negative, asks refill, support vanishes
- short trade: flow turns positive again, bids refill, reclaim begins

### 14.4 Time stop
Exit if the trade does not develop within a short predefined time budget.

### 14.5 Profit capture
Use either:
- partial scaling out into favorable movement
- or a tight trailing stop once the trade is sufficiently favorable

### 14.6 Position state machine
Each trade should move through states such as:
- candidate
- watch
- confirmed
- order_pending
- live_position
- reducing
- exited
- cancelled

All state transitions should be logged.

---

## 15. Risk Management

### 15.1 Per-trade risk
Define a fixed maximum risk per trade as a fraction of capital or daily risk budget.

### 15.2 Position sizing
Size by the lesser of:
- risk budget based on stop distance
- maximum notional allowed for that symbol
- maximum slippage-acceptable size based on available depth

### 15.3 Concurrency limits
Cap:
- total simultaneous positions
- total simultaneous tiny-coin exposure
- simultaneous long and short counts
- cluster-level exposure where symbols behave similarly

### 15.4 Portfolio-level guardrails
Add kill switches for:
- daily drawdown threshold
- consecutive loss threshold
- stale or degraded data feeds
- repeated order rejects
- loss of synchronization or venue health concerns

### 15.5 Venue and symbol risk exceptions
Block trading on symbols that show:
- excessive slippage
- unstable spreads
- repeated abnormal disconnect effects
- weak book quality

---

## 16. Backtesting, Replay, and Validation

### 16.1 Why replay matters
Tiny-coin strategies depend on event ordering, depth context, and slippage. Candle-only backtests are insufficient.

### 16.2 Required stored data
Persist enough data to replay:
- radar promotions
- order-book state for deep-monitor names
- aggregate trades
- feature snapshots
- score values
- entry and exit decisions
- execution outcomes

### 16.3 Replay engine goals
The replay system should allow:
- re-running abnormal periods with different thresholds
- analyzing false positives and false negatives
- comparing expected fills vs realistic fills
- studying how early the radar promoted symbols before ignition or exhaustion

### 16.4 Validation metrics
Track both signal metrics and trading metrics.

Signal metrics:
- promotions per hour
- confirmation rate
- percentage of promoted names that move materially in the next 30s, 60s, and 5m
- false-promotion rate

Trading metrics:
- win rate
- average win and average loss
- profit factor
- max drawdown
- average hold time
- slippage per fill

---

## 17. Operational Monitoring

### 17.1 Real-time dashboard
A live dashboard should show:
- top radar symbols
- current radar, long, and short scores
- lifecycle state per symbol
- watchlists and cooldowns
- live positions
- spread and depth conditions
- feed health and latency
- risk status and kill switches

### 17.2 Operator alerts
Generate alerts for:
- reconnect loops
- stale data
- clock drift
- order reject spikes
- kill-switch activation
- excessive promotion volume indicating noisy thresholds

---

## 18. Implementation Roadmap

### Phase 1: Market data and radar foundation
Build:
- exchange metadata loader
- universe manager
- broad radar ingest
- normalized event model
- lightweight rolling state
- basic abnormality ranking

Deliverable:
- stable exchange-wide radar with promotion logs only

### Phase 2: High-resolution monitoring
Build:
- promoted-symbol stream manager
- per-symbol order-book state
- deep feature engine
- lifecycle state manager

Deliverable:
- promoted symbols can be inspected in detail with full feature snapshots

### Phase 3: Long engine and paper execution
Build:
- long score
- long watch and confirmation logic
- paper execution simulator
- slippage estimation
- entry/exit logging

Deliverable:
- paper-traded ignition long engine with replay support

### Phase 4: Risk engine and live execution foundation
Build:
- symbol validation layer
- order manager
- live fill handling
- protective exits
- portfolio limits and kill switches

Deliverable:
- low-size live trading capability with strict controls

### Phase 5: Exhaustion short engine
Build:
- extension logic
- reversal score
- short watch and confirmation rules
- venue-specific short execution logic

Deliverable:
- controlled short engine only for eligible venues and symbols

### Phase 6: Advanced refinements
Add:
- build-up detection improvements
- ask-pull and bid-stack delta features
- cluster momentum overlays
- richer liquidity quality metrics
- optional ML ranking layer on top of rule-based candidates

Deliverable:
- higher-quality promotion and reduced false positives

---

## 19. Recommended Initial Build Choices

For the first production-oriented implementation, use:
- rule-based scoring before ML
- radar layer first
- long engine before short engine
- limited promoted-symbol count
- conservative position sizing
- strong replay and logging from day one

This sequencing reduces the risk of building a complex but untrustworthy system.

---

## 20. Common Failure Modes

Be prepared for the following:
- false vacuum signals due to insufficient book depth
- overtriggering on low-trade symbols
- slippage invalidating apparently good setups
- frequent reconnects resetting rolling state too often
- treating radar promotions as direct trade signals
- attempting shorts where venue support or execution quality is weak
- excessive concurrency in correlated tiny coins

Each of these should be explicitly tested and monitored.

---

## 21. Final Design Principle

The final design principle remains:

**Detect many, inspect few, trade fewer.**

A good system should watch broadly, promote intelligently, inspect deeply, and only act when:
- the signal is strong
- liquidity is sufficient
- slippage is acceptable
- risk is controlled
- the setup is still early enough for the intended strategy

That principle is more important than any single feature or score.

