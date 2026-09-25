# advancedTradingAdvisor

Phase 10: local Python 3.13 spot swing advisor with explicit protected core, reserve v2,
1D/4H structure, closed-1H trigger confirmation, persistent trade setups, ranked capital
deployment, bounded OpenAI reasoning, whole-plan validation, immutable history, foreground UTC
4H scheduling and outbound Telegram.
Recommendation-only: no trades are executed and advice never changes confirmed balances.

Install: `python3.13 -m venv .venv`, activate it, then `python -m pip install -e '.[dev]'`.
Run `advisor --help` or `python -m advanced_trading_advisor --help`.
Verify with `python -m pytest`, `ruff check .`, and `ruff format --check .`.
The same credential-free offline checks run on Ubuntu with Python 3.13 for pushes and pull requests;
CI never invokes live market, OpenAI, Telegram, analysis, or scheduler commands.

## Private HTTP portfolio and analysis API (Steps 8B–8D.4)

The ASGI adapter exposes authenticated `GET /v1/portfolio`, `GET /v1/analyses/latest`,
`GET /v1/analyses?limit=N`, and `GET /v1/analyses/{run_id}`. The portfolio route reads the current
confirmed snapshot through `ConfirmedStateService`; it never initializes storage, changes
portfolio state, or starts analysis or scheduling. Account holdings are authoritative, and
`aggregate_positions` is derived for display. All financial decimals are JSON strings.
Analysis routes use `AnalysisHistoryQuery` and preserve each run's historical snapshot ID;
completed details include the decoded analysis and active setups. All four routes are read-only.

`POST /v1/portfolio/accounts` is the first authenticated mutation. Its JSON body contains
`expected_revision`, `name`, `account_type` (`binance`, `external_wallet`, `other_exchange`, or
`manual`), and `positions` entries with a canonical `symbol` and decimal-string `amount`.
The server generates `account_id`; clients cannot provide it. A successful `201` response is the
complete updated portfolio, including its new revision and snapshot ID. A `409 revision_conflict`
means reload authoritative state and review the outcome, not automatically retry the mutation.
There is no HTTP idempotency key yet. Initial confirmed state must be bootstrapped separately.

`PATCH /v1/portfolio/accounts/{account_id}` renames an existing account; its body requires
`expected_revision` and `name`. `PUT /v1/portfolio/accounts/{account_id}/holdings` requires
`expected_revision` and a complete `positions` array with decimal-string amounts. PUT replaces
that account's holdings entirely, not as a delta or partial patch. Both routes use the account UUID
in the path and return the complete updated portfolio (`200`). A `409 revision_conflict` on either
means reload and review, never automatically retry the same mutation.

Authenticated `DELETE /v1/portfolio/accounts/{account_id}` takes a JSON body containing only
`expected_revision` and returns the complete updated portfolio (`200`). It cannot remove the final
account or an account referenced by a retained order; no order is deleted or changed as a side
effect. A `409 revision_conflict` requires reload and review, not automatic retry.

Install the project dependencies, then configure `ADVISOR_API_TOKEN`,
`ADVISOR_STORAGE_BACKEND=postgres`, and `ADVISOR_DATABASE_URL` in the HTTP process's private
environment. The token must be nonblank and contain no whitespace. The HTTP factory does not
load the project `.env`, and it refuses non-PostgreSQL production storage or a missing token.
Do not place the token in source control or a command-line argument. Start the ASGI server with:

```sh
.venv/bin/uvicorn advanced_trading_advisor.http.app:create_app --factory --host 127.0.0.1
```

Use a trusted HTTPS termination layer before exposing this private endpoint remotely. No cloud
deployment or public HTTP binding is configured by this step.

All financial inputs are finite Decimal values. Positions, expenses, runway, prices and quantities
must be strictly positive; omit zero-balance positions. An empty portfolio is allowed.
CLI parsing helpers accept plain period-separated decimal text and never round input.
Arithmetic uses a fixed 38-digit local context; computed indicator results may be rounded.

Confirmed snapshots and nested models are immutable. Only explicit user-confirmed operations
may produce a new state. Independent analysis run IDs allow repeated analysis of one snapshot.
RUNNING has no completion timestamp; all terminal statuses require one.

Recommendation models enforce structure; `PlanValidator` separately checks the whole plan's
capital, inventory, order references, duplicates and measurable allocation limits.
KEEP/CANCEL reference an order ID and inherit its terms; REPLACE carries full new terms.
New plans use `REBALANCE_INFORMATIONAL` with no trade terms. Historical `REBALANCE` remains
decodable only for old reports. Invalidation text is commentary, not an executable rule.

## Stateful swing setups (Phase 10)

Phase 10 extends the Phase 9 `TradeSetup`; it does not introduce a second signal model. A setup's
single lifecycle now includes deterministic trigger context and an append-only transition history.
The pipeline is closed 1H/4H candles and derived 1D context → deterministic structure candidate →
`SwingTriggerEngine` → persisted setup state → Phase 9 portfolio allocation → optional bounded AI
wording/selection → `ActivePlanValidator` v3. The trigger engine is a pure replay-friendly function:
it has no network, wall clock, portfolio mutation, AI, Telegram or execution dependency.

The initial temporal families are `SUPPORT_HOLD` for BUY candidates and
`RESISTANCE_REJECTION` for SELL candidates. Historical Phase 9 `PULLBACK_BUY` and
`PROFIT_TAKING_SELL` setups remain readable. A setup can progress through `CANDIDATE`,
`APPROACHING_ZONE`, `ZONE_TOUCHED`, `AWAITING_CONFIRMATION` and `CONFIRMED`; terminal outcomes
include `INVALIDATED`, `EXPIRED`, manual fill confirmation and manual cancellation. Every
transition stores the previous/current state, closed-candle cutoff, timeframe, reason and
reference price. Confirmation additionally stores the exact closed 1H OHLC candle, while the
context retains its ATR-derived zone, touch time, excursion, trigger reference and expiry.

Touch alone never creates a trade. A subsequent closed 1H candle must move away from the zone by
the configured 0.25 ATR confirmation threshold with a directionally consistent candle body. A
closed 4H move through the setup's ATR-derived invalidation level overrides prior trigger state.
A confirmed setup expires after four closed 1H candles or after price moves more than one creation
ATR away from its trigger reference, so the allocator does not chase missed entries. Approach,
touch, confirmation, expiry and invalidation parameters are immutable, versioned policy values;
their canonical serialization is included in decision configuration and its report fingerprint.
Insufficient 1H evidence leaves a setup non-actionable and explicit rather than fabricating a
confirmation.

Repeated candles at or before `last_processed_1h_candle_at` are ignored. Equivalent structure
retains the same deterministic setup UUID, terminal states cannot move backward, and the complete
context is stored atomically inside the successful immutable analysis result (schema v5). A new
run reloads the latest completed result before evaluation, so state survives process restarts.
There is no new SQLite table or schema migration; failed analyses cannot partially advance setup
state. Crossing a recommended limit in market data still does not imply a fill—only the user's
confirmed order state can record that outcome.

Temporal confirmation is one explicit 15-point component of active policy v2 scoring: candidate
0%, approaching 25%, touched/awaiting 50%, confirmed 100%. Only execution-valid `CONFIRMED`
setups can enter ranking or receive capital. The Phase 9 allocator remains the sole authority for
reserve, dry powder, concentration, competing opportunities, inventory and core constraints; the
trigger engine never sizes a final order.

Inspect stored state without market or AI calls:

```sh
advisor setups
advisor setups 00000000-0000-0000-0000-000000000000
advisor history latest
```

The optional UUID selects one exact persisted setup. Replace the example UUID with an ID shown by
`advisor setups`.

## Active management foundation (Phase 9)

The objective is long-term portfolio value, not trade frequency. Each cycle may validly choose
NO TRADE. Only spot limit-order recommendations are emitted; execution and fill confirmation remain
manual. The engine now separates each asset's decision (ADD, HOLD, WAIT, PARTIAL_SELL or
REBALANCE_INFORMATIONAL), the executable limit-order terms, and the portfolio-wide decision about
which ranked setups receive scarce cash. No coordinator, broker or exchange execution is present.

Confirm the current ETH core preference explicitly—this does not change balances or place orders:

```sh
advisor portfolio core --asset ETH --floor 2 --preferred 2.5
```

The command displays the proposed immutable snapshot and requires typing `yes`. Its audit entry
is `core_configuration_change`. Other assets have **no invented core allocation**. The hard 2 ETH
floor blocks tactical sells that would cross it; ordinary profit-taking sizes from holdings
above the preferred 2.5 ETH, and excess ETH is never sold solely because it exists. A later
portfolio replacement preserves core settings and fails if the new confirmed holdings would be
below the hard floor. Change the core setting explicitly first if necessary.

Reserve v2 uses a hard expense floor (`monthly expenses × target runway`) plus a versioned market
buffer applied **to that floor**, never to total portfolio value. Default buffer fractions are
RISK_ON 0%, CONSTRUCTIVE 5%, NEUTRAL 10%, ELEVATED_RISK 20%, RISK_OFF 35%. The default remains
12 months for new CLI setups; configure 6 months explicitly where appropriate. For $220/month,
6 months and $2,983 of stables in NEUTRAL, the hard floor is $1,320, buffer $132, recommended
reserve $1,452 and potential trading liquidity $1,531 before any OPEN BUY commitment or stable
SELL reservation. Those values are always calculated from the confirmed snapshot. Potential
liquidity is **not** a buy instruction. The versioned portfolio allocator uses at most 25%/20%/
15%/5%/0% of it by regime, splits available budget among ranked eligible setups, and may deploy
$0. The rest is strategy dry powder; existing BUY commitments stay reserved separately.

CoinGecko Demo supplies closed 4H OHLC candles and a shorter trigger window. Short-window
30-minute OHLC rows are validated and combined in consecutive pairs into closed UTC 1H candles;
native valid hourly rows are accepted too. Missing/gapped/future/stale trigger rows fail or
degrade explicitly according to the market policy and are never fabricated. Six consecutive 4H
bars ending at 00:00 UTC form one completed 1D bar. The 1D context uses only those completed days
for EMA20, EMA50 when available, and major support/resistance. With the Demo 30-day 4H window,
daily EMA50 is normally
**unavailable and displayed as such**; it is never fabricated. 1D trend can veto a 4H bullish
regime upgrade (market regime policy v2 when daily evidence exists). 4H RSI, EMA20/50, ATR and
support/resistance remain the tactical execution source. There is no indicator zoo or news feed.

Historical active setup policy v1 created deterministic pullback BUY and profit-taking SELL
candidates from closed-candle support/resistance, EMA and ATR. Every stored level has a source;
no model-created price is accepted. BUY risk/reward compares resistance upside with distance to a support/ATR
invalidation. For profit-taking SELL, the displayed ratio compares downside to support with
remaining upside to resistance; it is an opportunity heuristic, not a short-position return
forecast. The configurable minimum is 1.5, with configurable fee (0.1%) and slippage (0.1%)
assumptions. Scores have versioned, persisted component weights and thresholds: 80+ strong,
65–79 actionable, 50–64 monitor, below 50 WAIT. These are explicit unoptimized heuristics.

TradeSetup identity, status, originating cutoff, score decomposition, limit/invalidation/target,
R/R, capital ceiling, trigger state/history and related OPEN order IDs are persisted inside each
immutable v5 analysis result. Equivalent levels retain identity across 4H cycles. Material structural changes retire
the old setup; confirmed FILLED/CANCELLED orders alone can transition its status accordingly.
The engine never infers fills or changes balances. OPEN orders default to KEEP. A material level
shift (at least 2% **and** one 4H ATR) can justify REPLACE if the full old and new commitments fit;
reserve/core/structural invalidation can justify CANCEL. A strong near-support BUY setup may
produce up to three ATR-derived limit levels; sufficiently large profit-taking setups may use up
to three resistance/ATR levels. Otherwise only one level is proposed. Each level records its
source and priority; combined quantities share one validated budget or inventory ceiling.

`ActivePlanValidator` v3 validates the full candidate plan: existing and new BUY commitments,
shared deployment ceiling and dry powder, projected concentration, SELL inventory and core
floor, exact OPEN order IDs, replacements counted alongside old orders, duplicate/conflicting
actions and candidate prices. It rejects non-limit discretionary trades and any price absent
from execution-valid deterministic setups, including the exact setup UUID. The AI receives compact
setup/allowed-action context and may
only return the same ordered, validated trade terms; untrusted prose is discarded. It cannot
invent or reprice orders. A rejected candidate falls back to the deterministic plan. This is a
deliberately narrow AI role; richer selection/explanation is deferred until it can be validated.

Telegram severity is deterministic: QUIET for no trade/KEEP/informational-only, bounded UPDATE for
a useful waiting/expired trigger or material regime/risk-warning change, and ACTION for a concrete
validated order change, cancellation need or reserve deficit. Every severity still saves the
**full** analysis. `advisor history latest` renders the
persisted capital split, position core/tradable exposure, 1D context, ranked setup details,
actions, warnings and severity without fetching fresh market data. Historical v1–v4 results and
v1 snapshots remain readable; no stored row is rewritten.

Technical calculations were selectively adapted from CryptoPortfolioAdvisor:
backend/app/services/technical_features.py, backend/app/services/market_data.py,
backend/app/infrastructure/static_market_data.py and associated indicator tests.
No runtime imports from that project exist. Formulas are preserved; Candle/model validation
and a central numeric context are added. Static unknown symbols now fail explicitly.
The calculator requires normalized, ordered, valid, closed 4H candles. It verifies spacing;
the live adapter establishes closure against an injected UTC clock before calculation.

## SQLite → PostgreSQL migration tooling

Migration is an explicit operator workflow. It does **not** stop launchd, switch storage,
send messages, run analyses, or enable cloud scheduling. Keep local SQLite authoritative
during development and rehearsal. Never run local and cloud schedulers as simultaneous
authorities.

Capture a consistent source using the existing SQLite online backup API (never `cp` the
live database, WAL, or SHM files):

```bash
advisor --db SOURCE backup --output BACKUP
advisor migration inspect --source BACKUP
```

Use an explicit new backup path. Backup capture refuses overwrite, uses private file
permissions, and verifies integrity. Migration commands read backups in a consistent,
read-only transaction; known default/configured live SQLite paths are rejected. A custom
path is not proof of backup provenance: operators must use the capture command above.

For PostgreSQL operations, supply `ADVISOR_DATABASE_URL` through the **process environment**
using a private credential mechanism. Migration does not load this URL from `.env` or accept
it as a command argument. Do not put URLs in shell history, scripts, reports, or commits.

```bash
advisor migration rehearse --source BACKUP
```

Rehearsal creates its own `advisor_rehearsal_<uuid4hex>` schema, imports and verifies all
durable state, then drops that exact schema. It never initializes or imports into `public`.
It can report in-progress source state without recovering it. If cleanup fails, the error
reports only the generated schema name for explicit manual inspection/cleanup. An
unacknowledged CREATE may also require inspection; schemas are never enumerated/deleted by
prefix. Keep backups private and outside source control.

Final cutover is a separate, explicitly authorized operation: freeze manual writers, choose
a window between slots, verify no active analysis/scheduler/delivery work, unload local
launchd, confirm it stopped, and capture a **new final backup**. Then:

```bash
advisor migration import --source FINAL_BACKUP --target-schema public
advisor migration verify --source FINAL_BACKUP --target-schema public
```

Import refuses RUNNING analyses, RUNNING scheduler executions, and SENDING deliveries.
It locks the target, initializes a completely empty schema to v3 when necessary, and
imports in one transaction with constraints/triggers enabled. Existing initialized targets
must be compatible v3. Payload text and history are copied verbatim; IDs/timestamps retain
their exact values. Every row is validated before commit. A mismatch rolls back both new
schema initialization and data import. Partially populated/different targets are refused;
there is no merge, repair, overwrite, reset, or historical replay.

An identical rerun reports `verified_noop` without data writes. If COMMIT acknowledgement
is lost, rerun against the same backup: complete equality establishes success; different
state requires investigation. `verify` uses a PostgreSQL read-only repeatable-read
transaction, never initializes a schema, and refuses missing/incompatible/different state.
Reports contain counts, IDs, lifecycle summaries, and digests, never payloads or Telegram
destination hashes. The source must be schema v3; historical payload versions are retained.

Only after successful verification should an operator authorize a controlled cloud run,
then recurring scheduling. After the first PostgreSQL/cloud write, SQLite is stale:
re-enabling it is **not** a safe rollback. Stop cloud writers and reconcile/reverse-migrate
new analysis, portfolio, scheduler, and delivery history before changing authority.

Offline tests need no PostgreSQL. Disposable PostgreSQL 17 CI runs both storage and migration
integration modules; locally these remain skipped without `ADVISOR_TEST_POSTGRES_URL`.

## Persistence backends

SQLite remains the default local backend and is accessed through `aiosqlite` (tested with 0.22.1).
PostgreSQL is an explicit alternative implemented with the native asyncio driver
`asyncpg>=0.30,<1`; no ORM is used. Live HTTP uses `httpx` (0.28.1).
Neutral async repository protocols live in `portfolio/repositories.py` and
`analysis/repositories.py`, with equivalent scheduler and delivery protocols. `storage/`
implements them; domain models and application services import neither SQLite nor PostgreSQL.
The CLI selects a repository bundle once at startup.

Storage configuration is loaded from the optional working-directory `.env`, then overridden by
the real process environment:

```dotenv
ADVISOR_STORAGE_BACKEND=sqlite
# ADVISOR_DATABASE_URL is required only for an explicit postgres backend.
ADVISOR_DATABASE_URL=
```

`sqlite` is the backward-compatible default and continues to use `--db`, `ADVISOR_DB_PATH`, or
`~/.advanced_trading_advisor/advisor.sqlite`. Selecting `postgres` requires a
`postgres://`/`postgresql://` URL. The URL is excluded from settings/database representations and
normal errors. Do not commit it. The local launchd installation needs no configuration change and
continues to select SQLite. `advisor doctor`, `advisor backup`, and backup inspection remain
explicitly SQLite-only and fail before connecting when PostgreSQL is selected.

### SQLite

The SQLite core requires an explicit path; the CLI supplies the documented local default or a
caller override.

Composition example inside an async function:

```python
from pathlib import Path

from advanced_trading_advisor.portfolio.confirmed_state import ConfirmedStateService
from advanced_trading_advisor.storage.database import Database
from advanced_trading_advisor.storage.repositories import SQLiteSnapshotRepository

database = Database(Path("local-data/advisor.sqlite"))  # Explicit caller-owned location
await database.initialize(create_parent=True)  # Opt in to parent directory creation
state = ConfirmedStateService(SQLiteSnapshotRepository(database))
current = await state.load_current()  # None on a fresh database
```

Call `initialize()` on startup, including after a process restart. Parent creation is disabled
by default. Schema version 3 is recorded in `schema_metadata`; compatible v1/v2 databases migrate
to v3 in one transaction, preserving confirmed state, analysis history and scheduler slots. Unknown versions,
unversioned existing tables and incompatible table/column layouts fail without rewriting data.
There is no generic migration framework or automatic repair.

| Table | Purpose |
| --- | --- |
| `portfolio_snapshots` | Immutable versioned snapshot JSON plus identity/timestamps |
| `current_portfolio` | Singleton snapshot pointer and positive revision |
| `confirmed_updates` | Append-only transition identity, type and old/new snapshot references |
| `analysis_runs` | Independent run identity, snapshot reference, lifecycle and optional result |
| `scheduler_executions` | One durable, auditable UTC 4H slot and its outcome |
| `telegram_deliveries` | One logical Telegram delivery per analysis and destination |
| `schema_metadata` | Database schema version |

Each operation opens and closes its own connection. Foreign keys are enabled on every connection;
the default busy timeout is five seconds. Writes use short `BEGIN IMMEDIATE` transactions with
rollback on errors/cancellation. No network or analysis work belongs inside these transactions.
SQL values are bound parameters, and financial data never uses SQLite REAL.

`ConfirmedStateService.initialize(snapshot)` creates revision 1 with an INITIAL_STATE audit entry.
`confirm(snapshot, expected_revision=..., previous_snapshot_id=..., update_type=...)` validates
and writes the replacement snapshot, audit entry and pointer in one transaction. Stale revision,
wrong previous ID or backwards confirmation time fails without a partial commit. Callers must
reload and obtain explicit user confirmation again after a conflict; there is no automatic merge.

Saving an identical encoded snapshot under the same ID is idempotent. Different content under
that ID fails. SQL triggers prohibit snapshot and audit UPDATE/DELETE. A confirmed ID cannot be
reactivated: a new confirmation requires a new snapshot ID and unique audit entry. Standalone
snapshot save and audit append never change current state; normal confirmations use the service.
Audit payloads identify both immutable states rather than duplicating financial data or storing
free-form notes. Recommendations have no code path into this service and never execute trades.

Explicit version-1 codecs store Decimal values as plain decimal strings, UUIDs as canonical
strings, enums by their stable values, and aware timestamps as UTC ISO 8601 with microseconds.
They preserve position/order ordering and domain equality without rounding or float conversion.
They reject malformed payloads, duplicate JSON keys and unsupported versions. Snapshot payloads
include positions, financial settings and all orders, including resolved orders. The Phase 1
`StablecoinClassification` remains independent application configuration, not snapshot-specific
state; it is therefore not copied into snapshots.

Persist `AnalysisRun.start(...)` using `SQLiteAnalysisRunRepository.create`, close the transaction,
then perform future analysis separately. Finish using `mark_completed` or `mark_failed` with an
aware completion timestamp. Failures accept only `FailureCategory` enum values, never raw provider
errors. Terminal runs cannot be changed. Multiple runs of one snapshot use distinct run IDs; there
is no snapshot-keyed idempotency. Interrupted RUNNING records remain RUNNING after restart; this
Phase 6 recovers expired runs before the next analysis, as documented below; it never resumes work.

Completed runs may contain optional result JSON of the form
`{"schema_version":1,"data":{}}`. Phase 4 defines `AnalysisResultCodec` for completed reports; monetary values
must be strings and JSON floating-point/non-finite numbers are rejected. Do not put credentials,
raw provider responses or other secrets into results. The local SQLite file is not encrypted;
protect the caller-selected directory and backups appropriately. PostgreSQL transport and storage
protection depend on its later deployment configuration and are not claimed by this phase.

Tests use temporary file-backed databases and block network connections/DNS. They exercise
reopening in a separate process, exact codecs, revision conflicts, transaction rollback,
foreign keys, immutability and independent run lifecycles.

### PostgreSQL infrastructure

PostgreSQL schema metadata is also version 3. New empty schemas apply ordered v1→v3 migrations
for portfolio/history, scheduler, then Telegram delivery storage. Relational identities use native
UUID, indexed timestamps use `TIMESTAMPTZ`, and canonical versioned snapshot/update/result payloads
remain `TEXT` so their exact encoding is preserved. Foreign keys, checks, uniqueness constraints,
partial uniqueness for the single RUNNING analysis, and trigger guards protect immutable and
terminal records at the database boundary.

Confirmed portfolio transitions use one transaction containing an advisory lock, immutable
snapshot insert, immutable audit insert and revision-checked current-pointer update. Analysis,
scheduler and Telegram repositories combine transaction-scoped advisory locks, row locks,
conditional updates and database constraints for leases, stale recovery, owner fencing,
idempotent scheduled slots and delivery attempt transitions. Provider/connection exception text
is replaced with a fixed storage error; a database URL is never written to reports or state.

Normal local tests and the `offline-tests` CI job remain credential-free and do not require
PostgreSQL. A separate `postgres-integration` CI job starts a disposable PostgreSQL 17 service
with fixed test-only credentials, exposes `ADVISOR_TEST_POSTGRES_URL` only to the integration-test
step, and uploads no database artifacts. To run the same isolated contract locally against a
dedicated disposable test database only:

```sh
ADVISOR_TEST_POSTGRES_URL='postgresql://test-only-url' \
  .venv/bin/python -m pytest -m postgres_integration tests/test_postgres_storage.py
```

The integration test creates a unique temporary schema and drops it afterward. It never falls
back to `ADVISOR_DATABASE_URL`; do not point the dedicated test variable at production. Without
that variable the test is explicitly skipped. Cloud PostgreSQL provisioning and migration of the
existing local SQLite database are not implemented in this phase.

## Market data and cutoff

`market/live.py` adapts the old project's proven read-only CoinGecko behavior without runtime
imports from it. `MarketDataService.for_snapshot` requests exactly the union of portfolio symbols
and assets in OPEN orders. Closed orders do not extend coverage. The explicit Demo mapping is
BTC/bitcoin, ETH/ethereum, SOL/solana, USDT/tether and USDC/usd-coin. Unsupported symbols and
missing quotes fail explicitly; there is no fabricated $1 stable price or static fallback.

Configuration is deliberately small: `COINGECKO_API_KEY`, `COINGECKO_API_TIER=demo` and
`MARKET_DATA_TIMEOUT_SECONDS=10`. See `.env.example`. `python-dotenv` reads only the current
working directory's optional `.env`, without interpolation or parent-directory search.
Process environment overrides file values; explicit settings mappings bypass the file for tests.
Other tiers are rejected. The key
is sent in the Demo header, excluded from settings repr and never logged. HTTPS host is fixed;
redirects and implicit environment proxies are disabled. No keys are persisted.

`/simple/price` requests USD, full precision and `last_updated_at`. JSON numbers decode directly
to Decimal, not float. Quotes must be positive, finite, not future-dated and at most 10 minutes
old by default. Validation runs both after quotes arrive and after history fetching completes.
`as_of` records snapshot collection completion; each asset retains its actual quote timestamp.

`/coins/{id}/ohlc?days=30` selects automatic 4H granularity. For each non-stable asset,
`/coins/{id}/ohlc?days=1` supplies the bounded trigger window described above. CoinGecko documents
timestamps as **candle close times**, not open times:
[CoinGecko OHLC documentation](https://docs.coingecko.com/reference/coins-id-ohlc).
The adapter sorts input, validates OHLC ranges and the UTC 4H grid, rejects future candles,
duplicate timestamps and gaps, and requires the newest close to be no more than 5 hours old.
That permits a normal four-hour interval plus up to one hour of provider publication lag.
Freshness thresholds are configurable through `MarketDataPolicy`.

All returned assets with candles must share exactly the same latest closed cutoff. Mismatch
raises `MarketCutoffMismatchError`; this phase does not trim history or pretend the older asset
contains a newer candle. `MarketSnapshot.effective_market_cutoff` is present only when every
asset has the same cutoff. Empty history retains verified quotes and explicit missing features.
Insufficient history leaves individual indicators or the 1H trigger window absent, never invented.
For scheduled work, the slot's expected 4H cutoff is authoritative: the CoinGecko adapter caps
both validated 4H and aggregated closed 1H evidence before calculating technical features or
derived 1D context. Newer provider candles are valid but excluded from that run. The scheduler
still requires every asset's capped 4H cutoff to equal its expected cutoff. Per-asset 1H lag is
preserved and safely degrades only the affected trigger; it does not invalidate common 4H data.

Asset completeness retains Phase 1 PRICE_ONLY/PARTIAL/COMPLETE. Snapshot completeness is
UNAVAILABLE for empty coverage, PARTIAL when features/cutoffs are missing or inconsistent, and
COMPLETE only with all features and a common cutoff. Invalid/stale/missing quotes fail the live
fetch instead of returning a partially valued portfolio. `storage/market_codec.py` provides
backward-compatible v1–v3 Decimal/UTC JSON envelopes; v3 adds closed 1H evidence while retaining
cutoffs, closed prices, features and completeness. It checks derived metadata on decode. No new
SQLite table is added.

Network policy: one retry by default for transport errors/timeouts and 5xx, separated by 100 ms;
no retry for 429, other 4xx or redirects. Each attempt has a bounded wall-clock timeout in
addition to HTTP I/O timeouts. Cancellation propagates. All provider error messages are
sanitized; raw response bodies and HTTP request objects are not attached as explicit causes.
HTTP responses are mocked in tests; live calls never run in pytest.

## Historical Phase 3 deterministic baseline

This section preserves the Phase 3 rules for compatibility and audit context. Phase 10 uses the
active-management, reserve-v2 and 1D/4H/1H rules documented above whenever the richer evidence is
available; it does not rewrite historical analyses produced by these baseline policies.

`MarketRegimePolicy` version 1 uses ordered predicates, not a weighted score. Current analyses emit
policy version 2 when completed daily context is available and applies the daily veto described
above; incomplete/legacy evidence retains the compatible version-1 behavior. Thresholds are
validated, immutable and configurable in `MarketRegimeConfig`. Comparisons use the latest
**closed candle price**, not a potentially newer quote. Default rules, in order:

1. Stale/invalid data or incompatible cutoffs: RISK_OFF, unavailable evidence.
2. Missing BTC, incomplete features or missing closed prices: ELEVATED_RISK, incomplete evidence.
3. BTC close < EMA20 < EMA50 and (RSI <=35 or weekly change <=-10%): RISK_OFF.
4. Bearish BTC stack, ATR/close >=5%, or RSI >=75: ELEVATED_RISK.
5. BTC close > EMA20 > EMA50, weekly change >=0, RSI >=50, and >=60% of analyzed non-stables
   have a bullish stack: RISK_ON.
6. Bullish BTC stack and weekly change >=0 without full confirmation: CONSTRUCTIVE.
7. Otherwise: NEUTRAL.

Stablecoins are excluded from breadth. No implicit benchmark is fetched: if the exact portfolio/
OPEN-order union has no BTC, rule 2 applies. These are initial product rules, not a calibrated
forecast or evidence of investment performance. AI cannot select the regime.

`PortfolioCalculator` values every holding using its USD quote (including depegged stables),
calculates allocations and stable/non-stable totals, and aggregates only OPEN orders. Phase 1
`LimitOrder.target_price` is interpreted as **USD per asset**. BUY commitment is limit price ×
quantity; SELL reservations retain quantity, market notional and limit notional. Balances are
total owned inventory including reserved amounts, not exchange-reported free balances. Pending
SELL proceeds are not counted as cash. No holdings are mutated by these calculations.

Historical `StableReservePolicy` version 1 used these initial liquidity fractions. New analyses
analyses use reserve v2 from the active-management section; v1 remains callable/readable only for
compatibility and comparison:

| Regime | Portfolio liquidity floor |
| --- | --- |
| RISK_ON | 15% |
| CONSTRUCTIVE | 18% |
| NEUTRAL | 20% |
| ELEVATED_RISK | 25% |
| RISK_OFF | 30% |

The separate market-risk component is explicitly **zero** in version 1: the regime-dependent
liquidity floor already accounts for it. Fractions are configurable in `StableReserveConfig`.
The target runway defaults to 12 months in `FinancialSettings` and remains a per-state input;
existing snapshots with a different runway retain it. No storage migration is needed.

```text
available = max(stables - open BUY commitments - explicit liquidity buffer, 0)
expense floor = monthly expenses × target runway
minimum reserve = max(expense floor, portfolio liquidity floor, market-risk reserve)
deployable = max(available - minimum reserve, 0)
deficit = max(minimum reserve - available, 0)
actual runway = available / monthly expenses
```

The expense floor is never capped at portfolio size. Expenses must remain positive, as in
Phase 1; zero is rejected rather than producing infinity. Dominant-component ties favor
expenses, then liquidity. Assessments are immutable. Decimal arithmetic uses the existing
38-digit local calculation context; input parsing/serialization does not round values.

Example: portfolio $14,000, stables $4,000, expenses $250/month, 12 months and NEUTRAL gives
expense floor $3,000, liquidity floor $2,800, minimum $3,000 and deployable $1,000. A $400 OPEN
BUY reduces available liquidity to $3,600 and deployable capital to $600.

`PortfolioRiskPolicy` composes valuation, regime and reserve using the same stable classification
and freshness policy. OPEN SELL reservations of stable assets become an explicit liquidity
buffer so already-reserved stable units cannot be deployed again. Missing/stale quotes fail
valuation. Reserve deficit, no deployable stables, BUY commitments exceeding stable value,
SELL quantity exceeding holdings, or incomplete regime evidence block new buying capacity.
An asset allocation above 50% raises a configurable concentration warning (including individual
stablecoin exposure). Results expose all underlying assessments and conditions.
`new_buy_capacity_available` is only a necessary capital/data condition, **not** approval of a
recommendation. Phase 4 applies separate whole-plan validation after candidate generation.

Composition inside an async function, after loading a confirmed snapshot:

```python
from datetime import UTC, datetime
from advanced_trading_advisor.market.live import CoinGeckoMarketDataProvider
from advanced_trading_advisor.market.service import MarketDataService
from advanced_trading_advisor.market.settings import CoinGeckoSettings
from advanced_trading_advisor.risk.assessment import PortfolioRiskPolicy

provider = CoinGeckoMarketDataProvider(CoinGeckoSettings.from_environment())
market = await MarketDataService(provider).for_snapshot(current.snapshot)
assessment = PortfolioRiskPolicy().assess(current.snapshot, market, now=datetime.now(UTC))
```

Run this outside any SQLite transaction. It performs read-only market calls and calculations,
not analysis recommendations or confirmed-state writes.

### Optional manual market smoke test

From the project root, with a Demo key in local `.env` or process environment, run this explicitly.
It is not part of pytest and does
not load a portfolio or alter the database. It prints BTC/ETH/SOL quotes, timestamps, candle
cutoffs, usable candle counts, technical features and deterministic regime/version, but not the key.
The existing provider and feature calculator perform all market work; this is only diagnostic output:

```sh
python -m advanced_trading_advisor.market.smoke
```

## Phase 4 CLI workflow

No new third-party dependency is needed for the CLI (`argparse`). After updating an editable
install, both `advisor` and `python -m advanced_trading_advisor` support:

```sh
advisor setup --position BTC=0.1 --position USDC=4000 --expenses 250 --runway 12
advisor portfolio show
advisor portfolio replace --position BTC=0.09 --position USDC=4500 --expenses 250 --runway 12
advisor orders show
advisor orders add --asset BTC --side buy --price 50000 --quantity 0.001
advisor orders update ORDER_UUID --status cancelled
advisor analyze
advisor status
advisor history --limit 20
advisor history latest
```

Default DB: `~/.advanced_trading_advisor/advisor.sqlite`. `--db PATH` **before the command**
overrides `ADVISOR_DB_PATH`, which overrides the default. Parent creation is explicit in CLI
composition and uses only the chosen directory. No Git initialization is performed.
For development use a separate file, e.g. `advisor --db ./local-data/demo.sqlite ...`.

Setup/replacement collect full balances through repeated `--position SYMBOL=AMOUNT`, or `--empty`.
These are total owned balances including reserved inventory, NOT only free exchange balances.
Supported symbols: BTC, ETH, SOL, USDT, USDC. USDT/USDC stable classification is fixed in this
CLI and printed for confirmation; it does not imply a fixed $1 quote. Expenses are required;
runway defaults to 12 months. Zero balances must be omitted. Replacement preserves all existing
orders (including resolved history), shows the complete proposed state, and requires typing `yes`.
Declining/EOF does not save a snapshot. Revision conflicts fail and require reload/reconfirmation.

Order addition starts OPEN. Update requires an exact OPEN order UUID and supports FILLED,
CANCELLED or EXPIRED. A fill requires full reconciled balances, including fees and all actual
outcomes, supplied by the user, for example:

```sh
advisor orders update ORDER_UUID --status filled --position BTC=0.101 --position USDC=3950
```

No execution price, fee, balance change or sale proceeds are inferred. Cancellation/expiry keeps
balances unchanged. Old snapshots are never edited; snapshot + audit + revision advancement is
atomic after confirmation. Changing an order's price/quantity is not a hidden edit: confirm old
order cancellation and explicitly add the replacement order. Partial-fill workflows are deferred.

### Record an already-executed spot trade

`portfolio trade` records a real BTC, ETH or SOL trade that the user already executed externally.
It never connects to an exchange, places an order, or changes an existing order status. Interactive
mode prompts for the execution terms and shows exact before/after Decimal balances:

```sh
advisor portfolio trade
```

Explicit BUY and SELL forms are also available:

```sh
advisor portfolio trade --side buy --asset SOL --price 97 \
  --stable-debit 150 --stable USDC --fee 0
advisor portfolio trade --side sell --asset SOL --price 110 \
  --quantity 1.5 --stable USDC --fee 0
```

For BUY, `--stable-debit` is the total selected stablecoin amount actually debited, including the
fee. Trade notional is debit minus fee, and received asset quantity is notional divided by the
execution price. For SELL, gross proceeds are quantity times execution price and the selected
stablecoin receives gross proceeds minus fee. The MVP supports a fee only in that settlement
stablecoin; use full `portfolio replace` for a base-asset, third-token or multi-asset fee.

USDT and USDC remain separate persisted positions. The preview additionally displays their
nominal aggregate as `STABLES`; it never persists a synthetic asset or drains both coins for one
BUY. When both exist, interactive mode requires an explicit settlement choice. Financial
settings, core policies, unrelated positions and all orders are preserved. Existing OPEN orders
may trigger a review warning but are never filled, cancelled or resized.

Only exact `yes` persists one new immutable snapshot with a `spot_trade_recorded` audit update.
The existing transaction atomically inserts the snapshot and audit record and advances the
revision-checked current pointer. A concurrent state change aborts and requires the trade to be
reviewed again; it is never recalculated or retried automatically.

### Historical Phase 4 analysis and recommendation policy v1

The rules below describe the preserved Phase 4 baseline. Phase 10 supersedes candidate generation,
portfolio ranking, reserve sizing and order ladders with `ActiveManagementEngine`, temporal
confirmation and `ActivePlanValidator` v3, while retaining compatible old result decoding.

`AnalyzeCurrentPortfolio` loads the confirmed snapshot, commits a new RUNNING run, then performs
market work **outside** SQLite transactions. It fetches the holdings/OPEN-order union, computes
the Phase 3 risk assessment, generates candidates, validates the whole plan, and only then
persists/prints final recommendations. Each execution has a new run ID even for the same snapshot.
Completion persists the effective cutoff in both run metadata and result. A candle closing during
the fetch may follow run start, but never completion. Table payload codecs remain version 1;
the current database schema is version 3.

Failures store only allowlisted categories and no result; cancellation is recorded as failed if
storage remains available. If saving failure itself is impossible, CLI reports that explicitly.
Process termination/power loss can leave RUNNING for manual inspection; Phase 6 recovery and the
Phase 7 scheduler are documented below. Reports remain tied to the loaded snapshot even if another
process confirms a newer state while analysis runs.

`RecommendationPolicy` version 1 contains all sizing/target thresholds. New entries require
complete evidence, funded capacity, zero reserve deficit, a bullish closed-price/EMA20/EMA50
stack, nonnegative 7d change, RSI <=65 and 24h gain <=5%. For ELEVATED_RISK the limits tighten
to RSI <=55 and 24h gain <=2%. RISK_OFF creates no buys. No market BUY/chasing is generated.

```text
limit buy target = min(support, EMA20, EMA50, current quote) - depth × ATR14
depth = 1 normally, 2 in ELEVATED_RISK
```

Targets must stay positive and below the quote. The total new/replacement BUY budget is a
fraction of uncommitted deployable stables: RISK_ON 25%, CONSTRUCTIVE 20%, NEUTRAL 15%,
ELEVATED_RISK 5%, RISK_OFF 0%. Asset exposure including existing OPEN buys and new proposed
quantities is capped at 40% of the original portfolio value. SELL proceeds/cancellations cannot
increase this budget. Quantities floor to 1e-8 application precision; minimum proposal is $10.
These are not exchange lot/tick-size guarantees. Candidate allocation is deterministic: existing
orders sorted by UUID first, then assets alphabetically; this is not a performance ranking.

Every OPEN order receives an exact-ID review. BUYs failing reserve/allocation/technical rules or
with marketable targets are CANCEL candidates. Targets within one ATR of the new technical level
are KEEP; more distant targets may be REPLACE only if full additional capital and allocation room
exist, otherwise CANCEL. REPLACE carries full new terms and never assumes prior cancellation.
Inventory-backed defensive SELLs are kept; oversubscribed or marketable SELL orders are flagged
for manual cancellation/status review, never marked filled from a quote.

Reserve deficits or allocation above 60% produce **informational-only REBALANCE**, without
executable legs. The initial engine does not force liquidation or generate discretionary market
SELLs merely because conditions weaken. HOLD/WAIT are normal successful outcomes.

The validator checks the entire plan: combined BUY budgets, existing commitments, projected
asset exposure, combined SELL quantities after existing reservations, duplicate equivalent
proposals, duplicate/conflicting order references, and exact OPEN ID/asset/side matching.
Replacement counts as an additional full commitment/quantity alongside the old order. New stable
SELLs and unbounded market BUYs are rejected in v1. Numerical estimates are not an execution
guarantee: fees, venue rules and slippage still need manual review.

If any candidate is unsafe, the **entire candidate plan and its reasons are discarded** and
replaced with a generic safe WAIT. Reports are built only from final validated recommendations;
no candidate summaries survive rejection. No recommendations call the confirmed-state service.

### Historical result format and offline development

Result codec v1 stored UUIDs, UTC times, source, common cutoff, policy versions, financial/reserve
summary, asset technical evidence, warnings and only final recommendations. All financial JSON
values are decimal strings. Unknown/malformed versions fail clearly. History spans all snapshots;
`history latest` renders the latest completed result, not a fresh provider request. `status` shows
DB path, current snapshot/revision, position/open-order counts and latest completed analysis/cutoff
across snapshots. Read-only commands never call CoinGecko or load API credentials.

To exercise the CLI offline against your development file:

```sh
advisor --db ./local-data/demo.sqlite analyze --source static --no-ai
```

Static uses explicitly labelled fixture prices with fresh fixture timestamps and PRICE_ONLY data,
so speculative BUYs remain blocked. This is not live analysis. Automated tests use isolated temp
databases and fake providers; they never touch the default DB or perform network calls.
The separate live market smoke command above remains manual only.

Daemon installation, exchange integration, automatic execution, automatic portfolio
mutation, partial-fill reconciliation and backtesting/performance claims remain deferred.

## Optional AI reasoning (Phase 5 baseline, tightened in Phases 9–10)

Phase 10 keeps this provider/fallback lifecycle but narrows accepted AI output to the exact ordered
trade terms and setup IDs generated and validated by the deterministic active/trigger engine, as
documented above.

AI is disabled by default. No new dependency: `ai/service.py` uses existing async `httpx` with
the current [Responses API Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
The old project's configurable `gpt-5.6-terra` default is retained; it supports
[Responses and structured output](https://developers.openai.com/api/docs/models/gpt-5.6-terra).
No old backend package, deployment settings or credentials are imported.

| Variable | Default | Rule |
| --- | --- | --- |
| `OPENAI_ENABLED` | `false` | true / false |
| `OPENAI_API_KEY` | empty | Required when enabled; excluded from repr/logs |
| `OPENAI_MODEL` | `gpt-5.6-terra` | Nonempty identifier; API checks availability |
| `OPENAI_REASONING_EFFORT` | `low` | none / low / medium / high / xhigh / max |
| `OPENAI_TIMEOUT_SECONDS` | `45` | Integer 1–120; total wall-clock and HTTP I/O bound |

The shared `configuration.local_environment` retains the existing optional cwd `.env` loader:
no interpolation, parent search or process mutation; real environment wins. Explicit mappings
bypass files in tests. `.env` stays ignored. Configure only local files or process environment.
The adapter fixes the HTTPS endpoint, disables redirects/environment proxies, makes no retries,
sends `store=false` and caps output at 4,000 tokens including reasoning.

```sh
advisor analyze --no-ai  # Explicit deterministic override
advisor analyze --ai    # Explicit enable; valid key/config required
advisor analyze         # Uses OPENAI_ENABLED
```

Explicit enable with a missing key, invalid model syntax, effort or timeout fails configuration
before market/AI work. It never pretends AI is disabled. Use `--no-ai` to continue deterministically.
This resolves the specification's conflicting missing-key/fallback wording in favor of its
explicit fail-configuration requirement. Unsupported model/effort combinations returned by the
API are runtime fallback, not a configuration validator's claim of model compatibility.

### Boundary and plan selection

`analysis/reasoning.py` defines the neutral async `RecommendationReasoner` protocol. CLI composes
the optional adapter; domain, market, risk and the use case have no OpenAI imports.

1. Calculate deterministic metrics/risk and validate the Phase 4 baseline.
2. Request bounded reasoning/presentation around the complete deterministic plan using compact
   deterministic context.
3. Decode strict output into existing domain Recommendations; validate the whole alternative.
4. Select the candidate as AI_ASSISTED only when it is nonempty, fully valid and its canonical
   ordered action terms exactly equal the deterministic baseline. Otherwise retain the exact
   baseline as AI_FALLBACK. Never append the two plans.

The unchanged Phase 4 validator rejects the entire AI plan for any unsafe action: reserve/budget,
allocation, inventory/SELL reservations, exact OPEN IDs, replacement semantics, duplicates and
conflicts. Cancellations release no funds/units; replacements consume their FULL new cost/units
alongside the old order. Future SELL proceeds never finance BUYs. Market BUY and stable SELL
remain unavailable. Omitted orders remain OPEN and reserved. Passing these checks does not
guarantee profitability, execution feasibility or optimal advice.

### Context and schema

`ai/context.py` explicitly includes snapshot ID, holdings/allocations/stables, expenses and
target/actual runway, reserve/deployable/deficit, common closed 4H cutoff, regime/version,
quotes/closed prices/features/completeness, exact OPEN order IDs/terms/commitments/reservations,
risk warnings/concentration/buy-block, and code-calculated total budget, allocation cap, eligible
buy target and maximum per-asset buy/sell quantities. Individual maxima are not additive.
Capacity values are ceilings, never authorization. Context contract v4 additionally exposes the
deterministic recommended deployment and whether any execution-valid CONFIRMED or compatible
legacy ACTIVE setup can support an order action.
Serialization is sorted and deterministic; context above 24,000 characters triggers fallback,
never silent evidence truncation. No candles, history, environment, secrets or object dumps.

One canonical action-term representation is shared by context and the exact acceptance gate. It
contains action type, asset, side, derived USD amount, quantity, price, setup/order IDs, priority,
review time and expiry, with explicit nulls. The model must return every allowed item exactly once
in the same order; it may not add, remove, resize, reprice, reprioritize or retime actions. Prompt
contract v4 makes the AI a bounded reasoning/presentation layer, not an authority for executable
financial terms. Structured-output schema v3 is unchanged.

`ai/models.py` defines AIRecommendationPlan (summary, actions, observations). Each action supports
asset, action_type, side, existing_order_id, target_price, quantity_asset, priority, reason,
invalidation_condition, review_at and expires_at. Strict schema requires every key, using null
for irrelevant fields. Domain validation rejects conflicting terms. Existing lowercase action
enum values only, 1–20 actions, financial values as plain decimal strings, exact UUID references.
Unknown/duplicate keys, unsupported actions, numeric financial JSON, non-finite/exponent decimals
and malformed dates/UUIDs fail. No tools or conversation history are sent.

The system contract is spot-only advisory growth, expense/reserve protection, no forced or
unnecessary trading, favorable risk/reward and HOLD/WAIT on weak evidence. Deterministic metrics,
market regime and limits are authoritative: no recalculation or invented assets/prices/orders.

### Narrative, fallback and persistence

AI summary and observations are never displayed or persisted. Even accepted reasons/invalidation
prose are discarded: a valid HOLD reason could hide an unrelated unsafe trade. Commentary is
rebuilt from final validated action types. Final terms, priority and structurally checked optional
dates remain. No rejected advice or provider error body survives into reports/history.

Timeout, API/network failure, refusal, incomplete/malformed/schema-invalid output or unsafe plan
returns the deterministic baseline and the fixed warning:
“AI reasoning unavailable; deterministic recommendations used.” Analysis remains COMPLETED.
Cancellation still propagates through the normal failed-run lifecycle. Market/storage failures
remain real failures; AI fallback cannot fabricate market evidence.

Report envelope **v2** adds DETERMINISTIC / AI_ASSISTED / AI_FALLBACK, configured model for attempts
and an allowlisted AI warning. The codec reads old Phase 4 v1 reports as deterministic without
model/warning; old records are not rewritten. Unknown versions fail explicitly. SQLite schema,
snapshot and run-row versions stay 1. Storage validates typed v2 reports. No raw responses,
prompts, keys or chain-of-thought are persisted. Terminal and `history latest` show reasoning mode.

### Optional manual AI smoke (never pytest)

With local credentials, use a dedicated dev DB if there is no real confirmed portfolio. These
example balances are test input, not a trading recommendation:

```sh
advisor --db ./local-data/ai-demo.sqlite setup --position BTC=0.01 --position USDC=5000 --expenses 250
# Review and type yes only to confirm this dev fixture.
advisor --db ./local-data/ai-demo.sqlite analyze --ai
advisor --db ./local-data/ai-demo.sqlite history latest
```

The analysis fetches real CoinGecko data and makes one paid OpenAI call. It never executes orders
or changes holdings. Alternatively use `advisor analyze --ai` with your already confirmed default
portfolio. Automated tests use temporary SQLite files and mocked HTTP; passing them does not
imply live OpenAI verification.

## Local reliability and recovery (Phase 6)

Use local-disk SQLite, not a network filesystem. Initialization enables WAL after verifying the
supported schema. Each connection enables foreign keys and has a bounded five-second busy timeout.
Confirmed updates remain one short `BEGIN IMMEDIATE` transaction; network/AI work never holds
that writer transaction. Result payload and COMPLETED status commit together. Terminal runs now
also have SQL UPDATE/DELETE guards. Table/snapshot/run-row schema stays v1; guards are additive.
No automatic history deletion is introduced. Keep WAL/SHM files with a live database: never copy
only its main file to make a backup.

### Run-backed lease and interruption

Every application analysis acquires through `AnalysisRunRepository.acquire`, atomically checking
all RUNNING records and creating its own run. The run UUID is the owner/fencing token, `started_at`
is the acquisition timestamp, and expiry is **started_at + 15 minutes**, defined in
`analysis/recovery.py`. This reuses existing run storage; no lock table, process-local-only lock,
heartbeat or daemon. Low-level `create` remains for legacy/import/test records, not for
starting executions. A recent RUNNING record blocks acquisition across separate processes.

The complete execution has a **600-second** async deadline, shorter than the lease. Before a new
analysis, expired RUNNING records become FAILED/`interrupted`, with no result, in the same
transaction as acquisition. Recent runs are never recovered. Success/failure releases ownership
by making the run terminal; a crashed process blocks only until expiry. Completion after the
lease deadline is refused; a recovered old token cannot overwrite its terminal run or release
a new owner's run. Timestamps are UTC; keep the system clock synchronized. Recovery is demand-driven,
not immediate after a crash, and does not resume network requests or alter confirmed state.

Cancellation propagates through market/AI requests instead of becoming successful AI fallback.
Handled cancellation marks the owned run interrupted. If storage is unavailable or the process
is killed during cleanup, expiry/recovery is the backstop. Transactions roll back on BaseException.
Report formatting is prepared before completion is committed. After the completion commit, a
Ctrl+C/broken terminal during delivery does **not** delete the valid durable result: use
`history latest` to read it. SQLite's commit is the durable boundary; terminal output cannot be
atomically transacted with SQLite. No recommendation mutates balances or executes a trade.

### Local doctor, backup and inspection

```sh
advisor doctor
advisor backup
advisor backup --output ./local-data/backups/manual.sqlite
advisor backup inspect ./local-data/backups/manual.sqlite
advisor status
```

Use `--db PATH` before the command for a dev database. Doctor/inspection are read-only, never
initialize or repair the inspected database and never load provider credentials or call networks.
They report PASS/WARN/FAIL for schema, `integrity_check`, `foreign_key_check`, domain/result
codecs, current reference/revision, audit-chain continuity, snapshot/order structure and run/result
consistency. Expired RUNNING records produce WARN only; the next analysis performs recovery.
More than one unexpired lease is an integrity failure. Failures exit nonzero. Legacy COMPLETED
records without a result are flagged, not silently repaired or supplied a fabricated result.

Backup uses the **SQLite online backup API**, not file copying. The default is
`~/.advanced_trading_advisor/backups/advisor-<UTC timestamp>.sqlite`. Explicit outputs create missing
parents and reserve a new destination exclusively with mode 0600; existing paths, including
symlinks, are never overwritten. A bounded backup progress deadline prevents indefinite contention.
The copy is reopened read-only and checked with the same schema/current/history/integrity checks.
Only after successful verification is its path reported. Failed copies created by this operation
are removed; the source is not changed. Stale RUNNING records in a consistent backup may produce
a warning, not data loss. No `.env` or credentials are bundled.

Inspection shows schema version, current snapshot/revision, snapshot/run counts and last completed
timestamp. It is deliberately **inspection-only**: no automatic restore/replacement command.
Status adds main-file byte size and record counts (WAL/SHM sizes are not included). History/status
do not acquire analysis leases or start writer transactions. No pruning policy.

### Policy/configuration provenance and sanitized failures

New completed reports use envelope **v3**, with a canonical non-secret DecisionConfiguration:
market regime, stable reserve and whole-plan versions; target runway; live/static source; AI
enabled flag; model/effort; AI context/schema/prompt versions if available; AI fallback category;
and all deterministic recommendation-policy parameters. Target/sizing rules are part of the
recommendation policy, not a fictitious separate version. The terminal shows versions and SHA256
of safe configuration/policy material only—never a key hash. The runtime fallback category remains
in the audit record but is excluded from this configuration fingerprint. Regime/recommendation
versions also retain their existing top-level fields. New whole-plan and AI contract integers are
explicit constants.

The v1/v2 codecs remain readable; legacy reports show configuration unavailable, rather than
inventing historical provenance. Third-party fake reasoners without metadata similarly retain
unknown AI contract versions. No secrets, environment dump, prompt, response or hidden reasoning
is persisted. Unknown report versions and inconsistent provenance fail validation.

FailureCategory retains compatible legacy categories (`invalid_input`, `market_unavailable`,
`timeout`, `analysis_failed`) and adds `configuration`, `database`, `market_validation`,
`ai_timeout`, `ai_authentication_permission`, `ai_rate_limit`, `ai_provider_server`,
`ai_transport`, `ai_refusal`, `ai_incomplete`, `ai_provider`, `ai_schema`, `plan_validation`,
`validator_rejected`, `exact_terms_mismatch`, `interrupted`. These are value-free diagnostics:
they never persist status codes, provider text, candidate terms, prose, IDs or raw responses.
Historical `ai_provider` and `plan_validation` records remain readable.
Configuration errors
occur before analysis acquisition and are reported explicitly, not saved as fake attempts.
Run failures store database/market/input/internal/interruption categories; AI runtime errors
remain COMPLETED fallback reports with a separate allowlisted AI category. `analysis_failed`
continues to denote unexpected internal invariant/analysis failures. No noisy logging was added.

All regression tests remain offline. Reliability tests include separate-process acquisition,
stale-owner fencing, transaction cancellation, success/failure release, market/AI/persistence/
report cancellation, backup during an uncommitted writer, corruption, doctor exit codes and
legacy report compatibility. Manual reliability smoke uses only a temporary DB and static data;
no live OpenAI call is required. Restore and automatic execution remain deferred.

## Finite cloud dispatch (prepared, not activated)

`advisor scheduler dispatch` is the one-invocation cloud entry point. It uses the existing
`SchedulerPolicy.catchup(now)` and calls `Scheduler.once(scheduled_for=slot)` at most once.
There is no loop, historical queue, or new retry policy. At the default 4-hour cadence and
10-minute candle delay, an 08:10 UTC slot is eligible from 08:10 through **08:40 inclusive**.
08:09 and 08:40:00.000001 are no-ops. An 08:17 runner still uses the **08:00 market cutoff**.
Late runners outside the window skip the missed slot, rather than backfilling it.

The command reads **process environment only**, not `.env`, and requires PostgreSQL,
`SCHEDULER_ENABLED=true`, valid live CoinGecko settings, and valid AI/Telegram settings when
enabled. Configuration preflight performs no I/O. Outside the eligible window it does not
connect to storage. Eligible dispatch checks the existing PostgreSQL v3 schema read-only
and requires a confirmed portfolio; it never initializes/migrates storage. Eligibility is
rechecked after readiness checks. Existing local `run`/`once` and dotenv behavior are unchanged.

Safe outcomes: `completed`, `no_eligible_slot`, `already_claimed`, `already_terminal`, `skipped`,
or `failed`. No-op/duplicate/analysis-busy outcomes exit 0. A newly failed analysis, incomplete
delivery, or unexpected operational failure exits nonzero with sanitized information.
AI fallback remains a successful analysis. Telegram delivery runs only after durable successful
analysis/slot completion; delivery failure never undoes that completion. Repeating dispatch
does not retry a completed/failed slot or resend its notification. A crash between completion
and delivery requires separate operator review; dispatch does not replay delivery automatically.

PostgreSQL slot uniqueness, owner fencing, analysis leases, and delivery uniqueness are the
authority. Existing stale RUNNING recovery reconciles durable completion or marks interruption;
it does not reclaim/re-execute that slot. GitHub concurrency is only an outer guard, with
`cancel-in-progress: false` to avoid interrupting analysis/delivery.

`.github/workflows/advisor-scheduler.yml` has **only `workflow_dispatch`**, no automatic trigger.
The eventual UTC cron is `10 */4 * * *`, but a separate authorized activation commit must add
it after cutover. GitHub runner delays can exceed the catch-up window; no exact-time delivery
guarantee is implied. The job uses Ubuntu, Python 3.13, `pip install .`, read-only repository
permissions, and a 10-minute outer timeout. Timeout/crash recovery still uses existing leases.

Required GitHub Secrets (names only): `ADVISOR_DATABASE_URL`, `COINGECKO_API_KEY`,
`OPENAI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. They are scoped to the dispatch
step, never installation or artifacts. Do not write a runner `.env` or echo configuration.

Ordinary workflow environment is explicit:

| Setting | Workflow value |
| --- | --- |
| `ADVISOR_STORAGE_BACKEND` | `postgres` |
| `COINGECKO_API_TIER` / `MARKET_DATA_TIMEOUT_SECONDS` | `demo` / `10` (live adapter; no separate live-mode variable) |
| `OPENAI_ENABLED` / `OPENAI_MODEL` | `true` / `gpt-5.6-terra` (current application default) |
| `OPENAI_REASONING_EFFORT` / `OPENAI_TIMEOUT_SECONDS` | `low` / `45` |
| `TELEGRAM_ENABLED` / `TELEGRAM_TIMEOUT_SECONDS` | `true` / `10` |
| `TELEGRAM_DISABLE_NOTIFICATION` | `false` |
| `SCHEDULER_ENABLED` / `SCHEDULER_INTERVAL_HOURS` | `true` / `4` |
| `SCHEDULER_CANDLE_DELAY_MINUTES` | `10` |
| `SCHEDULER_MAX_STARTUP_CATCHUP_AGE_MINUTES` | `30` |
| `PYTHONUNBUFFERED` | `1` |

Manual acceptance is **not authorized by installing this workflow**. After offline and
disposable PostgreSQL CI pass, separately authorize cutover: stop local authority, freeze
writers, capture/import/verify the final backup using the migration runbook, then configure
GitHub Secrets privately. Review the workflow's model/configuration before use. Local launchd
must never overlap an activated cloud scheduler, even while SQLite and PostgreSQL are separate.
Only then authorize one `workflow_dispatch` inside the catch-up window. Verify one terminal
slot, one persisted analysis with the slot-derived cutoff, and one durable delivery result.
An authorized duplicate invocation must produce no additional analysis or notification.
Outside-window acceptance should be a clean no-op. Keep automatic scheduling absent until
this controlled acceptance succeeds and activation is explicitly approved. After cloud writes,
do not re-enable stale SQLite as a rollback.

## Foreground 4H scheduler (Phase 7)

The scheduler runs the existing `AnalyzeCurrentPortfolio` service against the latest explicitly
confirmed snapshot. It does not update holdings or orders. Use the same `--db PATH` placement as
other CLI commands:

```sh
advisor --db ./local-data/advisor.sqlite scheduler status
advisor --db ./local-data/advisor.sqlite scheduler once
SCHEDULER_ENABLED=true advisor --db ./local-data/advisor.sqlite scheduler run
```

`scheduler status` is read-only, creates no database, and makes no market or AI calls. It shows
configured enablement, 4H cadence, delay, current UTC, most recent stored slot/status/analysis ID,
and next target. `scheduler once` explicitly attempts the current most recent delayed slot even
with `SCHEDULER_ENABLED=false`; it uses the live provider and configured AI mode. `scheduler run`
requires enablement and stays in the foreground until Ctrl+C. Run only one foreground process per
database; SQLite still prevents duplicate slots if processes overlap. Do not use `--source static`
for scheduled work: a scheduled slot requires a real matching closed 4H market cutoff. A
test-injected fake provider is used only by offline tests and manual temporary-database smoke.

| Variable | Default | Policy |
| --- | --- | --- |
| `SCHEDULER_ENABLED` | `false` | `true` permits foreground `run`; `once` remains explicit |
| `SCHEDULER_INTERVAL_HOURS` | `4` | Only 4 is supported |
| `SCHEDULER_CANDLE_DELAY_MINUTES` | `10` | Integer 1–59 after each UTC boundary |
| `SCHEDULER_MAX_STARTUP_CATCHUP_AGE_MINUTES` | `30` | Integer 0–60; no backfill |

Targets are 00:10, 04:10, 08:10, 12:10, 16:10, and 20:10 UTC by default. The policy
accepts aware UTC times only. At startup, `run` attempts at most the newest missed target if
its age is within the catch-up window; otherwise it waits for the next target. After machine
suspension it applies the same current-slot rule. It never replays historical slots. The
clock and sleep function are injectable, so tests do not wait four hours.

### Autonomous macOS operation with launchd (Phase 10.3)

The optional user LaunchAgent runs that same foreground `advisor scheduler run` command. It does
not implement another clock, create calendar triggers, or alter slot/catch-up semantics. Before
installing it, make this explicit manual change in the project-local `.env`:

```dotenv
SCHEDULER_ENABLED=true
```

Do not put credentials in the generated plist. The application starts with this repository as
its working directory, so its existing loader reads `.env` there; real process environment
variables still override `.env`. Missing `.env` remains valid, but the helper refuses to load a
continuous scheduler unless the resulting application setting reports `Enabled: yes`.

macOS privacy protection does not reliably allow a background LaunchAgent to execute a project
from `~/Desktop`, `~/Documents`, or `~/Downloads`. Interactive Terminal access does not grant the
same access to launchd. Move the whole checkout, including its existing `.env`, to an unprotected
development location first, then recreate the path-bound virtual environment:

```sh
mkdir -p ~/Developer
mv ~/Desktop/advancedTradingAdvisor ~/Developer/advancedTradingAdvisor
cd ~/Developer/advancedTradingAdvisor
python3.13 -m venv --clear .venv
.venv/bin/python -m pip install -e '.[dev]'
```

The helper refuses `install`, `start`, and `restart` from a protected location before changing
launchd state. From the relocated repository root, manage the logged-in user's job without
`sudo`:

```sh
./scripts/advisor-launchd install
./scripts/advisor-launchd status
./scripts/advisor-launchd restart
./scripts/advisor-launchd stop
./scripts/advisor-launchd start       # `load` is an alias
./scripts/advisor-launchd logs 100
./scripts/advisor-launchd rotate-logs
./scripts/advisor-launchd uninstall
```

`install` creates a machine-specific, secret-free plist at
`~/Library/LaunchAgents/com.konstantinklinov.advanced-trading-advisor.scheduler.plist`, validates
it, and loads it with `launchctl bootstrap`. `RunAtLoad` starts it at login without an open
Terminal, while `KeepAlive` and a 30-second throttle restart it after termination. `start` safely
loads an unloaded installed job or uses `launchctl kickstart -k` when it is already loaded;
`stop` uses `bootout` for the current login session. Because the plist remains under
`~/Library/LaunchAgents`, use `uninstall` to prevent loading at later logins. The helper is tied to
the checkout and its `.venv`; reinstall after moving either path.

With the default UTC+3 Belarus time zone, the normal slots run at **03:10, 07:10, 11:10, 15:10,
19:10, and 23:10 local time**, corresponding to **00:10, 04:10, 08:10, 12:10, 16:10, and 20:10
UTC**. If the Mac is asleep, launchd does not wake it for these application-managed slots. When
the process resumes or is relaunched, the existing scheduler attempts only the newest slot when
it is still inside the configured 30-minute startup catch-up window; it never backfills older
slots. Offline/provider failures remain terminal for that slot, and the next 4H slot is the next
automatic opportunity.

Launchd stdout and stderr are local files at `.local/launchd/scheduler.stdout.log` and
`.local/launchd/scheduler.stderr.log`. The entire `.local/` directory is git-ignored and the
helper creates the files with user-only permissions. `logs` accepts at most 1,000 lines, and
`rotate-logs` truncates both files in place; launchd itself does not rotate them. Application
logging must never dump environment variables or provider credentials.

The service remains advisory only. After executing any real trade manually, confirm the updated
portfolio and order state through the existing CLI before relying on a later scheduled analysis.

Each target has one `scheduler_executions.scheduled_for` primary key. Claiming the slot happens
before analysis; any recorded slot, including RUNNING, FAILED and SKIPPED, is never relaunched.
A failed slot is terminal: the next 4H slot is the next automatic opportunity. This prevents
ambiguous double AI calls or duplicate reports after restarts. A schedule attempt stores only
the slot, expected cutoff, owner UUID, start/finish UTC times, status, linked analysis run UUID
when one exists, and an allowlisted failure category. No raw provider response or secret enters
the record. SQL guards make terminal slots immutable and prevent a recovered/expired owner from
starting a late analysis. Manual `advisor analyze` remains independent.

Scheduled acquisition uses the Phase 6 analysis lease. If another manual or scheduled analysis
owns it, the slot becomes SKIPPED/`analysis_busy` without waiting or a provider call. For an
08:10 slot, all fetched assets must have a common **08:00 UTC** closed-candle cutoff before
deterministic analysis or AI begins. The expected cutoff is passed into live acquisition so no
later 1H or 4H candle can influence features, derived 1D context, triggers, setup scoring or
recommendations for that slot. Provider candles newer than the cutoff are filtered, not treated
as an error. Only transient provider unavailability or a missing/stale
expected cutoff gets one additional market acquisition after 30 seconds; malformed data,
configuration and analysis errors are not retried. The CoinGecko adapter's own bounded HTTP
transport retry still applies inside each acquisition. If the expected cutoff remains absent,
the slot is FAILED; no AI call is made. Successful AI fallback is a COMPLETED analysis and does
not trigger a whole-run retry.

On normal cancellation during analysis, the owned analysis run and slot are marked
FAILED/`interrupted`. A hard crash can leave RUNNING; after the 15-minute lease, scheduler
startup or a new slot claim reconciles it. A durable completed analysis is linked as COMPLETED
only after checking its actual identity and market cutoff. Otherwise the stale slot becomes
FAILED/`interrupted`, with no invented report. Doctor and backups inspect scheduler records
read-only. UTC system time should be synchronized; a clock change does not remove SQLite's
slot identity protection.

## Outbound Telegram delivery (Phase 8)

Telegram is a delivery layer for the **persisted final completed** `AnalysisResult`. It never
receives an AI raw response, rejected candidate, mutable intermediate plan or portfolio write
capability. A scheduled analysis records its COMPLETED result and slot first, then attempts
Telegram delivery if enabled. Telegram failure has its own status; the AnalysisRun and scheduler
slot remain COMPLETED. AI_FALLBACK is a normal deliverable completed result.

```sh
advisor --db ./local-data/advisor.sqlite telegram status
advisor --db ./local-data/advisor.sqlite telegram test
advisor --db ./local-data/advisor.sqlite telegram send-latest
```

`telegram status` is local/read-only: it shows enablement, whether token/chat are configured,
and the latest delivery status and analysis ID. It never displays the token or contacts Telegram.
`telegram test` sends only a harmless connection message; no recommendations. `send-latest`
loads the newest persisted completed analysis without fetching market data or calling OpenAI.
It uses the same database-backed delivery identity and reports already-delivered results rather
than sending them again. An explicit `send-latest` can retry a FAILED delivery under the same
identity after an operator fixes the cause. It cannot resend an AMBIGUOUS delivery automatically.

| Variable | Default | Policy |
| --- | --- | --- |
| `TELEGRAM_ENABLED` | `false` | Explicitly enable outbound sends |
| `TELEGRAM_BOT_TOKEN` | empty | Required and syntax-checked when enabled; never logged/persisted |
| `TELEGRAM_CHAT_ID` | empty | Numeric chat ID or `@channel` username; required when enabled |
| `TELEGRAM_TIMEOUT_SECONDS` | `10` | Integer 1–60 per HTTP request |
| `TELEGRAM_DISABLE_NOTIFICATION` | `false` | Optional silent send |

The existing optional working-directory `.env` loader applies: process environment wins over
`.env`; tests provide synthetic settings and never need local credentials. Only a SHA-256
destination identity derived from the chat ID is stored, not the chat ID or bot token. Telegram
Bot API uses HTTPS through the existing `httpx` dependency; no Telegram SDK was added.

The formatter produces one deterministic plain-text message within the [Telegram `sendMessage`
4096-character limit](https://core.telegram.org/bots/api). It shows portfolio value/stables,
reserve requirements/deployable funds/deficit/runway, market regime/candle, reasoning mode,
actions and warnings. Each action uses advisory wording; KEEP/CANCEL/REPLACE include exact
existing-order UUIDs and REPLACE shows proposed new side/target/quantity. Decimal values stay
decimal strings, with no float conversion. Actionable items precede HOLD/WAIT, ordered by
priority, and low-priority full action blocks and warnings are omitted with counts only if the
message would exceed the limit. Free-text reasons are shortened at word boundaries; numeric
terms and order IDs are never cut. There is no markup parse mode, links/mentions in free text
are suppressed, and link previews are disabled. Every message ends with: “No trades were
executed. Confirm portfolio/order changes after executing them manually.”

Database schema v3 adds `telegram_deliveries`: UUID, completed analysis FK, channel, destination
identity, creation/start/finish timestamps, status, attempt count and sanitized category. The
unique `(analysis_run_id, channel, destination_key)` key prevents repeat delivery after restart.
Statuses are PENDING, SENDING, DELIVERED, FAILED and AMBIGUOUS. SQL checks and transition guards
protect the lifecycle. A known FAILED delivery can be retried only by explicit `send-latest`;
DELIVERED and AMBIGUOUS never auto-retry. A SENDING record older than 15 minutes with zero HTTP
attempts reverts to PENDING (definitely unsent). With one or more attempts it becomes AMBIGUOUS:
Telegram may have accepted a message just before the process stopped. It is not resent without
manual investigation. A hard stop between slot completion and delivery claim may leave no
delivery record; `telegram send-latest` is the explicit recovery command for that gap.

One logical delivery makes at most three HTTP attempts. Connect timeout/connection error, 5xx,
and 429 with `Retry-After` of 1–5 seconds are retried with bounded waits; authentication,
forbidden, bad request and unbounded rate limits fail immediately. A read/write timeout or
other lost response is AMBIGUOUS and is not retried for actionable advice. Only sanitized
`telegram_*` categories are persisted; provider response bodies and exception strings are not.
Doctor and backup inspection verify the delivery table and links locally; stale SENDING and
AMBIGUOUS deliveries appear as WARN, without contacting Telegram or changing state.
