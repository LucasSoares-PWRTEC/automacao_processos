# -*- coding: utf-8 -*-
"""Extração dos documentos da tabela #tblEventos (eproc) em 1 ida ao navegador.

Antes: cada linha da tabela e cada âncora de documento eram lidas com
`.find_elements` + `.text`/`get_attribute`, um elemento por vez — mesmo padrão
já eliminado do pje.py (uma ida ao WebDriver por elemento). Agora tenta 1
`execute_script` que devolve a estrutura pronta; se falhar por qualquer
motivo, cai no loop antigo (sem regressão).

Nada aqui sobe navegador: os dublês simulam o `driver.execute_script` e o
`find_elements` do Selenium.
"""

from __future__ import annotations

from organizador.integracoes.tjsp_scraper.scrapers.eproc import EprocScraper


def _scraper(driver) -> EprocScraper:
    """EprocScraper sem __init__ (que abriria um navegador de verdade)."""
    s = object.__new__(EprocScraper)
    s.driver = driver
    s._sigla = "SP"
    return s


class DriverExecuteScriptFalso:
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
# _ler_eventos_docs_js
# ---------------------------------------------------------------------
def test_ler_eventos_docs_js_extrai_estrutura_em_uma_chamada():
    resultado_js = [
        {
            "evento": "12",
            "data": "01/02/2026 10:00",
            "descricao": "Ato ordinatório",
            "docs": [
                {
                    "href": "https://eproc/controlador.php?acao=acessar_documento&doc=1",
                    "title": "Despacho\nPúblico\n10KB",
                    "label": "1",
                },
            ],
        },
        {
            "evento": "13",
            "data": "02/02/2026 11:00",
            "descricao": "Petição",
            "docs": [],
        },
    ]
    driver = DriverExecuteScriptFalso(resultado=resultado_js)
    s = _scraper(driver)

    docs = s._ler_eventos_docs_js()

    assert driver.chamadas == 1, "deveria extrair tudo numa única ida ao navegador"
    assert docs == [
        {
            "href": "https://eproc/controlador.php?acao=acessar_documento&doc=1",
            "title": "Despacho",  # só a 1ª linha do title (antes do \n)
            "label": "1",
            "evento": "12",
            "data": "01/02/2026 10:00",
            "descricao": "Ato ordinatório",
        },
    ]


def test_ler_eventos_docs_js_none_quando_execute_script_falha():
    driver = DriverExecuteScriptFalso(erro=RuntimeError("sem suporte a JS"))
    s = _scraper(driver)
    assert s._ler_eventos_docs_js() is None


def test_ler_eventos_docs_js_none_quando_resultado_e_none():
    driver = DriverExecuteScriptFalso(resultado=None)
    s = _scraper(driver)
    assert s._ler_eventos_docs_js() is None


# ---------------------------------------------------------------------
# _abrir_documentos — cai no loop antigo quando o JS falha
# ---------------------------------------------------------------------
class ElementoFalso:
    def __init__(self, texto: str = "", attrs: dict | None = None, filhos: list | None = None):
        self.text = texto
        self._attrs = attrs or {}
        self._filhos = filhos or []

    def get_attribute(self, nome):
        return self._attrs.get(nome)

    def find_elements(self, *_a, **_k):
        return self._filhos


class DriverTabelaFalso:
    """Simula find_elements(By.CSS_SELECTOR, ...) para a tabela de eventos,
    com execute_script sempre falhando (força o fallback)."""

    def __init__(self, linhas):
        self._linhas = linhas
        self.current_url = "https://eproc/detalhes"

    def execute_script(self, *_a, **_k):
        raise RuntimeError("sem execute_script neste driver falso")

    def find_elements(self, by, selector):
        if "tblEventos" in selector and "tr" in selector:
            return self._linhas
        return []


def test_abrir_documentos_usa_loop_antigo_quando_js_falha():
    anchor = ElementoFalso(
        texto="1",
        attrs={
            "href": "https://eproc/controlador.php?acao=acessar_documento&doc=1",
            "title": "Despacho\nPúblico\n10KB",
        },
    )
    td4 = ElementoFalso(filhos=[anchor])
    linha = ElementoFalso(filhos=[
        ElementoFalso("12"), ElementoFalso("01/02/2026 10:00"),
        ElementoFalso("Ato ordinatório"), ElementoFalso(""), td4,
    ])
    driver = DriverTabelaFalso([linha])
    s = _scraper(driver)

    capturado = {}

    def _baixar_falso(docs, url_detalhes):
        capturado["docs"] = docs
        capturado["url"] = url_detalhes
        return ["<html>doc 1</html>"]

    s._baixar_documentos_selenium = _baixar_falso

    resultado = s._abrir_documentos('id="tblEventos"', modo_docs="todos")

    assert resultado == ["<html>doc 1</html>"]
    assert capturado["docs"] == [
        {
            "href": "https://eproc/controlador.php?acao=acessar_documento&doc=1",
            "title": "Despacho",
            "label": "1",
            "evento": "12",
            "data": "01/02/2026 10:00",
            "descricao": "Ato ordinatório",
        },
    ]
