"""The generated systemd user unit must be runnable as Python."""

from __future__ import annotations

import sys
import unittest
from unittest import mock

from support import StateCase
from codex_auto_resume import service


class UnitTemplateTests(StateCase):
    def test_generated_unit_runs_the_module_with_python(self) -> None:
        with mock.patch.object(service, "_systemctl", return_value=""), mock.patch.object(
            service, "UNIT_NAME", "codex-auto-resume-helper-test.service"
        ):
            path = service.install_unit(codex_bin="/usr/bin/codex")
        self.addCleanup(path.unlink, missing_ok=True)
        content = path.read_text(encoding="utf-8")
        self.assertIn(f"ExecStart={sys.executable} -m codex_auto_resume helper", content)
        self.assertIn("Environment=PYTHONPATH=", content)
        self.assertIn("Environment=CODEX_AUTO_RESUME_CODEX_BIN=/usr/bin/codex", content)
        self.assertIn("KillMode=process", content)
        self.assertIn("WantedBy=default.target", content)
        self.assertNotIn("sh ", content.splitlines()[0])

    def test_package_parent_points_at_the_source_tree(self) -> None:
        from support import REPO_ROOT

        self.assertEqual(service._package_parent(), str(REPO_ROOT / "src"))


if __name__ == "__main__":
    unittest.main()
