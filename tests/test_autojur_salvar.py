# -*- coding: utf-8 -*-
"""Clique em "Salvar" no diálogo de tarefa do Autojur.

BUG QUE ORIGINOU ISTO (relatado com o erro na tela, 2026-08-20):
`Autojur: botão 'Salvar' não encontrado.` sempre que `salvar=True`.

Causa, lida do HTML real do rodapé do diálogo (mdAdicionarTarefa.jsf):

    <a id="form-adicionar-tarefa:btn-salvar" href="#"
       class="ui-commandlink ui-widget btn btn-md btn-primary"
       onclick="verificarConexaoEExecutar('form-adicionar-tarefa',
                'btn-salvar-real'); return false;...">
      <i class="far fa-save"></i> Salvar</a>

O botão é um <a> (commandLink do PrimeFaces) e o código procurava só
`//button[...]` — nenhum dos dois seletores podia casar. Perto dele existem
outros "Salvar" que NÃO servem: o do painel do GED (<span> com
ui-state-disabled) e os de modais fechados do diálogo; por isso o alvo é o id
estável + filtro de visível/habilitado.

Nada aqui sobe navegador: os dublês respondem como o Selenium responderia para
o DOM real acima.
"""

from __future__ import annotations

import pytest

from organizador.integracoes.tjsp_scraper.scrapers import autojur as mod
from organizador.integracoes.tjsp_scraper.scrapers.autojur import AutojurScraper


class ElementoFalso:
    def __init__(self, tag="a", id="", texto="", classes="", visivel=True,
                 disabled=None):
        self.tag = tag
        self.id = id
        self.texto = texto
        self.classes = classes
        self.visivel = visivel
        self.disabled = disabled
        self.cliques = 0

    def is_displayed(self):
        return self.visivel

    def get_attribute(self, nome):
        return {"class": self.classes, "disabled": self.disabled,
                "id": self.id}.get(nome)

    def click(self):
        self.cliques += 1


class DriverFalso:
    """Responde find_element(By.ID)/find_elements(By.XPATH) como o navegador
    faria para o rodapé do diálogo, e execute_script para o resto."""

    def __init__(self, elementos, dialogo_fecha=True):
        self.elementos = list(elementos)
        self.dialogo_fecha = dialogo_fecha
        self.saiu_do_iframe = 0

        class _Switch:
            def __init__(self, dono):
                self._dono = dono

            def default_content(self):
                self._dono.saiu_do_iframe += 1

        self.switch_to = _Switch(self)

    def find_element(self, _by, valor):
        for el in self.elementos:
            if el.id == valor:
                return el
        from selenium.common.exceptions import NoSuchElementException
        raise NoSuchElementException(valor)

    def find_elements(self, _by, _valor):
        # O XP_BTN_SALVAR casa qualquer tag cujo texto normalizado é "Salvar".
        return [el for el in self.elementos if el.texto.strip() == "Salvar"]

    def execute_script(self, script, *args):
        if "scrollIntoView" in script:
            return None
        if "querySelector('iframe')" in script:
            return self.dialogo_fecha
        if "querySelectorAll" in script:
            return [f"{el.id or el.tag}={el.texto}" for el in self.elementos
                    if el.visivel]
        if ".click()" in script:
            args[0].cliques += 1
            return None
        return None


def _scraper(driver, **overrides) -> AutojurScraper:
    """AutojurScraper sem __init__ (que abriria um navegador de verdade)."""
    s = object.__new__(AutojurScraper)
    s.driver = driver
    s._esperar_sem_overlay = lambda *a, **k: True
    s._screenshot = lambda *a, **k: None
    s._iframe_tarefa = lambda *a, **k: None
    s._mensagens_erro = lambda: ""
    for nome, valor in overrides.items():
        setattr(s, nome, valor)
    return s


# ---------------------------------------------------------------------
# _achar_botao_salvar
# ---------------------------------------------------------------------
def _rodape_real(dialogo_fecha=True) -> tuple[DriverFalso, ElementoFalso]:
    """O rodapé como está no HTML real, com os vizinhos que atrapalham."""
    salvar = ElementoFalso(
        "a", mod.SEL_BTN_SALVAR, "Salvar",
        "ui-commandlink ui-widget btn btn-md btn-primary")
    outros = [
        ElementoFalso("a", "form-adicionar-tarefa:btn-salvar-visualizar",
                      "Salvar e Visualizar", "ui-commandlink btn"),
        ElementoFalso("span", "ged-file-ui-tarefa:formTipoDocumento:btn-salvar-ged",
                      "Salvar", "ui-commandlink ui-state-disabled btn",
                      visivel=False),
        ElementoFalso("a", "j_idt417", "Salvar", "ui-commandlink btn",
                      visivel=False),
        ElementoFalso("a", "form-adicionar-tarefa:j_idt274", "Fechar", "btn"),
    ]
    return DriverFalso([*outros[:1], salvar, *outros[1:]], dialogo_fecha), salvar


def test_acha_o_link_salvar_do_rodape():
    """A REGRESSÃO: <a> commandLink, que o seletor de <button> nunca achava."""
    d, salvar = _rodape_real()
    assert _scraper(d)._achar_botao_salvar() is salvar


def test_ignora_o_salvar_desabilitado_do_ged():
    """O <span> do painel do GED tem o mesmo texto e ui-state-disabled: clicar
    nele não salva nada (e, visível, seria escolhido antes do certo)."""
    ged = ElementoFalso("span", "ged-file-ui-tarefa:formTipoDocumento:btn-salvar-ged",
                        "Salvar", "ui-commandlink ui-state-disabled btn")
    salvar = ElementoFalso("a", "outro-id-qualquer:btn-salvar-novo", "Salvar",
                           "ui-commandlink btn")
    assert _scraper(DriverFalso([ged, salvar]))._achar_botao_salvar() is salvar


def test_ignora_o_salvar_de_modal_fechado():
    escondido = ElementoFalso("a", "j_idt417", "Salvar", "btn", visivel=False)
    assert _scraper(DriverFalso([escondido]))._achar_botao_salvar() is None


def test_aceita_button_se_a_tela_mudar_de_componente():
    """O id é a primeira via, mas o fallback por texto não olha a tag: se o
    Autojur trocar o commandLink por um <button>, continua achando."""
    botao = ElementoFalso("button", "", "Salvar", "ui-button")
    assert _scraper(DriverFalso([botao]))._achar_botao_salvar() is botao


# ---------------------------------------------------------------------
# _clicar_salvar
# ---------------------------------------------------------------------
def test_clica_e_confirma_pelo_fechamento_do_dialogo():
    d, salvar = _rodape_real(dialogo_fecha=True)
    logs: list[str] = []
    _scraper(d)._clicar_salvar(log=logs.append)
    assert salvar.cliques == 1
    assert d.saiu_do_iframe >= 1, "esperou o diálogo de dentro do próprio iframe"
    assert any("salva" in l for l in logs)


def test_erro_quando_o_dialogo_nao_fecha():
    """Validação de campo obrigatório recusa o salvamento e mantém o diálogo:
    dizer 'tarefa salva' aí seria mentir para quem usa."""
    d, salvar = _rodape_real(dialogo_fecha=False)
    s = _scraper(d, _mensagens_erro=lambda: "Conteúdo é obrigatório")
    with pytest.raises(RuntimeError, match="não fechou"):
        s._clicar_salvar(log=lambda _m: None)
    assert salvar.cliques == 1


def test_erro_lista_os_botoes_visiveis_quando_nao_acha():
    """Se o rodapé mudar de novo, o erro tem de dizer o que havia na tela."""
    d = DriverFalso([ElementoFalso("a", "form-adicionar-tarefa:j_idt274",
                                   "Fechar", "btn")])
    with pytest.raises(RuntimeError, match="Fechar"):
        _scraper(d)._clicar_salvar(log=lambda _m: None)


def test_clique_interceptado_reencontra_e_repete():
    d, salvar = _rodape_real()
    from selenium.common.exceptions import ElementClickInterceptedException

    tentativas = {"n": 0}

    def click_interceptado():
        tentativas["n"] += 1
        if tentativas["n"] == 1:
            raise ElementClickInterceptedException("spinner na frente")
        salvar.cliques += 1

    salvar.click = click_interceptado
    _scraper(d)._clicar_salvar(log=lambda _m: None)
    assert salvar.cliques == 1 and tentativas["n"] == 2
