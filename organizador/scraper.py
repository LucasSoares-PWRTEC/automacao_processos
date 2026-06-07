# -*- coding: utf-8 -*-
"""Busca online de processos via scraper internalizado (imports tardios, isolados)."""

from __future__ import annotations

import io
import shutil
from typing import Callable

from .config import (
    CONFIG_DIR,
    CONFIG_ENV_EXAMPLE_PATH,
    CONFIG_ENV_PATH,
    LEGACY_ENV_PATH,
    TJ_SISTEMAS,
    _ESTADO_TJ_UF,
)
from .integracoes.tjsp_scraper.parser import _extrair_blocos_com_origem, extrair_penhora_bloqueio_tribunais
from .integracoes.tjsp_scraper.scrapers import criar_scraper
from .models import Processo
from .organizacao import _dict_para_processo
from .padroes import MAPA_UF_CNJ, RE_CNJ_TR, RE_NUMERO_CNJ


class _LogWriter(io.TextIOBase):
    """Redireciona os print() do scraper para uma callback (o log da aba).
    Essencial sob pythonw, onde sys.stdout é None e print() quebraria."""

    def __init__(self, put: Callable[[str], None]) -> None:
        self._put = put

    def write(self, s: str) -> int:
        if s:
            self._put(s)
        return len(s)


def _preparar_env_local() -> None:
    """Garante que exista um `.env` local, migrando do legado quando possível."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_ENV_PATH.exists():
        return
    if LEGACY_ENV_PATH.exists():
        shutil.copy2(LEGACY_ENV_PATH, CONFIG_ENV_PATH)
        return
    if CONFIG_ENV_EXAMPLE_PATH.exists():
        shutil.copy2(CONFIG_ENV_EXAMPLE_PATH, CONFIG_ENV_PATH)


def buscar_processo_online(
    numero: str, tj_label: str, headless: bool, log: Callable[[str], None],
    modo_docs: str = "nenhum",
) -> Processo:
    """Loga no TJ/sistema escolhido via integração internalizada, consulta o processo e
    devolve um Processo.

    `tj_label` é uma das chaves de TJ_SISTEMAS (ex.: "TJSP — eproc"). Só o eproc
    tem extrator estruturado (auto-preenche os campos); para e-SAJ/PJe devolve um
    Processo mínimo (número + tribunal/UF) e anexa as palavras-chave de
    penhora/bloqueio encontradas.

    `modo_docs` (só eproc): "nenhum" (default — não abre documentos, bem mais
    rápido; a petição não usa o teor dos atos), "atos" ou "todos".

    Levanta ValueError (número/TJ inválidos), ImportError (libs do scraper
    ausentes) ou RuntimeError (login/consulta sem resultado).
    """
    m = RE_NUMERO_CNJ.search(numero or "")
    if not m:
        raise ValueError(f"Número fora do padrão CNJ: {numero!r}")
    numero = m.group(0)
    if tj_label not in TJ_SISTEMAS:
        raise ValueError(f"TJ/sistema inválido: {tj_label!r}")
    site, estado, url = TJ_SISTEMAS[tj_label]
    tribunal, uf = _ESTADO_TJ_UF[estado]

    # Aviso (não bloqueia): número parece ser de outra UF que não a do TJ escolhido.
    tr = RE_CNJ_TR.search(numero)
    uf_num = MAPA_UF_CNJ.get(f"{tr.group(1)}.{tr.group(2)}", "") if tr else ""
    if uf_num and uf_num != uf:
        log(f"Aviso: o número parece ser de {uf_num}, mas você selecionou {tribunal} ({uf}).\n")

    from dotenv import load_dotenv
    _preparar_env_local()
    load_dotenv(CONFIG_ENV_PATH)

    log(f"Abrindo {tj_label} e logando...\n")
    scraper = criar_scraper(site=site, estado=estado, url=url, headless=headless)
    try:
        scraper.login()
        log(f"Consultando processo {numero}"
            + (f" (docs={modo_docs})" if site == "eproc" else "") + "...\n")
        if site == "eproc":
            html = scraper.consultar_processo(numero, modo_docs=modo_docs)
        else:
            html = scraper.consultar_processo(numero)  # esaj/pje não aceitam modo_docs
        if not html:
            raise RuntimeError("Scraper não retornou dados para esse número.")

        if site == "eproc":
            from .integracoes.tjsp_scraper.parser_processo import extrair_processo
            proc = _dict_para_processo(
                extrair_processo(html, _extrair_blocos_com_origem(html), numero_cnj=numero)
            )
            log("Dados extraídos com sucesso.\n")
        else:
            proc = Processo(numero=numero, tribunal=tribunal, uf=uf)
            log("Auto-preenchimento estruturado indisponível para e-SAJ/PJe — "
                "preencha os campos manualmente.\n")

        if not proc.numero:
            proc.numero = numero

        # Palavras-chave de penhora/bloqueio (extrator genérico, todos os sites).
        keywords = list(dict.fromkeys(
            k["descricao"] for k in extrair_penhora_bloqueio_tribunais(html) if k.get("descricao")
        ))
        if keywords:
            log("Palavras-chave de penhora/bloqueio: " + ", ".join(keywords) + "\n")
            if site != "eproc":  # no eproc não polui as tags estruturadas
                for kw in keywords:
                    if kw not in proc.tags:
                        proc.tags.append(kw)
        return proc
    finally:
        try:
            scraper.fechar()
        except Exception:
            pass
