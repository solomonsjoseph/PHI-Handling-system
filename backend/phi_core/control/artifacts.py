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
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from phi_core.paths import (
    CACHE_DIR,
    EVIDENCE_DIR,
    PUBLISHED_DIR,
    REVERSAL_DIR,
    STAGING_DIR,
    UPLOAD_DIR,
    run_scoped_dir,
    sanitise_filename,
)

from .records import ArtifactRecord, DataClass, PublicationPointer
from .store import ControlStore

_ROOT_DIRS: Mapping[str, Any] = {
    "intake": UPLOAD_DIR,
    "staging": STAGING_DIR,
    "evidence": EVIDENCE_DIR,
    "reversal": REVERSAL_DIR,
    "published": PUBLISHED_DIR,
    "cache": CACHE_DIR,
}

_PROMOTABLE_STATES = ("staged", "accepted")


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
        """
        sanitise_filename(filename)  # raises UnsafePath on a malformed name; result discarded by design
        if root not in _ROOT_DIRS:
            raise ArtifactError("unknown_root", f"no such artifact root: {root!r}")
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

        final_dir = run_scoped_dir(_root_dir(record.root), self.session_id, self.run_id)
        final_path = final_dir / artifact_id
        os.replace(tmp_path, final_path)

        updated = record.model_copy(update={"sha256": sha256, "size_bytes": size, "state": "staged"})
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
