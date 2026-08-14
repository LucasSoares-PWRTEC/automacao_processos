# -*- coding: utf-8 -*-
"""Limpeza das pastas _MEI* órfãs no %TEMP% (organizador.gui).

Por que existe: no modo onefile o app extrai ~98 MB para uma pasta ``_MEIxxxxx``
nova a cada abertura e a apaga ao sair — mas um processo morto sem passar por
essa saída normal (crash, "Finalizar tarefa", queda de energia) nunca faz essa
limpeza. Foram encontradas 16 pastas órfãs nesta máquina, o que dói justamente
nas máquinas fracas, que têm disco pequeno.

Requisitos de SEGURANÇA (mais importantes que a limpeza em si):
  - nunca apagar a pasta do processo atual (derrubaria o app em execução);
  - nunca tocar na pasta de outra instância viva (era o bug: o rmtree não
    "falha" nela, apaga tudo que não está travado e deixa só as DLLs);
  - nunca propagar exceção (roda no caminho de abertura).

O fim deste arquivo cobre o alarme que torna o estrago audível caso ele volte a
acontecer por outro caminho: dados do bundle ausentes no _MEIPASS.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from organizador.gui import (
    _EXTRACAO_S, _dados_do_bundle_faltando, _limpar_temp_orfaos, _mei_em_uso,
    _mei_recem_escrito,
)


def _envelhecer(d: Path, segundos: float | None = None) -> Path:
    """Recua o mtime de `d` e de tudo dentro dela.

    Órfã de verdade é sempre VELHA: a pasta parou de receber bytes quando o
    processo morreu. Pasta recém-criada é o que uma instância ABRINDO tem, e
    apagá-la é o bug que mutilava a extração (ver `_EXTRACAO_S` em gui.py).
    """
    quando = time.time() - (segundos if segundos is not None else _EXTRACAO_S + 60)
    for alvo in sorted(d.rglob("*"), reverse=True):
        os.utime(alvo, (quando, quando))
    os.utime(d, (quando, quando))
    return d


def _criar_mei(raiz, nome: str, com_arquivo: bool = True, orfa: bool = True):
    d = raiz / nome
    (d / "sub").mkdir(parents=True)
    if com_arquivo:
        (d / "sub" / "modulo.pyd").write_bytes(b"binario")
        (d / "raiz.txt").write_text("x", encoding="utf-8")
    return _envelhecer(d) if orfa else d


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


def test_nao_apaga_pasta_em_extracao(monkeypatch, tmp_path):
    """O bug que mutilava instâncias abrindo.

    O bootloader extrai os ~98 MB ANTES de carregar o python3xx.dll, então
    durante a extração NADA está travado e a sonda `_mei_em_uso` diz "órfã".
    Numa máquina fraca isso leva segundos, e outra instância abrindo nessa janela
    fazia rmtree no meio da extração. O app morria depois, longe daqui, num
    ImportError de .pyd — foi assim que apareceu `cannot import name
    '_imagingtk' from 'PIL'` numa VM com 96 GB de disco livre.
    """
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    extraindo = _criar_mei(tmp_path, "_MEI_EXTRAINDO", orfa=False)
    velha = _criar_mei(tmp_path, "_MEI_VELHA")

    assert _limpar_temp_orfaos() == 1
    assert extraindo.exists(), "apagou a pasta de uma instância que está abrindo"
    assert (extraindo / "sub" / "modulo.pyd").exists(), "mutilou a extração"
    assert not velha.exists(), "deixou de limpar a órfã de verdade"


def test_idade_e_medida_pelo_arquivo_mais_novo(tmp_path):
    """A pasta em si pode ter mtime velho enquanto os binários chegam: o mtime do
    diretório só muda quando entradas são criadas/removidas nele."""
    d = _criar_mei(tmp_path, "_MEI_MISTA")
    assert not _mei_recem_escrito(str(d))

    (d / "chegou_agora.pyd").write_bytes(b"novo")
    assert _mei_recem_escrito(str(d)), "não viu o arquivo recém-escrito"


def test_pasta_ilegivel_e_tratada_como_recente(tmp_path):
    """Na dúvida sobra lixo, que é barato — não se mutila extração alheia."""
    assert _mei_recem_escrito(str(tmp_path / "nem_existe"))


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


def _mei_com_dll_carregada(raiz, nome: str):
    """Um ``_MEI`` de instância VIVA: DLL de verdade carregada neste processo
    (o Windows trava a imagem) + os dados que o app lê de ``_MEIPASS``."""
    import ctypes
    import shutil as _shutil

    origem = Path(sys.base_prefix) / "vcruntime140.dll"
    if not origem.exists():
        candidatas = sorted(Path(sys.base_prefix).glob("*.dll"))
        if not candidatas:
            pytest.skip("nenhuma DLL disponível para simular instância viva")
        origem = candidatas[0]

    d = raiz / nome
    d.mkdir(parents=True)
    _shutil.copy2(origem, d / origem.name)
    (d / "Petições").mkdir()
    (d / "Petições" / "modelo.docx").write_bytes(b"docx")
    (d / "data").mkdir()
    (d / "data" / "processos_base.sqlite").write_bytes(b"sqlite")
    (d / "config").mkdir()
    (d / "config" / "credenciais.json").write_text("{}", encoding="utf-8")
    handle = ctypes.CDLL(str(d / origem.name))  # mantém a DLL carregada
    return d, handle


def test_nao_toca_na_pasta_de_outra_instancia_viva(monkeypatch, tmp_path):
    """O bug que motivou a sonda: ``rmtree(ignore_errors=True)`` NÃO "falha" numa
    pasta em uso — ele apaga tudo que não está travado e deixa só as DLLs. A
    instância viva perdia Petições/, data/ e config/ (que moram em _MEIPASS) e
    passava a abrir sem modelos e sem os processos pré-carregados, em silêncio.

    Este teste usa uma DLL REAL carregada; mockar o rmtree (como a versão
    anterior fazia) provava só que o mock funcionava."""
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    viva, _handle = _mei_com_dll_carregada(tmp_path, "_MEI_VIVA")
    livre = _criar_mei(tmp_path, "_MEI_LIVRE")

    assert _limpar_temp_orfaos() == 1
    assert not livre.exists()
    assert viva.exists(), "apagou a pasta de uma instância viva"
    assert (viva / "Petições" / "modelo.docx").exists(), "apagou os modelos de petição"
    assert (viva / "data" / "processos_base.sqlite").exists(), "apagou a base embutida"
    assert (viva / "config" / "credenciais.json").exists(), "apagou a config do escritório"


def test_sonda_distingue_pasta_viva_de_orfa(monkeypatch, tmp_path):
    viva, _handle = _mei_com_dll_carregada(tmp_path, "_MEI_VIVA")
    orfa = _criar_mei(tmp_path, "_MEI_ORFA")  # .pyd falso, ninguém carregou
    assert _mei_em_uso(str(viva)) is True
    assert _mei_em_uso(str(orfa)) is False


def test_sonda_ignora_pasta_sem_binarios(tmp_path):
    """Sem binário nenhum não há o que travar: é órfã e pode ir embora."""
    d = tmp_path / "_MEI_SO_DADOS"
    (d / "config").mkdir(parents=True)
    (d / "config" / "credenciais.json").write_text("{}", encoding="utf-8")
    assert _mei_em_uso(str(d)) is False


def test_temp_inacessivel_nao_levanta(monkeypatch, tmp_path):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path / "nao_existe"))
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert _limpar_temp_orfaos() == 0  # sem exceção


def test_temp_vazio(monkeypatch, tmp_path):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert _limpar_temp_orfaos() == 0


# ---------------------------------------------------------------------------
# Alarme: dados do bundle sumiram do _MEIPASS
# ---------------------------------------------------------------------------
# Perder Petições/ ou a base SQLite não gera erro nenhum: a lista de modelos
# aparece vazia, nenhum campo é preenchido e os processos pré-carregados somem.
# O app parece "bugado" sem dizer por quê. Estes testes garantem que ele diz.


@pytest.fixture
def bundle(monkeypatch, tmp_path):
    """Simula o app empacotado com um _MEIPASS completo."""
    import organizador.gui as gui

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    peticoes = tmp_path / "Petições"
    (peticoes / "Alvará").mkdir(parents=True)
    (peticoes / "Alvará" / "modelo.docx").write_bytes(b"docx")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "processos_base.sqlite").write_bytes(b"sqlite")
    (tmp_path / "config").mkdir()
    monkeypatch.setattr(gui, "PASTA_PETICOES", peticoes)
    monkeypatch.setattr(gui, "PROCESSOS_BASE_JSON", tmp_path / "data" / "processos_base.json.gz")
    monkeypatch.setattr(gui, "CONFIG_SEMENTE_DIR", tmp_path / "config")
    return tmp_path


def test_bundle_completo_nao_reclama(bundle):
    assert _dados_do_bundle_faltando() == []


def test_modelos_apagados_sao_denunciados(bundle):
    (bundle / "Petições" / "Alvará" / "modelo.docx").unlink()
    assert any("modelos" in item for item in _dados_do_bundle_faltando())


def test_base_embutida_apagada_e_denunciada(bundle):
    (bundle / "data" / "processos_base.sqlite").unlink()
    assert any("pré-carregados" in item for item in _dados_do_bundle_faltando())


def test_bundle_inteiro_apagado_lista_tudo(bundle):
    import shutil

    shutil.rmtree(bundle / "Petições")
    (bundle / "data" / "processos_base.sqlite").unlink()
    shutil.rmtree(bundle / "config")
    assert len(_dados_do_bundle_faltando()) == 3


def test_modo_fonte_nunca_reclama(monkeypatch):
    """Rodando do código-fonte não existe bundle: a checagem não se aplica."""
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert _dados_do_bundle_faltando() == []
