# -*- coding: utf-8 -*-
"""Artefatos de diagnóstico dos scrapers (organizador.integracoes...diagnostico).

O requisito central é NEGATIVO: com o debug desligado (o padrão), nada pode ser
gravado e o navegador não pode ser tocado. Screenshot e dump de DOM disparavam
em desfechos normais — "processo não está neste tribunal" é o caso comum, já que
o app tenta eproc × e-SAJ × PJe — e custavam 0,3-1,5 s (PNG) e 5-15 s (dump).
"""

from __future__ import annotations

import pytest

from organizador.integracoes.tjsp_scraper import diagnostico


class _DriverExplosivo:
    """Qualquer acesso é erro: prova que o caminho desligado não toca no driver."""

    def __getattr__(self, nome):
        raise AssertionError(f"driver acessado ({nome}) com o debug desligado")


class _DriverFalso:
    current_url = "https://exemplo.jus.br/processo"

    def __init__(self, dados=None):
        self.dados = dados if dados is not None else {"inputs": [], "botoes": []}
        self.screenshots: list[str] = []
        self.scripts = 0

    def save_screenshot(self, caminho: str) -> bool:
        from pathlib import Path

        Path(caminho).write_bytes(b"\x89PNG\r\n\x1a\n")  # PNG mínimo
        self.screenshots.append(caminho)
        return True

    def execute_script(self, _js, *_a):
        self.scripts += 1
        return self.dados


@pytest.fixture
def debug_ligado(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRAPER_DEBUG", "1")
    monkeypatch.setattr(diagnostico, "DEBUG_DIR", tmp_path / "_debug")
    return tmp_path / "_debug"


# ---------------------------------------------------------------------
# Desligado (o padrão)
# ---------------------------------------------------------------------
def test_desligado_por_padrao(monkeypatch):
    monkeypatch.delenv("SCRAPER_DEBUG", raising=False)
    assert diagnostico.debug_ativo() is False


@pytest.mark.parametrize("valor", ["", "0", "false", "False"])
def test_valores_que_mantem_desligado(monkeypatch, valor):
    monkeypatch.setenv("SCRAPER_DEBUG", valor)
    assert diagnostico.debug_ativo() is False


@pytest.mark.parametrize("valor", ["1", "true", "sim", "on"])
def test_valores_que_ligam(monkeypatch, valor):
    monkeypatch.setenv("SCRAPER_DEBUG", valor)
    assert diagnostico.debug_ativo() is True


def test_desligado_nao_toca_no_driver_nem_grava(monkeypatch, tmp_path):
    monkeypatch.delenv("SCRAPER_DEBUG", raising=False)
    monkeypatch.setattr(diagnostico, "DEBUG_DIR", tmp_path / "_debug")

    assert diagnostico.salvar_screenshot(_DriverExplosivo(), "pje", "x") is None
    diagnostico.dump_dom(_DriverExplosivo(), "contexto")

    assert not (tmp_path / "_debug").exists(), "criou a pasta com o debug desligado"


# ---------------------------------------------------------------------
# Ligado
# ---------------------------------------------------------------------
def test_screenshot_grava_quando_ligado(debug_ligado):
    d = _DriverFalso()
    caminho = diagnostico.salvar_screenshot(d, "pje", "busca_timeout")
    assert caminho is not None and caminho.exists()
    assert caminho.name.startswith("pje_busca_timeout_")
    assert caminho.suffix == ".png"
    assert caminho.parent == debug_ligado


def test_dump_dom_usa_uma_unica_ida_ao_navegador(debug_ligado, capsys):
    """O dump antigo fazia `get_attribute` por elemento (~1.750 idas numa página
    JSF). Agora é um `execute_script` só."""
    d = _DriverFalso({
        "inputs": [{"id": "user", "name": "u", "type": "text", "placeholder": ""}],
        "botoes": [{"id": "entrar", "texto": "Entrar"}],
    })
    diagnostico.dump_dom(d, "tela de login")
    assert d.scripts == 1
    saida = capsys.readouterr().out
    assert "tela de login" in saida and "user" in saida and "Entrar" in saida


def test_dump_dom_tolera_falha_do_script(debug_ligado, capsys):
    class _Quebrado(_DriverFalso):
        def execute_script(self, *_a):
            raise RuntimeError("sessão morreu")

    diagnostico.dump_dom(_Quebrado(), "ctx")  # não pode propagar
    assert "falha no dump" in capsys.readouterr().out


def test_screenshot_tolera_driver_quebrado(debug_ligado):
    class _Quebrado(_DriverFalso):
        def save_screenshot(self, _caminho):
            raise RuntimeError("sessão morreu")

    assert diagnostico.salvar_screenshot(_Quebrado(), "pje", "x") is None


# ---------------------------------------------------------------------
# Poda
# ---------------------------------------------------------------------
def test_poda_mantem_os_mais_recentes(debug_ligado):
    import os

    debug_ligado.mkdir(parents=True)
    for i in range(10):
        p = debug_ligado / f"arq_{i}.png"
        p.write_bytes(b"x")
        os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))

    assert diagnostico.podar(max_arquivos=4) == 6
    restantes = sorted(p.name for p in debug_ligado.iterdir())
    assert restantes == ["arq_6.png", "arq_7.png", "arq_8.png", "arq_9.png"]


def test_poda_sem_pasta_nao_levanta(debug_ligado):
    assert diagnostico.podar() == 0


def test_screenshot_poda_sozinho(debug_ligado, monkeypatch):
    monkeypatch.setattr(diagnostico, "MAX_ARQUIVOS", 3)
    d = _DriverFalso()
    for i in range(6):
        assert diagnostico.salvar_screenshot(d, "pje", f"n{i}") is not None
    assert len(list(debug_ligado.iterdir())) <= 3


def test_debug_dir_fica_fora_do_bundle():
    """No .exe, um caminho derivado de __file__ cai no _MEIPASS e é apagado na
    saída — os artefatos sumiam. Tem de ficar em APP_DATA_DIR."""
    from organizador.config import APP_DATA_DIR

    assert diagnostico.DEBUG_DIR == APP_DATA_DIR / "_debug"
