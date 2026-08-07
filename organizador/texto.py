# -*- coding: utf-8 -*-
"""Extração de texto bruto de PDFs (com cache) e classificação do tipo."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .padroes import RE_CODIGO_VERIFICADOR, RE_NUMERO_CNJ


def _hash_pdf(pdf_path: Path) -> str:
    """Gera hash MD5 do path + tamanho + mtime para identificar o PDF."""
    try:
        st = pdf_path.stat()
        raw = f"{pdf_path.resolve()}|{st.st_size}|{int(st.st_mtime)}"
        return hashlib.md5(raw.encode()).hexdigest()
    except OSError:
        return ""


def extrair_texto(pdf_path: Path, cache_dir: Path | None = None) -> str:
    """Extrai o texto bruto de todas as páginas do PDF, usando cache quando possível."""
    if cache_dir:
        h = _hash_pdf(pdf_path)
        if h:
            cache_file = cache_dir / f"{h}.txt"
            if cache_file.exists():
                try:
                    return cache_file.read_text(encoding="utf-8")
                except Exception:
                    pass

    # Import tardio: a GUI puxa este módulo (via parsing) só para desenhar a
    # janela, e o pypdf custa ~0,2 s de abertura sem ser usado até alguém abrir
    # um PDF. O cache acima resolve a maioria das chamadas sem chegar aqui.
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path), strict=False)
    blocos: list[str] = []
    for page in reader.pages:
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        blocos.append(t)
    texto = "\n".join(blocos)

    if cache_dir and h:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(texto, encoding="utf-8")
        except Exception:
            pass

    return texto


def detectar_tipo_pdf(texto: str) -> str:
    """
    Classifica o PDF em "extrato" (Justiça Estadual / distribuição),
    "ato" (despacho/decisão/sentença individual), "detalhes" (capa do processo)
    ou "desconhecido". A ordem importa: o extrato e o ato podem conter
    "Nº do processo" no corpo, mas só "detalhes" tem o layout tabular.
    """
    t = texto or ""
    if "Chave para consulta" in t and "Processo Eletr" in t:
        return "extrato"
    if RE_CODIGO_VERIFICADOR.search(t) and "Poder Judici" in t:
        return "ato"
    if "Consulta Processual" in t and "Detalhes do Processo" in t:
        return "detalhes"
    if "Partes e Representantes" in t and "Eventos" in t and RE_NUMERO_CNJ.search(t):
        return "detalhes"
    return "desconhecido"
