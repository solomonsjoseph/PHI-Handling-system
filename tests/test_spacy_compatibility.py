from __future__ import annotations

import re
import warnings
from importlib import import_module

import pytest
import spacy


SPACY_MODEL_PACKAGES = ("en_core_web_sm", "en_core_web_lg")


def _major_minor(version: str) -> tuple[str, str]:
    match = re.match(r"^(\d+)\.(\d+)", version)
    assert match is not None, f"could not parse major.minor from version {version!r}"
    return match.group(1), match.group(2)


@pytest.mark.parametrize("package_name", SPACY_MODEL_PACKAGES)
def test_installed_spacy_model_major_minor_matches_runtime(package_name: str) -> None:
    """spaCy model packages must match the installed spaCy runtime major.minor."""
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"^\[W095\] Model .* may not be 100% compatible",
                category=UserWarning,
            )
            model_package = import_module(package_name)
    except ModuleNotFoundError as exc:
        if exc.name == package_name:
            pytest.fail(f"{package_name} is not installed", pytrace=False)
        raise

    spacy_major_minor = _major_minor(spacy.__version__)
    model_major_minor = _major_minor(model_package.__version__)

    assert model_major_minor == spacy_major_minor, (
        f"{package_name} {model_package.__version__} is incompatible with "
        f"spaCy {spacy.__version__}: expected {package_name} major.minor "
        f"{'.'.join(model_major_minor)} to match spaCy major.minor "
        f"{'.'.join(spacy_major_minor)}"
    )
