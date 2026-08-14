# -*- coding: utf-8 -*-
"""Filtro indexado da aba Pesquisa.

`filtrar_processos` remonta o texto de busca de toda a base a cada tecla
digitada. Com ~3.200 processos isso é ~13 mil operações de string por caractere.
O índice pré-computado precisa dar EXATAMENTE o mesmo resultado — é isso que
estes testes garantem.
"""

from __future__ import annotations

from organizador.indice_processos import ResumoProcesso
from organizador.pesquisa import (
    filtrar_processos,
    filtrar_processos_indexado,
    indexar_para_filtro,
    texto_filtro_processo,
)


def _base() -> list[ResumoProcesso]:
    return [
        ResumoProcesso(numero="1001234-56.2025.8.26.0100", classe="Execução",
                       autor="BANCO ALFA S.A.", reu="João da Silva"),
        ResumoProcesso(numero="2005678-90.2024.8.13.0024", classe="Monitória",
                       autor="Construtora Beta", reu="MARIA SOUZA"),
        ResumoProcesso(numero="3009999-11.2023.8.26.0405", classe="Busca e Apreensão",
                       autor="Banco Gama", reu="Empresa Delta LTDA"),
    ]


def test_indice_alinhado_com_a_lista():
    procs = _base()
    textos = indexar_para_filtro(procs)
    assert len(textos) == len(procs)
    assert textos == [texto_filtro_processo(p) for p in procs]


def test_resultado_identico_ao_filtro_direto():
    procs = _base()
    textos = indexar_para_filtro(procs)
    for termo in ("", "  ", "banco", "BANCO", "silva", "8.26", "2024",
                  "monitória", "empresa delta", "nao existe", "1001234"):
        assert (
            filtrar_processos_indexado(procs, textos, termo)
            == filtrar_processos(procs, termo)
        ), f"divergiu no termo {termo!r}"


def test_filtro_vazio_devolve_tudo():
    procs = _base()
    textos = indexar_para_filtro(procs)
    assert filtrar_processos_indexado(procs, textos, "") == procs
    assert filtrar_processos_indexado(procs, textos, "   ") == procs


def test_busca_case_insensitive_em_numero_classe_autor_e_reu():
    procs = _base()
    textos = indexar_para_filtro(procs)
    assert [p.numero for p in filtrar_processos_indexado(procs, textos, "BANCO")] == [
        procs[0].numero, procs[2].numero,
    ]
    assert filtrar_processos_indexado(procs, textos, "maria souza") == [procs[1]]
    assert filtrar_processos_indexado(procs, textos, "monitória") == [procs[1]]
    assert filtrar_processos_indexado(procs, textos, "3009999") == [procs[2]]


def test_indice_dessincronizado_recalcula_em_vez_de_mentir():
    """Se a lista mudou sem reindexar, é melhor pagar o custo do que devolver o
    processo errado (o índice é posicional)."""
    procs = _base()
    textos = indexar_para_filtro(procs)
    procs.append(
        ResumoProcesso(numero="4000000-00.2026.8.26.0001", classe="Nova",
                       autor="BANCO OMEGA", reu="Alguém")
    )
    resultado = filtrar_processos_indexado(procs, textos, "omega")
    assert [p.numero for p in resultado] == ["4000000-00.2026.8.26.0001"]
    assert resultado == filtrar_processos(procs, "omega")


def test_indice_vazio_com_lista_cheia_nao_perde_processo():
    procs = _base()
    assert filtrar_processos_indexado(procs, [], "banco") == filtrar_processos(procs, "banco")


def test_listas_vazias():
    assert filtrar_processos_indexado([], [], "banco") == []
    assert indexar_para_filtro([]) == []


def test_campos_none_nao_estouram():
    """Resumos vindos de metadados antigos podem ter campos ausentes."""
    procs = [ResumoProcesso(numero="", classe="", autor="", reu="")]
    textos = indexar_para_filtro(procs)
    assert filtrar_processos_indexado(procs, textos, "qualquer") == []
    assert filtrar_processos_indexado(procs, textos, "") == procs


def test_linhas_da_tree_funcionam_para_resumo_e_para_processo():
    """`linhas_tree_processos` recebe as duas formas: o ResumoProcesso (lista da
    aba Pesquisa) e o Processo completo. O prazo vem de campos diferentes em
    cada uma, e uma delas não tem `partes`."""
    from organizador.models import Parte, Prazo, Processo
    from organizador.pesquisa import linhas_tree_processos

    resumo = ResumoProcesso(
        numero="1001234-56.2025.8.26.0100", classe="Execução",
        autor="Autor R", reu="Réu R", prazo_fim="10/03/2026",
    )
    completo = Processo(
        numero="2005678-90.2024.8.13.0024", classe="Monitória",
        partes=[Parte(papel="EXEQUENTE", nome="Autor P"),
                Parte(papel="EXECUTADO", nome="Réu P")],
        prazos_abertos=[Prazo(parte_nome="Réu P", inicio="01/03/2026", fim="15/03/2026")],
    )

    (_id_r, valores_r, _tag_r), (_id_c, valores_c, _tag_c) = linhas_tree_processos(
        [resumo, completo]
    )
    assert valores_r[2:4] == ("Autor R", "Réu R")
    assert valores_r[-1] == "10/03/2026", "prazo do ResumoProcesso não chegou à coluna"
    assert valores_c[2:4] == ("Autor P", "Réu P")
    assert valores_c[-1] == "15/03/2026", "prazo do Processo completo não chegou à coluna"


def test_resumo_sem_prazo_fica_em_branco():
    from organizador.pesquisa import _prazo_de

    assert _prazo_de(ResumoProcesso(numero="1", prazo_fim="")) == ""
    assert _prazo_de(ResumoProcesso(numero="1", prazo_fim="10/03/2026")) == "10/03/2026"
