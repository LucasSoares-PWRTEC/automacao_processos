# -*- coding: utf-8 -*-
"""Pipeline de parsing end-to-end (parse_pdf / parse_processo) sobre fixtures."""

from datetime import date
from pathlib import Path

import organizar_processos as op


# --------------------------------------------------------------------------
# Detalhes (capa do processo)
# --------------------------------------------------------------------------

def test_parse_detalhes_cabecalho(texto_detalhes):
    tipo, proc = op.parse_pdf(texto_detalhes, arquivo=Path("detalhes.pdf"))
    assert tipo == "detalhes"
    assert proc.numero == "4004459-04.2026.8.26.0248"
    assert proc.classe == "PROCEDIMENTO COMUM CÍVEL"
    assert proc.assunto == "Inadimplemento"
    assert proc.data_autuacao == "12/03/2026"
    assert proc.situacao == "Em andamento"
    assert proc.juiz == "JOANA PEREIRA DE SOUZA"
    assert proc.tribunal == "TJSP"
    assert proc.uf == "SP"
    assert proc.vara == "2ª Vara Cível"
    assert proc.comarca == "Comarca de Carapicuíba"
    assert "Idoso" in proc.tags
    assert proc.arquivos_origem == ["detalhes.pdf"]


def test_parse_detalhes_partes(texto_detalhes):
    _, proc = op.parse_pdf(texto_detalhes)
    assert len(proc.partes) == 2

    autor, reu = proc.partes
    assert autor.papel == "AUTOR"
    assert autor.nome == "EMPRESA EXEMPLO LTDA"
    assert autor.documento == "12.345.678/0001-90"
    assert autor.tipo == "Pessoa Jurídica"
    assert [a.oab for a in autor.advogados] == ["SP346433"]

    assert reu.papel == "RÉU"
    assert reu.nome == "FULANO DE TAL"
    assert reu.documento == "123.456.789-00"
    assert reu.tipo == "Pessoa Física"
    assert [a.oab for a in reu.advogados] == ["MG104784"]


def test_parse_detalhes_eventos(texto_detalhes):
    _, proc = op.parse_pdf(texto_detalhes)
    assert proc.total_eventos == 3
    # Eventos vêm em ordem decrescente (mais recente primeiro).
    numeros = [ev.numero for ev in proc.eventos]
    assert numeros == ["102", "101", "100"]

    ev102 = proc.eventos[0]
    assert ev102.data == "20/05/2026"
    assert ev102.hora == "14:30:00"
    assert "Juntada de Petição" in ev102.descricao
    assert ev102.documentos == ["PET1"]


def test_parse_detalhes_evento_com_prazo(texto_detalhes):
    _, proc = op.parse_pdf(texto_detalhes)
    ev101 = next(ev for ev in proc.eventos if ev.numero == "101")
    assert ev101.prazo_dias == 15
    assert ev101.prazo_status == "ABERTO"
    assert ev101.prazo_inicio == "16/05/2026"
    assert ev101.prazo_fim == "16/06/2026"
    assert ev101.documentos == ["DESPADEC2"]
    assert ev101.usuario == "secretaria"


def test_parse_detalhes_agregados(texto_detalhes):
    _, proc = op.parse_pdf(texto_detalhes)
    assert proc.primeira_movimentacao == "12/03/2026"
    assert proc.ultima_movimentacao.startswith("20/05/2026")


def test_derivar_agregados_prazo_aberto_futuro(texto_detalhes):
    _, proc = op.parse_pdf(texto_detalhes)
    # hoje antes do fim (16/06/2026) -> prazo aberto presente.
    op._derivar_agregados(proc, hoje=date(2026, 5, 20))
    assert len(proc.prazos_abertos) == 1
    pz = proc.prazos_abertos[0]
    assert pz.fim == "16/06/2026"
    assert pz.dias == 15
    assert pz.status == "ABERTO"
    assert pz.evento_origem == "101"


def test_derivar_agregados_prazo_vencido_filtrado(texto_detalhes):
    _, proc = op.parse_pdf(texto_detalhes)
    # hoje depois do fim -> prazo expirado é descartado.
    op._derivar_agregados(proc, hoje=date(2026, 7, 1))
    assert proc.prazos_abertos == []


# --------------------------------------------------------------------------
# Extrato de distribuição
# --------------------------------------------------------------------------

def test_parse_extrato(texto_extrato):
    tipo, proc = op.parse_pdf(texto_extrato, arquivo=Path("extrato.pdf"))
    assert tipo == "extrato"
    assert proc.numero == "4004459-04.2026.8.26.0248"
    assert proc.chave_consulta == "123456789012"
    assert proc.valor_causa == "R$ 44.924,15"
    assert proc.juiz == "JOANA PEREIRA DE SOUZA"
    assert proc.tribunal == "TJSP"
    assert proc.uf == "SP"
    papeis = {p.papel for p in proc.partes}
    assert "AUTOR" in papeis
    assert "RÉU" in papeis


def test_parse_extrato_advogado_no_autor(texto_extrato):
    _, proc = op.parse_pdf(texto_extrato)
    autor = next(p for p in proc.partes if p.papel == "AUTOR")
    assert any(a.oab == "SP346433" for a in autor.advogados)


# --------------------------------------------------------------------------
# Ato individual (despacho)
# --------------------------------------------------------------------------

def test_parse_ato(texto_ato):
    tipo, proc = op.parse_pdf(texto_ato, arquivo=Path("ato.pdf"))
    assert tipo == "ato"
    assert proc.numero == "4004459-04.2026.8.26.0248"
    assert proc.classe == "PROCEDIMENTO COMUM CÍVEL"
    assert proc.assunto == "Inadimplemento"
    assert len(proc.atos) == 1

    ato = proc.atos[0]
    assert ato.tipo == "DESPACHO"
    assert ato.signatario == "JOANA PEREIRA DE SOUZA"
    assert ato.data_assinatura == "20/05/2026 14:30:15"
    assert ato.codigo_verificador == "610009680897v1"
    assert ato.codigo_crc == "9c4507c5"
    assert ato.local == "Carapicuíba"
    assert ato.arquivo == "ato.pdf"
    assert "Intime-se" in ato.teor


def test_parse_ato_partes_e_endereco(texto_ato):
    _, proc = op.parse_pdf(texto_ato)
    nomes = {p.nome for p in proc.partes}
    assert "EMPRESA EXEMPLO LTDA" in nomes
    assert "FULANO DE TAL" in nomes
    assert "CEP 06320-000" in proc.foro_endereco


# --------------------------------------------------------------------------
# Desconhecido
# --------------------------------------------------------------------------

def test_parse_desconhecido_extrai_numero_do_texto():
    texto = "documento solto contendo 4004459-04.2026.8.26.0248 em algum lugar"
    tipo, proc = op.parse_pdf(texto)
    assert tipo == "desconhecido"
    assert proc.numero == "4004459-04.2026.8.26.0248"


def test_parse_evento_multilinha_com_referencia_e_nao_gerou_documento():
    linhas = [
        "101 20/05/2026 14:30:00 Intimacao da parte",
        "Refer. ao Evento 77,",
        "Evento não gerou documento",
    ]
    ev = op._parse_evento_linhas(linhas)
    assert ev is not None
    assert ev.numero == "101"
    assert ev.refer_evento == "77"
    assert "Refer. ao Evento 77," in ev.descricao
    assert ev.documentos == ["Evento nao gerou documento"]


def test_parse_evento_usuario_extraido_do_tail():
    linhas = [
        "101",
        "20/05/2026 14:30:00 Juntada de petição secretaria",
    ]
    ev = op._parse_evento_linhas(linhas)
    assert ev is not None
    assert ev.usuario == "secretaria"
    assert ev.descricao == "Juntada de petição"


def test_parse_data_br_invalida_retorna_none():
    assert op._parse_data_br("31/02/2026") is None
    assert op._parse_data_br("") is None


def test_evento_parte_papel_identifica_pelo_texto_e_por_decorrido_prazo():
    partes = [
        op.Parte(papel="RÉU", nome="FULANO DE TAL"),
        op.Parte(papel="AUTOR", nome="EMPRESA EXEMPLO LTDA"),
    ]
    ev1 = op.Evento(descricao="Prazo aberto (RÉU - FULANO DE TAL)")
    assert op._evento_parte_papel(ev1, partes) == ("FULANO DE TAL", "RÉU")

    ev2 = op.Evento(descricao="Decorrido prazo de FULANO DE TAL em 20/05")
    assert op._evento_parte_papel(ev2, partes) == ("FULANO DE TAL", "RÉU")


# --------------------------------------------------------------------------
# Classe adversarial (separa "não tem réu" de "perdemos o réu")
# --------------------------------------------------------------------------

def test_classe_exige_polo_passivo_nas_classes_adversariais():
    from organizador.parsing import classe_exige_polo_passivo as exige

    # Casa por substring, sem acento e em qualquer caixa — os tribunais sufixam
    # à vontade e alternam maiúsculas/minúsculas na mesma base.
    assert exige("EXECUÇÃO DE TÍTULO EXTRAJUDICIAL")
    assert exige("Execução de Título Extrajudicial - CPC")
    assert exige("PROCEDIMENTO COMUM CÍVEL")
    assert exige("CUMPRIMENTO DE SENTENÇA - CENTRASE CÍVEL BH")
    assert exige("Monitória")
    assert exige("Embargos à Execução")
    assert exige("Reintegração / Manutenção de Posse")


def test_classe_exige_polo_passivo_poupa_ritos_nao_adversariais():
    from organizador.parsing import classe_exige_polo_passivo as exige

    # Lista de inclusão: sem polo passivo constitutivo, nunca acusa dado faltante.
    assert not exige("Recuperação Judicial")
    assert not exige("Habilitação de crédito")
    assert not exige("Falência de Empresários, Sociedades Empresáriais")
    assert not exige("CARTA PRECATÓRIA CÍVEL")
    assert not exige("Petição Cível - Conflitos Fundiários")
    # Classe desconhecida ou ausente é indecidível — não afirma nada.
    assert not exige("")
    assert not exige(None)
