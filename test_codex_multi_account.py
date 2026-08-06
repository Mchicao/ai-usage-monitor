# pyright: basic, reportImplicitRelativeImport=false

import unittest
from unittest.mock import patch

import ai_quota_widget as monitor


class CodexMultiAccountTest(unittest.TestCase):
    def test_accounts_are_reported_separately_and_summary_uses_lowest_quota(self):
        credentials = [
            ("Codex", "token-a", "account-a"),
            ("Hermes", "token-b", "account-b"),
        ]

        def account(label, _token, _account_id):
            remaining = 20 if label == "Codex" else 70
            return {
                "label": label,
                "primary": {"remaining_percent": remaining},
                "secondary": {"remaining_percent": remaining},
            }

        with (
            patch.object(monitor, "_codex_credentials", return_value=credentials),
            patch.object(monitor, "_fetch_codex_account", side_effect=account),
        ):
            result = monitor.get_codex_quota()

        self.assertEqual(["Codex", "Hermes"], [a["label"] for a in result["data"]["accounts"]])
        self.assertEqual("Codex", result["data"]["label"])

    def test_zai_does_not_mislabel_mcp_allowance_as_weekly_model_quota(self):
        limits = monitor._normalise_zai_limits(
            [
                {
                    "type": "TIME_LIMIT",
                    "percentage": 74,
                    "remaining": 26,
                    "usageDetails": [{"modelCode": "search-prime", "usage": 74}],
                },
                {"type": "TOKENS_LIMIT", "percentage": 60},
            ]
        )

        self.assertEqual("mcp_tools", limits[0]["kind"])
        self.assertEqual("coding_5h", limits[1]["kind"])
        self.assertNotIn("7d", " ".join(limit["label"] for limit in limits))

    def test_antigravity_aliases_collapse_into_five_hour_families(self):
        groups = monitor._normalise_antigravity_groups(
            [
                {"label": "Gemini 3.5 Flash High", "remainingPercentage": 0.81, "resetTime": "t1"},
                {"label": "Gemini 3.5 Flash Low", "remainingPercentage": 0.81, "resetTime": "t1"},
                {"label": "Claude Opus", "remainingPercentage": 1.0, "resetTime": "t2"},
            ]
        )

        self.assertEqual(2, len(groups))
        self.assertTrue(all(group["weekly_remaining_percent"] is None for group in groups))

    def test_snapshot_keeps_accounts_separate_and_only_known_windows(self):
        data = {
            "timestamp": "2026-07-11T00:00:00+00:00",
            "codex": {
                "status": "ok",
                "data": {
                    "accounts": [
                        {
                            "label": "Codex",
                            "primary": {"remaining_percent": 12},
                            "secondary": {"remaining_percent": 61},
                        },
                        {
                            "label": "Hermes",
                            "primary": {"remaining_percent": 42},
                            "secondary": {"remaining_percent": 72},
                        },
                    ]
                },
            },
            "zai": {
                "status": "ok",
                "data": {
                    "limits": [
                        {"kind": "mcp_tools", "remaining_percent": 25},
                        {"kind": "coding_5h", "remaining_percent": 80},
                    ]
                },
            },
            "antigravity": {
                "status": "ok",
                "data": {
                    "groups": [
                        {"label": "Gemini", "remaining_5h_percent": 81},
                        {"label": "Claude y GPT", "remaining_5h_percent": 100},
                    ]
                },
            },
        }

        snapshot = monitor.build_quota_snapshot(data)

        self.assertEqual({"5h": 12, "7d": 61}, snapshot["quotas"]["codex:Codex"]["windows"])
        self.assertEqual({"5h": 42, "7d": 72}, snapshot["quotas"]["codex:Hermes"]["windows"])
        self.assertEqual({"5h": 80}, snapshot["quotas"]["zai:coding"]["windows"])
        self.assertNotIn("7d", snapshot["quotas"]["agy:gemini"]["windows"])

    def test_snapshot_omits_unknown_windows_and_uses_conservative_agy_minimum(self):
        data = {
            "codex": {
                "status": "ok",
                "data": {"accounts": [{"label": "Codex", "primary": {}, "secondary": {}}]},
            },
            "zai": {
                "status": "ok",
                "data": {"limits": [{"kind": "coding_5h", "remaining_percent": None}]},
            },
            "antigravity": {
                "status": "ok",
                "data": {
                    "groups": [
                        {"label": "Gemini", "remaining_5h_percent": 80},
                        {"label": "Gemini", "remaining_5h_percent": 12},
                    ]
                },
            },
        }

        snapshot = monitor.build_quota_snapshot(data)

        self.assertNotIn("codex:Codex", snapshot["quotas"])
        self.assertNotIn("zai:coding", snapshot["quotas"])
        self.assertEqual({"5h": 12}, snapshot["quotas"]["agy:gemini"]["windows"])

    def test_identity_snapshot_keeps_plan_emails_separate(self):
        data = {
            "codex": {
                "status": "ok",
                "data": {
                    "accounts": [
                        {"label": "Codex", "email": "one@example.com"},
                        {"label": "Hermes", "email": "two@example.com"},
                    ]
                },
            },
            "antigravity": {"status": "ok", "data": {"email": "agy@example.com"}},
            "zai": {"status": "ok", "data": {"email": "zai@example.com"}},
        }

        identities = monitor.build_quota_identities(data)["accounts"]

        self.assertEqual("one@example.com", identities["codex:Codex"])
        self.assertEqual("two@example.com", identities["codex:Hermes"])
        self.assertEqual("agy@example.com", identities["agy:gemini"])
        self.assertEqual("zai@example.com", identities["zai:coding"])


if __name__ == "__main__":
    unittest.main()
