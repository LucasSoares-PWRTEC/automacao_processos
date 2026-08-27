# -*- coding: utf-8 -*-
"""Metadados dos documentos da timeline do PJe numa ÚNICA ida ao navegador.

Antes eram TRÊS por documento (`get_attribute("id")`, `.text` e um
`execute_script` do _JS_DATA_GRUPO): num processo com ~100 docs, ~300 viagens
pelo protocolo do WebDriver só para montar a fila. Mesmo padrão já aplicado no
eproc.py (ver tests/test_eproc_documentos.py): tenta 1 `execute_script` que
devolve a estrutura pronta e, se falhar por qualquer motivo, cai no laço antigo.

O ponto DELICADO coberto aqui: o `txt` tem de manter a semântica do `.text` do
Selenium (vazio para elemento não renderizado). O `innerText` puro cai para
`textContent` quando o elemento não está sendo renderizado, e no PJe esses divs
vivem escondidos sob `mobile-close` — trocar a fonte do texto mudaria QUAIS
documentos o scraper abre, o que não é uma otimização, é outro comportamento.

Nada aqui sobe navegador: os dublês simulam `execute_script`/`find_elements`.
"""

from __future__ import annotations

import pytest

from organizador.integracoes.tjsp_scraper.scrapers import pje as M
from organizador.integracoes.tjsp_scraper.scrapers.pje import PJEScraper


def _scraper(driver) -> PJEScraper:
    """PJEScraper sem __init__ (que abriria um navegador de verdade)."""
    s = object.__new__(PJEScraper)
    s.driver = driver
    return s


class DriverJsFalso:
    def __init__(self, resultado=None, erro: Exception | None = None):
        self._resultado = resultado
        self._erro = erro
        self.chamadas = 0

    def execute_script(self, _script, *_a):
        self.chamadas += 1
        if self._erro is not None:
            raise self._erro
        return self._resultado


# ---------------------------------------------------------------------
# _docs_meta_js — caminho rápido
# ---------------------------------------------------------------------
def test_extrai_todos_os_docs_em_uma_unica_chamada():
    driver = DriverJsFalso(resultado=[
        {"sid": "divTimeLine:0:idDocumentoTimeLine",
         "txt": "10554398891 - Despacho", "data": "06 out 2025"},
        {"sid": "divTimeLine:1:idDocumentoTimeLine",
         "txt": "10554398892 - Sentença", "data": "07 out 2025"},
    ])
    metas = _scraper(driver)._docs_meta_js()

    assert driver.chamadas == 1, "deveria ler tudo numa única ida ao navegador"
    assert metas == [
        {"sid": "divTimeLine:0:idDocumentoTimeLine",
         "txt": "10554398891 - Despacho", "data": "06 out 2025"},
        {"sid": "divTimeLine:1:idDocumentoTimeLine",
         "txt": "10554398892 - Sentença", "data": "07 out 2025"},
    ]


def test_normaliza_campos_ausentes_ou_nulos():
    driver = DriverJsFalso(resultado=[{"sid": None, "txt": None},
                                      {"data": "  06 out 2025  "}])
    assert _scraper(driver)._docs_meta_js() == [
        {"sid": "", "txt": "", "data": ""},
        {"sid": "", "txt": "", "data": "06 out 2025"},
    ]


def test_lista_vazia_e_resultado_valido_nao_fallback():
    """Timeline sem documento não é falha: [] é resposta, não None."""
    driver = DriverJsFalso(resultado=[])
    assert _scraper(driver)._docs_meta_js() == []


@pytest.mark.parametrize("kwargs", [
    {"erro": RuntimeError("sem suporte a JS")},
    {"resultado": None},
    {"resultado": "nao é lista"},
    {"resultado": ["linha que não é dict"]},
])
def test_devolve_none_para_cair_no_fallback(kwargs):
    assert _scraper(DriverJsFalso(**kwargs))._docs_meta_js() is None


# ---------------------------------------------------------------------
# _docs_meta_elementos — fallback elemento a elemento
# ---------------------------------------------------------------------
class ElementoFalso:
    def __init__(self, sid: str, txt: str, erro: Exception | None = None):
        self._sid, self._txt, self._erro = sid, txt, erro

    def get_attribute(self, nome):
        if self._erro is not None:
            raise self._erro
        return self._sid if nome == "id" else None

    @property
    def text(self):
        if self._erro is not None:
            raise self._erro
        return self._txt


class DriverElementosFalso:
    """execute_script serve só ao _JS_DATA_GRUPO (o caminho rápido já falhou)."""
    def __init__(self, datas: dict):
        self._datas = datas
        self.chamadas_data = 0

    def execute_script(self, _script, elemento=None, *_a):
        self.chamadas_data += 1
        return self._datas.get(getattr(elemento, "_sid", ""), "")


def test_fallback_busca_data_so_de_quem_passa_no_trigger(monkeypatch):
    """A `data` custa uma ida ao navegador por documento — não vale gastá-la
    em quem o TRIGGER_DOC vai descartar (era o comportamento antigo)."""
    els = [
        ElementoFalso("s0", "1 - Despacho"),        # passa (DESPACHO)
        ElementoFalso("s1", "2 - Ata de audiência"),  # NÃO passa
        ElementoFalso("s2", "3 - Sentença"),        # passa (SENTENCA, sem acento)
    ]
    driver = DriverElementosFalso({"s0": "06 out 2025", "s2": "08 out 2025"})
    s = _scraper(driver)
    monkeypatch.setattr(M.PJEScraper, "_doc_elements", lambda self: els)

    metas = s._docs_meta_elementos()

    assert driver.chamadas_data == 2, "só os 2 que passam no TRIGGER pedem a data"
    assert metas == [
        {"sid": "s0", "txt": "1 - Despacho", "data": "06 out 2025"},
        {"sid": "s1", "txt": "2 - Ata de audiência", "data": ""},
        {"sid": "s2", "txt": "3 - Sentença", "data": "08 out 2025"},
    ]


def test_fallback_pula_elemento_que_ficou_stale(monkeypatch):
    els = [
        ElementoFalso("s0", "1 - Despacho"),
        ElementoFalso("s1", "2 - Sentença", erro=RuntimeError("stale element")),
    ]
    driver = DriverElementosFalso({"s0": "06 out 2025"})
    s = _scraper(driver)
    monkeypatch.setattr(M.PJEScraper, "_doc_elements", lambda self: els)

    assert [m["sid"] for m in s._docs_meta_elementos()] == ["s0"]


# ---------------------------------------------------------------------
# semântica do JS (o motivo de o `txt` não ser innerText puro)
# ---------------------------------------------------------------------
def test_js_usa_visibilidade_para_espelhar_o_text_do_selenium():
    js = M._JS_DOCS_META
    assert "offsetParent" in js and "getClientRects" in js, (
        "o `txt` precisa ser vazio para elemento não renderizado, como o `.text` "
        "do Selenium; sem o teste de visibilidade o innerText cai para "
        "textContent e muda QUAIS documentos entram na fila"
    )
    assert "visivel(e) ? (e.innerText || '') : ''" in js


def test_js_leva_o_regex_de_documento_literal_para_o_navegador():
    r"""A string do JS é raw: `\s`/`\d` têm de chegar literais ao navegador."""
    assert r"/^\s*\d+\s*[-—]/" in M._JS_DOCS_META
