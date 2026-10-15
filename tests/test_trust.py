"""Hook trust: reads the hash from Codex, writes it through Codex."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from support import StateCase
from test_ratelimit import make_fake_codex
from codex_auto_resume import trust


HOOKS = [
    {
        "cwd": "/tmp",
        "hooks": [
            {
                "key": "codex-auto-resume@local:hooks/hooks.json:session_start:0:0",
                "eventName": "SessionStart",
                "trustStatus": "untrusted",
                "currentHash": "sha256:aaaa",
            },
            {
                "key": "codex-auto-resume@local:hooks/hooks.json:session_end:0:0",
                "eventName": "SessionEnd",
                "trustStatus": "trusted",
                "currentHash": "sha256:bbbb",
            },
            {
                "key": "other@local:hooks/hooks.json:stop:0:0",
                "eventName": "Stop",
                "trustStatus": "untrusted",
                "currentHash": "sha256:cccc",
            },
        ]
    }
]


class TrustTests(StateCase):
    def setUp(self) -> None:
        super().setUp()
        self.fake_bin = make_fake_codex(self.tmp)
        self.scenario_path = self.tmp / "scenario.json"
        self.write_log = self.tmp / "config-write.jsonl"
        self.scenario_path.write_text(
            json.dumps({"hooks": HOOKS, "config_write_log": str(self.write_log)}),
            encoding="utf-8",
        )
        os.environ["FAKE_CODEX_SCENARIO"] = str(self.scenario_path)
        self.addCleanup(lambda: os.environ.pop("FAKE_CODEX_SCENARIO", None))

    def writes(self) -> list[dict]:
        if not self.write_log.exists():
            return []
        return [json.loads(line) for line in self.write_log.read_text().splitlines() if line.strip()]

    def test_status_lists_only_our_hooks(self) -> None:
        statuses = trust.plugin_hook_status(self.fake_bin, str(self.codex_home))
        keys = {item["key"] for item in statuses}
        self.assertEqual(len(keys), 2)
        self.assertTrue(all("codex-auto-resume@" in key for key in keys))

    def test_untrusted_hooks_are_written_with_the_reported_hash(self) -> None:
        result = trust.trust_plugin_hooks(self.fake_bin, str(self.codex_home))
        self.assertEqual(result["errors"], [])
        written = self.writes()
        self.assertEqual(len(written), 1, "only the untrusted hook needs a write")
        self.assertEqual(
            written[0]["keyPath"],
            'hooks.state."codex-auto-resume@local:hooks/hooks.json:session_start:0:0".trusted_hash',
        )
        self.assertEqual(written[0]["value"], "sha256:aaaa")

    def test_trusted_hooks_are_left_alone(self) -> None:
        trust.trust_plugin_hooks(self.fake_bin, str(self.codex_home))
        paths = " ".join(item["keyPath"] for item in self.writes())
        self.assertNotIn("session_end", paths)

    def test_dry_run_writes_nothing(self) -> None:
        result = trust.trust_plugin_hooks(self.fake_bin, str(self.codex_home), dry_run=True)
        self.assertEqual(self.writes(), [])
        self.assertTrue(any(item["action"] == "would-write" for item in result["hooks"]))

    def test_key_escaping_matches_codex(self) -> None:
        self.assertEqual(
            trust._escape_key('plugin."quoted"\\path'),
            'plugin.\\"quoted\\"\\\\path',
        )

    def test_missing_plugin_is_reported_not_crashed(self) -> None:
        self.scenario_path.write_text(json.dumps({"hooks": []}), encoding="utf-8")
        result = trust.trust_plugin_hooks(self.fake_bin, str(self.codex_home))
        self.assertTrue(result["errors"])
        self.assertEqual(result["hooks"], [])


if __name__ == "__main__":
    unittest.main()
