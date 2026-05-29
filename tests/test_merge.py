# -*- coding: utf-8 -*-
"""Mesclagem de processos vindos de PDFs diferentes (_merge_processos)."""

import organizar_processos as op
from organizar_processos import Advogado, AtoDocumento, Evento, Parte, Processo


def test_merge_preenche_campos_vazios():
    base = Processo(numero="4004459-04.2026.8.26.0248")
    novo = Processo(classe="EXECUÇÃO", valor_causa="R$ 1.000,00", tribunal="TJSP")
    op._merge_processos(base, novo, "extrato")
    assert base.classe == "EXECUÇÃO"
    assert base.valor_causa == "R$ 1.000,00"
    assert base.tribunal == "TJSP"


def test_merge_nao_sobrescreve_campo_preenchido():
    base = Processo(numero="X", classe="ORIGINAL")
    novo = Processo(classe="NOVO")
    op._merge_processos(base, novo, "extrato")
    assert base.classe == "ORIGINAL"


def test_merge_partes_escolhe_maior_score():
    base = Processo(partes=[Parte(papel="AUTOR", nome="A")])
    novo = Processo(partes=[
        Parte(papel="AUTOR", nome="A", documento="123.456.789-00",
              advogados=[Advogado(oab="SP1")]),
        Parte(papel="RÉU", nome="B"),
    ])
    op._merge_processos(base, novo, "detalhes")
    # Conjunto de partes do 'novo' tem score maior -> substitui.
    assert len(base.partes) == 2


def test_merge_partes_complementa_documento_e_advogados():
    base = Processo(partes=[
        Parte(papel="AUTOR", nome="EMPRESA", advogados=[Advogado(oab="SP1")]),
        Parte(papel="RÉU", nome="FULANO"),
    ])
    novo = Processo(partes=[
        Parte(papel="AUTOR", nome="EMPRESA", documento="12.345.678/0001-90"),
        Parte(papel="RÉU", nome="FULANO"),
    ])
    op._merge_processos(base, novo, "detalhes")
    autor = base.partes[0]
    assert autor.documento == "12.345.678/0001-90"
    assert any(a.oab == "SP1" for a in autor.advogados)


def test_merge_eventos_mantem_lista_mais_longa():
    base = Processo(eventos=[Evento(numero="1")])
    novo = Processo(eventos=[Evento(numero="1"), Evento(numero="2")])
    op._merge_processos(base, novo, "detalhes")
    assert len(base.eventos) == 2


def test_merge_atos_uniao_sem_duplicar():
    ato = AtoDocumento(codigo_verificador="V1", arquivo="a.pdf")
    base = Processo(atos=[ato])
    novo = Processo(atos=[
        AtoDocumento(codigo_verificador="V1", arquivo="a.pdf"),  # duplicado
        AtoDocumento(codigo_verificador="V2", arquivo="b.pdf"),  # novo
    ])
    op._merge_processos(base, novo, "ato")
    chaves = {(a.codigo_verificador, a.arquivo) for a in base.atos}
    assert chaves == {("V1", "a.pdf"), ("V2", "b.pdf")}


def test_merge_tags_e_arquivos_uniao():
    base = Processo(tags=["Idoso"], arquivos_origem=["a.pdf"])
    novo = Processo(tags=["Idoso", "Prioridade"], arquivos_origem=["b.pdf"])
    op._merge_processos(base, novo, "detalhes")
    assert sorted(base.tags) == ["Idoso", "Prioridade"]
    assert sorted(base.arquivos_origem) == ["a.pdf", "b.pdf"]
