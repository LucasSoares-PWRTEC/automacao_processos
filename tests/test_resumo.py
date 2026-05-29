# -*- coding: utf-8 -*-
"""Geração do resumo textual (_resumo_txt)."""

from pathlib import Path

import organizar_processos as op


def test_resumo_txt_contem_secoes_principais(texto_detalhes):
    _, proc = op.parse_pdf(texto_detalhes, arquivo=Path("detalhes.pdf"))
    txt = op._resumo_txt(proc)
    assert "RESUMO DO PROCESSO" in txt
    assert "4004459-04.2026.8.26.0248" in txt
    assert "PARTES" in txt
    assert "EMPRESA EXEMPLO LTDA" in txt
    assert "FULANO DE TAL" in txt
    assert "HISTÓRICO COMPLETO DE EVENTOS" in txt
    assert txt.endswith("\n")


def test_resumo_txt_processo_vazio():
    # Não deve quebrar com um processo sem dados.
    txt = op._resumo_txt(op.Processo())
    assert "RESUMO DO PROCESSO" in txt
    assert "(nenhuma parte identificada)" in txt
    assert "(nenhum evento identificado)" in txt


def test_fmt_advogados():
    from organizar_processos import Advogado
    advs = [Advogado(nome="ADV UM", oab="SP1"), Advogado(nome="ADV DOIS", oab="MG2")]
    assert op._fmt_advogados(advs) == "ADV UM (SP1), ADV DOIS (MG2)"
