"""The checked-in reference service files must not drift from the generators.

``systemd/``, ``launchd/`` and ``windows/`` hold human-readable copies of what
``codex-auto-resume install-service`` writes. They are documentation, so nothing
fails when they rot unless a test compares them; this module compares the
*structure* (keys, elements, actions) of each reference with the generated
artefact and fails on drift.
"""

from __future__ import annotations

import unittest
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from support import REPO_ROOT, StateCase
from codex_auto_resume.service import linux, macos, windows


def unit_keys(text: str) -> list[str]:
    return [
        line.split("=", 1)[0]
        for line in text.splitlines()
        if line and not line.startswith("#") and "=" in line
    ]


class SystemdReferenceTests(StateCase):
    def test_reference_unit_matches_the_generated_keys(self) -> None:
        reference = (REPO_ROOT / "systemd" / "codex-auto-resume-helper.service").read_text(
            encoding="utf-8"
        )
        generated = linux.render_unit("/usr/local/bin/codex")
        self.assertEqual(unit_keys(reference), unit_keys(generated))
        self.assertIn("WantedBy=default.target", reference)
        self.assertIn("Restart=on-failure", reference)
        self.assertIn("KillMode=process", reference)

    def test_reference_execstart_carries_the_state_dir(self) -> None:
        reference = (REPO_ROOT / "systemd" / "codex-auto-resume-helper.service").read_text(
            encoding="utf-8"
        )
        exec_start = next(
            line for line in reference.splitlines() if line.startswith("ExecStart=")
        )
        self.assertIn("helper", exec_start)
        self.assertIn("--state-dir", exec_start)

    def test_generated_unit_passes_state_dir_and_optional_codex_home(self) -> None:
        unit = linux.render_unit("/usr/local/bin/codex", codex_home="/tmp/codex home")
        self.assertIn("--state-dir", unit)
        self.assertIn("--codex-home /tmp/codex home", unit)
        self.assertIn("--codex-bin /usr/local/bin/codex", unit)


class LaunchdReferenceTests(StateCase):
    def test_reference_plist_matches_the_generated_keys(self) -> None:
        reference = ElementTree.parse(
            str(REPO_ROOT / "launchd" / macos.PLIST_NAME)
        ).getroot()
        generated = ElementTree.fromstring(macos.render_plist("/opt/homebrew/bin/codex"))

        def keys(root):
            dictionary = root.find("dict")
            children = list(dictionary)
            return [children[index].text for index in range(0, len(children), 2)]

        def arguments(root):
            array = root.find("dict").find("array")
            return [element.text for element in array]

        self.assertEqual(keys(reference), keys(generated))
        self.assertEqual(arguments(reference)[2:4], ["helper", "--state-dir"])
        self.assertEqual(arguments(generated)[2:4], ["helper", "--state-dir"])

    def test_reference_label_matches_the_backend(self) -> None:
        reference = (REPO_ROOT / "launchd" / macos.PLIST_NAME).read_text(encoding="utf-8")
        self.assertIn(f"<string>{macos.LABEL}</string>", reference)


class WindowsReferenceTests(StateCase):
    def test_reference_task_matches_the_generated_structure(self) -> None:
        reference = ElementTree.parse(
            str(REPO_ROOT / "windows" / "codex-auto-resume-helper.xml")
        ).getroot()
        generated = ElementTree.fromstring(windows.render_task_xml(r"C:\codex\codex.exe"))

        namespace = f"{{{windows.TASK_NAMESPACE}}}"

        def shape(root):
            tags = []
            for element in root.iter():
                if not isinstance(element.tag, str):
                    continue
                tag = element.tag.replace(namespace, "")
                tags.append(tag)
            return sorted(tags)

        self.assertEqual(shape(reference), shape(generated))

    def test_reference_action_runs_the_helper_with_the_state_dir(self) -> None:
        reference = ElementTree.parse(
            str(REPO_ROOT / "windows" / "codex-auto-resume-helper.xml")
        ).getroot()
        namespace = f"{{{windows.TASK_NAMESPACE}}}"
        arguments = reference.find(f".//{namespace}Arguments").text
        self.assertIn("helper", arguments)
        self.assertIn("--state-dir", arguments)


if __name__ == "__main__":
    unittest.main()
