# Branch migration inventory: `feat/agent-design-docs`

Inventory only. This document does not merge, rebase, or delete
`feat/agent-design-docs`; it records a disposition for every unique component
so a future decision (if any) has evidence instead of guesswork.

## Method

- `git log main..origin/feat/agent-design-docs`: **199 commits** unique to the
  branch. `git log origin/feat/agent-design-docs..main`: **66 commits** unique
  to `main`, confirming genuine divergence, not a simple fast-forward gap.
- `git diff main...origin/feat/agent-design-docs --stat` (three-dot: diffs from
  the merge base `c7ab628` to the branch tip): **171 files changed, 55509
  insertions(+), 2548 deletions(-)**. Because this is a merge-base diff, it
  enumerates what the branch added or removed relative to the fork point, not
  relative to `main`'s current tip; it does not by itself say whether `main`
  already has equivalent behavior. Every row below was checked with `git show
  main:<path>` and `diff`/`git diff main origin/feat/agent-design-docs --
  <path>` directly against `main`'s current tip (`e2f8d4b`) before assigning a
  disposition. Given the branch's scale, structural files were diffed in full
  and homogeneous groups (large test-file sets, dependency pin lists) were
  verified by file-existence and line-count comparison rather than a
  line-by-line read of every file; that method is noted per row.

- Repository identity: `main` HEAD `e2f8d4b`, 4 commits ahead of the
  `dcec23a` the original `PRE_IMPLEMENTATION_AUDIT.md` scoped itself to (all 4
  are that audit's own PR, no executable-code change). `feat/phi-infrastructure-v2`
  (this branch) forks directly from `main`'s current tip. `feat/agent-design-docs`
  forks from the older common ancestor `c7ab628` and never merged back.

## Summary finding

`feat/agent-design-docs` is an **older, superseded snapshot** of the same
backend/frontend PHI Console application, not a parallel feature branch with
unmerged capability. Every structural component checked (agent roster,
`server.py` routes, `phi_core` modules, test suite, frontend) already exists
on `main` in an equal-or-more-developed form. The branch's one architecturally
distinctive move -- deleting `phi_engine`, root `tests/`, `harness/`,
`generators/`, and `benchmarks/` to pivot the repo to backend/frontend only --
directly contradicts this rewrite's ground rule that `phi_engine`/root
`tests/`/`harness/`/`authorities/` are out of scope and never edited, and must
not be propagated.

## Disposition table

| Component | Files (representative) | Does `main` already have the behavior? | Useful and unmerged? | Disposition |
|---|---|---|---|---|
| Agent roster (`agents/*.py`) | `base.py`, `experts.py`, `manager.py`, `operator.py`, `orchestrator.py`, `outward.py`, `reasoning.py`, `reviewer.py`, `specialists.py`, `batching.py`, `__init__.py` | Yes. All 10 files exist on `main`, verified by direct `git show main:<path>` + line-count/diff comparison. `main`'s versions are larger for every file except `llm.py` (see below): `base.py` 375 vs 268 lines (wired to `control/context.py`, the current control-plane), `orchestrator.py` 1140 vs 780, `reasoning.py` 1787 vs 1581, `experts.py` 641 vs 492, `operator.py` 407 vs 348, `reviewer.py` 204 vs 178, `outward.py` 343 vs 243. | No | KEEP (on `main`; no action) |
| `agents/llm.py` | `llm.py` | Yes, deliberately smaller: `main` 74 lines vs branch 228 lines. Per `PRE_IMPLEMENTATION_AUDIT.md` Section 7, `main` intentionally reduced `llm.py` to config/parsing only after moving all inference through the single `ProviderGateway.complete` choke point; the branch's 228-line version still contains the pre-refactor direct-call pattern. | No -- the branch's larger version is the security regression `main` deliberately fixed. | DELETE (do not restore direct-call logic into `llm.py`) |
| `agents/cache.py` (web-fetch cache helper: `cache_get`/`cache_put` against a Mongo `web_cache` collection, 7-day manual staleness check) | `cache.py` | Functionally yes, architecturally superseded: `main` has no standalone `agents/cache.py`, but `control/context.py:88,126-127` implements the same `web_cache` read/write against `AgentContext`, and `control/migrate.py:63-71` creates a native Mongo TTL index (`web_cache.fetched_at`, `expireAfterSeconds`) instead of the branch's manual `datetime.now() - fetched_at > timedelta(days=7)` comparison in Python. | No -- `main`'s TTL-index approach is strictly better (expiry enforced by the database, not by every caller remembering to check) and already covers every call site the branch's version served. | DELETE (superseded by `control/context.py` + `control/migrate.py`'s native TTL index) |
| `server.py` | `backend/server.py` | Yes. `main` 3648 lines vs branch 2646 lines. Route-level check: extracted every `@app.*`/`@router.*("...")` decorator from both (44 on the branch, 47 on `main`); `comm -23` (branch-only routes) is empty -- every route the branch exposes already exists on `main`, which additionally exposes 3 more. | No | KEEP (on `main`; no action) |
| `phi_core/*.py` (non-agent modules: `anonymizer.py`, `bundle.py`, `chatgpt_auth.py`, `coverage_matrix.py`, `crypto.py`, `db.py`, `detectors.py`, `docx_safe.py`, `file_readers.py`, `intake.py`, `jurisdictions.py`, `llm_catalog.py`, `models.py`, `paths.py`, `publish_guard.py`, `security.py`, `validation.py`, `__init__.py`) | 18 files | Yes, all 18 exist on `main`; direct diffs show `main` at parity or larger for every file that differs (`crypto.py` 201 vs 187, `paths.py` 161 vs 96, `publish_guard.py` 510 vs 352, `security.py` 445 vs 405, `bundle.py` 677 vs 659); 8 of the 18 are byte-identical (`__init__.py`, `coverage_matrix.py`, `db.py`, `docx_safe.py`, `file_readers.py`, `jurisdictions.py` -- diff-lines `0`). | No | KEEP (on `main`; no action) |
| `phi_corpus/*` (synthetic-corpus generation and benchmark harness: `benchmark.py`, `campaign.py`, `scenarios.py`, `tiers.py`, `verify.py`, `replay.py`, `report.py`, `researcher.py`, `realism.py`, `edge_cases.py`, `generate.py`, `planters.py`, `study_data/`) | 17 files | Yes. `main`'s `backend/phi_corpus/` already contains this module (confirmed present in the current repo tree: `tiers.py`, `verify.py`, `replay.py`, `report.py`, `researcher.py`, `scenarios.py`, `benchmark.py`, `campaign.py`, `realism.py`, `study_data/`). Not diffed file-by-file (out of scope for this wave's file-ownership boundary and not touched by any Phase R defect), but existence is confirmed. | Not assessed at line level | REVIEW_REQUIRED (existence confirmed on `main`; a maintainer working in `phi_corpus/` should do the line-level diff this inventory skipped, since it is outside this wave's owned files) |
| `backend/tests/*.py` (legacy pre-control-plane test suite: ~50 files, e.g. `test_manager.py`, `test_operator.py`, `test_reviewer.py`, `test_agent_pipeline.py`, `test_publish_guard.py`) plus `tests/e2e/`, `tests/live/`, `tests/fixtures/`, `tests/corpora/` | 66 files under `backend/tests/` on the branch | Yes. `main` has 91 files under `backend/tests/` (branch has 66); every branch test-file basename (including subdirectory files under `e2e/`, `live/`, `fixtures/`, `corpora/`) is present on `main` -- `comm -13` of the basename sets is empty. Spot-checked fixtures directly: `backend/tests/fixtures/tb_collection_form.pdf` and `backend/tests/corpora/hipaa_categories.json` both exist on `main`. | No | KEEP (on `main`; no action) |
| `frontend/*` (React app: `App.js`, `components/ui.jsx`, `pages/SessionDetail.jsx`, `pages/Wizard.jsx`, `pages/Settings.jsx`, `lib/api.js`, Tailwind config, `package.json`) | 16 files on branch | Yes, and `main` is a strict superset: 19 files on `main` vs 16 on the branch. `frontend/src/App.js` is byte-identical between the two (`git diff main origin/feat/agent-design-docs -- frontend/src/App.js` produces no output). The 3 files `main` has beyond the branch are all tests (`SessionDetail.review.test.jsx`, `SessionDetail.stream.test.jsx`, `setupTests.js`). | No | KEEP (on `main`; no action) |
| `Dockerfile`, `docker-compose.yml`, `.dockerignore` | 3 files | Yes, byte-identical to `main` (`diff` produces no output for all three). | No | KEEP (on `main`; no action, already synced) |
| `.github/workflows/ci.yml` | 1 file | Partially. `main`'s workflow has 5 jobs (`test`, `xls-isolation`, `backend`, `credentialed`, `frontend`); the branch's has 2 (`backend`, `frontend`) -- it lost the `test` (root/`phi_engine` suite) and `xls-isolation` (pandas-version matrix) jobs when `phi_engine` was deleted, and never had the `credentialed` live-LLM job. | No | KEEP (`main`'s is a superset; the branch's is missing coverage `main` needs for `phi_engine`) |
| `backend/requirements.txt`, `backend/requirements-dev.txt` | 2 files | Yes, present on `main` with mostly newer pins (`aiohttp` 3.13.3->3.14.2 branch-newer, but `pydantic` 2.12.5 main-newer vs 2.9.2 branch, `openai` 2.24.0 main-newer vs 1.99.9 branch): ordinary independent dependency drift from two branches evolving separately, not a distinct capability. | No | KEEP (on `main`; no action -- this is pin drift, not missing functionality) |
| `backend/.env.example` | 1 file | Yes, byte-for-byte identical to the current file in this repo (115 lines both sides, `diff` produces no output, and the variable-name sets match exactly). | No | KEEP (already synced; the three `TRACE_RAW_*` flags added this wave are new on both sides going forward, unrelated to this branch) |
| `CLAUDE.md` (branch's full rewrite) | `CLAUDE.md` | No, structurally incompatible: the branch's `CLAUDE.md` (527 changed lines relative to the merge base) never mentions `phi_engine` (0 hits on a case-insensitive grep) because that branch deleted `phi_engine` outright. `main`'s current `CLAUDE.md` is explicitly a two-halves document ("two codebases sharing one git history... backend/frontend... and the standalone `phi_engine` pipeline package"). Merging the branch's version wholesale would delete the `phi_engine` half of the current instructions. | No | DELETE (do not merge; would regress `main`'s instruction coverage) |
| `README.md` (branch's full rewrite) | `README.md` | No, same incompatibility as `CLAUDE.md`: branch version is 78 lines and never mentions `phi_engine`; `main`'s current `README.md` is 325 lines and covers both halves. | No | DELETE (do not merge; would regress documentation coverage) |
| `docs/AGENT_ARCHITECTURE.md` | 1 file, 624 lines | No -- confirmed absent from `main` (also noted in `PRE_IMPLEMENTATION_AUDIT.md` Section 1). Its Level-0 mermaid diagram documents the pre-control-plane architecture: writes to the retired `agent_log` collection (Section 8 of the audit confirms `agent_log` is retired and migrated away) and a manual `web_cache` cache helper (superseded, see `agents/cache.py` row above). As written it describes behavior the current code no longer has. | Historically informative, but actively misleading if merged as current documentation. | REVIEW_REQUIRED (a maintainer should decide whether to adapt its Level-0 diagram as historical/prior-art context inside `docs/adr/`, which already covers the current architecture decisions; do not merge verbatim as live documentation) |
| `design_guidelines.json` (frontend design-token spec: typography scale, dark-mode-only theme, Tailwind class conventions) | 1 file | No -- absent from `main`. Frontend styling on `main` is out of scope for this backend-infrastructure wave; not diffed against `main`'s actual Tailwind usage. | Possibly, for a frontend design pass. | REVIEW_REQUIRED (frontend-owner decision, out of scope for Phase R backend work) |
| `scripts/cleanup.py` (git-ignored-artifact sweeper: dry-run by default, `--apply` to delete regenerable build output, `--all` for the `make distclean` tier) | 1 file | No -- absent from `main`, which only has `scripts/export_openapi.py`. Not the same concern as the runtime session-erasure/retention logic (`main` already has that, independently, via `RETENTION_DAYS`/`erasure_pending` in `server.py` and `control/artifacts.py`/`control/migrate.py`). This is a standalone repo-hygiene dev utility. | Possibly useful as a dev convenience, low production risk (touches only git-ignored paths, dry-run by default), but the repo's directory layout and `.gitignore` have both changed materially since the branch forked (e.g. `data/`, `docs/baseline/` did not exist there). | REVIEW_REQUIRED (small, low-risk, but needs re-verification against the current `.gitignore` before trusting `--apply`) |
| `memory/*.md` (`ARCHITECTURE.md`, `GOAL.md`, `PRD.md`, `TODO.md`, `VISION.md`, `test_credentials.md`) | 6 files | No -- absent from `main`. `main`'s current convention (stated in its `CLAUDE.md`) is that "planning, specs, and agent memory live outside the tree," which is exactly why `docs/MASTER_ARCHITECTURE_V2.md` is gitignored rather than committed. Committing a `memory/` directory to git directly contradicts that convention. `test_credentials.md` was read in full: it contains no real secret, only a note that the dev-preview deployment's `API_TOKEN` was empty and a reviewer-identity convention for tests. | No, and committing it would regress the "memory outside the tree" convention. | DELETE (contradicts current convention; content it does hold is non-sensitive but not something to re-commit) |
| `tasks/plan.md`, `tasks/todo.md` | 2 files | No -- absent from `main`, same "planning lives outside the tree" convention conflict as `memory/*`. | No | DELETE (contradicts current convention) |
| `.emergent/*` (`emergent.yml`, cron/webhook scripts, `system_deps.txt`) | 7 files | No -- absent from `main`. `.emergent/emergent.yml` contains a job id and creation timestamp tied to a specific Emergent.sh hosted-agent run (`"job_id": "77bda8e1-..."`, `"created_at": "2026-08-07T..."`); the cron/webhook scripts are that platform's deployment scaffolding. | No, environment-specific to a third-party hosted-agent platform, not portable. | DELETE (platform-specific artifact, not applicable to this environment) |
| `.gitconfig` (committed root-level git identity: `user.email = github@emergent.sh`, `user.name = emergent-agent-e1`) | 1 file | No -- absent from `main`, correctly: `main`'s actual convention (per the global `CLAUDE.md`) is per-repo identity switching between two real GitHub accounts (`solomonsjoseph` for work repos, `brucebanner010198-commits` for personal), automated by a local git hook. A committed `.gitconfig` pinning a third identity would conflict with that. | No | DELETE (conflicts with the repo's actual identity-switching convention; should never have been committed) |
| `.phi-build-status`, `setup-claude-code.sh` | 2 files | N/A -- both are deleted by the branch itself relative to its own merge base (`10 -` and `111 -` respectively in the diff stat) and are absent from both the branch's tip and `main`. | No | DELETE (already dead on both sides; nothing to migrate) |
| `phi_engine`, root `tests/`, `harness/`, `generators/`, `benchmarks/*.md`, root `requirements.txt` | Entire `phi_engine` pipeline package and its supporting tooling | Yes -- `main` keeps `phi_engine/`, `harness/` (confirmed present as top-level directories on `main`'s current tree), and root `tests/` intact. The branch deleted all of it (`generators/common.py -336`, `generators/hipaa_safe_harbor.py -603`, `harness/run_corpus_benchmark.py -710`, both `benchmarks/*.md` files removed, root `requirements.txt -35`) to pivot fully to the backend/frontend PHI Console. | No -- this deletion is the branch's single largest structural decision and it is a regression relative to `main`, not an unmerged improvement. | DELETE (must never be propagated: `phi_engine`/root `tests/`/`harness/`/`authorities/` are explicitly out of scope and read-only for this entire rewrite, per this wave's ground rules; deleting them would violate that directly) |
| `tests/__init__.py` (root), `validators/__init__.py` (root) | 2 empty files | `tests/__init__.py`: yes, already exists on `main` at the same path (it is `phi_engine`'s own root test package, out of scope). `validators/__init__.py`: no `validators/` directory exists on `main` at all. | No | DELETE (`tests/__init__.py` already covered on `main`; `validators/__init__.py` is an empty stub for a package that was never populated on the branch itself, nothing to migrate) |

## Disposition legend

- **KEEP**: `main` already has this, equal or better; no action.
- **DELETE**: do not migrate; superseded, contradicts current convention/ground
  rules, or already dead on both sides.
- **REVIEW_REQUIRED**: genuinely unmerged and not clearly superseded, but
  outside this wave's file-ownership boundary or requires a judgment call this
  wave is not positioned to make unilaterally.
- **MODIFY** / **MERGE** / **MIGRATE**: not used above -- no component in this
  inventory qualified. Every genuinely unmerged item found (`design_guidelines.json`,
  `scripts/cleanup.py`, `docs/AGENT_ARCHITECTURE.md`'s historical value,
  `phi_corpus/*`'s unverified line-level parity) landed on REVIEW_REQUIRED
  rather than a direct merge recommendation, because none of them are backend
  control-plane work this wave (R-Sandbox/Lineage/Trace/Handoff/Docs) is
  scoped to decide.

No component review here found a capability that exists on
`feat/agent-design-docs` and is missing on `main` in a way relevant to Phase R.
The branch is confirmed stale, consistent with `PRE_IMPLEMENTATION_AUDIT.md`
Section 1's original characterization.
