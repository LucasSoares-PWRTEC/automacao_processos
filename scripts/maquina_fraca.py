# -*- coding: utf-8 -*-
"""Simula uma máquina fraca NESTA máquina, para medir a abertura sem depender de
emprestar o i3 do escritório.

O QUE ISTO É: estrangulamento de CPU. O processo alvo é preso a UM núcleo e N
processos "ladrões" são presos ao MESMO núcleo, disputando o tempo de CPU. Com N
ladrões o alvo fica com ~1/(N+1) do núcleo, o que imita um relógio mais lento.
Nada é alterado na máquina: sem admin, sem powercfg, sem plano de energia. Os
ladrões são mortos no fim do teste.

O QUE ISTO **NÃO** É:
  * Não simula disco lento (HD mecânico). A abertura também lê a base SQLite e
    os modelos; num HD isso pesa e aqui não aparece.
  * Não simula pouca RAM nem swap.
  * Trabalho de GDI/fonte (o fallback de glifo — ver o docstring do gui.py) não
    escala igual a CPU pura, então para ele o fator é só aproximado.
  * Um i3 real também tem IPC e cache menores; aqui só o tempo é esticado.
Serve para saber se um ganho ESCALA e com que ordem de grandeza — não para
prever o número exato do i3.

A RÉGUA: antes de medir, roda um laço de CPU conhecido com e sem os ladrões e
imprime o fator obtido. É o que troca "parece mais lento" por "está ~4,2× mais
lento".

Uso:
    python scripts/maquina_fraca.py --calibrar --ladroes 3
    python scripts/maquina_fraca.py --abertura --ladroes 3
    python scripts/maquina_fraca.py --ladroes 3 -- python -m pytest tests/ -q
"""

from __future__ import annotations

import argparse
import ctypes
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
_WIN = os.name == "nt"

# Preâmbulo que o processo FILHO executa para se prender ao núcleo 0. Precisa
# rodar antes dos imports pesados, por isso é texto injetado e não uma função
# importada daqui.
_PRENDER = (
    "import ctypes\n"
    "_k = ctypes.WinDLL('kernel32')\n"
    "_k.GetCurrentProcess.restype = ctypes.c_void_p\n"
    "_k.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]\n"
    "_k.SetProcessAffinityMask(_k.GetCurrentProcess(), 1)\n"
) if _WIN else (
    "import os\n"
    "os.sched_setaffinity(0, {0})\n"
)


def fixar_no_nucleo(nucleo: int, pid: int) -> bool:
    """Prende um processo JÁ EM EXECUÇÃO a um único núcleo. True se conseguiu."""
    if not _WIN:
        try:
            os.sched_setaffinity(pid, {nucleo})  # type: ignore[attr-defined]
            return True
        except Exception:
            return False
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    k.OpenProcess.restype = ctypes.c_void_p
    k.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    k.SetProcessAffinityMask.restype = ctypes.c_int
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    PROCESS_SET_INFORMATION, PROCESS_QUERY_INFORMATION = 0x0200, 0x0400
    h = k.OpenProcess(PROCESS_SET_INFORMATION | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        return False
    try:
        return bool(k.SetProcessAffinityMask(h, 1 << nucleo))
    finally:
        k.CloseHandle(h)


class Ladroes:
    """Sobe/derruba os processos que disputam CPU com o alvo.

    Dois modos, e a escolha MUDA o resultado:

    `espalhar=True` (padrão): ladrões livres em todos os núcleos, alvo livre
    para migrar. Sobrecarrega a máquina de forma uniforme.

    `espalhar=False`: alvo e ladrões todos presos ao núcleo 0. NÃO use para
    medir imports — o resultado é patológico. Com 4 processos socando o mesmo
    núcleo, cada troca de contexto joga fora o L1/L2, e trabalho cache-hostil
    (import: descompactar .pyc, criar classes, carregar DLL) degrada muito além
    da perda de CPU: medido aqui, `PIL.Image` 32x e `logging` 70x, enquanto a
    régua de CPU marcava 3,7x. Um laço `s += i*i` tem working set minúsculo e
    sobrevive às trocas, então essa régua SUBESTIMA o caso do import. Ficou
    como opção porque para CPU-bound de working set pequeno é mais previsível.

    Laço apertado sem alocar: não queremos o GC do ladrão poluindo a medição.
    O `__exit__` mata todos — inclusive se a medição levantar exceção."""

    # DEAD-MAN SWITCH: o ladrão morre sozinho depois de TETO_S, mesmo que este
    # processo seja morto antes do __exit__ (timeout, Ctrl-C, kill). Sem isso um
    # laço infinito queimando CPU sobrevive na máquina de quem só queria medir
    # — aconteceu aqui, e o `x += 1` não tem como saber que ficou órfão.
    TETO_S = 900

    _LACO = (
        "import time\n"
        "_fim = time.monotonic() + {teto}\n"
        "x = 0\n"
        "while time.monotonic() < _fim:\n"
        "    for _ in range(100000):\n"
        "        x += 1\n"
    )

    def __init__(self, quantos: int, espalhar: bool = True, teto_s: int | None = None):
        self.quantos = max(0, quantos)
        self.espalhar = espalhar
        self.teto_s = teto_s or self.TETO_S
        self.procs: list[subprocess.Popen] = []

    def __enter__(self) -> "Ladroes":
        laco = self._LACO.format(teto=self.teto_s)
        codigo = laco if self.espalhar else (_PRENDER + laco)
        for _ in range(self.quantos):
            p = subprocess.Popen(
                [sys.executable, "-c", codigo],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if not self.espalhar:
                fixar_no_nucleo(0, p.pid)
            self.procs.append(p)
        if self.procs:
            time.sleep(0.5)  # deixa os ladrões de fato ocuparem a CPU
        return self

    def __exit__(self, *_exc) -> None:
        for p in self.procs:
            try:
                p.kill()
            except Exception:
                pass
        for p in self.procs:
            try:
                p.wait(timeout=5)
            except Exception:
                pass
        self.procs.clear()


# --------------------------------------------------------------------------
# régua: quanto mais lento ficou?
# --------------------------------------------------------------------------
_REGUA_CPU = (
    "import time\n"
    "t0 = time.perf_counter()\n"
    "s = 0\n"
    "for i in range(4000000):\n"
    "    s += i * i\n"
    "print((time.perf_counter() - t0) * 1000)\n"
)


# A régua de laço apertado mede CPU com working set minúsculo. O que o app faz
# na abertura é sobretudo IMPORTAR (descompactar .pyc, criar classes, carregar
# DLLs) — trabalho cache-hostil, que degrada diferente. Medimos as DUAS e
# mostramos as duas: se divergirem muito, o fator "certo" depende do que se está
# medindo, e é honesto dizer isso em vez de escolher o número que agrada.
_REGUA_IMPORT = (
    "import subprocess, sys, time\n"
    "MODS = ['json','logging','traceback','dataclasses','argparse','difflib',\n"
    "        'sqlite3','xml.etree.ElementTree','email','http.client','decimal',\n"
    "        'zipfile','csv','unittest','pickle','typing','statistics']\n"
    "t0 = time.perf_counter()\n"
    "subprocess.run([sys.executable,'-c','import '+','.join(MODS)],\n"
    "               capture_output=True)\n"
    "print((time.perf_counter()-t0)*1000)\n"
)


def _rodar_medindo(codigo: str, n: int, prender: bool = False) -> list[list[float]]:
    """Roda `codigo` n vezes num processo novo; devolve os números de cada saída.

    `prender`: o filho se prende ao núcleo 0 (só no modo --nucleo-unico; no modo
    espalhado o alvo fica livre para migrar, que é o ponto)."""
    if prender:
        codigo = _PRENDER + codigo
    saidas: list[list[float]] = []
    for _ in range(n):
        r = subprocess.run([sys.executable, "-c", codigo],
                           capture_output=True, text=True, cwd=str(_RAIZ))
        try:
            saidas.append([float(x) for x in r.stdout.split()])
        except ValueError:
            continue
    return saidas


def _mediana(codigo: str, n: int, coluna: int = 0, prender: bool = False) -> float:
    vals = [s[coluna] for s in _rodar_medindo(codigo, n, prender) if len(s) > coluna]
    return statistics.median(vals) if vals else float("nan")


def calibrar(ladroes: int, n: int = 3, espalhar: bool = True) -> float:
    """Imprime as duas réguas e devolve o fator da régua de IMPORT (a que
    corresponde ao que a abertura do app realmente faz)."""
    fatores = {}
    for rotulo, codigo in (("CPU (laço apertado)", _REGUA_CPU),
                           ("import de módulos", _REGUA_IMPORT)):
        livre = _mediana(codigo, n, prender=not espalhar)
        with Ladroes(ladroes, espalhar=espalhar):
            lento = _mediana(codigo, n, prender=not espalhar)
        f = lento / livre if livre else float("nan")
        fatores[rotulo] = f
        print(f"régua {rotulo:22} {livre:7.0f} ms livre -> "
              f"{lento:7.0f} ms  = {f:.2f}x")
    modo = "espalhados" if espalhar else "presos ao núcleo 0"
    print(f"({ladroes} ladrão(ões) {modo})")
    return fatores["import de módulos"]


# --------------------------------------------------------------------------
# medição da abertura
# --------------------------------------------------------------------------
_ABERTURA = (
    "import sys, time\n"
    "sys.path.insert(0, %r)\n"
    "t0 = time.perf_counter()\n"
    "import organizador.gui as G\n"
    "t_imp = time.perf_counter()\n"
    "for _m in ('_carregar_pesquisa_inicial', '_carregar_templates',\n"
    "           '_verificar_atualizacao_silenciosa', '_detectar_word_pdf'):\n"
    "    setattr(G.App, _m, lambda self: None)\n"
    "app = G.App()\n"
    "t_fim = time.perf_counter()\n"
    "app.destroy()\n"
    "print((t_imp-t0)*1000, (t_fim-t_imp)*1000, (t_fim-t0)*1000)\n"
) % str(_RAIZ)


def medir_abertura(n: int, prender: bool = False) -> tuple[float, float, float] | None:
    linhas = [s for s in _rodar_medindo(_ABERTURA, n, prender) if len(s) == 3]
    if not linhas:
        return None
    return tuple(statistics.median(c) for c in zip(*linhas))  # type: ignore[return-value]


def limpar_orfaos() -> list[int]:
    """Mata ladrões que sobreviveram a uma execução interrompida.

    Identifica pelo formato exato do `-c` do ladrão, e NUNCA pelo texto
    "while True" solto: um filtro assim casa o próprio processo que faz a busca
    (o texto está no seu `-c`) e ele se mata — foi o que aconteceu na primeira
    tentativa de limpeza. Também poupa o próprio PID e o do pai."""
    import os
    try:
        import psutil
    except ImportError:
        print("psutil não instalado: mate manualmente ou espere o TETO_S do ladrão")
        return []
    poupar = {os.getpid(), os.getppid()}
    mortos: list[int] = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        if p.info["pid"] in poupar:
            continue
        cl = p.info["cmdline"] or []
        if len(cl) >= 3 and cl[1] == "-c" and "_fim = time.monotonic()" in cl[2]:
            try:
                p.kill()
                mortos.append(p.info["pid"])
            except Exception:
                pass
    return mortos


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ladroes", type=int, default=3,
                    help="processos disputando o núcleo 0 (0 = sem estrangular)")
    ap.add_argument("-n", type=int, default=3, help="repetições da medição")
    ap.add_argument("--limpar", action="store_true",
                    help="mata ladrões órfãos de uma execução anterior e sai")
    ap.add_argument("--calibrar", action="store_true", help="só imprime a régua")
    ap.add_argument("--abertura", action="store_true", help="mede a abertura do app")
    ap.add_argument("--nucleo-unico", action="store_true",
                    help="alvo e ladrões presos ao núcleo 0 (patológico p/ imports)")
    ap.add_argument("resto", nargs=argparse.REMAINDER,
                    help="-- <comando> para rodar estrangulado")
    args = ap.parse_args()

    if args.limpar:
        print(f"ladrões órfãos mortos: {limpar_orfaos()}")
        return

    espalhar = not args.nucleo_unico
    if args.calibrar:
        calibrar(args.ladroes, args.n, espalhar)
        return

    if args.abertura:
        fator = calibrar(args.ladroes, args.n, espalhar)
        print()
        livre = medir_abertura(args.n, prender=not espalhar)
        with Ladroes(args.ladroes, espalhar=espalhar):
            lento = medir_abertura(args.n, prender=not espalhar)
        if not livre or not lento:
            print("não consegui medir a abertura (o processo filho não respondeu)")
            return
        print(f"{'etapa':16} {'livre':>12} {'estrangulado':>14}")
        for rotulo, i in (("import gui", 0), ("App.__init__", 1), ("TOTAL", 2)):
            print(f"{rotulo:16} {livre[i]:9.0f} ms {lento[i]:11.0f} ms")
        print()
        print(f"fator observado na abertura: {lento[2] / livre[2]:.2f}x"
              f"   (régua de import: {fator:.2f}x)")
        return

    cmd = [a for a in args.resto if a != "--"]
    if not cmd:
        ap.error("passe --calibrar, --abertura, ou -- <comando>")
    with Ladroes(args.ladroes, espalhar=espalhar):
        subprocess.run(cmd, cwd=str(_RAIZ))


if __name__ == "__main__":
    main()
