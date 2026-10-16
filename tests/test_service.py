"""The Linux systemd unit must be runnable as Python, unchanged.

These tests pin the verified Linux behaviour: the generated unit runs the
module with the current interpreter and PYTHONPATH, keeps ``KillMode=process``
so an in-flight resume is never signalled, and restarts on failure without
rate limiting.
"""

from __future__ import annotations

import sys
import unittest
from unittest import mock

from support import REPO_ROOT, StateCase
from codex_auto_resume.service import linux


class UnitTemplateTests(StateCase):
    def test_generated_unit_runs_the_module_with_python(self) -> None:
        with mock.patch.object(linux, "_systemctl", return_value=""), mock.patch.object(
            linux, "UNIT_NAME", "codex-auto-resume-helper-test.service"
        ):
            path = linux.install_unit(codex_bin="/usr/bin/codex")
        self.addCleanup(path.unlink, missing_ok=True)
        content = path.read_text(encoding="utf-8")
        self.assertIn(f"ExecStart={sys.executable} -m codex_auto_resume helper", content)
        self.assertIn("Environment=PYTHONPATH=", content)
        self.assertIn("Environment=CODEX_AUTO_RESUME_CODEX_BIN=/usr/bin/codex", content)
        self.assertIn("KillMode=process", content)
        self.assertIn("Restart=on-failure", content)
        self.assertIn("StartLimitIntervalSec=0", content)
        self.assertIn("WantedBy=default.target", content)
        self.assertNotIn("sh ", content.splitlines()[0])

    def test_package_parent_points_at_the_source_tree(self) -> None:
        self.assertEqual(linux.package_parent(), str(REPO_ROOT / "src"))

    def test_unit_path_is_the_user_unit(self) -> None:
        parts = linux.unit_path().parts
        self.assertEqual(
            parts[-3:], ("systemd", "user", "codex-auto-resume-helper.service")
        )
        self.assertTrue(str(linux.unit_path()).endswith(linux.UNIT_NAME))

    def test_render_escapes_percent_signs_for_systemd(self) -> None:
        rendered = linux.render_unit("/opt/100% codex/codex", python="/usr/bin/python3")
        self.assertIn("CODEX_AUTO_RESUME_CODEX_BIN=/opt/100%% codex/codex", rendered)


if __name__ == "__main__":
    unittest.main()
