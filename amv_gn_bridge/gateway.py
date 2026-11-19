# Copyright (c) 2026 Eunho Kim / AMEVA Foundation. All rights reserved.
# SPDX-License-Identifier: Apache-2.0 OR Commercial-License
"""
amv-gn-bridge OpenAI-Compatible REST API Gateway & Real-time Web Dashboard.
Default port: 11501 (coexists cleanly with amv-cp-bridge on 11500).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

try:
    from ._version import __version__
    from .engine import (
        GeminiAllQuotaExhausted,
        GeminiEngine,
        GeminiLoginRequired,
        GeminiPolicyRefusalError,
        GeminiQuotaError,
        event_bus,
    )
    from .telemetry_db import telemetry_db
    from . import updater
except (ImportError, ValueError):
    from amv_gn_bridge._version import __version__
    from amv_gn_bridge.engine import (
        GeminiAllQuotaExhausted,
        GeminiEngine,
        GeminiLoginRequired,
        GeminiPolicyRefusalError,
        GeminiQuotaError,
        event_bus,
    )
    from amv_gn_bridge.telemetry_db import telemetry_db
    from amv_gn_bridge import updater

app = FastAPI(title="amv-gn-bridge Gateway", version=__version__)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

engine = GeminiEngine()


class ChatMessage(BaseModel):
    role: str = "user"
    content: Any = ""


class ChatCompletionRequest(BaseModel):
    model: str = "gemini-web"
    messages: List[ChatMessage] = Field(default_factory=list)
    stream: bool = False
    reset_chat: bool = True
    reference_images: List[str] = Field(default_factory=list)


class ImageGenerationRequest(BaseModel):
    prompt: str
    model: str = "gemini-web-image"
    n: int = 1
    response_format: str = "b64_json"
    reference_images: List[str] = Field(default_factory=list)
    timeout_sec: float = 240.0
    min_bytes: int = 50_000
    reset_chat: bool = True


class AccountCreateRequest(BaseModel):
    name: str
    port: Optional[int] = None
    profile_dir: Optional[str] = None
    browser: Optional[str] = "edge"
    email: Optional[str] = ""


class SettingsUpdateRequest(BaseModel):
    mode: Optional[str] = None
    standby_timeout_sec: Optional[int] = None
    rate_limit_rpm: Optional[int] = None
    enable_deepthink: Optional[bool] = None
    quota_cooldown_sec: Optional[int] = None
    login_cooldown_sec: Optional[int] = None
    min_gap_sec: Optional[int] = None
    max_active_browsers: Optional[int] = None


def _extract_prompt_from_messages(messages: List[ChatMessage]) -> Tuple_prompt_and_imgs:
    parts: List[str] = []
    imgs: List[str] = []
    for m in messages:
        content = m.content
        if isinstance(content, str):
            txt = content.strip()
            if txt:
                parts.append(f"[{m.role}] {txt}" if len(messages) > 1 and m.role != "user" else txt)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and block.get("text"):
                    parts.append(str(block["text"]).strip())
                elif btype == "image_url":
                    url = (block.get("image_url") or {}).get("url") if isinstance(block.get("image_url"), dict) else block.get("image_url")
                    if isinstance(url, str) and url:
                        imgs.append(url)
    return "\n\n".join(parts).strip(), imgs


Tuple_prompt_and_imgs = tuple


def _materialize_reference_images(refs: List[str], temp_dir: Path) -> List[str]:
    out: List[str] = []
    for idx, item in enumerate(refs or []):
        if not item:
            continue
        s = str(item).strip()
        if s.startswith("data:image/"):
            header, _, b64 = s.partition(",")
            ext = ".png"
            if "jpeg" in header or "jpg" in header:
                ext = ".jpg"
            elif "webp" in header:
                ext = ".webp"
            target = temp_dir / f"ref_{idx}{ext}"
            target.write_bytes(base64.b64decode(b64))
            out.append(str(target))
        elif os.path.isfile(s):
            out.append(s)
    return out


def _client_meta(request: Request) -> Dict[str, str]:
    return {
        "source": request.headers.get("X-Bridge-Source") or "api",
        "client_app": request.headers.get("X-Client-App") or "",
        "ip_addr": request.client.host if request.client else "",
    }


@app.get("/")
async def root():
    return RedirectResponse(url="/dashboard")


@app.get("/health")
@app.get("/status")
async def health():
    accounts = engine.list_accounts()
    avail = sum(1 for a in accounts if not a["cooling_down"] and a["login_state"] != "required")
    return {
        "ok": True,
        "gateway": "amv-gn-bridge",
        "version": __version__,
        "mode": engine.mode,
        "accounts_total": len(accounts),
        "accounts_available": avail,
        "active_tasks": engine.active_count,
    }


@app.get("/api/status")
async def api_status():
    summary = telemetry_db.get_summary()
    by_acct = summary.get("by_account", {})
    workers = []
    for w in engine.list_accounts():
        st = by_acct.get(str(w["port"]), {})
        w["today_queries"] = st.get("today_queries", 0)
        w["total_queries"] = st.get("queries", 0)
        workers.append(w)
    return {
        "gateway": "amv-gn-bridge",
        "version": __version__,
        "mode": engine.mode,
        "settings": engine.settings,
        "active_count": engine.active_count,
        "current_task": engine.current_task,
        "task_start_time": engine.task_start_time,
        "server_time": time.time(),
        "workers": workers,
        "stats": summary,
    }


@app.get("/api/stats")
async def api_stats():
    return telemetry_db.get_summary()


@app.get("/api/chart")
async def api_chart(days: int = 14):
    return {"days": telemetry_db.get_daily_chart(days=max(1, min(90, days)))}


@app.post("/api/stats/reset")
async def api_stats_reset():
    telemetry_db.reset_all()
    event_bus.publish({"type": "stats_reset"})
    return {"ok": True}


@app.get("/api/record/{record_id}")
async def api_record(record_id: int):
    rec = telemetry_db.get_record_full(record_id)
    if not rec:
        raise HTTPException(status_code=404, detail="기록을 찾을 수 없습니다.")
    return rec


@app.post("/api/purge")
async def api_purge():
    closed = await engine.purge_all()
    return {"ok": True, "closed": closed}


@app.get("/api/settings")
async def api_get_settings():
    return engine.settings


@app.post("/api/settings")
async def api_update_settings(body: SettingsUpdateRequest):
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    return engine.update_settings(patch)


@app.get("/api/update/check")
async def api_update_check():
    return await asyncio.to_thread(updater.check_for_update)


@app.post("/api/update/apply")
async def api_update_apply():
    try:
        res = await asyncio.to_thread(updater.apply_update_and_prepare_restart)
    except updater.UpdateError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    async def _deferred_exit():
        await asyncio.sleep(0.8)
        os._exit(0)

    if res.get("should_exit"):
        asyncio.create_task(_deferred_exit())
    return res


@app.get("/api/accounts")
async def api_list_accounts():
    return {"accounts": engine.list_accounts(), "server_time": time.time()}


@app.post("/api/accounts", status_code=201)
async def api_add_account(body: AccountCreateRequest):
    try:
        acc = engine.add_account(
            name=body.name,
            port=body.port,
            profile_dir=body.profile_dir,
            browser=body.browser,
            email=body.email,
        )
        return {"ok": True, "account": acc}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/accounts/{port}")
async def api_remove_account(port: int):
    try:
        return await engine.remove_account(port)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/accounts/verify")
async def api_verify_all():
    results = await engine.verify_all()
    return {"ok": True, "accounts": results}


@app.post("/api/accounts/{port}/verify")
async def api_verify_one(port: int):
    try:
        res = await engine.verify_account(port)
        return {"ok": res.get("login_state") == "ok" and not res.get("cooling_down"), "account": res}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/accounts/{port}/login", status_code=202)
async def api_login_account(port: int):
    worker = engine.get_worker(port)
    if worker is None:
        raise HTTPException(status_code=404, detail=f"등록되지 않은 포트입니다: {port}")
    if worker.is_busy():
        raise HTTPException(status_code=409, detail=f"계정 '{worker.name}'이 현재 작업 중입니다.")
    asyncio.create_task(engine.open_login(port))
    return {"ok": True, "port": port}


@app.post("/api/accounts/{port}/close")
async def api_close_account(port: int):
    try:
        res = await engine.close_account(port)
        return {"ok": True, "account": res}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/accounts/{port}/reset-cooldown")
async def api_reset_cooldown(port: int):
    try:
        res = engine.clear_cooldown(port)
        return {"ok": True, "account": res}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/v1/models")
async def list_models():
    now = int(time.time())
    models = ["gemini-web", "gemini-web-image", "gemini-web-thinking"]
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "created": now, "owned_by": "amv-gn-bridge"} for m in models],
    }


@app.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest, request: Request):
    prompt, inline_imgs = _extract_prompt_from_messages(body.messages)
    if not prompt:
        raise HTTPException(status_code=400, detail="messages에 유효한 프롬프트가 없습니다.")
    meta = _client_meta(request)
    all_refs = list(body.reference_images or []) + inline_imgs
    req_id = f"chatcmpl-{uuid.uuid4().hex[:16]}"
    created = int(time.time())

    if body.stream:
        async def _gen():
            with tempfile.TemporaryDirectory(prefix="amv_gn_ref_") as td:
                ref_paths = _materialize_reference_images(all_refs, Path(td))
                try:
                    async for delta in engine.stream_query(
                        prompt,
                        reset_chat=body.reset_chat,
                        file_paths=ref_paths,
                        source=meta["source"],
                        client_app=meta["client_app"],
                        ip_addr=meta["ip_addr"],
                        model=body.model,
                    ):
                        chunk = {
                            "id": req_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": body.model,
                            "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    done_chunk = {
                        "id": req_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": body.model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                    yield f"data: {json.dumps(done_chunk, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                except Exception as exc:
                    err_chunk = {"error": {"message": str(exc), "type": type(exc).__name__}}
                    yield f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"

        return StreamingResponse(_gen(), media_type="text/event-stream")

    with tempfile.TemporaryDirectory(prefix="amv_gn_ref_") as td:
        ref_paths = _materialize_reference_images(all_refs, Path(td))
        try:
            text = await engine.query(
                prompt,
                file_paths=ref_paths,
                source=meta["source"],
                client_app=meta["client_app"],
                model=body.model,
            )
            return {
                "id": req_id,
                "object": "chat.completion",
                "created": created,
                "model": body.model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": max(1, int(len(prompt.encode("utf-8")) / 4.2)),
                    "completion_tokens": max(1, int(len(text.encode("utf-8")) / 4.2)),
                    "total_tokens": max(2, int((len(prompt.encode("utf-8")) + len(text.encode("utf-8"))) / 4.2)),
                },
            }
        except GeminiAllQuotaExhausted as exc:
            return JSONResponse(status_code=429, content={"error": {"message": str(exc), "type": "quota_exhausted"}})
        except GeminiLoginRequired as exc:
            return JSONResponse(status_code=401, content={"error": {"message": str(exc), "type": "login_required"}})
        except GeminiPolicyRefusalError as exc:
            return JSONResponse(status_code=422, content={"error": {"message": str(exc), "type": "policy_refusal"}})
        except Exception as exc:
            return JSONResponse(status_code=500, content={"error": {"message": str(exc), "type": type(exc).__name__}})


@app.post("/v1/images/generations")
async def image_generations(body: ImageGenerationRequest, request: Request):
    meta = _client_meta(request)
    with tempfile.TemporaryDirectory(prefix="amv_gn_img_") as td:
        ref_paths = _materialize_reference_images(body.reference_images, Path(td))
        try:
            res = await engine.generate_image(
                body.prompt,
                file_paths=ref_paths,
                reset_chat=body.reset_chat,
                timeout_sec=body.timeout_sec,
                min_bytes=body.min_bytes,
                source=meta["source"],
                client_app=meta["client_app"],
                ip_addr=meta["ip_addr"],
            )
            return {
                "created": int(time.time()),
                "worker_port": res.get("worker_port"),
                "account_name": res.get("account_name"),
                "text": res.get("text", ""),
                "data": res.get("images", []),
            }
        except GeminiAllQuotaExhausted as exc:
            return JSONResponse(status_code=429, content={"error": {"message": str(exc), "type": "quota_exhausted"}})
        except GeminiLoginRequired as exc:
            return JSONResponse(status_code=401, content={"error": {"message": str(exc), "type": "login_required"}})
        except GeminiPolicyRefusalError as exc:
            return JSONResponse(status_code=422, content={"error": {"message": str(exc), "type": "policy_refusal"}})
        except Exception as exc:
            return JSONResponse(status_code=500, content={"error": {"message": str(exc), "type": type(exc).__name__}})


@app.get("/api/stream")
async def api_stream():
    q = event_bus.subscribe()

    async def _events():
        try:
            yield f"data: {json.dumps({'type': 'connected', 'gateway': 'amv-gn-bridge'})}\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=20.0)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'ping', 'ts': time.time()})}\n\n"
        finally:
            event_bus.unsubscribe(q)

    return StreamingResponse(_events(), media_type="text/event-stream")


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>amv-gn-bridge · Gemini Multi-Account Dashboard</title>
<style>
  :root {
    --bg: #f5f7fb; --card: #ffffff; --border: #e4e9f2; --text: #161c2d; --muted: #64748b;
    --pri: #4f46e5; --pri-soft: #eef2ff; --ok: #059669; --ok-bg: #ecfdf5;
    --warn: #d97706; --warn-bg: #fffbeb; --err: #dc2626; --err-bg: #fef2f2;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', -apple-system, sans-serif; background: var(--bg); color: var(--text); padding: 24px; }
  .wrap { max-width: 1180px; margin: 0 auto; }
  .top { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }
  .brand { display: flex; align-items: center; gap: 10px; }
  .brand h1 { font-size: 20px; font-weight: 700; }
  .pill { font-size: 12px; padding: 4px 10px; border-radius: 999px; background: var(--pri-soft); color: var(--pri); font-weight: 600; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; }
  button { cursor: pointer; border: 1px solid var(--border); background: #fff; color: var(--text); border-radius: 8px; padding: 7px 12px; font-size: 13px; font-weight: 600; }
  button:hover { background: #f8fafc; }
  .btn-pri { background: var(--pri); color: #fff; border-color: var(--pri); }
  .btn-pri:hover { background: #4338ca; }
  .btn-del { color: var(--err); border-color: #fecaca; background: var(--err-bg); }
  .btn-warn { color: var(--warn); border-color: #fde68a; background: var(--warn-bg); }
  .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 18px; }
  @media(max-width:860px){ .grid { grid-template-columns: repeat(2, 1fr); } }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 16px 18px; box-shadow: 0 1px 2px rgba(15,23,42,.04); }
  .kpi-lbl { font-size: 12px; color: var(--muted); margin-bottom: 6px; }
  .kpi-val { font-size: 24px; font-weight: 700; }
  .kpi-sub { font-size: 12px; color: var(--muted); margin-top: 4px; }
  .sec-hd { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; gap: 8px; flex-wrap: wrap; }
  .sec-title { font-size: 15px; font-weight: 700; }
  .acct-list { display: flex; flex-direction: column; gap: 10px; }
  .acct { display: flex; justify-content: space-between; align-items: center; gap: 12px; border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px; flex-wrap: wrap; }
  .acct-name { font-weight: 700; font-size: 14px; }
  .acct-sub { font-size: 12px; color: var(--muted); margin-top: 3px; }
  .acct-cd { font-family: 'Consolas', monospace; font-size: 13px; font-weight: 700; color: var(--warn); background: var(--warn-bg); padding: 4px 9px; border-radius: 8px; margin-top: 6px; display: inline-block; }
  .badge { font-size: 12px; font-weight: 700; padding: 4px 10px; border-radius: 999px; }
  .b-ok { background: var(--ok-bg); color: var(--ok); }
  .b-quota { background: var(--warn-bg); color: var(--warn); }
  .b-req { background: var(--err-bg); color: var(--err); }
  .b-busy { background: var(--pri-soft); color: var(--pri); }
  .b-unk { background: #f1f5f9; color: #475569; }
  .w-btns { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .btn-xs { padding: 5px 9px; font-size: 12px; border-radius: 7px; }
  .two { display: grid; grid-template-columns: 1.4fr 1fr; gap: 16px; margin-bottom: 18px; }
  @media(max-width:960px){ .two { grid-template-columns: 1fr; } }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }
  th { font-size: 12px; color: var(--muted); font-weight: 600; }
  .modal-bg { position: fixed; inset: 0; background: rgba(15,23,42,.45); display: none; align-items: center; justify-content: center; z-index: 99; }
  .modal { background: #fff; border-radius: 14px; padding: 20px; width: 420px; max-width: 92vw; }
  .field { margin-bottom: 12px; }
  .field label { display: block; font-size: 12px; font-weight: 600; color: var(--muted); margin-bottom: 4px; }
  .field input, .field select { width: 100%; padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; font-size: 13px; }
  .notice { padding: 10px 14px; border-radius: 10px; margin-bottom: 14px; font-size: 13px; display: none; }
  .notice.ok { background: var(--ok-bg); color: var(--ok); display: block; }
  .notice.warn { background: var(--warn-bg); color: var(--warn); display: block; }
  .setting-row { display: flex; align-items: center; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid var(--border); font-size: 13px; }
  .setting-row:last-child { border-bottom: none; }
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div class="brand">
      <span style="font-size:22px;">✦</span>
      <h1>amv-gn-bridge</h1>
      <span class="pill">Gemini Multi-Account · :11501</span>
      <span class="pill" id="modeBadge">HOT</span>
    </div>
    <div class="actions">
      <button onclick="checkUpdate(true)">업데이트 확인</button>
      <button onclick="verifyAll()">전체 점검</button>
      <button class="btn-pri" onclick="openAddModal()">+ 계정 추가</button>
      <button onclick="purgeBrowsers()">메모리 정리</button>
      <button class="btn-del" onclick="resetStats()">통계 초기화</button>
    </div>
  </div>

  <div id="updateBanner" style="display:none;background:#eef2ff;border:1px solid #c7d2fe;color:#3730a3;padding:12px 16px;border-radius:12px;margin-bottom:14px;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
    <div id="updateBannerText" style="font-size:13px;font-weight:700;"></div>
    <button class="btn-pri" onclick="applyUpdate()">지금 업데이트 및 자동 재시작</button>
  </div>
  <div id="notice" class="notice"></div>

  <div class="grid">
    <div class="card"><div class="kpi-lbl">오늘 요청</div><div class="kpi-val" id="kToday">0</div><div class="kpi-sub" id="kTodayTok">0 tokens</div></div>
    <div class="card"><div class="kpi-lbl">가용 계정 / 전체</div><div class="kpi-val" id="kAccts">0 / 0</div><div class="kpi-sub" id="kQuotaCnt">한도 쉬는 중 0개</div></div>
    <div class="card"><div class="kpi-lbl">누적 요청 / 성공률</div><div class="kpi-val" id="kTotal">0</div><div class="kpi-sub" id="kRate">100%</div></div>
    <div class="card"><div class="kpi-lbl">현재 작업</div><div class="kpi-val" style="font-size:16px;" id="kTask">대기 중</div><div class="kpi-sub" id="kAvg">평균 0ms</div></div>
  </div>

  <div class="two">
    <div class="card">
      <div class="sec-hd">
        <div class="sec-title">Google Gemini 계정 풀 (한도 시:분:초 실시간 카운트다운)</div>
        <span style="font-size:12px;color:var(--muted);" id="acctCountLbl">0 accounts</span>
      </div>
      <div class="acct-list" id="acctList"></div>
    </div>

    <div class="card">
      <div class="sec-hd"><div class="sec-title">엔진 설정 (RAM 보호 · 자동 순환)</div></div>
      <div class="setting-row">
        <span>실행 모드</span>
        <select id="setMode" onchange="saveSet()" style="width:auto;padding:5px 8px;">
          <option value="hot">Hot (웜 스탠바이 + 15회 탭 리사이클링)</option>
          <option value="cold">Cold (요청 직후 즉시 0MB 종료)</option>
        </select>
      </div>
      <div class="setting-row">
        <span>기본 한도 쿨다운 (시간)</span>
        <input id="setQuotaH" type="number" min="1" max="48" value="6" onchange="saveSet()" style="width:74px;padding:4px 6px;"/>
      </div>
      <div class="setting-row">
        <span>최대 동시 활성 브라우저 (RAM 보호)</span>
        <input id="setMaxB" type="number" min="1" max="5" value="1" onchange="saveSet()" style="width:74px;padding:4px 6px;"/>
      </div>
      <div class="setting-row">
        <span>계정당 연속 요청 간격 (초)</span>
        <input id="setGap" type="number" min="0" max="120" value="10" onchange="saveSet()" style="width:74px;padding:4px 6px;"/>
      </div>
      <div class="setting-row">
        <span>OpenAI API Base URL</span>
        <code style="font-size:12px;color:var(--pri);">http://127.0.0.1:11501/v1</code>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="sec-hd"><div class="sec-title">최근 사용 기록 (telemetry.db)</div></div>
    <table>
      <thead><tr><th>시각</th><th>계정</th><th>모델</th><th>소스</th><th>소요</th><th>결과</th><th>프롬프트</th></tr></thead>
      <tbody id="recentBody"></tbody>
    </table>
  </div>
</div>

<div class="modal-bg" id="addModal">
  <div class="modal">
    <h3 style="margin-bottom:14px;">Google Gemini 계정 추가</h3>
    <div class="field"><label>계정 이름 *</label><input id="fName" placeholder="예: sub_account_6"/></div>
    <div class="field"><label>이메일 (선택)</label><input id="fEmail" placeholder="example@gmail.com"/></div>
    <div class="field"><label>CDP 포트 (비우면 자동 할당)</label><input id="fPort" type="number" placeholder="9342"/></div>
    <div class="field"><label>브라우저</label><select id="fBrowser"><option value="edge">Microsoft Edge</option><option value="chrome">Google Chrome</option></select></div>
    <div class="field"><label>프로필 경로 (비우면 자동 생성)</label><input id="fProf" placeholder="C:\Users\...\gemini_profile_..."/></div>
    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px;">
      <button onclick="closeAddModal()">취소</button>
      <button class="btn-pri" onclick="submitAdd()">추가 후 로그인</button>
    </div>
  </div>
</div>

<script>
let cachedWorkers = [];
let timeOffset = 0;

function esc(s){ return String(s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function fmtHms(sec){
  const t = Math.max(0, Math.floor(sec));
  const h = String(Math.floor(t/3600)).padStart(2,'0');
  const m = String(Math.floor((t%3600)/60)).padStart(2,'0');
  const s = String(t%60).padStart(2,'0');
  return `${h}:${m}:${s}`;
}
function showNotice(cls, msg){
  const el = document.getElementById('notice');
  el.className = 'notice ' + cls;
  el.textContent = msg;
  setTimeout(()=>{ if(el.textContent===msg) el.className='notice'; }, 6000);
}

function renderAccounts(){
  const now = (Date.now()/1000) + timeOffset;
  let avail = 0, quotaCnt = 0;
  const html = cachedWorkers.map(w => {
    const rem = w.cooldown_until > now ? (w.cooldown_until - now) : 0;
    const elapsed = (rem > 0 && w.quota_hit_at > 0) ? Math.max(0, now - w.quota_hit_at) : 0;
    const isCooling = rem > 0;
    if(isCooling && w.cooldown_kind === 'quota') quotaCnt++;
    if(!isCooling && w.login_state !== 'required') avail++;

    let badgeCls = 'b-unk', badgeTxt = '확인 전';
    if(w.verifying){ badgeCls='b-busy'; badgeTxt='점검 중...'; }
    else if(w.busy){ badgeCls='b-busy'; badgeTxt='작업 중'; }
    else if(isCooling && w.cooldown_kind === 'quota'){ badgeCls='b-quota'; badgeTxt='한도 쉬는 중'; }
    else if(w.login_state === 'required' || (isCooling && w.cooldown_kind === 'login')){ badgeCls='b-req'; badgeTxt='로그인 필요'; }
    else if(w.login_state === 'ok'){ badgeCls='b-ok'; badgeTxt = w.alive ? '연결됨 (활성)' : '연결됨'; }

    const cdHtml = isCooling
      ? `<div class="acct-cd">⏳ 남은 시간 ${fmtHms(rem)} · 경과 ${fmtHms(elapsed)} (시작 ${esc(w.quota_hit_str)} → 복귀 ${esc(w.cooldown_until_str)})</div>`
      : '';
    const reasonHtml = (isCooling && w.cooldown_reason) ? `<div class="acct-sub" style="color:var(--warn);">${esc(w.cooldown_reason)}</div>` : '';
    const resetBtn = isCooling ? `<button class="btn-xs btn-warn" onclick="resetCooldown(${w.port})">쿨다운 해제</button>` : '';
    const loginBtn = `<button class="btn-xs ${w.login_state==='ok'?'':'btn-pri'}" onclick="loginAcct(${w.port})">${w.login_state==='ok'?'창 열기':'계정 연결'}</button>`;

    return `<div class="acct">
      <div>
        <div class="acct-name">${esc(w.name)} <span style="font-weight:400;color:var(--muted);">:${w.port}</span></div>
        <div class="acct-sub">${esc(w.login_identity || w.email || w.profile_dir)} · 오늘 ${w.today_queries||0}건 / 누적 ${w.total_queries||0}건</div>
        ${cdHtml}${reasonHtml}
      </div>
      <div class="w-btns">
        <button class="btn-xs" onclick="verifyOne(${w.port})">점검</button>
        ${loginBtn}
        ${resetBtn}
        <button class="btn-xs btn-del" onclick="delAcct(${w.port}, ${JSON.stringify(w.name)})">삭제</button>
        <span class="badge ${badgeCls}">${badgeTxt}</span>
      </div>
    </div>`;
  }).join('');
  document.getElementById('acctList').innerHTML = html || '<div style="color:var(--muted);font-size:13px;">등록된 계정이 없습니다.</div>';
  document.getElementById('acctCountLbl').textContent = `${cachedWorkers.length} accounts`;
  document.getElementById('kAccts').textContent = `${avail} / ${cachedWorkers.length}`;
  document.getElementById('kQuotaCnt').textContent = `한도 쉬는 중 ${quotaCnt}개`;
}

async function refresh(){
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    timeOffset = (d.server_time || (Date.now()/1000)) - (Date.now()/1000);
    cachedWorkers = d.workers || [];
    document.getElementById('modeBadge').textContent = (d.mode||'hot').toUpperCase();
    document.getElementById('setMode').value = d.mode || 'hot';
    document.getElementById('setQuotaH').value = Math.round((d.settings?.quota_cooldown_sec || 21600)/3600);
    document.getElementById('setMaxB').value = d.settings?.max_active_browsers || 1;
    document.getElementById('setGap').value = d.settings?.min_gap_sec ?? 10;

    const st = d.stats || {};
    document.getElementById('kToday').textContent = (st.today?.queries||0).toLocaleString();
    document.getElementById('kTodayTok').textContent = `${(st.today?.tokens||0).toLocaleString()} tokens`;
    document.getElementById('kTotal').textContent = (st.total?.queries||0).toLocaleString();
    document.getElementById('kRate').textContent = `성공률 ${st.total?.success_rate ?? 100}%`;
    document.getElementById('kTask').textContent = d.current_task || '대기 중';
    document.getElementById('kAvg').textContent = `평균 ${Math.round(st.total?.avg_duration_ms || 0)}ms`;

    renderAccounts();
    const rows = (st.recent || []).map(x => `<tr>
      <td>${esc(x.time_str)}</td>
      <td>${esc(x.account_label)}:${x.worker_port}</td>
      <td>${esc(x.model)}</td>
      <td>${esc(x.source)}</td>
      <td>${Math.round(x.duration_ms||0)}ms</td>
      <td>${x.success ? '<span style="color:var(--ok);font-weight:700;">성공</span>' : '<span style="color:var(--err);font-weight:700;">실패</span>'}</td>
      <td style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(x.prompt_preview)}</td>
    </tr>`).join('');
    document.getElementById('recentBody').innerHTML = rows || '<tr><td colspan="7" style="color:var(--muted);text-align:center;">기록 없음</td></tr>';
  } catch(e){}
}

function openAddModal(){ document.getElementById('addModal').style.display='flex'; }
function closeAddModal(){ document.getElementById('addModal').style.display='none'; }

async function submitAdd(){
  const name = document.getElementById('fName').value.trim();
  if(!name){ alert('계정 이름을 입력하세요.'); return; }
  const portRaw = document.getElementById('fPort').value.trim();
  const payload = {
    name,
    email: document.getElementById('fEmail').value.trim(),
    browser: document.getElementById('fBrowser').value,
    profile_dir: document.getElementById('fProf').value.trim() || null,
    port: portRaw ? parseInt(portRaw, 10) : null,
  };
  const r = await fetch('/api/accounts', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
  const d = await r.json();
  if(!r.ok){ alert(d.detail || '계정 추가 실패'); return; }
  closeAddModal();
  document.getElementById('fName').value=''; document.getElementById('fEmail').value=''; document.getElementById('fPort').value=''; document.getElementById('fProf').value='';
  showNotice('ok', `계정 '${d.account.name}'(:${d.account.port}) 추가 완료 — 로그인 창을 엽니다.`);
  await refresh();
  await loginAcct(d.account.port);
}

async function loginAcct(port){
  const r = await fetch(`/api/accounts/${port}/login`, {method:'POST'});
  if(r.ok) showNotice('ok', `포트 ${port} 로그인 브라우저 창을 열었습니다. Google 로그인을 완료해 주세요.`);
  else showNotice('warn', '로그인 창 열기 실패 (작업 중일 수 있습니다).');
}
async function verifyOne(port){
  showNotice('ok', `포트 ${port} 로그인 및 이미지 한도 점검 중...`);
  await fetch(`/api/accounts/${port}/verify`, {method:'POST'});
  await refresh();
}
async function verifyAll(){
  showNotice('ok', '전체 계정 순차 점검을 시작합니다 (1개씩 실행하여 RAM 보호)...');
  await fetch('/api/accounts/verify', {method:'POST'});
  await refresh();
  showNotice('ok', '전체 계정 점검이 완료되었습니다.');
}
async function resetCooldown(port){
  await fetch(`/api/accounts/${port}/reset-cooldown`, {method:'POST'});
  showNotice('ok', `포트 ${port} 쿨다운을 즉시 해제했습니다.`);
  await refresh();
}
async function delAcct(port, name){
  if(!confirm(`'${name}'(:${port}) 계정을 목록에서 삭제할까요? (프로필 폴더 데이터는 유지됩니다)`)) return;
  await fetch(`/api/accounts/${port}`, {method:'DELETE'});
  await refresh();
}
async function purgeBrowsers(){
  if(!confirm('유휴 Edge/Chrome 프로세스를 모두 정리할까요?')) return;
  await fetch('/api/purge', {method:'POST'});
  showNotice('ok', '유휴 브라우저 프로세스를 정리했습니다.');
  await refresh();
}
async function resetStats(){
  if(!confirm('모든 사용량 통계 기록을 삭제할까요?')) return;
  await fetch('/api/stats/reset', {method:'POST'});
  await refresh();
}
async function saveSet(){
  await fetch('/api/settings', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      mode: document.getElementById('setMode').value,
      quota_cooldown_sec: parseInt(document.getElementById('setQuotaH').value||'6', 10) * 3600,
      max_active_browsers: parseInt(document.getElementById('setMaxB').value||'1', 10),
      min_gap_sec: parseInt(document.getElementById('setGap').value||'10', 10),
    })
  });
  showNotice('ok', '설정이 저장되었습니다.');
  await refresh();
}

async function checkUpdate(manual){
  try {
    const r = await fetch('/api/update/check');
    const d = await r.json();
    const banner = document.getElementById('updateBanner');
    if(d.has_update){
      document.getElementById('updateBannerText').textContent = `✦ 새 릴리스 v${d.latest_version} 출시됨 (현재 v${d.current_version}) — 클릭하면 자동으로 다운로드 후 재시작합니다.`;
      banner.style.display = 'flex';
    } else {
      banner.style.display = 'none';
      if(manual){
        showNotice(d.ok ? 'ok' : 'warn', d.ok ? `현재 최신 버전(v${d.current_version})입니다.` : `업데이트 확인 실패: ${d.error || '네트워크 오류'}`);
      }
    }
  } catch(e){
    if(manual) showNotice('warn', '업데이트 확인 중 오류가 발생했습니다.');
  }
}

async function applyUpdate(){
  if(!confirm('새 버전을 다운로드하여 자동 교체 및 재시작할까요?')) return;
  showNotice('ok', '새 버전 다운로드 및 자동 재시작을 준비 중입니다...');
  try {
    const r = await fetch('/api/update/apply', {method:'POST'});
    const d = await r.json();
    if(!r.ok) showNotice('warn', d.detail || '업데이트 적용 실패');
    else showNotice('ok', `v${d.version} 적용 중 — 곧 새 버전으로 자동 재시작됩니다.`);
  } catch(e){
    showNotice('ok', '업데이트 재시작이 시작되었습니다. 잠시 후 새로고침하세요.');
  }
}

// 매 1초마다 시:분:초 카운트다운 실시간 갱신
setInterval(renderAccounts, 1000);
// 매 5초마다 서버 상태 동기화 + SSE 즉시 반영
setInterval(refresh, 5000);
// 매 30분마다 GitHub 새 릴리스 동적 자동 체크
setInterval(() => checkUpdate(false), 1800000);
try {
  const es = new EventSource('/api/stream');
  es.onmessage = () => refresh();
} catch(e){}
refresh();
checkUpdate(false);
</script>
</body>
</html>
"""


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=DASHBOARD_HTML)
