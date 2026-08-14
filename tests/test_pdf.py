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


# ---------------------------------------------------------------------
# Conversão em lote — reaproveita UMA instância do Word para o lote inteiro
# ---------------------------------------------------------------------
def test_lote_word_usa_uma_unica_sessao(tmp_path, monkeypatch):
    """N documentos == 1 abertura/fechamento do Word, não N."""
    _forjar(word=True, soffice=None)
    docs = [tmp_path / f"peticao{i}.docx" for i in range(3)]
    for d in docs:
        d.write_bytes(b"PK\x03\x04")

    aberturas = {"n": 0}
    fechamentos = {"n": 0}

    def abrir_lote(paths, pdfs, log=None):
        aberturas["n"] += 1
        for p in paths:
            saida = p.with_suffix(".pdf")
            saida.write_bytes(b"%PDF-1.4")
            pdfs.append(saida)
            fechamentos["n"] += 1  # simboliza 1 conversão dentro da mesma sessão
        return []

    monkeypatch.setattr(pdf, "_lote_word", abrir_lote)
    pdfs, erros = pdf.converter_lote_para_pdf(docs)

    assert aberturas["n"] == 1, "deveria abrir o Word 1x para o lote inteiro"
    assert fechamentos["n"] == 3
    assert not erros
    assert all(p.is_file() for p in pdfs)


def test_lote_uma_falha_isolada_nao_aborta_as_demais(tmp_path, monkeypatch):
    _forjar(word=True, soffice=r"C:\LO\soffice.exe")
    doc_ok1 = tmp_path / "ok1.docx"
    doc_falha = tmp_path / "falha.docx"
    doc_ok2 = tmp_path / "ok2.docx"
    for d in (doc_ok1, doc_falha, doc_ok2):
        d.write_bytes(b"PK\x03\x04")

    def lote_word_falso(paths, pdfs, log=None):
        pendentes = []
        for p in paths:
            if p.name == "falha.docx":
                pendentes.append((p, "Microsoft Word: RuntimeError: quebrado"))
                continue
            saida = p.with_suffix(".pdf")
            saida.write_bytes(b"%PDF-1.4")
            pdfs.append(saida)
        return pendentes

    def libre_falha(_p):
        raise RuntimeError("também não converteu")

    monkeypatch.setattr(pdf, "_lote_word", lote_word_falso)
    monkeypatch.setitem(pdf._FUNCAO, "libreoffice", libre_falha)

    avisos: list[str] = []
    pdfs, erros = pdf.converter_lote_para_pdf(
        [doc_ok1, doc_falha, doc_ok2], log=avisos.append
    )

    assert {p.stem for p in pdfs} == {"ok1", "ok2"}
    assert len(erros) == 1
    assert "falha.docx" in erros[0]
    assert "Microsoft Word" in erros[0] and "LibreOffice" in erros[0]


def test_lote_sem_conversor_levanta(tmp_path):
    _forjar(word=False, soffice=None)
    with pytest.raises(RuntimeError, match="nenhum conversor de PDF"):
        pdf.converter_lote_para_pdf([tmp_path / "peticao.docx"])


def test_lote_vazio_nao_faz_nada():
    assert pdf.converter_lote_para_pdf([]) == ([], [])


def test_lote_sem_word_vai_direto_ao_outro_conversor(tmp_path, monkeypatch):
    """Máquina só com LibreOffice: nada de Word é chamado, e os PDFs saem."""
    _forjar(word=False, soffice=r"C:\LO\soffice.exe")
    docs = [tmp_path / f"p{i}.docx" for i in range(2)]
    for d in docs:
        d.write_bytes(b"PK\x03\x04")

    def _lote_word_proibido(*_a, **_k):
        raise AssertionError("não pode tentar o Word numa máquina sem Word")

    def libre_ok(p):
        saida = p.with_suffix(".pdf")
        saida.write_bytes(b"%PDF-1.4")
        return saida

    monkeypatch.setattr(pdf, "_lote_word", _lote_word_proibido)
    monkeypatch.setitem(pdf._FUNCAO, "libreoffice", libre_ok)

    avisos: list[str] = []
    pdfs, erros = pdf.converter_lote_para_pdf(docs, log=avisos.append)
    assert {p.stem for p in pdfs} == {"p0", "p1"}
    assert not erros
    assert sum("PDF:" in a for a in avisos) == 2


# ---------------------------------------------------------------------
# Detecção: os desfechos degradados
# ---------------------------------------------------------------------
def test_pywin32_ausente_nao_derruba_a_deteccao(monkeypatch):
    """Rodando do código-fonte fora do bundle o pywin32 pode não existir; isso é
    "sem Word", não uma exceção subindo pelo caminho de abertura."""
    import sys

    monkeypatch.setitem(sys.modules, "win32com.client", None)  # import -> ImportError
    assert pdf._pywin32_ok() is False


def test_registro_ilegivel_e_tratado_como_sem_word(monkeypatch):
    """Erro inesperado do winreg (não OSError) não pode virar traceback: sem
    certeza de que há Word, o certo é seguir para o LibreOffice."""
    import winreg

    monkeypatch.setattr(pdf, "_pywin32_ok", lambda: True)
    monkeypatch.setattr(winreg, "OpenKey", lambda *_a, **_k: (_ for _ in ()).throw(ValueError("registro corrompido")))
    pdf.limpar_memo()
    assert pdf.word_instalado() is False


def test_libreoffice_encontrado_fora_do_path(monkeypatch, tmp_path):
    """Instalação padrão do LibreOffice não põe o soffice no PATH."""
    import shutil

    soffice = tmp_path / "soffice.exe"
    soffice.write_bytes(b"")
    monkeypatch.setattr(shutil, "which", lambda _n: None)
    monkeypatch.setattr(pdf, "_CAMINHOS_SOFFICE", (r"Z:\nao\existe.exe", str(soffice)))
    pdf.limpar_memo()
    assert pdf.caminho_libreoffice() == str(soffice)


def test_converter_com_libreoffice_sem_o_programa_levanta(tmp_path):
    _forjar(word=False, soffice=None)
    with pytest.raises(RuntimeError, match="LibreOffice não encontrado"):
        pdf._converter_com_libreoffice(tmp_path / "peticao.docx")


# ---------------------------------------------------------------------
# Word via COM — dublês de pythoncom/win32com
#
# Estes caminhos rodam em TODA geração de PDF na máquina do escritório e nunca
# tiveram teste: um `doc` não fechado ou um Word não encerrado deixa WINWORD.EXE
# pendurado consumindo memória, que é exatamente o que dói na máquina fraca.
# ---------------------------------------------------------------------
class _DocFalso:
    def __init__(self, caminho, falhar=False):
        self.caminho = caminho
        self.falhar = falhar
        self.salvo_em = None
        self.formato = None
        self.fechado = False

    def SaveAs(self, destino, FileFormat=None):  # noqa: N803 - assinatura do COM
        if self.falhar:
            raise RuntimeError("COM error -2147221005")
        self.salvo_em = destino
        self.formato = FileFormat

    def Close(self, SaveChanges=None):  # noqa: N803 - assinatura do COM
        self.fechado = True


class _DocumentsFalso:
    def __init__(self, dono):
        self._dono = dono

    def Open(self, caminho):  # noqa: N802 - assinatura do COM
        doc = _DocFalso(caminho, falhar=Path(caminho).name in self._dono.falham)
        self._dono.abertos.append(doc)
        return doc


class _WordFalso:
    def __init__(self, falham=(), quit_falha=False):
        self.falham = set(falham)
        self.abertos: list[_DocFalso] = []
        self.Documents = _DocumentsFalso(self)
        self.Visible = None
        self.DisplayAlerts = None
        self.encerrado = 0
        self._quit_falha = quit_falha

    def Quit(self, SaveChanges=None):  # noqa: N803 - assinatura do COM
        self.encerrado += 1
        if self._quit_falha:
            raise RuntimeError("Word já morreu")


@pytest.fixture
def com_falso(monkeypatch):
    """Instala dublês de `pythoncom` e `win32com.client`. Devolve o estado."""
    import sys
    import types

    estado = {"co_init": 0, "co_uninit": 0, "dispatch": [], "word": None,
              "uninit_falha": False}

    pythoncom = types.ModuleType("pythoncom")
    pythoncom.CoInitialize = lambda: estado.__setitem__("co_init", estado["co_init"] + 1)

    def _co_uninit():
        estado["co_uninit"] += 1
        if estado["uninit_falha"]:
            raise RuntimeError("CoUninitialize falhou")

    pythoncom.CoUninitialize = _co_uninit

    win32com = types.ModuleType("win32com")
    client = types.ModuleType("win32com.client")

    def _dispatch(progid):
        estado["dispatch"].append(progid)
        if estado["word"] is None:
            estado["word"] = _WordFalso()
        return estado["word"]

    client.Dispatch = _dispatch
    win32com.client = client

    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    return estado


def test_converter_com_word_salva_em_pdf_e_encerra(tmp_path, com_falso):
    docx = tmp_path / "peticao.docx"
    docx.write_bytes(b"PK\x03\x04")

    saida = pdf._converter_com_word(docx)

    word = com_falso["word"]
    assert saida == docx.with_suffix(".pdf")
    assert com_falso["dispatch"] == ["Word.Application"]
    assert word.Visible is False and word.DisplayAlerts is False
    assert word.abertos[0].formato == 17, "17 = wdFormatPDF"
    assert word.abertos[0].fechado, "documento ficou aberto no Word"
    assert word.encerrado == 1, "WINWORD.EXE ficaria pendurado"
    assert com_falso["co_init"] == 1 and com_falso["co_uninit"] == 1


def test_falha_na_conversao_ainda_fecha_documento_e_word(tmp_path, com_falso):
    """O desfecho que enche a máquina de WINWORD.EXE: erro no meio da conversão."""
    docx = tmp_path / "quebrado.docx"
    docx.write_bytes(b"PK\x03\x04")
    com_falso["word"] = _WordFalso(falham={"quebrado.docx"})

    with pytest.raises(RuntimeError):
        pdf._converter_com_word(docx)

    word = com_falso["word"]
    assert word.abertos[0].fechado
    assert word.encerrado == 1
    assert com_falso["co_uninit"] == 1


def test_quit_e_couninitialize_quebrados_nao_mascaram_o_resultado(tmp_path, com_falso):
    """Limpeza é best-effort: se o Word já morreu, o PDF gerado continua válido."""
    docx = tmp_path / "peticao.docx"
    docx.write_bytes(b"PK\x03\x04")
    com_falso["word"] = _WordFalso(quit_falha=True)
    com_falso["uninit_falha"] = True

    assert pdf._converter_com_word(docx) == docx.with_suffix(".pdf")


def test_lote_word_abre_o_word_uma_vez_para_todos(tmp_path, com_falso):
    docs = [tmp_path / f"p{i}.docx" for i in range(4)]
    for d in docs:
        d.write_bytes(b"PK\x03\x04")

    pdfs: list[Path] = []
    avisos: list[str] = []
    pendentes = pdf._lote_word(docs, pdfs, log=avisos.append)

    assert com_falso["dispatch"] == ["Word.Application"], "abriu o Word mais de uma vez"
    assert com_falso["co_init"] == 1 and com_falso["co_uninit"] == 1
    assert len(pdfs) == 4 and not pendentes
    assert com_falso["word"].encerrado == 1
    assert all(d.fechado for d in com_falso["word"].abertos)


def test_lote_word_devolve_pendentes_para_o_proximo_conversor(tmp_path, com_falso):
    docs = [tmp_path / n for n in ("ok.docx", "ruim.docx", "ok2.docx")]
    for d in docs:
        d.write_bytes(b"PK\x03\x04")
    com_falso["word"] = _WordFalso(falham={"ruim.docx"})

    pdfs: list[Path] = []
    avisos: list[str] = []
    pendentes = pdf._lote_word(docs, pdfs, log=avisos.append)

    assert {p.stem for p in pdfs} == {"ok", "ok2"}
    assert [p.name for p, _ in pendentes] == ["ruim.docx"]
    assert "Microsoft Word" in pendentes[0][1]
    assert any("tentando outro conversor" in a for a in avisos)
    assert com_falso["word"].encerrado == 1, "uma falha isolada não pode deixar o Word aberto"


def test_lote_com_word_moribundo_ainda_entrega_os_pdfs(tmp_path, com_falso):
    """Word que morre no Quit (ou COM que recusa o CoUninitialize) não pode
    transformar um lote bem convertido em erro."""
    docx = tmp_path / "peticao.docx"
    docx.write_bytes(b"PK\x03\x04")
    com_falso["word"] = _WordFalso(quit_falha=True)
    com_falso["uninit_falha"] = True

    pdfs: list[Path] = []
    assert pdf._lote_word([docx], pdfs) == []
    assert pdfs == [docx.with_suffix(".pdf")]


def test_lote_completo_com_word_real_de_mentira(tmp_path, com_falso):
    """Ponta a ponta de `converter_lote_para_pdf` pelo caminho do Word."""
    _forjar(word=True, soffice=None)
    docs = [tmp_path / f"p{i}.docx" for i in range(3)]
    for d in docs:
        d.write_bytes(b"PK\x03\x04")

    pdfs, erros = pdf.converter_lote_para_pdf(docs)
    assert len(pdfs) == 3 and not erros
    assert com_falso["dispatch"] == ["Word.Application"]
