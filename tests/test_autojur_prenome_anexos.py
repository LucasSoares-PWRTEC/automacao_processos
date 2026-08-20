# -*- coding: utf-8 -*-
"""Pré-nomeação dos anexos enviados ao Autojur.

O anexo tem de chegar ao Autojur JÁ NOMEADO na convenção do protocolo —
numerado na ordem de envio, com "Petição - " nas peças geradas:

    1. Petição - Comprovante de pagamento
    2. Comprovante de custas
    3. ...

Antes o campo do modal vinha com o nome cru do arquivo gerado
("Comprovante de pagamento - 1102493-63.2022.8.26.0100.pdf"), sem número: a
ordem dos documentos no protocolo saía por sorte e alguém renomeava à mão os
N anexos, um por um.

Nada aqui abre janela: as funções de nome são puras, de propósito.
"""

from __future__ import annotations

import pytest

from organizador.gui import (
    _nome_base_anexo_autojur,
    _prenomear_anexo_autojur,
    _renumerar_anexos_autojur,
)


# ---------------------------------------------------------------------
# Nome base: tira extensão, nº do processo e numeração antiga
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "arquivo, esperado",
    [
        # Saída da geração de petições: "<modelo> - <nº do processo>.pdf".
        ("Comprovante de pagamento - 1102493-63.2022.8.26.0100.pdf",
         "Comprovante de pagamento"),
        # CNJ sem pontuação (o app aceita as duas formas no campo do número).
        ("Renajud - 11024936320228260100.pdf", "Renajud"),
        # Anexo escolhido à mão: só perde a extensão.
        ("Comprovante de custas.pdf", "Comprovante de custas"),
        # Vírgula no nome do modelo é válida em arquivo — não pode ser cortada.
        ("Sisbajud, Renajud e Infojud - 1102493-63.2022.8.26.0100.pdf",
         "Sisbajud, Renajud e Infojud"),
    ],
)
def test_nome_base_limpa_o_arquivo(arquivo: str, esperado: str):
    assert _nome_base_anexo_autojur(arquivo) == esperado


def test_nome_base_nao_come_hifen_do_meio_do_nome():
    """O sufixo removido é o nº do processo — não qualquer coisa após " - "."""
    assert (
        _nome_base_anexo_autojur("Petição - juntada de custas.pdf")
        == "Petição - juntada de custas"
    )


def test_peticao_gerada_ganha_prefixo():
    assert (
        _nome_base_anexo_autojur("Ciência - 1102493-63.2022.8.26.0100.pdf", peticao=True)
        == "Petição - Ciência"
    )


def test_prefixo_nao_duplica_quando_o_nome_ja_diz_peticao():
    """O modo 'documento único' gera "Petição Consolidada - <nº>.docx": não pode
    virar "Petição - Petição Consolidada"."""
    assert (
        _nome_base_anexo_autojur(
            "Petição Consolidada - 1102493-63.2022.8.26.0100.pdf", peticao=True
        )
        == "Petição Consolidada"
    )


def test_nome_que_sobra_vazio_cai_para_o_nome_do_arquivo():
    """Arquivo cujo nome é só o nº do processo: sem fallback o campo ficaria
    vazio e o envio seria barrado pela validação do modal."""
    assert _nome_base_anexo_autojur("1102493-63.2022.8.26.0100.pdf")


# ---------------------------------------------------------------------
# Numeração
# ---------------------------------------------------------------------
def test_prenomeia_na_ordem_da_lista():
    pdfs = [
        "Comprovante de pagamento - 1102493-63.2022.8.26.0100.pdf",
        "Planilha atualizada - 1102493-63.2022.8.26.0100.pdf",
    ]
    nomes = [
        _prenomear_anexo_autojur(i, p, peticao=True) for i, p in enumerate(pdfs, 1)
    ]
    assert nomes == [
        "1. Petição - Comprovante de pagamento",
        "2. Petição - Planilha atualizada",
    ]


def test_renumera_fecha_o_furo_de_quem_desmarcou_o_primeiro():
    """Desmarcar um arquivo deixaria a sequência começando em "2." no Autojur."""
    marcados = ["2. Petição - Planilha atualizada", "3. Comprovante de custas"]
    assert _renumerar_anexos_autojur(marcados) == [
        "1. Petição - Planilha atualizada",
        "2. Comprovante de custas",
    ]


def test_renumera_nao_empilha_prefixos():
    """Reabrir o modal / renumerar de novo não pode gerar "2. 1. Petição"."""
    uma_vez = _renumerar_anexos_autojur(["1. Petição - Ciência", "2. Anexo"])
    assert _renumerar_anexos_autojur(uma_vez) == uma_vez


def test_renumera_preserva_o_que_a_pessoa_editou():
    editado = ["1. peticao inicial revisada", "2. procuração assinada"]
    assert _renumerar_anexos_autojur(editado) == editado


def test_nome_prenomeado_e_valido_como_arquivo():
    """O nome vai virar nome de arquivo na cópia temporária enviada — não pode
    conter os caracteres que o Windows recusa."""
    import re

    nome = _prenomear_anexo_autojur(
        1, "Comprovante de pagamento - 1102493-63.2022.8.26.0100.pdf", peticao=True
    )
    assert not re.search(r'[<>:"/\\|?*\x00-\x1f]', nome)
