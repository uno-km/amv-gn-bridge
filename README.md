<div align="center">

# amv-gn-bridge

**Google Gemini 웹 멀티계정 풀을 로컬 OpenAI 호환 REST API(`:11501`), FastMCP 및 브라우저 사이드바 확장프로그램으로 연결하는 한도 자동 순환 데스크톱 브릿지**

[🤖 FOR_AI_README (AI 에이전트 전용 운영 매뉴얼)](FOR_AI_README.md) · [📋 CHANGELOG](CHANGELOG.md)

<p>
  <img src="https://img.shields.io/badge/v0.1.2-blue?style=flat-square" alt="version">
  <img src="https://img.shields.io/badge/Windows%20%7C%20macOS-supported-green?style=flat-square" alt="platform">
  <img src="https://img.shields.io/badge/License-Apache--2.0-blue?style=flat-square" alt="license">
  <img src="https://img.shields.io/badge/Multi--Account-Auto%20Failover-indigo?style=flat-square" alt="failover">
  <img src="https://img.shields.io/badge/Quota%20Timer-HH%3AMM%3ASS%20Live-orange?style=flat-square" alt="countdown">
  <img src="https://img.shields.io/badge/FastMCP-Native%20Supported-purple?style=flat-square" alt="fastmcp">
</p>

</div>

---

## 개요

`amv-gn-bridge`는 여러 개의 무료/일반 Google Gemini 웹(`https://gemini.google.com/app`) 계정을 독립된 브라우저 프로필(CDP 포트 `9337+`)로 등록해 두고, **한 계정의 일일 한도가 차면 자동으로 다음 계정으로 전환(Auto-Failover)**하면서 텍스트 추론과 이미지 생성을 수행하는 로컬 게이트웨이입니다.

- **무제한 계정 등록 및 원클릭 로그인**: 네이티브 GUI 및 웹 대시보드(`http://127.0.0.1:11501/dashboard`)에서 계정을 추가하고 `[계정 연결]`로 브라우저 창을 열어 Google 로그인을 완료하면 프로필에 세션이 유지됩니다.
- **한도 도달 시 자동 다음 계정 전환**: 이미지 생성 메뉴의 비활성(`aria-disabled`) 상태 또는 한도 안내 문구를 감지하면 즉시 다음 가용 계정으로 전환합니다.
- **한도별 시·분·초(`HH:MM:SS`) 실시간 카운트다운**: 한도에 걸린 시각과 재설정 시각(`오후 5:23에 재설정` 등 자동 파싱, 미표시 시 기본 6시간)을 기준으로 **남은 시간(`HH:MM:SS`)과 경과 시간(`HH:MM:SS`)**을 매초 카운트다운하며, `00:00:00` 도달 시 자동으로 가용 풀에 복귀합니다.
- **16GB RAM 보호 및 프로세스 트리 정리**: 계정이 10개 이상 등록되어 있어도 동시에 켜두는 브라우저는 `max_active_browsers`(기본 1개)로 제한하고, 전환/종료 시 `taskkill /F /T`로 자식 프로세스까지 정리합니다. `hot` 모드에서는 15회 요청마다 렌더러 탭을 새 탭으로 리사이클링합니다.
- **`amv-cp-bridge` 동일 규격**: 동일한 `telemetry.db` SQLite 스키마, OpenAI 호환 엔드포인트(`/v1/chat/completions`, `/v1/images/generations`), FastMCP 서버, 브라우저 사이드바 확장프로그램(`extension/`), 그리고 GitHub Releases 동적 감지 및 EXE 핫스왑 자동 재시작 업데이터를 제공합니다.

> 💡 **AI 코딩 에이전트(Claude Code, Cursor, Antigravity 등) 연동**:  
> 에이전트가 즉시 전체 API 계약과 에러 처리 규약을 파악할 수 있도록 **[🤖 FOR_AI_README.md](FOR_AI_README.md)**가 포함되어 있습니다.

---

## 빠른 시작

### 1. 원클릭 설치 (Windows PowerShell)

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/uno-km/amv-gn-bridge/main/scripts/setup_all.ps1 | iex"
```

### 2. 소스에서 실행

```bash
pip install -e . && python -m amv_gn_bridge.cli
```

또는 Windows에서 `start_gui.bat`를 더블클릭하면 네이티브 데스크톱 GUI와 `:11501` 게이트웨이가 즉시 기동됩니다.

---

## OpenAI 호환 REST API (`http://127.0.0.1:11501`)

### 텍스트 채팅 (`POST /v1/chat/completions`)

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:11501/v1/chat/completions" -Method Post -ContentType "application/json" -Body '{"model":"gemini-web","messages":[{"role":"user","content":"안녕하세요!"}]}'
```

### 이미지 생성 (`POST /v1/images/generations`)

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:11501/v1/images/generations" -Method Post -ContentType "application/json" -Body '{"prompt":"A photorealistic vertical 9:16 street photo in Seoul at dusk","model":"gemini-web-image"}'
```

---

## FastMCP 및 브라우저 확장프로그램

- **MCP 서버 실행**: `python -m amv_gn_bridge.cli mcp` (또는 `amv-gn-bridge.exe mcp`)
  - 제공 도구: `ask_gemini`, `generate_gemini_image`, `get_gemini_status`
- **브라우저 확장프로그램**: Chrome / Edge / Whale `확장 프로그램 관리 → 개발자 모드 → 압축해제된 확장 프로그램 로드`에서 `extension/` 폴더(또는 릴리스의 `amv-gn-bridge-extension.zip`)를 로드합니다. (사이드바 단축키: `Ctrl+Shift+G`)

---

## 단일 EXE 빌드 및 동적 자동 업데이트

```bash
python scripts/build/build_exe.py
```

- 빌드 시 `dist/amv-gn-bridge.exe`, `dist/amv-gn-bridge.exe.sha256`, `dist/build_report.json`이 생성되며 `selftest`를 자동으로 검증합니다.
- 실행 중인 앱은 GitHub Releases에 새 버전이 게시되면 상단에 동적 업데이트 배너를 표시하고, 클릭 시 새 `.exe` 다운로드 → SHA-256 검증 → 원자적 핫스왑 교체 → 자동 재시작을 수행합니다.

---

## 라이선스

Copyright © 2026 Eunho Kim / AMEVA Foundation.  
SPDX-License-Identifier: `Apache-2.0 OR Commercial-License`
