# pyright: basic, reportImplicitRelativeImport=false

import base64
import json
import tempfile
import time
import unittest
from unittest.mock import patch

import ai_quota_widget as monitor


def _b64(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _token(account_id: str, exp: float) -> str:
    claims = {"exp": exp, "https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    return f"{_b64({'alg': 'none'})}.{_b64(claims)}.sig"


class CodexMultiAccountTest(unittest.TestCase):
    def test_official_agy_usage_parser_keeps_weekly_and_five_hour_windows(self):
        data = monitor._parse_agy_usage_output(
            "\n".join(
                [
                    "Gemini Models\tWeekly Limit Remaining\t83%\t2026-09-24T16:30:27Z",
                    "Gemini Models\tFive Hour Limit Remaining\t0%\t2026-09-17T21:30:27Z",
                    "Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-09-24T20:57:12Z",
                    "Claude and GPT models\tFive Hour Limit Remaining\t100%\t2026-09-18T01:57:12Z",
                ]
            )
        )

        gemini = next(group for group in data["groups"] if group["label"] == "Gemini")
        self.assertEqual(83.0, gemini["weekly_remaining_percent"])
        self.assertEqual(0.0, gemini["remaining_5h_percent"])

    def test_antigravity_cache_failure_is_not_reported_as_fresh_ok(self):
        cached = {
            "status": "ok",
            "data": {
                "timestamp": "2026-09-03T04:49:01.526Z",
                "models": [],
                "groups": [{"label": "Gemini", "remaining_5h_percent": 100}],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = f"{tmp}\\agy.json"
            with open(snapshot, "w", encoding="utf-8") as handle:
                json.dump(cached, handle)
            with (
                patch.object(monitor, "AGY_SNAPSHOT_FILE", snapshot),
                patch.object(
                    monitor.subprocess,
                    "run",
                    return_value=type("Result", (), {"returncode": 1, "stderr": "offline", "stdout": ""})(),
                ) as run,
            ):
                result = monitor.get_antigravity_quota()

        self.assertEqual("stale", result["status"])
        self.assertTrue(result["data"]["stale"])
        self.assertIn("--refresh", run.call_args.args[0])
        self.assertEqual("utf-8", run.call_args.kwargs["encoding"])

    def test_expired_token_is_replaced_by_live_token_for_same_account(self):
        expired = _token("acct-1", time.time() - 60)
        live = _token("acct-1", time.time() + 3600)
        credentials = monitor._dedupe_credentials(
            [
                ("Hermes", expired),
                ("OpenCode", live),
                ("Codex", _token("acct-2", time.time() + 3600)),
            ]
        )

        self.assertEqual(
            [("OpenCode", "acct-1"), ("Codex", "acct-2")],
            [(label, account_id) for label, _token_, account_id in credentials],
        )
        self.assertEqual(live, credentials[0][1])

    def test_live_first_source_keeps_label_when_duplicate_arrives_later(self):
        live = time.time() + 3600
        credentials = monitor._dedupe_credentials(
            [
                ("Codex", _token("acct-1", live)),
                ("OpenCode", _token("acct-1", live)),
            ]
        )

        self.assertEqual([("Codex", "acct-1")], [(label, account_id) for label, _t, account_id in credentials])

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

    def test_weekly_as_primary_is_labelled_7d_and_null_secondary_hidden(self):
        """La API hoy devuelve la ventana semanal en primary y secondary en null."""
        account = {
            "label": "Codex",
            "primary": {"remaining_percent": 0, "window_hours": 168.0, "resets_in": "12h 28m"},
            "secondary": {"remaining_percent": None, "window_hours": 168.0},
        }

        windows = monitor.codex_windows_with_data(account)

        self.assertEqual([("7d", account["primary"])], windows)

        data = {"codex": {"status": "ok", "data": {"accounts": [account]}}}
        snapshot = monitor.build_quota_snapshot(data)
        self.assertEqual({"7d": 0}, snapshot["quotas"]["codex:Codex"]["windows"])

    def test_five_hour_window_keeps_5h_label_when_exposed(self):
        account = {
            "label": "Codex",
            "primary": {"remaining_percent": 12, "window_hours": 5.0, "resets_in": "1h 2m"},
            "secondary": {"remaining_percent": 61, "window_hours": 168.0, "resets_in": "3d 4h"},
        }

        windows = monitor.codex_windows_with_data(account)

        self.assertEqual(["5h", "7d"], [name for name, _w in windows])

    def test_summary_survives_account_without_window_data(self):
        credentials = [("Codex", "token-a", "account-a")]

        def account(_label, _token, _account_id):
            return {"label": "Codex", "primary": {"remaining_percent": None}, "secondary": {}}

        with (
            patch.object(monitor, "_codex_credentials", return_value=credentials),
            patch.object(monitor, "_fetch_codex_account", side_effect=account),
        ):
            result = monitor.get_codex_quota()

        self.assertEqual("ok", result["status"])

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
        gemini = next(group for group in groups if group["label"] == "Gemini")
        self.assertEqual(81.0, gemini["remaining_5h_percent"])
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
