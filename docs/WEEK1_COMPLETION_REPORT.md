# Week 1 Completion Report — Track 5 · Tunihara-MedGuard (Group G11)

**Date:** 2026-11-17 · **Scope:** approved Week 1 plan only · **Environment:** Windows 11, Python 3.12.10, Django 6.0.10,
PostgreSQL 16.15 and Redis 7 in Docker Desktop (29.3.1).
**Result:** 80/80 automated tests pass (0 skipped, 0 failed, run twice). Docker **image builds** for the app services could not be verified (see §6).

## 1. Initial state and upstream verification
* The project folder is **not a git repository**, so worktree isolation was impossible; work was done in place.
* Upstream `ChaoukiBayoudhi/tunisovereign-ds2-starter-template` was cloned to a separate temp directory (single commit
  `531909b`, 2026-11-08). `diff -r --strip-trailing-cr` against the local copy: **no differences** (only CRLF line endings).
  That clone served as the baseline for the change list below.
* All Week 2–4 files are unchanged (verified by diff): `agent/langgraph_agent.py`, `agent/skills/*`, `mcp_server/*`,
  `backend/tunisovereign_core/ml_anomaly.py`, `contracts/*`, `dashboard/*`, `workflows/*`.

## 2. Evidence per Week 1 requirement

| # | Requirement | Evidence (all executed) |
|---|---|---|
| 1 | Django 6 foundation | `manage.py check` → *no issues*; Django 6.0.10; `config/{settings,urls,asgi,wsgi}.py`; test `test_django_system_checks_pass` |
| 2 | PostgreSQL + Redis | `test_postgresql_connectivity` (`connection.vendor == "postgresql"`, `SELECT version()` = PostgreSQL 16.15); `test_redis_connectivity_and_django_cache_backend` ran (not skipped): PING + `RedisCache` set/get; compose `db`/`redis` containers *healthy* |
| 3 | Models + migrations | 10 models, `0001_initial.py` applied on PostgreSQL; `makemigrations --check` → *No changes detected*; `test_initial_migration_applied_all_tables` (starter table names absent) |
| 4 | Seed command | `manage.py seed_tunihara` → 12 facilities, 7 medications, 71 inventory positions, 6 users, 315 prescriptions; second run adds 0 (idempotent); `test_seed_command_is_idempotent_and_synthetic` |
| 5 | Four-role JWT | `test_four_roles_login_and_claims[×4]` (role, governorate, facility_id, wallet_address, dialect claims) |
| 6 | JWT refresh | `test_jwt_refresh`, `test_refresh_rejects_garbage_and_access_token` |
| 7 | Unauthorized rejection | 11 endpoints × no token → 401; invalid/expired token → 401; inactive user → 401; GraphQL 401; gRPC `UNAUTHENTICATED` |
| 8 | Role + facility/governorate isolation | `test_facility_isolation_between_roles`, `test_out_of_scope_objects_are_404_not_403`, scoped telemetry/inventory/prescriptions/anomalies/redistribution tests; same rules in GraphQL (`test_graphql_respects_role_scope`) and gRPC (`NOT_FOUND` for out-of-scope nodes) |
| 9 | REST CRUD + validation | 21 tests: role matrix per resource, immutable fields, server-controlled fields (`tx_hash`, `signature_verified`, `status`, `proposed_by`, `current_stock_units`) cannot be written, pseudonym format, value/timestamp validation, redistribution workflow, user admin |
| 10 | GraphQL nested queries | `test_nested_query_returns_data` (facilities → inventory → medication / facility / latestReadings / anomalies) |
| 11 | DataLoader query count | `test_dataloader_prevents_n_plus_one`: **6 SQL queries for 5 facilities and 6 for 17 facilities** (constant; 1 root + 5 batched loaders) |
| 12 | gRPC unary | `test_unary_*` (GetInventoryStatus, TriggerUrgentAudit): real in-process server + generated stubs; typed `InventoryStatus` / `AuditResponse` messages |
| 13 | gRPC client-streaming | `test_stream_*`: typed `IngestAck`; 1,200-packet stream exercises 500-packet chunking and reject-index offsets |
| 14 | Real PostgreSQL persistence | raw SQL `SELECT count(*), bool_and(signature_verified) … FROM tunisovereign_core_telemetryreading` → `(7, True, 'gRPC')`; live run persisted 7,080 rows in the dev DB |
| 15 | HMAC validation / rejection | tampered value, wrong secret, empty, garbage, signature replayed on another node/time → `INVALID_SIGNATURE`, nothing stored; unsigned/invalid format/metric/range/NaN/naive/future timestamp/unknown node/governorate mismatch → coded rejections |
| 16 | Simulator → gRPC end-to-end | `test_simulator_to_grpc_to_postgresql_end_to_end`: seed → simulator → real gRPC → PostgreSQL, `accepted == len(packets)` (>1,000), replay = 0 new rows. **Manual live run** (separate Daphne + gRPC processes, REST login for the AGENT_SERVICE token): `IngestAck: accepted=7080 duplicates=0 rejected=0` |
| 17 | UML consistency | `test_uml_consistency.py` (6 tests): diagram classes = the 10 models, diagram attributes exist on models, drawn relationships = actual foreign keys, RPC names exist in the proto, documented HTTP routes resolve, roles are real |
| 18 | Live HTTP smoke test | curl against running Daphne: unauthenticated 401; COOP sees 1 facility, auditor 2, admin 12; COOP `POST /facilities/` → 403; COOP GraphQL returns only its facility with 5 positions |

Test files (80 tests): `test_auth.py` 24, `test_rest_scope_crud.py` 21, `test_grpc_ingest.py` 10, `test_simulator_e2e.py` 8,
`test_graphql.py` 6, `test_uml_consistency.py` 6, `test_infrastructure.py` 5.

## 3. Commands executed (main ones)
```text
git clone --depth 1 https://github.com/ChaoukiBayoudhi/tunisovereign-ds2-starter-template <tmp>/upstream ; diff -rq --strip-trailing-cr
python -m venv .venv ; pip install  (django 6.0.10, DRF 3.18.3, simplejwt 5.5.1, strawberry-graphql 0.341.2, grpcio 1.85.1, protobuf 7.36.2, psycopg 3.3.9, pytest 9.2.0 …)
docker compose up -d db redis                     # db healthy on 127.0.0.1:5433 (5432 was already in use), redis healthy
python manage.py check ; makemigrations ; migrate ; makemigrations --check --dry-run
python manage.py seed_tunihara (twice)
python scripts/gen_proto.py
python -m pytest backend -v                       # 80 passed in 250 s (earlier run: 80 passed in 317 s)
python -m daphne -p 8010 … & python -m grpc_service.server (GRPC_PORT=50061) &
python -m simulator.tunisian_simulator --mode grpc --hours 24 --target 127.0.0.1:50061 --api-url http://127.0.0.1:8010
curl … /api/auth/login/, /api/v1/facilities/, /api/v1/telemetry/, /graphql/
docker compose config -q                          # valid
docker compose build web                          # FAILED – see §6
```
Test fixes made along the way (all test-side or hardening): disallowed methods now answer 405 instead of 403; a test helper compared
int ids to strings; one wrong expected count; Strawberry extension deprecation (`lambda: QueryDepthLimiter(...)`).

## 4. Changed / new files (vs upstream baseline)
**Modified starter files** (justified): `backend/tunisovereign_core/models.py` (water → Track 5, renames), `api_rest.py`
(decoupled from the broken agent, role matrix, scoping), `schema_graphql.py` (fake data → real ORM + DataLoaders),
`grpc_service/server.py` (non-functional servicer → real service, no agent call), `protos/tunisovereign.proto`
(backward-compatible v2), `simulator/tunisian_simulator.py` (water → medication telemetry, real gRPC client),
`Dockerfile` (Python 3.12, non-root, Django entrypoint), `docker-compose.yml` (web/grpc/simulator, healthchecks, volume,
loopback-bound ports; Week 2–4 services kept), `requirements.txt` (split), `.env.example` (placeholders), `.gitignore`, `README.md` (Week 1 section added; original text kept).
**New:** `manage.py`, `pytest.ini`, `.dockerignore`, `config/{__init__,settings,settings_test,urls,asgi,wsgi}.py`,
`requirements/week1.txt`, `scripts/gen_proto.py`, `grpc_service/tunisovereign_pb2{,_grpc}.py`,
`backend/tunisovereign_core/{apps,constants,scope,permissions,serializers,pagination,urls,graphql_view,signing,telemetry_ingest,seed_data}.py`,
`…/management/commands/seed_tunihara.py`, `…/migrations/0001_initial.py`, `…/tests/*` (8 files),
`docs/uml/{class_diagram,sequence_diagram}.md`, `docs/WEEK1_COMPLETION_REPORT.md`, empty `__init__.py` in packages
(`backend`, `agent`, `agent/skills`, `mcp_server`, `grpc_service`, `simulator`, `contracts`). Local only, git-ignored: `.env`, `.venv/`.
`docs/PROJECT_AUDIT_TRACK5.md` is the earlier audit.

## 5. Security controls implemented
Deny-by-default `RoleMatrixPermission`; row scoping in one module shared by REST/GraphQL/gRPC; no `AllowAny` on anything sensitive (only
register, login/refresh and `/health/`, the first three throttled); self-registration can never choose role/facility; explicit serializer
fields; server-controlled fields read-only; `is_verified_on_chain` default `False`; JWT required on every gRPC call, telemetry stream
restricted to `AGENT_SERVICE`; constant-time HMAC check done before any DB lookup; idempotent unique constraint; secrets only from
environment (`require_env`), none logged; GraphQL depth limit 8, GraphiQL off, JWT required; containers run as non-root; DB/Redis ports bound to 127.0.0.1;
password validators (min 10); pseudonym-only patients with `is_simulated` everywhere.

## 6. Remaining limitations (stated plainly)
1. **Docker app images not verified.** `docker compose build web` failed: `pip` inside the container gets
   `CERTIFICATE_VERIFY_FAILED` for pypi.org (HTTPS interception on this machine's network, CA not trusted by the container). I did not bypass TLS
   verification. Verified instead: base image is Python 3.12.16, `docker compose config` is valid, `db`/`redis` run healthy from compose and
   were used by every test; the application ran under host Python 3.12.10. **Not verified:** the `web`, `grpc`, `simulator` (and Week 4 `dashboard`) containers.
   Retry on another network or add the corporate CA to the build.
2. gRPC TLS is implemented (`GRPC_TLS_CERT_FILE/KEY_FILE`, client `GRPC_TLS_CA_FILE`) but **untested**; default is plaintext.
3. One shared HMAC secret for all producers (no per-device keys/rotation); past timestamps are accepted (backfill) – replays are neutralised by idempotency, future skew ≤ 300 s.
4. REST interprets naive timestamps as Africa/Tunis (DRF default); gRPC rejects them.
5. Login takes ~8–10 s on this machine (Django's 1.2 M-iteration PBKDF2); simulator login timeout is 90 s.
6. `anomalies` DataLoader returns all events per inventory (no per-inventory cap); GraphQL introspection is on.
7. No per-user lockout beyond the `auth` rate throttle (20/min per IP by default).
8. Mermaid diagrams are Markdown sources; PNG/SVG export for the slides is a manual `mermaid-cli` step.
9. Starter's `strawberry-graphql-django` is not used (core Strawberry Django view suffices) and was dropped from the requirements.
10. The unauthenticated `darja_intent_webhook` was removed from the REST routes (it called the broken agent); it must be reintroduced in Week 3 behind `AGENT_SERVICE` auth.

## 7. Known incompatibilities for Weeks 2–4 (components deliberately left untouched)
* `agent/langgraph_agent.py:52-57` calls `calculate_sovereign_quota_allocation(soil_moisture_pct=…, temp_c=…)` but the MCP tool takes
  `(current_metric_value, ambient_temp_c, …)` and returns `recommended_units`, while the agent reads `approved_units`/`max_daily_quota` → crashes. It also is not LangGraph.
* Agent state, skills, MCP policy table, `ml_anomaly.py` and the dashboard are water/agri-specific (moisture, litres, 8 "hubs").
  Week 2 should train on the simulator's labelled output (`is_anomaly_injected`, `anomaly_kind`) and read `TelemetryReading`.
* `contracts/web3_zk_client.py` and the ZK skill fabricate `tx_hash`/`block_number`; the Solidity function has no access control and stores quota values in clear.
* `workflows/*.json` has empty `connections`, targets `http://localhost:8000/api/v1/external-context/` (endpoint does not exist) and Open-Meteo ET0 (irrelevant).
* Governorate codes are now canonical (`Sidi_Bouzid`, `Ben_Arous`, `Kef`…, see `constants.GOVERNORATES`). The Week 2–4 code only knows a handful of governorates; the skills' parser and MCP policy table must be aligned to that list.
* Week 2–4 packages (channels, langgraph, mcp, web3, streamlit …) were not installed or checked against Django 6.

## 8. Next steps (Week 2 hooks already in place)
`TelemetryReading` (idempotent, indexed), simulator ground-truth labels, `AnomalyAndForecastEvent` (nullable score/forecast), `AgentLoopExecution`/`OnChainZKReceipt` schema,
`TriggerUrgentAudit` stub returning an empty `tx_hash`, and the `anomaly_triggered` / `agent_thread_id` ack fields.
