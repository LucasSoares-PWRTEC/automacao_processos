# -*- coding: utf-8 -*-
"""Gera organizador/assets/icone.ico a partir de formas vetoriais (PIL).

Ícone: documento (petição/processo) com um "check" verde, sobre fundo
azul-marinho arredondado. Usado tanto no ícone da janela (Tk, em tempo de
execução) quanto no ícone do .exe (PyInstaller, spec `icon=`).

Rodar:  python scripts/gerar_icone.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

RAIZ = Path(__file__).resolve().parent.parent
DESTINO = RAIZ / "organizador" / "assets" / "icone.ico"

AZUL = (20, 52, 92, 255)
BRANCO = (255, 255, 255, 255)
CINZA_LINHA = (188, 205, 224, 255)
VERDE = (46, 160, 90, 255)

TAMANHOS = (16, 24, 32, 48, 64, 128, 256)


def _desenhar(tam: int) -> Image.Image:
    img = Image.new("RGBA", (tam, tam), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    raio = tam * 0.22
    d.rounded_rectangle((0, 0, tam - 1, tam - 1), radius=raio, fill=AZUL)

    # Documento (folha com canto dobrado), centralizado.
    doc_w, doc_h = tam * 0.46, tam * 0.62
    doc_x0 = (tam - doc_w) / 2 - tam * 0.03
    doc_y0 = (tam - doc_h) / 2
    doc_x1 = doc_x0 + doc_w
    doc_y1 = doc_y0 + doc_h
    dobra = doc_w * 0.32

    d.polygon(
        [
            (doc_x0, doc_y0),
            (doc_x1 - dobra, doc_y0),
            (doc_x1, doc_y0 + dobra),
            (doc_x1, doc_y1),
            (doc_x0, doc_y1),
        ],
        fill=BRANCO,
    )
    d.polygon(
        [
            (doc_x1 - dobra, doc_y0),
            (doc_x1, doc_y0 + dobra),
            (doc_x1 - dobra, doc_y0 + dobra),
        ],
        fill=CINZA_LINHA,
    )

    # Linhas de texto simuladas.
    if tam >= 32:
        linha_h = max(1, round(tam * 0.02))
        for i in range(3):
            ly = doc_y0 + doc_h * 0.42 + i * doc_h * 0.15
            lx1 = doc_x1 - doc_w * (0.28 if i == 2 else 0.14)
            d.rounded_rectangle(
                (doc_x0 + doc_w * 0.14, ly, lx1, ly + linha_h),
                radius=linha_h / 2,
                fill=CINZA_LINHA,
            )

    # Selo verde de "organizado" no canto inferior direito do documento.
    selo_r = tam * 0.20
    selo_cx = doc_x1 - selo_r * 0.35
    selo_cy = doc_y1 - selo_r * 0.35
    d.ellipse(
        (selo_cx - selo_r, selo_cy - selo_r, selo_cx + selo_r, selo_cy + selo_r),
        fill=VERDE,
        outline=AZUL,
        width=max(1, round(tam * 0.02)),
    )
    if tam >= 24:
        w = max(1, round(tam * 0.035))
        d.line(
            [
                (selo_cx - selo_r * 0.45, selo_cy),
                (selo_cx - selo_r * 0.12, selo_cy + selo_r * 0.4),
                (selo_cx + selo_r * 0.5, selo_cy - selo_r * 0.4),
            ],
            fill=BRANCO,
            width=w,
            joint="curve",
        )

    return img


def main() -> None:
    DESTINO.parent.mkdir(parents=True, exist_ok=True)
    imagens = [_desenhar(t) for t in TAMANHOS]
    imagens[-1].save(
        DESTINO,
        format="ICO",
        sizes=[(t, t) for t in TAMANHOS],
    )
    print(f"[gerar_icone] gravado {DESTINO} ({DESTINO.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
