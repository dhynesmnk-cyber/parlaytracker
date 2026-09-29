"""Everything the application imports must be declared in pyproject.toml.

A package that happens to be installed in a developer's environment (or arrives as someone
else's dependency) passes locally and then fails in the image and in CI. Phase 6 shipped
importing `openai` without declaring it; this is the test that would have caught it.
"""
import ast
import re
import sys
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "parlaytracker"


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def declared() -> set[str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    return {normalise(re.split(r"[<>=!~\[; ]", spec, maxsplit=1)[0])
            for spec in project["dependencies"]}


def imported_modules() -> dict[str, list[str]]:
    """Top-level module -> files that import it, including imports inside functions."""
    found: dict[str, list[str]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                found.setdefault(top, []).append(str(path.relative_to(ROOT)))
    return found


def undeclared(have: set[str]) -> dict[str, list[str]]:
    """Third-party modules the application imports that no declared distribution provides."""
    to_dists = packages_distributions()
    missing = {}
    for module, files in imported_modules().items():
        if module in sys.stdlib_module_names or module == "parlaytracker":
            continue
        if not {normalise(d) for d in to_dists.get(module, [])} & have:
            missing[module] = sorted(set(files))[:3]
    return missing


def test_every_third_party_import_is_declared():
    missing = undeclared(declared())
    assert not missing, (
        f"imported but not declared in pyproject.toml [project] dependencies: {missing}")


def test_the_check_reports_a_package_that_is_not_declared():
    """Guard the guard: take a package out of the declarations and it must be named."""
    imported_as = {"openai": "openai", "pillow": "PIL", "pandas": "pandas",
                   "rapidfuzz": "rapidfuzz"}
    for distribution, module in imported_as.items():
        assert distribution in declared(), f"{distribution} should be declared"
        assert module in undeclared(declared() - {distribution}), distribution
