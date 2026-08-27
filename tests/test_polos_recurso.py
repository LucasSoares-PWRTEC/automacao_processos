# -*- coding: utf-8 -*-
"""Papéis de RECURSO/RECONVENÇÃO contam como polo, e a ordem da lista importa.

Três tabelas descrevem o mesmo conceito ("este papel é polo ativo ou passivo?"):
`padroes.PAPEIS_ATIVO/PASSIVO` (a que o app inteiro consulta via `_autor`/
`_reus_joined`), `parser_pje._PAPEIS_ATIVO_PJE/_PASSIVO_PJE` (dedupe por polo) e
os VALORES de `parser_esaj._PAPEL_ESAJ` (canonização das abreviações). Em
27/08/2026 as três discordavam: os parsers produziam APELANTE/APELADO/
RECORRENTE/RECORRIDO e `padroes` não os conhecia, então o processo era lido como
se não tivesse parte alguma — 4 processos do TJPA/TJSP apareciam "sem réu E sem
credor" com as duas partes salvas no disco.
"""

from __future__ import annotations

import re

from organizador.models import Parte, Processo
from organizador.organizacao import _autor, _reus_joined
from organizador.padroes import (
    PAPEIS_ATIVO_UP, PAPEIS_PASSIVO_UP, RE_EXTRATO_PARTE, RE_PARTE_ATO,
)


def test_polos_de_recurso_sao_lidos_como_partes():
    """O caso real do TJPA: APELANTE/APELADO e nada mais."""
    proc = Processo(numero="0001461-25.2018.8.14.0094", classe="MONITÓRIA")
    proc.partes = [
        Parte(papel="APELANTE", nome="DIRECIONAL DIAMANTE EMPREENDIMENTOS"),
        Parte(papel="APELADO", nome="MARCIONILA DUARTE DE LIMA"),
    ]
    assert _autor(proc) == "DIRECIONAL DIAMANTE EMPREENDIMENTOS"
    assert _reus_joined(proc) == "MARCIONILA DUARTE DE LIMA"


def test_reconvencao_e_interpelacao_tambem():
    proc = Processo(numero="1002254-87.2025.8.26.0248", classe="Procedimento Comum Cível")
    proc.partes = [
        Parte(papel="RECONVINTE", nome="Fabiano Aparecido dos Santos"),
        Parte(papel="RECONVINDO", nome="Villa Vic Indaiatuba Condominio"),
    ]
    assert _autor(proc) == "Fabiano Aparecido dos Santos"
    assert _reus_joined(proc) == "Villa Vic Indaiatuba Condominio"


def test_terceiros_nunca_viram_polo():
    """Virar "réu" no endereçamento da petição é pior que ficar vazio: só o polo
    de verdade entra. Estes rótulos EXISTEM na base e devem seguir de fora."""
    for papel in ("INTERESSADO", "TERCEIRO INTERESSADO", "INTERESDO.", "INTERESDA.",
                  "PERITO", "TESTEMUNHA", "ADVOGADO", "SOC. ADVOGADOS",
                  "ARREMTERC", "ADM-TERC.", "FIADTERC", "INVTARDA",
                  "CREDOR HIPOTECÁRIO", "FALECIDO", "GESTOR", "FISCAL", "VÍTIMA"):
        assert papel not in PAPEIS_ATIVO_UP, papel
        assert papel not in PAPEIS_PASSIVO_UP, papel


def test_nenhum_papel_em_dois_polos():
    assert not (PAPEIS_ATIVO_UP & PAPEIS_PASSIVO_UP)


def test_parsers_nao_produzem_papel_que_padroes_ignora():
    """A regressão original: o parser canoniza para um papel que `padroes` não
    conhece. Trava as três tabelas juntas."""
    from organizador.integracoes.tjsp_scraper.parser_esaj import _PAPEL_ESAJ
    from organizador.integracoes.tjsp_scraper.parser_pje import (
        _PAPEIS_ATIVO_PJE, _PAPEIS_PASSIVO_PJE,
    )
    conhecidos = PAPEIS_ATIVO_UP | PAPEIS_PASSIVO_UP
    # Os polos privados do parser do PJe são um SUBCONJUNTO dos de padroes.
    assert _PAPEIS_ATIVO_PJE <= PAPEIS_ATIVO_UP, _PAPEIS_ATIVO_PJE - PAPEIS_ATIVO_UP
    assert _PAPEIS_PASSIVO_PJE <= PAPEIS_PASSIVO_UP, _PAPEIS_PASSIVO_PJE - PAPEIS_PASSIVO_UP
    # O e-SAJ canoniza para polo OU para um terceiro declarado; nada no meio.
    terceiros_ok = {"TERCEIRO INTERESSADO", "INTERESSADO", "VÍTIMA"}
    for abrev, canonico in _PAPEL_ESAJ.items():
        assert canonico in conhecidos or canonico in terceiros_ok, (abrev, canonico)


def test_ordem_da_alternacao_nao_deixa_RE_engolir_RECORRIDO():
    """"RE" antes de "RECORRIDO" na lista faria a alternação casar o prefixo.
    Os âncoras salvariam por backtracking, mas o regex de cabeçalho de partes
    (`\b...\b`) é o caso frágil — este teste prova o resultado, não o mecanismo."""
    m = RE_PARTE_ATO.search("RECORRIDO: Maria da Silva\n")
    assert m and m.group("papel").upper() == "RECORRIDO"
    assert m.group("nome") == "Maria da Silva"

    m = RE_PARTE_ATO.search("RECONVINDO: Empresa X Ltda\n")
    assert m and m.group("papel").upper() == "RECONVINDO"

    m = RE_EXTRATO_PARTE.search("Empresa Y S/A - APELADO\n")
    assert m and m.group(2).upper() == "APELADO"

    # E o papel curto legítimo continua funcionando.
    m = RE_PARTE_ATO.search("RÉU: João\n")
    assert m and m.group("nome") == "João"
