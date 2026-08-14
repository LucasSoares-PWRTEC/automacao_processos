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


# ---------------------------------------------------------------------
# _rss_mb nos desfechos degradados
#
# A medição vai para dentro do kernel via ctypes. Se qualquer etapa falhar, a
# resposta tem de ser 0.0 — nunca uma exceção, porque quem chama é `marca()`,
# no caminho de abertura do app.
# ---------------------------------------------------------------------
def _fake_ctypes(monkeypatch, *, windll_falha=False, resultado=1, rss_bytes=0):
    """Troca ctypes.WinDLL por um dublê que não fala com o Windows de verdade."""
    import ctypes

    class _Funcao:
        def __init__(self, retorno):
            self._retorno = retorno
            self.restype = None
            self.argtypes = None

        def __call__(self, *a):
            if self._retorno == "handle":
                return 1
            if a and hasattr(a[1], "_obj"):
                a[1]._obj.WorkingSetSize = rss_bytes
            return resultado

    class _Lib:
        def __init__(self, nome):
            self.nome = nome

        def __getattr__(self, atributo):
            fn = _Funcao("handle" if atributo == "GetCurrentProcess" else resultado)
            object.__setattr__(self, atributo, fn)
            return fn

    def _windll(nome, **_k):
        if windll_falha:
            raise OSError(f"simulado: {nome} indisponível")
        return _Lib(nome)

    monkeypatch.setattr(ctypes, "WinDLL", _windll)


def test_rss_zero_quando_a_api_do_windows_recusa(monkeypatch):
    """GetProcessMemoryInfo devolvendo 0 (falso) é falha, não 0 MB de memória."""
    _fake_ctypes(monkeypatch, resultado=0)
    assert perf._rss_mb() == 0.0


def test_rss_converte_bytes_para_mb(monkeypatch):
    _fake_ctypes(monkeypatch, resultado=1, rss_bytes=150 * 1024 * 1024)
    assert perf._rss_mb() == 150.0


def test_rss_zero_quando_o_ctypes_explode(monkeypatch):
    """Máquina sem psapi/kernel32 acessível: 0.0, sem traceback."""
    _fake_ctypes(monkeypatch, windll_falha=True)
    assert perf._rss_mb() == 0.0


def test_rss_fora_do_windows_usa_o_resource(monkeypatch):
    """Ramo POSIX — o app roda em dev fora do Windows."""
    import sys
    import types

    recurso = types.ModuleType("resource")
    recurso.RUSAGE_SELF = 0
    recurso.getrusage = lambda _quem: types.SimpleNamespace(ru_maxrss=64 * 1024)
    monkeypatch.setitem(sys.modules, "resource", recurso)
    monkeypatch.setattr(perf.os, "name", "posix")
    assert perf._rss_mb() == 64.0


def test_rss_fora_do_windows_sem_resource_devolve_zero(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "resource", None)  # import -> ImportError
    monkeypatch.setattr(perf.os, "name", "posix")
    assert perf._rss_mb() == 0.0
