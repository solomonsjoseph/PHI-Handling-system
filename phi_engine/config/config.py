"""Central runtime configuration for RePORT AI Portal.

**What.** All path constants, environment-variable resolution, study
detection, LLM provider inference, staging-directory management,
and directory creation in one place.

**Why.** 138 call sites across the pipeline, agent, UI, and test suite
use ``import config`` — a single canonical location prevents scattered
``os.getenv`` and ``Path(...)`` literals throughout the codebase.

**How.** All values are resolved at import time. ``STUDY_NAME`` is
determined by the ``$STUDY_NAME`` env var or a filesystem scan of
``data/raw/``. LLM provider is inferred from model-name prefix unless
overridden by ``$LLM_PROVIDER``. Staging directories are NOT created
eagerly; call :func:`ensure_directories` after startup.
"""

# config.py
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, overload

import yaml

# ----------------------------------------------------------------------------
# ENV HELPERS (centralized, validated access)
# ----------------------------------------------------------------------------


@overload
def _get_env(key: str, default: str) -> str: ...
@overload
def _get_env(key: str, default: None = None) -> str | None: ...
def _get_env(key: str, default: str | None = None) -> str | None:
    value = os.getenv(key)
    return default if value is None or value == "" else value


def _get_env_int(key: str, default: int) -> int:
    raw = _get_env(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc


def _get_env_float(key: str, default: float) -> float:
    raw = _get_env(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be a float") from exc


def _get_env_bool(key: str, default: bool) -> bool:
    value = str(_get_env(key, str(default))).lower()
    return value in {"1", "true", "yes", "on"}


def production_mode_enabled() -> bool:
    """Return True when production controls should fail closed."""

    return (
        _get_env_bool("REPORT_AI_PRODUCTION", False)
        or _get_env_bool("REPORT_AI_REQUIRE_PHI_LOG_REDACTOR", False)
        or str(_get_env("REPORT_AI_AUTH_MODE", "")).strip().lower() == "proxy"
    )


def is_test_context() -> bool:
    """Return True ONLY when this process is genuinely running under pytest.

    Used by security-floor code (the disabled-scrub refusal in phi_scrub.run_scrub)
    to relax a control that would otherwise block deliberate test-only paths.

    SECURITY: the sole signal is ``"pytest" in sys.modules`` — a fact about the
    running interpreter that no pipeline entry point (``main.py --pipeline``, the
    skill wrapper, the SoT CLIs) ever satisfies, because none of them import
    pytest.  We deliberately do NOT consult operator/attacker-settable environment
    variables (``REPORTAL_TEST_FAKE_LLM``, ``PYTEST_CURRENT_TEST``): those are
    ordinary runtime flags (the fake-LLM smoke mode sets the former), so trusting
    them here would let a production operator who happens to have one set lower a
    raw-PHI fail-closed floor. Detection stays fully automatic — no operator flag
    needed — and cannot be spoofed from the environment.
    """
    import sys  # local import to avoid circular dependency at module level

    return "pytest" in sys.modules


def strict_study_detection_enabled() -> bool:
    """Return True when missing auto-detected study inputs should abort import."""

    return _get_env_bool("REPORT_AI_STRICT_STUDY_DETECTION", False)


# ----------------------------------------------------------------------------
# YAML CONFIG (config/config.yaml — optional overlay)
# ----------------------------------------------------------------------------

CONFIG_YAML_PATH = Path(__file__).resolve().parent / "config" / "config.yaml"


def _load_yaml_config() -> dict[str, Any]:
    """Load config.yaml if it exists; return empty dict otherwise."""
    if CONFIG_YAML_PATH.is_file():
        with CONFIG_YAML_PATH.open() as fh:
            data = yaml.safe_load(fh)
            return data if isinstance(data, dict) else {}
    return {}


_YAML_CFG: dict[str, Any] = _load_yaml_config()


def yaml_get(*keys: str, default: Any = None) -> Any:
    """Retrieve a nested key from the loaded YAML config.

    >>> yaml_get("app", "log_level", default="INFO")
    'INFO'
    """
    node: Any = _YAML_CFG
    for k in keys:
        if isinstance(node, dict):
            node = node.get(k)
        else:
            return default
    return node if node is not None else default


# ----------------------------------------------------------------------------
# VERSION
# ----------------------------------------------------------------------------

try:
    from __version__ import __version__
except ImportError:
    __version__ = "0.0.0"

DEFAULT_DATASET_NAME = "Indo-VAP"
DEFAULT_LOG_LEVEL = "INFO"

LOG_NAME = "report_ai_portal"
LOG_LEVEL = _get_env("LOG_LEVEL", yaml_get("app", "log_level", default=DEFAULT_LOG_LEVEL))
logger = logging.getLogger(LOG_NAME)

AGENT_MODEL_ID: str = os.environ.get("REPORTAL_AGENT_MODEL", "claude-opus-4-7")


# ----------------------------------------------------------------------------
# BASE PATHS
# ----------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
# Repo root alias — config.py lives at the repository root, so BASE_DIR *is* the
# repo root. Several UI/agent artifact-path resolvers reference ``config.REPO_ROOT``;
# expose it explicitly so those callers resolve against the repo root rather than
# silently falling back to the process CWD via ``getattr(config, "REPO_ROOT", ".")``.
REPO_ROOT = BASE_DIR
DATA_DIR = BASE_DIR / "data"
RAW_DATA_DIR = DATA_DIR / "raw"

OUTPUT_DIR = BASE_DIR / "output"
LOGS_DIR = BASE_DIR / ".logs"
TMP_DIR = BASE_DIR / "tmp"


# ----------------------------------------------------------------------------
# STUDY DETECTION
# ----------------------------------------------------------------------------


def detect_study_name(*, strict: bool | None = None) -> str:
    strict = strict_study_detection_enabled() if strict is None else strict
    if not RAW_DATA_DIR.exists():
        msg = f"RAW_DATA_DIR missing: {RAW_DATA_DIR}"
        if strict:
            raise RuntimeError(msg)
        logger.warning("%s → using default: %s", msg, DEFAULT_DATASET_NAME)
        return DEFAULT_DATASET_NAME

    try:
        exclude = {".backup", ".DS_Store", "output"}

        candidates = [
            p.name
            for p in RAW_DATA_DIR.iterdir()
            if p.is_dir() and not p.name.startswith(".") and p.name not in exclude
        ]

        for candidate in sorted(candidates):
            if (RAW_DATA_DIR / candidate / "datasets").is_dir():
                return candidate

        msg = f"No valid study found under {RAW_DATA_DIR}"
        if strict:
            raise RuntimeError(msg)
        logger.warning("%s → using default: %s", msg, DEFAULT_DATASET_NAME)
        return DEFAULT_DATASET_NAME

    except OSError as exc:
        if strict:
            raise RuntimeError(f"Study detection failed under {RAW_DATA_DIR}") from exc
        logger.warning("Study detection failed → fallback to default", exc_info=True)
        return DEFAULT_DATASET_NAME


# ENV override ALWAYS wins
_STUDY_NAME_ENV = _get_env("STUDY_NAME")
if _STUDY_NAME_ENV:
    if "/" in _STUDY_NAME_ENV or "\\" in _STUDY_NAME_ENV or _STUDY_NAME_ENV in {".", ".."}:
        raise ValueError("STUDY_NAME must be a plain folder name, not a path")
    STUDY_NAME = _STUDY_NAME_ENV
else:
    STUDY_NAME = detect_study_name()


# ----------------------------------------------------------------------------
# STUDY PATHS
# ----------------------------------------------------------------------------

STUDY_DATA_DIR = RAW_DATA_DIR / STUDY_NAME
STUDY_OUTPUT_DIR = OUTPUT_DIR / STUDY_NAME

# Raw study subdirectories (under data/raw/<study>/)
DATASETS_DIR = STUDY_DATA_DIR / "datasets"
ANNOTATED_PDFS_DIR = STUDY_DATA_DIR / "annotated_pdfs"
DATA_DICTIONARY_DIR = STUDY_DATA_DIR / "data_dictionary"

# Study config lives in config/<study>/ (underscore-prefixed YAML), separate
# from raw data (Excel/CSV in data/raw/<study>/datasets/). Note 11.
CONFIG_DIR = BASE_DIR / "config"
CONFIG_DEFAULTS_DIR = CONFIG_DIR / "_defaults"
STUDY_CONFIG_DIR = CONFIG_DIR / STUDY_NAME


def study_config_path(filename: str, *, study: str | None = None) -> Path:
    """Resolve a per-study config file under config/<study>/ (single chokepoint)."""
    return CONFIG_DIR / (study or STUDY_NAME) / filename


FORMS_MANIFEST_PATH = STUDY_CONFIG_DIR / "_forms_manifest.yaml"
STUDY_PRIVACY_PATH = STUDY_CONFIG_DIR / "_study_privacy.yaml"
STUDY_KNOWLEDGE_PATH = STUDY_CONFIG_DIR / "study_knowledge.yaml"

# Legacy constant retained for rollback/back-compat checks. The active
# LLM-visible clean tree is STUDY_LLM_SOURCE_DIR; this directory is not created
# by default.
TRIO_BUNDLE_DIR = STUDY_OUTPUT_DIR / "trio_bundle"

# LLM-visible source directory — canonical home for artefacts the agent reads.
STUDY_LLM_SOURCE_DIR = STUDY_OUTPUT_DIR / "llm_source"

TRIO_DATASETS_DIR = STUDY_LLM_SOURCE_DIR / "dataset_schema" / "files"

# Historical concept-index output is not part of the active Load Study plugin
# path. The current agent metadata surface is ``llm_source/SoT/<pair>/`` plus
# dataset and dictionary outputs.

STUDY_AUDIT_DIR = STUDY_OUTPUT_DIR / "audit"

# Per-run operational state (run_state.json, phi_handling_approval.json, the
# per-run human_review/run_<id> notes) lives under runs/<run_id>/ — operational
# bookkeeping, distinct from the IRB-evidence audit/ tree (Note 24). The
# per-run subdir is created on demand with the run id (see the dir-precreation
# helper); this constant names the parent.
STUDY_RUNS_DIR = STUDY_OUTPUT_DIR / "runs"

# Audit-report paths written by dataset cleanup / PHI scrub.
# Only the dataset publish leg produces audit reports. Dictionary mappings and
# legacy PDF JSON compatibility helpers are content-only from the host side.
# Step-cache manifests for dataset_processing also land under STUDY_AUDIT_DIR
# so the LLM-visible llm_source/ tree stays content-only.
AUDIT_DATASET_REPORT_PATH: Path = STUDY_AUDIT_DIR / "dataset_cleanup_report.json"
AUDIT_SCRUB_REPORT_PATH: Path = STUDY_AUDIT_DIR / "phi_scrub_report.json"

DICTIONARY_JSON_OUTPUT_DIR = STUDY_LLM_SOURCE_DIR / "dictionary_mapping" / "jsonl"

# ----------------------------------------------------------------------------
# PHASE 0 — SoT GAP CONSTANTS
# ----------------------------------------------------------------------------
# Source-of-Truth directory lives under data/SoT/<study>/ (not under raw/).
SOT_DIR: Path = DATA_DIR / "SoT" / STUDY_NAME
# Raw form PDFs live at the root of RAW_DATA_DIR / STUDY_NAME.
RAW_PDF_DIR: Path = RAW_DATA_DIR / STUDY_NAME
# Pilot extraction results land under tmp/results/.
PILOT_RESULTS_DIR: Path = TMP_DIR / "results"
# Working drafts for SoT-gap analysis live under tmp/sot_gap_drafts/.
SOT_GAP_DRAFTS_DIR: Path = TMP_DIR / "sot_gap_drafts"
# Coverage and report artefacts written at the end of a gap run.
SOT_GAP_COVERAGE_PATH: Path = TMP_DIR / "sot_gap_coverage.json"
SOT_GAP_REPORT_PATH: Path = TMP_DIR / "sot_gap_report.md"

# --- Phase 1: PHI rule audit and expand --------------------------------------
PHI_TECHNIQUES_INVENTORY_PATH: Path = (
    BASE_DIR / "docs" / "superpowers" / "specs" / "2026-05-08-phi-techniques-inventory.md"
)
PHI_COVERAGE_MATRIX_PATH: Path = (
    BASE_DIR / "docs" / "superpowers" / "specs" / "2026-05-08-phi-coverage-matrix.md"
)
PHI_SWEEP_FINDINGS_PATH: Path = TMP_DIR / "phi_sweep_findings.json"
PHI_SWEEP_HITL_DRAFTS_DIR: Path = TMP_DIR / "phi_sweep_hitl_drafts"
PHI_SWEEP_PR_DRAFTS_DIR: Path = TMP_DIR / "phi_sweep_pr_drafts"

# --- Phase 2: llm_source restructure -----------------------------------------
LLM_SOURCE_DATASET_SCHEMA_CATALOG_PATH: Path = (
    STUDY_LLM_SOURCE_DIR / "dataset_schema" / "catalog.json"
)
LLM_SOURCE_DICTIONARY_MAPPING_DIR: Path = STUDY_LLM_SOURCE_DIR / "dictionary_mapping"
LLM_SOURCE_DICTIONARY_MAPPING_JSONL_DIR: Path = LLM_SOURCE_DICTIONARY_MAPPING_DIR / "jsonl"
LLM_SOURCE_DICTIONARY_CATALOG_PATH: Path = LLM_SOURCE_DICTIONARY_MAPPING_DIR / "catalog.json"
LLM_SOURCE_STUDY_METADATA_DIR: Path = STUDY_LLM_SOURCE_DIR / "study_metadata"
LLM_SOURCE_STUDY_METADATA_CATALOG_PATH: Path = LLM_SOURCE_STUDY_METADATA_DIR / "catalog.json"
LLM_SOURCE_SOT_DIR: Path = STUDY_LLM_SOURCE_DIR / "SoT"
LLM_SOURCE_LEGACY_SOURCE_TRUTH_DIR: Path = STUDY_LLM_SOURCE_DIR / "source_truth"


def repoint_llm_source_base(new_base: Path) -> None:
    """Atomically repoint ``STUDY_LLM_SOURCE_DIR`` AND every derived constant.

    The llm_source-derived path constants above are computed once at import time
    from ``STUDY_LLM_SOURCE_DIR``. Setting ``STUDY_LLM_SOURCE_DIR`` alone (e.g.
    when the Load Study UI activates a snapshot) leaves the dataset-query and
    SoT-citation tools reading the LIVE output tree while only the readiness
    checks observe the new base — a split-brain read zone.

    This helper rebases ALL of them from *new_base*, mirroring the exact relative
    subpaths declared above, so a snapshot activation is complete and atomic: a
    single call repoints the whole llm_source surface to *new_base*.
    """
    new_base = Path(new_base)
    g = globals()
    g["STUDY_LLM_SOURCE_DIR"] = new_base
    g["TRIO_DATASETS_DIR"] = new_base / "dataset_schema" / "files"
    g["DICTIONARY_JSON_OUTPUT_DIR"] = new_base / "dictionary_mapping" / "jsonl"
    g["LLM_SOURCE_DATASET_SCHEMA_CATALOG_PATH"] = new_base / "dataset_schema" / "catalog.json"
    g["LLM_SOURCE_DICTIONARY_MAPPING_DIR"] = new_base / "dictionary_mapping"
    g["LLM_SOURCE_DICTIONARY_MAPPING_JSONL_DIR"] = new_base / "dictionary_mapping" / "jsonl"
    g["LLM_SOURCE_DICTIONARY_CATALOG_PATH"] = new_base / "dictionary_mapping" / "catalog.json"
    g["LLM_SOURCE_STUDY_METADATA_DIR"] = new_base / "study_metadata"
    g["LLM_SOURCE_STUDY_METADATA_CATALOG_PATH"] = new_base / "study_metadata" / "catalog.json"
    g["LLM_SOURCE_SOT_DIR"] = new_base / "SoT"
    g["LLM_SOURCE_LEGACY_SOURCE_TRUTH_DIR"] = new_base / "source_truth"


# Lean-catalog size thresholds (bytes). CI fails if a catalog exceeds.
LEAN_CATALOG_DICTIONARY_MAX_BYTES: int = 20 * 1024
LEAN_CATALOG_DATASET_SCHEMA_MAX_BYTES: int = 50 * 1024
LEAN_CATALOG_STUDY_METADATA_MAX_BYTES: int = 200 * 1024

# --- Phase 3: cross-verify ---------------------------------------------------
CROSS_VERIFY_SAFE_REPORT_PATH: Path = TMP_DIR / "cross_verify_safe_report.json"
CROSS_VERIFY_AGENT_WORKDIR: Path = TMP_DIR / "cross_verify_agent_workdir"
CROSS_VERIFY_PR_DRAFTS_DIR: Path = TMP_DIR / "cross_verify_pr_drafts"
CROSS_VERIFY_HITL_DRAFTS_DIR: Path = TMP_DIR / "cross_verify_hitl_drafts"
CROSS_VERIFY_REPEAT_THRESHOLD: int = 2

# --- Phase 4: audit-zone hardening -------------------------------------------
AUDIT_NO_LLM_SENTINEL_NAME: str = ".NO_LLM_ZONE"
AUDIT_SENTINEL_ALARM_PATH: Path = TMP_DIR / "audit_sentinel_alarms.jsonl"
AUDIT_NO_LLM_ZONE_ATTRIBUTE: str = "report-ai-portal-no-llm"

# ----------------------------------------------------------------------------
# AGENT STATE TIER (per-session state, NOT study output)
# ----------------------------------------------------------------------------
# Per-session, agent-owned operational state — analysis runs, conversation
# transcripts. Telemetry lives under STUDY_AUDIT_DIR so the LLM's permitted
# agent/** zone stays free of operator-audit bytes. Everything inside the
# fully-gitignored ``output/`` tree keeps PHI-scrubbed cohort bytes out of
# git by default.
AGENT_STATE_DIR: Path = STUDY_OUTPUT_DIR / "agent"
AGENT_OUTPUT_DIR: Path = AGENT_STATE_DIR / "analysis"
CONVERSATIONS_DIR: Path = AGENT_STATE_DIR / "conversations"

# ----------------------------------------------------------------------------
# SNAPSHOT TIER (legacy path — LLM-INVISIBLE security boundary)
# ----------------------------------------------------------------------------
# The snapshot/restore subsystem itself has been retired (SoT-based extraction
# now produces a reviewable ``llm_source/`` tree directly). This constant is
# preserved as a security-zone marker: ``data/snapshots/`` is intentionally
# OUTSIDE the LLM agent's read zone (which is ``llm_source/`` + ``agent/``),
# and ``cutover_gate`` plus the agent file-access tests still assert that any
# path under this directory is hard-rejected by ``validate_agent_read``.
STUDY_SNAPSHOTS_DIR: Path = DATA_DIR / "snapshots" / STUDY_NAME

# ----------------------------------------------------------------------------
# STUDY SNAPSHOT OUTPUT TIER (immutable clean-publish records — W1)
# ----------------------------------------------------------------------------
# Per-study, immutable record of a fully-clean publish pass written by
# ``scripts/utils/snapshot.py``. Each ``snapshots/{snapshot_id}/`` holds a copy
# of the run's ``llm_source/`` tree, its ``phi_handling_approval.json``, the
# verifier report, and a ``snapshot_manifest.json``. The Load Study UI's
# "existing study data" selector lists these and loads one in place of the live
# pipeline output.
#
# SECURITY: the snapshot ROOT is OUTSIDE the agent read zone (which is
# ``llm_source/`` + ``agent/``). ``validate_agent_read`` hard-rejects any path
# under this directory EXCEPT a ``snapshots/{id}/llm_source/`` subtree that has
# been explicitly selected (i.e. ``config.STUDY_LLM_SOURCE_DIR`` repointed at
# it). A ``.NO_LLM_ZONE`` sentinel is dropped at each snapshot root as
# defence-in-depth. Distinct from the legacy ``STUDY_SNAPSHOTS_DIR`` baseline
# marker above, which lives under ``data/`` and is never auto-created.
STUDY_SNAPSHOTS_OUTPUT_DIR: Path = STUDY_OUTPUT_DIR / "snapshots"

# Staging workspace — per-study tree inside TMP_DIR. Managed per-run by
# main.py's _prepare_staging() / _publish_staging(); NOT created eagerly by
# ensure_directories() so a stale workspace from a crashed previous run is
# always purged explicitly before reuse.
STUDY_STAGING_DIR: Path = TMP_DIR / STUDY_NAME
STAGING_DATASETS_DIR: Path = STUDY_STAGING_DIR / "datasets"
STAGING_DICTIONARY_DIR: Path = STUDY_STAGING_DIR / "dictionary"

# Note-16 pre-creation tree leaves (Break 5). Staging legs under TMP_DIR and the
# audit / llm_source legs under STUDY_OUTPUT_DIR. These mirror the per-run tree
# created by ``ensure_run_directories()`` below.
STAGING_HEADERS_DIR: Path = STUDY_STAGING_DIR / "headers"
STAGING_QUARANTINE_DIR: Path = STAGING_DATASETS_DIR / "quarantine"
STAGING_SOT_DIR: Path = STUDY_STAGING_DIR / "SoT"
AUDIT_HUMAN_REVIEW_DIR: Path = STUDY_AUDIT_DIR / "human_review"
AUDIT_DATASETS_DIR: Path = STUDY_AUDIT_DIR / "datasets"
AUDIT_SCRUBBING_CODE_DIR: Path = STUDY_AUDIT_DIR / "scrubbing_code"

# ----------------------------------------------------------------------------
# PHI SCRUB
# ----------------------------------------------------------------------------
# Narrow PHI handling: per-subject deterministic date jitter (SANT method) +
# HMAC-SHA256 ID pseudonymization. See scripts/security/phi_scrub.py.
#
# The scrub config is resolved per-study: a per-study override at
# ``config/<study>/phi_scrub.yaml`` wins over the packaged defaults at
# ``config/_defaults/phi_scrub.yaml``. ``phi_scrub.load_scrub_config()`` deep-
# merges the per-study file ON TOP of the defaults (the EFFECTIVE config); this
# resolver returns the single most-specific *existing* file so the ~24
# ``config.PHI_SCRUB_CONFIG_PATH`` consumers (existence checks, friendly
# messaging) keep working. The reproducibility-critical scrub_config_hash hashes
# the MERGED effective config, not this single path — see
# ``phi_scrub.effective_scrub_config_hash()``.
PHI_SCRUB_CONFIG_FILENAME = "phi_scrub.yaml"


def phi_scrub_config_path(study: str | None = None) -> Path:
    """Resolve the active scrub-config path for *study*.

    Returns ``config/<study>/phi_scrub.yaml`` when that per-study override
    exists, otherwise ``config/_defaults/phi_scrub.yaml``. The deep-merge of the
    two (when both exist) happens in ``phi_scrub.load_scrub_config()``; this
    helper only picks the most-specific existing file.
    """
    per_study = CONFIG_DIR / (study or STUDY_NAME) / PHI_SCRUB_CONFIG_FILENAME
    if per_study.is_file():
        return per_study
    return CONFIG_DEFAULTS_DIR / PHI_SCRUB_CONFIG_FILENAME


PHI_SCRUB_CONFIG_PATH: Path = phi_scrub_config_path()


def _phi_key_path() -> Path:
    """Resolve the sidecar PHI HMAC key path.

    Resolution order (Note 12):
    1. ``$PHI_KEY_PATH`` — explicit override (the spec's named storage env var);
    2. ``$XDG_CONFIG_HOME/report_ai_portal/phi_key`` when XDG is set;
    3. ``~/.config/report_ai_portal/phi_key`` fallback.

    The value is a PATH (not key material), so it is not a secret. The key file
    itself lives OUTSIDE the repo tree and is never read by the agent or committed
    to git.
    """
    explicit = os.getenv("PHI_KEY_PATH")
    if explicit:
        return Path(explicit)
    xdg = os.getenv("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "report_ai_portal" / "phi_key"


PHI_KEY_PATH: Path = _phi_key_path()


# ----------------------------------------------------------------------------
# EXTRACTION CONFIG (centralized — used by all extraction modules)
# ----------------------------------------------------------------------------

# Temporary-file prefixes for atomic writes.  Each module uses its own prefix
# so crash-leftover temp files can be attributed to their source.
TEMP_PREFIX_DATASET: str = "report_ai_portal_dataset_"
TEMP_PREFIX_DICT: str = "report_ai_portal_dict_"
TEMP_PREFIX_DEDUP: str = "report_ai_portal_dedup_"

# Secure temp workspace — the prefix is intentionally generic+randomised so
# the directory name leaks no information about what pipeline stage created it.
SECURE_TEMP_PREFIX: str = "rpln_"

# Duplicate-column detection regex for dataset extraction
DUPLICATE_COLUMN_PATTERN: str = r"^(.+?)_?(\d+)$"


# ----------------------------------------------------------------------------
# LLM PROVIDER INFERENCE
# ----------------------------------------------------------------------------


def _infer_provider(model_name: str) -> str:
    """Infer LangChain provider string from model name prefix.

    Recognised patterns:
        llama*, mistral*, phi*, gemma*, qwen* (incl. qwen3:8b), deepseek*,
        codellama*, tinyllama*, vicuna*, falcon*, orca*  → "ollama"
        claude*                               → "anthropic"
        gpt-*, o1*, o3*, o4*, text-davinci*   → "openai"
        gemini*                               → "google-genai"

    Falls back to ``"ollama"`` (local inference, no API key needed).
    """
    m = model_name.lower()
    _ollama_prefixes = (
        "llama",
        "mistral",
        "phi3",
        "phi-3",
        "gemma",
        "qwen",
        "deepseek",
        "codellama",
        "tinyllama",
        "vicuna",
        "falcon",
        "orca",
    )
    if m.startswith(_ollama_prefixes):
        return "ollama"
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith(("gpt-", "o1", "o3", "o4", "text-davinci")):
        return "openai"
    if m.startswith("gemini"):
        return "google-genai"
    if m.startswith("fake"):
        return "fake-local"
    # NVIDIA-hosted models use "org/model" slug format
    _nvidia_orgs = ("moonshotai/", "nvidia/", "mistralai/", "deepseek-ai/", "qwen/", "meta/")
    if any(m.startswith(prefix) for prefix in _nvidia_orgs):
        return "nvidia-ai-endpoints"
    return "ollama"  # safe default — local inference, no key needed


LLM_MODEL = _get_env("LLM_MODEL", yaml_get("ai_assistant", "llm_model", default="qwen3:8b"))
# LLM_PROVIDER: explicit env var wins; otherwise infer from model name.
LLM_PROVIDER: str = _get_env("LLM_PROVIDER") or _infer_provider(LLM_MODEL)

# PHI corpus LLM config -- read from llm: block in config.yaml, overridable by env vars.
PHI_LLM_PROVIDER: str = _get_env("PHI_LLM_PROVIDER", yaml_get("llm", "provider", default=LLM_PROVIDER))
PHI_LLM_MODEL: str = _get_env("PHI_LLM_MODEL", yaml_get("llm", "model", default=LLM_MODEL))
PHI_LLM_API_KEY_ENV: str = _get_env("PHI_LLM_API_KEY_ENV", yaml_get("llm", "api_key_env", default=""))
PHI_LLM_BASE_URL: str = _get_env("PHI_LLM_BASE_URL", yaml_get("llm", "base_url", default="http://localhost:11434"))
PHI_LLM_TIMEOUT_S: int = _get_env_int("PHI_LLM_TIMEOUT_S", int(yaml_get("llm", "timeout_s", default=60)))
PHI_LLM_MAX_RETRIES: int = _get_env_int("PHI_LLM_MAX_RETRIES", int(yaml_get("llm", "max_retries", default=3)))
PHI_CONFIDENCE_THRESHOLD: float = _get_env_float(
    "PHI_CONFIDENCE_THRESHOLD", float(yaml_get("llm", "confidence_threshold", default=0.75))
)


class LLMClient:
    """Thin provider-agnostic wrapper with a single .complete(prompt) -> str method.

    Supports anthropic, openai, google-genai, and ollama (local). The provider is
    selected by PHI_LLM_PROVIDER (env) or llm.provider (config.yaml). API keys are
    never stored here -- only the name of the env var that holds them.
    """

    def __init__(
        self,
        provider: str = PHI_LLM_PROVIDER,
        model: str = PHI_LLM_MODEL,
        api_key_env: str = PHI_LLM_API_KEY_ENV,
        base_url: str = PHI_LLM_BASE_URL,
        timeout_s: int = PHI_LLM_TIMEOUT_S,
        max_retries: int = PHI_LLM_MAX_RETRIES,
    ) -> None:
        self.provider = provider
        self.model = model
        self._api_key_env = api_key_env
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._max_retries = max_retries

    @property
    def _api_key(self) -> str:
        if not self._api_key_env:
            _defaults = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "google-genai": "GOOGLE_API_KEY"}
            self._api_key_env = _defaults.get(self.provider, "")
        return os.environ.get(self._api_key_env, "") if self._api_key_env else ""

    def complete(self, prompt: str) -> str:
        """Send prompt to LLM and return text response. Raises on unrecoverable error."""
        dispatch = {
            "anthropic": self._complete_anthropic,
            "openai": self._complete_openai,
            "google-genai": self._complete_google,
        }
        fn = dispatch.get(self.provider, self._complete_ollama)
        return fn(prompt)

    def _complete_anthropic(self, prompt: str) -> str:
        try:
            import anthropic as _anthropic
        except ImportError as exc:
            raise ImportError("pip install anthropic") from exc
        client = _anthropic.Anthropic(api_key=self._api_key, max_retries=self._max_retries)
        msg = client.messages.create(
            model=self.model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
            timeout=self._timeout_s,
        )
        return msg.content[0].text

    def _complete_openai(self, prompt: str) -> str:
        try:
            import openai as _openai
        except ImportError as exc:
            raise ImportError("pip install openai") from exc
        client = _openai.OpenAI(api_key=self._api_key, max_retries=self._max_retries, timeout=self._timeout_s)
        resp = client.chat.completions.create(
            model=self.model, messages=[{"role": "user", "content": prompt}]
        )
        return resp.choices[0].message.content

    def _complete_google(self, prompt: str) -> str:
        try:
            import google.generativeai as genai
        except ImportError as exc:
            raise ImportError("pip install google-generativeai") from exc
        genai.configure(api_key=self._api_key)
        return genai.GenerativeModel(self.model).generate_content(prompt).text

    def _complete_ollama(self, prompt: str) -> str:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError("pip install httpx") from exc
        resp = httpx.post(
            f"{self._base_url}/api/generate",
            json={"model": self.model, "prompt": prompt, "stream": False},
            timeout=self._timeout_s,
        )
        resp.raise_for_status()
        return resp.json()["response"]

    def __repr__(self) -> str:
        return f"LLMClient(provider={self.provider!r}, model={self.model!r})"


def get_llm_client() -> "LLMClient":
    """Return a configured LLMClient using current PHI_LLM_* constants."""
    return LLMClient()

# Qwen3 downgrade ladder — descending parameter count. When Ollama refuses
# a rung with "requires more system memory", _init_llm walks this list to
# find the largest rung that actually loads. Only applies to qwen3:* models;
# other models (Claude, GPT, custom Ollama) pass through unchanged.
QWEN3_DOWNGRADE_LADDER: tuple[str, ...] = ("qwen3:8b", "qwen3:4b", "qwen3:1.7b")


def preferred_or_installed_downgrade(model: str) -> list[str]:
    """Return the sequence of model names to try starting at ``model``.

    For qwen3 rungs in :data:`QWEN3_DOWNGRADE_LADDER`, returns the ladder
    from the given rung downward. For any other model, returns a one-element
    list — we only auto-step qwen3 because the three rungs are behaviourally
    compatible (same family, same tool-use format, same thinking convention).
    """
    if model in QWEN3_DOWNGRADE_LADDER:
        start = QWEN3_DOWNGRADE_LADDER.index(model)
        return list(QWEN3_DOWNGRADE_LADDER[start:])
    return [model]


# ----------------------------------------------------------------------------
# AI Assistant / AGENT
# ----------------------------------------------------------------------------

# Telemetry lives under STUDY_AUDIT_DIR (not AGENT_STATE_DIR) to keep the
# LLM's permitted agent/** zone clear of operator-audit bytes. Per the PHI
# rule, LLM must never read telemetry; parking it under audit/ — the same
# zone that holds phi_scrub_report.json and dataset_cleanup_report.json —
# makes that boundary structural, not a per-file carve-out.
TELEMETRY_DIR = STUDY_AUDIT_DIR / "telemetry"
TELEMETRY_SINK = TELEMETRY_DIR / "events.jsonl"

# ── PHI AI-assist (Notes 7 + 9) — default ON, with deterministic fallback ──
# Gate the LLM-assisted PHI subsystem. Default ON, but it only RUNS where an LLM
# is actually reachable: the publish supervisor constructs the aligner only when
# this flag is on, the process is not under pytest, AND the configured provider
# has a usable API key in the KeyStore (entered via the UI). When the LLM is NOT
# available — no key, airgapped, CI, pytest, or REPORTAL_PHI_ALIGNMENT_ENABLED=0 —
# the publish path FALLS BACK to the deterministic pinned-rules behavior, byte-
# identical to before, and NO LLM is constructed. When it does run, the LLM reads
# ONLY public regulation text (N7 rulebook) and column NAMES (N9 alignment) —
# never a dataset row value (GR-1). All AI output is deterministically verified,
# version-stamped, frozen, and the pinned rules remain the protection floor.
PHI_ALIGNMENT_ENABLED: bool = _get_env_bool("REPORTAL_PHI_ALIGNMENT_ENABLED", True)
RULEBOOK_AI_EXTRACT: bool = _get_env_bool("REPORTAL_RULEBOOK_AI_EXTRACT", False)
RULEBOOK_REQUIRE_LIVE: bool = _get_env_bool("REPORTAL_RULEBOOK_REQUIRE_LIVE", False)
PHI_SCRUB_GENERATED_FILENAME: str = "phi_scrub.generated.yaml"

# Chat / agent
AGENT_MAX_TOKENS: int = _get_env_int("AGENT_MAX_TOKENS", 16384)
AGENT_TIMEOUT: int = _get_env_int("AGENT_TIMEOUT", 300)
# Sampling temperature for the agent / eval judge. Default 0 for deterministic,
# reproducible answers — a graded eval (scripts/eval/cloud_eval.py) is only
# meaningful if the same question yields the same answer run-to-run. Override
# via AGENT_TEMPERATURE for exploratory/creative use.
AGENT_TEMPERATURE: float = _get_env_float("AGENT_TEMPERATURE", 0.0)
# Bounded automatic retries for transient provider errors (HTTP 429 rate
# limits, 5xx). The OpenAI/Anthropic SDKs back off exponentially and honour
# the server's Retry-After header up to this many attempts, so brief
# throttling is absorbed silently instead of surfacing as a chat error. Set
# to 0 to disable retries (fail fast). Default 5 ≈ tens of seconds of total
# backoff — enough for typical burst throttling without stalling the UI.
AGENT_MAX_RETRIES: int = _get_env_int("AGENT_MAX_RETRIES", 5)
CHAT_RATE_LIMIT_WINDOW_SECONDS: int = _get_env_int("CHAT_RATE_LIMIT_WINDOW_SECONDS", 60)
CHAT_RATE_LIMIT_MAX_TURNS: int = _get_env_int("CHAT_RATE_LIMIT_MAX_TURNS", 12)
# Watchdog on the agent stream: raise TimeoutError if no chunk is produced
# for this many seconds. Measures inter-chunk idle time, NOT total wall
# clock — so slow-but-steady streams (long tool runs) stay alive. The E3
# benchmark stall went 6+ minutes of total silence with no stop signal;
# 180s is ~10x the p99 of a healthy routing step.
AGENT_STREAM_IDLE_TIMEOUT: int = _get_env_int("AGENT_STREAM_IDLE_TIMEOUT", 180)

# Analytical engine limits
ANALYSIS_TIMEOUT: int = _get_env_int("ANALYSIS_TIMEOUT", 300)
ANALYSIS_MAX_OUTPUT: int = _get_env_int("ANALYSIS_MAX_OUTPUT", 200_000)
ANALYSIS_MAX_FIGURES: int = _get_env_int("ANALYSIS_MAX_FIGURES", 20)

# Sandbox subprocess limits — operational tunables (safe to lower; lowering
# only tightens the security envelope). The trust boundary itself
# (import allowlist, env-var blocklist, AST guards) is hardcoded in
# ``scripts.ai_assistant.sandbox`` and is not configurable from here.
#
# Defaults sized for production runs of the typical pandas+numpy+plotly
# stack: numpy alone reserves ~700 MB of address space on Linux when loaded
# (RLIMIT_AS is whole-vmap, not RSS). RLIMIT_NPROC is per-user not per-tree
# on Linux, so a small cap conflicts with whatever else the host user is
# running — 4096 is high enough to coexist with shared CI environments
# while still preventing runaway fork bombs.
SANDBOX_MAX_MEMORY_MB: int = _get_env_int("SANDBOX_MAX_MEMORY_MB", 2048)
SANDBOX_MAX_PROCS: int = _get_env_int("SANDBOX_MAX_PROCS", 4096)
SANDBOX_MAX_FILES: int = _get_env_int("SANDBOX_MAX_FILES", 256)
SANDBOX_PERSIST_CODE: bool = _get_env("SANDBOX_PERSIST_CODE", "true").lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# Orchestration mode: "auto" | "single-agent" | "multi-agent"
AGENT_ORCHESTRATION_MODE: str = _get_env(
    "AGENT_ORCHESTRATION_MODE",
    yaml_get("ai_assistant", "agent", "orchestration_mode", default="auto"),
)

# Enforce LangChain tracing OFF by default (privacy-first)
os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")


# ----------------------------------------------------------------------------
# DIRECTORY CREATION
# ----------------------------------------------------------------------------


def ensure_directories() -> None:
    """Create runtime directories. Sensitive dirs (containing PHI-scrubbed
    data, agent state, conversations, audit, or logs) are
    hardened to mode 0o700 after creation so they're not world-readable
    under the typical umask 0o022. Dirs that may legitimately need group
    access (``OUTPUT_DIR`` parent, ``TMP_DIR`` is already 0o700 via
    secure-staging) are left at default mode."""
    sensitive_paths = [
        STUDY_OUTPUT_DIR,
        LOGS_DIR,
        TRIO_DATASETS_DIR,
        DICTIONARY_JSON_OUTPUT_DIR,
        LLM_SOURCE_SOT_DIR,
        STUDY_AUDIT_DIR,
        AGENT_STATE_DIR,
        AGENT_OUTPUT_DIR,
        CONVERSATIONS_DIR,
        TELEMETRY_DIR,
        # NOTE: ``STUDY_SNAPSHOTS_DIR`` is intentionally NOT created here.
        # It is a human-reviewed baseline under ``data/snapshots/{STUDY}/``.
        # Auto-creating it would hide the absence of a reviewed fallback.
    ]
    for path in [OUTPUT_DIR, TMP_DIR, *sensitive_paths]:
        path.mkdir(parents=True, exist_ok=True)
    import contextlib

    for path in sensitive_paths:
        # Best-effort: a chmod failure (e.g., not the file owner) is not a
        # fatal startup error.
        with contextlib.suppress(OSError):
            path.chmod(0o700)


def ensure_run_directories(study: str | None = None, run_id: str | None = None) -> None:
    """Pre-create the full Note-16 per-study (and per-run) directory tree.

    Builds the complete tree a publish run expects so downstream legs never have
    to ``mkdir(parents=True)`` ad hoc:

        config/<study>/
        tmp/<study>/{headers, datasets, datasets/quarantine, SoT}
        output/<study>/{audit, audit/human_review, audit/datasets,
                        audit/scrubbing_code, runs/<run_id>, llm_source,
                        llm_source/SoT, snapshots}

    Sensitive leaves (anything that may carry PHI-scrubbed data, staging PHI, or
    audit evidence) are hardened to 0o700 after creation, mirroring
    ``ensure_directories()``. This does NOT replace ``ensure_directories()`` —
    it is the per-run superset used by the publish pipeline. ``study`` defaults
    to ``STUDY_NAME``; ``run_id`` adds ``runs/<run_id>`` when supplied.

    Pre-creating empty ``tmp/<study>/`` leaves is safe: secure_staging purges /
    re-creates the staging workspace explicitly before reuse, so an empty
    pre-created dir is indistinguishable from a fresh one.
    """
    import contextlib

    active_study = study or STUDY_NAME
    study_config_dir = CONFIG_DIR / active_study
    staging_root = TMP_DIR / active_study
    staging_datasets = staging_root / "datasets"
    output_dir = OUTPUT_DIR / active_study
    audit_dir = output_dir / "audit"
    llm_source = output_dir / "llm_source"

    # Non-sensitive parents created first.
    for path in (OUTPUT_DIR, TMP_DIR, CONFIG_DIR, study_config_dir, staging_root):
        path.mkdir(parents=True, exist_ok=True)

    sensitive_paths = [
        staging_root / "headers",
        staging_datasets,
        staging_datasets / "quarantine",
        staging_root / "SoT",
        output_dir,
        audit_dir,
        audit_dir / "human_review",
        audit_dir / "datasets",
        # NOTE (Note 24 / B7): audit/scrubbing_code is a placeholder for the
        # DEFERRED N9 AI-scrub-config-completion feature; it is never written
        # today, so it is no longer pre-created as an empty dir. When N9 lands it
        # creates AUDIT_SCRUBBING_CODE_DIR on demand. (Telemetry stays under
        # audit/ — the no-LLM-fenced zone — deliberately, NOT relocated to runs/.)
        llm_source,
        llm_source / "SoT",
        output_dir / "snapshots",
    ]
    if run_id:
        sensitive_paths.append(output_dir / "runs" / run_id)

    for path in sensitive_paths:
        path.mkdir(parents=True, exist_ok=True)
    for path in sensitive_paths:
        with contextlib.suppress(OSError):
            path.chmod(0o700)


# ----------------------------------------------------------------------------
# VALIDATION
# ----------------------------------------------------------------------------


def validate_config() -> None:
    # --- PATH VALIDATION ---
    required_paths = [
        RAW_DATA_DIR,
        STUDY_DATA_DIR,
        DATASETS_DIR,
        DATA_DICTIONARY_DIR,
    ]

    for path in required_paths:
        if not path.exists():
            raise FileNotFoundError(f"Missing required path: {path}")

    # PDF source is optional — the pipeline handles its absence gracefully
    if not ANNOTATED_PDFS_DIR.exists():
        logger.warning(
            "Annotated PDFs directory not found: %s — PDF extraction will be skipped",
            ANNOTATED_PDFS_DIR,
        )

    # Ensure the dictionary directory contains at least one file
    if DATA_DICTIONARY_DIR.is_dir() and not any(DATA_DICTIONARY_DIR.iterdir()):
        raise FileNotFoundError(f"Dictionary directory is empty: {DATA_DICTIONARY_DIR}")

    # --- LOG FINAL STATE ---
    logger.info(
        "Config loaded | study=%s",
        STUDY_NAME,
    )
