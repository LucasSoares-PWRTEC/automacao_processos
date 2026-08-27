# -*- coding: utf-8 -*-
"""Custo de primeiro uso de cada glifo num rótulo da interface.

Por que existe: no Windows a fonte da interface não tem todo glifo, e o Tk faz
fallback de fonte na PRIMEIRA vez que cada um aparece — dezenas de ms por
glifo, pagos ao construir o widget. Trocar os emoji do plano astral por
equivalentes do BMP tirou 622 ms dos 954 ms de ``App.__init__``; ver o
docstring de ``organizador/gui.py``.

A ARMADILHA que este script existe para evitar: medir vários glifos no MESMO
processo mente. O primeiro paga a inicialização do fallback e os seguintes
parecem baratos — numa primeira medição assim ``✏`` apareceu como "0,6 ms",
quando isolado custa 113 ms. Aqui cada glifo roda num processo novo.

Uso:
    python scripts/bench_glifos.py "↻▢◧▶▤"        # mede os glifos da string
    python scripts/bench_glifos.py --gui           # os glifos usados no gui.py
    python scripts/bench_glifos.py --teto 3 "↻✏"  # marca acima de 3 ms

O teto padrão é 8 ms: um glifo que a fonte TEM sai por 0,6-5 ms (com ruído de
uns 2 ms entre rodadas); quando falta e entra o fallback, salta para 12 ms ou
muito mais (``✏`` 113 ms, ``➕`` 168 ms). Não persiga diferenças de 2-4 ms.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent

# Roda no processo filho: mede UM glifo com a fonte/estilo já aquecidos, para o
# número ser só o custo do glifo.
_FILHO = """
import sys, time, ttkbootstrap as ttk
janela = ttk.Window(themename="litera")
frame = ttk.Frame(janela)
ttk.Button(frame, text="aquece o estilo")
t0 = time.perf_counter()
ttk.Button(frame, text=sys.argv[1] + " teste")
print(f"{(time.perf_counter() - t0) * 1000:.1f}")
janela.destroy()
"""


def _glifos_do_gui() -> list[str]:
    """Glifos não-ASCII usados em `text=` no gui.py, sem repetir."""
    import re

    fonte = (_RAIZ / "organizador" / "gui.py").read_text(encoding="utf-8")
    achados: dict[str, None] = {}
    for m in re.finditer(r'text=(["\'])(.*?)\1', fonte, re.S):
        for ch in m.group(2):
            if ord(ch) > 0x2000:
                achados[ch] = None
    return list(achados)


def medir(glifo: str) -> float | None:
    """Custo em ms, ou None se o processo filho falhar."""
    saida = subprocess.run(
        [sys.executable, "-c", _FILHO, glifo],
        capture_output=True, text=True, cwd=str(_RAIZ),
    )
    try:
        return float(saida.stdout.strip())
    except ValueError:
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("glifos", nargs="?", default="", help="string de glifos a medir")
    ap.add_argument("--gui", action="store_true", help="mede os glifos do gui.py")
    ap.add_argument("--teto", type=float, default=8.0, help="ms acima do qual marca CARO")
    args = ap.parse_args()

    alvos = _glifos_do_gui() if args.gui else list(dict.fromkeys(args.glifos))
    if not alvos:
        ap.error("passe uma string de glifos ou --gui")

    print(f"{'glifo':>6}  {'ms':>7}   (1 processo por glifo; teto {args.teto:g} ms)")
    caros: list[tuple[str, float]] = []
    for g in alvos:
        ms = medir(g)
        if ms is None:
            print(f"{g:>6}  {'ERRO':>7}")
            continue
        marca = "  CARO" if ms > args.teto else ""
        print(f"{g:>6}  {ms:7.1f}{marca}")
        if ms > args.teto:
            caros.append((g, ms))
    print()
    if caros:
        print("acima do teto: " + "  ".join(f"{g}={ms:.0f}ms" for g, ms in caros))
        print("troque por um glifo que a fonte tenha (formas geométricas U+25xx e")
        print("setas U+21xx costumam sair por ~2 ms).")
    else:
        print("todos dentro do teto.")


if __name__ == "__main__":
    main()
