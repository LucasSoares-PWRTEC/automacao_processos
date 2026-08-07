# -*- coding: utf-8 -*-
"""Controles de recurso da passada de monitoramento.

O motor completo (`rodar_passada`) exige Chrome, 2FA e as planilhas Google e está
fora do escopo de teste automatizado. As duas peças que protegem uma máquina de
4 GB, porém, são lógica pura e são testadas aqui:

  - `PoolScrapers`: teto LRU de navegadores simultâneos;
  - `_GravadorThrottled`: frequência de gravação do estado da passada.
"""

from __future__ import annotations

import json

import pytest

from organizador import monitoramento as M
from organizador.monitoramento import (
    MAX_NAVEGADORES,
    MAX_NAVEGADORES_MODO_LEVE,
    PoolScrapers,
    _GravadorThrottled,
    _gravar_estado,
    _teto_navegadores,
)


class _ScraperFalso:
    def __init__(self, rotulo: str, registro: list):
        self.rotulo = rotulo
        self._registro = registro
        self.aberto = True

    def login(self):
        self._registro.append(("login", self.rotulo))

    def fechar(self):
        self.aberto = False
        self._registro.append(("fechar", self.rotulo))


@pytest.fixture
def pool_falso(monkeypatch):
    """PoolScrapers com criar_scraper trocado por um dublê, e TJ_SISTEMAS mínimo."""
    registro: list = []
    criados: dict[str, _ScraperFalso] = {}

    def criar(site, estado, url, headless, perfil):
        s = _ScraperFalso(url, registro)
        criados[url] = s
        return s

    monkeypatch.setattr(M, "criar_scraper", criar)
    monkeypatch.setattr(
        M, "TJ_SISTEMAS",
        {f"TJ{i}": ("eproc", "SAO PAULO", f"url{i}", None) for i in range(1, 7)},
    )
    return registro, criados


# ---------------------------------------------------------------------
# PoolScrapers: reuso + teto LRU
# ---------------------------------------------------------------------
def test_reusa_a_mesma_sessao_do_sistema(pool_falso):
    """O ponto do pool: 1 login por sistema, não 1 por processo (evita bloqueio)."""
    registro, _ = pool_falso
    pool = PoolScrapers(headless=True, maximo=4)
    a = pool.obter("TJ1")
    b = pool.obter("TJ1")
    assert a is b
    assert registro.count(("login", "url1")) == 1


def test_teto_fecha_o_menos_usado_recentemente(pool_falso):
    registro, criados = pool_falso
    pool = PoolScrapers(headless=True, maximo=2)
    pool.obter("TJ1")
    pool.obter("TJ2")
    # Usa TJ1 de novo: TJ2 passa a ser o mais antigo.
    pool.obter("TJ1")
    pool.obter("TJ3")  # estoura o teto -> fecha TJ2

    assert ("fechar", "url2") in registro
    assert ("fechar", "url1") not in registro
    assert not criados["url2"].aberto
    assert set(pool._scrapers) == {"TJ1", "TJ3"}


def test_nunca_passa_do_teto(pool_falso):
    _registro, criados = pool_falso
    pool = PoolScrapers(headless=True, maximo=2)
    for rotulo in ("TJ1", "TJ2", "TJ3", "TJ4", "TJ5"):
        pool.obter(rotulo)
        assert len(pool._scrapers) <= 2
    assert sum(1 for s in criados.values() if s.aberto) == 2


def test_fechar_todos_fecha_e_limpa(pool_falso):
    _registro, criados = pool_falso
    pool = PoolScrapers(headless=True, maximo=4)
    pool.obter("TJ1")
    pool.obter("TJ2")
    pool.fechar_todos()
    assert pool._scrapers == {}
    assert all(not s.aberto for s in criados.values())


def test_login_que_falhou_nao_e_retentado(pool_falso, monkeypatch):
    _registro, _ = pool_falso

    def criar_que_falha(**_kw):
        raise RuntimeError("2FA recusado")

    monkeypatch.setattr(M, "criar_scraper", criar_que_falha)
    pool = PoolScrapers(headless=True, maximo=4)
    with pytest.raises(RuntimeError, match="falha ao logar"):
        pool.obter("TJ1")
    with pytest.raises(RuntimeError, match="já falhou nesta corrida"):
        pool.obter("TJ1")


def test_fechar_tolera_scraper_que_estoura(pool_falso):
    _registro, _ = pool_falso
    pool = PoolScrapers(headless=True, maximo=1)

    class _Ruim(_ScraperFalso):
        def fechar(self):
            raise RuntimeError("driver morreu")

    pool._scrapers["TJ1"] = _Ruim("url1", [])
    pool.fechar_todos()  # não pode propagar
    assert pool._scrapers == {}


def test_teto_por_modo_leve(monkeypatch, tmp_path):
    estado = tmp_path / "ui_state.json"
    monkeypatch.setattr(M, "MAX_NAVEGADORES", MAX_NAVEGADORES)

    import organizador.config as C
    monkeypatch.setattr(C, "UI_STATE_PATH", estado)

    estado.write_text(json.dumps({"modo_leve": True}), encoding="utf-8")
    assert _teto_navegadores() == MAX_NAVEGADORES_MODO_LEVE

    estado.write_text(json.dumps({"modo_leve": False}), encoding="utf-8")
    assert _teto_navegadores() == MAX_NAVEGADORES


def test_teto_sem_ui_state_nao_estoura(monkeypatch, tmp_path):
    import organizador.config as C
    monkeypatch.setattr(C, "UI_STATE_PATH", tmp_path / "nao_existe.json")
    assert _teto_navegadores() in (MAX_NAVEGADORES, MAX_NAVEGADORES_MODO_LEVE)


# ---------------------------------------------------------------------
# Gravação do estado
# ---------------------------------------------------------------------
def test_estado_gravado_sem_indentacao_e_atomico(tmp_path):
    path = tmp_path / "estado.json"
    _gravar_estado({"111": {"status": "ok"}}, path)
    texto = path.read_text(encoding="utf-8")
    assert texto == '{"111":{"status":"ok"}}'
    assert not path.with_suffix(path.suffix + ".tmp").exists()
    assert json.loads(texto) == {"111": {"status": "ok"}}


def test_gravacao_regulada_por_lote(tmp_path, monkeypatch):
    """Antes o estado inteiro era regravado após CADA processo."""
    path = tmp_path / "estado.json"
    monkeypatch.setattr(M, "GRAVAR_ESTADO_CADA", 3)
    monkeypatch.setattr(M, "GRAVAR_ESTADO_SEGUNDOS", 9999.0)
    g = _GravadorThrottled(path)

    assert g.talvez({"n": 1}) is False
    assert g.talvez({"n": 2}) is False
    assert not path.exists(), "gravou antes de fechar o lote"
    assert g.talvez({"n": 3}) is True
    assert json.loads(path.read_text(encoding="utf-8")) == {"n": 3}


def test_gravacao_regulada_por_tempo(tmp_path, monkeypatch):
    path = tmp_path / "estado.json"
    monkeypatch.setattr(M, "GRAVAR_ESTADO_CADA", 10_000)
    monkeypatch.setattr(M, "GRAVAR_ESTADO_SEGUNDOS", 0.0)  # sempre "já passou"
    g = _GravadorThrottled(path)
    assert g.talvez({"n": 1}) is True
    assert json.loads(path.read_text(encoding="utf-8")) == {"n": 1}


def test_finalizar_grava_o_que_estava_pendente(tmp_path, monkeypatch):
    """A passada sempre termina com finalizar(): nada fica só na memória."""
    path = tmp_path / "estado.json"
    monkeypatch.setattr(M, "GRAVAR_ESTADO_CADA", 10_000)
    monkeypatch.setattr(M, "GRAVAR_ESTADO_SEGUNDOS", 9999.0)
    g = _GravadorThrottled(path)
    g.talvez({"n": 1})
    assert not path.exists()
    g.finalizar({"n": 1, "fim": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"n": 1, "fim": True}


def test_contador_reinicia_apos_gravar(tmp_path, monkeypatch):
    path = tmp_path / "estado.json"
    monkeypatch.setattr(M, "GRAVAR_ESTADO_CADA", 2)
    monkeypatch.setattr(M, "GRAVAR_ESTADO_SEGUNDOS", 9999.0)
    g = _GravadorThrottled(path)
    assert [g.talvez({}) for _ in range(4)] == [False, True, False, True]
