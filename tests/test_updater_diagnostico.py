# -*- coding: utf-8 -*-
"""Diagnóstico do auto-updater (updater/updater.py).

Problema relatado: "as outras máquinas não estão aparecendo para atualizar".

O executável funciona — verificado rodando `Atualizador.exe --auto` numa pasta
simulada: com `version.txt` desatualizado (ou ausente) a janela aparece, e com a
versão em dia ele sai calado, como deve. O que faltava era poder DIAGNOSTICAR à
distância: no modo `--auto` todo erro é silencioso de propósito (não incomodar a
cada abertura), então "token expirado", "sem internet", "proxy bloqueando",
"Atualizador não instalado ao lado" e "já está atualizado" produziam exatamente o
mesmo nada.

Duas correções travadas aqui:
  1. registro de cada desfecho em ~/.automacao_processos/updater.log;
  2. `--versao-atual`, passado pelo app: o `version.txt` é um arquivo à parte que
     pode ficar velho ou ser copiado sem o executável, e nesse caso o Atualizador
     concluía "já atualizado" e não mostrava nada.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "updater"))

import updater  # noqa: E402


@pytest.fixture(autouse=True)
def _isolar(monkeypatch, tmp_path):
    monkeypatch.setenv("ORGANIZADOR_APP_DATA", str(tmp_path))
    monkeypatch.setattr(updater, "_versao_informada", None)
    yield
    monkeypatch.setattr(updater, "_versao_informada", None)


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
        ("0.0.0", "v1.9.3", True),    # version.txt ausente
        ("", "v1.9.3", True),         # version.txt vazio/ilegível
        ("lixo", "v1.9.3", True),     # conteúdo inválido -> força a oferta
        ("1.8.4", "v1.10.0", True),   # 10 > 9: comparação numérica, não textual
        ("1.10.0", "v1.9.3", False),
    ],
)
def test_decisao_de_oferecer_update(instalada, publicada, oferece):
    assert (
        updater.parse_version(publicada) > updater.parse_version(instalada)
    ) is oferece


def test_parse_version_invalido_forca_oferta():
    """(0,) perde de qualquer versão real, então um valor ilegível nunca deixa a
    máquina presa numa versão antiga."""
    assert updater.parse_version("") == (0,)
    assert updater.parse_version(None) == (0,)
    assert updater.parse_version("v1.9.3") > updater.parse_version("qualquer coisa")


# ---------------------------------------------------------------------
# --versao-atual: a versão do app vence o version.txt
# ---------------------------------------------------------------------
def test_versao_informada_tem_precedencia(monkeypatch, tmp_path):
    """O `version.txt` pode ter sido copiado sem o executável correspondente; o
    app sabe a própria versão."""
    monkeypatch.setattr(updater, "base_dir", lambda: tmp_path)
    (tmp_path / "version.txt").write_text("9.9.9", encoding="ascii")
    assert updater.versao_instalada() == "9.9.9"

    monkeypatch.setattr(updater, "_versao_informada", "1.9.3")
    assert updater.versao_instalada() == "1.9.3"


def test_sem_argumento_cai_no_version_txt(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "base_dir", lambda: tmp_path)
    (tmp_path / "version.txt").write_text("1.8.4", encoding="ascii")
    assert updater.versao_instalada() == "1.8.4"


def test_version_txt_ausente_vira_zero(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "base_dir", lambda: tmp_path / "vazio")
    assert updater.versao_instalada() == "0.0.0"


@pytest.mark.parametrize(
    "argv, esperado",
    [
        (["--versao-atual", "1.9.3"], "1.9.3"),
        (["--auto", "--versao-atual", "1.9.3", "--parent-pid", "42"], "1.9.3"),
        (["--auto"], None),
        (["--versao-atual"], None),          # flag sem valor
        (["--versao-atual", "  "], None),    # valor em branco
    ],
)
def test_leitura_do_argumento(argv, esperado):
    assert updater._arg_str(argv, "--versao-atual") == esperado


def test_parent_pid_continua_sendo_lido():
    assert updater._arg_int(["--parent-pid", "1234"], "--parent-pid") == 1234
    assert updater._arg_int(["--parent-pid", "abc"], "--parent-pid") is None
    assert updater._arg_int(["--auto"], "--parent-pid") is None


# ---------------------------------------------------------------------
# Log — é o que torna o silêncio diagnosticável
# ---------------------------------------------------------------------
def test_registra_no_app_data(tmp_path):
    updater.registrar("mensagem de teste")
    conteudo = _log(tmp_path)
    assert "mensagem de teste" in conteudo
    assert conteudo.count("\n") == 1
    # Traz data/hora: sem isso não dá para saber se é da execução de hoje.
    assert conteudo[:2].isdigit()


def test_registra_varias_linhas_em_ordem(tmp_path):
    updater.registrar("primeira")
    updater.registrar("segunda")
    linhas = _log(tmp_path).strip().splitlines()
    assert len(linhas) == 2
    assert "primeira" in linhas[0] and "segunda" in linhas[1]


def test_log_nao_cresce_sem_limite(tmp_path):
    """Roda em toda abertura do app; não pode virar um arquivo gigante."""
    for i in range(320):
        updater.registrar(f"linha {i}")
    linhas = _log(tmp_path).strip().splitlines()
    assert len(linhas) <= 200
    assert "linha 319" in linhas[-1], "manteve as antigas em vez das recentes"


def test_registrar_nunca_levanta(monkeypatch, tmp_path):
    """Log é acessório: falha de disco não pode derrubar a checagem."""
    alvo = tmp_path / "updater.log"
    alvo.mkdir(parents=True)  # diretório onde deveria haver arquivo
    updater.registrar("nao deve levantar")


def test_registrar_tolera_app_data_apontando_para_arquivo(monkeypatch, tmp_path):
    """Se ORGANIZADOR_APP_DATA apontar para um ARQUIVO, o mkdir falha — e mesmo
    assim a checagem de atualização tem de seguir."""
    arquivo = tmp_path / "sou_um_arquivo"
    arquivo.write_text("x", encoding="utf-8")
    monkeypatch.setenv("ORGANIZADOR_APP_DATA", str(arquivo))
    updater.registrar("nao deve levantar")


def test_caminho_do_log_segue_o_app_data(monkeypatch, tmp_path):
    monkeypatch.setenv("ORGANIZADOR_APP_DATA", str(tmp_path / "outro"))
    assert updater._caminho_log() == tmp_path / "outro" / "updater.log"


def test_log_fica_junto_do_resto_do_estado_do_app(monkeypatch):
    """Mesma pasta de ui_state.json/perf.log — é onde alguém vai procurar."""
    monkeypatch.delenv("ORGANIZADOR_APP_DATA", raising=False)
    from organizador.config import APP_DATA_DIR

    assert updater._caminho_log().parent == APP_DATA_DIR


# ---------------------------------------------------------------------
# O lado do app: Atualizador ausente é o caso 100% silencioso
# ---------------------------------------------------------------------
def test_app_registra_quando_o_atualizador_nao_existe(monkeypatch, tmp_path):
    import importlib

    monkeypatch.setenv("ORGANIZADOR_APP_DATA", str(tmp_path))
    import organizador.config as C

    importlib.reload(C)
    import organizador.gui as G

    monkeypatch.setattr(G, "APP_DATA_DIR", tmp_path)
    G._registrar_updater("Atualizador.exe NAO encontrado em C:\\qualquer")

    conteudo = (tmp_path / "updater.log").read_text(encoding="utf-8")
    assert "[app]" in conteudo, "a origem da linha (app x atualizador) importa"
    assert "NAO encontrado" in conteudo
    importlib.reload(C)
