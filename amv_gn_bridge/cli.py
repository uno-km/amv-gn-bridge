# Copyright (c) 2026 Eunho Kim / AMEVA Foundation. All rights reserved.
# SPDX-License-Identifier: Apache-2.0 OR Commercial-License
"""
amv-gn-bridge Unified CLI Entrypoint.
Commands:
  amv-gn-bridge               -> Launch native desktop GUI + embedded :11501 gateway
  amv-gn-bridge dashboard     -> Run :11501 gateway and open web dashboard in browser
  amv-gn-bridge gateway       -> Run OpenAI-compatible REST API gateway only
  amv-gn-bridge mcp           -> Run FastMCP stdio server for IDE/Agent integration
  amv-gn-bridge status        -> Print live account pool & HH:MM:SS quota countdown summary
  amv-gn-bridge ask <prompt>  -> Send a one-shot query to the Gemini account pool
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading
import time
import webbrowser

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

try:
    from ._version import __version__
except (ImportError, ValueError):
    from amv_gn_bridge._version import __version__


def cmd_gui(args):
    try:
        from .gui import run_gui
    except (ImportError, ValueError):
        from amv_gn_bridge.gui import run_gui
    run_gui(port=getattr(args, "port", 11501))


def cmd_gateway(args):
    import uvicorn
    try:
        from .gateway import app
    except (ImportError, ValueError):
        from amv_gn_bridge.gateway import app
    print(f"[GATEWAY] amv-gn-bridge REST API listening on http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


def cmd_dashboard(args):
    import uvicorn
    try:
        from .gateway import app
    except (ImportError, ValueError):
        from amv_gn_bridge.gateway import app
    port = getattr(args, "port", 11501)
    host = getattr(args, "host", "127.0.0.1")
    url = f"http://127.0.0.1:{port}/dashboard"
    print("=" * 65)
    print(f"[amv-gn-bridge v{__version__}] Gemini Multi-Account Dashboard & Gateway")
    print(f"[*] 대시보드 URL : {url}")
    print(f"[*] OpenAI API   : http://127.0.0.1:{port}/v1/chat/completions")
    print(f"[*] Image API    : http://127.0.0.1:{port}/v1/images/generations")
    print("=" * 65)

    def _open():
        time.sleep(1.2)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_open, daemon=True).start()
    uvicorn.run(app, host=host, port=port, log_level="info")


def cmd_mcp(_args):
    try:
        from .mcp_server import run_server
    except (ImportError, ValueError):
        from amv_gn_bridge.mcp_server import run_server
    run_server()


def cmd_status(_args):
    try:
        from .engine import GeminiEngine
    except (ImportError, ValueError):
        from amv_gn_bridge.engine import GeminiEngine
    eng = GeminiEngine()
    print(eng.get_status_summary())


def cmd_purge(_args):
    try:
        from .engine import GeminiEngine
    except (ImportError, ValueError):
        from amv_gn_bridge.engine import GeminiEngine
    eng = GeminiEngine()
    closed = asyncio.run(eng.purge_all())
    print(f"[amv-gn-bridge] Purged {closed} idle browser process tree(s).")


def cmd_ask(args):
    try:
        from .engine import GeminiEngine
    except (ImportError, ValueError):
        from amv_gn_bridge.engine import GeminiEngine
    prompt = " ".join(args.prompt).strip()
    if not prompt:
        print("Usage: amv-gn-bridge ask <prompt>")
        sys.exit(1)
    eng = GeminiEngine()
    print(asyncio.run(eng.query(prompt, source="cli")))


def cmd_selftest(args):
    import json
    from pathlib import Path

    report = {"version": __version__, "frozen": bool(getattr(sys, "frozen", False)), "ok": False, "checks": {}}
    try:
        from amv_gn_bridge import engine, gateway, mcp_server, settings, telemetry_db, updater
        report["checks"]["imports"] = True
        report["checks"]["format_hms"] = engine.format_hms(3661) == "01:01:01"
        report["checks"]["updater_parse"] = updater.is_newer("9.9.9", __version__)
        report["checks"]["gateway_routes"] = any(getattr(r, "path", "") == "/v1/images/generations" for r in gateway.app.routes)
        report["ok"] = all(report["checks"].values())
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if getattr(args, "out", None):
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    sys.exit(0 if report["ok"] else 1)


def _close_boot_splash() -> None:
    try:
        import pyi_splash  # type: ignore[import-not-found]
        pyi_splash.close()
    except Exception:
        pass


def main():
    import multiprocessing
    multiprocessing.freeze_support()

    parser = argparse.ArgumentParser(prog="amv-gn-bridge", description="Google Gemini Web Multi-Account Bridge")
    parser.add_argument("--version", action="version", version=f"amv-gn-bridge {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_gui = sub.add_parser("gui", help="Launch native desktop GUI + gateway")
    p_gui.add_argument("--port", type=int, default=11501)
    p_gui.set_defaults(func=cmd_gui)

    p_gw = sub.add_parser("gateway", help="Run REST API gateway only")
    p_gw.add_argument("--host", default="127.0.0.1")
    p_gw.add_argument("--port", type=int, default=11501)
    p_gw.set_defaults(func=cmd_gateway)

    p_dash = sub.add_parser("dashboard", help="Run gateway and open web dashboard")
    p_dash.add_argument("--host", default="127.0.0.1")
    p_dash.add_argument("--port", type=int, default=11501)
    p_dash.set_defaults(func=cmd_dashboard)

    p_mcp = sub.add_parser("mcp", aliases=["serve"], help="Run FastMCP stdio server")
    p_mcp.set_defaults(func=cmd_mcp)

    p_st = sub.add_parser("status", help="Show account pool and quota countdown status")
    p_st.set_defaults(func=cmd_status)

    p_purge = sub.add_parser("purge", help="Terminate idle Gemini browser process trees")
    p_purge.set_defaults(func=cmd_purge)

    p_ask = sub.add_parser("ask", help="One-shot text query")
    p_ask.add_argument("prompt", nargs="+")
    p_ask.set_defaults(func=cmd_ask)

    p_st_test = sub.add_parser("selftest", help="Run automated build self-test")
    p_st_test.add_argument("--out", default="")
    p_st_test.set_defaults(func=cmd_selftest)

    args = parser.parse_args()
    if args.command and args.command != "gui":
        _close_boot_splash()
    if hasattr(args, "func"):
        args.func(args)
    else:
        cmd_gui(args)


if __name__ == "__main__":
    main()
