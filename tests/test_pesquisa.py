# -*- coding: utf-8 -*-
"""Testes da lógica pura da aba Pesquisa de Processo."""

from __future__ import annotations

import gzip
import json
from dataclasses import asdict

from organizador.models import Parte, Prazo, Processo
from organizador.pesquisa import (
    carregar_processos_pesquisa,
    filtrar_processos,
    linhas_tree_processos,
    mesclar_processos_por_prioridade,
)


def _gravar_meta(base, proc: Processo) -> None:
    pasta = base / proc.numero
    pasta.mkdir(parents=True)
    (pasta / "metadados.json").write_text(
        json.dumps(asdict(proc), ensure_ascii=False),
        encoding="utf-8",
    )


def test_mesclar_processos_respeita_prioridade_e_deduplica_por_numero():
    base = Processo(numero="123", classe="base")
    online = Processo(numero="123", classe="online")
    embutido = Processo(numero="456", classe="snapshot")

    assert mesclar_processos_por_prioridade([base], [online], [embutido]) == [
        base,
        embutido,
    ]


def test_carregar_processos_pesquisa_prioriza_base_online_embutido(tmp_path):
    pasta_base = tmp_path / "base"
    online_dir = tmp_path / "online"
    pasta_base.mkdir()
    online_dir.mkdir()
    base_json = tmp_path / "processos_base.json.gz"

    _gravar_meta(pasta_base, Processo(numero="111", classe="curada"))
    _gravar_meta(online_dir, Processo(numero="111", classe="online-duplicado"))
    _gravar_meta(online_dir, Processo(numero="222", classe="online"))
    with gzip.open(base_json, "wt", encoding="utf-8") as fh:
        json.dump(
            [
                asdict(Processo(numero="222", classe="snapshot-duplicado")),
                asdict(Processo(numero="333", classe="snapshot")),
            ],
            fh,
        )

    procs = carregar_processos_pesquisa(pasta_base, online_dir, base_json)

    assert [(p.numero, p.classe) for p in procs] == [
        ("111", "curada"),
        ("222", "online"),
        ("333", "snapshot"),
    ]


def test_filtrar_processos_busca_numero_classe_autor_e_reu():
    proc = Processo(
        numero="4004459-04.2026.8.26.0248",
        classe="Execução",
        partes=[
            Parte(papel="EXEQUENTE", nome="Empresa Alfa"),
            Parte(papel="EXECUTADO", nome="Fulano Beta"),
        ],
    )
    outro = Processo(numero="999", classe="Conhecimento")

    assert filtrar_processos([proc, outro], "alfa") == [proc]
    assert filtrar_processos([proc, outro], "beta") == [proc]
    assert filtrar_processos([proc, outro], "4004459") == [proc]
    assert filtrar_processos([proc, outro], "execução") == [proc]
    assert filtrar_processos([proc, outro], "") == [proc, outro]


def test_linhas_tree_processos_monta_valores_estaveis():
    proc = Processo(
        numero="123",
        classe="Execução",
        valor_causa="R$ 1,00",
        ultima_movimentacao="10/07/2026 — despacho muito longo",
        partes=[
            Parte(papel="AUTOR", nome="Autor"),
            Parte(papel="RÉU", nome="Réu"),
        ],
        prazos_abertos=[Prazo(fim="15/07/2026")],
    )

    linhas = linhas_tree_processos([proc])

    assert linhas == [
        (
            "123",
            (
                "123",
                "Execução",
                "Autor",
                "Réu",
                "R$ 1,00",
                "10/07/2026 — despacho muito lo",
                "15/07/2026",
            ),
            "evenrow",
        )
    ]
