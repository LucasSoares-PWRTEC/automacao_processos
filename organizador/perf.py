# -*- coding: utf-8 -*-
"""Instrumentação de abertura (opt-in por ``ORGANIZADOR_PERF=1``).

A máquina de desenvolvimento abre o app em segundos; as máquinas fracas do
escritório levam mais de um minuto. Sem medir NA MÁQUINA LENTA a otimização é
às cegas — este módulo grava marcas de tempo/memória num arquivo de texto para
comparar antes/depois.

Custo quando desligado (o padrão): ``marca()`` faz um ``if`` e retorna. Nada de
arquivo, nada de ctypes.

Uso:
    set ORGANIZADOR_PERF=1
    python organizar_processos.py
    type %USERPROFILE%\\.automacao_processos\\perf.log
"""

from __future__ import annotations

import os
import time

# Relógio zerado o mais cedo possível: este módulo é importado no topo de
# organizador.gui, antes dos imports pesados. Não é o instante real em que o
# processo nasceu (o bootloader do PyInstaller e o interpretador já rodaram),
# então a extração do onefile NÃO aparece aqui — ela é medida de fora, pelo
# scripts/bench_abertura.py.
_T0 = time.perf_counter()

ATIVO = os.getenv("ORGANIZADOR_PERF", "") not in ("", "0", "false", "False")

_PERF_LOG_ENV = "ORGANIZADOR_PERF_LOG"


def _destino() -> "os.PathLike[str] | str":
    alvo = os.getenv(_PERF_LOG_ENV)
    if alvo:
        return alvo
    # Import tardio: config resolve caminhos e não deve ser puxado quando a
    # instrumentação está desligada.
    from .config import APP_DATA_DIR

    return APP_DATA_DIR / "perf.log"


def _rss_mb() -> float:
    """Memória residente do processo, em MB. 0.0 se não der para medir."""
    if os.name != "nt":
        try:
            import resource

            return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
        except Exception:
            return 0.0
    try:
        import ctypes
        from ctypes import wintypes

        class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        # argtypes/restype explícitos: sem eles o HANDLE pseudo (-1) devolvido por
        # GetCurrentProcess é passado como int de 32 bits e a chamada falha em x64.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.argtypes = []
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
            wintypes.DWORD,
        ]

        contadores = _PROCESS_MEMORY_COUNTERS()
        contadores.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
        if not psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(contadores), contadores.cb
        ):
            return 0.0
        return round(contadores.WorkingSetSize / (1024 * 1024), 1)
    except Exception:
        return 0.0


def marca(nome: str) -> None:
    """Registra ``nome;ms_desde_o_import;rss_mb;pico_rss_mb``. Nunca levanta."""
    if not ATIVO:
        return
    try:
        ms = round((time.perf_counter() - _T0) * 1000, 1)
        linha = f"{nome};{ms};{_rss_mb()}\n"
        destino = _destino()
        os.makedirs(os.path.dirname(str(destino)) or ".", exist_ok=True)
        with open(destino, "a", encoding="utf-8") as fh:
            fh.write(linha)
    except Exception:
        pass


def reiniciar_log() -> None:
    """Apaga o log (usado pelo bench antes de cada rodada)."""
    if not ATIVO:
        return
    try:
        os.remove(str(_destino()))
    except OSError:
        pass
