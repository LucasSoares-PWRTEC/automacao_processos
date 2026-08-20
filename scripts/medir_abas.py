# -*- coding: utf-8 -*-
"""Mede o custo de construção de cada aba da GUI.

Serve para justificar (e vigiar) a construção TARDIA das abas: só a aba inicial
é montada na abertura; Configurações e Organizador são montadas quando a pessoa
as seleciona. Ver `App._build_widgets`.

Uso:
    python scripts/medir_abas.py            # como o app abre hoje
    python scripts/medir_abas.py --eager    # forçando todas as abas na abertura
    python scripts/medir_abas.py -n 5       # mediana de 5 execuções

Nada é gravado: as tarefas de abertura (carga da lista, updater, monitoramento)
são desativadas e a janela é destruída ao fim de cada medição.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))


def _uma_medicao(eager: bool) -> tuple[float, dict[str, float]]:
    import organizador.gui as G

    tempos: dict[str, float] = {}

    def cronometrar(nome: str) -> None:
        orig = getattr(G.App, nome)
        if getattr(orig, "_cronometrado", False):
            return
        def wrapper(self, *a, **k):
            t0 = time.perf_counter()
            try:
                return orig(self, *a, **k)
            finally:
                tempos[nome] = tempos.get(nome, 0.0) + (time.perf_counter() - t0) * 1000
        wrapper._cronometrado = True  # type: ignore[attr-defined]
        setattr(G.App, nome, wrapper)

    for m in ("_build_aba_organizador", "_build_aba_pesquisa",
              "_build_aba_autojur", "_build_aba_config", "_carregar_config_na_ui"):
        cronometrar(m)

    # Sem os jobs de abertura: queremos o custo dos WIDGETS, não do disco/rede.
    G.App._carregar_pesquisa_inicial = lambda self: None
    G.App._carregar_templates = lambda self: None
    G.App._verificar_atualizacao_silenciosa = lambda self: None

    t0 = time.perf_counter()
    app = G.App()
    if eager:  # simula o comportamento antigo: todas as abas na abertura
        for chave in list(getattr(app, "_abas_tardias", {})):
            app._garantir_aba(chave)
    total = (time.perf_counter() - t0) * 1000
    app.update_idletasks()
    app.destroy()
    return total, tempos


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-n", type=int, default=3, help="execuções (mediana)")
    ap.add_argument("--eager", action="store_true",
                    help="constrói todas as abas na abertura (comportamento antigo)")
    ap.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    # Cada medição roda num processo NOVO: o ttkbootstrap registra estilos num
    # singleton global, e criar uma segunda janela Tk no mesmo processo faz o
    # tema perder layouts ("Layout warning.Round.Toggle not found").
    if args.once:
        import json as _json

        total, tempos = _uma_medicao(args.eager)
        print(_json.dumps({"total": total, "tempos": tempos}))
        return 0

    import json as _json
    import subprocess

    totais: list[float] = []
    por_etapa: dict[str, list[float]] = {}
    cmd = [sys.executable, __file__, "--once"] + (["--eager"] if args.eager else [])
    for _ in range(args.n):
        saida = subprocess.run(
            cmd, cwd=str(_RAIZ), capture_output=True, text=True, timeout=300
        )
        if saida.returncode != 0:
            print(saida.stderr[-2000:], file=sys.stderr)
            return 1
        dados = _json.loads(saida.stdout.strip().splitlines()[-1])
        totais.append(dados["total"])
        for k, v in dados["tempos"].items():
            por_etapa.setdefault(k, []).append(v)

    modo = "TODAS as abas na abertura" if args.eager else "só a aba inicial (atual)"
    print(f"{modo} — mediana de {args.n} execução(ões)\n")
    print(f'{"etapa":<30}{"ms":>9}')
    for k, v in sorted(por_etapa.items(), key=lambda kv: -statistics.median(kv[1])):
        print(f"{k:<30}{statistics.median(v):>9.0f}")
    print("-" * 39)
    print(f'{"App.__init__ inteiro":<30}{statistics.median(totais):>9.0f}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
