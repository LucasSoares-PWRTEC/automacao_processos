# -*- coding: utf-8 -*-
"""Cobertura da configuração local e da integração internalizada do scraper."""

from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path

import pytest

from organizador.models import Processo


def test_arquivos_de_exemplo_de_configuracao_existem():
    from organizador import config

    assert config.CONFIG_ENV_EXAMPLE_PATH.exists()
    assert config.CREDENCIAIS_EXAMPLE_PATH.exists()


def test_loader_de_credenciais_json(monkeypatch, tmp_path):
    mod = importlib.import_module("organizador.integracoes.tjsp_scraper.credenciais")
    cred_path = tmp_path / "credenciais.json"
    cred_path.write_text(
        json.dumps(
            {
                "credenciais": [
                    {"tj": "TJSP - Esaj", "login": "123", "senha": "abc"},
                    {"tj": "TJMG - PJE", "login": "456", "senha": "def"},
                ]
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(mod, "CREDENCIAIS_PATH", cred_path)
    monkeypatch.setattr(mod, "SENHAS_XLSX_PATH", tmp_path / "ausente.xlsx")
    mod._carregar_credenciais.cache_clear()

    try:
        assert mod.obter_credenciais("TJSP - Esaj") == ("123", "abc")
        assert mod.obter_credenciais("tjmg pje") == ("456", "def")
    finally:
        mod._carregar_credenciais.cache_clear()


def test_loader_de_credenciais_erro_quando_ausente(monkeypatch, tmp_path):
    mod = importlib.import_module("organizador.integracoes.tjsp_scraper.credenciais")
    monkeypatch.setattr(mod, "CREDENCIAIS_PATH", tmp_path / "credenciais.json")
    monkeypatch.setattr(mod, "SENHAS_XLSX_PATH", tmp_path / "senhas.xlsx")
    monkeypatch.setattr(mod, "LEGACY_SENHAS_XLSX_PATH", tmp_path / "senhas_legado.xlsx")
    mod._carregar_credenciais.cache_clear()

    try:
        with pytest.raises(FileNotFoundError, match="Credenciais não encontradas"):
            mod.obter_credenciais("TJSP - Esaj")
    finally:
        mod._carregar_credenciais.cache_clear()


def test_loader_bootstrapa_xlsx_legado(monkeypatch, tmp_path):
    mod = importlib.import_module("organizador.integracoes.tjsp_scraper.credenciais")
    legacy_xlsx = tmp_path / "senhas_legado.xlsx"
    legacy_xlsx.write_bytes(b"fake-xlsx")
    destino_xlsx = tmp_path / "config" / "senhas.xlsx"

    monkeypatch.setattr(mod, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(mod, "CREDENCIAIS_PATH", tmp_path / "config" / "credenciais.json")
    monkeypatch.setattr(mod, "SENHAS_XLSX_PATH", destino_xlsx)
    monkeypatch.setattr(mod, "LEGACY_SENHAS_XLSX_PATH", legacy_xlsx)
    mod._carregar_credenciais.cache_clear()

    try:
        with pytest.raises(Exception):
            mod._carregar_credenciais()
    finally:
        mod._carregar_credenciais.cache_clear()

    assert destino_xlsx.exists()
    assert destino_xlsx.read_bytes() == b"fake-xlsx"


def test_busca_online_usa_scraper_interno_sem_manipular_sys_path(monkeypatch):
    mod = importlib.import_module("organizador.scraper")
    parser_mod = importlib.import_module("organizador.integracoes.tjsp_scraper.parser_processo")

    class FakeScraper:
        def login(self) -> None:
            return None

        def consultar_processo(self, numero: str, modo_docs: str = "nenhum") -> str:
            assert numero == "4004459-04.2026.8.26.0248"
            assert modo_docs == "nenhum"
            return "<html>ok</html>"

        def fechar(self) -> None:
            return None

    env_calls: list[Path] = []
    monkeypatch.setitem(
        sys.modules,
        "dotenv",
        types.SimpleNamespace(load_dotenv=lambda path: env_calls.append(Path(path))),
    )
    monkeypatch.setattr(mod, "criar_scraper", lambda **kwargs: FakeScraper())
    monkeypatch.setattr(mod, "_extrair_blocos_com_origem", lambda html: [("", "", "", "", html)])
    monkeypatch.setattr(mod, "extrair_penhora_bloqueio_tribunais", lambda html: [{"descricao": "Penhora realizada"}])
    monkeypatch.setattr(mod, "_dict_para_processo", lambda payload: Processo(numero=payload["numero"], tribunal="TJSP", uf="SP"))
    monkeypatch.setattr(parser_mod, "extrair_processo", lambda html, blocos_documentos, numero_cnj: {"numero": numero_cnj})

    sys_path_antes = list(sys.path)
    logs: list[str] = []

    proc = mod.buscar_processo_online(
        "4004459-04.2026.8.26.0248",
        "TJSP — eproc",
        True,
        logs.append,
    )

    assert proc.numero == "4004459-04.2026.8.26.0248"
    assert sys.path == sys_path_antes
    assert env_calls == [mod.CONFIG_ENV_PATH]
    assert any("Dados extraídos com sucesso." in linha for linha in logs)


def test_preparar_env_local_copia_legado(monkeypatch, tmp_path):
    mod = importlib.import_module("organizador.scraper")
    legacy_env = tmp_path / "legacy.env"
    legacy_env.write_text("IMAP_USER=abc\n", encoding="utf-8")
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    env_destino = config_dir / ".env"
    exemplo = config_dir / ".env.example"
    exemplo.write_text("IMAP_USER=\n", encoding="utf-8")

    monkeypatch.setattr(mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(mod, "CONFIG_ENV_PATH", env_destino)
    monkeypatch.setattr(mod, "CONFIG_ENV_EXAMPLE_PATH", exemplo)
    monkeypatch.setattr(mod, "LEGACY_ENV_PATH", legacy_env)

    mod._preparar_env_local()

    assert env_destino.exists()
    assert env_destino.read_text(encoding="utf-8") == "IMAP_USER=abc\n"


def test_gui_inicializa_com_caminhos_padrao_validos():
    from organizador.gui import App

    try:
        app = App()
    except Exception as exc:
        pytest.skip(f"GUI indisponível neste ambiente: {exc}")

    try:
        assert app.var_origem.get()
        assert app.var_destino.get()
        assert app.var_pesquisa_base.get()
    finally:
        app.destroy()
