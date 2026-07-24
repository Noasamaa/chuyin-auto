#!/usr/bin/env python3
"""单元测试：覆盖两轮审查中的严重/高/中风险回归。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main as M  # noqa: E402


def _base_cfg(**over):
    cfg = {
        "domain": "www.yngal.com",
        "timeout": 5,
        "hunt_max": 3,
        "hunt_interval": 0,
        "job_max_seconds": 120,
        "accounts": [{"email": "a@b.com", "password": "x"}],
    }
    cfg.update(over)
    return cfg


class TestExtractToken(unittest.TestCase):
    def test_obj_token(self):
        self.assertEqual(
            M._extract_token({"code": 0, "obj": {"token": "abc"}}), "abc"
        )

    def test_user_dict(self):
        self.assertEqual(M._extract_token({"user": {"token": "t1"}}), "t1")

    def test_user_json_string(self):
        payload = {"user": json.dumps({"token": "t-str"})}
        self.assertEqual(M._extract_token(payload), "t-str")

    def test_missing(self):
        self.assertIsNone(M._extract_token({"code": 1, "msg": "失败"}))


class TestCodeOf(unittest.TestCase):
    def test_int(self):
        self.assertEqual(M._code_of({"code": 0}), 0)
        self.assertEqual(M._code_of({"code": 688}), 688)

    def test_bool_rejected(self):
        self.assertIsNone(M._code_of({"code": False}))
        self.assertIsNone(M._code_of({"code": True}))

    def test_float_non_int_rejected(self):
        self.assertIsNone(M._code_of({"code": 0.9}))

    def test_str_int(self):
        self.assertEqual(M._code_of({"code": "10"}), 10)


class TestParseConfig(unittest.TestCase):
    def test_ok(self):
        cfg = M.parse_config(_base_cfg(hunt_max=5))
        self.assertEqual(cfg["hunt_max"], 5)

    def test_accounts_string_fails(self):
        with self.assertRaises(M.ConfigError):
            M.parse_config(_base_cfg(accounts="not-a-list"))

    def test_accounts_int_fails(self):
        with self.assertRaises(M.ConfigError):
            M.parse_config(_base_cfg(accounts=123))

    def test_accounts_empty_fails(self):
        with self.assertRaises(M.ConfigError):
            M.parse_config(_base_cfg(accounts=[]))

    def test_missing_password_message_no_secret(self):
        with self.assertRaises(M.ConfigError) as cm:
            M.parse_config(
                _base_cfg(accounts=[{"email": "a@b.com", "password": ""}])
            )
        self.assertNotIn("SECRET", str(cm.exception))

    def test_nan_timeout(self):
        with self.assertRaises(M.ConfigError):
            M.parse_config(_base_cfg(timeout=float("nan")))

    def test_hunt_max_over_cap(self):
        with self.assertRaises(M.ConfigError):
            M.parse_config(_base_cfg(hunt_max=9999))

    def test_account_element_not_dict(self):
        with self.assertRaises(M.ConfigError):
            M.parse_config(_base_cfg(accounts=["x"]))

    def test_string_false_not_bool(self):
        with self.assertRaises(M.ConfigError):
            M.parse_config(_base_cfg(do_checkin="false"))

    def test_enabled_string_false(self):
        with self.assertRaises(M.ConfigError):
            M.parse_config(
                _base_cfg(
                    accounts=[
                        {
                            "email": "a@b.com",
                            "password": "p",
                            "enabled": "false",
                        }
                    ]
                )
            )

    def test_hunt_max_bool_rejected(self):
        with self.assertRaises(M.ConfigError):
            M.parse_config(_base_cfg(hunt_max=True))

    def test_empty_domain_rejected(self):
        with self.assertRaises(M.ConfigError):
            M.parse_config(_base_cfg(domain="   "))

    def test_domain_slash_only_rejected(self):
        with self.assertRaises(M.ConfigError):
            M.parse_config(_base_cfg(domain="https:///"))

    def test_job_limit_consistency(self):
        # hunt_max=50 * interval=30 远超 540s
        with self.assertRaises(M.ConfigError) as cm:
            M.parse_config(
                _base_cfg(hunt_max=50, hunt_interval=30, job_max_seconds=540)
            )
        self.assertIn("job", str(cm.exception).lower())

    def test_too_many_accounts(self):
        accs = [
            {"email": f"u{i}@b.com", "password": "p"}
            for i in range(M.MAX_ACCOUNTS + 1)
        ]
        with self.assertRaises(M.ConfigError):
            M.parse_config(_base_cfg(accounts=accs, hunt_interval=0, timeout=1))


class FakeResponse:
    def __init__(
        self,
        status=200,
        body=b"{}",
        content_type="application/json",
        path="/x",
        headers=None,
    ):
        self.status_code = status
        self._body = body
        self.headers = {"Content-Type": content_type}
        if headers:
            self.headers.update(headers)
        self.request = MagicMock()
        self.request.path_url = path

    def iter_content(self, chunk_size=8192):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    def close(self):
        pass


class TestClientRequest(unittest.TestCase):
    def setUp(self):
        self.session = MagicMock()
        self.client = M.ChuyinClient(
            "www.yngal.com",
            timeout=5,
            max_retries=3,
            session=self.session,
        )
        self.client.token = "SECRET_TOKEN"

    def test_http_503_raises(self):
        self.session.get.return_value = FakeResponse(
            status=503, body=b"nope", path="/addJf"
        )
        with self.assertRaises(M.RequestError) as cm:
            self.client.add_jf()
        self.assertIn("503", str(cm.exception))

    def test_body_size_cap(self):
        big = b"x" * (M.MAX_RESPONSE_BYTES + 10)
        self.session.get.return_value = FakeResponse(body=big, path="/getVip")
        with self.assertRaises(M.RequestError) as cm:
            self.client.get_vip()
        self.assertIn("上限", str(cm.exception))

    def test_redirect_rejected_no_follow(self):
        self.session.get.return_value = FakeResponse(
            status=302,
            body=b"x" * 100,
            path="/getVip",
            headers={"Location": "http://evil:9/steal"},
        )
        with self.assertRaises(M.RequestError) as cm:
            self.client.get_vip()
        self.assertIn("重定向", str(cm.exception))
        # 必须 allow_redirects=False
        kwargs = self.session.get.call_args.kwargs
        self.assertFalse(kwargs.get("allow_redirects", True))

    def test_hunt_no_retry_on_timeout(self):
        self.session.get.side_effect = M.requests.exceptions.ReadTimeout("t")
        with self.assertRaises(M.RequestError):
            self.client.hunt()
        self.assertEqual(self.session.get.call_count, 1)

    def test_getvip_retries_on_timeout(self):
        self.session.get.side_effect = [
            M.requests.exceptions.ReadTimeout("t"),
            FakeResponse(body=b'{"code":0,"obj":{}}', path="/getVip"),
        ]
        data = self.client.get_vip()
        self.assertEqual(data.get("code"), 0)
        self.assertEqual(self.session.get.call_count, 2)

    def test_auth_header_only_when_auth(self):
        self.session.get.return_value = FakeResponse(
            body=b'{"code":0}', path="/getVip"
        )
        self.client.get_vip()
        h = self.session.get.call_args.kwargs["headers"]
        self.assertEqual(h.get("X-Auth-Token"), "SECRET_TOKEN")


class TestRunAccountSemantics(unittest.TestCase):
    def _client(self):
        c = MagicMock(spec=M.ChuyinClient)
        c.warmup = MagicMock()
        c.login = MagicMock(return_value={"code": 0, "obj": {"token": "t"}})
        c.get_vip = MagicMock(
            return_value={
                "code": 0,
                "obj": {"nickname": "n", "lv": 1, "jf": 1, "jf2": 2},
            }
        )
        return c

    def test_checkin_500_not_ok(self):
        c = self._client()
        c.add_jf.return_value = {"code": 500, "msg": "err"}
        c.hunt.return_value = {"code": 688}
        r = M.run_account(
            c, "a@b.com", "pw",
            do_checkin=True, do_hunt=True,
            hunt_max=3, hunt_interval=0, dry_run=False,
        )
        self.assertFalse(r.ok)
        self.assertTrue(r.checkin_failed)

    def test_hunt_999_not_ok(self):
        c = self._client()
        c.add_jf.return_value = {"code": 10}
        c.hunt.return_value = {"code": 999}
        r = M.run_account(
            c, "a@b.com", "pw",
            do_checkin=True, do_hunt=True,
            hunt_max=3, hunt_interval=0, dry_run=False,
        )
        self.assertFalse(r.ok)
        self.assertTrue(r.hunt_failed)

    def test_hunt_999_with_items_not_ok(self):
        """高风险：未知 code 带 items 不得成功。"""
        c = self._client()
        c.add_jf.return_value = {"code": 10}
        c.hunt.return_value = {"code": 999, "obj": [{"x": 1}]}
        r = M.run_account(
            c, "a@b.com", "pw",
            do_checkin=True, do_hunt=True,
            hunt_max=3, hunt_interval=0, dry_run=False,
        )
        self.assertFalse(r.ok)
        self.assertTrue(r.hunt_failed)
        self.assertEqual(r.hunt_ok, 0)

    def test_hunt_688_is_ok(self):
        c = self._client()
        c.add_jf.return_value = {"code": 10}
        c.hunt.return_value = {"code": 688}
        r = M.run_account(
            c, "a@b.com", "pw",
            do_checkin=True, do_hunt=True,
            hunt_max=3, hunt_interval=0, dry_run=False,
        )
        self.assertTrue(r.ok)

    def test_checkin_error_still_runs_hunt(self):
        c = self._client()
        c.add_jf.side_effect = M.RequestError("addJf HTTP 503")
        c.hunt.return_value = {"code": 688}
        r = M.run_account(
            c, "a@b.com", "pw",
            do_checkin=True, do_hunt=True,
            hunt_max=3, hunt_interval=0, dry_run=False,
        )
        c.hunt.assert_called()
        self.assertTrue(r.checkin_failed)
        self.assertFalse(r.hunt_failed)
        self.assertFalse(r.ok)  # 签到失败整体仍 fail

    def test_getvip_fail_does_not_block_checkin(self):
        c = self._client()
        c.get_vip.side_effect = M.RequestError("getVip HTTP 503")
        c.add_jf.return_value = {"code": 0}
        r = M.run_account(
            c, "a@b.com", "pw",
            do_checkin=True, do_hunt=False,
            hunt_max=1, hunt_interval=0, dry_run=False,
        )
        self.assertTrue(r.ok)
        c.add_jf.assert_called()

    def test_hunt_items_capped(self):
        c = self._client()
        c.add_jf.return_value = {"code": 10}
        items = [{"id": i} for i in range(3)]
        c.hunt.side_effect = [
            {"code": 0, "obj": items},
            {"code": 0, "obj": items},
            {"code": 0, "obj": items},
            {"code": 688},
        ]
        r = M.run_account(
            c, "a@b.com", "pw",
            do_checkin=True, do_hunt=True,
            hunt_max=10, hunt_interval=0, dry_run=False,
        )
        self.assertTrue(r.ok)
        self.assertLessEqual(len(r.hunt_items), M.MAX_HUNT_ITEMS_KEEP)

    def test_huge_item_truncated(self):
        c = self._client()
        c.add_jf.return_value = {"code": 10}
        huge = {"blob": "Z" * (M.MAX_HUNT_ITEM_BYTES + 100)}
        c.hunt.side_effect = [
            {"code": 0, "obj": [huge]},
            {"code": 688},
        ]
        r = M.run_account(
            c, "a@b.com", "pw",
            do_checkin=True, do_hunt=True,
            hunt_max=5, hunt_interval=0, dry_run=False,
        )
        self.assertTrue(r.ok)
        self.assertTrue(r.hunt_items[0].get("_truncated"))


class TestLogNoPassword(unittest.TestCase):
    def test_main_config_error_no_password_in_log(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config.yaml"
            cfg.write_text(
                "accounts:\n  - email: ''\n    password: SUPER_SECRET_SENTINEL\n",
                encoding="utf-8",
            )
            with self.assertRaises(M.ConfigError) as cm:
                M.parse_config(M.load_config(cfg))
            self.assertNotIn("SUPER_SECRET_SENTINEL", str(cm.exception))


class TestInstallScriptSafety(unittest.TestCase):
    def test_script_guards(self):
        script = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
        self.assertIn("MARKER_NAME", script)
        self.assertIn("validate_app_dir", script)
        self.assertIn("is_symlink", script)
        self.assertIn("normalize_code_perms", script)
        self.assertIn("RUNTIME_SKIP_NAMES", script)
        self.assertIn(".venv", script)
        self.assertIn("write_marker", script)
        # 不得对整树盲 chmod 644 扫到 venv
        self.assertNotIn('find "$APP_DIR" -type f -exec chmod 644', script)
        unit = (ROOT / "deploy" / "chuyin-auto.service.in").read_text()
        self.assertIn("User=@RUN_USER@", unit)
        self.assertIn("RuntimeMaxSec=600", unit)
        self.assertIn("@APP_DIR@", unit)


if __name__ == "__main__":
    unittest.main()
