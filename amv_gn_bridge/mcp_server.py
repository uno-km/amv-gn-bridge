# Copyright (c) 2026 Eunho Kim / AMEVA Foundation. All rights reserved.
# SPDX-License-Identifier: Apache-2.0 OR Commercial-License
"""
amv-gn-bridge FastMCP Server.
Connects Antigravity / Claude / Cursor directly to the multi-account Google Gemini Web pool.
Routes through the running :11501 gateway when active (so dashboard/telemetry stay synced),
and falls back to direct local GeminiEngine execution otherwise.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import List, Optional

from mcp.server.fastmcp import FastMCP

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if package_root not in sys.path:
    sys.path.insert(0, package_root)

try:
    from amv_gn_bridge.engine import GeminiEngine
except (ImportError, ValueError):
    from engine import GeminiEngine

mcp = FastMCP("gemini-bridge")
engine = GeminiEngine()


def _find_gateway_url() -> Optional[str]:
    """활성화된 amv-gn-bridge 게이트웨이 탐색 (11501 최우선)."""
    for port in (11501, 11502, 11554):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            with urllib.request.urlopen(req, timeout=1.2) as r:
                if r.status == 200:
                    data = json.loads(r.read().decode("utf-8", errors="replace"))
                    if data.get("gateway") == "amv-gn-bridge":
                        return f"http://127.0.0.1:{port}"
        except Exception:
            pass
    return None


@mcp.tool()
async def ask_gemini(prompt: str, mode: str = "auto") -> str:
    """Google Gemini 웹 멀티계정 풀에 질문하고 답변을 받습니다.
    계정이 한도에 도달하면 자동으로 다음 가용 계정으로 전환합니다.

    Args:
        prompt: Gemini에게 전달할 질문 또는 작업 프롬프트
        mode: 'auto'(기본) 또는 'thinking'(단계별 심층 추론 요청)
    """
    model = "gemini-web-thinking" if mode == "thinking" else "gemini-web"
    full_prompt = ("[단계별로 치밀하게 분석하여 근거와 함께 답변해줘]\n\n" + prompt) if mode == "thinking" else prompt

    gw_url = _find_gateway_url()
    if gw_url:
        try:
            body = json.dumps({
                "model": model,
                "messages": [{"role": "user", "content": full_prompt}],
                "stream": False,
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{gw_url}/v1/chat/completions",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Bridge-Source": "mcp:gemini-bridge",
                    "X-Client-App": "mcp",
                },
                method="POST",
            )
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                None, lambda: json.loads(urllib.request.urlopen(req, timeout=240).read().decode("utf-8"))
            )
            return str(resp["choices"][0]["message"]["content"])
        except Exception as exc:
            print(f"[!] Gateway call failed, falling back to direct engine: {exc}", file=sys.stderr)

    try:
        return await engine.query(full_prompt, source="mcp:gemini-bridge", client_app="mcp", model=model)
    except Exception as exc:
        return f"[오류 발생] Gemini 질의 실패: {exc}"


@mcp.tool()
async def generate_gemini_image(
    prompt: str,
    output_path: str,
    reference_images: Optional[List[str]] = None,
) -> str:
    """Google Gemini 웹 멀티계정 풀로 이미지를 생성해 output_path에 저장합니다.
    계정이 일일 이미지 한도에 도달하면 자동으로 다음 가용 계정으로 전환합니다.

    Args:
        prompt: 이미지 생성 프롬프트
        output_path: 생성된 이미지를 저장할 절대 파일 경로 (.png)
        reference_images: (선택) 캐릭터/배경 일관성을 위한 참조 이미지 로컬 파일 경로 목록
    """
    refs = [p for p in (reference_images or []) if p and os.path.isfile(p)]
    gw_url = _find_gateway_url()
    if gw_url:
        try:
            encoded_refs = []
            for rp in refs:
                b64 = base64.b64encode(Path(rp).read_bytes()).decode("ascii")
                ext = Path(rp).suffix.lower().lstrip(".") or "png"
                mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
                encoded_refs.append(f"data:{mime};base64,{b64}")
            body = json.dumps({
                "prompt": prompt,
                "model": "gemini-web-image",
                "reference_images": encoded_refs,
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{gw_url}/v1/images/generations",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Bridge-Source": "mcp:gemini-bridge",
                    "X-Client-App": "mcp",
                },
                method="POST",
            )
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                None, lambda: json.loads(urllib.request.urlopen(req, timeout=300).read().decode("utf-8"))
            )
            items = resp.get("data") or []
            if not items:
                return f"[오류] 이미지 응답이 비어 있습니다: {resp}"
            raw = base64.b64decode(items[0]["b64_json"])
            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(raw)
            return f"생성 완료: {out} ({len(raw)} bytes, 계정={resp.get('account_name')}:{resp.get('worker_port')})"
        except Exception as exc:
            print(f"[!] Gateway image call failed, falling back to direct engine: {exc}", file=sys.stderr)

    try:
        res = await engine.generate_image(prompt, file_paths=refs, source="mcp:gemini-bridge", client_app="mcp")
        items = res.get("images") or []
        raw = base64.b64decode(items[0]["b64_json"])
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(raw)
        return f"생성 완료: {out} ({len(raw)} bytes, 계정={res.get('account_name')}:{res.get('worker_port')})"
    except Exception as exc:
        return f"[오류 발생] Gemini 이미지 생성 실패: {exc}"


@mcp.tool()
async def get_gemini_status() -> str:
    """등록된 모든 Google Gemini 계정의 상태와 한도별 남은 시:분:초(HH:MM:SS) 카운트다운을 조회합니다."""
    gw_url = _find_gateway_url()
    if gw_url:
        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(
                None, lambda: json.loads(urllib.request.urlopen(f"{gw_url}/api/accounts", timeout=5).read().decode("utf-8"))
            )
            lines = []
            for a in data.get("accounts", []):
                if a.get("cooling_down"):
                    lines.append(f"{a['name']}(:{a['port']}): 한도 쉬는 중 (남은 {a['cooldown_remaining_hms']} / 복귀 {a['cooldown_until_str']})")
                else:
                    lines.append(f"{a['name']}(:{a['port']}): {a.get('login_state', 'unknown')}")
            return "[상태 점검] " + (" | ".join(lines) or "등록된 계정 없음")
        except Exception:
            pass
    return f"[상태 점검] {engine.get_status_summary()}"


def run_server():
    import datetime
    now = datetime.datetime.now().strftime("%H:%M:%S")
    for line in (
        "",
        "┌──────────────────────────────────────────┐",
        "│   AMV-GN-BRIDGE  MCP Server  ✓ 가동 중   │",
        f"│   시작 시각: {now}                     │",
        "│   Gemini Web Multi-Account · FastMCP     │",
        "└──────────────────────────────────────────┘",
        "",
    ):
        print(line, file=sys.stderr, flush=True)
    mcp.run()


if __name__ == "__main__":
    run_server()
