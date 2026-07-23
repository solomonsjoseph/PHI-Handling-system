"""Shared test-only support for the standalone-pipeline test suite.

Not a test module itself (no ``test_`` prefix, never collected by pytest).
Provides two things every intake-driving test file otherwise re-implements:

1. :func:`hermetic_phi_workspace` -- env + ``phi_engine.*`` module isolation
   for one ``PHI_WORKSPACE``/study. Evicts every ``phi_engine.*`` module
   except the resident ``phi_engine.utils.pipeline_lock`` (never evicted, so
   its ``os.register_at_fork`` registration is not repeated across tests),
   rebinds that module's own frozen ``config`` attribute to the fresh
   import for the duration of the context, and on exit restores
   ``sys.modules`` plus every touched parent-package attribute to their
   EXACT pre-context identity. That last part matters for combined-order
   runs: a test file that captures a ``phi_engine.*`` reference once at
   collection time (e.g. ``import phi_engine.pipeline.run as pipeline_run``)
   depends on that object's identity staying stable for the rest of the
   session -- a workspace context that evicts modules and never restores
   them leaves a later file's own fresh, unpatched re-import bound to a
   different ``config`` object than the one that file's own top-level
   monkeypatches targeted.
2. Minimal deterministic writers for the intake-manifest/v3 mandatory
   ``datasets/`` + ``forms/`` + (``data_dictionary/`` or ``mappings/``)
   source-package layout -- CSV, single-sheet XLSX, and a tiny PDF form.
"""

from __future__ import annotations

import csv
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator

TEST_PHI_KEY_HEX = "0" * 64

_UNSET = object()  # sentinel: parent had no such attribute before the context
_KEEP_RESIDENT = frozenset({"phi_engine", "phi_engine.utils", "phi_engine.utils.pipeline_lock"})


# --- hermetic phi_engine.* module + env isolation ------------------------------------------


def phi_runtime_module_names() -> set[str]:
    """Names in ``sys.modules`` a fresh-import cycle would evict."""
    return {name for name in sys.modules if name.startswith("phi_engine.") and name not in _KEEP_RESIDENT}


def drop_phi_runtime_modules() -> None:
    for name in phi_runtime_module_names():
        del sys.modules[name]


def _snapshot_parent_attr(name: str) -> tuple[str, str, object]:
    parent_name, _, leaf = name.rpartition(".")
    parent = sys.modules.get(parent_name)
    previous = getattr(parent, leaf, _UNSET) if parent is not None else _UNSET
    return parent_name, leaf, previous


def _restore_phi_runtime_modules(
    saved_modules: dict[str, ModuleType],
    saved_parent_attrs: dict[str, tuple[str, str, object]],
    current_names: set[str],
) -> None:
    sys.modules.update(saved_modules)
    for name in current_names | saved_modules.keys():
        parent_name, leaf, previous = saved_parent_attrs.get(name, (None, None, _UNSET))
        if parent_name is None:
            parent_name, _, leaf = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is None:
            continue
        if previous is _UNSET:
            if hasattr(parent, leaf):
                delattr(parent, leaf)
        else:
            setattr(parent, leaf, previous)


@contextmanager
def hermetic_phi_workspace(
    tmp_path: Path, study: str, *, workspace: Path | None = None
) -> Iterator[Path]:
    """Set ``PHI_WORKSPACE``/``STUDY_NAME``/``PHI_KEY_PATH`` for *study*,
    force every non-resident ``phi_engine.*`` module to re-import against
    them, and fully restore prior module identity + env on exit."""
    old_workspace = os.environ.get("PHI_WORKSPACE")
    old_study = os.environ.get("STUDY_NAME")
    old_key = os.environ.get("PHI_KEY_PATH")
    key = tmp_path / "phi_key"
    key.write_text(TEST_PHI_KEY_HEX, encoding="utf-8")
    key.chmod(0o600)
    workspace_path = workspace if workspace is not None else (tmp_path / "workspace")

    pre_existing_names = phi_runtime_module_names()
    saved_modules = {name: sys.modules[name] for name in pre_existing_names}
    saved_parent_attrs = {name: _snapshot_parent_attr(name) for name in pre_existing_names}
    pipeline_lock_module = sys.modules.get("phi_engine.utils.pipeline_lock")
    original_pipeline_lock_config = (
        pipeline_lock_module.config if pipeline_lock_module is not None else None
    )

    try:
        os.environ["PHI_WORKSPACE"] = str(workspace_path)
        os.environ["STUDY_NAME"] = study
        os.environ["PHI_KEY_PATH"] = str(key)
        drop_phi_runtime_modules()
        import phi_engine.config.config as fresh_config

        if pipeline_lock_module is not None:
            pipeline_lock_module.config = fresh_config
        yield Path(os.environ["PHI_WORKSPACE"])
    finally:
        if old_workspace is None:
            os.environ.pop("PHI_WORKSPACE", None)
        else:
            os.environ["PHI_WORKSPACE"] = old_workspace
        if old_study is None:
            os.environ.pop("STUDY_NAME", None)
        else:
            os.environ["STUDY_NAME"] = old_study
        if old_key is None:
            os.environ.pop("PHI_KEY_PATH", None)
        else:
            os.environ["PHI_KEY_PATH"] = old_key
        if pipeline_lock_module is not None:
            pipeline_lock_module.config = original_pipeline_lock_config
        current_names = phi_runtime_module_names()
        drop_phi_runtime_modules()
        _restore_phi_runtime_modules(saved_modules, saved_parent_attrs, current_names)


# --- minimal intake-manifest/v3 source-package writers ---------------------------------------


def write_csv(path: Path, headers: list[str], rows: list[list[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        writer.writerows(rows)


def write_single_sheet_xlsx(
    path: Path, headers: list[str], rows: list[list[Any]], *, sheet_title: str = "Sheet1"
) -> None:
    from openpyxl import Workbook

    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title
    ws.append(headers)
    for row in rows:
        ws.append(row)
    wb.save(str(path))


def write_pdf_form(path: Path, lines: list[str]) -> None:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas as _canvas

    path.parent.mkdir(parents=True, exist_ok=True)
    c = _canvas.Canvas(str(path), pagesize=letter)
    y = 720
    for line in lines:
        c.drawString(72, y, line)
        y -= 20
    c.save()


def write_pdf_table(path: Path, headers: list[str], rows: list[list[Any]]) -> None:
    """A PDF whose content is an actual extractable table (as opposed to
    ``write_pdf_form``'s plain text) -- routes through the organizer's
    table-extraction path instead of its non-blocking
    'pdf-no-extractable-table' review branch."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(path), pagesize=letter)
    data = [headers] + [[str(cell) for cell in row] for row in rows]
    table = Table(data)
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 1, colors.black)]))
    doc.build([table])


def write_minimal_intake_package(
    root: Path,
    *,
    dataset_name: str = "dataset.csv",
    dataset_headers: list[str] | None = None,
    dataset_rows: list[list[Any]] | None = None,
    form_name: str = "form.pdf",
    dictionary_name: str = "dictionary.csv",
) -> None:
    """The smallest v3-ready package: one CSV dataset, one PDF form
    carrying an extractable table (so it never lands in the organizer's
    non-blocking 'pdf-no-extractable-table' review bucket), one dictionary
    CSV -- satisfies intake_preflight's mandatory ``datasets/`` +
    ``forms/`` + (``data_dictionary/`` or ``mappings/``) requirement. The
    dictionary intentionally does NOT name any dataset column, so it never
    triggers a same-stem/exact-header dependency recommendation a caller
    would otherwise have to resolve before a clean (exit_code 0) run."""
    write_csv(
        root / "datasets" / dataset_name,
        dataset_headers or ["SUBJID", "AGE"],
        dataset_rows or [["S001", "40"], ["S002", "52"]],
    )
    write_pdf_table(root / "forms" / form_name, ["FIELD", "VALUE"], [["consent", "signed"]])
    write_csv(
        root / "data_dictionary" / dictionary_name,
        ["reference_code", "reference_label"],
        [["REF-01", "General study reference material"]],
    )
