# -*- coding: utf-8 -*-
"""Mede o tempo de abertura do app, marca por marca.

A otimização para máquinas fracas precisa de número, não de impressão. Este
script abre o app N vezes com ``ORGANIZADOR_PERF=1``, espera as marcas
aparecerem no log, mata o processo e imprime a MEDIANA de cada marca.

Roda tanto no código-fonte quanto no .exe empacotado:

    python scripts/bench_abertura.py                    # fonte (pythonw)
    python scripts/bench_abertura.py --exe dist/Peticoes.exe
    python scripts/bench_abertura.py -n 5 --frio        # limpa caches entre rodadas

``total_externo`` é o tempo de parede desde o spawn até a última marca — é a
única medida que inclui a extração do onefile (que acontece ANTES de o Python
começar, logo fora do alcance de organizador/perf.py). A pasta de extração é o
``runtime_tmpdir`` do OrganizadorProcessos.spec, não o %TEMP%.

ATENÇÃO: o kill abaixo mata o processo que este script spawnou, que no onefile é
o BOOTLOADER — o filho com a GUI sobrevive e continua segurando a pasta extraída
(~99 MB por rodada). Medido em 31/08/2026: 3 rodadas deixaram 6 processos vivos e
3 pastas. Elas são recolhidas por gui._limpar_temp_orfaos quando os processos
morrem, mas até lá ocupam disco. Se for medir várias vezes, confira depois com
``Get-Process Peticoes``.
"""

from __future__ import annotations

import argparse
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

# Marca que sinaliza "abertura concluída" — a última do caminho de startup.
MARCA_FINAL = "tree:preenchida"
TIMEOUT_S = 300.0


def _ler_marcas(log: Path) -> list[tuple[str, float, float]]:
    if not log.exists():
        return []
    marcas: list[tuple[str, float, float]] = []
    for linha in log.read_text(encoding="utf-8", errors="replace").splitlines():
        partes = linha.split(";")
        if len(partes) != 3:
            continue
        try:
            marcas.append((partes[0], float(partes[1]), float(partes[2])))
        except ValueError:
            continue
    return marcas


def _limpar_caches_frios() -> None:
    """Remove os caches que fazem a 2ª abertura ser mais rápida que a 1ª."""
    from organizador.config import APP_DATA_DIR

    for nome in ("processos_indice.json", "templates_placeholders.json", "perf.log"):
        try:
            (APP_DATA_DIR / nome).unlink()
        except OSError:
            pass


def _uma_rodada(cmd: list[str], log: Path, env: dict[str, str]) -> tuple[list, float]:
    try:
        log.unlink()
    except OSError:
        pass
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, cwd=str(_RAIZ), env=env)
    marcas: list = []
    total = float("nan")
    try:
        while time.perf_counter() - t0 < TIMEOUT_S:
            time.sleep(0.25)
            marcas = _ler_marcas(log)
            if any(m[0].startswith(MARCA_FINAL) for m in marcas):
                total = (time.perf_counter() - t0) * 1000
                break
            if proc.poll() is not None:
                break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
    return marcas, total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-n", type=int, default=3, help="número de rodadas (default 3)")
    ap.add_argument("--exe", help="caminho do .exe empacotado (default: roda o fonte)")
    ap.add_argument(
        "--frio", action="store_true",
        help="apaga o índice/cache de templates antes de CADA rodada",
    )
    args = ap.parse_args()

    from organizador.config import APP_DATA_DIR

    log = APP_DATA_DIR / "perf.log"
    env = dict(os.environ, ORGANIZADOR_PERF="1")

    if args.exe:
        exe = Path(args.exe)
        if not exe.is_file():
            print(f"não encontrei {exe}", file=sys.stderr)
            return 2
        cmd = [str(exe.resolve())]
    else:
        cmd = [sys.executable, str(_RAIZ / "organizar_processos.py")]

    print(f"comando: {' '.join(cmd)}")
    print(f"log:     {log}")
    print(f"rodadas: {args.n}{' (frio)' if args.frio else ''}\n")

    por_marca: dict[str, list[float]] = {}
    rss_por_marca: dict[str, list[float]] = {}
    ordem: list[str] = []
    totais: list[float] = []

    for i in range(1, args.n + 1):
        if args.frio:
            _limpar_caches_frios()
        marcas, total = _uma_rodada(cmd, log, env)
        if not marcas:
            print(f"  rodada {i}: nenhuma marca (app não abriu? ORGANIZADOR_PERF ok?)")
            continue
        # Normaliza "tree:preenchida(3214)" -> "tree:preenchida" para agrupar.
        for nome, ms, rss in marcas:
            chave = nome.split("(")[0]
            if chave not in por_marca:
                por_marca[chave] = []
                rss_por_marca[chave] = []
                ordem.append(chave)
            por_marca[chave].append(ms)
            rss_por_marca[chave].append(rss)
        if total == total:  # não-NaN
            totais.append(total)
        print(f"  rodada {i}: {len(marcas)} marcas, total externo {total / 1000:.1f}s")

    if not por_marca:
        return 1

    print(f"\n{'marca':<28} {'mediana':>10} {'delta':>9} {'RSS':>9}")
    print("-" * 60)
    anterior = 0.0
    for chave in ordem:
        med = statistics.median(por_marca[chave])
        rss = statistics.median(rss_por_marca[chave])
        print(f"{chave:<28} {med / 1000:>9.2f}s {(med - anterior) / 1000:>8.2f}s {rss:>7.0f}MB")
        anterior = med
    if totais:
        med_total = statistics.median(totais)
        print("-" * 60)
        print(f"{'total externo (com extração)':<28} {med_total / 1000:>9.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
