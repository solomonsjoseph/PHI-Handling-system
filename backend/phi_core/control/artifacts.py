"""The D14 artifact registry: staging atomicity, hash-bound download, publication.

``ArtifactService`` is the only writer under any artifact root. Every
material object -- dictionary reports, evidence snapshots, dataset exports,
reversal keys, bundles -- follows the same two-phase path:

1. ``stage(...)`` inserts a ``provisional`` :class:`~.records.ArtifactRecord`
   *before* a single byte is written, then hands back a path under
   ``<root>/.tmp/<artifact_id>`` for the caller to write to.
2. ``finalize(artifact_id)`` hashes those bytes, atomically ``os.replace``s
   them onto the record's run-scoped final path, and only then flips the
   record from ``provisional`` to ``staged``.

An exception at any point before ``os.replace`` leaves the record
``provisional`` and no bytes at the final path -- there is nothing to
promote. ``certify_publication`` is the sole writer of
:class:`~.records.PublicationPointer`; ``open_for_download`` is the sole
read path, and it refuses on a stale state, a generation that does not match
the current pointer, or a hash that no longer matches what is on disk.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from phi_core.paths import (
    CACHE_DIR,
    EVIDENCE_DIR,
    PUBLISHED_DIR,
    REVERSAL_DIR,
    STAGING_DIR,
    UPLOAD_DIR,
    is_safe_scoped_id,
    run_scoped_dir,
    sanitise_filename,
)

from .policy import BudgetExceeded, CapabilityDenied
from .records import ArtifactRecord, CapabilityGrant, DataClass, PublicationPointer
from .runs import check_run_budget, record_run_usage
from .store import ControlStore
from .workflow import WorkflowError

_ROOT_DIRS: Mapping[str, Any] = {
    "intake": UPLOAD_DIR,
    "staging": STAGING_DIR,
    "evidence": EVIDENCE_DIR,
    "reversal": REVERSAL_DIR,
    "published": PUBLISHED_DIR,
    "cache": CACHE_DIR,
}

_PROMOTABLE_STATES = ("staged", "accepted")

# F-POLICY-002 interim retention defaults. These intentionally mirror the
# server's session-retention policy until a separate retention policy module
# replaces both call sites.
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))
REVIEW_RETENTION_DAYS = int(os.environ.get("REVIEW_RETENTION_DAYS", str(RETENTION_DAYS)))


def _expires_at(retention_class: str) -> str:
    days = REVIEW_RETENTION_DAYS if retention_class == "review" else RETENTION_DAYS
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ArtifactError(RuntimeError):
    """Raised with a fixed, testable ``reason`` string on any artifact refusal."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


def _root_dir(root: str) -> Any:
    try:
        return _ROOT_DIRS[root]
    except KeyError as exc:
        raise ArtifactError("unknown_root", f"no such artifact root: {root!r}") from exc


def _tmp_dir(root: str) -> Any:
    tmp = _root_dir(root) / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(tmp, 0o700)
    return tmp


def _hash_file(path: Any) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


async def is_session_tombstoned(store: ControlStore, session_id: str) -> bool:
    """Whether ``tombstone_session`` has already marked ``session_id`` for
    erasure. Checked by :meth:`ArtifactService.stage` so a worker still
    finishing a run it was never told to stop cannot recreate an artifact
    for a session the operator already asked to delete."""
    return await store.get_one("session_tombstones", {"session_id": session_id}) is not None


async def tombstone_session(store: ControlStore, session_id: str) -> None:
    """Mark ``session_id`` for erasure (Phase 4 step 7, session-deletion
    coordination). Idempotent: tombstoning an already-tombstoned session is
    a no-op, never a duplicate-key error."""
    if await is_session_tombstoned(store, session_id):
        return
    await store.insert("session_tombstones", {"session_id": session_id, "tombstoned_at": _now()})


def erase_session_artifacts(session_id: str) -> dict[str, str]:
    """Delete every on-disk artifact directory for ``session_id`` across
    every artifact root (staging, evidence, reversal, published, cache).

    Returns a mapping of root name -> error message for any root whose
    directory could not be removed (permission error, concurrent external
    change); empty when every root's directory is confirmed gone (already
    absent counts as gone, not a failure). Callers must check this rather
    than assume success -- Phase 7 replaces the ``ignore_errors=True``
    this function used to swallow with a result the caller can record and
    retry.

    Purely filesystem-level: the caller is responsible for erasing the
    matching ``artifacts``/``publication_pointers`` store records
    separately (``ArtifactService.erase_session_records``), since that
    needs the async ``ControlStore`` this function does not take."""
    if not is_safe_scoped_id(session_id):
        raise ArtifactError("unsafe_session_id", session_id)
    failures: dict[str, str] = {}
    for name, root_dir in _ROOT_DIRS.items():
        try:
            shutil.rmtree(root_dir / session_id)
        except FileNotFoundError:
            continue
        except OSError as exc:
            failures[name] = str(exc)
    return failures


class ArtifactService:
    """Owns every artifact-root write and every ``artifacts``/``publication_pointers`` transition.

    One instance is scoped to a single ``(session_id, run_id)``; callers
    pass ``producer_task_id`` explicitly to ``stage`` so a shared service
    can still attribute each artifact to the task that created it.
    """

    def __init__(self, store: ControlStore, *, session_id: str, run_id: str) -> None:
        self._store = store
        self.session_id = session_id
        self.run_id = run_id

    async def stage(
        self,
        type: str,
        filename: str,
        data_class: DataClass,
        retention_class: str,
        *,
        producer_task_id: str = "",
        scope: str = "run",
        root: str = "staging",
    ) -> tuple[str, Any]:
        """Register a ``provisional`` artifact and return ``(artifact_id, tmp_path)``.

        ``filename`` is validated with :func:`~phi_core.paths.sanitise_filename`
        for defense in depth (it must not encode a traversal attempt) but is
        never written to disk or stored on the record: every on-disk and
        served path is keyed by ``artifact_id`` alone, so an original
        upload filename can never leak into a path, an index, or a served
        ``Content-Disposition``.

        Phase 4 step 7: refuses with ``ArtifactError("session_tombstoned", ...)``
        once ``tombstone_session`` has marked this artifact's session for
        erasure. A worker still finishing a run it was never told to stop
        cannot recreate an artifact for a session the operator already
        asked to delete.
        """
        if await is_session_tombstoned(self._store, self.session_id):
            raise ArtifactError("session_tombstoned", self.session_id)
        sanitise_filename(filename)
        if root not in _ROOT_DIRS:
            raise ArtifactError("unknown_root", f"no such artifact root: {root!r}")
        if producer_task_id:
            grant_doc = await self._store.get_one(
                "capability_grants",
                {"run_id": self.run_id, "task_id": producer_task_id},
            )
            if grant_doc is not None:
                granted_roots = CapabilityGrant.model_validate(grant_doc).scope.artifact_roots
                if granted_roots and root not in granted_roots:
                    raise CapabilityDenied(f"artifact root {root!r} is not granted to task {producer_task_id!r}")
        artifact_id = uuid4().hex
        rel_path = f"{self.session_id}/{self.run_id}/{artifact_id}"
        record = ArtifactRecord(
            artifact_id=artifact_id,
            session_id=self.session_id,
            run_id=self.run_id,
            producer_task_id=producer_task_id,
            scope=scope,
            type=type,
            root=root,
            rel_path=rel_path,
            data_class=data_class,
            retention_class=retention_class,
            expires_at=_expires_at(retention_class),
            state="provisional",
        )
        await self._store.insert("artifacts", record)
        tmp_root = _tmp_dir(root)
        tmp_path = tmp_root / artifact_id
        return artifact_id, tmp_path

    async def finalize(self, artifact_id: str) -> ArtifactRecord:
        """Hash the staged bytes and atomically promote them to ``staged``.

        Raises :class:`ArtifactError` (never partially applies) when: the
        record is missing or not ``provisional``; the writer never produced
        a tmp file; or the compare-and-set to ``staged`` loses a race. In
        every failure path the tmp file (if any) and the provisional record
        are left exactly as they were -- there is nothing at the final path
        to promote.
        """
        doc = await self._store.get_one("artifacts", {"artifact_id": artifact_id})
        if doc is None:
            raise ArtifactError("artifact_missing", artifact_id)
        record = ArtifactRecord.model_validate(doc)
        if record.state != "provisional":
            raise ArtifactError("artifact_not_provisional", f"state={record.state!r}")
        tmp_path = _tmp_dir(record.root) / artifact_id
        if not tmp_path.is_file():
            raise ArtifactError("artifact_write_incomplete", str(tmp_path))

        # Any failure reading the tmp file (permission error, truncation
        # mid-read, disk fault) raises here -- before the rename and before
        # the record is touched, so the provisional record and the tmp file
        # are the only trace left, never a promotable partial file at the
        # final path.
        sha256, size = _hash_file(tmp_path)

        # D5 (Phase 5 step 7): MAX_ARTIFACT_BYTES_PER_RUN is a run-wide
        # aggregate, checked here rather than at `stage()` time because
        # only `finalize()` knows the real byte count. Refused before the
        # rename, so the tmp file and the provisional record are left
        # exactly as any other pre-`os.replace` failure leaves them.
        try:
            await check_run_budget(self._store, self.run_id, artifact_bytes=size)
        except BudgetExceeded as exc:
            raise ArtifactError("artifact_bytes_budget_exceeded", str(exc)) from exc

        final_dir = run_scoped_dir(_root_dir(record.root), self.session_id, self.run_id)
        final_path = final_dir / artifact_id
        os.replace(tmp_path, final_path)
        # Real bytes are on disk now regardless of what the CAS below does
        # -- record the consumption before it, best-effort: a usage-record
        # race must never turn a successful promotion into a raised error.
        try:
            await record_run_usage(self._store, self.run_id, artifact_bytes=size)
        except WorkflowError:
            pass

        updated = record.model_copy(update={
            "sha256": sha256,
            "size_bytes": size,
            "state": "staged",
            "expires_at": record.expires_at or _expires_at(record.retention_class),
        })
        if not await self._store.compare_and_set(
            "artifacts", {"artifact_id": artifact_id}, {"state": "provisional"}, updated
        ):
            raise ArtifactError("artifact_state_race", artifact_id)
        return updated

    async def _current_pointer(self, session_id: str) -> PublicationPointer | None:
        docs = await self._store.find_many("publication_pointers", {"session_id": session_id})
        if not docs:
            return None
        pointers = [PublicationPointer.model_validate(doc) for doc in docs]
        return max(pointers, key=lambda pointer: pointer.generation)

    async def reject_export(
        self,
        *,
        artifact_id: str,
        file_path: str,
        reason: str,
        sha256: str = "",
    ) -> ArtifactRecord:
        """Mark the canonical export behind a guardable alias rejected.

        Executor normally creates the record before it exposes the alias.
        The fallback registration covers a recovered alias whose record was
        lost before the guard ran, but only after proving the alias belongs
        to this service's run-scoped artifact directory.
        """
        doc = await self._store.get_one("artifacts", {"artifact_id": artifact_id})
        if doc is not None:
            record = ArtifactRecord.model_validate(doc)
            if record.session_id != self.session_id or record.run_id != self.run_id:
                raise ArtifactError("artifact_scope_mismatch", artifact_id)
            if record.state in ("deleted", "legal_hold"):
                raise ArtifactError("artifact_not_rejectable", f"state={record.state!r}")
            if record.state == "rejected" and record.rejection_reason == reason:
                return record
            rejected = record.model_copy(update={"state": "rejected", "rejection_reason": reason})
            if not await self._store.compare_and_set(
                "artifacts",
                {"artifact_id": artifact_id},
                {"state": record.state},
                rejected,
            ):
                raise ArtifactError("artifact_state_race", artifact_id)
            return rejected

        if not is_safe_scoped_id(artifact_id):
            raise ArtifactError("artifact_missing", artifact_id)
        alias_path = Path(file_path)
        try:
            resolved_alias = alias_path.resolve(strict=True)
        except OSError as exc:
            raise ArtifactError("artifact_missing", str(alias_path)) from exc
        for root, root_dir in _ROOT_DIRS.items():
            final_dir = run_scoped_dir(root_dir, self.session_id, self.run_id)
            canonical_path = final_dir / artifact_id
            if (
                resolved_alias.parent == final_dir.resolve()
                and resolved_alias.name.startswith(f"{artifact_id}.")
                and canonical_path.is_file()
            ):
                actual_sha256, size = _hash_file(canonical_path)
                if sha256 and sha256 != actual_sha256:
                    raise ArtifactError("artifact_hash_mismatch", artifact_id)
                record = ArtifactRecord(
                    artifact_id=artifact_id,
                    session_id=self.session_id,
                    run_id=self.run_id,
                    producer_task_id="",
                    scope="run",
                    type="guard_rejected_export",
                    root=root,
                    rel_path=f"{self.session_id}/{self.run_id}/{artifact_id}",
                    sha256=actual_sha256,
                    size_bytes=size,
                    state="rejected",
                    data_class="restricted_metadata",
                    retention_class="export",
                    expires_at=_expires_at("export"),
                    rejection_reason=reason,
                )
                await self._store.insert("artifacts", record)
                return record
        raise ArtifactError("artifact_missing", artifact_id)





    async def certify_publication(
        self,
        *,
        run_id: str,
        artifact_ids: list[str],
        gate_result_ids: list[str],
        fence: int,
        certified_by_task_id: str = "",
    ) -> PublicationPointer:
        """Record the winning publication generation and promote its artifacts.

        Fenced: a ``fence`` at or below the current pointer's fence is
        refused (``stale_fence``) rather than allowed to overwrite a newer
        generation, and a lost race for the next generation number is
        refused the same way -- the prior pointer always stands.
        """
        current = await self._current_pointer(self.session_id)
        if current is not None and fence <= current.fence:
            raise ArtifactError("stale_fence", f"fence={fence} <= current={current.fence}")
        next_generation = (current.generation + 1) if current else 1
        pointer = PublicationPointer(
            session_id=self.session_id,
            run_id=run_id,
            generation=next_generation,
            artifact_ids=list(artifact_ids),
            gate_result_ids=list(gate_result_ids),
            certified_at=_now(),
            certified_by_task_id=certified_by_task_id,
            fence=fence,
        )
        raced = await self._store.get_one(
            "publication_pointers", {"session_id": self.session_id, "generation": next_generation}
        )
        if raced is not None:
            raise ArtifactError("stale_fence", f"generation={next_generation} already certified")
        await self._store.insert("publication_pointers", pointer)
        for artifact_id in artifact_ids:
            await self._promote(artifact_id, generation=next_generation)
        return pointer

    async def _promote(self, artifact_id: str, *, generation: int) -> ArtifactRecord:
        doc = await self._store.get_one("artifacts", {"artifact_id": artifact_id})
        if doc is None:
            raise ArtifactError("artifact_missing", artifact_id)
        record = ArtifactRecord.model_validate(doc)
        if record.state not in _PROMOTABLE_STATES:
            raise ArtifactError("artifact_not_staged", f"state={record.state!r}")
        source_dir = run_scoped_dir(_root_dir(record.root), record.session_id, record.run_id)
        source_path = source_dir / artifact_id
        if not source_path.is_file():
            raise ArtifactError("artifact_missing", str(source_path))
        published_dir = run_scoped_dir(PUBLISHED_DIR, record.session_id, record.run_id)
        published_path = published_dir / artifact_id
        tmp_path = published_dir / f".{artifact_id}.promoting"
        # Copy to a hidden temp name in the destination directory, then an
        # atomic same-filesystem rename: a crash mid-copy leaves only the
        # hidden temp file, never a partial file at the served path.
        shutil.copyfile(source_path, tmp_path)
        os.replace(tmp_path, published_path)
        updated = record.model_copy(
            update={
                "root": "published",
                "rel_path": f"{record.session_id}/{record.run_id}/{artifact_id}",
                "state": "promoted",
                "generation": generation,
                "expires_at": record.expires_at or _expires_at(record.retention_class),
                "promoted_at": _now(),
            }
        )
        await self._store.replace_one("artifacts", {"artifact_id": artifact_id}, updated)
        return updated

    async def open_for_download(self, session_id: str, artifact_id: str) -> Any:
        """Return the on-disk path for a promoted, current, hash-verified artifact.

        Refuses with an :class:`ArtifactError` whose ``reason`` is exactly
        one of ``artifact_missing``, ``artifact_not_promoted``,
        ``generation_mismatch``, or ``artifact_hash_mismatch``.
        """
        doc = await self._store.get_one("artifacts", {"artifact_id": artifact_id, "session_id": session_id})
        if doc is None:
            raise ArtifactError("artifact_missing", artifact_id)
        record = ArtifactRecord.model_validate(doc)
        if record.state != "promoted":
            raise ArtifactError("artifact_not_promoted", f"state={record.state!r}")
        pointer = await self._current_pointer(session_id)
        if pointer is None or record.generation != pointer.generation:
            raise ArtifactError(
                "generation_mismatch",
                f"artifact generation={record.generation}, current={pointer.generation if pointer else None}",
            )
        path = run_scoped_dir(PUBLISHED_DIR, session_id, record.run_id) / artifact_id
        try:
            actual_sha256, _ = _hash_file(path)
        except OSError as exc:
            raise ArtifactError("artifact_missing", str(exc)) from exc
        if actual_sha256 != record.sha256:
            raise ArtifactError("artifact_hash_mismatch", f"expected={record.sha256} actual={actual_sha256}")
        return path

    async def erase_session_records(self, session_id: str) -> int:
        """Delete every ``artifacts`` and ``publication_pointers`` record
        for ``session_id`` (Phase 4 step 7). Returns the number of records
        removed. Call ``erase_session_artifacts(session_id)`` for the
        matching on-disk cleanup; this method only touches the store."""
        removed = 0
        for artifact in await self._store.find_many("artifacts", {"session_id": session_id}):
            if await self._store.delete_one("artifacts", {"artifact_id": artifact["artifact_id"]}):
                removed += 1
        for pointer in await self._store.find_many("publication_pointers", {"session_id": session_id}):
            if await self._store.delete_one("publication_pointers", {"pointer_id": pointer["pointer_id"]}):
                removed += 1
        return removed


async def register_guard_rejections(
    artifact_service: ArtifactService,
    *,
    guard_report: Mapping[str, Any],
) -> list[ArtifactRecord]:
    """Transition every Publish Guard-blocked export in ``guard_report``.

    ``guard_report`` is the serialized output of
    :func:`publish_guard.scan_all_exports`. Files without the Executor's
    canonical artifact id are not registered because no safe artifact-root
    path can be proven for them.
    """
    rejected: list[ArtifactRecord] = []
    for result in guard_report.get("results", []):
        if result.get("status") != "blocked":
            continue
        artifact_id = str(result.get("artifact_id") or "")
        if not artifact_id:
            continue
        rejected.append(await artifact_service.reject_export(
            artifact_id=artifact_id,
            file_path=str(result.get("file_path") or ""),
            reason=str(result.get("detail") or "Publish Guard blocked export."),
            sha256=str(result.get("sha256") or ""),
        ))
    return rejected


# ---- reconcile (Phase 7 step 3): periodic artifact collection -------------

DEFAULT_STALE_PROVISIONAL_HOURS = 24.0

# Rejected records are now produced by Reviewer and Publish Guard. The
# `deletion_pending` state is this function's own in-flight marker, picked
# up again after a prior sweep's filesystem deletion failed.
_DELETABLE_STATES = frozenset({"rejected", "superseded", "deletion_pending"})


def _artifact_disk_path(record: ArtifactRecord) -> Any:
    """Where ``record``'s bytes live right now. A ``provisional`` record
    was never promoted past ``.tmp``; everything else was ``os.replace``d
    onto its run-scoped (or, once promoted, ``published``-rooted) final
    path."""
    if record.state == "provisional":
        return _tmp_dir(record.root) / record.artifact_id
    return run_scoped_dir(_root_dir(record.root), record.session_id, record.run_id) / record.artifact_id


def _is_expired(record: ArtifactRecord) -> bool:
    if not record.expires_at:
        return False
    try:
        expires_at = datetime.fromisoformat(record.expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


async def _reconcile_one(store: ControlStore, record: ArtifactRecord) -> bool:
    """Mark ``record`` ``deletion_pending``, confirm its bytes are gone,
    and only then remove the database record entirely. Unlinks both the
    ``.tmp`` staging path and the run-scoped final path (``missing_ok``
    makes whichever one never existed a no-op) rather than branching on
    ``record.state`` -- once this function's own CAS has already
    overwritten that state to ``deletion_pending``, a retried sweep for a
    record that started ``provisional`` could no longer tell which path
    type it originally was. A filesystem failure leaves the record
    ``deletion_pending`` with ``delete_attempts`` incremented and
    ``delete_error`` set, for the next sweep to retry -- never silently
    dropped, never removed from the store before its bytes are confirmed
    gone."""
    pending = record.model_copy(update={"state": "deletion_pending"})
    if not await store.compare_and_set(
        "artifacts", {"artifact_id": record.artifact_id}, {"state": record.state}, pending,
    ):
        return False  # raced with a concurrent transition; retried next sweep
    try:
        (_tmp_dir(record.root) / record.artifact_id).unlink(missing_ok=True)
        final_dir = run_scoped_dir(_root_dir(record.root), record.session_id, record.run_id)
        (final_dir / record.artifact_id).unlink(missing_ok=True)
        for alias_path in final_dir.glob(f"{record.artifact_id}.*"):
            alias_path.unlink(missing_ok=True)
    except OSError as exc:
        failed = pending.model_copy(update={
            "delete_attempts": pending.delete_attempts + 1, "delete_error": str(exc),
        })
        await store.compare_and_set(
            "artifacts", {"artifact_id": record.artifact_id}, {"state": "deletion_pending"}, failed,
        )
        return False
    await store.delete_one("artifacts", {"artifact_id": record.artifact_id})
    return True


async def reconcile(store: ControlStore, *, stale_provisional_hours: float = DEFAULT_STALE_PROVISIONAL_HOURS) -> dict[str, int]:
    """Periodic global sweep (Phase 7 step 3), independent of any one
    ``(session_id, run_id)`` scope -- meant to be called on a fixed
    interval, the same way ``worker.reconcile_forever`` wraps
    ``TaskService.reconcile_leases``.

    Eligible for collection:

    - ``rejected`` / ``superseded`` / ``deletion_pending`` (see
      ``_DELETABLE_STATES``).
    - ``provisional`` older than ``stale_provisional_hours``: the
      producing task raised, was cancelled, or the process restarted
      between ``stage()`` and ``finalize()``, so nothing will ever call
      ``finalize()`` for it (covers "cancelled or failed staging" and
      "interrupted intake and partial extraction").
    - ``staged`` / ``accepted`` / ``promoted`` whose on-disk file is
      already gone: the deletion condition is trivially satisfied, so the
      dangling database reference is removed with no filesystem work left
      to do ("manifest records whose file is missing").

    Never touches a record whose ``hold`` is non-empty (D14 legal hold) or
    already ``deleted``/``legal_hold``. Returns a count per outcome for
    the caller (the admin assurance route) to report.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=stale_provisional_hours)).isoformat()
    counts = {"deleted": 0, "failed": 0, "skipped_hold": 0}
    for doc in await store.find_many("artifacts", {}):
        record = ArtifactRecord.model_validate(doc)
        if record.state in ("deleted", "legal_hold"):
            continue
        if record.hold:
            counts["skipped_hold"] += 1
            continue
        eligible = (
            record.state in _DELETABLE_STATES
            or (record.state == "provisional" and record.created_at < cutoff)
            or _is_expired(record)
        )
        if not eligible and record.state in (*_PROMOTABLE_STATES, "promoted"):
            if not _artifact_disk_path(record).is_file():
                eligible = True
        if not eligible:
            continue
        counts["deleted" if await _reconcile_one(store, record) else "failed"] += 1
    return counts
