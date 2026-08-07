# -*- coding: utf-8 -*-
"""Conversão .docx → PDF (organizador.pdf).

Bug que originou o módulo: numa máquina do escritório sem Microsoft Office o PDF
não era gerado. A detecção antiga era só `import win32com.client` — e o pywin32
VEM no bundle, então dava True mesmo sem Office: o app habilitava e marcava a
opção "PDF", e a falha só aparecia depois, numa linha do log oculto.

Estes testes travam as duas correções: detectar o Word de verdade e ter um
segundo conversor (LibreOffice).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from organizador import pdf


@pytest.fixture(autouse=True)
def _memo_limpo():
    pdf.limpar_memo()
    yield
    pdf.limpar_memo()


def _forjar(word: bool, soffice: str | None) -> None:
    """Finge a presença de cada conversor, sem tocar no registro/disco reais."""
    pdf._memo["word"] = word
    pdf._memo["soffice"] = soffice


# ---------------------------------------------------------------------
# Escolha do conversor
# ---------------------------------------------------------------------
def test_prefere_word_quando_ha_os_dois():
    _forjar(word=True, soffice=r"C:\LO\soffice.exe")
    assert pdf.conversor_disponivel() == "word"


def test_usa_libreoffice_quando_nao_ha_word():
    """O caso da máquina fraca: sem Office, mas com LibreOffice."""
    _forjar(word=False, soffice=r"C:\LO\soffice.exe")
    assert pdf.conversor_disponivel() == "libreoffice"


def test_sem_nenhum_conversor_devolve_none():
    _forjar(word=False, soffice=None)
    assert pdf.conversor_disponivel() is None


def test_pywin32_importavel_nao_basta_para_dizer_que_ha_word(monkeypatch):
    """A REGRESSÃO a evitar: o pywin32 está no bundle e importa sempre; isso não
    prova que o Word existe."""
    import sys
    import types

    # pywin32 presente...
    monkeypatch.setitem(sys.modules, "win32com", types.ModuleType("win32com"))
    monkeypatch.setitem(sys.modules, "win32com.client", types.ModuleType("win32com.client"))
    # ...mas o registro sem o ProgID do Word.
    import winreg

    def sem_word(_raiz, chave, *a, **k):
        if "Word.Application" in chave:
            raise OSError(2, "não existe")
        raise OSError(2, "não existe")

    monkeypatch.setattr(winreg, "OpenKey", sem_word)
    pdf.limpar_memo()
    assert pdf.word_instalado() is False
    assert pdf.conversor_disponivel() is None


def test_word_instalado_le_o_registro(monkeypatch):
    import winreg

    monkeypatch.setattr(pdf, "_pywin32_ok", lambda: True)
    vistas: list[str] = []

    class _Ctx:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def abrir(_raiz, chave, *a, **k):
        vistas.append(chave)
        return _Ctx()

    monkeypatch.setattr(winreg, "OpenKey", abrir)
    pdf.limpar_memo()
    assert pdf.word_instalado() is True
    assert any("Word.Application" in c for c in vistas)


def test_deteccao_e_memoizada(monkeypatch):
    """Não vale bater no registro/disco a cada petição gerada."""
    chamadas = {"n": 0}

    def contar():
        chamadas["n"] += 1
        return False

    monkeypatch.setattr(pdf, "_pywin32_ok", contar)
    pdf.limpar_memo()
    pdf.word_instalado()
    pdf.word_instalado()
    pdf.word_instalado()
    assert chamadas["n"] == 1


# ---------------------------------------------------------------------
# Mensagens para a interface
# ---------------------------------------------------------------------
def test_mensagem_de_indisponivel_nomeia_os_dois_programas():
    msg = pdf.descricao_indisponivel()
    assert "Word" in msg and "LibreOffice" in msg


def test_rotulos_legiveis():
    assert pdf.rotulo("word") == "Microsoft Word"
    assert pdf.rotulo("libreoffice") == "LibreOffice"
    assert pdf.rotulo("desconhecido") == "desconhecido"


# ---------------------------------------------------------------------
# Conversão
# ---------------------------------------------------------------------
def test_sem_conversor_levanta_com_mensagem_util(tmp_path):
    _forjar(word=False, soffice=None)
    with pytest.raises(RuntimeError, match="nenhum conversor de PDF"):
        pdf.converter_para_pdf(tmp_path / "peticao.docx")


def test_cai_para_libreoffice_quando_o_word_falha(tmp_path, monkeypatch):
    """Word instalado mas quebrado (licença, COM travado) não pode impedir o PDF:
    a pessoa quer o arquivo, não saber qual programa falhou."""
    _forjar(word=True, soffice=r"C:\LO\soffice.exe")
    docx = tmp_path / "peticao.docx"
    docx.write_bytes(b"PK\x03\x04")

    def word_quebrado(_p):
        raise RuntimeError("COM error -2147221005")

    def libre_ok(p):
        saida = p.with_suffix(".pdf")
        saida.write_bytes(b"%PDF-1.4")
        return saida

    monkeypatch.setitem(pdf._FUNCAO, "word", word_quebrado)
    monkeypatch.setitem(pdf._FUNCAO, "libreoffice", libre_ok)

    avisos: list[str] = []
    saida = pdf.converter_para_pdf(docx, log=avisos.append)
    assert saida.is_file() and saida.read_bytes().startswith(b"%PDF-")
    assert any("Microsoft Word falhou" in a for a in avisos)


def test_erro_final_lista_o_que_foi_tentado(tmp_path, monkeypatch):
    _forjar(word=True, soffice=r"C:\LO\soffice.exe")
    docx = tmp_path / "peticao.docx"
    docx.write_bytes(b"PK\x03\x04")

    def falha(_p):
        raise RuntimeError("sem chance")

    monkeypatch.setitem(pdf._FUNCAO, "word", falha)
    monkeypatch.setitem(pdf._FUNCAO, "libreoffice", falha)
    with pytest.raises(RuntimeError) as exc:
        pdf.converter_para_pdf(docx)
    assert "Microsoft Word" in str(exc.value) and "LibreOffice" in str(exc.value)


# ---------------------------------------------------------------------
# Invocação do LibreOffice
# ---------------------------------------------------------------------
def test_libreoffice_chamado_em_headless_com_perfil_isolado(tmp_path, monkeypatch):
    """`-env:UserInstallation` é obrigatório: sem ele, se o usuário tiver o
    LibreOffice ABERTO, o modo headless recusa a rodar."""
    _forjar(word=False, soffice=r"C:\LO\soffice.exe")
    docx = tmp_path / "peticao.docx"
    docx.write_bytes(b"PK\x03\x04")
    capturado: dict = {}

    def run_falso(cmd, **kwargs):
        capturado["cmd"] = cmd
        capturado["timeout"] = kwargs.get("timeout")
        (tmp_path / "peticao.pdf").write_bytes(b"%PDF-1.4")

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    monkeypatch.setattr(subprocess, "run", run_falso)
    saida = pdf.converter_para_pdf(docx)

    cmd = capturado["cmd"]
    assert cmd[0] == r"C:\LO\soffice.exe"
    assert "--headless" in cmd and "--convert-to" in cmd and "pdf" in cmd
    assert any(a.startswith("-env:UserInstallation=") for a in cmd)
    assert capturado["timeout"] == pdf._TIMEOUT_SOFFICE
    assert saida == tmp_path / "peticao.pdf"


def test_libreoffice_sem_produzir_arquivo_levanta(tmp_path, monkeypatch):
    _forjar(word=False, soffice=r"C:\LO\soffice.exe")
    docx = tmp_path / "peticao.docx"
    docx.write_bytes(b"PK\x03\x04")

    def run_falso(_cmd, **_k):
        class _R:
            returncode = 1
            stdout = ""
            stderr = "Error: source file could not be loaded"

        return _R()

    monkeypatch.setattr(subprocess, "run", run_falso)
    with pytest.raises(RuntimeError, match="não produziu o PDF"):
        pdf.converter_para_pdf(docx)


def test_caminho_libreoffice_acha_no_path(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda n: r"C:\bin\soffice.exe" if "soffice" in n else None)
    pdf.limpar_memo()
    assert pdf.caminho_libreoffice() == r"C:\bin\soffice.exe"


def test_caminho_libreoffice_none_quando_nao_existe(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _n: None)
    monkeypatch.setattr(pdf, "_CAMINHOS_SOFFICE", (r"Z:\nao\existe\soffice.exe",))
    pdf.limpar_memo()
    assert pdf.caminho_libreoffice() is None
