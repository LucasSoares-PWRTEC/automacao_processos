# -*- coding: utf-8 -*-
"""Substituição de placeholders e geração de .docx em memória.

Não cobre a conversão .doc -> .docx (requer Word via COM) — apenas o caminho
.docx puro, manipulado em memória com python-docx.
"""

import docx
from docx.enum.text import WD_COLOR_INDEX
from docx.shared import Pt

import organizar_processos as op
from organizar_processos import Contexto


# --------------------------------------------------------------------------
# _aplicar_substituicoes / _resolver_auto
# --------------------------------------------------------------------------

def test_substituir_placeholders_fixos():
    dados = {"secretaria": "1ª Secretaria", "numero": "4004459-04.2026.8.26.0248"}
    texto = "À (secretaria), processo (número do processo)."
    out = op._aplicar_substituicoes(texto, dados)
    assert out == "À 1ª Secretaria, processo 4004459-04.2026.8.26.0248."


def test_substituir_placeholder_template_generico():
    # Placeholder "(nome)" não-mapeado cai no dado de mesma chave.
    dados = {"municipio": "Carapicuíba"}
    assert op._aplicar_substituicoes("(municipio)", dados) == "Carapicuíba"


def test_substituir_mantem_placeholder_sem_dado():
    # Sem dado correspondente, o placeholder é preservado.
    assert op._aplicar_substituicoes("(vara)", {}) == "(vara)"


def test_resolver_auto_imovel():
    assert op._resolver_auto("imóvel ou imóveis", None, multiplos_imoveis=False) == "imóvel"
    assert op._resolver_auto("imóvel ou imóveis", None, multiplos_imoveis=True) == "imóveis"


def test_resolver_auto_reu_genero():
    assert op._resolver_auto("réu ou ré", Contexto(genero_reu="m"), False) == "réu"
    assert op._resolver_auto("réu ou ré", Contexto(genero_reu="f"), False) == "ré"


def test_resolver_auto_desconhecido():
    assert op._resolver_auto("inexistente", None, False) is None


def test_resolver_auto_conectores_por_numero_de_reus():
    # "(do ou dos)/(da ou das)/(o ou os)" concordam com o nº de réus, não com imóvel.
    assert op._resolver_auto("do ou dos", Contexto(numero_reu="s"), False) == "do"
    assert op._resolver_auto("do ou dos", Contexto(numero_reu="p"), False) == "dos"
    assert op._resolver_auto("da ou das", Contexto(numero_reu="s"), False) == "da"
    assert op._resolver_auto("da ou das", Contexto(numero_reu="p"), False) == "das"
    assert op._resolver_auto("o ou os", Contexto(numero_reu="p"), False) == "os"
    # O checkbox "Múltiplos imóveis" NÃO afeta mais os conectores.
    assert op._resolver_auto("do ou dos", Contexto(numero_reu="s"), multiplos_imoveis=True) == "do"


def test_resolver_auto_imovel_e_sao_seguem_multiplos_imoveis():
    # Placeholders genuinamente de imóvel continuam controlados por multiplos_imoveis.
    assert op._resolver_auto("são", None, multiplos_imoveis=False) == "é"
    assert op._resolver_auto("são", None, multiplos_imoveis=True) == "são"
    # E independem do número de réus.
    assert op._resolver_auto("imóvel ou imóveis", Contexto(numero_reu="p"), multiplos_imoveis=False) == "imóvel"


def test_substituicoes_conector_citacao_plural():
    ctx = Contexto(numero_reu="p")
    out = op._aplicar_substituicoes(
        "requer a citação (da ou das) (parte ou partes) (ré ou rés)", {}, ctx=ctx,
    )
    assert out == "requer a citação das partes rés"


def test_substituicoes_auto_imovel_no_texto():
    out = op._aplicar_substituicoes("penhora dos (imóvel ou imóveis)", {}, multiplos_imoveis=True)
    assert out == "penhora dos imóveis"


def test_resolver_auto_reu_num():
    # "(parte ou partes)" flexiona por número de réus.
    assert op._resolver_auto("parte ou partes", Contexto(numero_reu="s"), False) == "parte"
    assert op._resolver_auto("parte ou partes", Contexto(numero_reu="p"), False) == "partes"
    # Verbo do "Descumprimento de acordo".
    assert op._resolver_auto("cumpriu ou cumpriram", Contexto(numero_reu="s"), False) == "cumpriu"
    assert op._resolver_auto("cumpriu ou cumpriram", Contexto(numero_reu="p"), False) == "cumpriram"


def test_resolver_auto_reu_genum_4vias():
    ph = "ré ou rés ou réu ou réus"
    assert op._resolver_auto(ph, Contexto(genero_reu="m", numero_reu="s"), False) == "réu"
    assert op._resolver_auto(ph, Contexto(genero_reu="m", numero_reu="p"), False) == "réus"
    assert op._resolver_auto(ph, Contexto(genero_reu="f", numero_reu="s"), False) == "ré"
    assert op._resolver_auto(ph, Contexto(genero_reu="f", numero_reu="p"), False) == "rés"


def test_resolver_auto_sem_contexto_usa_primeira_forma():
    # Sem Contexto, cai na 1ª forma (singular/masculino) sem estourar no unpack.
    assert op._resolver_auto("ré ou rés ou réu ou réus", None, False) == "réu"
    assert op._resolver_auto("parte ou partes", None, False) == "parte"


def test_substituicoes_auto_genum_no_texto():
    # No texto, "(ré ou rés ou réu ou réus)" é resolvido pelo gênero+número do réu.
    ctx = Contexto(genero_reu="f", numero_reu="p")
    out = op._aplicar_substituicoes("requer a intimação d@ (ré ou rés ou réu ou réus)", {}, ctx=ctx)
    assert out == "requer a intimação d@ rés"


def test_extrair_ignora_literais_nao_sao_campos(tmp_path):
    # Número por extenso após dígito e a linha de assinatura não viram campos.
    doc = docx.Document()
    doc.add_paragraph("requer a concessão do prazo de 10 (dez) dias")
    doc.add_paragraph("(assinado eletronicamente)")
    doc.add_paragraph("À (secretaria), réu (ré ou rés ou réu ou réus)")
    p = tmp_path / "modelo.docx"
    doc.save(str(p))
    phs = op._extrair_placeholders_template(p)
    assert "dez" not in phs
    assert "assinado eletronicamente" not in phs
    # Placeholders reais continuam detectados.
    assert "secretaria" in phs
    assert "ré ou rés ou réu ou réus" in phs


def test_extrair_mantem_placeholder_mapeado_mesmo_apos_digito(tmp_path):
    # Placeholder mapeado precedido de dígito NÃO é descartado (guarda do filtro).
    doc = docx.Document()
    doc.add_paragraph("no percentual de 30 (trinta por cento)")
    p = tmp_path / "modelo2.docx"
    doc.save(str(p))
    phs = op._extrair_placeholders_template(p)
    assert "trinta por cento" in phs


# --------------------------------------------------------------------------
# Geração de .docx em memória (_preencher_campos_docx e helpers)
# --------------------------------------------------------------------------

def test_preencher_campos_docx_substitui_placeholders():
    doc = docx.Document()
    doc.add_paragraph("À (secretaria) da (vara).")
    # "(executado ou executados)" é placeholder de réu -> vira o nome do réu.
    doc.add_paragraph("Requer a citação do (executado ou executados).")
    dados = {"secretaria": "1ª Secretaria", "vara": "2ª Vara Cível", "reu": "FULANO"}
    op._preencher_campos_docx(doc, dados)
    textos = [p.text for p in doc.paragraphs]
    assert "1ª Secretaria" in textos[0]
    assert "2ª Vara Cível" in textos[0]
    assert "FULANO" in textos[1]


def test_preencher_campos_docx_flexiona_papel_nu():
    # Palavra de papel fora de parênteses (não é placeholder) sofre flexão no BODY.
    doc = docx.Document()
    doc.add_paragraph("Citar o executado no prazo legal.")
    ctx = Contexto(genero_reu="f", numero_reu="s")
    op._preencher_campos_docx(doc, {}, ctx=ctx)
    assert doc.paragraphs[0].text == "Citar a executada no prazo legal."


def test_padronizar_espacamento_paragrafo_com_texto():
    doc = docx.Document()
    p = doc.add_paragraph("Texto qualquer")
    op._padronizar_espacamento(p)
    pf = p.paragraph_format
    assert pf.space_before == Pt(0)
    assert pf.space_after == Pt(op.SPACE_AFTER_PT)
    assert pf.line_spacing == op.LINE_SPACING


def test_padronizar_espacamento_paragrafo_vazio():
    doc = docx.Document()
    p = doc.add_paragraph("")
    op._padronizar_espacamento(p)
    pf = p.paragraph_format
    assert pf.space_before == Pt(0)
    assert pf.space_after == Pt(0)
    assert pf.line_spacing == 1.0


def test_remover_highlight_amarelo():
    doc = docx.Document()
    p = doc.add_paragraph()
    run = p.add_run("destaque")
    run.font.highlight_color = WD_COLOR_INDEX.YELLOW
    op._remover_highlight_amarelo(p)
    assert run.font.highlight_color is None


def test_colapsar_paragrafos_vazios():
    doc = docx.Document()
    doc.add_paragraph("")          # vazio inicial -> removido
    doc.add_paragraph("Primeiro")
    doc.add_paragraph("")
    doc.add_paragraph("")          # vazio consecutivo -> removido
    doc.add_paragraph("Segundo")
    doc.add_paragraph("")          # vazio final -> removido
    op._colapsar_paragrafos_vazios(doc)
    textos = [p.text for p in doc.paragraphs]
    assert textos == ["Primeiro", "", "Segundo"]


def test_gera_docx_em_disco(tmp_path):
    # Sanidade: documento preenchido pode ser salvo e relido.
    doc = docx.Document()
    doc.add_paragraph("Processo (numero do processo).")
    op._preencher_campos_docx(doc, {"numero": "4004459-04.2026.8.26.0248"})
    destino = tmp_path / "peticao.docx"
    doc.save(str(destino))
    assert destino.exists()
    relido = docx.Document(str(destino))
    assert "4004459-04.2026.8.26.0248" in relido.paragraphs[0].text


# --------------------------------------------------------------------------
# Tratamento judicial no HEADER
# --------------------------------------------------------------------------


def test_header_tratamento_judicial_feminino():
    doc = docx.Document()
    doc.add_paragraph("EXCELENTÍSSIMO(A) SENHOR(A) DOUTOR(A) JUIZ(A)")
    doc.add_paragraph("")
    doc.add_paragraph("Vem requerer o que segue.")
    ctx = Contexto(genero_juiz="f", genero_autor="f", numero_autor="s")
    zonas = op._classificar_paragrafos(doc)
    op._preencher_campos_docx(doc, {}, ctx=ctx, zonas_paragrafos=zonas)
    assert "EXCELENT\u00cdSSIMA SENHORA DOUTORA JUIZA" in doc.paragraphs[0].text


def test_header_tratamento_judicial_masculino():
    doc = docx.Document()
    doc.add_paragraph("EXCELENTÍSSIMO(A) SENHOR(A) DOUTOR(A) JUIZ(A)")
    doc.add_paragraph("")
    doc.add_paragraph("Vem requerer o que segue.")
    ctx = Contexto(genero_juiz="m", genero_autor="f", numero_autor="s")
    zonas = op._classificar_paragrafos(doc)
    op._preencher_campos_docx(doc, {}, ctx=ctx, zonas_paragrafos=zonas)
    assert "EXCELENT\u00cdSSIMO SENHOR DOUTOR JUIZ" in doc.paragraphs[0].text


def test_header_meritissimo_feminino():
    doc = docx.Document()
    doc.add_paragraph("MERITÍSSIMO(A) SENHOR(A) DOUTOR(A) JUIZ(A)")
    doc.add_paragraph("")
    doc.add_paragraph("Vem requerer o que segue.")
    ctx = Contexto(genero_juiz="f", genero_autor="m", numero_autor="s")
    zonas = op._classificar_paragrafos(doc)
    op._preencher_campos_docx(doc, {}, ctx=ctx, zonas_paragrafos=zonas)
    assert "MERIT\u00cdSSIMA SENHORA DOUTORA JUIZA" in doc.paragraphs[0].text


def test_header_tratamento_judicial_sem_doutor_feminino():
    doc = docx.Document()
    doc.add_paragraph("EXCELENT\u00cdSSIMO(A) SENHOR(A) JUIZ(A)")
    doc.add_paragraph("")
    doc.add_paragraph("Vem requerer o que segue.")
    ctx = Contexto(genero_juiz="f", genero_autor="f", numero_autor="s")
    zonas = op._classificar_paragrafos(doc)
    op._preencher_campos_docx(doc, {}, ctx=ctx, zonas_paragrafos=zonas)
    assert "EXCELENT\u00cdSSIMA SENHORA JUIZA" in doc.paragraphs[0].text


def test_header_tratamento_judicial_sem_doutor_masculino():
    doc = docx.Document()
    doc.add_paragraph("EXCELENT\u00cdSSIMO(A) SENHOR(A) JUIZ(A)")
    doc.add_paragraph("")
    doc.add_paragraph("Vem requerer o que segue.")
    ctx = Contexto(genero_juiz="m", genero_autor="f", numero_autor="s")
    zonas = op._classificar_paragrafos(doc)
    op._preencher_campos_docx(doc, {}, ctx=ctx, zonas_paragrafos=zonas)
    assert "EXCELENT\u00cdSSIMO SENHOR JUIZ" in doc.paragraphs[0].text
