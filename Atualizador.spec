# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — Atualizador do Peticoes (onefile, GUI sem console).

Empacota só o updater (stdlib + tkinter), com o token de leitura do GitHub
embutido via updater/_token.py — arquivo gerado pelo release.ps1, fora do git.

Build:  .\release.ps1   (gera o _token.py e roda este spec)
Saída:  dist/Atualizador.exe
"""
from pathlib import Path

if not Path("updater/_token.py").exists():
    raise SystemExit(
        "ERRO: updater/_token.py não existe — o Atualizador sairia sem token.\n"
        "Rode o build pelo release.ps1 (ele gera o _token.py a partir de "
        "secrets\\token_github.txt)."
    )

a = Analysis(
    ["updater/updater.py"],
    pathex=["updater"],
    binaries=[],
    # Mesmo ícone do Peticoes.exe (ver OrganizadorProcessos.spec), copiado para
    # a raiz do bundle (=_MEIPASS no exe) — é onde updater._caminho_icone() procura.
    datas=[("organizador/assets/icone.ico", ".")],
    hiddenimports=["_token"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pandas", "numpy", "selenium", "ttkbootstrap", "openpyxl", "docx",
        "pypdf", "bs4", "requests", "PIL", "matplotlib", "pytest",
        "webdriver_manager", "pyotp", "dotenv", "unidecode",
        "PyQt5", "PyQt6", "PySide2", "PySide6",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Atualizador",
    icon="organizador/assets/icone.ico",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # GUI: sem janela de console
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
