# -*- coding: utf-8 -*-
"""Conversão .docx → .pdf, com mais de um conversor.

POR QUE EXISTE
--------------
A conversão era feita direto pelo Microsoft Word via COM, e a checagem de
disponibilidade era só ``import win32com.client``. O pywin32 VEM no bundle, então
esse import sempre funciona — inclusive em máquina sem Office. Resultado: o app
habilitava e MARCAVA a opção "PDF", e só na hora de gerar o
``Dispatch("Word.Application")`` falhava; a petição .docx saía e o PDF não, com o
erro escondido numa linha do log. Foi o que apareceu ao testar numa máquina
fraca do escritório: "o PDF não está sendo gerado".

Duas correções:

1. Detectar o Word DE VERDADE (registro do Windows, sem subir COM — é barato e
   não abre o Word só para perguntar se ele existe).
2. Um segundo conversor: o LibreOffice (``soffice --convert-to pdf``), que é o
   que costuma existir numa máquina sem Office. Assim a máquina fraca gera PDF
   em vez de só avisar que não dá.

Quando nenhum dos dois existe, a interface diz isso ANTES de a pessoa tentar
gerar, e o erro (se ela tentar) nomeia os dois programas aceitos.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

# Ordem de preferência. O Word é o primeiro porque é o fluxo validado no
# escritório e preserva melhor a formatação dos modelos .docx.
CONVERSORES = ("word", "libreoffice")

_ROTULO = {
    "word": "Microsoft Word",
    "libreoffice": "LibreOffice",
}

# Caminhos onde o soffice costuma ficar, além do PATH.
_CAMINHOS_SOFFICE = (
    r"%ProgramFiles%\LibreOffice\program\soffice.exe",
    r"%ProgramFiles(x86)%\LibreOffice\program\soffice.exe",
    r"%ProgramFiles%\LibreOffice 7\program\soffice.exe",
)

_TIMEOUT_SOFFICE = 180  # s por documento (máquina fraca + 1º start é lento)

# Memo por processo: a detecção mexe em registro/disco e o resultado não muda
# durante a execução.
_memo: dict[str, object] = {}


def _pywin32_ok() -> bool:
    try:
        import win32com.client  # noqa: F401

        return True
    except Exception:
        return False


def word_instalado() -> bool:
    """O Microsoft Word está instalado?

    Olha o registro (``HKCR\\Word.Application``), que é como o COM resolve o
    ProgID. NÃO usa ``Dispatch`` de propósito: subir o Word só para descobrir se
    ele existe custa segundos e deixa processo pendurado numa máquina fraca."""
    if "word" in _memo:
        return bool(_memo["word"])
    ok = False
    if os.name == "nt" and _pywin32_ok():
        try:
            import winreg

            for chave in (r"Word.Application\CurVer", "Word.Application"):
                try:
                    with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, chave):
                        ok = True
                        break
                except OSError:
                    continue
        except Exception:
            ok = False
    _memo["word"] = ok
    return ok


def caminho_libreoffice() -> str | None:
    """Caminho do ``soffice``, ou None."""
    if "soffice" in _memo:
        return _memo["soffice"]  # type: ignore[return-value]
    achado = shutil.which("soffice") or shutil.which("soffice.exe")
    if not achado:
        for bruto in _CAMINHOS_SOFFICE:
            caminho = os.path.expandvars(bruto)
            if "%" not in caminho and os.path.isfile(caminho):
                achado = caminho
                break
    _memo["soffice"] = achado
    return achado


def conversor_disponivel() -> str | None:
    """Qual conversor usar nesta máquina ("word" | "libreoffice" | None)."""
    for nome in CONVERSORES:
        if nome == "word" and word_instalado():
            return "word"
        if nome == "libreoffice" and caminho_libreoffice():
            return "libreoffice"
    return None


def descricao_indisponivel() -> str:
    """Mensagem para a interface quando não há conversor."""
    return (
        "nenhum conversor de PDF encontrado (é preciso Microsoft Word ou "
        "LibreOffice instalado) — contate o TI"
    )


def rotulo(nome: str) -> str:
    return _ROTULO.get(nome, nome)


def limpar_memo() -> None:
    """Esquece a detecção (usado nos testes)."""
    _memo.clear()


# --------------------------------------------------------------------------
# Conversores
# --------------------------------------------------------------------------
def _converter_com_word_aberto(word, docx_path: Path) -> Path:
    """Converte usando uma instância do Word JÁ aberta — não abre nem fecha o
    processo. Existe para o lote (`_lote_word`) reaproveitar a mesma sessão em
    vez de pagar o custo de subir/derrubar o Word a cada documento."""
    pdf_path = docx_path.with_suffix(".pdf")
    doc = word.Documents.Open(str(docx_path.resolve()))
    try:
        doc.SaveAs(str(pdf_path.resolve()), FileFormat=17)  # 17 = wdFormatPDF
    finally:
        doc.Close(SaveChanges=False)
    return pdf_path


def _converter_com_word(docx_path: Path) -> Path:
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    word = None
    try:
        word = win32com.client.Dispatch("Word.Application")
        word.Visible = False
        word.DisplayAlerts = False
        return _converter_com_word_aberto(word, docx_path)
    finally:
        if word is not None:
            try:
                word.Quit(SaveChanges=False)
            except Exception:
                pass
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def _converter_com_libreoffice(docx_path: Path) -> Path:
    soffice = caminho_libreoffice()
    if not soffice:
        raise RuntimeError("LibreOffice não encontrado.")

    destino = docx_path.parent
    # `-env:UserInstallation` isola o perfil: sem isso, se o usuário tiver o
    # LibreOffice ABERTO, o modo headless recusa a rodar ("javaldx"/perfil em uso).
    perfil = (destino / ".soffice_perfil").resolve().as_uri()
    cmd = [
        soffice,
        f"-env:UserInstallation={perfil}",
        "--headless", "--norestore", "--nolockcheck", "--nodefault", "--nologo",
        "--convert-to", "pdf",
        "--outdir", str(destino.resolve()),
        str(docx_path.resolve()),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=_TIMEOUT_SOFFICE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    pdf_path = destino / (docx_path.stem + ".pdf")
    if not pdf_path.is_file():
        detalhe = (proc.stderr or proc.stdout or "").strip()[:200]
        raise RuntimeError(
            f"LibreOffice não produziu o PDF (código {proc.returncode})"
            + (f": {detalhe}" if detalhe else "")
        )
    return pdf_path


_FUNCAO = {"word": _converter_com_word, "libreoffice": _converter_com_libreoffice}


def converter_para_pdf(docx_path: Path, log=None) -> Path:
    """Converte `docx_path` para PDF no mesmo diretório e devolve o caminho.

    Tenta os conversores em ordem; se o primeiro falhar (Word instalado mas
    quebrado, licença expirada, COM travado), tenta o seguinte antes de desistir
    — a pessoa quer o PDF, não um diagnóstico de qual programa falhou."""
    docx_path = Path(docx_path)
    candidatos = [n for n in CONVERSORES if (
        word_instalado() if n == "word" else caminho_libreoffice()
    )]
    if not candidatos:
        raise RuntimeError(descricao_indisponivel())

    erros: list[str] = []
    for nome in candidatos:
        try:
            return _FUNCAO[nome](docx_path)
        except Exception as exc:
            erros.append(f"{rotulo(nome)}: {type(exc).__name__}: {exc}")
            if log and len(candidatos) > 1:
                log(f"  {rotulo(nome)} falhou; tentando outro conversor...")
    raise RuntimeError("falha ao converter para PDF — " + " | ".join(erros))


def _lote_word(docx_paths: list[Path], pdfs: list[Path], log=None) -> list[tuple[Path, str]]:
    """Converte o que puder de `docx_paths` usando UMA única instância do Word
    para o lote inteiro (abrir/fechar o Word custa segundos; pagar esse custo
    uma vez por lote em vez de uma vez por arquivo é o ganho real numa máquina
    fraca gerando várias petições).

    Devolve os que falharam nesta sessão, junto com o motivo, para o chamador
    tentar outro conversor — mesma semântica de fallback do `converter_para_pdf`
    de arquivo único."""
    import pythoncom
    import win32com.client

    pendentes: list[tuple[Path, str]] = []
    pythoncom.CoInitialize()
    word = None
    try:
        word = win32com.client.Dispatch("Word.Application")
        word.Visible = False
        word.DisplayAlerts = False
        for docx_path in docx_paths:
            try:
                pdf_path = _converter_com_word_aberto(word, docx_path)
                pdfs.append(pdf_path)
                if log:
                    log(f"  PDF: {pdf_path.name}")
            except Exception as exc:
                if log:
                    log(f"  Microsoft Word falhou em {docx_path.name}; tentando outro conversor...")
                pendentes.append(
                    (docx_path, f"{rotulo('word')}: {type(exc).__name__}: {exc}")
                )
    finally:
        if word is not None:
            try:
                word.Quit(SaveChanges=False)
            except Exception:
                pass
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass
    return pendentes


def converter_lote_para_pdf(
    docx_paths: list[Path], log=None
) -> tuple[list[Path], list[str]]:
    """Converte vários `.docx` para PDF, reaproveitando um único conversor
    aberto para o lote inteiro (o Word, em particular — ver `_lote_word`).

    Uma falha isolada não aborta o lote: o arquivo que falhou é reportado em
    `erros` e os demais continuam. Devolve `(pdfs, erros)`."""
    docx_paths = [Path(p) for p in docx_paths]
    if not docx_paths:
        return [], []

    candidatos = [n for n in CONVERSORES if (
        word_instalado() if n == "word" else caminho_libreoffice()
    )]
    if not candidatos:
        raise RuntimeError(descricao_indisponivel())

    pdfs: list[Path] = []
    erros: list[str] = []

    if "word" in candidatos:
        pendentes = _lote_word(docx_paths, pdfs, log=log)
    else:
        pendentes = [(p, "") for p in docx_paths]

    outros = [n for n in candidatos if n != "word"]
    for docx_path, detalhe_word in pendentes:
        detalhes = [detalhe_word] if detalhe_word else []
        sucesso = False
        for nome in outros:
            try:
                pdf_path = _FUNCAO[nome](docx_path)
                pdfs.append(pdf_path)
                sucesso = True
                if log:
                    log(f"  PDF: {pdf_path.name}")
                break
            except Exception as exc:
                detalhes.append(f"{rotulo(nome)}: {type(exc).__name__}: {exc}")
        if not sucesso:
            motivo = " | ".join(detalhes) if detalhes else "nenhum conversor disponível"
            erros.append(f"{docx_path.name}: {motivo}")
            if log:
                log(f"  ERRO ao gerar PDF de {docx_path.name}: {motivo}")

    return pdfs, erros
