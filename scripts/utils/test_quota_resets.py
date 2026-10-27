"""Comprobaciones focalizadas: errores Z.AI y vencimientos de resets."""

import sys
import unittest
import importlib
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import ai_quota_widget as monitor


class QuotaResetsTest(unittest.TestCase):
    def test_zcode_native_decryption_authenticates_ciphertext(self):
        session = importlib.import_module("scripts.utils.zcode_session")
        encrypted = "enc:v1:AAECAwQFBgcICQoL.pDgAVwUYxIHJXs-yqAhSqA.dnlWI6JDR2z3DbvZRUPvvAjhiA"
        self.assertEqual("fake-platform-token", session.decrypt_credential(encrypted, "monitor-test-secret"))
        with self.assertRaises(ValueError):
            session.decrypt_credential(encrypted, "wrong-secret")

    def test_zcode_session_overrides_rejected_opencode_key_and_keeps_cards(self):
        session = {"platform_token": "platform", "zcode_token": "zcode", "email": None}
        quota = {"success": True, "data": {"level": "lite", "limits": [{"type": "TOKENS_LIMIT", "percentage": 0}]}}
        resets = {"code": 0, "data": {"available_five_hour_resets": [{"expire_at": 1900000000000}], "available_week_resets": []}}
        with (patch.object(monitor, "read_zcode_session", return_value=session, create=True),
              patch.object(monitor, "_zai_request", side_effect=[quota, resets], create=True) as request,
              patch.object(monitor, "_read_zai_api_key", side_effect=AssertionError("No debe consultar la clave antigua"))):
            result = monitor.get_zai_quota()
        self.assertEqual("ok", result["status"])
        self.assertEqual(100, result["data"]["limits"][0]["remaining_percent"])
        self.assertEqual("ok", result["data"]["reset_credits"]["status"])
        self.assertEqual(1, len(result["data"]["reset_credits"]["credits"]))
        self.assertEqual("platform", request.call_args_list[0].args[1]["Authorization"])

    def test_zcode_quota_and_cards_fail_independently(self):
        session = {"platform_token": "platform", "zcode_token": "zcode", "email": None}
        quota = {"success": True, "data": {"limits": [{"type": "TOKENS_LIMIT", "percentage": 0}]}}
        cards = {"code": 0, "data": {"available_five_hour_resets": [], "available_week_resets": []}}
        for responses, quota_status, card_status in (([quota, OSError()], "ok", "error"),
                                                     ([OSError(), cards], "error", "ok")):
            with (self.subTest(quota_status=quota_status),
                  patch.object(monitor, "read_zcode_session", return_value=session),
                  patch.object(monitor, "_zai_request", side_effect=responses)):
                result = monitor.get_zai_quota()
                self.assertEqual(quota_status, result["status"])
                self.assertEqual(card_status, result["data"]["reset_credits"]["status"])

    def test_credit_limit_weekly_is_not_labelled_as_five_hours(self):
        limits = monitor._normalise_zai_limits([
            {"type": "CREDIT_LIMIT", "unit": 6, "number": 1, "percentage": 40},
            {"type": "CREDIT_LIMIT", "unit": 3, "number": 5, "percentage": 10}])
        self.assertEqual(["coding_weekly", "coding_5h"], [limit["kind"] for limit in limits])

    def test_zai_rejects_business_error_and_empty_limits(self):
        for body in ({"success": False, "code": 1000}, {"data": {"limits": []}}):
            with self.subTest(body=body):
                self.assertEqual("error", monitor._normalise_zai_response(body)["status"])

    def test_zai_preserves_reset_timestamp_and_unknown_percentage(self):
        result = monitor._normalise_zai_response({"success": True, "data": {
            "level": "pro", "limits": [{"type": "TOKENS_LIMIT", "percentage": 25,
            "nextResetTime": 1900000000000}, {"type": "TIME_LIMIT"}]}})
        self.assertEqual("ok", result["status"])
        coding, unknown = result["data"]["limits"]
        self.assertEqual(75, coding["remaining_percent"])
        self.assertEqual(datetime.fromtimestamp(1900000000, timezone.utc).isoformat(), coding["reset_at"])
        self.assertIsNone(unknown["remaining_percent"])

    def test_resets_keep_authoritative_count_and_local_expiry_time(self):
        expiry = "2099-10-29T01:23:00Z"
        result = monitor._normalise_codex_resets({"available_count": 7, "credits": [
            {"id": "available", "status": "available", "expires_at": expiry},
            {"id": "expired", "status": "available", "expires_at": "2000-01-01T00:00:00Z"},
            {"id": "used", "status": "redeemed", "expires_at": expiry}]})
        self.assertEqual(7, result["available_count"])
        self.assertEqual(["available"], [c["id"] for c in result["credits"]])
        self.assertEqual(monitor._format_local_datetime(expiry), result["credits"][0]["date"])
        self.assertIn(":", result["credits"][0]["date"])


if __name__ == "__main__":
    unittest.main()
