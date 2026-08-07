# -*- coding: utf-8 -*-
"""Caches que precisam sobreviver ao _MEIPASS do PyInstaller.

No modo onefile o bootloader extrai os dados embutidos para uma pasta NOVA em
%TEMP% a cada abertura, e os arquivos ganham timestamp novo. Qualquer cache
cujo invalidador seja o `mtime` de um arquivo do bundle portanto NUNCA acerta no
executável — só rodando do código-fonte.

Esse bug já apareceu duas vezes neste projeto:
  1. o índice de resumos usava `mtime` de data/processos_base.json.gz, e o
     snapshot de 70 MB era reparseado em toda abertura do .exe (~25 s num i3);
  2. o cache de placeholders dos modelos .docx, com o mesmo padrão.

Estes testes existem para não haver uma terceira vez.
"""

from __future__ import annotations

import os
import sys

from organizador import base_embutida, gui
from organizador.indice_processos import caminho_sqlite


class _Congelado:
    """Simula o app empacotado: sys.frozen + sys._MEIPASS."""

    def __init__(self, monkeypatch, meipass):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)


def _docx_falso(pasta, nome: str, tamanho: int, mtime: float):
    pasta.mkdir(parents=True, exist_ok=True)
    p = pasta / nome
    p.write_bytes(b"x" * tamanho)
    os.utime(p, (mtime, mtime))
    return p


def test_chave_de_template_ignora_mtime_quando_empacotado(monkeypatch, tmp_path):
    """O caso que quebrava: mesma extração lógica, mtime diferente."""
    _Congelado(monkeypatch, tmp_path / "meipass1")

    a = _docx_falso(tmp_path / "extracao1", "Penhora.docx", 1234, 1_700_000_000)
    b = _docx_falso(tmp_path / "extracao2", "Penhora.docx", 1234, 1_800_000_000)
    assert gui._chave_cache_template(a) == gui._chave_cache_template(b), (
        "a chave mudou só porque o mtime mudou — no .exe isso significa "
        "reescanear os 39 modelos em TODA abertura"
    )


def test_chave_de_template_muda_quando_o_modelo_muda(monkeypatch, tmp_path):
    """A chave não pode ser estável demais: editar um modelo tem de invalidar."""
    _Congelado(monkeypatch, tmp_path / "meipass")
    a = _docx_falso(tmp_path / "v1", "Penhora.docx", 1234, 1_700_000_000)
    b = _docx_falso(tmp_path / "v2", "Penhora.docx", 5678, 1_700_000_000)
    assert gui._chave_cache_template(a) != gui._chave_cache_template(b)


def test_chave_de_template_muda_com_a_versao_do_app(monkeypatch, tmp_path):
    """Modelos viajam dentro da release: versão nova = revalidar."""
    _Congelado(monkeypatch, tmp_path / "meipass")
    a = _docx_falso(tmp_path / "x", "Penhora.docx", 1234, 1_700_000_000)
    chave_antes = gui._chave_cache_template(a)
    monkeypatch.setattr(gui, "__version__", "99.99.99")
    assert gui._chave_cache_template(a) != chave_antes


def test_chave_de_template_usa_mtime_rodando_do_fonte(monkeypatch, tmp_path):
    """Do fonte o mtime é estável e é o sinal mais preciso de edição."""
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    a = _docx_falso(tmp_path / "f", "Penhora.docx", 1234, 1_700_000_000)
    chave_antes = gui._chave_cache_template(a)
    os.utime(a, (1_800_000_000, 1_800_000_000))
    assert gui._chave_cache_template(a) != chave_antes


def test_chave_de_template_tolera_arquivo_ausente(tmp_path):
    assert gui._chave_cache_template(tmp_path / "nao_existe.docx")


def test_base_embutida_nao_depende_de_mtime(monkeypatch, tmp_path):
    """A base SQLite é validada por versão de esquema, não por mtime: a mesma
    base extraída em pastas diferentes tem de continuar válida."""
    from dataclasses import asdict

    from organizador.indice_processos import ORIGEM_EMBUTIDO, _resumo_de_dict
    from organizador.models import Processo

    db = tmp_path / "processos_base.sqlite"
    base_embutida.gravar(
        db,
        [asdict(Processo(numero="111", classe="Execução"))],
        resumo_de_dict=lambda d: _resumo_de_dict(d, ORIGEM_EMBUTIDO, ORIGEM_EMBUTIDO),
    )
    assert base_embutida.disponivel(db)

    # "Reextrai": novo mtime, mesmo conteúdo.
    os.utime(db, (1_900_000_000, 1_900_000_000))
    assert base_embutida.disponivel(db), "a base deixou de valer só por mudar o mtime"
    assert [d["numero"] for d in base_embutida.ler_resumos(db)] == ["111"]
    assert base_embutida.ler_processo(db, "111")["classe"] == "Execução"


def test_caminho_sqlite_acompanha_o_snapshot_no_meipass(monkeypatch, tmp_path):
    """No .exe o .sqlite fica ao lado do snapshot, dentro do _MEIPASS."""
    meipass = tmp_path / "_MEI12345"
    (meipass / "data").mkdir(parents=True)
    assert (
        caminho_sqlite(meipass / "data" / "processos_base.json.gz")
        == meipass / "data" / "processos_base.sqlite"
    )
