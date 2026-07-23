"""Local, support-only AI boundary for deriving an intake study name.

**What.** Given a verified :class:`~phi_engine.pipeline.intake_preflight.IntakePreflight`
result, decide the study name for an intake run: the user-supplied name, an
AI-inferred name derived *only* from support-component evidence (``forms``,
``data_dictionary``, ``mappings`` -- never ``datasets`` or ``_unclassified``),
or a random fallback when neither is available.

**Why.** Dataset content must never reach an LLM, even a local one. This
module is the *only* place in the standalone pipeline that is allowed to
open a source file for AI evidence, and it does so exclusively through
:func:`~phi_engine.pipeline.verified_source.open_verified_source` with a
required, re-verified ``source_component`` and identity -- the structural
proof that a dataset artifact (or anything preflight left ``_unclassified``)
can never become AI evidence, by construction rather than by convention.
A post-preflight ``datasets/`` hardlink is an additional, independent TOCTOU
threat this identity check alone cannot see (link count and bytes/inode do
not change): every naming candidate is re-checked against a fresh
descriptor-relative scan of ``datasets/`` immediately before its content is
parsed, and again immediately before any evidence is dispatched to the
local model, discarding all collected evidence and dispatching nothing the
moment such an alias is found.

**How.** Evidence is extracted with fixed, ordered readers (PDF via
``pdfplumber``, CSV via ``TextIOWrapper``/``csv.reader``, ``.xlsx`` via
``openpyxl``) operating directly on a duplicated descriptor
(``os.fdopen(os.dup(fd), "rb")``) for the *entire* verified-descriptor
lifetime -- hashing, ZIP/archive validation, and parsing all happen while
``open_verified_source``'s context is still open, and every reader/workbook
object is closed before that context exits (so its post-read identity
check always covers the complete, exact read). No full-document byte copy
is ever retained. Every reader/parser failure -- including openpyxl's lazy
worksheet iteration, not just ``load_workbook`` -- collapses to the fixed
``support-evidence-limit`` code; a raw descriptor ``OSError`` collapses to
``source-unreadable``; never a raw exception. Evidence is built
incrementally against each 8,192-UTF-8-byte cap *while parsing* (forms and
dictionary/mapping tracked independently; combined rebuilt from the same
already-bounded fragments), so retained state never scales with the total
number or size of support candidates -- only with what can ever fit the
canonical payload. ``.xlsx`` ZIP/archive bounds are enforced through the
one shared, directory-bounded ``zipfile.ZipFile`` primitive
(:func:`phi_engine.pipeline.support_files.open_bounded_zipfile`) also used
by :mod:`phi_engine.pipeline.intake_preflight`, never a second divergent
validator. CSV field allocation is bounded by a process-wide, lock-
serialized ``csv.field_size_limit`` in addition to an explicit UTF-8
byte-length check on every parsed field (not just the retained ones) before
any column slicing, so a field beyond column 20 cannot bypass the check by
never being kept. Every canonical evidence payload is gated through
``phi_gate_check`` before local dispatch and every response through
``guard_llm_output`` after, and dispatched only through the loopback-only,
attested, digest-pinned
:class:`~phi_engine.security.model_routing.OfflineLocalLLMClient`
(never ``config.get_llm_client()``). Local client construction/
configuration failures, transport/model failures, and output-validation
failures all collapse to the fixed ``study-name-inspection-failed`` error.
No source filename, path, header, sheet name, cell, value, raw exception,
prompt, or model response ever escapes this module.

Registry scanning and generated-tree reuse/promotion (matching a freshly
allocated ``study-<hex>`` name back to a prior study-less intake of the same
source, or promoting it once a real name is later supplied) are
:mod:`phi_engine.pipeline.intake` reconciliation concerns, not this
module's: they require the ``intake-registry`` lock and the v3 manifest
store, neither of which this module touches. :func:`canonical_source_root`
is the narrow, pure (no filesystem scan, no mutation) helper that
reconciliation layer needs to key its lookup; ``intake_root`` is accepted
here only so a future caller can thread it through unchanged.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import math
import os
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, Iterator, Literal, Mapping

import openpyxl
import pdfplumber

from phi_engine.audit.review_paths import safe_review_slug
from phi_engine.config import config
from phi_engine.pipeline import intake_preflight, support_files
from phi_engine.pipeline.intake_preflight import IntakeCandidate, IntakePreflight
from phi_engine.pipeline.verified_source import VerifiedSourceError, open_verified_source
from phi_engine.security.llm_tool_guard import LLMToolOutputBlocked, guard_llm_output
from phi_engine.security.model_routing import (
    LocalModelUnavailableError,
    ModelResponseError,
    OfflineLocalLLMClient,
    new_offline_local_client,
)
from phi_engine.security.phi_gate import PHIEgressBlockedError, phi_gate_check
from phi_engine.utils import pipeline_lock

__all__ = [
    "StudyNameSource",
    "StudyResolution",
    "resolve_intake_study",
    "canonical_source_root",
]

StudyNameSource = Literal["user", "ai", "generated"]

# --- fixed evidence bounds (plan step 2) ------------------------------------------------
_MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
_MAX_PDF_PAGES = 2
_MAX_WORKBOOK_SHEETS = 4
_MAX_ROWS = 20
_MAX_COLS = 20
_MAX_FRAGMENT_CODEPOINTS = 256
_MAX_CSV_FIELD_BYTES = 32 * 1024
_MAX_EVIDENCE_BYTES = 8192
_MAX_OUTPUT_TOKENS = 128
_MAX_MODEL_RESPONSE_BYTES = 4096
_HASH_CHUNK_SIZE = 1 << 20

# VerifiedSourceError reasons that mean "we could not trust this read at
# all" -- bucketed as value-free errors, mirroring intake_preflight.py's
# own convention so the two modules never disagree about which fixed
# reasons are retryable-by-a-human review items versus hard errors.
_ERROR_REASONS = frozenset({"source-unreadable", "source-target-outside-root"})

_NAMING_COMPONENTS = frozenset({"forms", "data_dictionary", "mappings"})
_ROOT_PATH = ""  # fixed string path for whole-source-root review/error records


@dataclass(frozen=True)
class StudyResolution:
    name: str
    source: StudyNameSource
    review_items: tuple[dict[str, Any], ...]
    errors: tuple[dict[str, Any], ...]


class _EvidenceLimitError(Exception):
    """Private control-flow signal: a candidate's content could not be
    safely bounded for naming evidence (oversized, expanded, or
    pathological input, including zip-bomb, malformed-archive, and any
    other parser/load/lazy-iteration/close failure)."""


class _InspectionFailed(Exception):
    """Private control-flow signal: the local model's response failed
    strict size/parse/key/type/range validation."""


def canonical_source_root(source: Path) -> str:
    """Canonical absolute source-root string, pure path resolution only.

    No filesystem scanning, no manifest reads, no mutation. Step 3's
    registry-scan/promotion reconciliation is expected to key its
    generated-manifest lookup on this exact value alongside
    ``StudyResolution.source == "generated"``.
    """
    return str(source.resolve())


def _generate_study_name() -> str:
    return f"study-{secrets.token_hex(4)}"


# --- descriptor-relative dataset-hardlink guard ---------------------------------------------
#
# FileIdentity (device/inode/size/mtime_ns) alone cannot see a hardlink
# created *after* preflight computed it: a new dataset dirent pointing at
# the same inode as an already-admitted support candidate changes neither
# that candidate's stat fields nor its content/hash. Closing this TOCTOU
# window requires an independent, fresh, descriptor-relative scan of the
# *current* datasets/ tree, compared by (device, inode) against whatever
# support candidate is about to be read.


def _current_dataset_identities(source: Path) -> frozenset[tuple[int, int]] | None:
    """Fresh scan of every regular file's ``(device, inode)`` currently
    reachable under ``<source>/datasets/``, delegated entirely to the
    shared, preflight-proven
    :func:`phi_engine.pipeline.intake_preflight._scan_component_identities`
    primitive (built on the same ``_walk_tree`` engine, NOFOLLOW
    discipline, and per-directory identity recheck
    :func:`~phi_engine.pipeline.intake_preflight.inspect_intake_source`
    itself uses) -- never a second, divergent traversal. Computed fresh
    immediately before parsing each naming candidate and again immediately
    before every individual model dispatch; never cached across those
    checkpoints.

    Returns ``None`` -- never a silently-empty set -- whenever the scan is
    inconclusive for any reason (unpinnable root, absent/symlinked/
    unopenable ``datasets/``, or any traversal-time failure, including a
    final/intermediate path swap). Callers MUST treat ``None`` identically
    to a confirmed hardlink alias: fail closed, discard all evidence,
    dispatch nothing.
    """
    identities, _reason = intake_preflight._scan_component_identities(source, "datasets")
    return identities


def _hardlink_race_detected(source: Path, identities: frozenset[tuple[int, int]] | set[tuple[int, int]]) -> bool:
    if not identities:
        return False
    current = _current_dataset_identities(source)
    if current is None:
        return True
    return bool(identities & current)


# --- entry point -------------------------------------------------------------------------


def _resolve_intake_study(
    source: Path,
    preflight: IntakePreflight,
    *,
    explicit_study: str | None,
    support_confirmed_no_phi: bool,
    intake_root: Path,
    generate_study_name: Callable[[], str] = _generate_study_name,
) -> StudyResolution:
    del intake_root  # reserved for step 3's registry-scan/promotion wiring; unused here

    if explicit_study is not None:
        # No-write validation contract: raises ValueError on a malformed
        # name. Deliberately not caught -- an invalid --study is a caller
        # bug, not a data-driven outcome with a fixed reason code.
        pipeline_lock.lock_path_for(explicit_study)
        return StudyResolution(name=explicit_study, source="user", review_items=(), errors=())

    if not support_confirmed_no_phi:
        # Zero naming-content extraction, zero model calls. The default
        # false/unknown consent state performs no reads beyond what
        # preflight already did.
        return StudyResolution(
            name=generate_study_name(),
            source="generated",
            review_items=({"path": _ROOT_PATH, "reason": "support-phi-status-required", "blocking": True},),
            errors=(),
        )

    admitted = sorted(
        (
            candidate
            for candidate in preflight.candidates
            if candidate.component in _NAMING_COMPONENTS and candidate.component == candidate.source_component
        ),
        key=lambda c: c.relative_path,
    )

    forms_docs, dict_docs, review_items, errors, contributing_identities, hardlink_detected = _collect_evidence(
        source, admitted
    )

    if hardlink_detected:
        return StudyResolution(
            name=generate_study_name(),
            source="generated",
            review_items=({"path": _ROOT_PATH, "reason": "cross-component-hardlink", "blocking": True},),
            errors=(),
        )

    forms_payload = _forms_payload_dict(forms_docs)
    dict_payload = _dict_payload_dict(dict_docs)
    forms_json = _canonical_json(forms_payload) if forms_payload["documents"] else None
    dict_json = _canonical_json(dict_payload) if dict_payload["documents"] else None

    chosen: str | None = None
    phi_blocked = False
    hardlink_race = False
    client: OfflineLocalLLMClient | None = None

    def get_client() -> OfflineLocalLLMClient:
        nonlocal client
        if client is None:
            client = new_offline_local_client()
        return client

    def dispatch_guarded(evidence_json: str) -> str | None:
        """Recheck the dataset-hardlink guard immediately before THIS
        specific dispatch (not once for the whole resolution) -- a client
        whose own first call creates a dict-to-datasets hardlink must
        never see a second call."""
        nonlocal hardlink_race
        if _hardlink_race_detected(source, contributing_identities):
            hardlink_race = True
            return None
        return _dispatch(get_client, evidence_json, errors)

    forms_name: str | None = None
    dict_name: str | None = None

    try:
        if forms_json is not None and not hardlink_race:
            forms_name = dispatch_guarded(forms_json)
        if dict_json is not None and not hardlink_race and not phi_blocked:
            dict_name = dispatch_guarded(dict_json)
    except PHIEgressBlockedError:
        phi_blocked = True
        forms_name = dict_name = None

    if not phi_blocked and not hardlink_race:
        if forms_name is not None and dict_name is not None:
            if forms_name.casefold() == dict_name.casefold():
                chosen = forms_name  # preserve the forms spelling
            else:
                review_items.append(
                    {
                        "path": _ROOT_PATH,
                        "reason": "study-name-conflict",
                        "blocking": True,
                        "candidates": {"forms": forms_name, "dictionary_mapping": dict_name},
                    }
                )
        elif forms_name is not None:
            chosen = forms_name
        elif dict_name is not None:
            chosen = dict_name
        elif forms_docs or dict_docs:
            forms_fragments = _forms_fragments_from_docs(forms_docs)
            dict_fragments = _dict_fragments_from_docs(dict_docs)
            combined_payload = _grow_combined(forms_fragments, dict_fragments, _MAX_EVIDENCE_BYTES)
            combined_json = (
                _canonical_json(combined_payload)
                if (combined_payload["forms"] or combined_payload["dictionary_mapping"])
                else None
            )
            if combined_json is not None:
                try:
                    chosen = dispatch_guarded(combined_json)
                except PHIEgressBlockedError:
                    phi_blocked = True

    if hardlink_race:
        review_items.append({"path": _ROOT_PATH, "reason": "cross-component-hardlink", "blocking": True})
        chosen = None
    if phi_blocked:
        review_items.append({"path": _ROOT_PATH, "reason": "possible-phi-requires-study", "blocking": True})
        chosen = None

    if chosen is not None:
        return StudyResolution(name=chosen, source="ai", review_items=tuple(review_items), errors=tuple(errors))

    return StudyResolution(
        name=generate_study_name(),
        source="generated",
        review_items=tuple(review_items),
        errors=tuple(errors),
    )


def resolve_intake_study(
    source: Path,
    preflight: IntakePreflight,
    *,
    explicit_study: str | None,
    support_confirmed_no_phi: bool,
    intake_root: Path,
) -> StudyResolution:
    return _resolve_intake_study(
        source,
        preflight,
        explicit_study=explicit_study,
        support_confirmed_no_phi=support_confirmed_no_phi,
        intake_root=intake_root,
    )


# --- local dispatch ------------------------------------------------------------------------

_PROMPT_PREFIX = (
    "You infer a short filesystem-safe study folder name from de-identified "
    "structural evidence only (no patient data). Respond with exactly one "
    'JSON object and nothing else: {"study_name": <string-or-null>, '
    '"confidence": <number 0 to 1>}. Use null when no clear name is present. '
    "Evidence:\n"
)


def _build_prompt(evidence_json: str) -> str:
    return _PROMPT_PREFIX + evidence_json


def _dispatch(
    get_client: Callable[[], OfflineLocalLLMClient], evidence_json: str, errors: list[dict[str, Any]]
) -> str | None:
    gate = phi_gate_check(evidence_json)
    if gate.blocked:
        raise PHIEgressBlockedError("naming evidence blocked by phi_gate_check")

    prompt = _build_prompt(evidence_json)
    try:
        client = get_client()
        raw = client.complete_bounded(
            prompt, max_output_tokens=_MAX_OUTPUT_TOKENS, max_response_bytes=_MAX_MODEL_RESPONSE_BYTES
        )
        guard_llm_output(raw)
        study_name, confidence = _parse_model_output(raw)
    except (
        LocalModelUnavailableError,
        ModelResponseError,
        LLMToolOutputBlocked,
        _InspectionFailed,
        config.LocalLLMConfigurationError,
    ):
        errors.append({"path": None, "reason": "study-name-inspection-failed"})
        return None

    if study_name is None or confidence < config.PHI_CONFIDENCE_THRESHOLD:
        return None
    return _normalize_and_validate(study_name)


def _parse_model_output(raw: str) -> tuple[str | None, float]:
    if not isinstance(raw, str) or not raw:
        raise _InspectionFailed()
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (ValueError, TypeError, RecursionError):
        raise _InspectionFailed() from None
    if not isinstance(value, dict) or set(value) != {"study_name", "confidence"}:
        raise _InspectionFailed()

    study_name = value["study_name"]
    if study_name is not None and not isinstance(study_name, str):
        raise _InspectionFailed()

    confidence = value["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise _InspectionFailed()
    confidence = float(confidence)
    if not math.isfinite(confidence) or not (0.0 <= confidence <= 1.0):
        raise _InspectionFailed()

    return study_name, confidence


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _normalize_and_validate(raw: str) -> str | None:
    slug = safe_review_slug(raw)[:128]
    try:
        pipeline_lock.lock_path_for(slug)
    except ValueError:
        return None
    return slug


# --- shared, process-wide CSV field-size ceiling --------------------------------------------
#
# csv.field_size_limit() is process-global mutable state. Serializing its
# mutation under a lock and always restoring the previous value keeps this
# module's bound from leaking into (or being clobbered by) any other CSV
# parsing happening concurrently elsewhere in the process.

_CSV_FIELD_LIMIT_LOCK = threading.Lock()


@contextlib.contextmanager
def _bounded_csv_field_limit() -> Iterator[None]:
    with _CSV_FIELD_LIMIT_LOCK:
        previous = csv.field_size_limit()
        csv.field_size_limit(_MAX_CSV_FIELD_BYTES)
        try:
            yield
        finally:
            csv.field_size_limit(previous)


# --- candidate evidence extraction ----------------------------------------------------------


def _hash_fd_bounded(fd: int, max_bytes: int) -> str:
    """Chunked SHA-256 of the current descriptor's remaining content,
    never retaining more than one bounded chunk in memory at a time (no
    full-document byte copy). Raises :class:`_EvidenceLimitError` if the
    actual bytes read exceed ``max_bytes``, independent of the recorded
    stat size."""
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = os.read(fd, _HASH_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise _EvidenceLimitError()
        digest.update(chunk)
    return digest.hexdigest()


def _process_candidate(
    source: Path, candidate: IntakeCandidate, *, parse_content: bool
) -> tuple[Any, bool, dict[str, Any] | None, dict[str, Any] | None]:
    """Open, dataset-hardlink-recheck, hash-verify, and (when
    ``parse_content``) parse one admitted naming candidate.

    Returns ``(fragments, hardlink_detected, review_item, error_item)``.
    ``fragments`` is ``list[str]`` (pages) for a forms candidate, or
    ``list[tuple[sheet_index, rows]]`` for a dictionary/mapping candidate;
    ``None``/``[]`` when nothing was retained. ``hardlink_detected`` is
    ``True`` only when this candidate's current descriptor identity now
    aliases something under ``datasets/`` -- the caller must discard all
    evidence and dispatch nothing when this fires, not just skip this one
    candidate. Every admitted candidate is opened, hardlink-rechecked, and
    hash-verified regardless of ``parse_content`` (so a later candidate
    past an already-exhausted evidence budget can still trip the hardlink
    guard); ``parse_content=False`` only skips the expensive reader call
    for content that would never be retained.

    The verified-descriptor context stays open for hashing AND parsing;
    every reader/workbook object opened from a duplicate of that
    descriptor is closed before this function returns, so the descriptor's
    own post-read identity check (performed by ``open_verified_source`` on
    context exit) always covers the exact, complete read.
    """

    if candidate.identity.size > _MAX_DOCUMENT_BYTES:
        return None, False, {"path": candidate.relative_path, "reason": "support-evidence-limit", "blocking": True}, None

    is_forms = candidate.component == "forms"
    suffix = PurePosixPath(candidate.relative_path).suffix.lower()

    try:
        with open_verified_source(
            source,
            candidate.relative_path,
            required_source_component=candidate.source_component,
            expected_identity=candidate.identity,
        ) as fd:
            info = os.fstat(fd)
            current_datasets = _current_dataset_identities(source)
            if current_datasets is None or (info.st_dev, info.st_ino) in current_datasets:
                return None, True, None, None

            digest = _hash_fd_bounded(fd, _MAX_DOCUMENT_BYTES)
            if digest != candidate.sha256:
                raise VerifiedSourceError("source-unreadable")

            if not parse_content:
                return None, False, None, None

            os.lseek(fd, 0, os.SEEK_SET)
            stream = os.fdopen(os.dup(fd), "rb")
            if is_forms:
                fragments: Any = _extract_pdf_pages(stream)
            elif suffix == ".csv":
                fragments = [(1, _extract_csv_rows(stream))]
            else:
                fragments = _extract_xlsx_sheets(stream)
    except VerifiedSourceError as exc:
        if exc.reason in _ERROR_REASONS:
            return None, False, None, {"path": candidate.relative_path, "reason": exc.reason}
        return None, False, {"path": candidate.relative_path, "reason": exc.reason, "blocking": True}, None
    except OSError:
        # Descriptor-level read/dup/fdopen failure distinct from what
        # open_verified_source's own identity/symlink checks normalize.
        return None, False, None, {"path": candidate.relative_path, "reason": "source-unreadable"}
    except _EvidenceLimitError:
        return None, False, {"path": candidate.relative_path, "reason": "support-evidence-limit", "blocking": True}, None

    return fragments, False, None, None


def _collect_evidence(
    source: Path, admitted: list[IntakeCandidate]
) -> tuple[
    dict[int, list[str]],
    dict[int, tuple[str, dict[int, list[list[str]]]]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    frozenset[tuple[int, int]],
    bool,
]:
    """Validate every admitted candidate (open, dataset-hardlink recheck,
    hash-verify) and incrementally grow the forms/dictionary_mapping
    evidence dicts fragment-by-fragment, each capped at
    ``_MAX_EVIDENCE_BYTES`` the moment it would be exceeded -- so retained
    state is always already-bounded, never an unbounded intermediate list
    of every parsed document. Once a component's budget closes, further
    candidates of that component are still opened/validated (for the
    hardlink guard) but their content is not parsed. A dataset hardlink
    found on any candidate immediately discards everything collected so
    far and stops the whole scan.
    """
    forms_docs: dict[int, list[str]] = {}
    dict_docs: dict[int, tuple[str, dict[int, list[list[str]]]]] = {}
    review_items: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    contributing_identities: set[tuple[int, int]] = set()
    form_index = 0
    dict_index = 0
    forms_budget_open = True
    dict_budget_open = True

    for candidate in admitted:
        is_forms = candidate.component == "forms"
        budget_open = forms_budget_open if is_forms else dict_budget_open

        fragments, hardlink_detected, review_item, error_item = _process_candidate(
            source, candidate, parse_content=budget_open
        )
        if hardlink_detected:
            return {}, {}, [], [], frozenset(), True
        if review_item is not None:
            review_items.append(review_item)
        if error_item is not None:
            errors.append(error_item)
        if not fragments or not budget_open:
            continue

        identity_key = (candidate.identity.device, candidate.identity.inode)
        if is_forms:
            form_index += 1
            idx = form_index
            for page in fragments:
                trial = {key: list(value) for key, value in forms_docs.items()}
                trial.setdefault(idx, []).append(page)
                if _encoded_len(_forms_payload_dict(trial)) > _MAX_EVIDENCE_BYTES:
                    forms_budget_open = False
                    break
                forms_docs = trial
                contributing_identities.add(identity_key)
        else:
            dict_index += 1
            idx = dict_index
            kind = candidate.component
            stop = False
            for sheet_index, rows in fragments:
                if stop:
                    break
                for row in rows:
                    trial = {
                        key: (value[0], {sk: list(sv) for sk, sv in value[1].items()}) for key, value in dict_docs.items()
                    }
                    _, sheets = trial.get(idx, (kind, {}))
                    sheets = dict(sheets)
                    sheets[sheet_index] = [*sheets.get(sheet_index, []), row]
                    trial[idx] = (kind, sheets)
                    if _encoded_len(_dict_payload_dict(trial)) > _MAX_EVIDENCE_BYTES:
                        dict_budget_open = False
                        stop = True
                        break
                    dict_docs = trial
                    contributing_identities.add(identity_key)

    return forms_docs, dict_docs, review_items, errors, frozenset(contributing_identities), False


def _extract_pdf_pages(stream: BinaryIO) -> list[str]:
    try:
        try:
            with pdfplumber.open(stream) as pdf:
                texts = [(page.extract_text() or "") for page in pdf.pages[:_MAX_PDF_PAGES]]
        finally:
            stream.close()
    except _EvidenceLimitError:
        raise
    except Exception:
        raise _EvidenceLimitError() from None
    for text in texts:
        if len(text) > _MAX_FRAGMENT_CODEPOINTS:
            raise _EvidenceLimitError()
    return texts


def _extract_csv_rows(stream: BinaryIO) -> list[list[str]]:
    with _bounded_csv_field_limit():
        try:
            text_stream = io.TextIOWrapper(stream, encoding="utf-8-sig", errors="strict", newline="")
            try:
                reader = csv.reader(text_stream)
                rows: list[list[str]] = []
                for _row_index, row in zip(range(_MAX_ROWS), reader):
                    # Validate every parsed field's UTF-8 byte size before
                    # any column slicing, so a field past column 20 cannot
                    # bypass the bound by never being retained.
                    for cell in row:
                        if len(cell.encode("utf-8")) > _MAX_CSV_FIELD_BYTES:
                            raise _EvidenceLimitError()
                    trimmed = row[:_MAX_COLS]
                    for cell in trimmed:
                        if len(cell) > _MAX_FRAGMENT_CODEPOINTS:
                            raise _EvidenceLimitError()
                    rows.append(trimmed)
            finally:
                text_stream.close()
        except _EvidenceLimitError:
            raise
        except (UnicodeDecodeError, csv.Error):
            raise _EvidenceLimitError() from None
    return rows


def _extract_xlsx_sheets(stream: BinaryIO) -> list[tuple[int, list[list[str]]]]:
    limits = support_files.DEFAULT_LIMITS
    try:
        try:
            # Pass 1: shared, directory-bounded ZipFile validation --
            # member count, per-member size, aggregate expansion, and
            # decompression ratio -- all against DEFAULT_LIMITS, via the
            # exact same primitive intake_preflight.py uses. Its central-
            # directory read is itself capped at max_zip_directory_bytes,
            # closing the allocation gap a bare zipfile.ZipFile(...) would
            # leave open.
            with support_files.open_bounded_zipfile(stream, limits) as (_zf, _guarded):
                pass
            # Pass 2: openpyxl performs its own internal ZipFile
            # construction regardless of what we already validated, so we
            # rewind and hand it the SAME stream wrapped in a fresh bound
            # (now the generous max_source_bytes -- the archive's
            # structure is already known-safe from pass 1) rather than an
            # unguarded raw stream.
            stream.seek(0)
            bounded_for_openpyxl = support_files._BoundedReader(stream, limits["max_source_bytes"])
            workbook = openpyxl.load_workbook(bounded_for_openpyxl, read_only=True, data_only=True, keep_links=False)
            try:
                sheets: list[tuple[int, list[list[str]]]] = []
                for sheet_index, worksheet in enumerate(workbook.worksheets[:_MAX_WORKBOOK_SHEETS], start=1):
                    rows: list[list[str]] = []
                    # Lazy XML parsing happens here, not at load_workbook()
                    # -- deliberately inside this same try/except so any
                    # failure during iteration is normalized identically.
                    for row in worksheet.iter_rows(
                        min_row=1, max_row=_MAX_ROWS, min_col=1, max_col=_MAX_COLS, values_only=True
                    ):
                        cells: list[str] = []
                        for value in row:
                            text = "" if value is None else str(value)
                            if len(text) > _MAX_FRAGMENT_CODEPOINTS:
                                raise _EvidenceLimitError()
                            cells.append(text)
                        rows.append(cells)
                    sheets.append((sheet_index, rows))
                return sheets
            finally:
                workbook.close()
        finally:
            stream.close()
    except _EvidenceLimitError:
        raise
    except Exception:
        # Catches support_files.BoundedZipMemberError, zipfile.BadZipFile,
        # openpyxl load/close failures, and any lazy-worksheet-iteration
        # failure (malformed cell references, corrupt shared strings,
        # etc.) uniformly -- never a raw exception.
        raise _EvidenceLimitError() from None


# --- canonical evidence JSON, incrementally bounded to 8192 bytes -------------------------


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _encoded_len(payload: Mapping[str, Any]) -> int:
    return len(_canonical_json(payload).encode("utf-8"))


def _forms_fragments_from_docs(docs: dict[int, list[str]]) -> list[tuple[int, str]]:
    return [(index, page) for index in sorted(docs) for page in docs[index]]


def _dict_fragments_from_docs(
    docs: dict[int, tuple[str, dict[int, list[list[str]]]]]
) -> list[tuple[int, str, int, list[str]]]:
    result: list[tuple[int, str, int, list[str]]] = []
    for index in sorted(docs):
        kind, sheets = docs[index]
        for sheet_index in sorted(sheets):
            for row in sheets[sheet_index]:
                result.append((index, kind, sheet_index, row))
    return result


def _forms_payload_dict(docs: dict[int, list[str]]) -> dict[str, Any]:
    return {"component": "forms", "documents": [{"index": i, "pages": docs[i]} for i in sorted(docs)]}


def _dict_payload_dict(docs: dict[int, tuple[str, dict[int, list[list[str]]]]]) -> dict[str, Any]:
    documents = []
    for i in sorted(docs):
        kind, sheets = docs[i]
        documents.append(
            {"index": i, "kind": kind, "sheets": [{"index": s, "rows": sheets[s]} for s in sorted(sheets)]}
        )
    return {"component": "dictionary_mapping", "documents": documents}


def _combined_payload_dict(
    forms_docs: dict[int, list[str]], dict_docs: dict[int, tuple[str, dict[int, list[list[str]]]]]
) -> dict[str, Any]:
    return {
        "component": "combined",
        "forms": _forms_payload_dict(forms_docs)["documents"],
        "dictionary_mapping": _dict_payload_dict(dict_docs)["documents"],
    }


def _grow_combined(
    forms_fragments: list[tuple[int, str]], dict_fragments: list[tuple[int, str, int, list[str]]], budget: int
) -> dict[str, Any]:
    """Rebuild the combined payload from the SAME already-bounded
    forms/dictionary_mapping fragments under one shared budget, appending
    forms fragments first then dictionary_mapping fragments, stopping at
    the first fragment that would exceed the cap (a single, deterministic,
    monotonic truncation boundary)."""
    forms_docs: dict[int, list[str]] = {}
    dict_docs: dict[int, tuple[str, dict[int, list[list[str]]]]] = {}
    stopped = False

    for index, page in forms_fragments:
        if stopped:
            break
        trial = {key: list(value) for key, value in forms_docs.items()}
        trial.setdefault(index, []).append(page)
        if _encoded_len(_combined_payload_dict(trial, dict_docs)) > budget:
            stopped = True
            break
        forms_docs = trial

    for index, kind, sheet_index, row in dict_fragments:
        if stopped:
            break
        trial = {key: (value[0], {sk: list(sv) for sk, sv in value[1].items()}) for key, value in dict_docs.items()}
        _, sheets = trial.get(index, (kind, {}))
        sheets = dict(sheets)
        sheets[sheet_index] = [*sheets.get(sheet_index, []), row]
        trial[index] = (kind, sheets)
        if _encoded_len(_combined_payload_dict(forms_docs, trial)) > budget:
            stopped = True
            break
        dict_docs = trial

    return _combined_payload_dict(forms_docs, dict_docs)
