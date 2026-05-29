# -*- coding: utf-8 -*-
"""Helpers de CNJ, OAB e órgão julgador."""

import organizar_processos as op


def test_re_numero_cnj_encontra_padrao():
    texto = "Processo nº 4004459-04.2026.8.26.0248 distribuído"
    m = op.RE_NUMERO_CNJ.search(texto)
    assert m is not None
    assert m.group(0) == "4004459-04.2026.8.26.0248"


def test_inferir_tribunal_sp():
    assert op._inferir_tribunal("4004459-04.2026.8.26.0248") == ("TJSP", "SP")


def test_inferir_tribunal_mg():
    assert op._inferir_tribunal("4004459-04.2026.8.13.0024") == ("TJMG", "MG")


def test_inferir_tribunal_desconhecido():
    # Justiça federal (J=4) não está no mapa estadual -> vazio.
    assert op._inferir_tribunal("0001234-56.2026.4.03.6100") == ("", "")


def test_inferir_tribunal_sem_match():
    assert op._inferir_tribunal("texto sem cnj") == ("", "")


def test_uf_do_cnj():
    assert op.uf_do_cnj("4004459-04.2026.8.26.0248") == "SP"
    assert op.uf_do_cnj("4004459-04.2026.8.13.0024") == "MG"
    assert op.uf_do_cnj("") == ""
    assert op.uf_do_cnj("sem cnj") == ""


def test_oab_completa():
    assert op._oab_completa("sp", "346.433") == "OAB/SP 346.433"
    assert op._oab_completa("SP", "346.433") == "OAB/SP 346.433"


def test_oab_completa_parcial():
    assert op._oab_completa("", "346.433") == "346.433"
    assert op._oab_completa("SP", "") == "OAB/SP"
    assert op._oab_completa("", "") == ""


def test_municipio_de_comarca():
    assert op._municipio_de_comarca("Comarca de Carapicuíba") == "Carapicuíba"
    assert op._municipio_de_comarca("Foro Regional de Pinheiros") == "Pinheiros"
    assert op._municipio_de_comarca("São Paulo") == "São Paulo"
    assert op._municipio_de_comarca("") == ""


def test_parse_orgao_julgador():
    vara, comarca = op._parse_orgao_julgador("2ª Vara Cível da Comarca de Carapicuíba")
    assert vara == "2ª Vara Cível"
    assert comarca == "Comarca de Carapicuíba"


def test_parse_orgao_julgador_vazio():
    assert op._parse_orgao_julgador("") == ("", "")
