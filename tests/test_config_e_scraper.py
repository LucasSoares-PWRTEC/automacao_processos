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


def test_candidatos_tj_para_uf_ordena_e_cobre_fallback():
    from organizador.config import TJ_SISTEMAS, candidatos_tj_para_uf

    mg = candidatos_tj_para_uf("MG")
    assert len(mg) == 2
    assert TJ_SISTEMAS[mg[0]][0] == "pje"  # MG: os alvos estão no PJe → 1º
    assert {TJ_SISTEMAS[lbl][0] for lbl in mg} == {"pje", "eproc"}

    sp = candidatos_tj_para_uf("SP")
    assert TJ_SISTEMAS[sp[0]][0] == "eproc"  # SP: eproc 1º (traz documento/réu)

    assert candidatos_tj_para_uf("BA") == []  # UF sem rota
    assert candidatos_tj_para_uf("") == []


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


def test_busca_online_esaj_usa_parser_estruturado(monkeypatch):
    """No e-SAJ a busca deve devolver um Processo estruturado (classe, partes,
    movimentações), não mais um Processo mínimo."""
    mod = importlib.import_module("organizador.scraper")
    esaj_parser = importlib.import_module(
        "organizador.integracoes.tjsp_scraper.parser_esaj"
    )

    class FakeScraper:
        def login(self) -> None:
            return None

        def consultar_processo(self, numero: str) -> str:  # e-SAJ não recebe modo_docs
            return "<html>esaj</html>"

        def fechar(self) -> None:
            return None

    _fake_dotenv(monkeypatch)
    monkeypatch.setattr(mod, "criar_scraper", lambda **kwargs: FakeScraper())
    monkeypatch.setattr(mod, "extrair_penhora_bloqueio_tribunais", lambda html: [])
    monkeypatch.setattr(
        esaj_parser, "extrair_processo_esaj",
        lambda html, numero_cnj: {
            "numero": numero_cnj,
            "classe": "Execução de Título Extrajudicial",
            "partes": [{"papel": "EXEQUENTE", "nome": "Fulano", "advogados": []}],
            "eventos": [{"data": "08/05/2026", "descricao": "Conclusos"}],
            "total_eventos": 1,
        },
    )

    logs: list[str] = []
    proc = mod.buscar_processo_online(
        "1102493-63.2022.8.26.0100", "TJSP — e-SAJ", True, logs.append,
    )

    assert proc.numero == "1102493-63.2022.8.26.0100"
    assert proc.classe == "Execução de Título Extrajudicial"
    assert [p.nome for p in proc.partes] == ["Fulano"]
    assert proc.total_eventos == 1
    assert proc.tribunal == "TJSP" and proc.uf == "SP"  # fallback do config
    assert any("Dados extraídos do e-SAJ" in linha for linha in logs)


def test_busca_online_sinaliza_processo_nao_encontrado(monkeypatch):
    """Página de 'Processo não encontrado' deve virar erro claro, não um
    registro vazio (eproc devolve a página, não None).

    `fallback=False` porque o alvo aqui é a DETECÇÃO do "não encontrado" numa
    rota. Com o fallback ligado a busca seguiria para o e-SAJ de SP, e o dublê —
    que devolve a página do eproc em qualquer rota — passaria pelo detector do
    e-SAJ e "acharia" o processo. O fallback tem cobertura própria em
    tests/test_busca_fallback.py."""
    mod = importlib.import_module("organizador.scraper")

    class FakeScraper:
        def login(self) -> None:
            return None

        def consultar_processo(self, numero: str, modo_docs: str = "nenhum") -> str:
            # É o que o eproc devolve quando o número não está na carteira.
            return '<span class="infraExcecao">Processo não encontrado. [00002235220258260127]</span>'

        def fechar(self) -> None:
            return None

    _fake_dotenv(monkeypatch)
    monkeypatch.setattr(mod, "criar_scraper", lambda **kwargs: FakeScraper())

    logs: list[str] = []
    with pytest.raises(RuntimeError, match="não encontrado"):
        mod.buscar_processo_online(
            "0000223-52.2025.8.26.0127", "TJSP — eproc", True, logs.append,
            fallback=False,
        )


def test_html_indica_nao_encontrado_por_site():
    mod = importlib.import_module("organizador.scraper")

    assert mod._html_indica_nao_encontrado("Processo não encontrado.", "eproc")
    assert mod._html_indica_nao_encontrado(
        "Não existem informações disponíveis para os parâmetros informados", "esaj"
    )
    # Marcador de um site não dispara em outro nem em página normal.
    assert not mod._html_indica_nao_encontrado("Processo não encontrado.", "esaj")
    assert not mod._html_indica_nao_encontrado("<html>Detalhes do Processo</html>", "eproc")


def _fake_dotenv(monkeypatch):
    monkeypatch.setitem(
        sys.modules, "dotenv",
        # `path=None`: o organizador chama load_dotenv(CONFIG_ENV_PATH), mas o
        # webdriver_manager chama load_dotenv() sem argumento ao ser importado —
        # com a lambda de 1 parâmetro obrigatório o import estourava TypeError.
        types.SimpleNamespace(load_dotenv=lambda path=None: None),
    )


def test_modo_debug_pos_login_para_antes_da_consulta(monkeypatch):
    mod = importlib.import_module("organizador.scraper")

    eventos: list[str] = []

    class FakeScraper:
        def login(self) -> None:
            eventos.append("login")

        def consultar_processo(self, numero: str, modo_docs: str = "nenhum") -> str:
            eventos.append("consulta")  # NÃO deve ser chamado em pos_login
            return "<html>ok</html>"

        def fechar(self) -> None:
            eventos.append("fechar")  # NÃO deve fechar em debug

    _fake_dotenv(monkeypatch)
    monkeypatch.setattr(mod, "criar_scraper", lambda **kwargs: FakeScraper())

    retidos: list[object] = []
    logs: list[str] = []
    proc = mod.buscar_processo_online(
        "4004459-04.2026.8.26.0248", "TJSP — eproc", False,
        logs.append, modo_debug="pos_login", reter_scraper=retidos.append,
    )

    assert eventos == ["login"]  # logou, não consultou, não fechou
    assert len(retidos) == 1  # scraper retido p/ o navegador não fechar
    assert proc.numero == "4004459-04.2026.8.26.0248"
    assert any("após o login" in linha for linha in logs)


def test_modo_debug_pos_consulta_extrai_e_nao_fecha(monkeypatch):
    mod = importlib.import_module("organizador.scraper")
    parser_mod = importlib.import_module("organizador.integracoes.tjsp_scraper.parser_processo")

    eventos: list[str] = []

    class FakeScraper:
        def login(self) -> None:
            eventos.append("login")

        def consultar_processo(self, numero: str, modo_docs: str = "nenhum") -> str:
            eventos.append("consulta")
            return "<html>ok</html>"

        def fechar(self) -> None:
            eventos.append("fechar")  # NÃO deve fechar em debug

    _fake_dotenv(monkeypatch)
    monkeypatch.setattr(mod, "criar_scraper", lambda **kwargs: FakeScraper())
    monkeypatch.setattr(mod, "_extrair_blocos_com_origem", lambda html: [("", "", "", "", html)])
    monkeypatch.setattr(mod, "extrair_penhora_bloqueio_tribunais", lambda html: [])
    monkeypatch.setattr(mod, "_dict_para_processo", lambda payload: Processo(numero=payload["numero"], tribunal="TJSP", uf="SP"))
    monkeypatch.setattr(parser_mod, "extrair_processo", lambda html, blocos_documentos, numero_cnj: {"numero": numero_cnj})

    retidos: list[object] = []
    logs: list[str] = []
    proc = mod.buscar_processo_online(
        "4004459-04.2026.8.26.0248", "TJSP — eproc", False,
        logs.append, modo_debug="pos_consulta", reter_scraper=retidos.append,
    )

    assert eventos == ["login", "consulta"]  # consultou, mas não fechou
    assert len(retidos) == 1
    assert proc.numero == "4004459-04.2026.8.26.0248"
    assert any("valide os dados extraídos" in linha for linha in logs)


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


def test_opcoes_esaj_multitribunal():
    """e-SAJ de AM/MS entram nas opções, resolvem UF e derivam a chave de
    credencial correta a partir do host."""
    from organizador.config import TJ_SISTEMAS, _ESTADO_TJ_UF, uf_efetiva_tj
    from organizador.integracoes.tjsp_scraper.scrapers.esaj import _derivar_login_e_chave

    for label, uf, chave in [
        ("TJSP — e-SAJ", "SP", "TJSP - Esaj"),
        ("TJAM — e-SAJ", "AM", "TJAM - Esaj"),
        ("TJMS — e-SAJ", "MS", "TJMS - Esaj"),
    ]:
        site, estado, url, _perfil = TJ_SISTEMAS[label]
        assert site == "esaj"
        assert estado in _ESTADO_TJ_UF
        assert uf_efetiva_tj(label) == uf
        assert _derivar_login_e_chave(url)[1] == chave


def test_uf_efetiva_tj_usa_perfil_ou_estado():
    from organizador.config import uf_efetiva_tj

    assert uf_efetiva_tj("TJMG — eproc") == "MG"
    assert uf_efetiva_tj("TJSP — eproc") == "SP"
    assert uf_efetiva_tj("TJSP — e-SAJ") == "SP"  # sem perfil → UF do estado
    assert uf_efetiva_tj("inexistente") == ""


def test_sugerir_tj_para_uf_prefere_eproc():
    from organizador.config import TJ_SISTEMAS, sugerir_tj_para_uf

    sug_mg = sugerir_tj_para_uf("MG")
    assert sug_mg is not None and TJ_SISTEMAS[sug_mg][0] == "eproc"

    assert sugerir_tj_para_uf("SP") == "TJSP — eproc"
    assert sugerir_tj_para_uf("sp") == "TJSP — eproc"  # case-insensitive
    assert sugerir_tj_para_uf("BA") is None  # sem sistema configurado
    assert sugerir_tj_para_uf("") is None


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


def test_toda_rota_pje_tem_rotulo_de_credencial_proprio():
    """Sem entrada em _CRED_POR_ESTADO a rota cai no rótulo do TJMG em SILÊNCIO.

    Foi o que escondeu a senha vencida do TJDFT: o app mandava a credencial do
    TJMG para o pje.tjdft.jus.br e, quando a senha do DF foi trocada sozinha, o
    sintoma virou "a senha nova não funciona". Fallback silencioso não avisa —
    este teste avisa.
    """
    from organizador.config import TJ_SISTEMAS
    from organizador.integracoes.tjsp_scraper.scrapers.pje import _CRED_POR_ESTADO

    estados_pje = {
        estado for (site, estado, _url, _perfil) in TJ_SISTEMAS.values() if site == "pje"
    }
    sem_rotulo = sorted(estados_pje - set(_CRED_POR_ESTADO))
    assert not sem_rotulo, (
        f"rotas PJe sem rótulo próprio em _CRED_POR_ESTADO: {sem_rotulo} "
        "— cairiam no fallback do TJMG sem avisar"
    )
