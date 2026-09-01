from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _requirements() -> list[str]:
    lines = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


class PackagingTests(unittest.TestCase):
    """The Docker image installs requirements.txt while local dev installs pyproject.

    Nothing keeps the two lists aligned at runtime, so drift would mean the container
    ships different versions than anything that was tested. Assert they match instead.
    """

    def test_requirements_match_pyproject_dependencies(self):
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        declared = pyproject["project"]["dependencies"]
        self.assertEqual(sorted(_requirements()), sorted(declared))

    def test_every_runtime_dependency_is_pinned(self):
        for requirement in _requirements():
            self.assertIn("==", requirement, f"{requirement} is not pinned to an exact version")

    def test_mobile_library_cards_are_excluded_from_wide_table_minimum(self):
        styles = (REPO_ROOT / "app/static/styles.css").read_text(encoding="utf-8")
        self.assertIn(
            ".table-wrap:not(.responsive-records):not(.library-table) > table",
            styles,
        )
        self.assertNotIn(
            "  .table-wrap:not(.responsive-records) > table,",
            styles,
        )


if __name__ == "__main__":
    unittest.main()
