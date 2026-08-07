# -*- coding: utf-8 -*-
"""Escolha do navegador, com fallback (scrapers/base.py).

Bug que originou isto: numa máquina do escritório sem Google Chrome instalado,
nada abria — nem a busca online nem o Autojur. Agora a ordem é
Chrome → Edge → Firefox. O Edge resolve o caso real sem instalar nada: vem no
Windows 10/11 e é Chromium (aceita as mesmas flags).

Nada aqui sobe navegador de verdade; os testes cobrem a DECISÃO (qual tentar, em
que ordem, e o que acontece quando falha).
"""

from __future__ import annotations

import pytest

from organizador.integracoes.tjsp_scraper.scrapers import base


@pytest.fixture(autouse=True)
def _sem_memo_de_navegador(monkeypatch):
    monkeypatch.setattr(base, "_navegador_ok", None)
    monkeypatch.delenv(base._ENV_NAVEGADOR, raising=False)
    yield


def _instalados(monkeypatch, *nomes: str) -> None:
    monkeypatch.setattr(
        base, "caminho_navegador",
        lambda n: rf"C:\fake\{n}.exe" if n in nomes else None,
    )


class _DriverFalso:
    def __init__(self, nome):
        self.nome = nome


def _iniciadores(monkeypatch, funcionam: set[str]) -> list[str]:
    """Substitui os iniciadores; devolve a lista de tentativas registradas."""
    tentativas: list[str] = []

    def fabricar(nome):
        def iniciar(_options):
            tentativas.append(nome)
            if nome not in funcionam:
                raise RuntimeError(f"{nome} não pôde abrir")
            return _DriverFalso(nome)
        return iniciar

    monkeypatch.setattr(base, "_INICIADOR_POR_NAVEGADOR", {
        n: fabricar(n) for n in base.NAVEGADORES
    })
    return tentativas


# ---------------------------------------------------------------------
# Detecção
# ---------------------------------------------------------------------
def test_lista_na_ordem_de_preferencia(monkeypatch):
    _instalados(monkeypatch, "firefox", "edge", "chrome")
    assert base.navegadores_disponiveis() == ["chrome", "edge", "firefox"]


def test_so_lista_o_que_esta_instalado(monkeypatch):
    _instalados(monkeypatch, "edge")
    assert base.navegadores_disponiveis() == ["edge"]


def test_sem_nada_detectado_ainda_tenta_todos(monkeypatch):
    """A detecção olha caminhos conhecidos + PATH; uma instalação fora do padrão
    pode escapar dela. Falhar tentando é melhor que desistir sem tentar."""
    _instalados(monkeypatch)
    assert base._ordem_de_tentativa() == list(base.NAVEGADORES)


def test_variavel_de_ambiente_forca_um_navegador(monkeypatch):
    _instalados(monkeypatch, "chrome", "edge")
    monkeypatch.setenv(base._ENV_NAVEGADOR, "edge")
    assert base._ordem_de_tentativa() == ["edge"]


def test_variavel_invalida_e_ignorada(monkeypatch):
    _instalados(monkeypatch, "chrome")
    monkeypatch.setenv(base._ENV_NAVEGADOR, "netscape")
    assert base._ordem_de_tentativa() == ["chrome"]


# ---------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------
def test_usa_chrome_quando_disponivel(monkeypatch):
    _instalados(monkeypatch, "chrome", "edge")
    tentativas = _iniciadores(monkeypatch, {"chrome", "edge"})
    d = base.criar_chrome_driver(headless=True)
    assert d.nome == "chrome"
    assert tentativas == ["chrome"], "não devia tentar outro navegador"


def test_cai_para_edge_sem_chrome_instalado(monkeypatch):
    """O caso relatado: máquina sem Chrome."""
    _instalados(monkeypatch, "edge", "firefox")
    tentativas = _iniciadores(monkeypatch, {"edge", "firefox"})
    d = base.criar_chrome_driver(headless=True)
    assert d.nome == "edge"
    assert tentativas == ["edge"]


def test_cai_para_edge_quando_chrome_esta_instalado_mas_nao_abre(monkeypatch):
    """Chrome presente no disco mas quebrado/incompatível com o driver."""
    _instalados(monkeypatch, "chrome", "edge")
    tentativas = _iniciadores(monkeypatch, {"edge"})
    d = base.criar_chrome_driver(headless=True)
    assert d.nome == "edge"
    assert tentativas == ["chrome", "edge"]


def test_cai_para_firefox_em_ultimo_caso(monkeypatch):
    _instalados(monkeypatch, "chrome", "edge", "firefox")
    tentativas = _iniciadores(monkeypatch, {"firefox"})
    assert base.criar_chrome_driver(headless=True).nome == "firefox"
    assert tentativas == ["chrome", "edge", "firefox"]


def test_erro_final_diz_o_que_instalar(monkeypatch):
    _instalados(monkeypatch)
    _iniciadores(monkeypatch, set())
    with pytest.raises(RuntimeError) as exc:
        base.criar_chrome_driver(headless=True)
    msg = str(exc.value)
    assert "Chrome" in msg and "Edge" in msg, "a mensagem tem de dizer o que instalar"


def test_erro_final_lista_os_detectados_quando_havia_algum(monkeypatch):
    _instalados(monkeypatch, "edge")
    _iniciadores(monkeypatch, set())
    with pytest.raises(RuntimeError, match="Detectados nesta máquina: edge"):
        base.criar_chrome_driver(headless=True)


def test_navegador_que_funcionou_e_tentado_primeiro(monkeypatch):
    """O monitoramento cria vários scrapers; não deve repetir a tentativa-e-erro
    do Chrome a cada um."""
    _instalados(monkeypatch, "chrome", "edge")
    tentativas = _iniciadores(monkeypatch, {"edge"})
    base.criar_chrome_driver(headless=True)
    assert tentativas == ["chrome", "edge"]
    tentativas.clear()
    base.criar_chrome_driver(headless=True)
    assert tentativas == ["edge"], "não memorizou o navegador que funcionou"


# ---------------------------------------------------------------------
# Opções por navegador
# ---------------------------------------------------------------------
def test_edge_recebe_as_mesmas_flags_do_chrome():
    args_chrome = set(base._opcoes_chrome(True, None).arguments)
    args_edge = set(base._opcoes_edge(True, None).arguments)
    assert args_chrome == args_edge
    assert "--headless=new" in args_edge
    assert "--blink-settings=imagesEnabled=false" in args_edge


def test_firefox_nao_recebe_flags_de_chromium():
    """Passar flags do Chromium ao Firefox faria o launch falhar."""
    args = base._opcoes_firefox(True, None).arguments
    assert "--headless" in args
    assert not any(a.startswith("--blink-settings") for a in args)
    assert not any("--disable-dev-shm-usage" == a for a in args)


def test_perfil_persistente_so_no_chromium(monkeypatch):
    perfil = r"C:\perfil\autojur"
    assert any(perfil in a for a in base._opcoes_chrome(True, perfil).arguments)
    assert any(perfil in a for a in base._opcoes_edge(True, perfil).arguments)


def test_firefox_avisa_que_ignora_o_perfil(monkeypatch, capsys):
    """A sessão do Autojur não é reaproveitada no Firefox — o 2FA pode voltar a
    ser pedido, e isso tem de aparecer no log."""
    _iniciadores(monkeypatch, {"firefox"})
    base._iniciar_navegador("firefox", True, r"C:\perfil\autojur")
    assert "perfil persistente não suportado" in capsys.readouterr().out


def test_log_seguro_nao_propaga_erro_de_encoding(monkeypatch):
    """Um print não pode impedir o navegador de abrir: em console cp1252,
    imprimir acento/emoji levanta UnicodeEncodeError (aconteceu ao testar o Edge)."""
    def print_que_falha(*_a, **_k):
        raise UnicodeEncodeError("charmap", "x", 0, 1, "boom")

    monkeypatch.setattr("builtins.print", print_que_falha)
    base._log_seguro("Navegador em uso: edge")  # não pode levantar
