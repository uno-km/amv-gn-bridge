# Copyright (c) 2026 Eunho Kim / AMEVA Foundation. All rights reserved.
# SPDX-License-Identifier: Apache-2.0 OR Commercial-License
"""
amv-gn-bridge Core Engine — Multi-Account Google Gemini Web Orchestrator.
- Supports unlimited Google account profiles with isolated CDP debugging ports (9337+).
- Automatic quota detection (UI aria-disabled + response text) with exact HH:MM:SS countdown & auto-failover to next account.
- Zero-leak process tree teardown (/F /T), crash bubble suppression, and hot-mode renderer tab recycling.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import hashlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

try:
    from .telemetry_db import telemetry_db
    from .settings import load_settings, save_settings, DEFAULT_SETTINGS, CONFIG_DIR
except (ImportError, ValueError):
    from amv_gn_bridge.telemetry_db import telemetry_db
    from amv_gn_bridge.settings import load_settings, save_settings, DEFAULT_SETTINGS, CONFIG_DIR

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

GEMINI_URL = "https://gemini.google.com/app"
COMPOSER_SELECTOR = "div[contenteditable='true'], rich-textarea .ql-editor, textarea[aria-label]"
LOGIN_HOSTS = ("accounts.google.com", "accounts.youtube.com")
COMPOSER_TIMEOUT_SEC = 45
LOGIN_TIMEOUT_SEC = 300
DEFAULT_BASE_CDP_PORT = 9337

try:
    TAB_RECYCLE_INTERVAL = max(1, int(os.environ.get("GEMINI_TAB_RECYCLE_INTERVAL", "15")))
except ValueError:
    TAB_RECYCLE_INTERVAL = 15

CONFIG_PATH = os.path.join(CONFIG_DIR, "accounts.json")
LOGIN_STATE_PATH = os.path.join(CONFIG_DIR, "account_state.json")
ACCOUNT_META_PATH = os.path.join(CONFIG_DIR, "account_meta.json")
LEGACY_GEMINI_ACCOUNTS_PATH = os.environ.get("AMV_GN_IMPORT_ACCOUNTS", "")


class GeminiLoginRequired(RuntimeError):
    """The account profile is not signed in to Google Gemini."""


class GeminiQuotaError(RuntimeError):
    """The account reached its Gemini usage/image generation limit."""

    def __init__(self, message: str, *, reset_wait_sec: Optional[float] = None):
        super().__init__(message)
        self.reset_wait_sec = reset_wait_sec


class GeminiTransientError(RuntimeError):
    """Temporary Gemini service or network error."""


class GeminiPolicyRefusalError(RuntimeError):
    """Content generation refused by Gemini policy."""


class GeminiAllQuotaExhausted(GeminiQuotaError):
    """All configured Gemini accounts are currently in quota/login cooldown."""


_RE_QUOTA = re.compile(
    r"(한도|limit|quota|내일|tomorrow|too many|너무 많|도달|재설정|reset|rate.?limit|"
    r"any more images|more images today|daily (?:image )?(?:limit|quota))",
    re.IGNORECASE,
)
_RE_TRANSIENT = re.compile(
    r"(잠시 후 다시|다시 시도해|문제가 발생했|문제가 생겼|나중에 다시|일시적|"
    r"try again later|please try again|something went wrong|temporarily unavailable)",
    re.IGNORECASE,
)
_RE_REFUSAL = re.compile(
    r"(생성할 수 없|만들 수 없|도와드릴 수 없|정책|가이드라인|안전 정책|"
    r"can['’]?t (?:create|generate|help)|cannot (?:create|generate)|unable to (?:create|generate)|safety policy)",
    re.IGNORECASE,
)


def format_hms(seconds: float) -> str:
    """초 단위 시간을 HH:MM:SS 문자열로 포맷한다."""
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def parse_reset_wait_sec(text: str, *, extra_buffer_sec: float = 180.0) -> Optional[float]:
    """'오후 5:23에 재설정' / '5:23 PM' 같은 안내에서 다음 재설정까지 남은 초(+여유 버퍼)를 추출한다."""
    if not text:
        return None
    m = re.search(r"(오전|오후)\s*(\d{1,2}):(\d{2})", text) or re.search(
        r"(\d{1,2}):(\d{2})\s*(AM|PM)", text, re.IGNORECASE
    )
    if not m:
        return None
    if m.group(1) in ("오전", "오후"):
        meridiem, hour, minute = m.group(1), int(m.group(2)) % 12, int(m.group(3))
    else:
        meridiem, hour, minute = m.group(3), int(m.group(1)) % 12, int(m.group(2))
    if meridiem.lower() in ("오후", "pm"):
        hour += 12
    now = time.localtime()
    target = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hour, minute, 0, 0, 0, -1))
    now_ts = time.time()
    if target <= now_ts:
        target += 86400
    return max(60.0, (target - now_ts) + extra_buffer_sec)


def _is_login_url(url: str) -> bool:
    low = (url or "").lower()
    return any(host in low for host in LOGIN_HOSTS)


class EventBus:
    """SSE 구독자 브로드캐스터 — asyncio.Queue 기반."""

    def __init__(self):
        self._queues: List[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        try:
            self._queues.remove(q)
        except ValueError:
            pass

    def publish(self, event: Dict[str, Any]):
        dead = []
        for q in self._queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    dead.append(q)
            except Exception:
                dead.append(q)
        for q in dead:
            self.unsubscribe(q)


event_bus = EventBus()


def _find_edge_binary() -> str:
    if sys.platform == "darwin":
        mac_path = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
        return mac_path if os.path.isfile(mac_path) else "microsoft-edge"
    candidates = [
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return "msedge"


def _find_chrome_binary() -> str:
    if sys.platform == "darwin":
        mac_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        return mac_path if os.path.isfile(mac_path) else "google-chrome"
    candidates = [
        os.path.join(os.environ.get("ProgramFiles", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return ""


def _find_browser_binary(profile_dir: Optional[str] = None, preferred_browser: Optional[str] = None) -> str:
    """프로필의 Last Browser 기록 또는 계정 browser 선호 설정에 맞춰 Edge/Chrome 바이너리를 선택한다."""
    if profile_dir:
        lb = Path(profile_dir) / "Last Browser"
        if lb.is_file():
            try:
                raw = lb.read_bytes().replace(b"\x00", b"").decode("utf-8", errors="ignore").lower()
                if "chrome.exe" in raw or "google chrome" in raw:
                    chrome_bin = _find_chrome_binary()
                    if chrome_bin:
                        return chrome_bin
                elif "msedge.exe" in raw or "microsoft edge" in raw:
                    return _find_edge_binary()
            except OSError:
                pass
    pref = (preferred_browser or "").strip().lower()
    if pref in ("chrome", "google-chrome", "google chrome"):
        chrome_bin = _find_chrome_binary()
        if chrome_bin:
            return chrome_bin
    return _find_edge_binary()


def _sanitize_profile_exit_type(profile_dir: str) -> None:
    """Edge/Chrome 비정상 종료 기록으로 인한 '페이지 복원' 팝업 버블을 원천 방지한다."""
    if not profile_dir:
        return
    pref_path = os.path.join(profile_dir, "Default", "Preferences")
    if not os.path.isfile(pref_path):
        return
    try:
        with open(pref_path, "r", encoding="utf-8", errors="replace") as f:
            prefs = json.load(f)
        modified = False
        profile_section = prefs.get("profile")
        if isinstance(profile_section, dict):
            if profile_section.get("exit_type") != "Normal":
                profile_section["exit_type"] = "Normal"
                modified = True
            if profile_section.get("exited_cleanly") is not True:
                profile_section["exited_cleanly"] = True
                modified = True
        if modified:
            with open(pref_path, "w", encoding="utf-8") as f:
                json.dump(prefs, f, ensure_ascii=False)
    except Exception:
        pass


def _extract_profile_account_hint(profile_dir: str) -> str:
    """브라우저 프로필 Default/Preferences의 account_info에서 Google 계정 식별 힌트를 읽는다."""
    if not profile_dir:
        return ""
    pref_path = os.path.join(profile_dir, "Default", "Preferences")
    if not os.path.isfile(pref_path):
        return ""
    try:
        with open(pref_path, "r", encoding="utf-8", errors="replace") as f:
            prefs = json.load(f)
    except (OSError, ValueError):
        return ""
    info_list = prefs.get("account_info")
    if not isinstance(info_list, list):
        return ""
    for item in info_list:
        if not isinstance(item, dict):
            continue
        email = str(item.get("email") or "").strip()
        full_name = str(item.get("full_name") or "").strip()
        if email and full_name and full_name.lower() != email.lower():
            return f"{full_name} ({email})"
        if email:
            return email
        if full_name:
            return full_name
    return ""


async def _extract_gemini_identity(page: Page, profile_dir: str = "") -> str:
    """활성 Gemini 페이지 DOM(Google 계정 버튼 aria-label 등)에서 로그인 이메일을 추출한다."""
    js_probe = r"""() => {
      const emailRe = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/;
      const nodes = document.querySelectorAll('a[aria-label*="Google"], button[aria-label*="Google"], a[aria-label*="@"], button[aria-label*="@"], img[alt*="@"]');
      for (const el of nodes) {
        const raw = (el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('alt') || '').trim();
        const m = raw.match(emailRe);
        if (m) return m[0];
      }
      return '';
    }"""
    try:
        ident = await page.evaluate(js_probe)
        if isinstance(ident, str) and ident.strip():
            return ident.strip()
    except Exception:
        pass
    return _extract_profile_account_hint(profile_dir)


def _read_json_dict(path: str, label: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"[!] {label} 읽기 실패: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_dict(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_login_states() -> Dict[str, str]:
    raw = _read_json_dict(LOGIN_STATE_PATH, "account_state.json")
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}


def _write_login_state(port: int, state: str) -> None:
    states = _read_login_states()
    states[str(port)] = state
    _write_json_dict(LOGIN_STATE_PATH, states)


def _read_account_meta() -> Dict[str, Dict[str, Any]]:
    raw = _read_json_dict(ACCOUNT_META_PATH, "account_meta.json")
    return {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}


def _write_account_meta(port: int, patch: Dict[str, Any]) -> None:
    meta = _read_account_meta()
    cur = dict(meta.get(str(port), {}))
    cur.update(patch)
    meta[str(port)] = cur
    _write_json_dict(ACCOUNT_META_PATH, meta)


def _is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", int(port))) == 0


def _pick_next_port(used_ports: set[int]) -> int:
    cand = DEFAULT_BASE_CDP_PORT
    while cand in used_ports or _is_port_in_use(cand):
        cand += 1
        if cand > 65535:
            raise ValueError("사용 가능한 CDP 포트를 찾을 수 없습니다.")
    return cand


def _save_accounts(accounts: List[Dict[str, Any]]) -> float:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)
    return os.path.getmtime(CONFIG_PATH)


def _load_accounts() -> List[Dict[str, Any]]:
    """계정 목록 로드. 최초 실행 시 기존 gemini_web_accounts.json(5개 프로필)이 있으면 자동 임포트한다."""
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, list) and len(loaded) > 0:
                return loaded
        except Exception as exc:
            print(f"[!] accounts.json 읽기 실패, 기본값으로 재생성합니다: {exc}")

    # 기존 AMV-COMPANY에 로그인된 gemini_web_accounts.json이 있으면 자동 이식
    if LEGACY_GEMINI_ACCOUNTS_PATH and os.path.isfile(LEGACY_GEMINI_ACCOUNTS_PATH):
        try:
            with open(LEGACY_GEMINI_ACCOUNTS_PATH, "r", encoding="utf-8") as f:
                legacy = json.load(f)
            imported = []
            for item in legacy:
                if not isinstance(item, dict) or not all(k in item for k in ("name", "port")):
                    continue
                prof = item.get("profile_dir") or item.get("profile") or ""
                if not prof:
                    continue
                imported.append({
                    "port": int(item["port"]),
                    "name": str(item["name"]),
                    "profile_dir": str(prof),
                    "browser": str(item.get("browser") or "edge"),
                    "email": str(item.get("email") or ""),
                })
            if imported:
                print(f"[+] 기존 Gemini 웹 계정 {len(imported)}개를 자동 임포트했습니다.")
                _save_accounts(imported)
                return imported
        except Exception as exc:
            print(f"[!] 기존 gemini_web_accounts.json 자동 임포트 경고: {exc}")

    home = str(Path.home())
    defaults = [
        {
            "port": DEFAULT_BASE_CDP_PORT,
            "name": "Gemini 기본 계정",
            "profile_dir": os.path.join(home, "Desktop", "gemini_profile"),
            "browser": "edge",
            "email": "",
        }
    ]
    _save_accounts(defaults)
    return defaults


_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class GeminiWorker:
    """단일 Google 계정용 Gemini 웹 CDP 워커."""

    def __init__(
        self,
        port: int,
        name: str,
        profile_dir: str,
        mode: str = "hot",
        standby_timeout_sec: int = 300,
        browser: Optional[str] = None,
        email: Optional[str] = None,
        quota_cooldown_sec: int = 21600,
        login_cooldown_sec: int = 900,
        min_gap_sec: int = 10,
    ):
        self.port = int(port)
        self.name = name
        self.profile_dir = profile_dir
        self.configured_email: str = (email or "").strip()
        self.preferred_browser: Optional[str] = browser.strip().lower() if isinstance(browser, str) and browser.strip() else "edge"
        self.mode = mode if mode in ("cold", "hot") else "hot"
        self.standby_timeout_sec = max(10, int(standby_timeout_sec))
        self.quota_cooldown_sec = max(60, int(quota_cooldown_sec))
        self.login_cooldown_sec = max(30, int(login_cooldown_sec))
        self.min_gap_sec = max(0, int(min_gap_sec))

        self.cdp_url = f"http://127.0.0.1:{self.port}"
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._lock = asyncio.Lock()
        self._reserved: bool = False

        self.login_state: str = _read_login_states().get(str(self.port), "unknown")
        _meta = _read_account_meta().get(str(self.port), {})
        self.login_identity: str = str(_meta.get("identity") or self.configured_email or "")
        self.verified_at: float = float(_meta.get("verified_at") or 0.0)
        self.verify_detail: str = str(_meta.get("detail") or "")
        self.quota_hit_at: float = float(_meta.get("quota_hit_at") or 0.0)
        self.cooldown_until: float = float(_meta.get("cooldown_until") or 0.0)
        self.cooldown_kind: str = str(_meta.get("cooldown_kind") or "")
        self.cooldown_reason: str = str(_meta.get("cooldown_reason") or "")
        self.verifying: bool = False

        self._standby_task: Optional[asyncio.Task] = None
        self._standby_deadline: Optional[float] = None
        self.last_text: str = ""
        self._last_call_at: float = 0.0
        self._launched_by_bridge: bool = False
        self._browser_pid: Optional[int] = None
        self._tab_request_count: int = 0
        self._needs_tab_recycle: bool = False
        self.tab_recycle_interval: int = TAB_RECYCLE_INTERVAL

    # ── 한도·쿨다운 시:분:초 상태 관리 ────────────────────────────────────

    def cooldown_remaining_sec(self, now: Optional[float] = None) -> float:
        t = time.time() if now is None else now
        if self.cooldown_until <= t:
            return 0.0
        return self.cooldown_until - t

    def is_cooling_down(self, now: Optional[float] = None) -> bool:
        t = time.time() if now is None else now
        if self.cooldown_until > t:
            return True
        # 쿨다운이 만료되었으면 상태를 자동 복귀
        if self.cooldown_until > 0 and self.cooldown_until <= t:
            self.clear_cooldown(auto_expired=True)
        return False

    def mark_quota(self, reason: str, *, reset_wait_sec: Optional[float] = None) -> None:
        """한도 도달 시각과 재설정까지 남은 초를 기록하고 시:분:초 카운트다운을 시작한다."""
        now = time.time()
        wait = float(reset_wait_sec) if (reset_wait_sec is not None and reset_wait_sec > 0) else float(self.quota_cooldown_sec)
        wait = min(float(self.quota_cooldown_sec) * 4, max(60.0, wait))
        self.quota_hit_at = round(now, 1)
        self.cooldown_until = round(now + wait, 1)
        self.cooldown_kind = "quota"
        self.cooldown_reason = (reason or "일일 한도 도달")[:240]
        self._set_login_state("quota")
        self._persist_meta(detail="quota_exhausted", verified_at=now)
        until_str = datetime.fromtimestamp(self.cooldown_until).strftime("%H:%M:%S")
        print(f"[!] [{self.name}] 한도 도달 -> {format_hms(wait)} 쿨다운 시작 (복귀 예정 {until_str})")
        event_bus.publish({"type": "accounts", "event": "quota_hit", "port": self.port, "worker": self.name})

    def mark_login_required(self, reason: str = "Google 로그인 필요") -> None:
        now = time.time()
        self.quota_hit_at = round(now, 1)
        self.cooldown_until = round(now + float(self.login_cooldown_sec), 1)
        self.cooldown_kind = "login"
        self.cooldown_reason = (reason or "Google 로그인 필요")[:240]
        self._set_login_state("required")
        self._persist_meta(detail="login_required", verified_at=now)
        event_bus.publish({"type": "accounts", "event": "login_required", "port": self.port, "worker": self.name})

    def clear_cooldown(self, *, auto_expired: bool = False) -> None:
        had_cd = self.cooldown_until > 0 or self.cooldown_kind != ""
        self.quota_hit_at = 0.0
        self.cooldown_until = 0.0
        self.cooldown_kind = ""
        self.cooldown_reason = ""
        if self.login_state == "quota":
            self._set_login_state("ok")
        if had_cd:
            self._persist_meta(detail="cooldown_expired" if auto_expired else "cooldown_cleared")
            event_bus.publish({"type": "accounts", "event": "cooldown_cleared", "port": self.port, "worker": self.name})

    def quota_snapshot(self, now: Optional[float] = None) -> Dict[str, Any]:
        t = time.time() if now is None else now
        rem = self.cooldown_remaining_sec(t)
        elapsed = max(0.0, t - self.quota_hit_at) if (rem > 0 and self.quota_hit_at > 0) else 0.0
        return {
            "cooling_down": rem > 0,
            "cooldown_kind": self.cooldown_kind if rem > 0 else "",
            "cooldown_reason": self.cooldown_reason if rem > 0 else "",
            "quota_hit_at": self.quota_hit_at if rem > 0 else 0.0,
            "quota_hit_str": datetime.fromtimestamp(self.quota_hit_at).strftime("%H:%M:%S") if (rem > 0 and self.quota_hit_at > 0) else "",
            "cooldown_until": self.cooldown_until if rem > 0 else 0.0,
            "cooldown_until_str": datetime.fromtimestamp(self.cooldown_until).strftime("%H:%M:%S") if rem > 0 else "",
            "cooldown_remaining_sec": round(rem, 1),
            "cooldown_remaining_hms": format_hms(rem) if rem > 0 else "00:00:00",
            "quota_elapsed_sec": round(elapsed, 1),
            "quota_elapsed_hms": format_hms(elapsed) if rem > 0 else "00:00:00",
        }

    def _set_login_state(self, state: str):
        self.login_state = state
        _write_login_state(self.port, state)

    def _persist_meta(
        self,
        *,
        identity: Optional[str] = None,
        detail: Optional[str] = None,
        verified_at: Optional[float] = None,
    ) -> None:
        if identity is not None:
            self.login_identity = identity
        if detail is not None:
            self.verify_detail = detail
        if verified_at is not None:
            self.verified_at = round(float(verified_at), 1)
        _write_account_meta(
            self.port,
            {
                "identity": self.login_identity,
                "detail": self.verify_detail,
                "verified_at": self.verified_at,
                "quota_hit_at": self.quota_hit_at,
                "cooldown_until": self.cooldown_until,
                "cooldown_kind": self.cooldown_kind,
                "cooldown_reason": self.cooldown_reason,
            },
        )

    # ── 웜 스탠바이 및 동적 설정 반영 ──────────────────────────────────────

    def _cancel_standby(self):
        if self._standby_task and not self._standby_task.done():
            self._standby_task.cancel()
        self._standby_task = None
        self._standby_deadline = None

    def _schedule_standby(self):
        self._cancel_standby()
        self._standby_deadline = time.time() + self.standby_timeout_sec
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._standby_deadline = None
            return
        self._standby_task = loop.create_task(self._standby_countdown())

    async def _standby_countdown(self):
        try:
            await asyncio.sleep(self.standby_timeout_sec)
            if not self.is_busy():
                print(f"[+] [{self.name}] [Standby] 웜 스탠바이({self.standby_timeout_sec}초) 만료 — 브라우저 프로세스 정리")
                await self._teardown_browser()
                self._standby_deadline = None
                event_bus.publish({"type": "accounts", "event": "standby_expired", "worker": self.name})
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            print(f"[!] [{self.name}] 웜 스탠바이 카운트다운 경고: {exc}")

    def apply_settings(self, settings: Dict[str, Any]):
        new_mode = str(settings.get("mode", self.mode)).lower().strip()
        if new_mode in ("cold", "hot"):
            self.mode = new_mode
        if "standby_timeout_sec" in settings:
            self.standby_timeout_sec = max(10, int(settings["standby_timeout_sec"]))
        if "quota_cooldown_sec" in settings:
            self.quota_cooldown_sec = max(60, int(settings["quota_cooldown_sec"]))
        if "login_cooldown_sec" in settings:
            self.login_cooldown_sec = max(30, int(settings["login_cooldown_sec"]))
        if "min_gap_sec" in settings:
            self.min_gap_sec = max(0, int(settings["min_gap_sec"]))

        if self.mode == "cold":
            self._cancel_standby()
            if not self.is_busy() and self.is_alive():
                try:
                    asyncio.get_running_loop().create_task(self.close())
                except RuntimeError:
                    pass
        elif self.mode == "hot":
            if not self.is_busy() and self.is_alive():
                self._schedule_standby()

    # ── 브라우저 프로세스 수명주기 (Zero-Leak /F /T + 탭 리사이클링) ────────

    def get_listening_pids(self) -> List[int]:
        now = time.monotonic()
        if hasattr(self, "_pids_cache_time") and now - self._pids_cache_time < 8.0:
            return self._pids_cache  # type: ignore[attr-defined]
        pids: List[int] = []
        try:
            port_str = str(int(self.port))
            if sys.platform == "darwin":
                result = subprocess.run(["lsof", "-ti", f":{port_str}"], capture_output=True, text=True, timeout=3)
                for line in result.stdout.strip().splitlines():
                    if line.strip().isdigit():
                        pids.append(int(line.strip()))
            else:
                netstat = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=3)
                for line in netstat.stdout.splitlines():
                    if f":{port_str}" not in line or "LISTENING" not in line:
                        continue
                    parts = line.strip().split()
                    if len(parts) >= 5 and parts[-1].isdigit() and int(parts[-1]) > 0:
                        pids.append(int(parts[-1]))
        except Exception:
            pass
        result = list(set(pids))
        self._pids_cache = result
        self._pids_cache_time = now
        return result

    def is_alive(self) -> bool:
        now = time.monotonic()
        if hasattr(self, "_alive_cache_time") and now - self._alive_cache_time < 2.5:
            return self._alive_cache  # type: ignore[attr-defined]
        alive = False
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.08)
                sock.connect(("127.0.0.1", int(self.port)))
            with _NO_PROXY_OPENER.open(f"{self.cdp_url}/json/version", timeout=0.5) as req:
                alive = req.status == 200
        except Exception:
            pass
        self._alive_cache = alive
        self._alive_cache_time = now
        return alive

    def is_busy(self) -> bool:
        return self._lock.locked() or self._reserved

    async def ensure_running(self, visible: bool = False) -> bool:
        if self.is_alive():
            if self._browser_pid is None:
                self._launched_by_bridge = False
            return True

        if isinstance(self._browser_pid, int) and self._browser_pid > 0:
            self.kill_browser_process(force=True)

        print(f"[!] [{self.name}] 포트 {self.port} Gemini 브라우저 기동 중...")
        os.makedirs(self.profile_dir, exist_ok=True)
        _sanitize_profile_exit_type(self.profile_dir)
        browser_path = _find_browser_binary(self.profile_dir, self.preferred_browser)
        args = [
            browser_path,
            f"--remote-debugging-port={int(self.port)}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
            "--noerrdialogs",
            "--disable-features=InfiniteSessionRestore",
            "--restore-last-session=false",
            "--disable-background-mode",
            "--disable-background-networking",
            "--disable-sync",
            "--no-service-autorun",
            "--metrics-recording-only",
        ]
        if not visible:
            args.append("--window-position=-3000,-3000")
        else:
            args.extend(["--window-position=120,80", "--window-size=1100,860"])
        popen_kwargs: Dict[str, Any] = {}
        if visible and sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 1
            popen_kwargs["startupinfo"] = si
        proc = subprocess.Popen(args + [GEMINI_URL], **popen_kwargs)
        raw_pid = getattr(proc, "pid", None)
        self._browser_pid = raw_pid if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) and raw_pid > 0 else None
        self._launched_by_bridge = True
        self.__dict__.pop("_pids_cache_time", None)
        self.__dict__.pop("_alive_cache_time", None)

        for _ in range(30):
            await asyncio.sleep(0.5)
            self.__dict__.pop("_alive_cache_time", None)
            if self.is_alive():
                print(f"[+] [{self.name}] 포트 {self.port} CDP 준비 완료.")
                return True
        raise RuntimeError(f"[{self.name}] 포트 {self.port} 브라우저를 기동할 수 없습니다.")

    async def connect(self, *, visible: bool = False) -> Page:
        await self.ensure_running(visible=visible)
        if not self.playwright:
            self.playwright = await async_playwright().start()
        if not self.browser or not self.browser.is_connected():
            self.browser = await self.playwright.chromium.connect_over_cdp(self.cdp_url)
            self.context = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()

        target_page = None
        for p in self.context.pages:
            url = (p.url or "").lower()
            if "gemini.google.com" in url or _is_login_url(url):
                target_page = p
                break
        if target_page is None:
            target_page = await self.context.new_page()
            await target_page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(2.0)

        self.page = target_page
        return self.page

    async def _recycle_tab(self, reason: str) -> Page:
        """hot 모드 장기 구동 시 누적된 렌더러 메모리를 해제하기 위해 새 탭을 먼저 연 뒤 기존 탭을 닫는다."""
        if not self.context or not self.browser or not self.browser.is_connected():
            self._tab_request_count = 0
            self._needs_tab_recycle = False
            return await self.connect()

        old_pages = list(self.context.pages)
        new_page = await self.context.new_page()
        try:
            await new_page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(1.5)
        except Exception:
            try:
                if not new_page.is_closed():
                    await new_page.close()
            except Exception:
                pass
            raise

        self.page = new_page
        closed_cnt = 0
        for p in old_pages:
            if p is new_page:
                continue
            try:
                if not p.is_closed():
                    await p.close()
                    closed_cnt += 1
            except Exception:
                pass
        self._tab_request_count = 0
        self._needs_tab_recycle = False
        print(f"[+] [{self.name}] [Recycle] 렌더러 탭 리사이클링 완료 ({reason}, 구 탭 {closed_cnt}개 정리)")
        return new_page

    async def cleanup(self):
        self._tab_request_count = 0
        self._needs_tab_recycle = False
        try:
            if self.page and not self.page.is_closed():
                await self.page.close()
        except Exception:
            pass
        self.page = None
        try:
            if self.context:
                await self.context.close()
        except Exception:
            pass
        self.context = None
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass
        self.browser = None
        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            pass
        self.playwright = None

    async def _graceful_close_browser(
        self,
        ctx: Optional[BrowserContext],
        page: Optional[Page],
        wait_timeout: float = 1.8,
    ) -> None:
        if ctx is None or page is None:
            return
        try:
            cdp = await ctx.new_cdp_session(page)
            try:
                await cdp.send("Browser.close")
            finally:
                try:
                    await cdp.detach()
                except Exception:
                    pass
        except Exception:
            return
        deadline = time.time() + wait_timeout
        while time.time() < deadline:
            self.__dict__.pop("_pids_cache_time", None)
            self.__dict__.pop("_alive_cache_time", None)
            if not self.get_listening_pids():
                break
            await asyncio.sleep(0.15)

    def kill_browser_process(self, *, force: bool = False, known_pids: Optional[List[int]] = None):
        if not force and not self._launched_by_bridge and self._browser_pid is None:
            return
        self.__dict__.pop("_pids_cache_time", None)
        self.__dict__.pop("_alive_cache_time", None)
        target_pids = set(self.get_listening_pids())
        if known_pids:
            target_pids.update(int(p) for p in known_pids if p)
        if isinstance(self._browser_pid, int) and self._browser_pid > 0:
            target_pids.add(int(self._browser_pid))

        for pid in sorted(target_pids):
            try:
                if sys.platform == "darwin":
                    res = subprocess.run(["kill", "-9", str(int(pid))], capture_output=True, timeout=5)
                else:
                    res = subprocess.run(["taskkill", "/F", "/T", "/PID", str(int(pid))], capture_output=True, timeout=5)
                if res.returncode == 0:
                    print(f"[+] [{self.name}] 브라우저 프로세스 트리(PID {pid}) 종료")
            except Exception as e:
                print(f"[!] [{self.name}] 브라우저 프로세스(PID {pid}) 종료 경고: {e}")

        self._launched_by_bridge = False
        self._browser_pid = None
        self.__dict__.pop("_pids_cache_time", None)
        self.__dict__.pop("_alive_cache_time", None)
        _sanitize_profile_exit_type(self.profile_dir)

    async def _teardown_browser(self, *, force: bool = False):
        self.__dict__.pop("_pids_cache_time", None)
        pids_before = self.get_listening_pids()
        if force or self._launched_by_bridge or self._browser_pid is not None:
            await self._graceful_close_browser(self.context, self.page)
        await self.cleanup()
        self.kill_browser_process(force=force, known_pids=pids_before)

    async def close(self, *, force: bool = False):
        self._cancel_standby()
        await self._teardown_browser(force=force)

    # ── 로그인/창 열기/한도 사전 점검 ──────────────────────────────────────

    async def _bring_window_to_front(self, page: Page) -> None:
        try:
            if self.context is not None:
                cdp = await self.context.new_cdp_session(page)
                try:
                    win = await cdp.send("Browser.getWindowForTarget")
                    wid = win.get("windowId") if isinstance(win, dict) else None
                    if wid is not None:
                        await cdp.send(
                            "Browser.setWindowBounds",
                            {
                                "windowId": wid,
                                "bounds": {"left": 120, "top": 80, "width": 1100, "height": 860, "windowState": "normal"},
                            },
                        )
                finally:
                    await cdp.detach()
        except Exception:
            pass
        try:
            await page.bring_to_front()
        except Exception:
            pass

    async def _is_signed_out_page(self, page: Page) -> bool:
        if _is_login_url(page.url):
            return True
        try:
            if await page.get_by_text("로그인", exact=True).count() > 0:
                return True
            if await page.get_by_text("Sign in", exact=True).count() > 0:
                return True
        except Exception:
            pass
        return False

    async def _wait_for_signed_in_composer(self, page: Page, timeout: float = COMPOSER_TIMEOUT_SEC) -> bool:
        deadline = time.time() + timeout
        textbox = page.locator(COMPOSER_SELECTOR).first
        while time.time() < deadline:
            if not await self._is_signed_out_page(page):
                try:
                    if await textbox.is_visible():
                        return True
                except Exception:
                    pass
            await asyncio.sleep(0.5)
        return False

    async def open_login(self, timeout: float = LOGIN_TIMEOUT_SEC) -> bool:
        """사용자가 Google 계정으로 직접 로그인할 수 있도록 브라우저 창을 전면에 띄운다."""
        async with self._lock:
            self._cancel_standby()
            try:
                await self._teardown_browser(force=True)
                await self.ensure_running(visible=True)
                page = await self.connect(visible=True)
                await self._bring_window_to_front(page)
                connected = await self._wait_for_signed_in_composer(page, timeout=timeout)
                if connected:
                    ident = await _extract_gemini_identity(page, self.profile_dir) or self.login_identity
                    self.clear_cooldown()
                    self._set_login_state("ok")
                    self._persist_meta(identity=ident, detail="verified_login", verified_at=time.time())
                else:
                    self._set_login_state("required")
                    self._persist_meta(detail="login_timeout", verified_at=time.time())
                return connected
            except Exception as exc:
                print(f"[!] [{self.name}] 계정 연결 실패: {type(exc).__name__}: {exc}")
                self._set_login_state("required")
                return False
            finally:
                await self._teardown_browser(force=True)

    async def _check_image_quota_menu(self, page: Page) -> Tuple[bool, str, Optional[float]]:
        """'업로드 및 도구' -> '이미지 만들기' 메뉴의 aria-disabled 상태로 이미지 한도를 점검한다.
        Returns: (quota_hit, detail_msg, reset_wait_sec)
        """
        try:
            tools_btn = page.get_by_label(re.compile(r"업로드 및 도구|Upload|Tools", re.IGNORECASE)).first
            if await tools_btn.count() == 0:
                return False, "", None
            await tools_btn.click(timeout=6000)
            await asyncio.sleep(0.7)
            create_item = page.locator("span:visible, button:visible, [role=menuitem]:visible").filter(
                has_text=re.compile(r"^\s*(이미지 만들기|Create images?)\s*$", re.IGNORECASE)
            ).first
            if await create_item.count() == 0:
                await page.keyboard.press("Escape")
                return False, "", None
            disabled = bool(
                await create_item.evaluate("e => !!e.closest('[aria-disabled=true], .mdc-list-item--disabled')")
            )
            if disabled:
                body_txt = ""
                try:
                    body_txt = await page.inner_text("body", timeout=3000)
                except Exception:
                    pass
                note_m = re.search(
                    r"(한도[^\n]{0,60}재설정[^\n]{0,20}|limit[^\n]{0,60}reset[^\n]{0,20}|오[전후]\s*\d{1,2}:\d{2}[^\n]{0,30})",
                    body_txt,
                    re.IGNORECASE,
                )
                msg = note_m.group(0).strip() if note_m else "이미지 만들기 비활성(일일 한도 도달)"
                await page.keyboard.press("Escape")
                return True, msg, parse_reset_wait_sec(msg)
            await page.keyboard.press("Escape")
            return False, "", None
        except Exception:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            return False, "", None

    async def verify_login(self, timeout: float = 40.0) -> Dict[str, Any]:
        """실제 Gemini 웹에 진입해 로그인 상태와 이미지 생성 한도(재설정 시각 포함)를 실시간 점검한다."""
        async with self._lock:
            self.verifying = True
            was_alive = self.is_alive()
            self._cancel_standby()
            probe_page: Optional[Page] = None
            ctx: Optional[BrowserContext] = None
            own_pw = None
            own_browser = None
            connected = False
            try:
                await self.ensure_running(visible=False)
                if self.browser and self.browser.is_connected() and self.context:
                    ctx = self.context
                else:
                    own_pw = await async_playwright().start()
                    own_browser = await own_pw.chromium.connect_over_cdp(self.cdp_url)
                    ctx = own_browser.contexts[0] if own_browser.contexts else await own_browser.new_context()

                probe_page = await ctx.new_page()
                try:
                    await probe_page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=min(30000, int(timeout * 1000)))
                except Exception:
                    pass
                await asyncio.sleep(2.5)

                if await self._is_signed_out_page(probe_page):
                    self.mark_login_required("Google 로그인 필요")
                    connected = False
                else:
                    connected = await self._wait_for_signed_in_composer(probe_page, timeout=max(10.0, timeout - 5.0))
                    if connected:
                        ident = await _extract_gemini_identity(probe_page, self.profile_dir) or self.login_identity
                        quota_hit, quota_msg, wait_sec = await self._check_image_quota_menu(probe_page)
                        if quota_hit:
                            self._persist_meta(identity=ident)
                            self.mark_quota(quota_msg, reset_wait_sec=wait_sec)
                        else:
                            self.clear_cooldown()
                            self._set_login_state("ok")
                            self._persist_meta(identity=ident, detail="verified_live", verified_at=time.time())
                    else:
                        self._set_login_state("required")
                        self._persist_meta(detail="composer_timeout", verified_at=time.time())

                return self.brief()
            except Exception as exc:
                print(f"[!] [{self.name}] 계정 라이브 점검 실패: {type(exc).__name__}: {exc}")
                self._set_login_state("required")
                self._persist_meta(detail=f"probe_error:{type(exc).__name__}", verified_at=time.time())
                return self.brief()
            finally:
                self.verifying = False
                if probe_page is not None and not probe_page.is_closed():
                    try:
                        await probe_page.close()
                    except Exception:
                        pass
                if not was_alive:
                    await self._graceful_close_browser(ctx, probe_page)
                if own_browser is not None:
                    try:
                        await own_browser.close()
                    except Exception:
                        pass
                if own_pw is not None:
                    try:
                        await own_pw.stop()
                    except Exception:
                        pass
                if was_alive and connected and self.mode == "hot":
                    self._schedule_standby()
                elif not was_alive:
                    await self._teardown_browser(force=True)

    # ── 텍스트 질의 및 이미지 생성 실행 ────────────────────────────────────

    async def _respect_min_gap(self):
        if self.min_gap_sec <= 0 or self._last_call_at <= 0:
            self._last_call_at = time.time()
            return
        gap = self.min_gap_sec - (time.time() - self._last_call_at)
        if gap > 0:
            await asyncio.sleep(gap)
        self._last_call_at = time.time()

    async def _prepare_page(self, *, reset_chat: bool = True) -> Page:
        page = await self.connect()
        recycled = False
        if self.mode == "hot":
            if self._needs_tab_recycle:
                page = await self._recycle_tab("예외/타임아웃 복구")
                recycled = True
            elif reset_chat and self._tab_request_count >= self.tab_recycle_interval:
                page = await self._recycle_tab(f"{self._tab_request_count}회 요청 주기 도달")
                recycled = True
        if reset_chat and not recycled:
            await page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(2.2)

        if await self._is_signed_out_page(page):
            self.mark_login_required("Google 로그인 풀림")
            raise GeminiLoginRequired(f"[{self.name}] Google 로그인이 필요합니다. GUI/대시보드에서 [계정 연결]을 진행하십시오.")

        if not await self._wait_for_signed_in_composer(page, timeout=COMPOSER_TIMEOUT_SEC):
            self.mark_login_required("Gemini 입력창 미감지")
            raise GeminiLoginRequired(f"[{self.name}] Gemini 입력창을 찾지 못했습니다. (URL: {page.url[:120]})")
        return page

    async def _attach_files(self, page: Page, file_paths: List[str]) -> None:
        valid = [str(Path(f).resolve()) for f in file_paths if os.path.isfile(f)]
        if not valid:
            return
        tools_btn = page.get_by_label(re.compile(r"업로드 및 도구|Upload|Tools", re.IGNORECASE)).first
        await tools_btn.click(timeout=8000)
        await asyncio.sleep(0.6)
        async with page.expect_file_chooser(timeout=10000) as fc_info:
            upload_item = page.locator("span:visible, button:visible, [role=menuitem]:visible").filter(
                has_text=re.compile(r"^\s*(파일|Upload files?)\s*$", re.IGNORECASE)
            ).first
            await upload_item.click(timeout=8000)
        fc = await fc_info.value
        await fc.set_files(valid)
        await asyncio.sleep(3.5)

    async def stream_query(
        self,
        prompt: str,
        *,
        reset_chat: bool = True,
        file_paths: Optional[List[str]] = None,
        timeout_sec: float = 300.0,
    ) -> AsyncGenerator[str, None]:
        """Gemini 웹 텍스트 질의 및 실시간 스트리밍 delta 출력."""
        async with self._lock:
            self._cancel_standby()
            self.last_text = ""
            await self._respect_min_gap()
            try:
                page = await self._prepare_page(reset_chat=reset_chat)
                if file_paths:
                    await self._attach_files(page, file_paths)

                baseline_count = await page.locator("model-response").count()
                composer = page.locator(COMPOSER_SELECTOR).first
                await composer.click()
                await page.keyboard.insert_text(prompt)
                await asyncio.sleep(0.4)
                await page.keyboard.press("Enter")

                emitted = ""
                stable = 0
                deadline = time.time() + max(15.0, float(timeout_sec))
                await asyncio.sleep(1.5)

                while time.time() < deadline:
                    state = await page.evaluate(
                        """(baseline) => {
                            const responses = Array.from(document.querySelectorAll('model-response'));
                            const stopBtn = document.querySelector('button[aria-label*="중지"], button[aria-label*="Stop"]');
                            if (responses.length <= baseline) return { text: '', stopping: !!stopBtn, count: responses.length };
                            const last = responses[responses.length - 1];
                            const body = last.querySelector('.markdown, message-content') || last;
                            return { text: (body.innerText || '').trim(), stopping: !!stopBtn, count: responses.length };
                        }""",
                        baseline_count,
                    )
                    txt = str(state.get("text") or "").strip()
                    stopping = bool(state.get("stopping"))

                    if txt and len(txt) > len(emitted):
                        if txt.startswith(emitted):
                            delta = txt[len(emitted):]
                        else:
                            delta = txt
                        emitted = txt
                        stable = 0
                        if delta:
                            yield delta
                    else:
                        stable += 1

                    if not stopping and emitted and stable >= 4:
                        break
                    await asyncio.sleep(0.5)

                self.last_text = emitted
                if not emitted:
                    raise TimeoutError(f"[{self.name}] Gemini 응답 시간 초과({timeout_sec:.0f}초)")

                if len(emitted) < 300 and _RE_QUOTA.search(emitted):
                    wait = parse_reset_wait_sec(emitted)
                    self.mark_quota(emitted, reset_wait_sec=wait)
                    raise GeminiQuotaError(f"[{self.name}] 한도 도달: {emitted[:200]}", reset_wait_sec=wait)

                self._set_login_state("ok")
            except (GeminiLoginRequired, GeminiQuotaError):
                await self._teardown_browser(force=True)
                raise
            except Exception as exc:
                if self.mode == "hot" and not isinstance(exc, GeminiPolicyRefusalError):
                    self._needs_tab_recycle = True
                raise
            finally:
                if self.mode == "cold":
                    await self._teardown_browser()
                elif self.is_alive():
                    self._tab_request_count += 1
                    self._schedule_standby()

    async def generate_image(
        self,
        prompt: str,
        *,
        file_paths: Optional[List[str]] = None,
        reset_chat: bool = True,
        timeout_sec: float = 240.0,
        min_bytes: int = 50_000,
    ) -> Dict[str, Any]:
        """Gemini 웹에서 이미지를 생성하고 원본 바이너리(base64)를 반환한다."""
        ref_hashes: set[str] = set()
        ref_sizes: set[Tuple[int, int]] = set()
        for fp in (file_paths or []):
            p = Path(fp)
            if not p.is_file():
                continue
            raw_ref = p.read_bytes()
            ref_hashes.add(hashlib.sha256(raw_ref).hexdigest())
            try:
                from PIL import Image
                with Image.open(io.BytesIO(raw_ref)) as im:
                    ref_sizes.add(im.size)
            except Exception:
                pass

        async with self._lock:
            self._cancel_standby()
            self.last_text = ""
            await self._respect_min_gap()
            try:
                page = await self._prepare_page(reset_chat=reset_chat)
                if file_paths:
                    await self._attach_files(page, file_paths)

                # '업로드 및 도구' -> '이미지 만들기' 클릭 및 한도(aria-disabled) 즉시 판별
                tools_btn = page.get_by_label(re.compile(r"업로드 및 도구|Upload|Tools", re.IGNORECASE)).first
                await tools_btn.click(timeout=8000)
                await asyncio.sleep(0.6)
                create_item = page.locator("span:visible, button:visible, [role=menuitem]:visible").filter(
                    has_text=re.compile(r"^\s*(이미지 만들기|Create images?)\s*$", re.IGNORECASE)
                ).first
                if await create_item.count() > 0:
                    disabled = bool(
                        await create_item.evaluate("e => !!e.closest('[aria-disabled=true], .mdc-list-item--disabled')")
                    )
                    if disabled:
                        body_txt = await page.inner_text("body", timeout=3000)
                        note_m = re.search(
                            r"(한도[^\n]{0,60}재설정[^\n]{0,20}|limit[^\n]{0,60}reset[^\n]{0,20}|오[전후]\s*\d{1,2}:\d{2}[^\n]{0,30})",
                            body_txt,
                            re.IGNORECASE,
                        )
                        msg = note_m.group(0).strip() if note_m else "이미지 만들기가 비활성(일일 한도 도달)"
                        wait = parse_reset_wait_sec(msg)
                        self.mark_quota(msg, reset_wait_sec=wait)
                        raise GeminiQuotaError(f"[{self.name}] {msg}", reset_wait_sec=wait)
                    await create_item.click(timeout=8000)
                    await asyncio.sleep(0.7)

                composer = page.locator(COMPOSER_SELECTOR).first
                await composer.click()
                await page.keyboard.insert_text(prompt)
                await asyncio.sleep(0.4)
                await page.keyboard.press("Enter")

                start = time.time()
                deadline = start + max(30.0, float(timeout_sec))
                ready = False
                last_text = ""
                while time.time() < deadline:
                    await asyncio.sleep(2.5)
                    ready = bool(
                        await page.evaluate(
                            """() => [...document.querySelectorAll('model-response img, generated-image img')]
                                .some(i => i.naturalWidth >= 400 && i.naturalHeight >= 400)"""
                        )
                    )
                    if ready:
                        break
                    last_text = str(
                        await page.evaluate(
                            "() => { const r=[...document.querySelectorAll('model-response')]; return r.length ? (r[r.length-1].innerText || '') : ''; }"
                        )
                        or ""
                    ).strip()
                    stopping = await page.locator("button[aria-label*='중지'], button[aria-label*='Stop']").count() > 0
                    if last_text and not stopping and (time.time() - start > 25):
                        if _RE_QUOTA.search(last_text):
                            wait = parse_reset_wait_sec(last_text)
                            self.mark_quota(last_text, reset_wait_sec=wait)
                            raise GeminiQuotaError(f"[{self.name}] 한도 도달: {last_text[:220]}", reset_wait_sec=wait)
                        if _RE_REFUSAL.search(last_text):
                            raise GeminiPolicyRefusalError(f"[{self.name}] 정책 거부: {last_text[:240]}")
                        if time.time() - start > 45:
                            raise RuntimeError(f"[{self.name}] 이미지 없이 텍스트 응답만 수신됨: {last_text[:200]}")

                if not ready:
                    raise TimeoutError(f"[{self.name}] Gemini 이미지 생성 시간 초과({timeout_sec:.0f}초)")

                raw_candidates: List[Tuple[bytes, str]] = []
                # 1차: 원본 고화질 다운로드 버튼 클릭
                dl_btn = page.locator("button[aria-label*='다운로드'], button[aria-label*='Download']").last
                if await dl_btn.count() > 0:
                    try:
                        with tempfile.TemporaryDirectory(prefix="amv_gn_dl_") as td:
                            out_tmp = Path(td) / "gen.png"
                            async with page.expect_download(timeout=30000) as dl_info:
                                await dl_btn.click()
                            dl = await dl_info.value
                            await dl.save_as(str(out_tmp))
                            if out_tmp.is_file() and out_tmp.stat().st_size > 0:
                                raw_candidates.append((out_tmp.read_bytes(), "image/png"))
                    except Exception as exc:
                        print(f"[!] [{self.name}] 다운로드 버튼 추출 실패, DOM fetch 폴백 시도: {exc}")

                # 2차 폴백: DOM 이미지 직접 fetch
                if not raw_candidates:
                    fetched = await page.evaluate(
                        """async () => {
                            const imgs = [...document.querySelectorAll('model-response img, generated-image img')]
                                .filter(i => i.naturalWidth >= 400 && i.naturalHeight >= 400);
                            const out = [];
                            for (const img of imgs) {
                                try {
                                    const r = await fetch(img.src);
                                    const ct = (r.headers.get('content-type') || 'image/png').split(';')[0];
                                    const buf = new Uint8Array(await r.arrayBuffer());
                                    let bin = '';
                                    for (let i = 0; i < buf.length; i += 8192) {
                                        bin += String.fromCharCode(...buf.subarray(i, i + 8192));
                                    }
                                    out.push({ b64: btoa(bin), mime: ct });
                                } catch (e) {}
                            }
                            return out;
                        }"""
                    )
                    for item in fetched or []:
                        b64_val = item.get("b64") or ""
                        if b64_val:
                            raw_candidates.append((base64.b64decode(b64_val), item.get("mime") or "image/png"))

                valid_images: List[Dict[str, Any]] = []
                for raw, mime in raw_candidates:
                    if len(raw) < min_bytes:
                        continue
                    sha = hashlib.sha256(raw).hexdigest()
                    if sha in ref_hashes:
                        continue
                    if ref_sizes:
                        try:
                            from PIL import Image
                            with Image.open(io.BytesIO(raw)) as im:
                                if im.size in ref_sizes:
                                    continue
                        except Exception:
                            pass
                    valid_images.append({
                        "b64_json": base64.b64encode(raw).decode("ascii"),
                        "mime_type": mime,
                        "sha256": sha,
                        "bytes_len": len(raw),
                    })

                if not valid_images:
                    raise RuntimeError(f"[{self.name}] 유효한 생성 이미지를 추출하지 못했습니다(후보 {len(raw_candidates)}건).")

                self.last_text = last_text
                self._set_login_state("ok")
                return {
                    "images": valid_images,
                    "text": last_text,
                    "worker_port": self.port,
                    "account_name": self.name,
                }
            except (GeminiLoginRequired, GeminiQuotaError):
                await self._teardown_browser(force=True)
                raise
            except Exception as exc:
                if self.mode == "hot" and not isinstance(exc, GeminiPolicyRefusalError):
                    self._needs_tab_recycle = True
                raise
            finally:
                if self.mode == "cold":
                    await self._teardown_browser()
                elif self.is_alive():
                    self._tab_request_count += 1
                    self._schedule_standby()

    def brief(self) -> Dict[str, Any]:
        # 호출 시점에 만료된 쿨다운 자동 해제 체크
        self.is_cooling_down()
        q = self.quota_snapshot()
        return {
            "port": self.port,
            "name": self.name,
            "profile_dir": self.profile_dir,
            "browser": self.preferred_browser or "edge",
            "email": self.configured_email,
            "alive": self.is_alive(),
            "busy": self.is_busy(),
            "verifying": self.verifying,
            "login_state": self.login_state,
            "login_identity": self.login_identity,
            "verified_at": self.verified_at,
            "verify_detail": self.verify_detail,
            "mode": self.mode,
            "tab_requests": self._tab_request_count,
            **q,
        }


class GeminiPool:
    """무제한 Google 계정 로드밸런서 + 한도 도달 시 자동 다음 계정 전환 오케스트레이터."""

    def __init__(self, mode: Optional[str] = None, standby_timeout_sec: Optional[int] = None):
        self.settings = load_settings()
        if mode in ("cold", "hot"):
            self.settings["mode"] = mode
        if standby_timeout_sec is not None:
            self.settings["standby_timeout_sec"] = max(10, int(standby_timeout_sec))

        self.mode = self.settings.get("mode", "hot")
        self.standby_timeout_sec = int(self.settings.get("standby_timeout_sec", 300))
        self.max_active_browsers = int(self.settings.get("max_active_browsers", 1))

        accounts = _load_accounts()
        self.workers: List[GeminiWorker] = [self._make_worker(acc) for acc in accounts]
        self._accounts_mtime: float = os.path.getmtime(CONFIG_PATH) if os.path.isfile(CONFIG_PATH) else 0.0
        self._rr_cursor: int = 0
        self._queue_lock = asyncio.Lock()
        self.queued_count: int = 0
        self.active_count: int = 0
        self.current_task: Optional[str] = None
        self.task_start_time: Optional[float] = None
        self._task_queue: List[Dict[str, Any]] = []

    def _make_worker(self, acc: Dict[str, Any]) -> GeminiWorker:
        return GeminiWorker(
            port=int(acc["port"]),
            name=str(acc["name"]),
            profile_dir=str(acc["profile_dir"]),
            mode=self.mode,
            standby_timeout_sec=self.standby_timeout_sec,
            browser=acc.get("browser"),
            email=acc.get("email"),
            quota_cooldown_sec=int(self.settings.get("quota_cooldown_sec", 21600)),
            login_cooldown_sec=int(self.settings.get("login_cooldown_sec", 900)),
            min_gap_sec=int(self.settings.get("min_gap_sec", 10)),
        )

    def _serialize_workers_config(self) -> List[Dict[str, Any]]:
        return [
            {
                "port": w.port,
                "name": w.name,
                "profile_dir": w.profile_dir,
                "browser": w.preferred_browser or "edge",
                "email": w.configured_email,
            }
            for w in self.workers
        ]

    def _push(self, event: Dict[str, Any]):
        event_bus.publish(event)

    def update_settings(self, new_data: Dict[str, Any]) -> Dict[str, Any]:
        persisted = save_settings(new_data)
        self.settings = persisted
        self.mode = persisted.get("mode", "hot")
        self.standby_timeout_sec = int(persisted.get("standby_timeout_sec", 300))
        self.max_active_browsers = int(persisted.get("max_active_browsers", 1))
        for w in self.workers:
            w.apply_settings(persisted)
        self._push({"type": "settings_updated", "settings": persisted, "workers": self.list_accounts()})
        return persisted

    def list_accounts(self) -> List[Dict[str, Any]]:
        if len(self.workers) <= 1:
            return [w.brief() for w in self.workers]
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(self.workers))) as pool:
            return list(pool.map(lambda w: w.brief(), self.workers))

    def get_worker(self, port: int) -> Optional[GeminiWorker]:
        return next((w for w in self.workers if w.port == int(port)), None)

    def add_account(
        self,
        *,
        name: str,
        port: Optional[int] = None,
        profile_dir: Optional[str] = None,
        browser: Optional[str] = "edge",
        email: Optional[str] = "",
    ) -> Dict[str, Any]:
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("계정 이름이 비어 있습니다.")
        used_ports = {w.port for w in self.workers}
        chosen_port = int(port) if port is not None else _pick_next_port(used_ports)
        if chosen_port in used_ports:
            raise ValueError(f"포트 {chosen_port}은 이미 다른 계정에서 사용 중입니다.")
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", clean_name).strip("_") or f"acct_{chosen_port}"
        chosen_prof = (profile_dir or "").strip() or os.path.join(str(Path.home()), "Desktop", f"gemini_profile_{slug}")
        os.makedirs(chosen_prof, exist_ok=True)

        acc = {
            "port": chosen_port,
            "name": clean_name,
            "profile_dir": chosen_prof,
            "browser": (browser or "edge").strip().lower(),
            "email": (email or "").strip(),
        }
        worker = self._make_worker(acc)
        self.workers.append(worker)
        self._accounts_mtime = _save_accounts(self._serialize_workers_config())
        self._push({"type": "accounts", "event": "added", "workers": self.list_accounts()})
        return worker.brief()

    async def remove_account(self, port: int) -> Dict[str, Any]:
        worker = self.get_worker(port)
        if worker is None:
            raise KeyError(f"포트 {port} 계정을 찾을 수 없습니다.")
        if worker.is_busy():
            raise RuntimeError(f"계정 '{worker.name}'이 현재 작업 중이라 삭제할 수 없습니다.")
        await worker.close(force=False)
        self.workers = [w for w in self.workers if w.port != int(port)]
        self._accounts_mtime = _save_accounts(self._serialize_workers_config())
        self._push({"type": "accounts", "event": "removed", "workers": self.list_accounts()})
        return {"ok": True, "removed_port": int(port)}

    async def open_login(self, port: int) -> bool:
        worker = self.get_worker(port)
        if worker is None:
            raise KeyError(f"포트 {port} 계정을 찾을 수 없습니다.")
        ok = await worker.open_login()
        self._push({"type": "accounts", "event": "login_done", "port": int(port), "ok": ok, "workers": self.list_accounts()})
        return ok

    async def verify_account(self, port: int) -> Dict[str, Any]:
        worker = self.get_worker(port)
        if worker is None:
            raise KeyError(f"포트 {port} 계정을 찾을 수 없습니다.")
        res = await worker.verify_login()
        self._push({"type": "accounts", "event": "verified", "port": int(port), "workers": self.list_accounts()})
        return res

    async def verify_all(self) -> List[Dict[str, Any]]:
        """한 번에 1개씩 순차 점검하여 RAM 16GB 폭주를 방지한다."""
        results = []
        for w in list(self.workers):
            if w.is_busy():
                results.append(w.brief())
                continue
            results.append(await self.verify_account(w.port))
        return results

    def clear_cooldown(self, port: int) -> Dict[str, Any]:
        worker = self.get_worker(port)
        if worker is None:
            raise KeyError(f"포트 {port} 계정을 찾을 수 없습니다.")
        worker.clear_cooldown(auto_expired=False)
        self._push({"type": "accounts", "event": "cooldown_cleared", "port": int(port), "workers": self.list_accounts()})
        return worker.brief()

    async def close_account(self, port: int) -> Dict[str, Any]:
        worker = self.get_worker(port)
        if worker is None:
            raise KeyError(f"포트 {port} 계정을 찾을 수 없습니다.")
        await worker.close(force=True)
        self._push({"type": "accounts", "event": "closed", "port": int(port), "workers": self.list_accounts()})
        return worker.brief()

    async def purge_all(self) -> int:
        closed = 0
        for w in list(self.workers):
            if not w.is_busy():
                await w.close(force=True)
                closed += 1
        self._push({"type": "accounts", "event": "purged", "workers": self.list_accounts()})
        return closed

    async def _enforce_max_active_browsers(self, keep_port: int) -> None:
        """활성 브라우저 수를 max_active_browsers(기본 1) 이내로 유지해 RAM을 보호한다."""
        limit = max(1, int(self.max_active_browsers))
        alive_others = [w for w in self.workers if w.port != keep_port and not w.is_busy() and w.is_alive()]
        while len(alive_others) >= limit:
            victim = alive_others.pop(0)
            print(f"[*] [RAM Guard] 계정 전환 -> 유휴 브라우저 '{victim.name}(:{victim.port})' 정리")
            await victim.close(force=True)

    def _ordered_candidates(self) -> Tuple[List[GeminiWorker], Optional[GeminiWorker]]:
        """가용(쿨다운 아님) 워커를 우선순위 순으로 정렬한다.
        모두 쿨다운 중이면 가장 빨리 풀리는 워커를 두 번째 값으로 반환한다.
        """
        now = time.time()
        avail: List[GeminiWorker] = []
        cooling: List[GeminiWorker] = []
        for w in self.workers:
            if w.is_cooling_down(now):
                cooling.append(w)
            else:
                avail.append(w)

        if not avail:
            earliest = min(cooling, key=lambda x: x.cooldown_until) if cooling else None
            return [], earliest

        # 라운드 로빈 회전 후, 현재 이미 살아있고 한도 여유 있는 워커를 1순위로 유지(불필요한 브라우저 재기동 최소화)
        n = len(avail)
        shift = self._rr_cursor % n
        rotated = avail[shift:] + avail[:shift]
        rotated.sort(key=lambda w: (0 if (w.is_alive() and not w.is_busy()) else 1, 0 if w.login_state == "ok" else 1))
        return rotated, None

    def _raise_all_exhausted(self, earliest: Optional[GeminiWorker], errors: List[str]) -> None:
        if earliest is not None:
            rem = earliest.cooldown_remaining_sec()
            until_str = datetime.fromtimestamp(earliest.cooldown_until).strftime("%H:%M:%S")
            raise GeminiAllQuotaExhausted(
                f"모든 Gemini 계정이 한도/쿨다운 상태입니다. 가장 빠른 복귀: [{earliest.name}] 남은 시간 {format_hms(rem)} (복귀 {until_str}). 상세: {' | '.join(errors[-3:])}",
                reset_wait_sec=rem,
            )
        raise GeminiAllQuotaExhausted(f"사용 가능한 Gemini 계정이 없습니다. 상세: {' | '.join(errors[-3:])}")

    async def generate_image(
        self,
        prompt: str,
        *,
        file_paths: Optional[List[str]] = None,
        reset_chat: bool = True,
        timeout_sec: float = 240.0,
        min_bytes: int = 50_000,
        source: str = "api",
        client_app: str = "",
        ip_addr: str = "",
    ) -> Dict[str, Any]:
        """가용 계정을 순회하며 이미지를 생성한다. 한도 도달 시 자동으로 다음 계정으로 전환한다."""
        t0 = time.time()
        errors: List[str] = []
        candidates, earliest = self._ordered_candidates()
        if not candidates:
            self._raise_all_exhausted(earliest, errors)

        self.active_count += 1
        self.current_task = f"[Image] {prompt[:60]}"
        self.task_start_time = t0
        self._push({"type": "task_start", "task": self.current_task, "source": source})
        try:
            for worker in candidates:
                if worker.is_cooling_down():
                    continue
                await self._enforce_max_active_browsers(worker.port)
                w_t0 = time.time()
                try:
                    res = await worker.generate_image(
                        prompt,
                        file_paths=file_paths,
                        reset_chat=reset_chat,
                        timeout_sec=timeout_sec,
                        min_bytes=min_bytes,
                    )
                    dur_ms = (time.time() - w_t0) * 1000.0
                    total_bytes = sum(int(im.get("bytes_len", 0)) for im in res.get("images", []))
                    telemetry_db.record_query(
                        prompt=prompt,
                        duration_ms=dur_ms,
                        bytes_len=total_bytes,
                        worker_port=worker.port,
                        account_label=worker.name,
                        model="gemini-web-image",
                        source=source,
                        success=True,
                        client_app=client_app,
                        prompt_bytes=len(prompt.encode("utf-8")),
                        ip_addr=ip_addr,
                        full_response=f"[Generated {len(res.get('images', []))} image(s), {total_bytes} bytes]",
                    )
                    self._push({"type": "task_done", "worker": worker.name, "port": worker.port, "ok": True})
                    return res
                except GeminiPolicyRefusalError:
                    # 정책 거부는 다른 계정으로 돌려도 동일하므로 즉시 반환
                    telemetry_db.record_query(
                        prompt=prompt,
                        duration_ms=(time.time() - w_t0) * 1000.0,
                        bytes_len=0,
                        worker_port=worker.port,
                        account_label=worker.name,
                        model="gemini-web-image",
                        source=source,
                        success=False,
                        client_app=client_app,
                        ip_addr=ip_addr,
                        full_response="Policy Refusal",
                    )
                    raise
                except (GeminiQuotaError, GeminiLoginRequired) as exc:
                    errors.append(f"[{worker.name}] {exc}")
                    self._rr_cursor += 1
                    telemetry_db.record_query(
                        prompt=prompt,
                        duration_ms=(time.time() - w_t0) * 1000.0,
                        bytes_len=0,
                        worker_port=worker.port,
                        account_label=worker.name,
                        model="gemini-web-image",
                        source=source,
                        success=False,
                        client_app=client_app,
                        ip_addr=ip_addr,
                        full_response=str(exc)[:500],
                    )
                    print(f"[*] [Failover] [{worker.name}] 불가({type(exc).__name__}) -> 다음 계정으로 자동 전환합니다.")
                    continue
                except Exception as exc:
                    errors.append(f"[{worker.name}] {type(exc).__name__}: {exc}")
                    self._rr_cursor += 1
                    telemetry_db.record_query(
                        prompt=prompt,
                        duration_ms=(time.time() - w_t0) * 1000.0,
                        bytes_len=0,
                        worker_port=worker.port,
                        account_label=worker.name,
                        model="gemini-web-image",
                        source=source,
                        success=False,
                        client_app=client_app,
                        ip_addr=ip_addr,
                        full_response=str(exc)[:500],
                    )
                    continue

            # 모든 후보 실패
            _, next_earliest = self._ordered_candidates()
            self._raise_all_exhausted(next_earliest, errors)
            raise RuntimeError(" | ".join(errors))
        finally:
            self.active_count = max(0, self.active_count - 1)
            if self.active_count == 0:
                self.current_task = None
                self.task_start_time = None

    async def stream_query(
        self,
        prompt: str,
        *,
        reset_chat: bool = True,
        file_paths: Optional[List[str]] = None,
        source: str = "api",
        client_app: str = "",
        ip_addr: str = "",
        model: str = "gemini-web",
    ) -> AsyncGenerator[str, None]:
        """가용 계정을 순회하며 텍스트 응답을 스트리밍한다. 한도 도달 시 다음 계정으로 자동 전환한다."""
        errors: List[str] = []
        candidates, earliest = self._ordered_candidates()
        if not candidates:
            self._raise_all_exhausted(earliest, errors)

        self.active_count += 1
        self.current_task = prompt[:60]
        self.task_start_time = time.time()
        self._push({"type": "task_start", "task": self.current_task, "source": source})
        try:
            for worker in candidates:
                if worker.is_cooling_down():
                    continue
                await self._enforce_max_active_browsers(worker.port)
                w_t0 = time.time()
                collected: List[str] = []
                try:
                    async for delta in worker.stream_query(prompt, reset_chat=reset_chat, file_paths=file_paths):
                        collected.append(delta)
                        yield delta
                    full_text = "".join(collected)
                    telemetry_db.record_query(
                        prompt=prompt,
                        duration_ms=(time.time() - w_t0) * 1000.0,
                        bytes_len=len(full_text.encode("utf-8")),
                        worker_port=worker.port,
                        account_label=worker.name,
                        model=model,
                        source=source,
                        success=True,
                        client_app=client_app,
                        prompt_bytes=len(prompt.encode("utf-8")),
                        ip_addr=ip_addr,
                        full_response=full_text,
                    )
                    self._push({"type": "task_done", "worker": worker.name, "port": worker.port, "ok": True})
                    return
                except (GeminiQuotaError, GeminiLoginRequired) as exc:
                    errors.append(f"[{worker.name}] {exc}")
                    self._rr_cursor += 1
                    telemetry_db.record_query(
                        prompt=prompt,
                        duration_ms=(time.time() - w_t0) * 1000.0,
                        bytes_len=0,
                        worker_port=worker.port,
                        account_label=worker.name,
                        model=model,
                        source=source,
                        success=False,
                        client_app=client_app,
                        ip_addr=ip_addr,
                        full_response=str(exc)[:500],
                    )
                    if collected:
                        raise  # 이미 일부 스트리밍을 클라이언트로 보낸 뒤라면 중복 출력 방지
                    continue
                except Exception as exc:
                    errors.append(f"[{worker.name}] {type(exc).__name__}: {exc}")
                    self._rr_cursor += 1
                    if collected:
                        raise
                    continue

            _, next_earliest = self._ordered_candidates()
            self._raise_all_exhausted(next_earliest, errors)
        finally:
            self.active_count = max(0, self.active_count - 1)
            if self.active_count == 0:
                self.current_task = None
                self.task_start_time = None

    async def query(
        self,
        prompt: str,
        *,
        file_paths: Optional[List[str]] = None,
        source: str = "api",
        client_app: str = "",
        model: str = "gemini-web",
    ) -> str:
        parts: List[str] = []
        async for chunk in self.stream_query(prompt, file_paths=file_paths, source=source, client_app=client_app, model=model):
            parts.append(chunk)
        return "".join(parts).strip()

    def get_status_summary(self) -> str:
        lines = []
        for b in self.list_accounts():
            if b["cooling_down"]:
                st = f"한도 쉬는 중 (남은 {b['cooldown_remaining_hms']} / 복귀 {b['cooldown_until_str']})"
            elif b["busy"]:
                st = "작업 중"
            elif b["login_state"] == "ok":
                st = "연결됨" + (" [활성]" if b["alive"] else " [대기]")
            elif b["login_state"] == "required":
                st = "로그인 필요"
            else:
                st = "확인 전"
            lines.append(f"{b['name']}(:{b['port']}): {st}")
        return " | ".join(lines) if lines else "등록된 계정 없음"


GeminiEngine = GeminiPool
