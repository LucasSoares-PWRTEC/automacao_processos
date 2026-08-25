# -*- coding: utf-8 -*-
"""Perfil de hardware da máquina + decisão do "Modo leve".

O app roda tanto em máquinas novas quanto em i3 de 3ª geração com 4 GB de RAM. O
que é razoável numa é inviável na outra: subir um Chrome headless de pré-login
(300-500 MB) ao abrir custa pouco em 16 GB e joga a máquina de 4 GB em swap.

Em vez de exigir que o usuário descubra isso, o app mede a máquina na primeira
execução e liga o Modo leve sozinho quando ela é fraca. O valor fica em
``ui_state.json`` e pode ser sobreposto à mão em Configurações — a detecção nunca
sobrescreve uma escolha explícita.

O que o Modo leve muda está em ``AJUSTES_MODO_LEVE`` (e é o que a aba
Configurações mostra ao usuário).

Só stdlib: ``ctypes``/``os``. Nada de psutil (seriam ~2 MB no bundle para ler
dois números).
"""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass

# Limites da detecção automática. 6 GB (não 4) porque o Windows 10 já come
# ~2 GB: numa máquina de 8 GB o app tem folga, numa de 4-6 GB não tem.
RAM_MINIMA_MB = 6144
NUCLEOS_MINIMOS = 4

# Resumo, para exibir em Configurações.
AJUSTES_MODO_LEVE = (
    "não abre o Chrome do Autojur ao iniciar (o pré-login vira manual)",
    "não sincroniza planilhas automaticamente (só pelo botão)",
    "mostra a lista de processos em páginas de 300 linhas",
    "usa menos temporizadores de interface",
    "mantém no máximo 2 navegadores abertos durante o monitoramento",
    "encerra o navegador oculto do Autojur após 5 min sem uso (15 min nas demais)",
)


@dataclass(frozen=True)
class PerfilMaquina:
    ram_total_mb: int   # 0 quando não foi possível medir
    nucleos: int

    @property
    def fraca(self) -> bool:
        """RAM ou CPU abaixo do piso.

        `ram_total_mb == 0` significa "não sei medir" — nesse caso NÃO assumimos
        máquina fraca (não vale degradar a experiência por falha de medição);
        a decisão fica só com a contagem de núcleos."""
        pouca_ram = 0 < self.ram_total_mb < RAM_MINIMA_MB
        poucos_nucleos = self.nucleos < NUCLEOS_MINIMOS
        return pouca_ram or poucos_nucleos

    def descricao(self) -> str:
        ram = f"{self.ram_total_mb / 1024:.1f} GB" if self.ram_total_mb else "RAM ?"
        return f"{ram}, {self.nucleos} núcleo(s)"


def _ram_total_mb() -> int:
    """RAM física total em MB; 0 se não der para descobrir."""
    if os.name == "nt":
        try:
            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]
            kernel32.GlobalMemoryStatusEx.restype = ctypes.c_int

            st = _MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return int(st.ullTotalPhys // (1024 * 1024))
        except Exception:
            pass
        return 0
    try:  # POSIX (só para o app rodar em dev fora do Windows)
        return int(
            os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1024 * 1024)
        )
    except (OSError, ValueError, AttributeError):
        return 0


def perfil_maquina() -> PerfilMaquina:
    return PerfilMaquina(ram_total_mb=_ram_total_mb(), nucleos=os.cpu_count() or 1)


def modo_leve_automatico() -> bool:
    """Sugestão para a 1ª execução; depois vale o que estiver no ui_state."""
    return perfil_maquina().fraca


def resolver_modo_leve(salvo: object) -> bool:
    """Decide o Modo leve: escolha do usuário, se houver; senão, detecção.

    `salvo` é o valor de ``ui_state.json`` — ``None``/ausente significa "nunca
    foi decidido". Aceita bool ou string ("true"/"1"), porque o ui_state é JSON
    editado a mão de vez em quando."""
    if isinstance(salvo, bool):
        return salvo
    if isinstance(salvo, str) and salvo.strip():
        return salvo.strip().lower() in ("1", "true", "sim", "yes", "on")
    if isinstance(salvo, (int, float)):
        return bool(salvo)
    return modo_leve_automatico()
