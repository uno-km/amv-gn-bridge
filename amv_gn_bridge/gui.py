# Copyright (c) 2026 Eunho Kim / AMEVA Foundation. All rights reserved.
# SPDX-License-Identifier: Apache-2.0 OR Commercial-License
"""
amv-gn-bridge Native Desktop GUI (CustomTkinter).
Embeds the :11501 FastAPI gateway in a background thread and displays real-time HH:MM:SS quota countdowns,
multi-account management, usage statistics, and one-click login/verification/cleanup controls.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
import webbrowser
from typing import Any, Dict, List

import customtkinter as ctk
import uvicorn

try:
    from ._version import __version__
    from .gateway import app as gateway_app, engine
except (ImportError, ValueError):
    from amv_gn_bridge._version import __version__
    from amv_gn_bridge.gateway import app as gateway_app, engine


DEFAULT_PORT = 11501


def _fmt_hms(sec: float) -> str:
    t = max(0, int(sec))
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


class GeminiBridgeGUI(ctk.CTk):
    def __init__(self, port: int = DEFAULT_PORT):
        super().__init__()
        self.port = int(port)
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._workers_cache: List[Dict[str, Any]] = []
        self._server_offset: float = 0.0
        self._closing = False
        self._uvicorn_server = None

        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        self.title(f"amv-gn-bridge v{__version__} · Gemini Multi-Account Manager")
        self.geometry("960x720")
        self.minsize(820, 580)
        self.protocol("WM_DELETE_WINDOW", self._on_quit)

        self._start_gateway_thread()
        self._build_ui()
        self._close_boot_splash()
        self.after(600, self._poll_status)
        self.after(1000, self._tick_countdowns)
        self.after(3000, lambda: self._check_update(manual=False))

    @staticmethod
    def _close_boot_splash():
        try:
            import pyi_splash  # type: ignore[import-not-found]
            pyi_splash.close()
        except Exception:
            pass

    def _start_gateway_thread(self):
        config = uvicorn.Config(gateway_app, host="127.0.0.1", port=self.port, log_level="warning")
        self._uvicorn_server = uvicorn.Server(config)

        def _run():
            try:
                self._uvicorn_server.run()
            except Exception as exc:
                print(f"[!] Gateway thread stopped: {exc}")

        threading.Thread(target=_run, daemon=True).start()

    def _build_ui(self):
        # Top Header
        top = ctk.CTkFrame(self, fg_color="#ffffff", corner_radius=12)
        top.pack(fill="x", padx=16, pady=(14, 10))

        title_lbl = ctk.CTkLabel(
            top,
            text=f"✦  amv-gn-bridge  v{__version__}  (Gemini 멀티계정 · :{self.port})",
            font=ctk.CTkFont(size=17, weight="bold"),
            text_color="#161c2d",
        )
        title_lbl.pack(side="left", padx=14, pady=12)

        btn_bar = ctk.CTkFrame(top, fg_color="transparent")
        btn_bar.pack(side="right", padx=10, pady=8)

        ctk.CTkButton(btn_bar, text="+ 계정 추가", width=92, fg_color="#4f46e5", hover_color="#4338ca", command=self._open_add_dialog).pack(side="left", padx=3)
        ctk.CTkButton(btn_bar, text="전체 점검", width=78, fg_color="#f1f5f9", text_color="#161c2d", hover_color="#e2e8f0", command=self._verify_all).pack(side="left", padx=3)
        ctk.CTkButton(btn_bar, text="업데이트", width=74, fg_color="#f1f5f9", text_color="#161c2d", hover_color="#e2e8f0", command=lambda: self._check_update(manual=True)).pack(side="left", padx=3)
        ctk.CTkButton(btn_bar, text="메모리 정리", width=88, fg_color="#f1f5f9", text_color="#161c2d", hover_color="#e2e8f0", command=self._purge_browsers).pack(side="left", padx=3)
        ctk.CTkButton(btn_bar, text="브라우저", width=74, fg_color="#eef2ff", text_color="#4f46e5", hover_color="#e0e7ff", command=lambda: webbrowser.open(f"{self.base_url}/dashboard")).pack(side="left", padx=3)
        ctk.CTkButton(btn_bar, text="완전 종료", width=78, fg_color="#fef2f2", text_color="#dc2626", hover_color="#fee2e2", command=self._on_quit).pack(side="left", padx=3)

        # Dynamic Update Banner (hidden until a newer GitHub Release is detected)
        self.update_banner = ctk.CTkFrame(self, fg_color="#eef2ff", corner_radius=10, border_width=1, border_color="#c7d2fe")
        self.update_banner_lbl = ctk.CTkLabel(self.update_banner, text="", font=ctk.CTkFont(size=12, weight="bold"), text_color="#3730a3")
        self.update_banner_lbl.pack(side="left", padx=12, pady=8)
        self.update_banner_btn = ctk.CTkButton(
            self.update_banner,
            text="지금 업데이트 & 자동 재시작",
            width=180,
            height=28,
            fg_color="#4f46e5",
            hover_color="#4338ca",
            command=self._apply_update,
        )
        self.update_banner_btn.pack(side="right", padx=10, pady=6)

        # KPI Strip
        kpi_row = ctk.CTkFrame(self, fg_color="transparent")
        kpi_row.pack(fill="x", padx=16, pady=(0, 10))
        kpi_row.columnconfigure((0, 1, 2, 3), weight=1)

        self.kpi_today = self._make_kpi(kpi_row, 0, "오늘 요청", "0건")
        self.kpi_accts = self._make_kpi(kpi_row, 1, "가용 / 전체 계정", "0 / 0")
        self.kpi_quota = self._make_kpi(kpi_row, 2, "한도 쉬는 중", "0개")
        self.kpi_mode = self._make_kpi(kpi_row, 3, "실행 모드", "HOT (RAM 보호)")

        # Notice Bar
        self.notice_lbl = ctk.CTkLabel(self, text="준비됨 · OpenAI API: http://127.0.0.1:11501/v1", font=ctk.CTkFont(size=12), text_color="#64748b", anchor="w")
        self.notice_lbl.pack(fill="x", padx=20, pady=(0, 6))

        # Accounts Scrollable Panel
        acct_wrap = ctk.CTkFrame(self, fg_color="#ffffff", corner_radius=12)
        acct_wrap.pack(fill="both", expand=True, padx=16, pady=(0, 14))

        sec_bar = ctk.CTkFrame(acct_wrap, fg_color="transparent")
        sec_bar.pack(fill="x", padx=14, pady=(10, 4))
        ctk.CTkLabel(sec_bar, text="Google Gemini 계정 상태 (한도 시:분:초 실시간 카운트다운)", font=ctk.CTkFont(size=14, weight="bold"), text_color="#161c2d").pack(side="left")

        self.mode_var = ctk.StringVar(value=engine.mode)
        mode_seg = ctk.CTkSegmentedButton(sec_bar, values=["hot", "cold"], variable=self.mode_var, command=self._toggle_mode, width=120)
        mode_seg.pack(side="right")

        self.acct_scroll = ctk.CTkScrollableFrame(acct_wrap, fg_color="#f8fafc", corner_radius=10)
        self.acct_scroll.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        self._card_cd_labels: Dict[int, ctk.CTkLabel] = {}

    def _make_kpi(self, parent, col: int, title: str, initial: str) -> ctk.CTkLabel:
        card = ctk.CTkFrame(parent, fg_color="#ffffff", corner_radius=10)
        card.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 6, 0))
        ctk.CTkLabel(card, text=title, font=ctk.CTkFont(size=11), text_color="#64748b").pack(anchor="w", padx=12, pady=(10, 2))
        val = ctk.CTkLabel(card, text=initial, font=ctk.CTkFont(size=18, weight="bold"), text_color="#161c2d")
        val.pack(anchor="w", padx=12, pady=(0, 10))
        return val

    def _api_call_bg(self, method: str, path: str, payload: Dict[str, Any] | None = None, on_done=None):
        def _work():
            try:
                data = json.dumps(payload).encode("utf-8") if payload is not None else None
                headers = {"Content-Type": "application/json"} if data is not None else {}
                req = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=120) as r:
                    res = json.loads(r.read().decode("utf-8", errors="replace"))
                if on_done and not self._closing:
                    self.after(0, lambda: on_done(True, res))
            except Exception as exc:
                if on_done and not self._closing:
                    self.after(0, lambda: on_done(False, str(exc)))

        threading.Thread(target=_work, daemon=True).start()

    def _poll_status(self):
        if self._closing:
            return

        def _done(ok: bool, data: Any):
            if ok and isinstance(data, dict):
                self._server_offset = float(data.get("server_time", time.time())) - time.time()
                self._workers_cache = data.get("workers", [])
                st = data.get("stats", {})
                self.kpi_today.configure(text=f"{st.get('today', {}).get('queries', 0)}건")
                mode = str(data.get("mode", "hot")).upper()
                self.mode_var.set(mode.lower())
                self.kpi_mode.configure(text=f"{mode} (최대 {data.get('settings', {}).get('max_active_browsers', 1)}창)")
                self._rebuild_account_cards()
            if not self._closing:
                self.after(4000, self._poll_status)

        self._api_call_bg("GET", "/api/status", on_done=_done)

    def _rebuild_account_cards(self):
        for child in self.acct_scroll.winfo_children():
            child.destroy()
        self._card_cd_labels.clear()

        now = time.time() + self._server_offset
        avail = 0
        quota_cnt = 0

        for w in self._workers_cache:
            port = int(w["port"])
            rem = max(0.0, float(w.get("cooldown_until", 0.0)) - now)
            is_cooling = rem > 0
            if is_cooling and w.get("cooldown_kind") == "quota":
                quota_cnt += 1
            if not is_cooling and w.get("login_state") != "required":
                avail += 1

            card = ctk.CTkFrame(self.acct_scroll, fg_color="#ffffff", corner_radius=10, border_width=1, border_color="#e2e8f0")
            card.pack(fill="x", padx=4, pady=5)

            left = ctk.CTkFrame(card, fg_color="transparent")
            left.pack(side="left", fill="x", expand=True, padx=12, pady=10)

            ctk.CTkLabel(
                left,
                text=f"{w['name']}  (:{port})",
                font=ctk.CTkFont(size=14, weight="bold"),
                text_color="#161c2d",
                anchor="w",
            ).pack(anchor="w")

            ident = w.get("login_identity") or w.get("email") or w.get("profile_dir") or ""
            ctk.CTkLabel(
                left,
                text=f"{ident}  ·  오늘 {w.get('today_queries', 0)}건 / 누적 {w.get('total_queries', 0)}건",
                font=ctk.CTkFont(size=11),
                text_color="#64748b",
                anchor="w",
            ).pack(anchor="w", pady=(2, 0))

            if is_cooling:
                elapsed = max(0.0, now - float(w.get("quota_hit_at", 0.0))) if w.get("quota_hit_at", 0.0) > 0 else 0.0
                cd_lbl = ctk.CTkLabel(
                    left,
                    text=f"⏳ 남은 {_fmt_hms(rem)}  |  경과 {_fmt_hms(elapsed)}  (복귀 {w.get('cooldown_until_str', '')})",
                    font=ctk.CTkFont(family="Consolas", size=12, weight="bold"),
                    text_color="#d97706",
                    anchor="w",
                )
                cd_lbl.pack(anchor="w", pady=(4, 0))
                self._card_cd_labels[port] = cd_lbl

            right = ctk.CTkFrame(card, fg_color="transparent")
            right.pack(side="right", padx=10, pady=10)

            if w.get("verifying"):
                st_txt, st_fg, st_tc = "점검 중...", "#eef2ff", "#4f46e5"
            elif w.get("busy"):
                st_txt, st_fg, st_tc = "작업 중", "#eef2ff", "#4f46e5"
            elif is_cooling and w.get("cooldown_kind") == "quota":
                st_txt, st_fg, st_tc = "한도 쉬는 중", "#fffbeb", "#d97706"
            elif w.get("login_state") == "required":
                st_txt, st_fg, st_tc = "로그인 필요", "#fef2f2", "#dc2626"
            elif w.get("login_state") == "ok":
                st_txt, st_fg, st_tc = ("연결됨(활성)" if w.get("alive") else "연결됨"), "#ecfdf5", "#059669"
            else:
                st_txt, st_fg, st_tc = "확인 전", "#f1f5f9", "#475569"

            ctk.CTkButton(right, text="점검", width=52, height=28, fg_color="#f1f5f9", text_color="#161c2d", hover_color="#e2e8f0", command=lambda p=port: self._verify_one(p)).pack(side="left", padx=3)
            ctk.CTkButton(
                right,
                text="창 열기" if w.get("login_state") == "ok" else "계정 연결",
                width=74,
                height=28,
                fg_color="#eef2ff" if w.get("login_state") == "ok" else "#4f46e5",
                text_color="#4f46e5" if w.get("login_state") == "ok" else "#ffffff",
                hover_color="#e0e7ff" if w.get("login_state") == "ok" else "#4338ca",
                command=lambda p=port: self._login_one(p),
            ).pack(side="left", padx=3)
            if is_cooling:
                ctk.CTkButton(right, text="해제", width=48, height=28, fg_color="#fffbeb", text_color="#d97706", hover_color="#fef3c7", command=lambda p=port: self._reset_cd(p)).pack(side="left", padx=3)
            ctk.CTkButton(right, text="삭제", width=48, height=28, fg_color="#fef2f2", text_color="#dc2626", hover_color="#fee2e2", command=lambda p=port: self._delete_one(p)).pack(side="left", padx=3)
            ctk.CTkLabel(right, text=st_txt, fg_color=st_fg, text_color=st_tc, corner_radius=8, padx=10, pady=4, font=ctk.CTkFont(size=11, weight="bold")).pack(side="left", padx=(6, 0))

        self.kpi_accts.configure(text=f"{avail} / {len(self._workers_cache)}")
        self.kpi_quota.configure(text=f"{quota_cnt}개")

    def _tick_countdowns(self):
        if self._closing:
            return
        now = time.time() + self._server_offset
        for w in self._workers_cache:
            port = int(w["port"])
            lbl = self._card_cd_labels.get(port)
            if not lbl:
                continue
            rem = max(0.0, float(w.get("cooldown_until", 0.0)) - now)
            if rem <= 0:
                lbl.configure(text="✓ 쿨다운 종료 · 복귀 완료", text_color="#059669")
            else:
                elapsed = max(0.0, now - float(w.get("quota_hit_at", 0.0))) if w.get("quota_hit_at", 0.0) > 0 else 0.0
                lbl.configure(text=f"⏳ 남은 {_fmt_hms(rem)}  |  경과 {_fmt_hms(elapsed)}  (복귀 {w.get('cooldown_until_str', '')})")
        self.after(1000, self._tick_countdowns)

    def _toggle_mode(self, mode: str):
        self.notice_lbl.configure(text=f"모드를 {mode.upper()}로 변경 중...")
        self._api_call_bg("POST", "/api/settings", {"mode": mode}, on_done=lambda ok, _: self.notice_lbl.configure(text=f"모드 변경 완료: {mode.upper()}"))

    def _verify_one(self, port: int):
        self.notice_lbl.configure(text=f"포트 {port} 로그인 및 이미지 한도 점검 중...")
        self._api_call_bg("POST", f"/api/accounts/{port}/verify", on_done=lambda ok, _: self.notice_lbl.configure(text=f"포트 {port} 점검 완료"))

    def _verify_all(self):
        self.notice_lbl.configure(text="전체 계정 순차 점검 시작 (RAM 보호를 위해 1개씩 점검)...")
        self._api_call_bg("POST", "/api/accounts/verify", on_done=lambda ok, _: self.notice_lbl.configure(text="전체 계정 점검 완료"))

    def _login_one(self, port: int):
        self.notice_lbl.configure(text=f"포트 {port} Google 로그인 창을 띄웠습니다. 로그인 완료 후 자동 반영됩니다.")
        self._api_call_bg("POST", f"/api/accounts/{port}/login")

    def _reset_cd(self, port: int):
        self._api_call_bg("POST", f"/api/accounts/{port}/reset-cooldown", on_done=lambda ok, _: self.notice_lbl.configure(text=f"포트 {port} 쿨다운을 즉시 해제했습니다."))

    def _delete_one(self, port: int):
        self._api_call_bg("DELETE", f"/api/accounts/{port}", on_done=lambda ok, _: self.notice_lbl.configure(text=f"포트 {port} 계정 삭제 완료"))

    def _purge_browsers(self):
        self._api_call_bg("POST", "/api/purge", on_done=lambda ok, _: self.notice_lbl.configure(text="유휴 Edge/Chrome 프로세스 정리 완료"))

    def _check_update(self, manual: bool = False):
        if self._closing:
            return
        if manual:
            self.notice_lbl.configure(text="GitHub 최신 릴리스 확인 중...")

        def _done(ok: bool, data: Any):
            if ok and isinstance(data, dict) and data.get("has_update"):
                ver = data.get("latest_version", "")
                self.update_banner_lbl.configure(text=f"✦ 새 버전 v{ver} 출시됨 (현재 v{__version__}) — 클릭 시 자동 다운로드·교체 후 재시작합니다.")
                self.update_banner.pack(fill="x", padx=16, pady=(0, 8), before=self.notice_lbl)
                self.notice_lbl.configure(text=f"새 릴리스 v{ver}을 감지했습니다.")
            else:
                try:
                    self.update_banner.pack_forget()
                except Exception:
                    pass
                if manual:
                    if ok and isinstance(data, dict) and data.get("ok"):
                        self.notice_lbl.configure(text=f"현재 최신 버전(v{__version__})입니다.")
                    else:
                        err = data.get("error") if isinstance(data, dict) else data
                        self.notice_lbl.configure(text=f"업데이트 확인 실패: {err}")
            if not manual and not self._closing:
                self.after(1800000, lambda: self._check_update(manual=False))

        self._api_call_bg("GET", "/api/update/check", on_done=_done)

    def _apply_update(self):
        self.update_banner_btn.configure(state="disabled", text="다운로드 및 교체 중...")
        self.notice_lbl.configure(text="새 릴리스 다운로드 중... 완료 즉시 자동 교체 및 재시작됩니다.")

        def _done(ok: bool, data: Any):
            if ok and isinstance(data, dict) and data.get("ok"):
                self.notice_lbl.configure(text=f"v{data.get('version', '')} 교체 완료 — 자동 재시작합니다.")
            else:
                self.update_banner_btn.configure(state="normal", text="지금 업데이트 & 자동 재시작")
                self.notice_lbl.configure(text=f"업데이트 적용 실패: {data}")

        self._api_call_bg("POST", "/api/update/apply", on_done=_done)

    def _open_add_dialog(self):
        dlg = ctk.CTkToplevel(self)
        dlg.title("Google Gemini 계정 추가")
        dlg.geometry("400x340")
        dlg.grab_set()

        ctk.CTkLabel(dlg, text="계정 이름 *").pack(anchor="w", padx=18, pady=(14, 2))
        e_name = ctk.CTkEntry(dlg, placeholder_text="예: sub_account_6")
        e_name.pack(fill="x", padx=18)

        ctk.CTkLabel(dlg, text="이메일 (선택)").pack(anchor="w", padx=18, pady=(10, 2))
        e_email = ctk.CTkEntry(dlg, placeholder_text="example@gmail.com")
        e_email.pack(fill="x", padx=18)

        ctk.CTkLabel(dlg, text="CDP 포트 (비우면 자동 할당)").pack(anchor="w", padx=18, pady=(10, 2))
        e_port = ctk.CTkEntry(dlg, placeholder_text="자동 할당")
        e_port.pack(fill="x", padx=18)

        ctk.CTkLabel(dlg, text="프로필 폴더 (비우면 자동 생성)").pack(anchor="w", padx=18, pady=(10, 2))
        e_prof = ctk.CTkEntry(dlg, placeholder_text="자동 생성")
        e_prof.pack(fill="x", padx=18)

        def _submit():
            name = e_name.get().strip()
            if not name:
                return
            port_s = e_port.get().strip()
            payload = {
                "name": name,
                "email": e_email.get().strip(),
                "port": int(port_s) if port_s.isdigit() else None,
                "profile_dir": e_prof.get().strip() or None,
                "browser": "edge",
            }
            dlg.destroy()

            def _after_add(ok: bool, res: Any):
                if ok and isinstance(res, dict) and res.get("account"):
                    p = int(res["account"]["port"])
                    self.notice_lbl.configure(text=f"'{name}'(:{p}) 추가 완료 — 로그인 창을 엽니다.")
                    self._login_one(p)
                else:
                    self.notice_lbl.configure(text=f"계정 추가 실패: {res}")

            self._api_call_bg("POST", "/api/accounts", payload, on_done=_after_add)

        ctk.CTkButton(dlg, text="추가 후 로그인 창 열기", fg_color="#4f46e5", command=_submit).pack(fill="x", padx=18, pady=20)

    def _on_quit(self):
        self._closing = True
        try:
            if self._uvicorn_server is not None:
                self._uvicorn_server.should_exit = True
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass
        os._exit(0)


def run_gui(port: int = DEFAULT_PORT):
    app = GeminiBridgeGUI(port=port)
    app.mainloop()
