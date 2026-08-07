# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — Organizador de Processos (onefile, GUI sem console).

Empacota TUDO num único .exe: o pacote `organizador/`, os modelos de petição
(`Petições/`) e a config do escritório (`config/` com credenciais.json e .env).
Roda em outra máquina sem instalar Python nem configurar nada.

Requisitos na máquina-alvo (não dá pra embutir):
  - Google Chrome instalado (o scraper dirige o Chrome).
  - Internet no 1º uso da busca (baixa o chromedriver compatível 1 vez).

PESO DO BUNDLE — POR QUE IMPORTA
--------------------------------
No modo onefile o bootloader extrai TODO o payload para uma pasta nova em
%TEMP% a CADA abertura, antes de o Python começar. Num i3 de 3ª geração com HD
mecânico isso era o maior custo de abertura do app (316 MB / 7.743 arquivos).
Portanto: cada MB aqui é pago em toda abertura, em toda máquina.

Antes de acrescentar um `collect_all`, confira se a lib é realmente importada
pelo app (`python -X importtime -c "import organizador.gui"`). O caso que doeu
foi `collect_all("pandas")`: o pandas era usado só para ler as planilhas, mas
arrastava pyarrow (81 MB), numpy (21 MB) e, via `pandas.io.formats.printing`,
a árvore IPython/jedi/nbformat/ipykernel/zmq/pydantic. Hoje `organizador.sheets`
lê as planilhas com openpyxl e nada disso entra.

Build:  python -m PyInstaller --noconfirm OrganizadorProcessos.spec
Saída:  dist/Peticoes.exe
"""
import os

from PyInstaller.utils.hooks import collect_all

datas = [
    ("config", "config"),        # credenciais.json + .env (embutidos)
    ("Petições", "Petições"),    # modelos .docx das petições
    # Processos pré-carregados. SQLite (não mais o .json.gz de 8 MB) porque a
    # abertura só precisa da tabela de resumos, e o Processo completo é buscado
    # por número — sem parsear os 70 MB do snapshot inteiro. Gerado no release
    # por scripts/gerar_base_sqlite.py.
    ("data/processos_base.sqlite", "data"),
    # Conta de serviço do Google (leitura das planilhas) na RAIZ do bundle
    # (=_MEIPASS no exe), onde config.GOOGLE_SA_PATH a procura. Sem isto, o
    # monitoramento falha em outras máquinas ("credencial não encontrada").
    ("token_google_api.json", "."),
]
binaries = []
hiddenimports = []

# Pacotes com dados/submódulos que o PyInstaller não pega sozinho.
# NÃO acrescente "pandas" aqui (ver cabeçalho): quem lê planilha é o openpyxl.
for _pkg in (
    "ttkbootstrap", "selenium", "webdriver_manager", "openpyxl",
    "pyotp", "bs4", "pypdf", "docx", "unidecode", "dotenv", "requests",
    # google-auth (conta de serviço p/ as planilhas) + backend de assinatura RSA
    # via cryptography (é o backend em uso; a lib 'rsa' nem está instalada).
    "google.auth", "google.oauth2", "cryptography",
):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

# Submódulos importados de forma tardia (dentro de funções) — o analisador
# estático do PyInstaller não os alcança a partir do entry point.
hiddenimports += [
    "google.oauth2.service_account",
    "google.auth.transport.requests",
    "google.auth.crypt._cryptography_rsa",
    "cachetools",
]

# Nada aqui é importado pelo app; tudo vinha de carona (a maior parte pelo
# pandas, que saiu). Manter a lista explícita evita que uma dependência nova
# reintroduza a árvore inteira sem ninguém notar.
# Atenção: "pytest" NÃO cobre "_pytest", que é o nome real do pacote.
excludes = [
    # suítes de teste / ferramentas de dev
    "tests", "pytest", "_pytest", "setuptools", "pip",
    # a árvore que o pandas arrastava
    "pandas", "numpy", "pyarrow", "IPython", "jedi", "parso",
    "nbformat", "ipykernel", "jupyter_client", "zmq", "tornado",
    "pydantic", "pydantic_core", "rich", "pygments", "prompt_toolkit",
    "markdown_it", "trio", "aiohttp", "psutil", "lark", "jsonschema",
    # GUIs alternativas e plotagem
    "matplotlib", "PyQt5", "PyQt6", "PySide2", "PySide6", "PIL.ImageQt",
    # o IDE do pywin32 (o app usa win32com/pythoncom, não o Pythonwin)
    "Pythonwin", "pythonwin",
]


a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)


# --------------------------------------------------------------------------
# Poda por caminho: binários que o PyInstaller inclui mas são inúteis aqui.
# --------------------------------------------------------------------------
# (fragmento do caminho em minúsculas, motivo)
_PODAR = [
    # O selenium embarca o "selenium-manager" dos TRÊS sistemas operacionais.
    # Num .exe de Windows os outros dois são 13,5 MB de peso morto.
    ("selenium/webdriver/common/macos/", "selenium-manager de macOS"),
    ("selenium/webdriver/common/linux/", "selenium-manager de Linux"),
    # Codec AVIF do Pillow (7,8 MB). O ttkbootstrap usa o PIL só para desenhar
    # os temas em memória; nenhum arquivo .avif é aberto. O Pillow registra
    # plugins dentro de try/except ImportError, então a ausência é tolerada.
    ("pil/_avif", "codec AVIF do Pillow"),
    # O IDE Pythonwin (MFC + Scintilla), 7 MB. O app usa win32com para converter
    # .docx em PDF pelo Word; isso não passa pelo Pythonwin.
    ("pythonwin/", "IDE Pythonwin (MFC/Scintilla)"),
]


def _podar(lista, rotulo):
    """Remove de `lista` (TOC do PyInstaller) as entradas casadas por _PODAR."""
    mantidos, removidos = [], []
    for entrada in lista:
        destino = str(entrada[0]).replace("\\", "/").lower()
        origem = str(entrada[1] or "").replace("\\", "/").lower()
        for fragmento, motivo in _PODAR:
            if fragmento in destino or fragmento in origem:
                removidos.append((entrada[0], motivo))
                break
        else:
            mantidos.append(entrada)
    for nome, motivo in removidos:
        print(f"[spec] podado de {rotulo}: {nome}  ({motivo})")
    return mantidos


a.binaries = _podar(a.binaries, "binaries")
a.datas = _podar(a.datas, "datas")

# Relatório do que sobrou — comparar entre builds pega regressão de tamanho.
_total = sum(
    os.path.getsize(e[1])
    for lista in (a.binaries, a.datas)
    for e in lista
    if e[1] and os.path.isfile(e[1])
)
print(
    f"[spec] payload: {len(a.binaries)} binaries + {len(a.datas)} datas "
    f"= {_total / 1e6:.1f} MB extraídos no %TEMP% por abertura"
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
