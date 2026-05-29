# -*- coding: utf-8 -*-
"""Classificação do tipo de PDF (detectar_tipo_pdf)."""

import organizar_processos as op


def test_detecta_detalhes(texto_detalhes):
    assert op.detectar_tipo_pdf(texto_detalhes) == "detalhes"


def test_detecta_detalhes_por_partes_e_eventos():
    # Sem "Consulta Processual"/"Detalhes do Processo", mas com partes+eventos+CNJ.
    texto = (
        "Partes e Representantes\nAUTOR RÉU\n"
        "Eventos\n4004459-04.2026.8.26.0248\n"
    )
    assert op.detectar_tipo_pdf(texto) == "detalhes"


def test_detecta_extrato(texto_extrato):
    assert op.detectar_tipo_pdf(texto_extrato) == "extrato"


def test_detecta_ato(texto_ato):
    assert op.detectar_tipo_pdf(texto_ato) == "ato"


def test_detecta_desconhecido():
    assert op.detectar_tipo_pdf("texto qualquer sem marcadores") == "desconhecido"
    assert op.detectar_tipo_pdf("") == "desconhecido"


def test_extrato_tem_prioridade_sobre_detalhes(texto_extrato):
    # Extrato contém "Processo Eletrônico" + "Chave para consulta" -> deve vencer.
    assert "Chave para consulta" in texto_extrato
    assert op.detectar_tipo_pdf(texto_extrato) == "extrato"
