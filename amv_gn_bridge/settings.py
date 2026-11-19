# Copyright (c) 2026 Eunho Kim / AMEVA Foundation. All rights reserved.
# SPDX-License-Identifier: Apache-2.0 OR Commercial-License
"""
Persistent configuration management for amv-gn-bridge.
Manages bridge operation mode ('hot' vs 'cold'), standby timeout, quota cooldown, and persistence to ~/.amv_gn_bridge/settings.json.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict

CONFIG_DIR = os.path.join(str(Path.home()), ".amv_gn_bridge")
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")

DEFAULT_SETTINGS: Dict[str, Any] = {
    "mode": "hot",                   # "cold" (단발성 즉시 종료) | "hot" (웜 스탠바이 유지 + 탭 리사이클링)
    "standby_timeout_sec": 300,      # 핫로딩 모드 대기 만료 시간(초) — 기본 300초 (5분)
    "rate_limit_rpm": 30,            # IP당 분당 요청 제한
    "enable_deepthink": False,       # Thinking 모드 기본 활성화 여부
    "quota_cooldown_sec": 21600,     # 한도 파싱 불가 시 기본 쿨다운(초) — 기본 6시간 (21600초)
    "login_cooldown_sec": 900,       # 로그인 풀림 시 기본 쿨다운(초) — 기본 15분 (900초)
    "min_gap_sec": 10,               # 동일 계정 연속 요청 최소 간격(초)
    "max_active_browsers": 1,        # 동시 유지 브라우저 최대 개수 (16GB RAM 보호)
}


def load_settings() -> Dict[str, Any]:
    """Load settings from ~/.amv_gn_bridge/settings.json, with fallback to DEFAULT_SETTINGS."""
    settings = dict(DEFAULT_SETTINGS)
    if os.path.isfile(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                mode = str(loaded.get("mode", DEFAULT_SETTINGS["mode"])).lower().strip()
                if mode in ("cold", "hot"):
                    settings["mode"] = mode
                for key, lo, hi in (
                    ("standby_timeout_sec", 10, 86400),
                    ("rate_limit_rpm", 1, 1000),
                    ("quota_cooldown_sec", 60, 86400 * 7),
                    ("login_cooldown_sec", 30, 86400),
                    ("min_gap_sec", 0, 300),
                    ("max_active_browsers", 1, 10),
                ):
                    if key in loaded:
                        try:
                            val = int(loaded[key])
                            settings[key] = max(lo, min(hi, val))
                        except (ValueError, TypeError):
                            pass
                if "enable_deepthink" in loaded:
                    settings["enable_deepthink"] = bool(loaded["enable_deepthink"])
                return settings
        except Exception as exc:
            print(f"[!] settings.json 읽기 실패, 기본값을 사용합니다: {exc}")

    save_settings(settings)
    return settings


def save_settings(new_data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and persist settings to ~/.amv_gn_bridge/settings.json."""
    current = dict(DEFAULT_SETTINGS)
    if os.path.isfile(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                current.update(loaded)
        except Exception:
            pass

    if "mode" in new_data:
        mode = str(new_data.get("mode", "hot")).lower().strip()
        current["mode"] = mode if mode in ("cold", "hot") else "hot"

    for key, lo, hi in (
        ("standby_timeout_sec", 10, 86400),
        ("rate_limit_rpm", 1, 1000),
        ("quota_cooldown_sec", 60, 86400 * 7),
        ("login_cooldown_sec", 30, 86400),
        ("min_gap_sec", 0, 300),
        ("max_active_browsers", 1, 10),
    ):
        if key in new_data:
            try:
                val = int(new_data[key])
                current[key] = max(lo, min(hi, val))
            except (ValueError, TypeError):
                pass

    if "enable_deepthink" in new_data:
        current["enable_deepthink"] = bool(new_data["enable_deepthink"])

    os.makedirs(CONFIG_DIR, exist_ok=True)
    temp_path = SETTINGS_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, SETTINGS_PATH)
    return current
