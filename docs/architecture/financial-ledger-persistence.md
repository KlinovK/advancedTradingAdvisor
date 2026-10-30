# Financial ledger persistence contract (TG-7.5.5-1)

Status: approved architecture decisions D1–D5; PostgreSQL schema v4, strict mapping, transactional repository, and separate legacy/financial readiness are implemented in the local working tree. This contract does not authorize deployment or change the existing snapshot portfolio. The financial repository is not connected to existing HTTP endpoints or the Advisor Scheduler.

## Scope and boundaries

Persist immutable, fee-free Spot opening, buy, and sell facts and their explicit balance postings. Reconstruct `LedgerOperation` values and use `financial.replay.replay` as the sole accounting calculation and historical validation engine. USDT is the accounting and settlement currency. No trading execution, API endpoint, legacy snapshot conversion, mutable corrections, derived-balance table, frontend, or AI integration is in scope.

**D1 — Stack.** Extend `PostgresDatabase` and `POSTGRES_MIGRATIONS` using asyncpg. No SQLAlchemy, Alembic, second engine, or second database lifecycle. The existing schema version progresses v1 → v2 → v3 → v4.

**D2 — Identity.** A financial portfolio has its own UUID. It is not automatically linked to the legacy singleton `current_portfolio`. The ledger is authoritative only for its own financial facts and replayed balances; no bidirectional synchronization or competing balance projection is introduced.

**D3 — Sequence.** Persist sequences as positive `BIGINT`, from 1 through 9223372036854775807 inclusive. Check range before SQL insertion. The pure domain continues to accept any positive Python `int`; the narrower persistence boundary must be explicit. Exhaustion rejects a new append without modifying the ledger.

**D4 — Idempotency.** The caller supplies a stable, non-nil operation UUID, unique within a financial portfolio. Same UUID and equivalent canonical client facts returns the original operation. Different canonical facts gives an explicit conflict. Sequence and `recorded_at` are server-assigned and excluded from this comparison.

**D5 — Recording time.** Capture UTC `recorded_at` once for a successful append, within its write transaction; require `recorded_at >= executed_at`. Persist it unchanged. A retry returns the stored timestamp. A rejected or rolled-back attempt creates no recorded fact.

The domain remains pure: it imports no asyncpg, SQLAlchemy, FastAPI, or PostgreSQL types. `PostgresFinancialLedgerRepository` owns transactional append orchestration, including locking, idempotency checks, strict row mapping, historical replay, inserts, and the sequence update. The `FinancialLedgerRepository` protocol exposes `create_portfolio`, `load_portfolio`, `load_operations`, and atomic `append`; it does not expose partial commits. Mapping reconstructs every value object and exact posting tuple, rejecting corrupt rows before replay.

## PostgreSQL v4 schema

All three tables live in the **existing PostgreSQL schema v4**. Names are separate from legacy snapshot tables. `NUMERIC` below means unconstrained `NUMERIC`, never `FLOAT`, `REAL`, or `DOUBLE PRECISION`. The implemented `financial_decimal_valid` SQL function rejects non-finite values, out-of-range magnitudes, excessive scale, and excessive normalized coefficient length; each financial numeric column also has its sign check. Python validation remains necessary because SQL numeric normalization cannot enforce every Decimal representation rule. All UUIDs must be non-nil; text asset identifiers must match the domain's uppercase ASCII 1–20-character pattern.

| Table | Columns and types | Keys, checks, and indexes | Mutation policy |
| --- | --- | --- | --- |
| `financial_portfolios` | `id UUID NOT NULL`; `last_sequence BIGINT NOT NULL DEFAULT 0` | PK `id`; non-nil ID; `last_sequence BETWEEN 0 AND 9223372036854775807`. | Only `last_sequence` changes, under row lock, in the same append transaction. Creation is explicit. |
| `financial_operations` | `portfolio_id UUID NOT NULL`; `operation_id UUID NOT NULL`; `sequence BIGINT NOT NULL`; `operation_type TEXT NOT NULL`; `executed_at TIMESTAMPTZ NOT NULL`; `recorded_at TIMESTAMPTZ NOT NULL`; `asset TEXT NOT NULL`; `quantity NUMERIC NOT NULL`; nullable `known_cost_usdt NUMERIC`, `incomplete_quantity NUMERIC`, `completeness TEXT`, `principal_usdt NUMERIC`, `proceeds_usdt NUMERIC` | PK `(portfolio_id, operation_id)`; FK `portfolio_id → financial_portfolios(id)`; UNIQUE `(portfolio_id, sequence)`; CHECK positive sequence, non-nil operation ID, `recorded_at >= executed_at`, canonical asset, positive quantity, valid type, and type-specific field/null/sign rules. Index `(portfolio_id, executed_at, sequence)` for replay. | INSERT only; reject UPDATE, DELETE, and TRUNCATE. |
| `financial_postings` | `portfolio_id UUID NOT NULL`; `operation_id UUID NOT NULL`; `ordinal INTEGER NOT NULL`; `asset TEXT NOT NULL`; `delta NUMERIC NOT NULL`; `role TEXT NOT NULL` | PK `(portfolio_id, operation_id, ordinal)`; composite FK `(portfolio_id, operation_id) → financial_operations`; CHECK positive ordinal, canonical asset, nonzero valid Decimal delta, and allowed role. FK index is supplied by the PK's leading columns. | INSERT only; reject UPDATE, DELETE, and TRUNCATE. |

Operation row checks must allow exactly these shapes: opening has `known_cost_usdt` nullable and `incomplete_quantity` plus `completeness` required, with principal/proceeds null; buy has positive `principal_usdt` and no opening/proceeds fields; sell has positive `proceeds_usdt` and no opening/principal fields. Buy/sell asset cannot be USDT. Opening completeness rules mirror `OpeningFacts`: complete means known cost and zero incomplete quantity; unknown means null cost and incomplete quantity equal to quantity; partial means known cost and incomplete quantity strictly between zero and quantity. USDT opening is complete with known cost equal to quantity. Domain reconstruction remains the final type and posting check.

A composite foreign key prevents cross-portfolio postings. It does **not** guarantee that an operation has all required postings. Implemented deferred constraint triggers, evaluated at transaction end, check that each inserted operation has exactly one opening posting or exactly two trade postings, with ordinals, roles, assets, and signed amounts matching the operation facts. Real PostgreSQL tests cover missing, extra, duplicate, and contradictory rows, posting-only insertions, multiple operations in one transaction, and failure timing. Database CHECK constraints are row-local; the deferred triggers check cross-row completeness; `LedgerOperation` reconstruction checks domain consistency; `replay` checks historical balances and cost invariants. None replaces the others.

The implemented immutable-record triggers reject operation and posting UPDATE, DELETE, and TRUNCATE, including direct TRUNCATE CASCADE. Production still requires a separate restricted runtime role and privileged migration role. The runtime role must not own the financial tables or schema, have schema ALTER authority or CREATE privilege, or have TRUNCATE or UPDATE/DELETE privileges on immutable operations/postings. Grant only necessary SELECT/INSERT and controlled portfolio sequence updates, then verify effective privileges. These grants and role checks are not implemented here. A sufficiently privileged database administrator can still alter history. Corrections require a future, separately designed append-only operation.

## Decimal compatibility

`financial.values.exact_decimal` requires a finite `Decimal`, at most 60 coefficient digits, `adjusted() < 60`, and exponent at least -24. Quantity and USDT amount value objects are nonnegative; operation facts require positive quantity/principal/proceeds. Posting deltas may be signed but cannot be zero. Replay uses a 120-digit half-even calculation context and rounds proportional allocations to a 24-place quantum; PostgreSQL must store facts, not recalculate allocations.

PostgreSQL `NUMERIC` has ample range for these values. An unconstrained column does not round input to a declared scale. The implemented SQL predicate rejects `NaN`, positive/negative infinity, magnitudes at or above `1e60`, values with scale above 24, and normalized coefficient lengths above 60; field checks enforce signs. The 60-coefficient-digit rule depends on Decimal representation, not numeric value alone: SQL numeric comparisons cannot fully enforce that input representation. Python validates before write and after read, so the database predicate is a storage guard rather than a claim of complete equivalence. Never convert through `float`.

Numeric equality, rather than Decimal tuple/exponent equality, defines idempotent financial equivalence: `1`, `1.0`, and `1E+0` are the same value after each independently passes input validation. Signed `-0` compares equal to `0` and may be canonicalized to unsigned zero; zero remains invalid for nonzero postings and positive fields. Distinct values, including one quantum apart, conflict. Scientific notation is accepted only when its Decimal value satisfies the domain checks. Real PostgreSQL tests must round-trip `1e59`, 60-digit coefficients, `1e-24`, 24-place values with trailing zeros, signed zero, high-magnitude fractions, and values just outside each bound. `NUMERIC(60,24)` is too narrow for a valid 60-digit integer; `NUMERIC(84,24)` would coerce scale and is not the selected policy.

## Canonical retry content

Compare the validated client-supplied tuple `(portfolio_id, operation_id, operation_type, executed_at, facts)` after constructing domain value objects. Asset IDs compare by canonical string; execution times compare as the same UTC instant at PostgreSQL's microsecond resolution; decimal fields compare by exact numeric value, not their textual notation. Reject non-UTC or precision-losing timestamps before persistence. Fact fields are:

- Opening: asset, quantity, nullable known USDT cost, incomplete quantity, completeness. Null cost is distinct from known zero cost.
- Buy: asset, quantity, principal in USDT.
- Sell: asset, quantity, proceeds in USDT.

Type, timestamp, asset, presence/nullness, completeness, or any financial value difference is a conflict. Do not compare server sequence, recording time, row metadata, or newly generated postings. On a concurrent duplicate, the second writer waits for the portfolio row lock, then reads the committed operation and returns it if equivalent. If the first transaction rolled back, the second may append normally. No automatic retry is implied; a caller that retries must reuse the same UUID.

## Append transaction and replay

Use the existing asyncpg transaction context and PostgreSQL `READ COMMITTED` isolation for writes. After validating the request, one transaction: (1) locks the financial portfolio row with `SELECT ... FOR UPDATE`; (2) loads and strictly maps all persisted operations and postings, checks contiguous sequences, and replays the complete history; (3) checks the operation UUID against validated client facts, returning an equivalent stored operation or rejecting a conflict; (4) for a new operation, allocates the next positive BIGINT sequence, captures UTC recording time, and constructs exact postings; (5) replays history plus the candidate, ordered by `(executed_at, sequence)`; (6) inserts the operation and postings; (7) advances `last_sequence`; (8) commits. A duplicate returns the original stored operation without advancing sequence or replacing its timestamp.

Any exception, failed domain replay, constraint violation, timestamp invalidity, database failure, or sequence exhaustion rolls back all writes and the sequence advance. Backdated operations must replay against every historical prefix; a later positive balance cannot excuse an earlier negative balance. The row lock serializes same-portfolio appends, including the initially empty ledger, while separate portfolios can append independently. No distributed lock is needed. A read during append sees committed data; when operations and postings require multiple SELECTs, use one consistent read-only transaction snapshot (or one joined query) to avoid mixing revisions. Full replay is intentionally used at this stage; checkpoints and materialized balances are deferred.

## Migration and rollout

`POSTGRES_MIGRATIONS[4]` and `POSTGRES_SCHEMA_VERSION = 4` are implemented without editing v1–v3. Empty initialization applies v1–v4 in order; an existing v3 database gains the new tables, constraints, indexes, and triggers without altering legacy rows. The schema advisory lock and transaction make upgrade atomic. Legacy HTTP readiness, Scheduler dispatch, and ordinary PostgreSQL CLI commands validate complete v3 or v4 without migration. Financial repository operations validate complete v4, including enabled immutable-ledger triggers, before accessing financial tables. Explicit initialization remains migration-capable; `advisor setup` invokes it before portfolio confirmation, so treat setup on an existing v3 database as a planned migration action. Use a backed-up operator action and an authorized migration role for a v3→v4 upgrade. Reverting application code after a v4 migration must not assume an automatic downgrade; dropping ledger tables loses immutable financial history and requires an explicit backup and data-loss decision.

Readiness checks required legacy tables and columns at v3 and rejects partial financial objects.
At v4 it checks financial table and column shapes; each critical trigger's table, event,
timing, row/statement mode, function identity, enabled state, and constraint/deferred flags;
and the three supporting functions' language, return type, volatility, strictness, security
mode, and whitespace-normalized source against the migration definitions. This detects changed
function bodies with unchanged signatures. It does not prove every database constraint or
prevent privileged administrators from changing objects after validation. A restricted runtime
role remains mandatory.

## Acceptance matrix

| Stage | Required tests and gate |
| --- | --- |
| TG-7.5.5-2 | Real PostgreSQL 17 empty initialization and populated v3→v4 upgrade; unchanged old rows; PK/FK/unique/type/sign/NUMERIC checks; exact Decimal boundaries and round trips; update/delete denial; deferred posting completeness for missing/extra/wrong ordinal, role, asset, and amount; migration rollback. |
| TG-7.5.5-3 | Mapper rejects malformed or cross-portfolio rows, reconstructs exact immutable facts/postings, distinguishes null cost from zero, preserves timestamps and numeric values; pure-domain tests still pass. |
| TG-7.5.5-4 | Atomic append/reload/replay equivalence; rollback after operation or partial posting insertion; identical and conflicting retries; uncertain-commit retry with original sequence/time; backdated invalid history; BIGINT exhaustion; no partial commit. |
| TG-7.5.5-5 | Real PostgreSQL same-portfolio competing appends and duplicate submissions, independent portfolios, read/write consistency, cross-portfolio isolation, CI integration and final security review. SQLite/mocks cannot prove PostgreSQL locking or NUMERIC behavior. |

## Learning notes and deferred work

asyncpg fits this stage because the project already uses it for transaction and migration lifecycles. A row lock makes competing writers to one portfolio wait until the first transaction completes; it does not validate accounting. A transaction commits all operation/posting/sequence writes or none; it does not by itself prove that the intended posting set was complete. Database constraints guard storage shape, while deterministic replay remains authoritative for historical financial validity. SQLAlchemy could provide mapped models and session management, but would still require the same domain mapping, lock, constraints, and replay; adding it only here would duplicate existing infrastructure.

Deferred: fees, transfers, corrections, multi-user ownership and API authorization, legacy snapshot migration, trading execution, portfolio read/write HTTP endpoints, pooled connections, materialized balances, and replay checkpoints. SQL precision, deferred triggers, timestamp precision, and BIGINT sequence exhaustion have local integration coverage. Restricted production runtime/migration roles and their effective privilege verification remain open, as do replay performance at production scale beyond small local characterization and reconciliation after an unknown COMMIT outcome. None of these deployment gates overrides D1–D5.
