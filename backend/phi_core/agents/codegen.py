"""Rewrite plan step 9: the codegen safety machinery every code-writing
agent (Schema, step 10; Executor, step 11) drives through before its
generated source ever touches real dataset rows.

Five independent controls, none of which trusts the model:

1. :func:`check_generated_code` -- static, pre-execution AST analysis.
   Rejects an import outside :data:`STATIC_CHECK_ALLOWED_IMPORTS` (including
   a bare name imported out of an otherwise-allowed module, e.g. ``from
   pandas import read_pickle``, and a wildcard import, which defeats
   name-based detection entirely), a pandas-shaped attribute outside
   :data:`PANDAS_ALLOWED_METHODS`, a dunder attribute access (closes the
   classic ``().__class__.__bases__[0].__subclasses__()`` interpreter
   escape, which touches no banned token), a ``Name`` load of a small set
   of dangerous builtins, or an attribute chain rooted at a banned module.
   A syntax error is itself a violation. The string-literal path
   heuristic an earlier design considered is deliberately absent:
   runtime-constructed paths defeat it; ``control/runner.py``'s
   container filesystem jail is the real control against path escape.
2. :func:`check_entrypoint_shape` -- a separate, structural check (kept
   apart from (1) so (1)'s own unit tests can stay focused on individual
   denial categories rather than full entrypoint-shaped modules): source
   must define a top-level ``def run()`` (or ``async def run()``), the
   exact entrypoint :func:`run_generated` calls. Catching a missing
   entrypoint here turns what would otherwise be a confusing
   container-boundary failure into the same structured retry diagnostic
   every other violation produces, before Docker ever spins up.
3. :func:`assert_no_dataset_literals` -- runs inside a sandbox worker
   (``control.sandbox.run_isolated``) against the *real* data, because
   only a raw-row-touching boundary may safely make this comparison.
   Returns only a boolean and a count, never the offending values --
   this closes the case where a model inlines a real lookup table (for
   example a facility-name mapping) directly into generated source.
   Purely numeric/boolean-ish literals and any caller-supplied
   ``known_safe_values`` (a run's own code-map values) are excluded, so
   an entirely legitimate categorical recode (``{"M": "Male", "F":
   "Female"}``) does not spuriously match real cell content that
   happens to share the same short, common label.
4. :func:`run_generated` -- executes the checked source inside
   ``control.runner.ContainerRunner`` (the hardened, network-denied,
   read-only-rootfs boundary step 5 built), never in-process.
5. :func:`workspace_diff_check` plus :func:`assert_no_formula_injection_in_outputs`
   -- the set of files actually created must equal the declared output
   set exactly (a non-LLM check that cannot be talked out of firing,
   which is the property a reviewing agent lacks against prompt
   injection: a model that convinces a reviewer an extra artifact is
   fine still fails this check), and every declared output's actual
   cell content is scanned for an Excel/LibreOffice/Sheets external-link
   formula trigger -- checked on the produced *content*, not the
   generated *source* text, since a source-level literal check both
   false-positives on ordinary code (``"key=value".split("=")``) and
   misses the real vector: a formula-shaped value passed through
   untouched from real input data, or built at runtime.

:func:`generate_with_retry` composes all five behind a bounded
two-attempt budget, rebuilding the prompt from the previous source plus
the structured diagnostic list -- never the free-text exception text --
on failure. Exhausting both attempts raises
:class:`CodeGenerationExhausted`; the caller (Schema/Executor's own
dispatch handler) escalates to human review, never retries a third time
itself.
"""
from __future__ import annotations

import ast
import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..control import limits
from ..control.records import SandboxRecord
from ..control.runner import (
    RESULT_FILENAME,
    ContainerRunner,
    ContainerRunnerError,
    ContainerRunResult,
    ContainerTimeout,
    ContainerWorkerFailure,
)
from ..control.sandbox import SandboxError, SandboxTimeout, run_isolated

# ---- 1. static check --------------------------------------------------

# Every top-level module a generated script may import. Deliberately
# narrow: no `os`/`sys`/`subprocess`/network modules, no `pickle` (an
# arbitrary-deserialization vector on its own).
STATIC_CHECK_ALLOWED_IMPORTS: frozenset[str] = frozenset({
    "csv", "json", "pathlib", "datetime", "hashlib", "hmac", "secrets",
    "random", "re", "math", "statistics", "decimal", "collections",
    "itertools", "typing", "openpyxl", "pandas",
})

# The pandas API surface generated code may use -- a method-name
# allowlist, not a module allowlist. `pandas` itself is an allowed
# import (above), but `pd.read_pickle(...)` is arbitrary code execution
# from inside that allowed module, which is exactly why gating stops at
# the module boundary is not enough.
PANDAS_ALLOWED_METHODS: frozenset[str] = frozenset({
    "read_csv", "read_excel", "to_csv", "to_excel", "DataFrame", "Series",
    "concat", "merge", "isna", "notna", "apply", "map", "astype", "drop",
    "rename", "fillna",
})

# Explicitly denied pandas-shaped attribute names (docs step 9): checked
# unconditionally, on any attribute access with this name regardless of
# the receiver's inferred type, since precise static type inference is
# not achievable from AST alone and a false positive here (rejecting a
# same-named method on some unrelated object) is a safe cost -- a false
# negative on `read_pickle` is not.
_PANDAS_DENIED_METHODS: frozenset[str] = frozenset({
    "read_pickle", "to_pickle", "read_html", "read_xml", "read_sql",
    "to_sql", "read_parquet", "eval", "query",
})
_PANDAS_RELEVANT_ATTRS: frozenset[str] = PANDAS_ALLOWED_METHODS | _PANDAS_DENIED_METHODS

# `Name` loads of any of these are rejected outright: the interpreter/
# introspection primitives that could otherwise defeat every other
# check here (dynamic import, dynamic exec, reflection-based attribute
# access that never spells out a banned dotted path).
_DENIED_NAME_LOADS: frozenset[str] = frozenset({
    "exec", "eval", "compile", "__import__", "globals", "locals", "vars",
    "getattr", "setattr", "delattr", "type", "object", "dir", "help",
    "memoryview", "breakpoint", "input",
})

# Attribute chains rooted at any of these names are rejected regardless
# of how deep the chain goes (`os.path.join(...)`, `subprocess.run(...)`,
# `urllib.request.urlopen(...)`) -- the whole point being that none of
# these modules is ever legitimately needed by a column-transformation
# or extraction script.
_DENIED_MODULE_ROOTS: frozenset[str] = frozenset({
    "os", "sys", "subprocess", "socket", "shutil", "ctypes", "pickle",
    "marshal", "importlib", "urllib", "requests", "http",
})


def _top_level_module(dotted: str) -> str:
    return dotted.split(".", 1)[0]


def _attribute_root_name(node: ast.Attribute) -> str | None:
    """Walk an attribute chain (``a.b.c`` -> the ``ast.Attribute`` for
    ``.c``) down to its root ``Name``, or ``None`` if the chain does not
    bottom out at a bare name (e.g. it starts from a call or a literal,
    which ``_DENIED_MODULE_ROOTS`` cannot apply to meaningfully)."""
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        current = current.value
    return current.id if isinstance(current, ast.Name) else None


# ---- Executor's first-party container shim (rewrite plan step 11) ----

# Rewrite plan step 11: "generated code refers to columns by position
# and opaque token. A first-party non-agent shim inside the container
# resolves token to real header at execution time using the run's
# opaque map. The real header never enters a prompt, a report or the
# generated source." This module is never model-written and never
# regenerated -- it is a fixed constant string materialized into the
# container's ``/input`` alongside the generated sources, exactly like
# ``run_generated``'s other ``source_files`` entries, so generated code
# may ``import phi_container_shim`` to resolve a column token to the
# real header name at execution time. It reads two fixed mounted
# inputs, never a prompt-carried literal: ``/data/column_map.json``
# (opaque token -> real header, this file's slice of the run's opaque
# map) and ``/data/pseudonym_salt.txt`` (the server-held per-session
# salt, D6: never leaves this process otherwise). Deliberately narrow
# stdlib-only surface (``json``, ``pathlib``) so it never itself trips
# :func:`check_generated_code`.
CONTAINER_SHIM_FILENAME = "phi_container_shim.py"
CONTAINER_SHIM_MODULE_NAME = "phi_container_shim"
CONTAINER_SHIM_SOURCE = '''"""First-party container shim -- never model-written. See
agents/codegen.py's CONTAINER_SHIM_SOURCE docstring for the contract."""
import json
from pathlib import Path

_COLUMN_MAP: dict | None = None


def resolve_header(token: str) -> str:
    """Opaque column token -> this file's real header name."""
    global _COLUMN_MAP
    if _COLUMN_MAP is None:
        _COLUMN_MAP = json.loads(Path("/data/column_map.json").read_text(encoding="utf-8"))
    if token not in _COLUMN_MAP:
        raise KeyError(f"unknown column token: {token!r}")
    return _COLUMN_MAP[token]


def load_salt() -> str:
    """The server-held per-session pseudonym salt, mounted read-only --
    never a prompt-carried or source-embedded literal."""
    return Path("/data/pseudonym_salt.txt").read_text(encoding="utf-8").strip()


def load_pseudonym_state() -> dict:
    """This run's accumulated real-value -> pseudonym map so far
    (empty on the first dataset file processed this run)."""
    path = Path("/data/pseudonym_state_in.json")
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


_FORMULA_LEAD_CHARS = ("=", "+", "-", "@", chr(9), chr(13))


def neutralise_formula(value: str) -> str:
    """Deterministic parity with control/transform_primitives.py's own
    _neutralise_formula (the DeterministicVerifier recompute oracle):
    prefix a spreadsheet-formula-shaped value with a leading apostrophe
    so a cell beginning with =, +, -, @, tab, or carriage return lands
    as inert text, never an executable formula, when the recipient
    opens the export in a spreadsheet application. Exposed here, never
    left to model-authored code, so every generated apply module gets
    this security-relevant behaviour for free rather than risking a
    model that forgets to neutralise a pass-through cell."""
    if value and value[0] in _FORMULA_LEAD_CHARS:
        return "'" + value
    return value
'''


def check_generated_code(source: str, *, extra_allowed_modules: frozenset[str] = frozenset()) -> list[str]:
    """Static, pre-execution violations found in ``source``. An empty
    list means the source passed every check here -- not that it is
    safe to run unchecked; :func:`check_entrypoint_shape`,
    :func:`assert_no_dataset_literals`, and the container boundary are
    separate, still-mandatory controls.

    A syntax error is itself a violation, reported as a single-item
    list rather than raising, so a caller can fold it into the same
    retry-diagnostic path as every other check."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"syntax_error: {exc.msg} (line {exc.lineno})"]

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = _top_level_module(alias.name)
                if top not in STATIC_CHECK_ALLOWED_IMPORTS and top not in extra_allowed_modules:
                    violations.append(f"import_denied: {alias.name!r} not in the allowed import list")
        elif isinstance(node, ast.ImportFrom):
            top = _top_level_module(node.module or "")
            if top not in STATIC_CHECK_ALLOWED_IMPORTS and top not in extra_allowed_modules:
                violations.append(f"import_denied: {node.module!r} not in the allowed import list")
                continue
            # The module itself is allowed, but a *name* imported out of
            # it can still be arbitrary code execution
            # (`from pandas import read_pickle`) or unverifiable
            # (`from pandas import *`, which imports everything the
            # module exports without ever spelling out a name this
            # function could inspect). Both are checked here, at the
            # import statement itself, rather than trying to catch every
            # later use of the bound name -- denying the import is
            # simpler and cannot be defeated by reassignment.
            for alias in node.names:
                if alias.name == "*":
                    violations.append(f"import_denied: wildcard import from {node.module!r} cannot be statically verified")
                elif alias.name in _PANDAS_DENIED_METHODS:
                    violations.append(f"pandas_method_denied: {alias.name!r} (imported as a bare name)")
                elif alias.name in _DENIED_NAME_LOADS:
                    violations.append(f"name_denied: {alias.name!r} (imported as a bare name)")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                violations.append(f"dunder_attribute_denied: {node.attr!r}")
                continue
            if node.attr in _PANDAS_RELEVANT_ATTRS and node.attr not in PANDAS_ALLOWED_METHODS:
                violations.append(f"pandas_method_denied: {node.attr!r}")
                continue
            root = _attribute_root_name(node)
            if root is not None and root in _DENIED_MODULE_ROOTS:
                violations.append(f"module_root_denied: {root!r}")
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in _DENIED_NAME_LOADS:
                violations.append(f"name_denied: {node.id!r}")
    return violations


def check_entrypoint_shape(source: str) -> list[str]:
    """Structural check, run alongside :func:`check_generated_code` but
    kept separate from it: generated source must define a top-level
    ``def run()`` (or ``async def run()``) -- the exact entrypoint
    :func:`run_generated`/``ContainerRunner`` calls. A function named
    ``run`` nested inside another function does not satisfy this (it is
    not reachable as the module's own entrypoint). A syntax error is
    not re-diagnosed here -- :func:`check_generated_code` already
    reports it, and an unparseable source has no "top level" to check."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    has_run = any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run"
        for node in tree.body
    )
    return [] if has_run else ["missing_entrypoint: no top-level def run(...) found"]


_CODE_FENCE_RE = re.compile(r"^```(?:python)?\n(.*?)\n?```\s*$", re.DOTALL)


def _strip_code_fences(text: str) -> str:
    """Remove a single leading/trailing Markdown code fence (either
    ` ``` ` or ` ```python `) that an LLM commonly wraps generated
    source in. Idempotent on text that already has no fence -- only
    ``.strip()`` applies in that case."""
    stripped = text.strip()
    match = _CODE_FENCE_RE.match(stripped)
    return match.group(1) if match else stripped


# ---- 2. dataset-literal check (sandboxed) -----------------------------

# Tokens with no security significance as an inlined lookup-table entry
# even past the length-4 floor below: a purely numeric string (a
# sentinel code, a zip-shaped digit run) is checked via `float()`
# parsing; these boolean-ish words are checked by exact (case-folded)
# match. Judge's classification decisions and Lexicon's own code maps
# routinely carry values shaped exactly like this (a `"9999"` missing-
# value sentinel, a `"true"`/`"false"` recoded flag), and matching them
# here would exhaust every generation attempt on entirely legitimate
# code -- see `assert_no_dataset_literals`'s own docstring.
_BOOLEAN_ISH_TOKENS: frozenset[str] = frozenset({
    "true", "false", "yes", "no", "y", "n", "t", "f", "null", "none",
    "nan", "na", "n/a",
})


def _is_numeric_or_boolean_ish(value: str) -> bool:
    if value.strip().lower() in _BOOLEAN_ISH_TOKENS:
        return True
    try:
        float(value)
    except ValueError:
        return False
    return True


def _extract_string_literals(source: str, *, known_safe_values: frozenset[str] = frozenset()) -> list[str]:
    """Every string constant in ``source``'s own AST, deduplicated,
    excluding: literals under 4 characters (single characters and
    common separators produce overwhelming false-positive matches
    against any real dataset, with no security value -- a real inlined
    lookup-table entry is never that short), purely numeric/boolean-ish
    tokens (see :func:`_is_numeric_or_boolean_ish`), and any value the
    caller already knows is a legitimate code-map/categorical-label
    value (``known_safe_values`` -- intended to carry a run's own
    ``StudyKnowledgePackage`` code-map values once Executor's rewrite,
    step 11, wires it through; empty here since nothing populates it
    yet, which is a known, documented limitation until that wiring
    lands, not a claim that the exclusion is complete today)."""
    tree = ast.parse(source)
    literals: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) >= 4:
            if _is_numeric_or_boolean_ish(node.value) or node.value in known_safe_values:
                continue
            literals.add(node.value)
    return sorted(literals)


def _count_literal_matches(literals: list[str], dataset_path: str) -> int:
    """Sandbox worker body (runs inside ``run_isolated``, a separate,
    network-denied process): count how many of ``literals`` appear as an
    exact cell value anywhere in the dataset at ``dataset_path``. Never
    returns which literal matched or which cell it came from -- only the
    count, per this module's own zero-row-read-outside-the-sandbox
    invariant."""
    import pandas as pd

    if not literals:
        return 0
    needles = set(literals)
    frame = pd.read_csv(dataset_path, dtype=str, keep_default_na=False) if str(dataset_path).endswith(".csv") \
        else pd.read_excel(dataset_path, dtype=str)
    hits = 0
    for column in frame.columns:
        values = set(frame[column].dropna().astype(str))
        hits += len(needles & values)
    return hits


def assert_no_dataset_literals(
    record: SandboxRecord, source: str, dataset_path: str, *, known_safe_values: frozenset[str] = frozenset(),
) -> tuple[bool, int]:
    """``True`` (plus the match count) when at least one string literal
    in ``source`` also appears as a real cell value in the dataset at
    ``dataset_path``, checked inside ``record``'s sandbox -- never in
    the calling agent's own process, since only a raw-row-touching
    boundary may safely make this comparison. A hit is a hard
    violation: the generated code inlined a real value rather than
    deriving it, exactly the case a real lookup table (a facility-name
    mapping, for example) baked into ``transformations.py`` would
    produce."""
    literals = _extract_string_literals(source, known_safe_values=known_safe_values)
    if not literals:
        return False, 0
    count = run_isolated(record, _count_literal_matches, literals, dataset_path, return_kind="count")
    return count > 0, count


# ---- 3. sandboxed execution --------------------------------------------

def run_generated(
    source_files: dict[str, str], entrypoint: str, inputs: dict[str, str], *,
    timeout_s: int = limits.MAX_CONTAINER_WALL_SECONDS,
) -> ContainerRunResult:
    """Write ``source_files`` into a fresh container workspace and run
    ``entrypoint``'s own ``run()`` function through
    :class:`~..control.runner.ContainerRunner` with ``return_kind="path"``
    -- the checked generated code is executed in the hardened,
    network-denied, read-only-rootfs boundary, never in-process.
    ``entrypoint`` is the filename (e.g. ``"extract.py"``), matching
    ``ContainerRunner.run``'s own contract exactly: it must be a key of
    ``source_files``."""
    return ContainerRunner().run(source_files, entrypoint, inputs, return_kind="path", timeout_s=timeout_s)


def snapshot_workspace(workspace_path: Path) -> frozenset[str]:
    """The set of relative filenames currently under ``workspace_path``,
    for :func:`workspace_diff_check`'s before/after comparison."""
    if not workspace_path.exists():
        return frozenset()
    return frozenset(str(p.relative_to(workspace_path)) for p in workspace_path.rglob("*") if p.is_file())


def workspace_diff_check(before: frozenset[str], after: frozenset[str], declared_outputs: frozenset[str]) -> bool:
    """``True`` only when the files created between ``before`` and
    ``after`` equal ``declared_outputs`` exactly. Any extra artifact --
    or a missing declared one -- fails the run. A non-LLM check that
    cannot be talked out of firing, which is the property a reviewing
    agent lacks against prompt injection. ``ContainerRunner`` always
    leaves its own ``RESULT_FILENAME`` bookkeeping file in the
    workspace regardless of what the generated code did; the caller is
    expected to exclude it from ``after`` before calling this (see
    :func:`_try_one_generation`), since it is never a declarable output
    and never absent."""
    created = after - before
    return created == set(declared_outputs)


# ---- 4. formula-injection check on real output content (sandboxed) ----

# The unambiguous Excel/LibreOffice/Sheets external-formula leading
# characters (OWASP CSV/formula-injection guidance). Checked against
# real *output cell content*, never generated source text: a source-
# level check both false-positives on ordinary code (`"key=value"
# .split("=")`, `separator = "="`) and misses the actual vector -- a
# formula-shaped value that flows through untouched from real input
# data, or is built at runtime rather than ever appearing as a single
# source literal.
_FORMULA_LEADING_ALWAYS: frozenset[str] = frozenset({"=", "@"})
_FORMULA_LEADING_UNLESS_NUMERIC: frozenset[str] = frozenset({"+", "-", "\t", "\r"})


def _is_formula_trigger_value(value: str) -> bool:
    if not value:
        return False
    lead = value[0]
    if lead in _FORMULA_LEADING_ALWAYS:
        return True
    if lead in _FORMULA_LEADING_UNLESS_NUMERIC:
        # A numeric sentinel (`-99`, `+1`) is common, legitimate
        # transformed output and must never be flagged; a value that
        # merely *starts* with one of these characters but is not
        # itself a plain number is the actual external-formula shape
        # (e.g. `-cmd|'/C calc'!A1`, `+HYPERLINK(...)`).
        try:
            float(value)
        except ValueError:
            return True
        return False
    return False


_TABULAR_OUTPUT_SUFFIXES: frozenset[str] = frozenset({".csv", ".tsv", ".xlsx", ".xls"})


def _scan_paths_for_formula_injection(paths: list[str]) -> int:
    """Sandbox worker body: count cells across the csv/xlsx files at
    ``paths`` whose value an Excel/LibreOffice/Sheets client would
    interpret as an external-link formula on open. A declared output
    with no spreadsheet-shaped suffix (``.json``, ``.txt``, ...) is
    skipped outright -- formula injection is a spreadsheet-cell
    concept, and attempting to parse a non-tabular file as one would
    raise, not silently pass. ``header=None`` on every read: unlike
    :func:`_count_literal_matches` (whose header row is already known
    to the LLM by design, per this system's zero-row-read invariant),
    an *output* file's header row is a real, LLM-generated cell like
    any other, and Excel does not distinguish a header cell from a
    data cell when deciding whether to interpret it as a formula on
    open -- excluding row 0 here would leave exactly that cell
    unchecked. ``.tsv`` is read with ``sep="\\t"``, not the default
    comma: without it, a genuinely tab-separated file collapses into
    one string-valued column per row and every embedded formula
    trigger silently escapes detection (verified empirically before
    this fix). Never returns which cell or file matched -- only the
    count, matching this module's own zero-row-read-outside-the-
    sandbox invariant."""
    import pandas as pd

    hits = 0
    for raw_path in paths:
        path = Path(raw_path)
        suffix = path.suffix.lower()
        if not path.is_file() or suffix not in _TABULAR_OUTPUT_SUFFIXES:
            continue
        if suffix in (".csv", ".tsv"):
            sep = "\t" if suffix == ".tsv" else ","
            frame = pd.read_csv(path, dtype=str, keep_default_na=False, header=None, sep=sep)
        else:
            frame = pd.read_excel(path, dtype=str, header=None)
        for column in frame.columns:
            hits += sum(1 for v in frame[column].dropna().astype(str) if _is_formula_trigger_value(v))
    return hits


def assert_no_formula_injection_in_outputs(record: SandboxRecord, output_paths: list[str]) -> tuple[bool, int]:
    """``True`` (plus the match count) when any file at ``output_paths``
    contains a cell value that would be interpreted as an external-link
    formula by Excel/LibreOffice/Sheets on open -- the output-content
    analogue of :func:`assert_no_dataset_literals`, checked inside
    ``record``'s sandbox against the real produced content."""
    if not output_paths:
        return False, 0
    count = run_isolated(record, _scan_paths_for_formula_injection, output_paths, return_kind="count")
    return count > 0, count


# ---- 5. the bounded retry loop -----------------------------------------

class CodeGenerationExhausted(RuntimeError):
    """Raised by :func:`generate_with_retry` once every attempt has
    failed the full check chain. The caller escalates to human review;
    this function never retries a third time itself."""

    def __init__(self, message: str, *, diagnostics: list[str]) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


@dataclass(frozen=True)
class _AttemptFailure:
    diagnostics: list[str]


async def generate_with_retry(
    agent: Any,
    build_prompt: Callable[[str | None, list[str] | None], str],
    *,
    phase: str,
    dataset_path: str,
    inputs: dict[str, str],
    entrypoint: str,
    declared_outputs: frozenset[str],
    sandbox: SandboxRecord,
    attempts: int = 2,
    call_timeout_s: float | None = None,
    known_safe_values: frozenset[str] = frozenset(),
    extra_sources: dict[str, str] | None = None,
    extra_allowed_modules: frozenset[str] = frozenset(),
) -> tuple[str, ContainerRunResult]:
    """Drive up to ``attempts`` full generate-check-execute-verify
    rounds. ``build_prompt(previous_source, previous_diagnostics)`` is
    called fresh each round -- ``None``/``None`` on the first -- so the
    caller controls exactly how the previous failure is folded back into
    the prompt; this function only ever passes the structured diagnostic
    list, never a raw exception's free text.

    ``extra_sources`` (rewrite plan step 11): already-validated companion
    modules -- a shared ``transformations.py`` and/or a first-party,
    non-generated shim -- materialized alongside ``entrypoint`` in the
    same container run so the newly generated source may import them.
    Never re-generated here; every attempt reuses the same
    ``extra_sources`` verbatim. Still re-checked by
    :func:`check_generated_code` and scanned for dataset literals on
    every attempt, against *this* attempt's dataset, since a companion
    module validated once against one dataset file is not thereby proven
    safe against every other file in the same run. ``extra_allowed_modules``
    names the extra sources' own module names (their filename stems) so
    ``import transformations`` in the generated entrypoint is not itself
    flagged as an unlisted import.

    Returns ``(source, result)`` on success -- ``source`` is only the
    newly generated ``entrypoint`` source, never ``extra_sources``, which
    the caller already has. Raises :class:`CodeGenerationExhausted`
    (carrying every attempt's diagnostics) once ``attempts`` rounds have
    all failed.
    """
    previous_source: str | None = None
    previous_diagnostics: list[str] | None = None
    all_diagnostics: list[str] = []

    for attempt in range(1, attempts + 1):
        prompt = build_prompt(previous_source, previous_diagnostics)
        source = await agent.call(prompt, phase=phase, timeout_s=call_timeout_s)
        outcome = await _try_one_generation(
            source=source, dataset_path=dataset_path, inputs=inputs, entrypoint=entrypoint,
            declared_outputs=declared_outputs, sandbox=sandbox, known_safe_values=known_safe_values,
            extra_sources=extra_sources, extra_allowed_modules=extra_allowed_modules,
        )
        if isinstance(outcome, ContainerRunResult):
            return source, outcome
        previous_source = source
        previous_diagnostics = outcome.diagnostics
        all_diagnostics.extend(outcome.diagnostics)
        if attempt >= attempts:
            raise CodeGenerationExhausted(
                f"code generation exhausted after {attempts} attempts", diagnostics=all_diagnostics,
            )
    raise CodeGenerationExhausted(f"code generation exhausted after {attempts} attempts", diagnostics=all_diagnostics)


async def _try_one_generation(
    *, source: str, dataset_path: str, inputs: dict[str, str], entrypoint: str,
    declared_outputs: frozenset[str], sandbox: SandboxRecord,
    known_safe_values: frozenset[str] = frozenset(),
    extra_sources: dict[str, str] | None = None,
    extra_allowed_modules: frozenset[str] = frozenset(),
) -> "ContainerRunResult | _AttemptFailure":
    """One full check_generated_code / check_entrypoint_shape /
    assert_no_dataset_literals / run_generated / workspace_diff_check /
    assert_no_formula_injection_in_outputs round. Returns the successful
    :class:`ContainerRunResult` directly, or an :class:`_AttemptFailure`
    carrying this round's structured diagnostics -- a plain
    either-of-two-types return, not a side channel, so
    :func:`generate_with_retry` never needs a second lookup to recover
    a successful run's result.

    ``assert_no_dataset_literals`` and ``run_generated`` are both
    blocking calls (multiprocessing spawn+join; a foreground `docker
    run` up to the container wall-clock budget); both are routed through
    ``asyncio.to_thread`` so a single generation attempt never stalls
    every other agent's provider call or the SSE stream sharing this
    event loop -- the same pattern ``agents/reasoning.py`` already uses
    for its own ``run_isolated`` call sites."""
    source = _strip_code_fences(source)
    if not source.strip():
        return _AttemptFailure(diagnostics=["empty_reply: the model returned no source"])

    extra_sources = extra_sources or {}
    violations = check_generated_code(source, extra_allowed_modules=extra_allowed_modules) + check_entrypoint_shape(source)
    for name, extra_source in extra_sources.items():
        violations += [
            f"{name}: {v}" for v in check_generated_code(extra_source, extra_allowed_modules=extra_allowed_modules)
        ]
    if violations:
        return _AttemptFailure(diagnostics=violations)

    try:
        has_literal, literal_count = await asyncio.to_thread(
            assert_no_dataset_literals, sandbox,
            "\n".join([source, *extra_sources.values()]), dataset_path,
            known_safe_values=known_safe_values,
        )
    except SandboxTimeout:
        return _AttemptFailure(diagnostics=["dataset_literal_check_timeout"])
    except SandboxError as exc:
        return _AttemptFailure(diagnostics=[f"dataset_literal_check_failed: {type(exc).__name__}"])
    if has_literal:
        return _AttemptFailure(diagnostics=[f"dataset_literal_detected: {literal_count} literal(s) matched real data"])

    before = frozenset()
    try:
        result = await asyncio.to_thread(run_generated, {**extra_sources, entrypoint: source}, entrypoint, inputs)
    except ContainerTimeout:
        return _AttemptFailure(diagnostics=["execution_timeout"])
    except (ContainerRunnerError, ContainerWorkerFailure) as exc:
        return _AttemptFailure(diagnostics=[f"execution_failed: {type(exc).__name__}"])

    # ContainerRunner always leaves its own RESULT_FILENAME bookkeeping
    # file in the workspace -- it is not a declarable output and never
    # absent, so it is excluded before the diff comparison rather than
    # ever appearing as a phantom "extra artifact".
    after = snapshot_workspace(result.workspace_path) - {RESULT_FILENAME}
    if not workspace_diff_check(before, after, declared_outputs):
        result.cleanup()
        return _AttemptFailure(diagnostics=[
            f"workspace_diff_mismatch: expected exactly {sorted(declared_outputs)}, got {sorted(after)}",
        ])

    output_paths = [str(result.workspace_path / name) for name in declared_outputs]
    try:
        has_formula, formula_count = await asyncio.to_thread(
            assert_no_formula_injection_in_outputs, sandbox, output_paths,
        )
    except SandboxTimeout:
        result.cleanup()
        return _AttemptFailure(diagnostics=["formula_scan_timeout"])
    except SandboxError as exc:
        result.cleanup()
        return _AttemptFailure(diagnostics=[f"formula_scan_failed: {type(exc).__name__}"])
    if has_formula:
        result.cleanup()
        return _AttemptFailure(diagnostics=[f"formula_injection_in_output: {formula_count} cell(s) matched"])

    return result
