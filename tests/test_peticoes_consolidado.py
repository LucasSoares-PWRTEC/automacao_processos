# -*- coding: utf-8 -*-
"""Cobertura do consolidado de petições e caminhos de erro."""

from __future__ import annotations

from pathlib import Path

import docx
import pytest

import organizar_processos as op
from organizador.models import Contexto, Processo


def _criar_template(path: Path, header: str, body: list[str], closing: str | None = None, com_tabela: bool = False):
    doc = docx.Document()
    doc.add_paragraph(header)
    doc.add_paragraph("")
    for linha in body:
        doc.add_paragraph(linha)
    if com_tabela:
        tabela = doc.add_table(rows=1, cols=1)
        tabela.cell(0, 0).text = "Tabela (numero do processo)"
    if closing is not None:
        doc.add_paragraph(closing)
    doc.save(str(path))


def test_gerar_peticao_consolidada_preserva_tabela_e_deduplica_body(tmp_path):
    t1 = tmp_path / "a.docx"
    t2 = tmp_path / "b.docx"
    _criar_template(
        t1,
        "EXCELENTÍSSIMO(A) SENHOR(A) JUIZ(A)",
        ["Linha longa de corpo para deduplicar com mais de vinte e cinco chars.", "Corpo exclusivo A"],
        "Fecho A",
        com_tabela=True,
    )
    _criar_template(
        t2,
        "CABECALHO IGNORADO",
        ["Linha longa de corpo para deduplicar com mais de vinte e cinco chars.", "Corpo exclusivo B"],
        "Fecho B",
    )

    saida = op._gerar_peticao_consolidada(
        [t1, t2],
        tmp_path,
        {"numero": "4004459-04.2026.8.26.0248"},
        Contexto(genero_juiz="m"),
    )

    doc = docx.Document(str(saida))
    textos = [p.text for p in doc.paragraphs if p.text.strip()]
    assert textos[0] == "EXCELENTÍSSIMO SENHOR JUIZ"
    assert textos.count("Linha longa de corpo para deduplicar com mais de vinte e cinco chars.") == 1
    assert "Corpo exclusivo A" in textos
    assert "Corpo exclusivo B" in textos
    assert "Fecho A" in textos
    assert len(doc.tables) == 1
    assert "4004459-04.2026.8.26.0248" in doc.tables[0].cell(0, 0).text


def test_gerar_peticao_consolidada_injeta_closing_default(tmp_path):
    t1 = tmp_path / "a.docx"
    _criar_template(t1, "EXCELENTÍSSIMO(A) SENHOR(A) JUIZ(A)", ["Corpo sem fechamento suficiente e comprido."])
    saida = op._gerar_peticao_consolidada([t1], tmp_path, {}, Contexto(genero_juiz="f"))
    doc = docx.Document(str(saida))
    assert any("Termos em que pede deferimento." == p.text for p in doc.paragraphs)


def test_gerar_peticao_consolidada_sem_templates_falha(tmp_path):
    with pytest.raises(ValueError, match="Nenhum template selecionado"):
        op._gerar_peticao_consolidada([], tmp_path, {}, None)


def test_gerar_peticao_unica_formato_invalido_falha(tmp_path):
    template = tmp_path / "modelo.txt"
    template.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="Formato de template não suportado"):
        op._gerar_peticao_unica(template, tmp_path, {})


def test_gerar_peticao_unica_doc_usa_cache_convertido(monkeypatch, tmp_path):
    template_docx = tmp_path / "modelo_cache.docx"
    _criar_template(template_docx, "HEADER", ["Processo (numero do processo)"], "Fecho")
    template_doc = tmp_path / "modelo.doc"
    template_doc.write_text("binario", encoding="utf-8")

    monkeypatch.setattr(op.peticoes, "_obter_docx_cacheado", lambda _path: template_docx)
    op._gerar_peticao_unica(template_doc, tmp_path, {"numero": "123"})
    saida = tmp_path / "modelo - 123.docx"
    assert saida.exists()


def test_gerar_peticoes_modo_documento_unico_registra_erro(monkeypatch, tmp_path):
    proc = Processo(numero="123")
    logs = []
    monkeypatch.setattr(op.peticoes, "_gerar_peticao_consolidada", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("quebrou")))
    op.gerar_peticoes(proc, [tmp_path / "a.docx", tmp_path / "b.docx"], tmp_path, {}, log=logs.append, documento_unico=True)
    assert any("ERRO ao consolidar" in linha for linha in logs)


def test_gerar_peticoes_modo_documento_unico_loga_sucesso(monkeypatch, tmp_path):
    proc = Processo(numero="123")
    logs = []
    monkeypatch.setattr(op.peticoes, "_gerar_peticao_consolidada", lambda *args, **kwargs: tmp_path / "saida.docx")
    op.gerar_peticoes(proc, [tmp_path / "a.docx", tmp_path / "b.docx"], tmp_path, {}, log=logs.append, documento_unico=True)
    assert any("OK: documento consolidado -> saida.docx" in linha for linha in logs)
