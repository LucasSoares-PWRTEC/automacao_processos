# -*- coding: utf-8 -*-
"""Configurações: caminhos, mapa de TJ/sistema, OAB do escritório e layout."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _raiz_app() -> Path:
    """Raiz onde ficam os dados do app (config/, Petições/).

    - Em execução normal: a pasta que contém o pacote ``organizador/``.
    - Empacotado com PyInstaller (onefile): ``sys._MEIPASS`` (pasta temporária
      onde o exe extrai os dados embutidos). Assim o exe acha config/.env,
      credenciais.json e os modelos de petição embutidos, sem instalação.
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent.parent


# Raiz do projeto = pasta que contém o pacote organizador/ (ou o bundle).
_RAIZ = _raiz_app()

def _resolver_pasta_padrao() -> str:
    env = os.getenv("ORGANIZADOR_PASTA_PADRAO")
    if env:
        return str(Path(env).expanduser())
    desktop = Path.home() / "Desktop" / "Processos"
    if desktop.exists():
        return str(desktop)
    return str((Path.home() / "Processos").expanduser())


PASTA_PADRAO = _resolver_pasta_padrao()
PASTA_NAO_IDENTIFICADOS = "_nao_identificados"
CACHE_DIR = ".cache_texto"

# Dados aprendidos do app, persistidos em local estável do usuário (NÃO no
# bundle/_MEIPASS, que é temporário no exe). Hoje guarda só o cache de
# código-de-origem CNJ → comarca, aprendido dos processos já vistos.
APP_DATA_DIR = Path(
    os.getenv("ORGANIZADOR_APP_DATA") or (Path.home() / ".automacao_processos")
)
COMARCAS_CACHE_PATH = APP_DATA_DIR / "comarcas_por_origem.json"
# Estado da UI lembrado entre execuções (pastas base/destino da aba Pesquisa),
# para pré-carregar os processos já trabalhados ao reabrir o app.
UI_STATE_PATH = APP_DATA_DIR / "ui_state.json"
# Perfil persistente do Chrome usado SÓ pelo Autojur: guarda cookies/dispositivo
# confiável entre sessões para pular o 2FA "novo dispositivo" nas próximas vezes
# (login cai de ~3-4 min para segundos). A busca online usa perfil temporário
# próprio — um perfil do Chrome só pode ser aberto por um processo por vez.
AUTOJUR_PROFILE_DIR = APP_DATA_DIR / "autojur_profile"
# Processos trazidos pela busca online são persistidos aqui (um
# <numero>/metadados.json por processo) para reaparecerem na lista ao reabrir.
PROCESSOS_ONLINE_DIR = APP_DATA_DIR / "processos_online"
# Índice de RESUMO (só campos de exibição/filtro) de todos os processos já no
# sistema, atualizado incrementalmente por mtime. Torna a carga da lista da aba
# Pesquisa quase instantânea: sem ele, todos os metadados.json seriam relidos e
# parseados a cada abertura. O Processo completo é carregado sob demanda.
PROCESSOS_INDICE_JSON = APP_DATA_DIR / "processos_indice.json"
# Processos pré-carregados EMBUTIDOS na release (JSON consolidado, gzip), para
# aparecerem já populados em qualquer máquina onde o app for instalado. Fica em
# _RAIZ (=_MEIPASS no exe), como config/ e Petições/. Prioridade menor que a
# pasta base e que o cache de buscas online do próprio usuário.
PROCESSOS_BASE_JSON = _RAIZ / "data" / "processos_base.json.gz"
PASTA_PETICOES = _RAIZ / "Petições"
# Conta de serviço do Google (somente leitura das planilhas de credores). Fica na
# raiz do projeto (=_MEIPASS no exe); override por GOOGLE_CREDENTIALS_FILE. Usada
# por organizador.sheets para o monitoramento periódico planilhas → mapeamento.
GOOGLE_SA_PATH = Path(
    os.getenv("GOOGLE_CREDENTIALS_FILE") or (_RAIZ / "token_google_api.json")
)
CONFIG_DIR = _RAIZ / "config"
CONFIG_ENV_PATH = CONFIG_DIR / ".env"
CONFIG_ENV_EXAMPLE_PATH = CONFIG_DIR / ".env.example"
CREDENCIAIS_PATH = CONFIG_DIR / "credenciais.json"
CREDENCIAIS_EXAMPLE_PATH = CONFIG_DIR / "credenciais.example.json"
LEGACY_SCRAPER_DIR = _RAIZ.parent / "TJSP-Scraper"
LEGACY_ENV_PATH = LEGACY_SCRAPER_DIR / ".env"
LEGACY_SENHAS_XLSX_PATH = LEGACY_SCRAPER_DIR / "data" / "senhas.xlsx"

# Rótulo do seletor de TJ/Sistema -> (site, estado, url, perfil). O e-SAJ deriva
# a URL de login (sajcas) e a chave de credencial do host da `url` do cpopg, então
# a url IMPORTA para o e-SAJ (e atende vários estados); o PJe ainda usa url fixa na
# classe. e-SAJ e eproc têm extrator estruturado (auto-preenchem a petição); o PJe
# entra com Processo mínimo + palavras-chave de penhora.
#
# `perfil` (4º elemento, só eproc): sigla da OAB a selecionar na tela "Seleção de
# perfil". A conta do TJSP eproc dá acesso a 6 perfis de OAB do mesmo advogado
# (SP, MG, RJ, PR, RN, GO) — o login/2FA é o mesmo; muda só qual botão clicar.
# Quando None, o scraper usa a sigla padrão do estado (ex.: TJMG → "MG").
TJ_SISTEMAS = {
    # ── SP ──────────────────────────────────────────────────────────────────
    "TJSP — eproc":  ("eproc", "SAO PAULO", "https://eproc1g.tjsp.jus.br/eproc/", "SP"),
    "TJSP — e-SAJ":  ("esaj",  "SAO PAULO", "https://esaj.tjsp.jus.br/cpopg/open.do", None),
    # ── AM ──────────────────────────────────────────────────────────────────
    "TJAM — e-SAJ":  ("esaj",  "AMAZONAS",           "https://consultasaj.tjam.jus.br/cpopg/open.do", None),
    # ── DF ──────────────────────────────────────────────────────────────────
    # PJe nacional (login.seam → sso.cloud.pje.jus.br, client pje-tjdft-1g).
    # Validado ao vivo: a conta tem acesso ao TJDFT. (TJBA é PJe nacional também,
    # mas a conta NÃO está habilitada lá — login não confirma após o TOTP.)
    "TJDFT — PJe":   ("pje",   "DISTRITO FEDERAL",   "https://pje.tjdft.jus.br/pje/", None),
    # ── CE ──────────────────────────────────────────────────────────────────
    # PJe é nacional (login único via PDPJ): o scraper deriva login.seam e a
    # busca interna da URL base abaixo (cada tribunal tem seu host/path: /pje/,
    # /pje1grau/, /1g/...). O TOTP é o mesmo (TOTP_PDPJ) em todos.
    "TJCE — PJe":    ("pje",   "CEARA",              "https://pje.tjce.jus.br/pje1grau/", None),
    # ── GO ──────────────────────────────────────────────────────────────────
    # Acesso via perfil OAB-GO na plataforma eproc do TJSP (mesmo login/2FA).
    "TJGO — eproc":  ("eproc", "SAO PAULO",           "https://eproc1g.tjsp.jus.br/eproc/", "GO"),
    # ── MA ──────────────────────────────────────────────────────────────────
    "TJMA — PJe":    ("pje",   "MARANHAO",            "https://pje.tjma.jus.br/pje/", None),
    # ── MG ──────────────────────────────────────────────────────────────────
    "TJMG — eproc":  ("eproc", "MINAS GERAIS",        "https://eproc1g.tjmg.jus.br/eproc/", None),
    "TJMG — PJe":    ("pje",   "MINAS GERAIS",        "https://pje.tjmg.jus.br/pje/", None),
    # ── MS ──────────────────────────────────────────────────────────────────
    "TJMS — e-SAJ":  ("esaj",  "MATO GROSSO DO SUL",  "https://esaj.tjms.jus.br/cpopg/open.do", None),
    # ── PA ──────────────────────────────────────────────────────────────────
    "TJPA — PJe":    ("pje",   "PARA",                "https://pje.tjpa.jus.br/pje/", None),
    # ── PE ──────────────────────────────────────────────────────────────────
    "TJPE — PJe":    ("pje",   "PERNAMBUCO",          "https://pje.cloud.tjpe.jus.br/1g/", None),
    # ── PR ──────────────────────────────────────────────────────────────────
    # Acesso via perfil OAB-PR na plataforma eproc do TJSP (mesmo login/2FA).
    "TJPR — eproc":  ("eproc", "SAO PAULO",           "https://eproc1g.tjsp.jus.br/eproc/", "PR"),
    # ── RJ ──────────────────────────────────────────────────────────────────
    # CONSULTA PÚBLICA do TJRJ (www3.tjrj.jus.br/consultaprocessual): API REST
    # ABERTA — sem login, sem certificado, sem captcha (recuperar-site-key → 204).
    # O PJe do TJRJ exige certificado digital e o eproc/perfil-RJ do TJSP não
    # hospeda processos do TJRJ; esta consulta pública é a rota viável. 1ª inst.
    "TJRJ — Consulta": ("tjrj", "RIO DE JANEIRO", "https://www3.tjrj.jus.br/consultaprocessual/api/", None),
    # ── RN ──────────────────────────────────────────────────────────────────
    # Acesso via perfil OAB-RN na plataforma eproc do TJSP (mesmo login/2FA).
    "TJRN — eproc":  ("eproc", "SAO PAULO",           "https://eproc1g.tjsp.jus.br/eproc/", "RN"),
    # ── SC ──────────────────────────────────────────────────────────────────
    "TJSC — eproc":  ("eproc", "SANTA CATARINA",      "https://eproc1g.tjsc.jus.br/eproc/", "SC"),
}
_ESTADO_TJ_UF = {
    "SAO PAULO": ("TJSP", "SP"),
    "MINAS GERAIS": ("TJMG", "MG"),
    "RIO DE JANEIRO": ("TJRJ", "RJ"),
    "DISTRITO FEDERAL": ("TJDFT", "DF"),
    "AMAZONAS": ("TJAM", "AM"),
    "MATO GROSSO DO SUL": ("TJMS", "MS"),
    "CEARA": ("TJCE", "CE"),
    "MARANHAO": ("TJMA", "MA"),
    "PARA": ("TJPA", "PA"),
    "PERNAMBUCO": ("TJPE", "PE"),
    "SANTA CATARINA": ("TJSC", "SC"),
}


def uf_efetiva_tj(tj_label: str) -> str:
    """UF que o sistema efetivamente atende (perfil da OAB ou UF do estado)."""
    if tj_label not in TJ_SISTEMAS:
        return ""
    _site, estado, _url, perfil = TJ_SISTEMAS[tj_label]
    return (perfil or _ESTADO_TJ_UF[estado][1]).upper()


def sugerir_tj_para_uf(uf: str) -> str | None:
    """Rótulo de TJ_SISTEMAS cuja UF efetiva == uf. Prefere eproc; senão o 1º."""
    uf = (uf or "").upper()
    candidatos = [lbl for lbl in TJ_SISTEMAS if uf_efetiva_tj(lbl) == uf]
    if not candidatos:
        return None
    eproc = [lbl for lbl in candidatos if TJ_SISTEMAS[lbl][0] == "eproc"]
    return (eproc or candidatos)[0]


# Ordem de preferência por sistema no fallback entre tribunais da MESMA UF.
# eproc e PJe expõem partes COM documento (necessário p/ pegar o réu); o e-SAJ
# público não traz CPF/CNPJ → fica por último.
_PRIORIDADE_SITE = {"eproc": 0, "pje": 1, "esaj": 2}
# Override por UF: em MG os processos-alvo estão quase todos no PJe (o eproc tem
# pouquíssimos), então tentar PJe primeiro evita uma consulta eproc inútil.
_PRIORIDADE_SITE_POR_UF = {
    "MG": {"pje": 0, "eproc": 1, "esaj": 2},
}


def candidatos_tj_para_uf(uf: str) -> list[str]:
    """Todos os rótulos de TJ_SISTEMAS que atendem a UF, em ordem de preferência
    para enriquecer (sistemas que trazem partes com documento primeiro).

    Base do fallback pedido pelo usuário: se o processo não está num sistema,
    tenta o próximo (eproc × e-SAJ × PJe). Lista vazia = UF sem rota suportada."""
    uf = (uf or "").upper()
    prioridade = _PRIORIDADE_SITE_POR_UF.get(uf, _PRIORIDADE_SITE)
    candidatos = [lbl for lbl in TJ_SISTEMAS if uf_efetiva_tj(lbl) == uf]
    return sorted(candidatos, key=lambda lbl: prioridade.get(TJ_SISTEMAS[lbl][0], 99))

# OAB do advogado do escritório (Felipe Machado) por UF — usado para
# autopreencher os campos OAB UF / OAB Nº ao selecionar um processo.
OAB_ESCRITORIO_POR_UF: dict[str, str] = {
    "AM": "A 1543",
    "BA": "49.510",
    "CE": "37.928-A",
    "DF": "55.703",
    "ES": "26.487",
    "GO": "39.882-A",
    "MG": "104.784",
    "MS": "22.345-A",
    "MT": "21.457-A",
    "PE": "45.442",
    "PR": "70.339",
    "RJ": "186.328",
    "RN": "01.049-A",
    "SC": "51.064",
    "SP": "346.433",
}

# Padronização de layout do consolidado.
SPACE_AFTER_PT = 6
LINE_SPACING = 1.5
