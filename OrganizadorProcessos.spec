# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — Organizador de Processos (onefile, GUI sem console).

Empacota TUDO num único .exe: o pacote `organizador/`, os modelos de petição
(`Petições/`) e a config do escritório (`config/` com credenciais.json e .env).
Roda em outra máquina sem instalar Python nem configurar nada.

Requisitos na máquina-alvo (não dá pra embutir):
  - Google Chrome instalado (o scraper dirige o Chrome).
  - Internet no 1º uso da busca (baixa o chromedriver compatível 1 vez).

Build:  python -m PyInstaller --noconfirm OrganizadorProcessos.spec
Saída:  dist/OrganizadorProcessos.exe
"""
from PyInstaller.utils.hooks import collect_all

datas = [
    ("config", "config"),        # credenciais.json + .env (embutidos)
    ("Petições", "Petições"),    # modelos .docx das petições
]
binaries = []
hiddenimports = []

# Pacotes com dados/submódulos que o PyInstaller não pega sozinho.
for _pkg in (
    "ttkbootstrap", "selenium", "webdriver_manager", "pandas", "openpyxl",
    "pyotp", "bs4", "pypdf", "docx", "unidecode", "dotenv", "requests",
):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h


a = Analysis(
    ["organizar_processos.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests", "pytest", "matplotlib", "PyQt5", "PyQt6", "PySide2", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Peticoes",
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
