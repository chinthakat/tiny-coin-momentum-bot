# Pump Radar Trading System -- Code Review Findings

## Summary

The system shortlisted and promoted many coins but executed **zero
trades**.\
This review analyzes the most likely causes and provides concrete
improvements.

Overall assessment: - Architecture: strong - Radar detection:
functioning - Promotion pipeline: functioning - Conversion to trades:
failing

The issue is most likely in the transition: promotion → deep monitoring
→ watch → confirmation → execution.

------------------------------------------------------------------------

# Main Reasons No Trades Occurred

## 1. Deep Feature Warmup Too Long

Promoted symbols must wait for deep features to warm up before
evaluation.

Current config: min_warmup_seconds = 30

This is too long for tiny‑coin ignition moves where the entire
opportunity may last less than 30 seconds.

### Recommendation

Reduce warmup to:

5--10 seconds for promoted symbols.

------------------------------------------------------------------------

## 2. Long Engine Funnel Too Strict

A promoted symbol must pass many stages:

1.  Hard filters
2.  Score threshold
3.  Persistence filter
4.  Watch state
5.  Confirmation logic
6.  Execution validation
7.  Risk approval

Even if radar correctly detects the coin, any single stage failing will
prevent entry.

------------------------------------------------------------------------

## 3. Confirmation Logic May Suppress Valid Signals

Long confirmation logic requires multiple conditions.

If breakout reference levels or timing logic are incorrect, watches may
expire before confirmation occurs.

Recommendation: Require confirmation across **multiple ticks**, but not
overly strict persistence.

------------------------------------------------------------------------

## 4. Weak Deep Feature Signals

Deep features depend on:

-   order book imbalance
-   flow imbalance
-   ask depletion
-   rehydration
-   volume acceleration

Earlier code versions used synthetic baselines for ask depletion and did
not compute rehydration correctly.

Recommendation: Use historical baselines from rolling windows.

------------------------------------------------------------------------

## 5. Execution Filters Too Conservative

Current config includes:

max_spread_pct = 1.5 min_top_book_notional_usdt = 100 max_slippage_pct =
0.5

During tiny‑coin ignition phases spreads and depth change rapidly.

Execution filters may reject most candidates before entry.

------------------------------------------------------------------------

# Key Debugging Tasks

## Task 1 -- Log Promotion to Deep Monitoring Pipeline

For each promoted symbol log:

-   promotion time
-   deep stream attach time
-   first order book message
-   first trade message
-   deep warmup complete time
-   first deep feature computation

Goal: verify deep monitoring becomes usable quickly enough.

------------------------------------------------------------------------

## Task 2 -- Reduce Warmup Time

Change configuration:

min_warmup_seconds: 5--10

This allows deep feature evaluation sooner.

------------------------------------------------------------------------

## Task 3 -- Add Detailed Rejection Logging

For every promoted symbol log one of:

-   rejected_not_warmed_up
-   rejected_hard_filters
-   rejected_score_below_threshold
-   rejected_persistence
-   entered_watch
-   watch_invalidated_flow
-   watch_invalidated_spread
-   watch_timeout
-   watch_confirmed

This reveals exactly where the signal pipeline fails.

------------------------------------------------------------------------

## Task 4 -- Relax Confirmation for Fast Ignition Signals

Fast ignition signals should require lighter confirmation than slow
buildup signals.

Implement separate confirmation logic for:

-   buildup promotions
-   fast ignition promotions

------------------------------------------------------------------------

## Task 5 -- Improve Ask Depletion and Rehydration

Use rolling baselines:

ask_depletion = 1 − current_ask_band / median_ask_band

rehydration_rate = new_asks_added / asks_removed

Low rehydration → genuine vacuum\
High rehydration → hidden resistance

------------------------------------------------------------------------

## Task 6 -- Validate Deep Feature Engine Activation

Ensure DeepFeatureEngine.compute() is executed only after:

-   depth stream active
-   trade stream active
-   minimum samples collected

------------------------------------------------------------------------

## Task 7 -- Build Promotion → Entry Funnel Metrics

After each run report:

-   symbols promoted
-   deep monitoring attached
-   deep warmup completed
-   passed hard filters
-   entered watch
-   confirmed signals
-   execution rejected
-   trades executed

This quickly identifies where the system blocks trades.

------------------------------------------------------------------------

# Most Likely Root Cause

The system successfully detects candidate coins but fails to convert
them into trades because:

-   deep monitoring attaches too late
-   warmup delay blocks evaluation
-   confirmation logic too strict
-   execution filters reject remaining cases

------------------------------------------------------------------------

# Recommended Immediate Fix Order

1.  Reduce deep warmup time
2.  Log full promotion → entry pipeline
3.  Validate deep features are being computed
4.  Relax confirmation rules slightly
5.  Adjust execution filters for tiny coins
6.  Implement funnel metrics reporting

------------------------------------------------------------------------

# Final Assessment

The architecture is sound.

The problem is not signal detection but **signal conversion into
trades**.

Once the warmup, confirmation, and execution thresholds are tuned, the
system should begin producing entries.
