# -*- coding: utf-8 -*-
"""O 'Message:' vazio: erro na tela sem uma palavra sobre o que falhou.

Relatado com o app na tela em 27/08/2026: ao criar tarefa no Autojur, apareceu
um diálogo de erro cujo corpo inteiro era "Message:". Investigando o navegador
que ficou aberto: o PDF ESTAVA anexado, os campos preenchidos, e o que faltava
era "Requer Protocolo" — ou seja, o fluxo parou logo depois do upload, na única
espera desprotegida e sem mensagem que existe ali.

Duas causas independentes, uma coberta por cada metade deste arquivo:

1. `WebDriverWait.until()` sem `message=` levanta TimeoutException com msg
   vazia, e `str()` de exceção do Selenium é "Message: {msg}". Sem o tipo, a
   tela não dizia nem que era timeout.
2. A espera olhava `body.text`, que no Selenium é só o texto VISÍVEL. O painel
   do GED é um acordeão, e o cabeçalho dele continua marcando "0 registro(s)"
   mesmo com o arquivo na lista — ler a tela não provava o que a espera queria.
"""

from __future__ import annotations

import importlib

import pytest
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)


# ---------------------------------------------------------------------------
# 1) a mensagem que chega na tela
# ---------------------------------------------------------------------------

@pytest.fixture()
def gui():
    return importlib.import_module("organizador.gui")


def test_timeout_do_selenium_sem_mensagem_nao_vira_dialogo_vazio(gui):
    """Este é literalmente o erro que o usuário viu."""
    exc = TimeoutException("")  # o que `until()` levanta sem `message=`
    assert str(exc).strip() == "Message:", "premissa: o Selenium formata assim"

    texto = gui._descrever_erro(exc)

    assert texto == "TimeoutException (sem mensagem)"
    assert "Message:" not in texto


@pytest.mark.parametrize("exc, esperado", [
    (TimeoutException("o PDF não apareceu na lista do GED em 60 s"),
     "TimeoutException: o PDF não apareceu na lista do GED em 60 s"),
    (WebDriverException(), "WebDriverException (sem mensagem)"),
    (RuntimeError("Autojur: botão 'Salvar' não encontrado"),
     "RuntimeError: Autojur: botão 'Salvar' não encontrado"),
    (ValueError(""), "ValueError (sem mensagem)"),
])
def test_erro_sempre_diz_o_tipo_e_a_mensagem(gui, exc, esperado):
    assert gui._descrever_erro(exc) == esperado


def test_mensagem_longa_do_selenium_e_preservada(gui):
    """`NoSuchElementException` cola um link de documentação na mensagem. Não é
    para truncar nada: o texto útil vem antes, e o tipo abre a linha."""
    texto = gui._descrever_erro(NoSuchElementException("não achei #foo"))
    assert texto.startswith("NoSuchElementException: não achei #foo")


def test_prefixo_message_do_selenium_nao_aparece_duas_vezes(gui):
    """O `str()` do Selenium já vem com "Message: "; repetir isso na tela é
    ruído que empurra a mensagem útil para fora do diálogo."""
    texto = gui._descrever_erro(TimeoutException("iframe não apareceu"))
    assert texto.count("Message") == 0
    assert texto == "TimeoutException: iframe não apareceu"


# ---------------------------------------------------------------------------
# 2) a espera que olhava a tela em vez do DOM
# ---------------------------------------------------------------------------

def test_toda_espera_desprotegida_do_autojur_tem_mensagem():
    """Uma espera sem `message=` e sem try/except em volta é um diálogo vazio
    esperando para acontecer. Este teste é o que impede a reincidência."""
    from pathlib import Path

    import organizador.integracoes.tjsp_scraper.scrapers.autojur as A

    linhas = Path(A.__file__).read_text(encoding="utf-8").splitlines()
    faltando = []
    for i, linha in enumerate(linhas):
        if "WebDriverWait(" not in linha:
            continue
        bloco = "".join(linhas[i:i + 10])
        if ".until" not in bloco:
            continue  # `self.wait = WebDriverWait(...)`, sem espera aqui
        if "message=" in bloco:
            continue
        if any("try:" in linhas[j] for j in range(max(0, i - 3), i)):
            continue  # protegida: o timeout é tratado, não sobe para a tela
        faltando.append(f"{A.__file__}:{i + 1}")

    assert not faltando, (
        "esperas que podem estourar na cara do usuário sem dizer o que "
        f"aguardavam: {faltando}"
    )


def test_espera_do_pdf_conta_linhas_e_nao_compara_nomes():
    """A causa real, confirmada no navegador do usuário: **o Autojur guarda o
    nome do arquivo SEM ACENTOS**. Um "1. Petição - Penhora de cotas.pdf" entra
    na lista como "1. Peticao - Penhora de cotas.pdf", então a comparação por
    nome falhava justamente nos nomes que o app gera — e gastava os 60 s da
    espera depois de um upload que tinha funcionado.

    Contar linhas do GED não depende de como o servidor reescreve o nome, e usa
    `querySelectorAll` em vez de `.text` (que devolve só o visível, e o painel é
    um acordeão)."""
    import inspect

    import organizador.integracoes.tjsp_scraper.scrapers.autojur as A

    src = inspect.getsource(A.AutojurScraper.criar_tarefa_protocolo)
    assert "_docs_no_ged()" in src, "a espera tem de contar documentos do GED"
    assert "docs_antes" in src, "tem de comparar com a contagem ANTES do upload"
    assert "nome_base" not in src, "voltou a comparar por nome do arquivo"
    assert 'find_element(By.TAG_NAME, "body").text' not in src, (
        "voltou a depender de texto visível"
    )

    contador = inspect.getsource(A.AutojurScraper._docs_no_ged)
    assert "SEL_PAINEL_GED" in contador, "a contagem tem de ser escopada no painel"
    assert "querySelectorAll" in contador
    # Só o CÓDIGO: a docstring cita `.text` de propósito, para explicar por que
    # não o usa — assertar sobre o texto todo reprovava a própria explicação.
    # (`__doc__` não serve para recortar: vem sem a indentação do fonte.)
    corpo = contador.split('"""')
    codigo = corpo[0] + "".join(corpo[2:]) if len(corpo) >= 3 else contador
    assert ".text" not in codigo, "voltou a depender de texto visível"


def test_nome_de_arquivo_com_acento_nao_participa_da_espera():
    """Guarda de regressão do caso exato relatado: o nome com cedilha não pode
    ser o que decide se o upload terminou."""
    import inspect

    import organizador.integracoes.tjsp_scraper.scrapers.autojur as A

    src = inspect.getsource(A.AutojurScraper.criar_tarefa_protocolo)
    espera = src[src.index("docs_antes = self._docs_no_ged()"):src.index("PDF anexado")]
    assert "Path(pp).stem" not in espera, (
        "o stem do arquivo voltou para dentro da condição da espera"
    )
    # o nome pode (e deve) aparecer na MENSAGEM de erro, só não na condição.
    assert "Path(pp).name" in espera, "a mensagem de erro tem de citar o arquivo"
