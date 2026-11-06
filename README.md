# THE SLEEPING CAMPUS — Occupancy-Aware Energy Intelligence (PS6)
Zero third-party dependencies: Python 3.8+ standard library only (http.server + sqlite3). Works offline.
All data is SYNTHETIC and SIMULATED. Devices/control endpoints are simulated; no real infrastructure is touched.

## Commands (macOS)
- Python 3: `python3 --version` (if missing: `xcode-select --install`, or install from python.org)
- Setup: `cd THE_SLEEPING_CAMPUS` (nothing to install)
- Start: `./run.sh`  (or `python3 server.py`) then open http://127.0.0.1:8000
- Test: `python3 -m unittest discover -s tests -v`
- Demo reset: `python3 server.py --reset` (or the "Reset demo" button)

## 60-90 s judge script
1. Digital Twin → Active 11:00 → click **Classroom A204** (red): no class, 0 occupants, AC still ~2.9 kW vs 0.6 kW baseline; read the reason, evidence, ₹ estimate (tariff assumption shown).
2. Click **A301**: scheduled but low attendance → "request review", NOT shutdown. Click **A205**: cancelled class. Click **Server Room**: protected.
3. Role = facility_manager → **Approve shutdown** on A204 (try it on Server Room: refused).
4. Concurrency Lab → *Duplicate attack*: 10 attempts → 1 success, 9 idempotent duplicates, 1 transition, 1 savings event.
5. *Conflicting administrators* → one wins, the other is rejected (stale version). *Stale-version attack* → v1 request rejected after v2.
6. Privacy Centre → live verification checks. 7. Forecast & Savings → estimated vs measured(simulated). 8. Audit → full history incl. rejections.
Phase buttons (Morning/Active/Evening/Night) re-drive occupancy, schedule, power and the forecast.

## Architecture
Browser ⇄ `server.py` (127.0.0.1) ⇄ SQLite. Business logic is all in the backend: schedule-aware model, baseline-ratio anomaly rules,
profile+temperature forecast (rules, NOT machine learning), recommendation engine, command pipeline.
Command pipeline (single transaction, `BEGIN IMMEDIATE` + process lock): idempotency lookup → role check → critical-equipment guard →
optimistic version check → limit/transition validation → apply (+version bump, savings row) → ops + audit rows.
**Conflict policy:** first-committed-wins by device version; later requests on an old version are rejected as stale and audited.
Protected equipment can never be shut down, whatever the role. Savings rows are keyed by idempotency key (counted once).
Estimated savings = predicted; "measured" = simulated post-action reading (labelled simulated; not real-world savings).

## Privacy
Only aggregate per-room occupant counts are stored. No names, IDs, MACs or individual attendance exist in the schema. Audit stores demo roles only.
HTTP access logging is disabled. The Privacy Centre runs heuristic checks (external URLs, AI/analytics SDK names, PII regexes, schema columns).
This is not a certification or a privacy guarantee.

## Known limitations
- Single-machine concurrency (process lock + SQLite). Multi-server deployment needs a shared DB with row-level locking/compare-and-swap.
- Approval is one-step: the role performing the action is the approver (roles are demo-only, no authentication). No separate "request review" queue or configurable multi-approver chain yet.
- Timetable/events are encoded in the synthetic model (not a separate editable calendar table/UI); occupancy is a deterministic simulation.
- Fewer than the 14 listed scenarios are separately packaged; most appear via the phase buttons and fixed rooms (A204, A205, A301, L2-L4, H1, infra).
- The twin is a 2D building/room grid, not isometric. Forecast is a transparent profile model, not a trained model.


## v3 additions
Live shared data stream (/api/live) drives every tab. Sleep Score, autopilot proposal queue (proposes, never executes), scenario injector, savings that accrue over simulated time (estimated vs measured-simulated), live browser network audit. Approving a proposal uses a deterministic idempotency key and the device version captured when it was proposed, so double-clicks and stale proposals are handled by the backend.
