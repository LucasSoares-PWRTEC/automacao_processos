# -*- coding: utf-8 -*-
"""Nenhum emoji do plano astral (U+10000+) nos rótulos da interface.

No Windows a fonte da interface não tem esses glifos, então o Tk faz fallback de
fonte na PRIMEIRA vez que cada um aparece: ~100 ms por glifo, pago ao construir
o widget. Eram 606 ms dos 954 ms de `App.__init__` (13 glifos) antes da troca
pelos equivalentes BMP (⟳ ▭ ✏ ⚿ …). Sem este teste, a próxima feature que
escrever `text="🔄 Sincronizar"` devolve o custo sem ninguém notar.

Cobre organizador/gui.py inteiro (é lá que a janela é construída); mensagens de
log de outros módulos podem usar emoji — o custo delas é pago quando a linha
aparece, não na abertura.
"""

from __future__ import annotations

from pathlib import Path

GUI = Path(__file__).resolve().parent.parent / "organizador" / "gui.py"


def _astrais(texto: str) -> list[str]:
    return sorted({c for c in texto if ord(c) >= 0x10000})


def test_gui_sem_glifos_astrais() -> None:
    achados = _astrais(GUI.read_text(encoding="utf-8"))
    assert not achados, (
        "glifo(s) do plano astral em organizador/gui.py: "
        + " ".join(f"{c} (U+{ord(c):04X})" for c in achados)
        + " — troque por um equivalente do BMP (ver o docstring do gui.py). "
        "Cada glifo astral custa ~100 ms na abertura."
    )


def test_deteccao_reconhece_um_astral_plantado() -> None:
    """Guarda o próprio detector: se `_astrais` parar de achar, o teste acima
    passa a aprovar qualquer coisa."""
    astral = chr(0x1F504)   # o emoji de setas circulares
    assert _astrais(f'text="{astral} Sincronizar"') == [astral]
    assert _astrais('text="⟳ Sincronizar"') == []
