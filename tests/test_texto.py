# -*- coding: utf-8 -*-
"""Cobertura de extração de texto com cache e falhas toleradas."""

from __future__ import annotations

from pathlib import Path

import pypdf
import organizar_processos as op

# `organizador.texto` importa o PdfReader DENTRO de extrair_texto (import
# tardio: a GUI puxa este módulo só para desenhar a janela e não deve pagar o
# pypdf). Como a resolução é no momento da chamada, o ponto de injeção destes
# testes é o atributo em `pypdf`, não em `organizador.texto`.


class _Page:
    def __init__(self, texto: str | None = None, exc: Exception | None = None):
        self._texto = texto
        self._exc = exc

    def extract_text(self):
        if self._exc is not None:
            raise self._exc
        return self._texto


class _Reader:
    def __init__(self, pages):
        self.pages = pages


def test_hash_pdf_retorna_vazio_em_oserror(monkeypatch, tmp_path):
    pdf = tmp_path / "arquivo.pdf"
    pdf.write_text("x", encoding="utf-8")

    def quebrar_stat(self):
        raise OSError("sem acesso")

    monkeypatch.setattr(Path, "stat", quebrar_stat)
    assert op._hash_pdf(pdf) == ""


def test_extrair_texto_usa_cache_quando_presente(monkeypatch, tmp_path):
    pdf = tmp_path / "arquivo.pdf"
    pdf.write_text("binario", encoding="utf-8")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    h = op._hash_pdf(pdf)
    (cache_dir / f"{h}.txt").write_text("cache pronto", encoding="utf-8")

    def falhar_reader(*args, **kwargs):
        raise AssertionError("PdfReader nao deveria ser chamado")

    monkeypatch.setattr(pypdf, "PdfReader", falhar_reader)
    assert op.extrair_texto(pdf, cache_dir) == "cache pronto"


def test_extrair_texto_faz_fallback_quando_cache_corrompido(monkeypatch, tmp_path):
    pdf = tmp_path / "arquivo.pdf"
    pdf.write_text("binario", encoding="utf-8")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    h = op._hash_pdf(pdf)
    cache_file = cache_dir / f"{h}.txt"
    cache_file.write_text("cache ruim", encoding="utf-8")

    def quebrar_read_text(*args, **kwargs):
        raise UnicodeDecodeError("utf-8", b"x", 0, 1, "bad")

    monkeypatch.setattr(Path, "read_text", quebrar_read_text)
    monkeypatch.setattr(pypdf, "PdfReader", lambda *args, **kwargs: _Reader([_Page("novo texto")]))
    assert op.extrair_texto(pdf, cache_dir) == "novo texto"


def test_extrair_texto_tolera_excecao_em_pagina(monkeypatch, tmp_path):
    pdf = tmp_path / "arquivo.pdf"
    pdf.write_text("binario", encoding="utf-8")
    monkeypatch.setattr(
        pypdf,
        "PdfReader",
        lambda *args, **kwargs: _Reader([_Page("primeira"), _Page(exc=RuntimeError("falhou"))]),
    )
    assert op.extrair_texto(pdf) == "primeira\n"


def test_extrair_texto_tolera_falha_ao_gravar_cache(monkeypatch, tmp_path):
    pdf = tmp_path / "arquivo.pdf"
    pdf.write_text("binario", encoding="utf-8")
    cache_dir = tmp_path / "cache"

    def quebrar_write_text(self, *args, **kwargs):
        raise OSError("disco cheio")

    monkeypatch.setattr(Path, "write_text", quebrar_write_text)
    monkeypatch.setattr(pypdf, "PdfReader", lambda *args, **kwargs: _Reader([_Page("texto")]))
    assert op.extrair_texto(pdf, cache_dir) == "texto"


def test_extrair_texto_sem_cache_quando_hash_vazio(monkeypatch, tmp_path):
    pdf = tmp_path / "arquivo.pdf"
    pdf.write_text("binario", encoding="utf-8")
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(op.texto, "_hash_pdf", lambda _pdf: "")
    monkeypatch.setattr(pypdf, "PdfReader", lambda *args, **kwargs: _Reader([_Page("texto limpo")]))
    assert op.extrair_texto(pdf, cache_dir) == "texto limpo"
