#!/usr/bin/env python3
"""Pruebas de descubrimiento seguro de las fuentes de cuota."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
import ai_quota_widget as quota  # noqa: E402


class QuotaSourceTests(unittest.TestCase):
    def test_antigravity_command_honours_explicit_path(self):
        original = quota.ANTIGRAVITY_USAGE_COMMAND
        try:
            quota.ANTIGRAVITY_USAGE_COMMAND = sys.executable
            self.assertEqual(quota._antigravity_usage_command(), sys.executable)
        finally:
            quota.ANTIGRAVITY_USAGE_COMMAND = original

    def test_antigravity_cloudcode_rows_group_gemini_models(self):
        groups = quota._normalise_antigravity_groups(
            [
                {
                    "modelId": "gemini-3.5-flash-low",
                    "label": "gemini-3.5-flash-low",
                    "remainingPercentage": 0.9899,
                    "resetTime": "2026-08-07T01:09:42Z",
                }
            ]
        )
        self.assertEqual(groups[0]["label"], "Gemini")
        self.assertEqual(groups[0]["remaining_5h_percent"], 99.0)

    def test_antigravity_cloudcode_rows_merge_same_family(self):
        groups = quota._normalise_antigravity_groups(
            [
                {
                    "modelId": "claude-sonnet-4-6",
                    "label": "claude-sonnet-4-6",
                    "remainingPercentage": 1.0,
                    "resetTime": "2026-08-07T01:20:00Z",
                },
                {
                    "modelId": "gpt-oss-120b-medium",
                    "label": "gpt-oss-120b-medium",
                    "remainingPercentage": 0.75,
                    "resetTime": "2026-08-07T01:10:00Z",
                },
            ]
        )
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["label"], "Claude y GPT")
        self.assertEqual(groups[0]["model_count"], 2)
        self.assertEqual(groups[0]["remaining_5h_percent"], 75.0)
        self.assertEqual(groups[0]["reset_5h_at"], "2026-08-07T01:10:00Z")

    def test_antigravity_summary_groups_extracts_5h_and_weekly(self):
        summary_groups = [
            {
                "displayName": "Gemini Models",
                "buckets": [
                    {
                        "bucketId": "gemini-weekly",
                        "displayName": "Weekly Limit Remaining",
                        "window": "weekly",
                        "remainingFraction": 0.76,
                        "resetTime": "2026-09-10T19:00:00Z",
                        "description": "Weekly refresh in 2 days",
                    },
                    {
                        "bucketId": "gemini-5h",
                        "displayName": "Five Hour Limit Remaining",
                        "window": "5h",
                        "remainingFraction": 0.68,
                        "resetTime": "2026-09-08T00:00:00Z",
                        "description": "5h refresh in 3 hours",
                    },
                ],
            }
        ]
        groups = quota._normalise_antigravity_summary_groups(summary_groups)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["label"], "Gemini")
        self.assertEqual(groups[0]["remaining_5h_percent"], 68.0)
        self.assertEqual(groups[0]["weekly_remaining_percent"], 76.0)
        self.assertEqual(groups[0]["description_5h"], "5h refresh in 3 hours")
        self.assertEqual(groups[0]["description_weekly"], "Weekly refresh in 2 days")

    def test_zai_token_can_come_from_environment_without_writing_it(self):
        original = quota.ZAI_PLATFORM_TOKEN
        try:
            quota.ZAI_PLATFORM_TOKEN = "test-platform-token"
            self.assertEqual(quota._read_zai_platform_token(), "test-platform-token")
        finally:
            quota.ZAI_PLATFORM_TOKEN = original

    def test_browser_cookie_dump_is_not_treated_as_platform_token(self):
        original_token = quota.ZAI_PLATFORM_TOKEN
        original_token_file = quota.ZAI_PLATFORM_TOKEN_FILE
        original_session_file = quota.ZAI_SESSION_FILE
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                session_path = os.path.join(temp_dir, "session.json")
                with open(session_path, "w", encoding="utf-8") as handle:
                    json.dump({"cookies": {"token": "browser-cookie"}}, handle)
                quota.ZAI_PLATFORM_TOKEN = ""
                quota.ZAI_PLATFORM_TOKEN_FILE = os.path.join(temp_dir, "missing.txt")
                quota.ZAI_SESSION_FILE = session_path
                self.assertEqual(quota._read_zai_platform_token(), "")
        finally:
            quota.ZAI_PLATFORM_TOKEN = original_token
            quota.ZAI_PLATFORM_TOKEN_FILE = original_token_file
            quota.ZAI_SESSION_FILE = original_session_file

    def test_zai_api_key_can_come_from_opencode_auth_shape(self):
        original_key = quota.ZAI_API_KEY
        original_file = quota.ZAI_API_KEY_FILE
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                auth_path = os.path.join(temp_dir, "auth.json")
                with open(auth_path, "w", encoding="utf-8") as handle:
                    json.dump({"zai-coding-plan": {"type": "api", "key": "zai-key"}}, handle)
                quota.ZAI_API_KEY = ""
                quota.ZAI_API_KEY_FILE = auth_path
                self.assertEqual(quota._read_zai_api_key(), "zai-key")
        finally:
            quota.ZAI_API_KEY = original_key
            quota.ZAI_API_KEY_FILE = original_file


if __name__ == "__main__":
    unittest.main()
