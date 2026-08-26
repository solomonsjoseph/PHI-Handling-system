# 0004: Artifact registry with staged, hash-bound, opaque-named writes

## Status

Accepted

## Context

Before this work, `Executor` wrote dataset/metadata/narrative exports directly to `EXPORT_DIR` under a session/run path built from the readable original filename, with no intermediate record of intent to write. A crash mid-write could leave a partial file at the path a later reader (Publish Guard, the bundle builder, a download route) would treat as complete. Download routes (`session_export`, `session_bundle`, `session_reversal_key`) trusted a raw filesystem path recorded on the session document with no binding to the bytes actually scanned by Publish Guard, and a `force` parameter could serve a blocked file regardless of the guard's verdict.

## Decision

`backend/phi_core/control/artifacts.py::ArtifactService` owns every write under an artifact root (`paths.py`'s `STAGING_DIR`, `EVIDENCE_DIR`, `REVERSAL_DIR`, `PUBLISHED_DIR`, `CACHE_DIR`, each `run_scoped_dir`-validated and mode `0o700`). `stage(root, ...)` creates a provisional `ArtifactRecord` and returns a tmp path under `<root>/.tmp/<artifact_id>`; the producer writes to that path; `finalize(artifact_id)` hashes the file, `os.replace`s it into `<root>/<session>/<run>/<artifact_id>` (bare artifact id, no extension: the canonical name never carries the original upload filename), and flips the record to `staged`. `control/writer.py::ArtifactWriter` wraps this pair as a context manager: a mid-write exception leaves zero files under the real (non-`.tmp`) root, and the record stays `provisional`, never promotable.

`open_for_download(artifact_id, ...)` is the only path a download route may use to serve a file: it refuses on state other than `promoted`, on a `PublicationPointer` generation mismatch, or on an on-disk hash that no longer matches the certified hash, each with a fixed, testable reason (`artifact_missing`, `artifact_hash_mismatch`, `state_not_promoted`, `generation_mismatch`). `certify_publication` performs the fenced, CAS-incremented generation bump the first time a clean result needs publishing. `session_export`, `session_bundle`, and `session_reversal_key` (`server.py`) now resolve the requested export's `artifact_id` and route through `open_for_download`; `session_bundle`/`session_reversal_key` additionally hash-verify every currently-clean export through the same path before bundling or unlocking a reversal key, so a tampered or corrupted published file can no longer be silently served. The `force` parameter and the `guard_overrides` write path are deleted from every download/publish route; a blocked per-file guard result is unconditionally unservable, with no override of any kind.

`publish_guard.py`'s `GuardResult` now carries `artifact_id`/`sha256`, computed from the exact bytes scanned, so a guard verdict binds to a specific hash rather than a filename that could be reused across sessions. `scan_names(text, jurisdiction)` adds Presidio's PERSON recognizer as HIPAA category A evidence, scanned per-cell (not per-line or whole-file, which produced false positives on this repository's own SDTM test corpus) for CSV/TSV/XLSX and whole-text for TXT/MD; every other extension keeps its pre-existing hard block unchanged.

Executor's canonical staged artifacts are extension-less by design (D14). Publish Guard's format dispatch (`scan_export_file`) and `bundle.py`'s archive-member naming still read a raw, suffix-bearing filesystem path; rather than changing either (out of scope for this phase), `Executor._finalize_export` creates a same-inode hard-link alias next to the canonical artifact carrying the real extension, and returns that alias to every caller that still needs a suffix-bearing path. The canonical hash-tracked artifact under the real artifact root is untouched by the alias; only the alias is deleted once Publish Guard and `bundle.py` are themselves migrated onto the artifact registry (tracked as a Phase 5+ follow-up, not a new numbered finding, since both call sites are read-only consumers of a path that already resolves correctly).

## Consequences

- A crash between `stage` and `finalize` is provably invisible to every downstream reader: nothing under the real artifact root exists until the write completes and is hashed.
- Every served download is bound to a specific, certified hash; a file that changes on disk after certification is refused, not served with stale trust.
- `force`/`guard_overrides` are structurally absent from the codebase, not merely unused: `inspect.signature` on every download/publish route has no such parameter, proven directly in `test_download_artifact_binding.py`.
- Publish Guard and `bundle.py` are not yet reading through the artifact registry directly; they consume the hard-link alias. This is a real, tracked gap (not a leak: the alias and the canonical artifact are the same inode, so no extra readable copy exists), closed when those two modules migrate.
