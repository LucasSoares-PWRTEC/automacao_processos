# -*- coding: utf-8 -*-
"""Instrumentação de abertura (organizador.perf).

Requisitos que importam: desligada por padrão (custo zero no uso normal), nunca
levanta exceção mesmo com destino inválido, e quando ligada grava uma linha por
marca no formato ``nome;ms;rss``.
"""

from __future__ import annotations

import organizador.perf as perf


def _ligar(monkeypatch, tmp_path):
    log = tmp_path / "perf.log"
    monkeypatch.setattr(perf, "ATIVO", True)
    monkeypatch.setenv(perf._PERF_LOG_ENV, str(log))
    return log


def test_desligada_por_padrao_nao_grava_nada(monkeypatch, tmp_path):
    log = tmp_path / "perf.log"
    monkeypatch.setattr(perf, "ATIVO", False)
    monkeypatch.setenv(perf._PERF_LOG_ENV, str(log))
    perf.marca("qualquer")
    perf.reiniciar_log()
    assert not log.exists()


def test_marca_grava_nome_tempo_e_rss(monkeypatch, tmp_path):
    log = _ligar(monkeypatch, tmp_path)
    perf.marca("primeira")
    perf.marca("segunda")

    linhas = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(linhas) == 2
    nomes, tempos = [], []
    for linha in linhas:
        nome, ms, rss = linha.split(";")
        nomes.append(nome)
        tempos.append(float(ms))
        assert float(rss) >= 0.0
    assert nomes == ["primeira", "segunda"]
    assert tempos[0] <= tempos[1]  # monotônico
    assert tempos[0] >= 0.0


def test_reiniciar_log_apaga(monkeypatch, tmp_path):
    log = _ligar(monkeypatch, tmp_path)
    perf.marca("x")
    assert log.exists()
    perf.reiniciar_log()
    assert not log.exists()
    perf.reiniciar_log()  # idempotente: não levanta se já não existe


def test_destino_invalido_nao_derruba_o_app(monkeypatch, tmp_path):
    """A instrumentação é acessória: falha de escrita nunca pode virar exceção
    no caminho de abertura."""
    monkeypatch.setattr(perf, "ATIVO", True)
    # Um diretório no lugar do arquivo: abrir para escrita falha.
    destino = tmp_path / "sou_um_diretorio"
    destino.mkdir()
    monkeypatch.setenv(perf._PERF_LOG_ENV, str(destino))
    perf.marca("nao_deve_levantar")
    perf.reiniciar_log()


def test_destino_default_usa_app_data(monkeypatch):
    monkeypatch.delenv(perf._PERF_LOG_ENV, raising=False)
    from organizador.config import APP_DATA_DIR

    assert perf._destino() == APP_DATA_DIR / "perf.log"


def test_rss_reflete_alocacao():
    antes = perf._rss_mb()
    assert antes > 0.0  # o próprio pytest já ocupa memória
    bloco = bytearray(80 * 1024 * 1024)
    depois = perf._rss_mb()
    assert depois > antes
    del bloco
