# -*- coding: utf-8 -*-
"""Config editável em local persistente (organizador.config.semear_config).

Bug corrigido: no .exe (onefile) a config do escritório fica em `sys._MEIPASS`,
uma pasta TEMPORÁRIA apagada ao fechar o app. `CONFIG_DIR` apontava para lá, e
portanto `salvar_config_2fa()` e o "Salvar configurações" da aba Configurações
gravavam no temporário: a edição parecia dar certo e sumia. Trocar uma senha
vencida não surtia efeito em NENHUMA máquina que rodasse o executável.

Agora o bundle é a SEMENTE e a config viva mora em APP_DATA_DIR.

POR QUE SUBPROCESSO: `CONFIG_DIR` é resolvido no import de `organizador.config`,
e vários módulos fazem `from .config import CONFIG_ENV_PATH`. Simular o app
empacotado com `importlib.reload` deixa esses módulos com referências antigas e
vaza estado para os OUTROS testes (aconteceu: `test_config_e_scraper` quebrou
quando rodou depois destes). Um interpretador novo por caso resolve de vez.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent

# Prepara o ambiente "app empacotado" e devolve o resultado como JSON no stdout.
_MOLDE = """
import json, os, shutil, sys
sys.frozen = {frozen}
{def_meipass}
os.environ["ORGANIZADOR_APP_DATA"] = r"{appdata}"
sys.path.insert(0, r"{raiz}")

import organizador.config as C
{corpo}
"""


def _rodar(corpo: str, appdata: Path, meipass: Path | None) -> dict:
    codigo = _MOLDE.format(
        frozen="True" if meipass else "False",
        def_meipass=f'sys._MEIPASS = r"{meipass}"' if meipass else "",
        appdata=appdata,
        raiz=RAIZ,
        corpo=textwrap.dedent(corpo),
    )
    proc = subprocess.run(
        [sys.executable, "-c", codigo],
        capture_output=True, text=True, timeout=180, cwd=str(RAIZ),
    )
    assert proc.returncode == 0, f"subprocesso falhou:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture
def semente(tmp_path) -> Path:
    """Uma pasta config/ como a que viaja no bundle."""
    d = tmp_path / "meipass" / "config"
    d.mkdir(parents=True)
    (d / ".env").write_text("TOTP_PDPJ=DOBUNDLE\nIMAP_USER=a@b.c\n", encoding="utf-8")
    (d / ".env.example").write_text("TOTP_PDPJ=\n", encoding="utf-8")
    (d / "credenciais.json").write_text('{"credenciais": []}', encoding="utf-8")
    (d / "credenciais.example.json").write_text('{"credenciais": []}', encoding="utf-8")
    (d / "senhas.xlsx").write_bytes(b"PK\x03\x04")
    return d.parent


# ---------------------------------------------------------------------
# Onde a config viva mora
# ---------------------------------------------------------------------
def test_empacotado_usa_app_data_e_nao_o_meipass(tmp_path, semente):
    appdata = tmp_path / "appdata"
    r = _rodar(
        """
        print(json.dumps({
            "config_dir": str(C.CONFIG_DIR),
            "semente": str(C.CONFIG_SEMENTE_DIR),
            "env": str(C.CONFIG_ENV_PATH),
        }))
        """,
        appdata, semente,
    )
    assert r["config_dir"] == str(appdata / "config")
    assert "_MEIPASS" not in r["config_dir"]
    assert r["semente"] == str(semente / "config")
    assert r["config_dir"] != r["semente"]
    assert r["env"].endswith(".env")


def test_do_fonte_nada_muda_de_lugar(tmp_path):
    """Quem roda do código-fonte continua editando o config/ do repositório."""
    r = _rodar(
        """
        print(json.dumps({
            "iguais": C.CONFIG_DIR == C.CONFIG_SEMENTE_DIR,
            "copiados": C.semear_config(),
        }))
        """,
        tmp_path / "appdata", meipass=None,
    )
    assert r["iguais"] is True
    assert r["copiados"] == []  # no-op


# ---------------------------------------------------------------------
# Semeadura
# ---------------------------------------------------------------------
def test_primeira_execucao_copia_a_config_do_bundle(tmp_path, semente):
    appdata = tmp_path / "appdata"
    r = _rodar(
        """
        copiados = C.semear_config()
        print(json.dumps({
            "copiados": sorted(copiados),
            "env_conteudo": C.CONFIG_ENV_PATH.read_text(encoding="utf-8"),
            "cred_existe": C.CREDENCIAIS_PATH.exists(),
        }))
        """,
        appdata, semente,
    )
    assert r["copiados"] == [
        ".env", ".env.example", "credenciais.example.json",
        "credenciais.json", "senhas.xlsx",
    ]
    assert "DOBUNDLE" in r["env_conteudo"]
    assert r["cred_existe"] is True


def test_nao_sobrescreve_o_que_o_usuario_editou(tmp_path, semente):
    """O ponto mais importante: uma ATUALIZAÇÃO não pode apagar a senha que a
    pessoa acabou de corrigir na máquina dela."""
    r = _rodar(
        """
        C.semear_config()
        C.CONFIG_ENV_PATH.write_text("TOTP_PDPJ=EDITADO\\n", encoding="utf-8")
        segunda = C.semear_config()          # 2ª abertura / depois de um update
        print(json.dumps({
            "copiados_na_2a": segunda,
            "conteudo": C.CONFIG_ENV_PATH.read_text(encoding="utf-8"),
        }))
        """,
        tmp_path / "appdata", semente,
    )
    assert ".env" not in r["copiados_na_2a"]
    assert "EDITADO" in r["conteudo"]


def test_semeadura_e_idempotente(tmp_path, semente):
    r = _rodar(
        """
        print(json.dumps({"1a": bool(C.semear_config()), "2a": C.semear_config()}))
        """,
        tmp_path / "appdata", semente,
    )
    assert r["1a"] is True and r["2a"] == []


def test_arquivo_ausente_na_semente_e_ignorado(tmp_path, semente):
    (semente / "config" / "senhas.xlsx").unlink()
    r = _rodar(
        "print(json.dumps({'copiados': sorted(C.semear_config())}))",
        tmp_path / "appdata", semente,
    )
    assert "senhas.xlsx" not in r["copiados"]
    assert ".env" in r["copiados"]  # os outros seguem normalmente


def test_falha_de_disco_nao_derruba_o_app(tmp_path, semente):
    """Semeadura é best-effort: sem permissão de escrita o app ainda abre."""
    r = _rodar(
        """
        def quebrado(*a, **k):
            raise OSError("disco cheio")
        shutil.copy2 = quebrado
        C.shutil.copy2 = quebrado
        print(json.dumps({"copiados": C.semear_config()}))
        """,
        tmp_path / "appdata", semente,
    )
    assert r["copiados"] == []  # não levantou


# ---------------------------------------------------------------------
# O comportamento que o usuário percebe
# ---------------------------------------------------------------------
def test_edicao_de_2fa_sobrevive_ao_fechar_o_app(tmp_path, semente):
    """Reproduz o ciclo real: edita, fecha (o _MEIPASS é apagado), reabre com um
    _MEIPASS NOVO, e o valor editado tem de continuar lá."""
    appdata = tmp_path / "appdata"
    r1 = _rodar(
        """
        C.semear_config()
        from organizador.scraper import ler_config_2fa, salvar_config_2fa
        salvar_config_2fa({"TOTP_PDPJ": "SEGREDONOVO"})
        print(json.dumps({"logo_apos_editar": ler_config_2fa()["TOTP_PDPJ"]}))
        """,
        appdata, semente,
    )
    assert r1["logo_apos_editar"] == "SEGREDONOVO"

    # "Fecha o app": o onefile apaga a pasta temporária. Reabre com outra.
    import shutil as _sh
    _sh.rmtree(semente, ignore_errors=True)
    nova = tmp_path / "meipass2"
    (nova / "config").mkdir(parents=True)
    (nova / "config" / ".env").write_text("TOTP_PDPJ=DOBUNDLE\n", encoding="utf-8")

    r2 = _rodar(
        """
        C.semear_config()
        from organizador.scraper import ler_config_2fa
        print(json.dumps({"apos_reabrir": ler_config_2fa()["TOTP_PDPJ"]}))
        """,
        appdata, nova,
    )
    assert r2["apos_reabrir"] == "SEGREDONOVO", (
        "a edição foi perdida — a config voltou a ser gravada no temporário"
    )


def test_credenciais_apontam_para_o_local_persistente(tmp_path, semente):
    """`senhas.xlsx` e `credenciais.json` são derivados de CONFIG_DIR em
    integracoes/.../credenciais.py; têm de acompanhar a mudança."""
    appdata = tmp_path / "appdata"
    r = _rodar(
        """
        from organizador.integracoes.tjsp_scraper import credenciais as cred
        print(json.dumps({
            "senhas": str(cred.SENHAS_XLSX_PATH),
            "cred": str(cred.CREDENCIAIS_PATH),
        }))
        """,
        appdata, semente,
    )
    assert str(appdata) in r["senhas"]
    assert str(appdata) in r["cred"]
