# -*- coding: utf-8 -*-
"""Auto-atualização embutida no Peticoes.exe (organizador/atualizacao.py).

Antes disto existia um Atualizador.exe separado, que matava o Peticoes.exe com
TerminateProcess para trocar o .exe. Agora o próprio app se substitui: baixa a
nova versão, renomeia A SI MESMO para .old (confirmado ao vivo que o Windows
permite isso com o processo rodando) e move o download para o nome original.

Este módulo testa a parte determinística (comparação de versão, log, troca de
arquivo). O download/checagem de release já são cobertos indiretamente pelos
mesmos handlers de erro do antigo updater — a lógica de rede não mudou.
"""

from __future__ import annotations

import sys

import pytest

from organizador import atualizacao


@pytest.fixture(autouse=True)
def _isolar(monkeypatch, tmp_path):
    monkeypatch.setenv("ORGANIZADOR_APP_DATA", str(tmp_path))
    monkeypatch.setattr(atualizacao, "APP_DATA_DIR", tmp_path)
    yield


def _log(tmp_path) -> str:
    p = tmp_path / "updater.log"
    return p.read_text(encoding="utf-8") if p.exists() else ""


# ---------------------------------------------------------------------
# Comparação de versões (o que decide se a janela aparece)
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "instalada, publicada, oferece",
    [
        ("1.9.0", "v1.9.3", True),    # desatualizada -> aparece
        ("1.9.3", "v1.9.3", False),   # em dia -> silencioso, correto
        ("1.9.4", "v1.9.3", False),   # à frente (build local) -> não regride
        ("1.8.4", "v1.10.0", True),   # 10 > 9: comparação numérica, não textual
        ("1.10.0", "v1.9.3", False),
    ],
)
def test_decisao_de_oferecer_update(instalada, publicada, oferece):
    assert (
        atualizacao.parse_version(publicada) > atualizacao.parse_version(instalada)
    ) is oferece


def test_parse_version_invalido_forca_oferta():
    """(0,) perde de qualquer versão real, então um valor ilegível nunca deixa a
    máquina presa numa versão antiga."""
    assert atualizacao.parse_version("") == (0,)
    assert atualizacao.parse_version(None) == (0,)
    assert atualizacao.parse_version("v1.9.3") > atualizacao.parse_version("qualquer coisa")


# ---------------------------------------------------------------------
# Caminho do próprio exe: fonte única da versão instalada (sem version.txt)
# ---------------------------------------------------------------------
def test_exe_atual_none_fora_do_build(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert atualizacao._exe_atual() is None


def test_exe_atual_e_o_proprio_executavel_no_build(monkeypatch, tmp_path):
    exe = tmp_path / "Peticoes.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe), raising=False)
    assert atualizacao._exe_atual() == exe.resolve()


# ---------------------------------------------------------------------
# limpar_sobras: restos de uma atualização anterior interrompida
# ---------------------------------------------------------------------
def test_limpar_sobras_remove_old_e_download(monkeypatch, tmp_path):
    exe = tmp_path / "Peticoes.exe"
    exe.write_bytes(b"atual")
    (tmp_path / "Peticoes.exe.old").write_bytes(b"velho")
    (tmp_path / "Peticoes.exe.download").write_bytes(b"parcial")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe), raising=False)

    atualizacao.limpar_sobras()

    assert exe.exists()
    assert not (tmp_path / "Peticoes.exe.old").exists()
    assert not (tmp_path / "Peticoes.exe.download").exists()


def test_limpar_sobras_no_op_fora_do_build(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    atualizacao.limpar_sobras()  # não pode levantar


# ---------------------------------------------------------------------
# substituir_a_si_mesmo: nunca escreve por cima do exe em execução, só
# renomeia — é essa permissão do Windows que dispensa um segundo processo.
# ---------------------------------------------------------------------
def test_substituir_troca_e_preserva_conteudo_novo(tmp_path):
    alvo = tmp_path / "Peticoes.exe"
    alvo.write_bytes(b"versao velha")
    novo = tmp_path / "Peticoes.exe.download"
    novo.write_bytes(b"versao nova")

    atualizacao.substituir_a_si_mesmo(novo, alvo)

    assert alvo.read_bytes() == b"versao nova"
    assert not novo.exists()
    assert not (tmp_path / "Peticoes.exe.old").exists()  # limpo no final


def test_substituir_propaga_erro_de_arquivo_travado(monkeypatch, tmp_path):
    """Se o rename falhar (ex.: outra instância do app com o mesmo exe aberto),
    quem chama precisa de uma mensagem para o usuário, não um traceback cru."""
    alvo = tmp_path / "Peticoes.exe"
    alvo.write_bytes(b"versao velha")
    novo = tmp_path / "Peticoes.exe.download"
    novo.write_bytes(b"versao nova")

    def _rename_falha(*_a, **_k):
        raise OSError("simulado: arquivo em uso")

    monkeypatch.setattr(atualizacao.os, "rename", _rename_falha)

    with pytest.raises(atualizacao.AppRunningError):
        atualizacao.substituir_a_si_mesmo(novo, alvo)
    assert alvo.read_bytes() == b"versao velha"  # nada foi perdido na falha


# ---------------------------------------------------------------------
# Log — é o que torna o silêncio do modo --auto diagnosticável
# ---------------------------------------------------------------------
def test_registra_no_app_data(tmp_path):
    atualizacao.registrar("mensagem de teste")
    conteudo = _log(tmp_path)
    assert "mensagem de teste" in conteudo
    assert conteudo.count("\n") == 1
    assert conteudo[:2].isdigit()  # traz data/hora


def test_registra_varias_linhas_em_ordem(tmp_path):
    atualizacao.registrar("primeira")
    atualizacao.registrar("segunda")
    linhas = _log(tmp_path).strip().splitlines()
    assert len(linhas) == 2
    assert "primeira" in linhas[0] and "segunda" in linhas[1]


def test_log_nao_cresce_sem_limite(tmp_path):
    """Roda em toda abertura do app; não pode virar um arquivo gigante."""
    for i in range(320):
        atualizacao.registrar(f"linha {i}")
    linhas = _log(tmp_path).strip().splitlines()
    assert len(linhas) <= 200
    assert "linha 319" in linhas[-1], "manteve as antigas em vez das recentes"


def test_registrar_nunca_levanta(tmp_path):
    """Log é acessório: falha de disco não pode derrubar a checagem."""
    alvo = tmp_path / "updater.log"
    alvo.mkdir(parents=True)  # diretório onde deveria haver arquivo
    atualizacao.registrar("nao deve levantar")


def test_caminho_do_log_segue_o_app_data(tmp_path):
    assert atualizacao._caminho_log() == tmp_path / "updater.log"


def test_log_fica_junto_do_resto_do_estado_do_app(monkeypatch):
    """Mesma pasta de ui_state.json/perf.log — é onde alguém vai procurar."""
    monkeypatch.delenv("ORGANIZADOR_APP_DATA", raising=False)
    import importlib

    import organizador.config as C

    importlib.reload(C)
    monkeypatch.setattr(atualizacao, "APP_DATA_DIR", C.APP_DATA_DIR)
    assert atualizacao._caminho_log().parent == C.APP_DATA_DIR
