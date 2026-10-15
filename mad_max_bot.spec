# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

spec_dir = Path(SPECPATH)
a = Analysis(
    [str(spec_dir / "main.py")], pathex=[str(spec_dir)], binaries=[],
    datas=[(str(spec_dir / "web" / "index.html"), "web"),
           *[(str(spec_dir / name), ".") for name in
             ("config.ini", "channels.txt", "keywords.txt", "keywords.json", "recipients.txt")]],
    hiddenimports=collect_submodules("uvicorn"),
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="MAD_MAX_BOT",
          debug=False, strip=False, upx=False, console=True, contents_directory=".")
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="MAD_MAX_BOT")
