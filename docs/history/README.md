# Development log

These documents are a chronological record of how the system was built and
reviewed. They are kept for reference and are **not** maintained alongside the
code — they describe the repository as it stood on the date each was written,
so treat any code they quote as a snapshot rather than as current behaviour.

The living specification lives in [`docs/system-design.md`](../system-design.md).

| Document | Written | What it is |
| --- | --- | --- |
| [walkthrough.md](walkthrough.md) | 2026-03 | Summary of what had been built at the point the dashboard landed: module layout, dashboard tabs, minimum-quantity mode, how to run it. |
| [dashboard-implementation-plan.md](dashboard-implementation-plan.md) | 2026-03 | The plan for the aiohttp dashboard, listing the endpoints and files it would add. All of it was implemented. |
| [pump-radar-code-review.md](pump-radar-code-review.md) | 2026-03 | Review written after a session where the radar promoted many symbols but executed zero trades. Diagnoses the promotion-to-execution funnel and proposes the loosened thresholds that became the `fast_ignition_*` settings in `config.yaml`. |
| [core-logic-review.md](core-logic-review.md) | 2026-03-07 | A module-by-module annotated dump of the core logic (config, database, radar, monitor, signals, execution, exit, risk, loggers, main). Useful as a guided tour; the pasted source is a snapshot and has since drifted. |
