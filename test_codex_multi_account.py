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
    def test_opencode_v2_credentials_replace_expired_legacy_credentials(self):
        expired = _token("account-opencode", time.time() - 60)
        live = _token("account-opencode", time.time() + 3600)
        with tempfile.TemporaryDirectory() as tmp:
            codex_file, hermes_file, opencode_file = (f"{tmp}/{name}.json" for name in ("codex", "hermes", "opencode"))
            for filename, content in ((codex_file, {"tokens": {"access_token": _token("account-codex", time.time() + 3600)}}),
                                      (hermes_file, {"providers": {"openai-codex": {"tokens": {"access_token": expired}}}}),
                                      (opencode_file, {"openai": {"access": expired}})):
                with open(filename, "w", encoding="utf-8") as handle:
                    json.dump(content, handle)
            exported = [{"integrationID": "openai", "value": {"type": "oauth", "access": live}}]
            with (patch.object(monitor, "CODEX_AUTH_FILE", codex_file),
                  patch.object(monitor, "HERMES_AUTH_FILE", hermes_file),
                  patch.object(monitor, "OPENCODE_AUTH_FILE", opencode_file),
                  patch.object(monitor, "_run_quota_command", return_value=monitor.subprocess.CompletedProcess([], 0, json.dumps(exported), ""))):
                credentials = monitor._codex_credentials()
        self.assertEqual([("Codex", "account-codex"), ("OpenCode", "account-opencode")], [(label, account) for label, _, account in credentials])
        self.assertEqual(live, credentials[1][1])

    def test_native_argon_cache_survives_native_failure_and_cloud_fallback(self):
        cached = {"status": "ok", "data": {"method": "agy", "groups": [{"label": "Gemini 4 Argon", "weekly_remaining_percent": 90}]}}
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/agy.json"
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(cached, handle)
            def command(args, **_kwargs):
                if args[0] == "agy":
                    return monitor.subprocess.CompletedProcess(args, 1, "", "offline")
                return monitor.subprocess.CompletedProcess(args, 0, json.dumps({"models": [{"label": "Gemini", "remainingPercentage": 1}]}), "")
            with patch.object(monitor, "AGY_SNAPSHOT_FILE", path), patch.object(monitor, "_run_quota_command", side_effect=command):
                result = monitor.get_antigravity_quota()
        self.assertEqual("stale", result["status"])
        self.assertEqual("Gemini 4 Argon", result["data"]["groups"][0]["label"])
        self.assertEqual(90, result["data"]["groups"][0]["weekly_remaining_percent"])

    def test_failed_second_account_remains_visible_without_inventing_a_quota(self):
        import urllib.error
        import win_tray_widget as tray

        def fetch(label, *_args):
            if label == "OpenCode":
                raise urllib.error.HTTPError("https://example.invalid", 401, "expired", {}, None)
            return {"label": label, "primary": {"remaining_percent": 50, "window_hours": 5}}

        with patch.object(monitor, "_codex_credentials", return_value=[("Codex", "a", "one"), ("OpenCode", "b", "two")]), patch.object(monitor, "_fetch_codex_account", side_effect=fetch):
            result = monitor.get_codex_quota()
        self.assertEqual(["Codex", "OpenCode"], [a["label"] for a in result["data"]["accounts"]])
        popup = tray.FluentPopup.__new__(tray.FluentPopup)
        popup.data = {"codex": result}
        cards = dict(popup._get_cx_accounts())
        self.assertIn("HTTP 401", cards["Codex · OpenCode"]["subtitle"])
        self.assertEqual([], cards["Codex · OpenCode"]["windows"])
        self.assertEqual("", cards["Codex · OpenCode"]["resets_text"])

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

    def test_argon_weekly_quota_does_not_overwrite_gemini_or_invent_five_hours(self):
        data = monitor._parse_agy_usage_output(
            "Gemini Models\tWeekly Limit Remaining\t99%\t2026-10-23T03:50:41Z\n"
            "Gemini Models\tFive Hour Limit Remaining\t100%\t2026-10-16T23:15:48Z\n"
            "Claude and GPT models\tWeekly Limit Remaining\t50%\t2026-10-20T14:56:49Z\n"
            "Gemini 4 Argon\tGemini 4 Argon Weekly Limit\t93%\t2026-10-22T23:06:18Z\n"
        )
        groups = {group["label"]: group for group in data["groups"]}
        self.assertEqual(3, len(groups))
        self.assertEqual(99, groups["Gemini"]["weekly_remaining_percent"])
        self.assertEqual(50, groups["Claude y GPT"]["weekly_remaining_percent"])
        self.assertEqual(93, groups["Gemini 4 Argon"]["weekly_remaining_percent"])
        self.assertIsNone(groups["Gemini 4 Argon"]["remaining_5h_percent"])
        snapshot = monitor.build_quota_snapshot({"antigravity": {"status": "ok", "data": data}})
        self.assertEqual({"7d": 93}, snapshot["quotas"]["agy:gemini_argon"]["windows"])
        self.assertEqual(99, snapshot["quotas"]["agy:gemini"]["windows"]["7d"])

    def test_argon_and_gemini_fallback_groups_stay_separate_with_identical_limits(self):
        groups = monitor._normalise_antigravity_groups([
            {"label": "Gemini 3.8 Flash", "remainingPercentage": 0.93, "resetTime": "2026-10-22T23:06:18Z"},
            {"label": "Gemini 4 Argon", "remainingPercentage": 0.93, "resetTime": "2026-10-22T23:06:18Z"},
        ])
        self.assertEqual({"Gemini", "Gemini 4 Argon"}, {group["label"] for group in groups})

    def test_tray_tooltip_includes_argon_when_only_its_weekly_quota_is_exhausted(self):
        import win_tray_widget as tray

        data = {"antigravity": {"status": "ok", "data": {"groups": [
            {"label": "Gemini", "remaining_5h_percent": 100},
            {"label": "Claude y GPT", "remaining_5h_percent": 100},
            {"label": "Gemini 4 Argon", "remaining_5h_percent": None, "weekly_remaining_percent": 0},
        ]}}}
        self.assertIn("AG:0%", tray.get_compact_tooltip(data))

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


    def test_normalise_codex_resets_with_credits(self):
        payload = {
            "credits": [
                {
                    "id": "c1",
                    "status": "available",
                    "expires_at": "2099-10-04T05:00:00Z",
                },
                {
                    "id": "c2",
                    "status": "available",
                    "expires_at": "2099-10-04T22:00:00Z",
                },
                {
                    "id": "c3",
                    "status": "available",
                    "expires_at": "2099-10-22T19:00:00Z",
                },
            ],
            "available_count": 3,
        }
        res = monitor._normalise_codex_resets(payload)
        self.assertEqual(3, res["available_count"])
        self.assertEqual(3, len(res["credits"]))
        self.assertIn("2 el 04/10", res["summary"])
        self.assertIn("1 el 22/10", res["summary"])

    def test_normalise_codex_resets_fallback_count(self):
        fallback = {"available_count": 2}
        res = monitor._normalise_codex_resets(None, fallback_data=fallback)
        self.assertEqual(2, res["available_count"])
        self.assertEqual("2 disponibles", res["summary"])

    def test_normalise_codex_resets_zero_credits(self):
        res = monitor._normalise_codex_resets({"credits": [], "available_count": 0})
        self.assertEqual(0, res["available_count"])
        self.assertEqual("Sin resets disponibles", res["summary"])

    def test_ts_to_countdown_supports_iso_strings(self):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        target_5h = now + timedelta(hours=2, minutes=15, seconds=30)
        iso_str = target_5h.isoformat().replace("+00:00", "Z")

        countdown = monitor._ts_to_countdown(iso_str)
        self.assertEqual("2h 15m", countdown)

        target_past = now - timedelta(minutes=5)
        self.assertEqual("Ya reseteado", monitor._ts_to_countdown(target_past.isoformat()))
        self.assertEqual("N/A", monitor._ts_to_countdown(None))

    def test_agy_usage_parser_populates_countdown_fields(self):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        reset_5h = (now + timedelta(hours=1, minutes=30, seconds=30)).isoformat().replace("+00:00", "Z")
        reset_weekly = (now + timedelta(days=2, hours=4, seconds=30)).isoformat().replace("+00:00", "Z")

        data = monitor._parse_agy_usage_output(
            "\n".join(
                [
                    f"Gemini Models\tFive Hour Limit Remaining\t45%\t{reset_5h}",
                    f"Gemini Models\tWeekly Limit Remaining\t80%\t{reset_weekly}",
                ]
            )
        )
        gemini = next(g for g in data["groups"] if g["label"] == "Gemini")
        self.assertEqual("1h 30m", gemini["reset_5h_in"])
        self.assertEqual("2d 4h", gemini["reset_weekly_in"])

    def test_win_tray_antigravity_groups_include_reset_countdown(self):
        import win_tray_widget as tray

        data = {
            "antigravity": {
                "status": "ok",
                "data": {
                    "groups": [
                        {
                            "label": "Gemini",
                            "remaining_5h_percent": 45.0,
                            "reset_5h_in": "1h 15m",
                            "weekly_remaining_percent": 80.0,
                            "reset_weekly_in": "3d 2h",
                        }
                    ]
                },
            }
        }
        tooltip = tray.get_compact_tooltip(data)
        self.assertIn("🛸AG:45%(1h 15m)", tooltip)

        # Mock FluentPopup instance to call _get_ag_groups without creating CTk window
        popup = tray.FluentPopup.__new__(tray.FluentPopup)
        popup.data = data
        groups = popup._get_ag_groups()
        self.assertEqual(1, len(groups))
        label, info = groups[0]
        self.assertEqual("Gemini", label)
        self.assertIn("5h 45% (Reset: 1h 15m)", info["subtitle"])
        self.assertIn("7d 80% (Reset: 3d 2h)", info["subtitle"])


if __name__ == "__main__":

    unittest.main()
