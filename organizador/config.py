# -*- coding: utf-8 -*-
"""Configurações: caminhos, mapa de TJ/sistema, OAB do escritório e layout."""

from __future__ import annotations

import os
from pathlib import Path

# Raiz do projeto = pasta que contém o pacote organizador/.
_RAIZ = Path(__file__).resolve().parent.parent

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
PASTA_PETICOES = _RAIZ / "Petições"
CONFIG_DIR = _RAIZ / "config"
CONFIG_ENV_PATH = CONFIG_DIR / ".env"
CONFIG_ENV_EXAMPLE_PATH = CONFIG_DIR / ".env.example"
CREDENCIAIS_PATH = CONFIG_DIR / "credenciais.json"
CREDENCIAIS_EXAMPLE_PATH = CONFIG_DIR / "credenciais.example.json"
LEGACY_SCRAPER_DIR = _RAIZ.parent / "TJSP-Scraper"
LEGACY_ENV_PATH = LEGACY_SCRAPER_DIR / ".env"
LEGACY_SENHAS_XLSX_PATH = LEGACY_SCRAPER_DIR / "data" / "senhas.xlsx"

# Rótulo do seletor de TJ/Sistema -> (site, estado, url). e-SAJ e PJe têm a URL
# de login fixa dentro da própria classe do scraper (ignoram a url passada); só
# o eproc usa a url. Apenas o eproc tem extrator estruturado (auto-preenche a
# petição); e-SAJ/PJe entram com Processo mínimo + palavras-chave de penhora.
TJ_SISTEMAS = {
    "TJSP — eproc": ("eproc", "SAO PAULO",    "https://eproc1g.tjsp.jus.br/eproc/"),
    "TJSP — e-SAJ": ("esaj",  "SAO PAULO",    "https://esaj.tjsp.jus.br/cpopg/open.do"),
    "TJMG — eproc": ("eproc", "MINAS GERAIS", "https://eproc1g.tjmg.jus.br/eproc/"),
    "TJMG — PJe":   ("pje",   "MINAS GERAIS", "https://pje.tjmg.jus.br/pje/"),
}
_ESTADO_TJ_UF = {"SAO PAULO": ("TJSP", "SP"), "MINAS GERAIS": ("TJMG", "MG")}

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
