# QA evidence and release verification

A green unit/browser workflow is not evidence of real Hermes Desktop acceptance. A release handoff must bind the full source commit, finite plugin-payload hash, host commits, platform results, real Desktop artifacts, independent review, and catalog pin. Those receipts are produced after the source is frozen; this file does not embed a self-referential release SHA.

## Pinned hosts

| Target | Exact commit | Role |
| --- | --- | --- |
| Hermes v0.23.0 | `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19` | Claimed released compatibility floor |
| Hermes development main | `c8d4e43efd296f632116cf3f17738aa2c4838460` | Development pin resolved at startup |

The stable tag is annotated; the commit above is its resolved source commit, not the tag object. Both pins were checked with disposable homes, public plugin registration, CLI discovery, standalone API authentication/scope checks, native skill discovery/view/edit, and native locks/approvals. The checked host Python metadata is 3.11–3.14. Local product tests use Python 3.13.16; the real-host managed environment uses Python 3.14.9 on macOS 27.0.1 arm64. No production Hermes profile is part of the test fixtures.

## Recorded development checks

- Local source through content-store flush hardening and fresh namespaced-loader and concurrent-idempotency regressions: **174 passed, 12 platform-specific skips**, one upstream FastAPI/httpx test-client deprecation warning. Ruff check/format and mypy passed. The skips are not passes for those platform paths.
- Browser harness: **14 passed**, including escaped untrusted text, explicit conflict resolution with captured sides/proposal, review-hash publication, stale/error states, profile-switch response rejection, recovery controls, and narrow/desktop layouts. This harness uses SDK fixtures and cannot establish real host compatibility.
- Sanitized CI source `ac0b2c3b21250e14031bdc83a64da15de4e9d31e`: all six jobs passed in [run 42444854073](https://github.com/asimons81/hermes-skill-layers/actions/runs/42444854073): Ubuntu Python 3.11 and 3.14, macOS Python 3.13, native Windows Python 3.13, static checks, and Chromium UI tests. Later candidate changes require their own workflow run; inspect Actions on the actual catalog-pinned source commit.
- A real Hermes Desktop smoke test selected the synthetic release-notes source, prepared a full derivative review, published it, and displayed the exact native selector. The release gate additionally requires the complete refresh/conflict/rollback journey from a clean candidate.
- Named-profile release validation uses one disposable Hermes root/runtime and a physical plugin copy in each real named profile. The second profile must start with an independent source/layer scope and derive/publish independently. The source layout uses a normal Python package entry for the Dashboard API and virtual dependency members; no installed self-distribution is needed. Exact install, disable/re-enable, remove, and source-preservation receipts belong to the final candidate run.

The independent review reproduced a destination-parent swap, incomplete-stage retry failure, hardlink object crash window, and missing conflict review sides. Fixes added held-parent no-replace renames, preserved partial-stage abort/retry, atomic object moves, and captured B/L/U/proposal previews. The reviewer reran focused service/store tests and a browser conflict-review test on the corrected implementation. Final review and leader sanity must identify the frozen candidate separately.

## Coverage map

| Requirements | Evidence location |
| --- | --- |
| T01–T02, T07–T09: capture/identity/paths/limits/modes | `tests/unit/test_identity.py`, snapshot/source/Windows tests and independent fixtures |
| T03–T06, T22: three-way merge, explicit decisions, Git failures | merge tests, `test_service.py`, CLI/API and browser tests |
| T10–T14, T18: stale inputs, concurrency, crashes, no-clobber, rollback | service/publication/store tests and `test_fault_acceptance.py` |
| T15, T19–T20: profile boundaries, read-only tools, text-only UI | profile/store/API tests, browser profile-switch tests, real named-profile runner |
| T16–T17, T23: public loader and native tools/policy | real-host `scripts/qa.py` reports on both host pins |
| T21: restart/migration/lifecycle | store migrations, service reopening, and real-host disable/re-enable/remove runner checks |
| T24: intended package, exact pin, source/version/capabilities | clean checkout audit, host validator, catalog validator and actual CI receipts |
| T25: no plugin-owned external runtime calls | dependency/subprocess inspection and socket-denied real-host derive/refresh/publish/rollback workflow |

Injected `ENOSPC`, `EACCES`, and `EXDEV` errors test error/recovery decisions. They are not proof of every real filesystem's permission, mount, power-loss, or durability behavior. WSL has no separate acceptance run. No autonomous background learning or behavioral quality improvement is claimed.

## Reproduce the checks

Use a clean plugin checkout, supported Python, local Git, and isolated pinned Hermes contributor environments. Do not link a development project containing private `work/` into a Hermes profile. The QA runner copies only an allowlisted finite payload and creates disposable real named profiles under its artifacts directory. It reuses only a host-commit/platform-specific tool and download cache under `<artifacts>/.qa-runtime/`; profile homes, identities, configuration and plugin history remain new for each run. The report records the configured and canonical cache paths. An explicitly preseeded cache symlink must resolve to `work/host-runtime` or `work/release-runtime` within the plugin checkout (these may themselves point to an already verified task-owned PM store). This reuses the public `HERMES_RUNTIME_DIR` mechanism without copying or moving PM-managed state. Never point these test paths at a production store.

```sh
python -m pip install -e ".[dev]"
python -m ruff check .
python -m ruff format --check .
python -m mypy skill_layers dashboard
python -m pytest -q tests/unit tests/integration
npm ci
npx playwright install chromium
npx playwright test
python scripts/qa.py --hermes-root /absolute/pinned/hermes --suite integration --artifacts /absolute/qa-results
python scripts/qa.py --hermes-root /absolute/pinned/hermes --suite golden --artifacts /absolute/qa-results
hermes plugins validate /absolute/clean/plugin --install-deps
```

Repeat integration for each host pin. The runner verifies the actual requested host checkout and module path, records full commits and a payload hash, keeps environment changes in child processes, and returns nonzero for failed or required-but-unexecuted checks. Golden validation requires a real Desktop evidence manifest under the artifacts directory with matching commits/payload hash and independently hashed artifacts for every required journey step. Backend-only success cannot satisfy it.

The outbound-denied check covers plugin-owned local workflows, not the surrounding Hermes application's own provider, update, or catalog transport. Desktop acceptance uses Hermes' existing authenticated transport; Skill Layers starts no separate server and has no telemetry or model client.
