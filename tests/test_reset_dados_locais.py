# -*- coding: utf-8 -*-
"""Botão "Restaurar dados do programa" (aba Configurações).

Ele existe porque atualizar o .exe NÃO conserta uma máquina cujo cache local
está com dados errados: a lista da Pesquisa dá prioridade ao que está em
APP_DATA_DIR sobre a base embutida na release, e a atualização não toca em
APP_DATA_DIR de propósito (as senhas do usuário moram lá).

O risco do botão é o oposto do bug: apagar de mais. Estes testes fixam a
fronteira — o que é derivado vai, o que o usuário digitou fica.
"""

from __future__ import annotations

import shutil

from organizador import gui
from organizador.config import (
    AUTOJUR_PROFILE_DIR, COMARCAS_CACHE_PATH, CONFIG_DIR, CONFIG_ENV_PATH,
    CREDENCIAIS_PATH, UI_STATE_PATH,
)


def _app_data_falso(tmp_path, n_processos: int = 3):
    """APP_DATA_DIR de mentira, com derivados E dados do usuário."""
    online = tmp_path / "processos_online"
    for i in range(n_processos):
        pasta = online / f"000{i}-11.2020.8.26.0100"
        pasta.mkdir(parents=True)
        (pasta / "metadados.json").write_text("{}", encoding="utf-8")
    indice = tmp_path / "processos_indice.json"
    indice.write_text("{}", encoding="utf-8")
    templates = tmp_path / "templates_placeholders.json"
    templates.write_text("{}", encoding="utf-8")
    return online, indice, templates


def _apontar(monkeypatch, online, indice, templates):
    monkeypatch.setattr(gui, "PROCESSOS_ONLINE_DIR", online)
    monkeypatch.setattr(gui, "PROCESSOS_INDICE_JSON", indice)
    monkeypatch.setattr(gui, "TEMPLATES_CACHE_PATH", templates)


def test_apaga_os_tres_derivados(monkeypatch, tmp_path):
    online, indice, templates = _app_data_falso(tmp_path)
    _apontar(monkeypatch, online, indice, templates)

    apagados, falhas = gui._resetar_dados_locais()

    assert falhas == []
    assert len(apagados) == 3
    assert not online.exists()
    assert not indice.exists()
    assert not templates.exists()


def test_nao_apaga_nada_que_o_usuario_digitou():
    """A fronteira do botão, em forma de teste.

    `config/` guarda as senhas dos tribunais e os segredos de 2FA; o perfil do
    Autojur guarda o dispositivo confiável (apagar = 2FA de novo); o ui_state
    guarda as pastas escolhidas; e comarcas_por_origem.json tem correções
    MANUAIS de comarca. Nada disso se reconstrói sozinho — se um dia alguém
    incluir um deles na limpeza, este teste quebra antes de virar release."""
    alvos = {c.resolve() for c, _rotulo in gui._itens_reset()}
    intocaveis = (
        CONFIG_DIR, CONFIG_ENV_PATH, CREDENCIAIS_PATH,
        AUTOJUR_PROFILE_DIR, UI_STATE_PATH, COMARCAS_CACHE_PATH,
    )
    for caminho in intocaveis:
        assert caminho.resolve() not in alvos, f"{caminho.name} não pode ser apagado"


def test_nao_toca_no_resto_de_app_data(monkeypatch, tmp_path):
    online, indice, templates = _app_data_falso(tmp_path)
    _apontar(monkeypatch, online, indice, templates)
    vizinhos = []
    for nome in ("ui_state.json", "comarcas_por_origem.json", "updater.log"):
        p = tmp_path / nome
        p.write_text("x", encoding="utf-8")
        vizinhos.append(p)
    config = tmp_path / "config"
    config.mkdir()
    (config / ".env").write_text("SENHA=123", encoding="utf-8")

    gui._resetar_dados_locais()

    assert all(p.exists() for p in vizinhos)
    assert (config / ".env").read_text(encoding="utf-8") == "SENHA=123"


def test_sem_nada_para_apagar_nao_e_falha(monkeypatch, tmp_path):
    """Segunda vez seguida no botão: "nada a restaurar", não "deu erro"."""
    _apontar(
        monkeypatch,
        tmp_path / "nao_existe", tmp_path / "sumiu.json", tmp_path / "sumiu2.json",
    )
    assert gui._resetar_dados_locais() == ([], [])


def test_pasta_que_sobrevive_vira_falha(monkeypatch, tmp_path):
    """`rmtree(ignore_errors=True)` não levanta quando um arquivo está travado —
    ele apaga o resto e volta calado. Sem a conferência depois, o app diria
    "pronto, limpei" para uma pasta que continua lá."""
    online, indice, templates = _app_data_falso(tmp_path)
    _apontar(monkeypatch, online, indice, templates)
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)

    apagados, falhas = gui._resetar_dados_locais()

    assert falhas == ["processos guardados de buscas online"]
    assert "processos guardados de buscas online" not in apagados
    assert not indice.exists()  # os outros dois seguem sendo limpos


def test_contagem_de_processos_locais(monkeypatch, tmp_path):
    online, indice, templates = _app_data_falso(tmp_path, n_processos=7)
    _apontar(monkeypatch, online, indice, templates)
    assert gui._contar_processos_locais() == 7


def test_contagem_sem_cache_e_zero(monkeypatch, tmp_path):
    _apontar(monkeypatch, tmp_path / "nao_existe", tmp_path / "a", tmp_path / "b")
    assert gui._contar_processos_locais() == 0
