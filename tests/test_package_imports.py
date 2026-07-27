"""Whole-package import smoke test.

``phi_engine.utils.snapshot`` shipped with a stray dedented statement that
made the module a syntax error (``IndentationError: unexpected indent``) --
908 other tests passed anyway because nothing in the suite ever imported
that module. This test closes that blind spot generically: it walks every
module under the three top-level source packages and imports each one, so
any future syntax error, or an import of a module that no longer exists,
in a module the rest of the suite happens not to exercise turns into a
suite failure instead of a silent landmine.

Deliberately does NOT call into anything -- module-local, function-body
imports (e.g. an import statement inside a function that only runs when
that function is called) are out of scope here by design; that class of
staleness is caught by exercising the function itself, not by import
discovery.
"""

from __future__ import annotations

import importlib
import pkgutil
import traceback

import pytest

_TOP_LEVEL_PACKAGES = ("phi_engine", "harness", "scripts")


def _discover_modules() -> list[str]:
    names: list[str] = []
    for root in _TOP_LEVEL_PACKAGES:
        package = importlib.import_module(root)
        names.append(root)
        for module_info in pkgutil.walk_packages(package.__path__, prefix=f"{root}."):
            names.append(module_info.name)
    return sorted(names)


@pytest.mark.parametrize("module_name", _discover_modules())
def test_module_imports_cleanly(module_name: str) -> None:
    try:
        importlib.import_module(module_name)
    except BaseException as exc:  # noqa: BLE001 -- report every failure mode, not just ImportError
        formatted = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        pytest.fail(f"{module_name} failed to import: {formatted}")
