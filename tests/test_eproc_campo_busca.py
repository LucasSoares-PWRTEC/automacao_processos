# -*- coding: utf-8 -*-
"""O campo de busca do eproc: nunca o <span> do número do processo.

BUG QUE ORIGINOU ISTO (log de uma corrida no TJMG, 2026-08-19): TODA consulta
ao eproc/MG imprimia

    ♻️  eproc/MG 1102758-57.2025.8.13.0024: campo em estado inválido
        (InvalidElementStateException), recarregando painel e tentando de novo...

Causa: na página de detalhes do processo, o número é um SPAN com o mesmo id que
o input de busca de telas antigas (trecho literal do dump `tmp_4018574.html`):

    <span aria-description="Copiar número do processo." id="txtNumProcesso"
          tabindex="0">4018574-23.2025.8.26.0100</span>

`_campo_busca_processo` procurava `(By.ID, "txtNumProcesso")` PRIMEIRO, casava o
span, e `clear()` num span estoura InvalidElementStateException — 100% das vezes,
não esporádico. O tratamento então recarregava o painel: cada processo custava
DUAS páginas ao tribunal em vez de uma, com um `driver.get` extra por consulta.
Volume assim é o que faz o sistema externo nos bloquear.

Correção testada aqui: o campo certo é a pesquisa rápida da barra de topo
(#txtNumProcessoPesquisaRapida), que existe TAMBÉM na página de detalhes — e os
localizadores só aceitam elemento editável, então nenhuma isca com o id esperado
volta a derrubar a consulta.

Nada aqui sobe navegador: os dublês imitam o Selenium (InvalidElementState ao
mexer em elemento não editável, NoSuchElement quando o localizador não casa).
"""

from __future__ import annotations

import re

import pytest
from selenium.common.exceptions import (
    InvalidElementStateException,
    NoSuchElementException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from organizador.integracoes.tjsp_scraper.scrapers.eproc import EprocScraper


# ---------------------------------------------------------------------
# Dublês
# ---------------------------------------------------------------------
class ElementoFalso:
    def __init__(self, tag: str, *, texto: str = "", displayed: bool = True, **attrs):
        self.tag_name = tag
        self.attrs = {k.replace("_", "-"): v for k, v in attrs.items()}
        self._texto = texto
        self._displayed = displayed
        self.valor = ""
        self.teclas: list[str] = []

    # --- API do Selenium usada pelo scraper ---
    @property
    def text(self) -> str:
        return self._texto

    def is_displayed(self) -> bool:
        return self._displayed

    def is_enabled(self) -> bool:
        return not self.attrs.get("disabled")

    def get_attribute(self, nome: str):
        return self.attrs.get(nome)

    def clear(self):
        self._exigir_editavel()
        self.valor = ""

    def send_keys(self, texto):
        self._exigir_editavel()
        if texto == Keys.ENTER:
            self.teclas.append("ENTER")
        else:
            self.valor += str(texto)

    def _exigir_editavel(self):
        """Como o Selenium: só input/textarea editável aceita clear/send_keys."""
        if self.tag_name.lower() not in ("input", "textarea"):
            raise InvalidElementStateException(
                f"invalid element state: <{self.tag_name}> is not editable"
            )
        if self.attrs.get("readonly") or self.attrs.get("disabled"):
            raise InvalidElementStateException("invalid element state: not editable")


_RE_XPATH = re.compile(r"^//(\w+|\*)\[(.+)\]$")


def _casa_xpath(el: ElementoFalso, xpath: str) -> bool:
    """Subconjunto de XPath: //tag[@a="v" or contains(@b,"v") ...].

    Levanta NotImplementedError em forma não suportada — assim um localizador
    novo aparece como erro no teste, não como falso "não casou".
    """
    m = _RE_XPATH.match(xpath.strip())
    if not m:
        raise NotImplementedError(f"XPath fora do subconjunto do dublê: {xpath}")
    tag, predicado = m.groups()
    if tag != "*" and el.tag_name.lower() != tag.lower():
        return False
    for termo in re.split(r"\s+or\s+", predicado):
        termo = termo.strip()
        igual = re.fullmatch(r'@([\w-]+)\s*=\s*"([^"]*)"', termo)
        if igual:
            if el.attrs.get(igual.group(1)) == igual.group(2):
                return True
            continue
        contem = re.fullmatch(r'contains\(@([\w-]+)\s*,\s*"([^"]*)"\)', termo)
        if contem:
            if contem.group(2) in (el.attrs.get(contem.group(1)) or ""):
                return True
            continue
        raise NotImplementedError(f"predicado fora do subconjunto do dublê: {termo}")
    return False


class _SemAlerta:
    @property
    def alert(self):
        raise NoSuchElementException("no alert open")


class DriverFalso:
    def __init__(self, elementos: list[ElementoFalso], page_source: str = "", url: str = ""):
        self.elementos = list(elementos)
        self.page_source = page_source
        self.current_url = url
        self.navegacoes: list[str] = []
        self.switch_to = _SemAlerta()

    def get(self, url):
        self.navegacoes.append(url)

    def find_element(self, by, valor):
        achados = self.find_elements(by, valor)
        if not achados:
            raise NoSuchElementException(f"{by}={valor}")
        return achados[0]

    def find_elements(self, by, valor):
        if by == By.ID:
            return [e for e in self.elementos if e.attrs.get("id") == valor]
        if by == By.NAME:
            return [e for e in self.elementos if e.attrs.get("name") == valor]
        if by == By.XPATH:
            return [e for e in self.elementos if _casa_xpath(e, valor)]
        if by == By.CSS_SELECTOR:
            return []  # nenhum modal / nenhum link "e outros"
        raise NotImplementedError(f"estratégia não suportada no dublê: {by}")

    def execute_script(self, script, *args):
        if "readOnly" in script:  # _campo_editavel
            el = args[0]
            return (
                el.tag_name.lower() in ("input", "textarea")
                and not el.attrs.get("disabled")
                and not el.attrs.get("readonly")
            )
        if "querySelectorAll" in script:  # detectar_atribuicao
            return []
        return None


@pytest.fixture(autouse=True)
def _poll_rapido(monkeypatch):
    """WebDriverWait dorme 0,5 s entre tentativas; com 7 localizadores os casos
    de "não achou" custariam segundos de suíte. Só encurta a espera — não muda
    o comportamento testado."""
    original = WebDriverWait.__init__

    def rapido(self, driver, timeout, poll_frequency=0.01, *a, **k):
        original(self, driver, timeout, 0.01, *a, **k)

    monkeypatch.setattr(WebDriverWait, "__init__", rapido)


def _scraper(driver) -> EprocScraper:
    """EprocScraper sem __init__ (que abriria um navegador de verdade)."""
    s = object.__new__(EprocScraper)
    s.driver = driver
    s.wait = WebDriverWait(driver, 1)
    s._sigla = "MG"
    s._logado = True
    s.url = "https://eproc1g.tjmg.jus.br/eproc/"
    return s


# ---------------------------------------------------------------------
# DOM real da página de detalhes (dump tmp_4018574.html)
# ---------------------------------------------------------------------
def _span_numero_processo() -> ElementoFalso:
    return ElementoFalso(
        "span",
        id="txtNumProcesso",
        tabindex="0",
        aria_description="Copiar número do processo.",
        texto="4018574-23.2025.8.26.0100",
    )


def _input_pesquisa_rapida() -> ElementoFalso:
    return ElementoFalso(
        "input",
        id="txtNumProcessoPesquisaRapida",
        name="txtNumProcessoPesquisaRapida",
        type="search",
        placeholder="Nº de processo",
        aria_label="Pesquisar pelo número do processo (Ctrl + Shift + F)",
    )


def _pagina_de_detalhes(extras=()) -> DriverFalso:
    return DriverFalso(
        [
            _input_pesquisa_rapida(),
            _span_numero_processo(),
            ElementoFalso("div", id="fldInformacoesAdicionais_content", texto="Valor da causa"),
            *extras,
        ],
        page_source="<html>Detalhes do Processo …</html>",
        url="controlador.php?acao=processo_selecionar&num_processo=40185742320258260100",
    )


# ---------------------------------------------------------------------
# _campo_busca_processo
# ---------------------------------------------------------------------
def test_nao_pega_o_span_do_numero_do_processo():
    """A REGRESSÃO: o span#txtNumProcesso vinha antes do input de busca."""
    campo = _scraper(_pagina_de_detalhes())._campo_busca_processo(timeout=1)
    assert campo is not None, "não achou campo de busca na página de detalhes"
    assert campo.tag_name == "input"
    assert campo.attrs["id"] == "txtNumProcessoPesquisaRapida"


def test_span_sozinho_nao_passa_por_campo_de_busca():
    """Sem input nenhum, o span NÃO pode ser devolvido — melhor None do que um
    elemento que estoura no clear() (e `_parece_logado` daria falso positivo)."""
    d = DriverFalso([_span_numero_processo()], url="…processo_selecionar…")
    assert _scraper(d)._campo_busca_processo(timeout=0.1) is None


def test_input_readonly_e_recusado_e_a_busca_segue():
    """Input com o id esperado mas readonly: descarta e tenta o próximo
    localizador em vez de gastar uma requisição para descobrir no clear()."""
    isca = ElementoFalso("input", id="txtNumProcesso", readonly="true")
    d = DriverFalso(
        [isca, _input_pesquisa_rapida()],
        url="…painel_adv_listar…",
    )
    campo = _scraper(d)._campo_busca_processo(timeout=0.1)
    assert campo is not None and campo.attrs["id"] == "txtNumProcessoPesquisaRapida"


def test_input_desabilitado_e_recusado():
    d = DriverFalso([ElementoFalso("input", id="txtNumProcessoPesquisaRapida", disabled="true")])
    assert _scraper(d)._campo_busca_processo(timeout=0.1) is None


def test_dom_antigo_com_input_txtnumprocesso_ainda_funciona():
    """Compatibilidade: onde o id antigo é um INPUT de verdade, serve."""
    antigo = ElementoFalso("input", id="txtNumProcesso", placeholder="Nº do processo")
    d = DriverFalso([antigo])
    assert _scraper(d)._campo_busca_processo(timeout=0.1) is antigo


def test_campo_editavel_sobrevive_a_execute_script_quebrado():
    class DriverSemScript(DriverFalso):
        def execute_script(self, script, *args):
            raise RuntimeError("javascript error")

    d = DriverSemScript([_input_pesquisa_rapida()])
    assert _scraper(d)._campo_editavel(_input_pesquisa_rapida()) is False


# ---------------------------------------------------------------------
# consultar_processo — o custo em requisições
# ---------------------------------------------------------------------
def test_consulta_a_partir_da_pagina_de_detalhes_nao_recarrega_o_painel():
    """O ponto de todo o conserto: uma consulta = uma requisição ao tribunal.

    Vindo da página de detalhes do processo anterior (situação de TODA consulta
    depois da primeira), o scraper digita direto na pesquisa rápida — sem o
    `driver.get(self.url)` que dobrava a carga.
    """
    d = _pagina_de_detalhes()
    s = _scraper(d)

    html = s.consultar_processo("1102758-57.2025.8.13.0024", modo_docs="nenhum")

    assert html, "não devolveu o HTML dos detalhes"
    assert d.navegacoes == [], f"recarregou página sem necessidade: {d.navegacoes}"
    campo = d.find_element(By.ID, "txtNumProcessoPesquisaRapida")
    assert campo.valor == "1102758-57.2025.8.13.0024"
    assert campo.teclas == ["ENTER"]


def test_recarregamento_segue_disponivel_como_rede_de_seguranca():
    """Se o campo ficar mesmo inválido na hora de digitar (DOM trocado), o
    fallback de recarregar o painel continua valendo — só deixou de ser a regra."""
    quebrado = _input_pesquisa_rapida()

    def clear_que_estoura():
        raise InvalidElementStateException("element not interactable")

    quebrado.clear = clear_que_estoura
    d = DriverFalso([quebrado], url="…processo_selecionar…")
    s = _scraper(d)

    assert s.consultar_processo("1102758-57.2025.8.13.0024", modo_docs="nenhum") is None
    assert d.navegacoes == [s.url], "não tentou recarregar o painel uma vez"


def test_campo_ausente_reloga_uma_unica_vez():
    """Sessão caída: reloga uma vez e desiste — sem laço de login, que é o que
    queima a conta compartilhada."""
    d = DriverFalso([], url="…login…")
    s = _scraper(d)
    logins = {"n": 0}

    def login_falso():
        logins["n"] += 1
        s._logado = True

    s.login = login_falso
    assert s.consultar_processo("1102758-57.2025.8.13.0024", modo_docs="nenhum") is None
    assert logins["n"] == 1, f"relogou {logins['n']}× numa consulta"
