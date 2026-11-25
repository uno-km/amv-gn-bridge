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

    def test_parallel_requests_dispatch_across_distinct_workers(self) -> None:
        pool = engine.GeminiPool(mode="hot")
        w1 = engine.GeminiWorker(9337, "acct1", str(self.tmp / "p1"), mode="hot")
        w2 = engine.GeminiWorker(9338, "acct2", str(self.tmp / "p2"), mode="hot")
        w3 = engine.GeminiWorker(9339, "acct3", str(self.tmp / "p3"), mode="hot")
        for w in (w1, w2, w3):
            w.login_state = "ok"
        pool.workers = [w1, w2, w3]

        fake_b64 = base64.b64encode(b"x" * 100).decode("ascii")
        started_ports: list[int] = []

        async def run_parallel() -> list[int]:
            async def fake_gen(w: engine.GeminiWorker, prompt: str, **kwargs):
                started_ports.append(w.port)
                await asyncio.sleep(0.15)
                return {
                    "images": [{"b64_json": fake_b64, "mime_type": "image/png", "sha256": "abc", "bytes_len": 100}],
                    "text": "",
                    "worker_port": w.port,
                    "account_name": w.name,
                }

            def make_gen(w: engine.GeminiWorker):
                async def _impl(*a, **kw):
                    return await fake_gen(w, *a, **kw)
                return _impl

            with patch.object(w1, "is_alive", return_value=False), \
                 patch.object(w2, "is_alive", return_value=False), \
                 patch.object(w3, "is_alive", return_value=False), \
                 patch.object(w1, "generate_image", side_effect=make_gen(w1)), \
                 patch.object(w2, "generate_image", side_effect=make_gen(w2)), \
                 patch.object(w3, "generate_image", side_effect=make_gen(w3)):
                results = await asyncio.gather(
                    pool.generate_image("p1"),
                    pool.generate_image("p2"),
                    pool.generate_image("p3"),
                )
            return [r["worker_port"] for r in results]

        used_ports = asyncio.run(run_parallel())
        self.assertEqual(set(used_ports), {9337, 9338, 9339})
        self.assertEqual(len(started_ports), 3)
        for w in (w1, w2, w3):
            self.assertEqual(w._pending_count, 0)

    def test_sorry_url_fast_failover(self) -> None:
        self.assertTrue(engine._is_sorry_url("https://www.google.com/sorry/index?continue=https://gemini.google.com/app"))
        self.assertFalse(engine._is_sorry_url("https://gemini.google.com/app"))
        w = engine.GeminiWorker(9337, "acct1", str(self.tmp / "p1"), mode="hot")
        fake_page = MagicMock()
        fake_page.url = "https://www.google.com/sorry/index?continue=https://gemini.google.com/app"
        fake_page.goto = AsyncMock()
        with patch.object(w, "connect", AsyncMock(return_value=fake_page)), \
             patch.object(engine.asyncio, "sleep", AsyncMock()):
            with self.assertRaises(engine.GeminiLoginRequired):
                asyncio.run(w._prepare_page(reset_chat=True))
        self.assertTrue(w.is_cooling_down())
        self.assertEqual(w.cooldown_kind, "login")

    def test_profile_sanitize_prunes_sessions_preserves_cookies(self) -> None:
        prof = self.tmp / "edge_prof"
        default_dir = prof / "Default"
        sess_dir = default_dir / "Sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        prefs = default_dir / "Preferences"
        prefs.write_text(json.dumps({"profile": {"exit_type": "Crashed", "exited_cleanly": False}}), encoding="utf-8")
        cookies = default_dir / "Cookies"
        cookies.write_bytes(b"SQLITE_COOKIES_DATA")
        tabs_f = sess_dir / "Tabs_13370001"
        sess_f = sess_dir / "Session_13370001"
        tabs_f.write_bytes(b"x" * 1024)
        sess_f.write_bytes(b"y" * 512)

        engine._sanitize_profile_exit_type(str(prof))
        removed = engine._prune_stale_session_files(str(prof))

        self.assertEqual(removed, 2)
        self.assertFalse(tabs_f.exists())
        self.assertFalse(sess_f.exists())
        self.assertTrue(cookies.exists())
        saved_prefs = json.loads(prefs.read_text(encoding="utf-8"))
        self.assertEqual(saved_prefs["profile"]["exit_type"], "Normal")
        self.assertTrue(saved_prefs["profile"]["exited_cleanly"])
        self.assertEqual(saved_prefs["session"]["restore_on_startup"], 5)

    def test_connect_trims_extra_tabs_to_single_page(self) -> None:
        w = engine.GeminiWorker(9337, "acct1", str(self.tmp / "p1"), mode="hot")

        def make_tab(url: str) -> MagicMock:
            p = MagicMock()
            p.url = url
            p.is_closed.return_value = False
            p.close = AsyncMock()
            return p

        p1 = make_tab(engine.GEMINI_URL)
        p2 = make_tab("about:blank")
        p3 = make_tab("edge://newtab/")
        ctx = MagicMock()
        ctx.pages = [p1, p2, p3]
        browser = MagicMock()
        browser.is_connected.return_value = True
        browser.contexts = [ctx]

        pw_obj = MagicMock()
        pw_obj.chromium.connect_over_cdp = AsyncMock(return_value=browser)
        pw_cm = MagicMock()
        pw_cm.start = AsyncMock(return_value=pw_obj)

        with patch.object(w, "ensure_running", AsyncMock(return_value=True)), \
             patch.object(engine, "async_playwright", return_value=pw_cm):
            page = asyncio.run(w.connect())

        self.assertIs(page, p1)
        p1.close.assert_not_awaited()
        p2.close.assert_awaited_once()
        p3.close.assert_awaited_once()

    def test_ensure_running_kills_process_on_early_exit(self) -> None:
        w = engine.GeminiWorker(9337, "acct1", str(self.tmp / "p1"), mode="hot")
        fake_proc = MagicMock()
        fake_proc.pid = 42424
        fake_proc.poll.return_value = 0

        with patch.object(w, "is_alive", return_value=False), \
             patch.object(w, "kill_browser_process") as mock_kill, \
             patch.object(engine, "_find_browser_binary", return_value="msedge.exe"), \
             patch.object(engine.subprocess, "Popen", return_value=fake_proc), \
             patch.object(engine.asyncio, "sleep", AsyncMock()):
            with self.assertRaises(RuntimeError):
                asyncio.run(w.ensure_running(visible=False))

        mock_kill.assert_called_with(force=True)

    def test_fifo_queue_order_and_queued_vs_active_counts(self) -> None:
        pool = engine.GeminiPool(mode="hot")
        w1 = engine.GeminiWorker(9337, "acct1", str(self.tmp / "p1"), mode="hot")
        w2 = engine.GeminiWorker(9338, "acct2", str(self.tmp / "p2"), mode="hot")
        w1.login_state = "ok"
        w2.login_state = "ok"
        pool.workers = [w1, w2]

        fake_b64 = base64.b64encode(b"z" * 100).decode("ascii")
        started_order: list[str] = []
        observed_counts: list[tuple[int, int]] = []

        async def run_scenario() -> None:
            def make_gen(w: engine.GeminiWorker):
                async def _impl(prompt: str, **kwargs):
                    started_order.append(prompt)
                    await asyncio.sleep(0.35)
                    return {
                        "images": [{"b64_json": fake_b64, "mime_type": "image/png", "sha256": "abc", "bytes_len": 100}],
                        "text": "",
                        "worker_port": w.port,
                        "account_name": w.name,
                    }
                return _impl

            with patch.object(w1, "is_alive", return_value=False), \
                 patch.object(w2, "is_alive", return_value=False), \
                 patch.object(w1, "generate_image", side_effect=make_gen(w1)), \
                 patch.object(w2, "generate_image", side_effect=make_gen(w2)):
                tasks = []
                for i in range(5):
                    tasks.append(asyncio.create_task(pool.generate_image(f"req-{i}")))
                    await asyncio.sleep(0.03)
                await asyncio.sleep(0.05)
                observed_counts.append((pool.active_count, pool.queued_count))
                await asyncio.gather(*tasks)

        asyncio.run(run_scenario())
        self.assertEqual(observed_counts[0], (2, 3))
        self.assertEqual(started_order[:2], ["req-0", "req-1"])
        self.assertEqual(started_order[2:], ["req-2", "req-3", "req-4"])
        self.assertEqual(pool.active_count, 0)
        self.assertEqual(pool.queued_count, 0)

    def test_mcp_does_not_double_launch_on_gateway_timeout_or_http_error(self) -> None:
        import urllib.error
        import io

        mock_engine = MagicMock()
        mock_engine.query = AsyncMock(return_value="should-not-run")
        mock_engine.generate_image = AsyncMock(return_value={})

        with patch.object(mcp_server, "engine", mock_engine), \
             patch.object(mcp_server, "_find_gateway_url", return_value="http://127.0.0.1:11501"):
            with patch.object(mcp_server.urllib.request, "urlopen", side_effect=TimeoutError("read timed out")):
                res_q = asyncio.run(mcp_server.ask_gemini("hello"))
                res_i = asyncio.run(mcp_server.generate_gemini_image("draw cat", str(self.tmp / "out.png")))
            self.assertIn("이중 실행 방지", res_q)
            self.assertIn("이중 실행 방지", res_i)

            http_err = urllib.error.HTTPError(
                "http://127.0.0.1:11501/v1/chat/completions", 429, "Too Many Requests", {}, io.BytesIO(b'{"detail":"quota"}')
            )
            with patch.object(mcp_server.urllib.request, "urlopen", side_effect=http_err):
                res_429 = asyncio.run(mcp_server.ask_gemini("hello"))
            self.assertIn("HTTP 429", res_429)

        mock_engine.query.assert_not_awaited()
        mock_engine.generate_image.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
