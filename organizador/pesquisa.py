# -*- coding: utf-8 -*-
"""Lógica pura da aba Pesquisa de Processo.

Mantém fora do Tkinter as operações que podem ficar caras com bases maiores:
carregamento/mescla de fontes, filtro e montagem das linhas exibidas na lista.
"""

from __future__ import annotations

from pathlib import Path

from .models import Processo
from .organizacao import (
    _autor,
    _carregar_processos,
    _reus_joined,
    carregar_processos_base,
)


def mesclar_processos_por_prioridade(*fontes: list[Processo]) -> list[Processo]:
    """Mescla fontes preservando a prioridade da esquerda para a direita.

    Processos com número vazio não são deduplicados, pois não há chave confiável.
    """
    resultados: list[Processo] = []
    vistos: set[str] = set()
    for fonte in fontes:
        for proc in fonte:
            numero = (proc.numero or "").strip()
            if numero:
                if numero in vistos:
                    continue
                vistos.add(numero)
            resultados.append(proc)
    return resultados


def carregar_processos_pesquisa(
    pasta_base: Path,
    processos_online_dir: Path,
    processos_base_json: Path,
) -> list[Processo]:
    """Carrega base curada, cache online e snapshot embutido, nesta prioridade."""
    pasta_base = Path(pasta_base).expanduser()
    base = _carregar_processos(pasta_base) if pasta_base.is_dir() else []
    online = _carregar_processos(processos_online_dir)
    embutidos = carregar_processos_base(processos_base_json)
    return mesclar_processos_por_prioridade(base, online, embutidos)


# As funções abaixo aceitam tanto Processo (que tem `.partes`, exige derivar
# autor/réu/prazo) quanto ResumoProcesso (que já traz `.autor`/`.reu`/`.prazo_fim`
# prontos). O duck-typing por `hasattr(x, "partes")` evita importar
# ResumoProcesso aqui (e o acoplamento/ciclo que isso traria).

def _autor_de(x) -> str:
    return _autor(x) if hasattr(x, "partes") else (x.autor or "")


def _reu_de(x) -> str:
    return _reus_joined(x) if hasattr(x, "partes") else (x.reu or "")


def _prazo_de(x) -> str:
    if hasattr(x, "partes"):
        return (x.prazos_abertos[0].fim or "?") if x.prazos_abertos else ""
    return x.prazo_fim or ""


def texto_filtro_processo(proc) -> str:
    return " ".join(
        (
            proc.numero or "",
            proc.classe or "",
            _autor_de(proc),
            _reu_de(proc),
        )
    ).lower()


def filtrar_processos(processos: list, filtro: str) -> list:
    termo = (filtro or "").lower().strip()
    if not termo:
        return list(processos)
    return [proc for proc in processos if termo in texto_filtro_processo(proc)]


def linhas_tree_processos(processos: list) -> list[tuple[str, tuple[str, ...], str]]:
    """Monta os dados consumidos pela Treeview sem tocar em widgets.

    Aceita Processo ou ResumoProcesso (mesmos campos de exibição)."""
    linhas: list[tuple[str, tuple[str, ...], str]] = []
    for idx, proc in enumerate(processos):
        tag = "oddrow" if idx % 2 else "evenrow"
        linhas.append((
            proc.numero,
            (
                proc.numero,
                proc.classe,
                _autor_de(proc),
                _reu_de(proc),
                proc.valor_causa,
                proc.ultima_movimentacao[:30],
                _prazo_de(proc),
            ),
            tag,
        ))
    return linhas
