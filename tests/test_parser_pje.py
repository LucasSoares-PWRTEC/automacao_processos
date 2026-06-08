# -*- coding: utf-8 -*-
"""Cobertura do parser estruturado do PJe (TJMG 1º grau).

O blob abaixo reproduz a estrutura real confirmada no processo
5000432-19.2021.8.13.0079: timeline em texto (newest-first, com cabeçalhos
de data "DD mmm AAAA", descrições, linhas de documento "ID - Tipo" e horários
HH:MM) seguida dos corpos dos documentos abertos, cada um precedido pelo
marcador <!-- DOCUMENTO_ORIGEM: ... -->.
"""

from __future__ import annotations

from organizador.integracoes.tjsp_scraper.parser_pje import (
    extrair_processo_pje,
    _canonizar_papel,
    _converter_data,
    _tipo_documento,
)
from organizador.organizacao import _dict_para_processo


_BLOB = """22 jan 2021
18 fev 2026
JUNTADA DE PETIÇÃO DE PETIÇÃO
10629074581 - Petição
10629074582 - Petição (manifestacao enderecos)
16:32
09 fev 2026
EXPEDIÇÃO DE COMUNICAÇÃO VIA SISTEMA.
10623477594 - Intimação
08:59
31 mai 2022
10578543024 - Sentença
14:06
11 jan 2021
DISTRIBUÍDO POR SORTEIO
16:54
<!-- DOCUMENTO_ORIGEM: 10623476141 - Juntada (Pesquisa Sisbajud) | EVENTO:  | DATA: 09 fev 2026 | ID: 10623476141 -->
PODER JUDICIÁRIO DO ESTADO DE MINAS GERAIS
Comarca de Contagem / 4ª Vara Cível da Comarca de Contagem
Avenida Maria da Glória Rocha, 425, Centro, Contagem - MG - CEP: 32010-375
PROCESSO Nº 5000432-19.2021.8.13.0079
[CÍVEL] CUMPRIMENTO DE SENTENÇA (156)
REQUERENTE: ANAPU EMPREENDIMENTOS IMOBILIÁRIOS LTDA CPF: 10.923.840/0001-80
REQUERIDO(A): LINDIOMAR NASCIMENTO DE JESUS CPF: 085.118.676-94
REQUERIDO(A): CARMEN DA SILVA SOUZA CPF: 114.120.726-54
<!-- DOCUMENTO_ORIGEM: 10578543024 - Sentença | EVENTO:  | DATA: 31 mai 2022 | ID: 10578543024 -->
PROCESSO Nº: 5000432-19.2021.8.13.0079
CLASSE: [CÍVEL] CUMPRIMENTO DE SENTENÇA (156)
ASSUNTO: [Compra e Venda]
AUTOR: ANAPU EMPREENDIMENTOS IMOBILIÁRIOS LTDA CPF: 10.923.840/0001-80
RÉU: LINDIOMAR NASCIMENTO DE JESUS CPF: 085.118.676-94 e outros
Homologo o acordo celebrado pelas partes, suspendendo o feito.
"""


def _proc():
    return _dict_para_processo(
        extrair_processo_pje(_BLOB, numero_cnj="5000432-19.2021.8.13.0079")
    )


# --- helpers -----------------------------------------------------------------
def test_converter_data():
    assert _converter_data("18 fev 2026") == "18/02/2026"
    assert _converter_data("9 jan 2021") == "09/01/2021"
    assert _converter_data("lixo") == ""


def test_tipo_documento_por_mascara():
    assert _tipo_documento("10.923.840/0001-80") == "Pessoa Jurídica"
    assert _tipo_documento("085.118.676-94") == "Pessoa Física"
    assert _tipo_documento("") == ""


def test_papeis_canonizados():
    assert _canonizar_papel("REQUERIDO(A)") == "REQUERIDO"
    assert _canonizar_papel("RÉU") == "RÉU"
    assert _canonizar_papel("Exequente") == "EXEQUENTE"


# --- cabeçalho ---------------------------------------------------------------
def test_cabecalho_basico():
    p = _proc()
    assert p.numero == "5000432-19.2021.8.13.0079"
    assert p.classe == "[CÍVEL] CUMPRIMENTO DE SENTENÇA (156)"
    assert p.assunto == "Compra e Venda"
    assert p.comarca == "Contagem"
    assert p.vara == "4ª Vara Cível da Comarca de Contagem"
    assert p.orgao_julgador == "4ª Vara Cível da Comarca de Contagem — Contagem"
    assert "CEP: 32010-375" in p.foro_endereco


def test_tribunal_uf_inferidos_do_cnj():
    p = _proc()
    assert p.tribunal == "TJMG"
    assert p.uf == "MG"


# --- partes ------------------------------------------------------------------
def test_partes_com_documento_e_tipo_dedupe_por_nome():
    partes = {(p.papel, p.nome, p.documento, p.tipo) for p in _proc().partes}
    assert ("REQUERENTE", "ANAPU EMPREENDIMENTOS IMOBILIÁRIOS LTDA",
            "10.923.840/0001-80", "Pessoa Jurídica") in partes
    assert ("REQUERIDO", "LINDIOMAR NASCIMENTO DE JESUS",
            "085.118.676-94", "Pessoa Física") in partes
    assert ("REQUERIDO", "CARMEN DA SILVA SOUZA",
            "114.120.726-54", "Pessoa Física") in partes
    # ANAPU aparece como REQUERENTE (juntada) e AUTOR (sentença) → 1 entrada.
    nomes = [p.nome for p in _proc().partes]
    assert nomes.count("ANAPU EMPREENDIMENTOS IMOBILIÁRIOS LTDA") == 1
    # "e outros" não vaza para o nome do réu.
    assert all(not n.endswith("e outros") for n in nomes)


# --- eventos -----------------------------------------------------------------
def test_eventos_e_agregados():
    p = _proc()
    # 4 entradas na timeline (cada uma fechada por um HH:MM).
    assert p.total_eventos == 4
    # newest-first: 1º é o mais recente; autuação = data mais antiga vista.
    assert p.eventos[0].data == "18/02/2026"
    assert p.eventos[0].descricao == "JUNTADA DE PETIÇÃO DE PETIÇÃO"
    assert p.eventos[0].documentos == [
        "Petição", "Petição (manifestacao enderecos)"
    ]
    assert p.data_autuacao == "11/01/2021"
    assert p.primeira_movimentacao == "11/01/2021"
    assert p.ultima_movimentacao.startswith("18/02/2026 —")


# --- atos --------------------------------------------------------------------
def test_atos_com_teor_e_data():
    atos = _proc().atos
    assert len(atos) == 1
    sentenca = atos[0]
    assert sentenca.tipo == "SENTENÇA"
    assert sentenca.data_assinatura == "31/05/2022"
    assert "Homologo o acordo" in sentenca.teor
    assert sentenca.arquivo == "10578543024 - Sentença"


# --- robustez ----------------------------------------------------------------
def test_blob_vazio_nao_quebra():
    d = extrair_processo_pje("", numero_cnj="5000432-19.2021.8.13.0079")
    assert d["partes"] == []
    assert d["eventos"] == []
    assert d["atos"] == []
    assert d["numero"] == "5000432-19.2021.8.13.0079"
    assert d["tribunal"] == "TJMG"
