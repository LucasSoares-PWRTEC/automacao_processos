# -*- coding: utf-8 -*-
"""Resistência a StaleElementReferenceException no Autojur.

BUG QUE ORIGINOU ISTO (relatado com o erro na tela, 2026-08-07):
`stale element reference: stale element not found in the current frame`.

Causa: ao trocar as esperas FIXAS por esperas condicionais (v1.9.0), o código
passou a receber o controle no PRIMEIRO item de uma lista — e o PrimeFaces
substitui o bloco inteiro logo depois (autocomplete carregando sugestões,
datatable montando linhas). O padrão "acha a lista → itera lendo .text → guarda
uma referência → clica depois" então quebrava no meio. Antes o `sleep` fixo
deixava o DOM assentar e escondia o problema.

Correção testada aqui: esperar a lista PARAR de mudar e reencontrar o elemento no
momento de usá-lo, com repetição em cima de stale.

Nada aqui sobe navegador: os dublês simulam exatamente o comportamento do
Selenium (levantar StaleElementReferenceException ao tocar num elemento velho).
"""

from __future__ import annotations

import pytest
from selenium.common.exceptions import StaleElementReferenceException

from organizador.integracoes.tjsp_scraper.scrapers.autojur import (
    AutojurScraper,
    _LinhaEvento,
)


# ---------------------------------------------------------------------
# Dublês
# ---------------------------------------------------------------------
class ElementoFalso:
    """Elemento que fica OBSOLETO quando `vivo` vira False, como no Selenium."""

    def __init__(self, texto: str, filhos: list | None = None):
        self._texto = texto
        self._filhos = filhos or []
        self.vivo = True
        self.cliques = 0

    def _checar(self):
        if not self.vivo:
            raise StaleElementReferenceException("stale element not found")

    @property
    def text(self) -> str:
        self._checar()
        return self._texto

    def find_elements(self, *_a, **_k):
        self._checar()
        return self._filhos

    def find_element(self, *_a, **_k):
        self._checar()
        if not self._filhos:
            raise AssertionError("sem filhos")
        return self._filhos[0]

    def click(self):
        self._checar()
        self.cliques += 1


class DriverFalso:
    """`find_elements` devolve a lista atual; trocar `itens` simula re-render."""

    def __init__(self, itens=None):
        self.itens = list(itens or [])
        self.buscas = 0

    def find_elements(self, *_a, **_k):
        self.buscas += 1
        return list(self.itens)

    def find_element(self, *_a, **_k):
        self.buscas += 1
        if not self.itens:
            raise AssertionError("nada encontrado")
        return self.itens[0]

    def rerenderizar(self, novos):
        """Como o PrimeFaces: os antigos morrem e são substituídos."""
        for el in self.itens:
            el.vivo = False
        self.itens = list(novos)


def _scraper(driver) -> AutojurScraper:
    """AutojurScraper sem __init__ (que abriria um navegador de verdade)."""
    s = object.__new__(AutojurScraper)
    s.driver = driver
    return s


# ---------------------------------------------------------------------
# _esperar_lista_estavel
# ---------------------------------------------------------------------
def test_espera_a_lista_parar_de_crescer(monkeypatch):
    d = DriverFalso([ElementoFalso("a")])
    s = _scraper(d)
    chamadas = {"n": 0}
    real_find = d.find_elements

    def crescendo(*a, **k):
        chamadas["n"] += 1
        if chamadas["n"] == 2:  # chega mais uma linha no meio
            d.itens.append(ElementoFalso("b"))
        return real_find(*a, **k)

    monkeypatch.setattr(d, "find_elements", crescendo)
    total = s._esperar_lista_estavel(
        "css selector", "tr", ciclos=2, intervalo=0.01, timeout=5
    )
    assert total == 2, "devolveu antes de a lista assentar"


def test_lista_ja_estavel_retorna_rapido():
    d = DriverFalso([ElementoFalso("a"), ElementoFalso("b")])
    s = _scraper(d)
    import time

    t0 = time.perf_counter()
    assert s._esperar_lista_estavel("css selector", "tr", ciclos=2, intervalo=0.01) == 2
    assert time.perf_counter() - t0 < 1.0, "gastou tempo com a lista já pronta"


def test_lista_que_nunca_estabiliza_respeita_o_timeout():
    d = DriverFalso([ElementoFalso("a")])
    s = _scraper(d)

    original = d.find_elements

    def sempre_mudando(*a, **k):
        d.itens.append(ElementoFalso("x"))
        return original(*a, **k)

    d.find_elements = sempre_mudando
    import time

    t0 = time.perf_counter()
    total = s._esperar_lista_estavel(
        "css selector", "tr", ciclos=2, intervalo=0.01, timeout=0.3
    )
    assert 0.2 < time.perf_counter() - t0 < 3.0
    assert total > 0  # devolve a última contagem vista, sem levantar


def test_lista_vazia_nao_levanta():
    s = _scraper(DriverFalso([]))
    assert s._esperar_lista_estavel("css selector", "tr", ciclos=1, intervalo=0.01) == 0


# ---------------------------------------------------------------------
# _clicar_item — o coração da correção
# ---------------------------------------------------------------------
def test_clica_o_item_que_casa():
    alvo = ElementoFalso("Protocolo")
    d = DriverFalso([ElementoFalso("Outro"), alvo, ElementoFalso("Mais um")])
    s = _scraper(d)
    texto = s._clicar_item(
        lambda: d.find_elements(), lambda t, _e: t == "Protocolo", "nao achou"
    )
    assert texto == "Protocolo"
    assert alvo.cliques == 1


def test_recupera_quando_a_lista_e_trocada_no_meio_da_leitura():
    """A REGRESSÃO: o PrimeFaces re-renderiza enquanto lemos os rótulos."""
    velhos = [ElementoFalso("Outro"), ElementoFalso("Protocolo")]
    d = DriverFalso(velhos)
    s = _scraper(d)

    novo_alvo = ElementoFalso("Protocolo")
    estado = {"trocou": False}

    def achar():
        itens = d.find_elements()
        if not estado["trocou"]:
            # Mata os elementos DEPOIS de entregá-los: a leitura vai falhar.
            estado["trocou"] = True
            d.rerenderizar([ElementoFalso("Outro"), novo_alvo])
        return itens

    texto = s._clicar_item(achar, lambda t, _e: t == "Protocolo", "nao achou")
    assert texto == "Protocolo"
    assert novo_alvo.cliques == 1, "não clicou no elemento novo"


def test_recupera_quando_o_proprio_clique_fica_obsoleto():
    alvo_velho = ElementoFalso("Protocolo")
    d = DriverFalso([alvo_velho])
    s = _scraper(d)
    alvo_novo = ElementoFalso("Protocolo")

    def click_que_morre():
        d.rerenderizar([alvo_novo])
        raise StaleElementReferenceException("morreu no clique")

    alvo_velho.click = click_que_morre
    assert s._clicar_item(
        lambda: d.find_elements(), lambda t, _e: t == "Protocolo", "nao achou"
    ) == "Protocolo"
    assert alvo_novo.cliques == 1


def test_primeiro_match_por_padrao():
    a, b = ElementoFalso("Protocolo"), ElementoFalso("Protocolo")
    d = DriverFalso([a, b])
    s = _scraper(d)
    s._clicar_item(lambda: d.find_elements(), lambda t, _e: t == "Protocolo", "x")
    assert a.cliques == 1 and b.cliques == 0


def test_ultimo_match_quando_pedido():
    """A seleção de Lote pegava a ÚLTIMA opção que casava (laço sem `break`);
    trocar isso mudaria silenciosamente qual lote é escolhido."""
    a, b = ElementoFalso("06/08"), ElementoFalso("07/08")
    d = DriverFalso([a, b])
    s = _scraper(d)
    texto = s._clicar_item(
        lambda: d.find_elements(), lambda t, _e: bool(t), "x", ultimo=True
    )
    assert texto == "07/08"
    assert b.cliques == 1 and a.cliques == 0


def test_erro_util_quando_nada_casa():
    d = DriverFalso([ElementoFalso("Outro")])
    s = _scraper(d)
    with pytest.raises(RuntimeError, match="evento 'X' não encontrado"):
        s._clicar_item(
            lambda: d.find_elements(), lambda _t, _e: False,
            "evento 'X' não encontrado",
        )


def test_desiste_apos_as_tentativas_com_lista_sempre_obsoleta():
    d = DriverFalso([ElementoFalso("Protocolo")])
    s = _scraper(d)

    def achar_sempre_morto():
        el = ElementoFalso("Protocolo")
        el.vivo = False  # já nasce obsoleto
        return [el]

    with pytest.raises(RuntimeError, match="re-renderizada"):
        s._clicar_item(achar_sempre_morto, lambda t, _e: True, "algo", tentativas=2)


def test_predicado_recebe_o_elemento_para_checar_o_selo():
    """O autocomplete de Evento só aceita a linha com o selo 'T'
    (span.label-success), que exige olhar DENTRO do elemento."""
    selo = ElementoFalso("T")
    com_selo = ElementoFalso("Protocolo", filhos=[selo])
    sem_selo = ElementoFalso("Protocolo", filhos=[])
    d = DriverFalso([sem_selo, com_selo])
    s = _scraper(d)

    def casa(t, el):
        return t == "Protocolo" and bool(el.find_elements("css selector", "span"))

    s._clicar_item(lambda: d.find_elements(), casa, "nao achou")
    assert com_selo.cliques == 1 and sem_selo.cliques == 0


# ---------------------------------------------------------------------
# _sem_stale
# ---------------------------------------------------------------------
def test_sem_stale_repete_e_devolve():
    tentativas = {"n": 0}

    def instavel():
        tentativas["n"] += 1
        if tentativas["n"] < 3:
            raise StaleElementReferenceException("ainda não")
        return "pronto"

    assert AutojurScraper._sem_stale(instavel, espera=0.01) == "pronto"
    assert tentativas["n"] == 3


def test_sem_stale_propaga_depois_de_esgotar():
    def sempre_stale():
        raise StaleElementReferenceException("nunca estabiliza")

    with pytest.raises(StaleElementReferenceException):
        AutojurScraper._sem_stale(sempre_stale, tentativas=2, espera=0.01)


def test_sem_stale_nao_engole_outros_erros():
    """Só stale é motivo para repetir; o resto tem de subir na hora."""
    chamadas = {"n": 0}

    def erro_de_verdade():
        chamadas["n"] += 1
        raise IndexError("índice fora do intervalo")

    with pytest.raises(IndexError):
        AutojurScraper._sem_stale(erro_de_verdade, tentativas=3, espera=0.01)
    assert chamadas["n"] == 1, "repetiu um erro que não era stale"


# ---------------------------------------------------------------------
# _LinhaEvento
# ---------------------------------------------------------------------
def test_linha_evento_le_da_linha_e_clica_na_celula():
    """No PrimeFaces o texto está no `tr`, mas clicar no `tr` não seleciona."""
    celula = ElementoFalso("td")
    linha = ElementoFalso("Protocolo T", filhos=[celula])
    par = _LinhaEvento(linha, celula)

    assert par.text == "Protocolo T"
    assert par.find_elements("css selector", "span") == [celula]
    par.click()
    assert celula.cliques == 1 and linha.cliques == 0


def test_linha_evento_propaga_stale_para_o_retry_funcionar():
    linha = ElementoFalso("Protocolo T")
    par = _LinhaEvento(linha, linha)
    linha.vivo = False
    with pytest.raises(StaleElementReferenceException):
        _ = par.text
