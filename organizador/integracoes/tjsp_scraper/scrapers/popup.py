# -*- coding: utf-8 -*-
"""Detector conservador do pop-up de "atribuir/vincular o processo ao advogado".

Em alguns sistemas, abrir um processo no qual o advogado/escritório ainda não
tem vínculo dispara um modal avisando que, se prosseguir, o processo será
ATRIBUÍDO a ele (habilitação/vinculação). O usuário pediu: ao ver esse pop-up,
PARAR o enriquecimento daquele processo e registrar a observação.

Regra de ouro: **NUNCA confirmar** — não clicar em Sim/Confirmar/Continuar/OK/
Habilitar/Prosseguir. Quando detecta um modal/alert com cara de atribuição, tenta
fechá-lo com segurança (botão Não/Cancelar/Fechar/Sair ou ESC) e devolve o texto
observado para o chamador abortar o processo.

Como não se sabe ao certo em qual sistema/versão o pop-up aparece, a detecção é
genérica (alert JS nativo + vários seletores de modal) e conservadora: no pior
caso pula um processo a mais — jamais atribui um processo por engano.
"""

from __future__ import annotations

from unidecode import unidecode

# Um texto "parece atribuição" quando combina um VERBO de vinculação com um ALVO
# (advogado/processo/autos…). Exigir as duas coisas reduz falso-positivo frente a
# usar um termo isolado (ex.: "atribuição de competência" não casa sozinho).
_VERBOS = (
    "atribu", "vincul", "habilit", "associ", "incluid", "incluir",
    "passa a constar", "passara a constar", "constara", "responsav",
)
_ALVOS = ("advogad", "procurador", "processo", "autos", "polo", "parte")

# Rótulos de botão: jamais clicar nos POSITIVOS; preferir os NEGATIVOS p/ fechar.
_POS = ("sim", "confirmar", "continuar", "prosseguir", "habilitar", "ok",
        "aceitar", "concordo", "avancar", "avançar")
_NEG = ("nao", "não", "cancelar", "fechar", "sair", "voltar", "recusar", "discordo")

_SELETORES_MODAL = (
    "[role=dialog]", ".ui-dialog", ".modal.in", ".modal.show",
    ".ui-confirm-dialog", ".swal2-popup", ".rf-pp-cntr", ".rf-pp-cnt",
    ".rich-mpnl-panel", ".rich-mpnl-body", ".dialogContent",
    "div[id*='modal']", "div[id*='Modal']",
    "div[id*='dialog']", "div[id*='Dialog']",
    "div[id*='popup']", "div[id*='Popup']",
)


class AtribuicaoDetectadaError(Exception):
    """Pop-up de atribuição/vínculo detectado; enriquecimento abortado p/ o processo."""

    def __init__(self, observacao: str):
        super().__init__(observacao)
        self.observacao = observacao


def _parece_atribuicao(texto: str) -> bool:
    t = unidecode(texto or "").lower()
    if not t.strip():
        return False
    return any(v in t for v in _VERBOS) and any(a in t for a in _ALVOS)


def _resumir(texto: str, limite: int = 400) -> str:
    t = " ".join((texto or "").split())
    return t[:limite] + ("…" if len(t) > limite else "")


def _fechar_seguro(driver, modal_el) -> None:
    """Fecha o modal sem confirmar: clica num botão negativo (Não/Cancelar/Fechar)
    ou, na falta de um, pressiona ESC. NUNCA clica num botão positivo."""
    from selenium.webdriver.common.by import By
    try:
        botoes = modal_el.find_elements(
            By.XPATH,
            ".//button | .//a | .//input[@type='button'] | "
            ".//input[@type='submit'] | .//span[@role='button']",
        )
    except Exception:
        botoes = []
    for b in botoes:
        try:
            rotulo = unidecode((b.text or b.get_attribute("value") or "")).strip().lower()
        except Exception:
            continue
        if not rotulo or any(p in rotulo for p in _POS):
            continue  # vazio ou positivo → não toca
        if any(n in rotulo for n in _NEG):
            try:
                driver.execute_script("arguments[0].click();", b)
                return
            except Exception:
                pass
    # Sem botão seguro: ESC (não confirma nada). Se nem isso funcionar, o
    # chamador segue para o próximo processo e a navegação seguinte limpa o modal.
    try:
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.keys import Keys
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
    except Exception:
        pass


def detectar_atribuicao(driver) -> str | None:
    """Se houver um pop-up de atribuição/vínculo, fecha-o com segurança e devolve
    o texto observado; senão devolve None. Nunca confirma nada."""
    # 1) Alert JS nativo (window.confirm/alert). dismiss() = Cancelar/Não.
    try:
        alerta = driver.switch_to.alert
        texto = (alerta.text or "").strip()
        try:
            alerta.dismiss()
        except Exception:
            pass
        if _parece_atribuicao(texto):
            return "[alert] " + _resumir(texto)
    except Exception:
        pass  # sem alert nativo

    # 2) Modais no DOM
    from selenium.webdriver.common.by import By
    for sel in _SELETORES_MODAL:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
        except Exception:
            continue
        for el in els:
            try:
                if not el.is_displayed():
                    continue
                txt = el.text or ""
            except Exception:
                continue
            if _parece_atribuicao(txt):
                obs = _resumir(txt)
                _fechar_seguro(driver, el)
                return obs
    return None
