# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
ICON = os.path.join(ROOT, "amv-gn-bridge.ico")

DROPPED_DATA_PREFIXES = (
    os.path.join("playwright", "driver", "package", "lib", "vite") + os.sep,
    os.path.join("playwright", "driver", "package", "types") + os.sep,
    "_tcl_data" + os.sep + "tzdata" + os.sep,
    "_tk_data" + os.sep + "demos" + os.sep,
)
DROPPED_DATA_SUFFIXES = (".bak", ".map")

datas = [(ICON, ".")]
binaries = []
hiddenimports = [
    "tkinter", "tkinter.ttk", "tkinter.messagebox", "sqlite3",
    "amv_gn_bridge", "amv_gn_bridge._version", "amv_gn_bridge.updater",
    "amv_gn_bridge.settings", "amv_gn_bridge.telemetry_db", "amv_gn_bridge.engine",
    "amv_gn_bridge.gateway", "amv_gn_bridge.mcp_server", "amv_gn_bridge.gui",
]

for package in ("uvicorn", "fastapi", "starlette", "pydantic", "websockets",
                "customtkinter", "PIL", "playwright", "certifi", "mcp"):
    collected = collect_all(package)
    datas += collected[0]
    binaries += collected[1]
    hiddenimports += collected[2]

a = Analysis(
    [os.path.join(ROOT, "amv_gn_bridge", "cli.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "unittest", "IPython", "jupyter", "numpy", "matplotlib", "pandas", "scipy"],
    noarchive=False,
    optimize=0,
)

a.datas = [
    entry for entry in a.datas
    if not entry[0].startswith(DROPPED_DATA_PREFIXES) and not entry[0].endswith(DROPPED_DATA_SUFFIXES)
]

pyz = PYZ(a.pure)


def make_boot_image(path):
    from PIL import Image, ImageDraw, ImageFont

    os.makedirs(os.path.dirname(path), exist_ok=True)
    image = Image.new("RGB", (500, 150), "#4f46e5")
    draw = ImageDraw.Draw(image)
    if os.path.isfile(ICON):
        logo = Image.open(ICON).convert("RGBA").resize((56, 56))
        image.paste(logo, (34, 47), logo)
    try:
        font_b = ImageFont.truetype("segoeuib.ttf", 28)
        font_r = ImageFont.truetype("segoeui.ttf", 14)
    except Exception:
        font_b = ImageFont.load_default()
        font_r = ImageFont.load_default()
    draw.text((106, 44), "amv-gn-bridge", fill="#ffffff", font=font_b)
    draw.text((106, 84), "Gemini Multi-Account Manager · Starting...", fill="#c7d2fe", font=font_r)
    image.save(path)


BOOT_IMAGE = os.path.join(ROOT, "build", "boot_splash.png")
make_boot_image(BOOT_IMAGE)
splash = Splash(BOOT_IMAGE, binaries=a.binaries, datas=a.datas, minify_script=True, always_on_top=True)

exe = EXE(
    pyz,
    a.scripts,
    splash,
    splash.binaries,
    a.binaries,
    a.datas,
    [],
    name="amv-gn-bridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[ICON] if os.path.isfile(ICON) else None,
)
