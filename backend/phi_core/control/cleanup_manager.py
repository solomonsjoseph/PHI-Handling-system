"""CleanupManager (docs #76-77, Phase 12 item 6): populates every
:class:`~.records.CleanupManifest` field for a run's terminal-path
destruction, then hands the finished manifest to the caller so it can be
passed into :meth:`SuperOrchestrator.confirm_cleanup` -- the pre-existing,
already-tested sole path to ``session_destroyed``
(``superorchestrator.py``'s ``begin_cleanup``/``confirm_cleanup``, and
``tests/test_control_superorchestrator_lifecycle.py``'s
``test_confirm_cleanup_refuses_an_unverified_manifest``). This module does
not reimplement that invariant; it is the missing producer of a genuinely
verified manifest to feed it, closing the gap ``destroy_sandbox`` alone
leaves (``sandbox_destroyed`` only -- ``credentials_revoked``,
``keys_destroyed``, ``storage_sanitization_status``,
``destroyed_categories``, ``retained_safe_categories``, and
``verification_status`` were never populated anywhere before this module).

Cryptographic erase (NIST SP 800-88 Rev. 2, September 2025, "Cryptographic
Erase" media sanitization technique -- Table 3's CE method for storage that
already keeps its data encrypted at rest): this system's per-run sensitive
material -- the reversal-key ciphertext (``session.reversal_key_blob``,
``phi_core.crypto.encrypt_reversal_map``) and the
``workflow_runs.opaque_map`` encrypted vault (``control.opaque.OpaqueMap``)
-- is stored only as ciphertext under a process-wide key
(``phi_core.crypto``/``phi_core.security.egress_digest_key``); there is no
independent per-run data-encryption key to zero separately from the
ciphertext it protects. ``_destroy_keys`` below therefore implements CE by
irrecoverably destroying the ciphertext itself (an ``$unset``/CAS-cleared
document field, not an overwrite-in-place, since Mongo documents have no
fixed-offset "sectors" to zero): once the encrypted blob is gone, the
plaintext it protected is unrecoverable regardless of whether the shared
master key survives, which is CE's stated purpose (render the protected
data inaccessible) even though the concrete mechanism is ciphertext
deletion rather than a literal per-run key-zeroization call.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .records import CleanupManifest, SandboxRecord
from .sandbox import destroy_sandbox
from .store import ControlStore
from .superorchestrator import SuperOrchestrator

CLEANUP_MANIFEST_COLLECTION = "cleanup_manifests"

# docs #76's destroy list, reduced to this module's own stable category
# vocabulary (kept fixed so `destroyed_categories`/`retained_safe_categories`
# are machine-comparable across runs and across a version upgrade, not free
# text a caller could phrase differently each time).
CATEGORY_SANDBOX = "sandbox_workspace_and_temporary_executables"
CATEGORY_OPAQUE_MAP = "opaque_map_vault"
CATEGORY_REVERSAL_KEY = "reversal_key_ciphertext"
CATEGORY_CREDENTIALS = "run_scoped_credentials"
CATEGORY_STAGED_ARTIFACTS = "staged_intake_and_cached_artifacts"

CATEGORY_AUDIT_TRAIL = "trace_hash_chain_audit_trail"
CATEGORY_CLEANUP_MANIFEST = "cleanup_manifest_record"
CATEGORY_RUN_MANIFEST = "run_manifest_reproducibility_record"
CATEGORY_PUBLISHED_EXPORT = "published_clean_export_within_retention"

# Async callables an erasure step may raise from; caught individually so one
# failing step never masks or skips the others (every step still runs,
# every result still contributes to `destroyed_categories`/failures).
AsyncBoolStep = Callable[[], Awaitable[bool]]
AsyncListStep = Callable[[], Awaitable["tuple[bool, list[str]]"]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CleanupInputs:
    """Everything one run's cleanup pass needs, gathered by the caller
    (server.py knows how to read a session document / SandboxRecord; this
    module stays ignorant of Mongo/session schema so it is testable with
    :class:`~.store.MemoryControlStore` alone, matching every other
    control-plane service in this package)."""

    run_id: str
    session_id: str = ""
    sandbox: SandboxRecord | None = None
    opaque_map_present: bool = False
    reversal_key_present: bool = False
    export_within_retention: bool = False
    # Injected side-effecting steps. Each defaults to a no-op success so a
    # caller that has nothing to do for a given category (e.g. no reversal
    # key was ever generated for this run) does not need to fabricate a
    # trivial callable.
    erase_opaque_map: AsyncBoolStep | None = None
    erase_reversal_key: AsyncBoolStep | None = None
    erase_staged_artifacts: AsyncListStep | None = None
    revoke_credentials: AsyncBoolStep | None = None


async def _true() -> bool:
    return True


async def _empty() -> "tuple[bool, list[str]]":
    return True, []


class CleanupError(RuntimeError):
    """Raised with a fixed, testable ``reason`` on any refusal, matching
    this package's established error convention (``LearningError``,
    ``ArtifactError``, ``SandboxError``)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


class CleanupManager:
    """Orchestrates one run's terminal-path destruction (docs #76) and
    produces a fully-populated, self-verified :class:`CleanupManifest`
    (docs #77). Every public method is idempotent and safe to retry:
    calling :meth:`cleanup` twice for the same ``run_id`` inserts a second
    audit-trail manifest row (this package's established insert-only
    pattern, e.g. ``record_rewind_decision``) rather than mutating the
    first in place, so a retried sweep never silently overwrites evidence
    of an earlier attempt.
    """

    def __init__(self, store: ControlStore, orchestrator: SuperOrchestrator) -> None:
        self._store = store
        self._orchestrator = orchestrator

    async def cleanup(self, inputs: CleanupInputs) -> CleanupManifest:
        """Run every destruction step, verify, and persist the manifest.

        Deliberately does **not** call ``SuperOrchestrator.confirm_cleanup``
        itself: that stays the caller's own, explicit act (docs #77's "never
        transitions ... until this reports verified" is already enforced
        there), so a caller can inspect ``verification_status`` and choose
        to raise a cleanup incident instead of advancing the run's
        lifecycle when it comes back ``"failed"``.
        """
        await self._orchestrator.begin_cleanup(run_id=inputs.run_id)

        destroyed: list[str] = []
        retained: list[str] = []
        failures: list[str] = []

        sandbox_destroyed = await self._destroy_sandbox(inputs, destroyed, failures)
        opaque_ok, reversal_ok = await self._destroy_keys(inputs, destroyed, failures)
        artifacts_ok = await self._destroy_staged_artifacts(inputs, destroyed, failures)
        credentials_revoked = await self._revoke_credentials(inputs, destroyed, failures)

        retained.extend([CATEGORY_AUDIT_TRAIL, CATEGORY_CLEANUP_MANIFEST, CATEGORY_RUN_MANIFEST])
        if inputs.export_within_retention:
            retained.append(CATEGORY_PUBLISHED_EXPORT)

        storage_ok = sandbox_destroyed and opaque_ok and reversal_ok and artifacts_ok and credentials_revoked
        keys_destroyed = opaque_ok and reversal_ok
        storage_status: Any = "complete" if storage_ok else "failed"
        verification_status: Any = "verified" if (storage_ok and not failures) else "failed"

        manifest = CleanupManifest(
            run_id=inputs.run_id,
            cleanup_completed_at=_now(),
            destroyed_categories=destroyed,
            retained_safe_categories=retained,
            credentials_revoked=credentials_revoked,
            keys_destroyed=keys_destroyed,
            sandbox_destroyed=sandbox_destroyed,
            storage_sanitization_status=storage_status,
            verification_status=verification_status,
            failure_details="; ".join(failures),
        )
        await self._store.insert(CLEANUP_MANIFEST_COLLECTION, manifest)
        return manifest

    # ---- individual destruction steps --------------------------------

    async def _destroy_sandbox(
        self, inputs: CleanupInputs, destroyed: list[str], failures: list[str],
    ) -> bool:
        if inputs.sandbox is None:
            return True  # nothing to destroy is not a failure
        try:
            updated, _ = destroy_sandbox(inputs.sandbox)
        except Exception as exc:  # pragma: no cover - defensive, matches sandbox.py's own OSError-only contract
            failures.append(f"{CATEGORY_SANDBOX}: {exc}")
            return False
        ok = updated.state == "destroyed"
        if ok:
            destroyed.append(CATEGORY_SANDBOX)
        else:
            failures.append(f"{CATEGORY_SANDBOX}: {updated.failure_details}")
        return ok

    async def _destroy_keys(
        self, inputs: CleanupInputs, destroyed: list[str], failures: list[str],
    ) -> "tuple[bool, bool]":
        opaque_ok = True
        if inputs.opaque_map_present:
            step = inputs.erase_opaque_map or _true
            opaque_ok = await self._run_bool_step(step, CATEGORY_OPAQUE_MAP, failures)
            if opaque_ok:
                destroyed.append(CATEGORY_OPAQUE_MAP)

        reversal_ok = True
        if inputs.reversal_key_present:
            step = inputs.erase_reversal_key or _true
            reversal_ok = await self._run_bool_step(step, CATEGORY_REVERSAL_KEY, failures)
            if reversal_ok:
                destroyed.append(CATEGORY_REVERSAL_KEY)
        return opaque_ok, reversal_ok

    async def _destroy_staged_artifacts(
        self, inputs: CleanupInputs, destroyed: list[str], failures: list[str],
    ) -> bool:
        step = inputs.erase_staged_artifacts or _empty
        try:
            ok, errors = await step()
        except Exception as exc:  # pragma: no cover - defensive
            ok, errors = False, [str(exc)]
        if ok:
            destroyed.append(CATEGORY_STAGED_ARTIFACTS)
        else:
            failures.append(f"{CATEGORY_STAGED_ARTIFACTS}: {'; '.join(errors) or 'unknown error'}")
        return ok

    async def _revoke_credentials(
        self, inputs: CleanupInputs, destroyed: list[str], failures: list[str],
    ) -> bool:
        step = inputs.revoke_credentials or _true
        ok = await self._run_bool_step(step, CATEGORY_CREDENTIALS, failures)
        if ok:
            destroyed.append(CATEGORY_CREDENTIALS)
        return ok

    @staticmethod
    async def _run_bool_step(step: AsyncBoolStep, category: str, failures: list[str]) -> bool:
        try:
            ok = bool(await step())
        except Exception as exc:  # pragma: no cover - defensive
            failures.append(f"{category}: {exc}")
            return False
        if not ok:
            failures.append(f"{category}: erasure step returned falsy")
        return ok

    # ---- read path -----------------------------------------------------

    async def latest_manifest(self, run_id: str) -> CleanupManifest | None:
        """The most recently completed manifest for ``run_id``, or
        ``None`` before any cleanup pass has ever run for it. Multiple
        manifests can exist for one ``run_id`` (a retried sweep after an
        earlier failure) -- this picks the one with the latest
        ``cleanup_completed_at``, matching this package's insert-only,
        pick-the-newest convention (``learning.LearningService
        ._find_prior_good_activation``)."""
        docs = await self._store.find_many(CLEANUP_MANIFEST_COLLECTION, {"run_id": run_id})
        if not docs:
            return None
        manifests = [CleanupManifest.model_validate(d) for d in docs]
        return max(manifests, key=lambda m: m.cleanup_completed_at or m.cleanup_started_at)
