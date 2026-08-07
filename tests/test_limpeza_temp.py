# -*- coding: utf-8 -*-
"""Limpeza das pastas _MEI* órfãs no %TEMP% (organizador.gui).

Por que existe: no modo onefile o app extrai ~98 MB para uma pasta ``_MEIxxxxx``
nova a cada abertura e a apaga ao sair — mas o Atualizador encerra o app com
TerminateProcess para trocar o .exe, e um processo morto assim nunca faz essa
limpeza. Foram encontradas 16 pastas órfãs nesta máquina, o que dói justamente
nas máquinas fracas, que têm disco pequeno.

Requisitos de SEGURANÇA (mais importantes que a limpeza em si):
  - nunca apagar a pasta do processo atual (derrubaria o app em execução);
  - nunca falhar por causa de pasta em uso por outra instância viva;
  - nunca propagar exceção (roda no caminho de abertura).
"""

from __future__ import annotations

import sys

from organizador.gui import _limpar_temp_orfaos


def _criar_mei(raiz, nome: str, com_arquivo: bool = True):
    d = raiz / nome
    (d / "sub").mkdir(parents=True)
    if com_arquivo:
        (d / "sub" / "modulo.pyd").write_bytes(b"binario")
        (d / "raiz.txt").write_text("x", encoding="utf-8")
    return d


def test_remove_orfaos(monkeypatch, tmp_path):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    a = _criar_mei(tmp_path, "_MEI111")
    b = _criar_mei(tmp_path, "_MEI222")

    assert _limpar_temp_orfaos() == 2
    assert not a.exists() and not b.exists()


def test_preserva_a_pasta_do_processo_atual(monkeypatch, tmp_path):
    """O caso perigoso: apagar o próprio _MEIPASS derrubaria o app rodando."""
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    atual = _criar_mei(tmp_path, "_MEI_ATUAL")
    outra = _criar_mei(tmp_path, "_MEI_OUTRA")
    monkeypatch.setattr(sys, "_MEIPASS", str(atual), raising=False)

    assert _limpar_temp_orfaos() == 1
    assert atual.exists(), "apagou a pasta do próprio processo"
    assert not outra.exists()


def test_ignora_o_que_nao_e_mei(monkeypatch, tmp_path):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    outro = tmp_path / "pasta_qualquer"
    outro.mkdir()
    arquivo = tmp_path / "_MEI_nao_sou_pasta.txt"
    arquivo.write_text("x", encoding="utf-8")
    mei = _criar_mei(tmp_path, "_MEI999")

    assert _limpar_temp_orfaos() == 1
    assert outro.exists() and arquivo.exists()
    assert not mei.exists()


def test_pasta_travada_e_contabilizada_como_nao_removida(monkeypatch, tmp_path):
    """Instância viva de outra sessão: o rmtree falha e a pasta FICA — sem
    exceção e sem contar como removida (foi o que aconteceu na verificação
    manual: 6 pastas em uso por processos vivos, 0 removidas)."""
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    travada = _criar_mei(tmp_path, "_MEI_TRAVADA")
    livre = _criar_mei(tmp_path, "_MEI_LIVRE")

    import shutil
    real = shutil.rmtree

    def rmtree_falso(caminho, *a, **k):
        if "TRAVADA" in str(caminho):
            return  # ignore_errors=True: falha silenciosa, igual ao WinError 5
        return real(caminho, *a, **k)

    monkeypatch.setattr(shutil, "rmtree", rmtree_falso)
    assert _limpar_temp_orfaos() == 1
    assert travada.exists()
    assert not livre.exists()


def test_temp_inacessivel_nao_levanta(monkeypatch, tmp_path):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path / "nao_existe"))
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert _limpar_temp_orfaos() == 0  # sem exceção


def test_temp_vazio(monkeypatch, tmp_path):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert _limpar_temp_orfaos() == 0
