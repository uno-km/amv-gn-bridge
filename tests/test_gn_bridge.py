# Copyright (c) 2026 Eunho Kim / AMEVA Foundation. All rights reserved.
# SPDX-License-Identifier: Apache-2.0 OR Commercial-License
"""Unit and integration tests for amv-gn-bridge."""

from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from amv_gn_bridge import engine, gateway, mcp_server
from amv_gn_bridge.telemetry_db import TelemetryDB


class TestGeminiBridgeCore(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self._patches = [
            patch.object(engine, "CONFIG_DIR", str(self.tmp)),
            patch.object(engine, "CONFIG_PATH", str(self.tmp / "accounts.json")),
            patch.object(engine, "LOGIN_STATE_PATH", str(self.tmp / "account_state.json")),
            patch.object(engine, "ACCOUNT_META_PATH", str(self.tmp / "account_meta.json")),
            patch.object(engine, "LEGACY_GEMINI_ACCOUNTS_PATH", str(self.tmp / "nonexistent_legacy.json")),
        ]
        for p in self._patches:
            p.start()
        self.test_db = TelemetryDB(db_path=self.tmp / "telemetry.db")
        self._db_patch_eng = patch.object(engine, "telemetry_db", self.test_db)
        self._db_patch_gw = patch.object(gateway, "telemetry_db", self.test_db)
        self._db_patch_eng.start()
        self._db_patch_gw.start()

    def tearDown(self) -> None:
        self._db_patch_gw.stop()
        self._db_patch_eng.stop()
        for p in reversed(self._patches):
            p.stop()
        self._td.cleanup()

    def test_format_hms_and_parse_reset_wait_sec(self) -> None:
        self.assertEqual(engine.format_hms(0), "00:00:00")
        self.assertEqual(engine.format_hms(3661), "01:01:01")
        self.assertEqual(engine.format_hms(21600), "06:00:00")

        wait_ko = engine.parse_reset_wait_sec("한도 도달. 오후 5:23에 재설정됩니다")
        self.assertIsNotNone(wait_ko)
        self.assertGreaterEqual(wait_ko, 60.0)
        self.assertLessEqual(wait_ko, 86400.0 + 300.0)

        wait_en = engine.parse_reset_wait_sec("Limit reached. Resets at 11:45 AM")
        self.assertIsNotNone(wait_en)
        self.assertIsNone(engine.parse_reset_wait_sec("no time info here"))

    def test_worker_quota_countdown_and_auto_expire(self) -> None:
        w = engine.GeminiWorker(9337, "acct1", str(self.tmp / "p1"), quota_cooldown_sec=3600)
        self.assertFalse(w.is_cooling_down())

        w.mark_quota("한도 도달", reset_wait_sec=7200)
        self.assertTrue(w.is_cooling_down())
        self.assertEqual(w.login_state, "quota")

        snap = w.quota_snapshot()
        self.assertTrue(snap["cooling_down"])
        self.assertEqual(snap["cooldown_kind"], "quota")
        self.assertTrue(snap["cooldown_remaining_hms"].startswith(("01:59:", "02:00:")))
        self.assertNotEqual(snap["quota_hit_str"], "")
        self.assertNotEqual(snap["cooldown_until_str"], "")

        # Persisted across worker reloads
        w2 = engine.GeminiWorker(9337, "acct1", str(self.tmp / "p1"))
        self.assertTrue(w2.is_cooling_down())
        self.assertEqual(w2.cooldown_kind, "quota")

        # Auto-expire when time passes cooldown_until
        self.assertFalse(w2.is_cooling_down(now=w2.cooldown_until + 5.0))
        self.assertEqual(w2.login_state, "ok")

    def test_pool_auto_failover_on_quota_and_all_exhausted(self) -> None:
        pool = engine.GeminiPool(mode="hot")
        pool.workers = [
            engine.GeminiWorker(9337, "acct1", str(self.tmp / "p1"), mode="hot"),
            engine.GeminiWorker(9338, "acct2", str(self.tmp / "p2"), mode="hot"),
        ]
        pool.workers[0].login_state = "ok"
        pool.workers[1].login_state = "ok"

        fake_b64 = base64.b64encode(b"x" * 100).decode("ascii")

        async def fail_quota(*args, **kwargs):
            pool.workers[0].mark_quota("일일 한도 도달", reset_wait_sec=3600)
            raise engine.GeminiQuotaError("한도 도달", reset_wait_sec=3600)

        async def ok_img(*args, **kwargs):
            return {
                "images": [{"b64_json": fake_b64, "mime_type": "image/png", "sha256": "abc", "bytes_len": 100}],
                "text": "",
                "worker_port": 9338,
                "account_name": "acct2",
            }

        with patch.object(pool.workers[0], "is_alive", return_value=False), \
             patch.object(pool.workers[1], "is_alive", return_value=False), \
             patch.object(pool.workers[0], "generate_image", side_effect=fail_quota), \
             patch.object(pool.workers[1], "generate_image", side_effect=ok_img):
            res = asyncio.run(pool.generate_image("a cinematic photo"))

        self.assertEqual(res["worker_port"], 9338)
        self.assertEqual(res["account_name"], "acct2")
        self.assertTrue(pool.workers[0].is_cooling_down())
        self.assertFalse(pool.workers[1].is_cooling_down())

        summary = self.test_db.get_summary()
        self.assertEqual(summary["total"]["queries"], 2)
        self.assertIn("9338", summary["by_account"])

        # Now exhaust acct2 as well -> GeminiAllQuotaExhausted with HH:MM:SS
        pool.workers[1].mark_quota("한도 도달", reset_wait_sec=1800)
        with self.assertRaises(engine.GeminiAllQuotaExhausted) as ctx:
            asyncio.run(pool.generate_image("another photo"))
        self.assertIn("acct2", str(ctx.exception))
        self.assertRegex(str(ctx.exception), r"\d{2}:\d{2}:\d{2}")

    def test_hot_mode_tab_recycling_opens_new_before_closing_old(self) -> None:
        w = engine.GeminiWorker(9337, "acct1", str(self.tmp / "p1"), mode="hot")
        w.tab_recycle_interval = 2
        events: list[str] = []

        def make_page(tag: str) -> MagicMock:
            p = MagicMock()
            p.url = engine.GEMINI_URL
            p.is_closed.return_value = False
            p.goto = AsyncMock(side_effect=lambda *a, **kw: events.append(f"goto:{tag}"))
            p.close = AsyncMock(side_effect=lambda: (events.append(f"close:{tag}"), setattr(p.is_closed, "return_value", True)))
            return p

        old_p = make_page("old")
        new_p = make_page("new")
        ctx = MagicMock()
        ctx.pages = [old_p]
        ctx.new_page = AsyncMock(side_effect=lambda: (events.append("new_page"), ctx.pages.append(new_p), new_p)[-1])
        browser = MagicMock()
        browser.is_connected.return_value = True
        w.browser = browser
        w.context = ctx
        w.page = old_p
        w._tab_request_count = 2

        with patch.object(engine.asyncio, "sleep", AsyncMock()):
            res = asyncio.run(w._recycle_tab("test"))
        self.assertIs(res, new_p)
        self.assertEqual(events, ["new_page", "goto:new", "close:old"])
        self.assertEqual(w._tab_request_count, 0)

    def test_gateway_accounts_crud_and_endpoints(self) -> None:
        pool = engine.GeminiPool(mode="hot")
        with patch.object(gateway, "engine", pool):
            client = TestClient(gateway.app)

            r_h = client.get("/health")
            self.assertEqual(r_h.status_code, 200)
            self.assertEqual(r_h.json()["gateway"], "amv-gn-bridge")

            r_add = client.post("/api/accounts", json={"name": "sub_acct", "port": 9455, "email": "sub@gmail.com"})
            self.assertEqual(r_add.status_code, 201)
            self.assertEqual(r_add.json()["account"]["port"], 9455)

            w = pool.get_worker(9455)
            self.assertIsNotNone(w)
            w.mark_quota("한도 도달", reset_wait_sec=3600)
            self.assertTrue(w.is_cooling_down())

            r_cd = client.post("/api/accounts/9455/reset-cooldown")
            self.assertEqual(r_cd.status_code, 200)
            self.assertFalse(r_cd.json()["account"]["cooling_down"])

            r_dash = client.get("/dashboard")
            self.assertEqual(r_dash.status_code, 200)
            self.assertIn("amv-gn-bridge", r_dash.text)

            r_models = client.get("/v1/models")
            self.assertEqual(r_models.status_code, 200)
            model_ids = [m["id"] for m in r_models.json()["data"]]
            self.assertIn("gemini-web-image", model_ids)

            r_del = client.delete("/api/accounts/9455")
            self.assertEqual(r_del.status_code, 200)

    def test_legacy_accounts_auto_import(self) -> None:
        legacy_file = self.tmp / "legacy_gemini.json"
        legacy_file.write_text(
            json.dumps([
                {"name": "main", "port": 9337, "profile": str(self.tmp / "prof_main")},
                {"name": "sub1", "port": 9338, "profile": str(self.tmp / "prof_sub1")},
            ]),
            encoding="utf-8",
        )
        with patch.object(engine, "LEGACY_GEMINI_ACCOUNTS_PATH", str(legacy_file)):
            accts = engine._load_accounts()
        self.assertEqual(len(accts), 2)
        self.assertEqual(accts[0]["name"], "main")
        self.assertEqual(accts[1]["port"], 9338)

    def test_mcp_status_tool(self) -> None:
        pool = engine.GeminiPool(mode="hot")
        with patch.object(mcp_server, "engine", pool), patch.object(mcp_server, "_find_gateway_url", return_value=None):
            out = asyncio.run(mcp_server.get_gemini_status())
        self.assertIn("[상태 점검]", out)

    def test_updater_check_and_hot_swap_script(self) -> None:
        from amv_gn_bridge import updater

        self.assertTrue(updater.is_newer("0.2.0", "0.1.0"))
        self.assertFalse(updater.is_newer("0.1.0", "0.1.0"))

        script = updater.build_swap_script(
            pid=12345,
            target_exe=self.tmp / "amv-gn-bridge.exe",
            new_exe=self.tmp / "amv-gn-bridge-0.2.0.exe.new",
        )
        self.assertIn("$waitPid = 12345", script)
        self.assertIn("Move-Item", script)
        self.assertIn("Start-Process -FilePath $target", script)

        fake_rel = updater.ReleaseInfo(
            version="0.2.0",
            notes="New features",
            url="https://github.com/uno-km/amv-gn-bridge/releases/tag/v0.2.0",
            exe_url="https://example.com/amv-gn-bridge.exe",
            sha256_url="https://example.com/amv-gn-bridge.exe.sha256",
            asset_size=1024,
        )
        pool = engine.GeminiPool(mode="hot")
        with patch.object(gateway, "engine", pool), \
             patch.object(updater, "fetch_latest_release", return_value=fake_rel):
            client = TestClient(gateway.app)
            r = client.get("/api/update/check")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertTrue(body["ok"])
            self.assertTrue(body["has_update"])
            self.assertEqual(body["latest_version"], "0.2.0")
            self.assertEqual(body["release"]["exe_url"], "https://example.com/amv-gn-bridge.exe")


if __name__ == "__main__":
    unittest.main()
