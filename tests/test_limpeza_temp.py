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

import organizador.gui as gui
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


# ---------------------------------------------------------------------------
# Extração incompleta do onefile: o erro que sai TARDE
# ---------------------------------------------------------------------------
# Relatado numa máquina em 27/08/2026, com duas janelas de erro:
#   "ModuleNotFoundError: No module named 'select'"  (no meio de uma busca)
#   "Failed to remove temporary directory: ..._MEI54442"  (do bootloader)
# `select.pyd` ESTÁ no exe publicado — conferido byte a byte no artefato. O que
# faltava era na PASTA EXTRAÍDA. E como o `select` só entra tarde (pelo urllib3,
# na primeira conexão da busca online), a checagem de abertura passava limpa e o
# estouro aparecia longe da causa.

def test_modulos_binarios_tardios_sao_conferidos_na_abertura():
    """Sem isto, uma extração incompleta só aparece no meio de um fluxo."""
    assert "select" in gui._MODULOS_TARDIOS, (
        "o módulo que estourou na máquina do usuário tem de estar na lista"
    )
    # Rodando os testes, o Python está inteiro: a checagem não pode acusar nada.
    assert gui._modulos_binarios_faltando() == []


def test_modulo_ausente_entra_no_relatorio_de_bundle_incompleto(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(gui, "_modulos_binarios_faltando", lambda: ["select"])
    # o resto do bundle presente, para isolar o que este teste mede
    monkeypatch.setattr(gui, "PASTA_PETICOES", Path(__file__).parent)
    monkeypatch.setattr(gui, "caminho_sqlite", lambda _p: Path(__file__))
    monkeypatch.setattr(gui, "CONFIG_SEMENTE_DIR", Path(__file__).parent)

    faltando = gui._dados_do_bundle_faltando()

    assert any("select" in item for item in faltando), faltando


def test_temp_apertado_aponta_disco_cheio_e_orfaos(monkeypatch, tmp_path):
    """As duas causas de extração parcial, e as duas que o usuário pode resolver:
    disco sem espaço e pastas _MEI* de sobra (cada uma pesa o bundle inteiro)."""
    import shutil
    import tempfile

    for nome in ("_MEI111", "_MEI222"):
        (tmp_path / nome).mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_MEIatual"), raising=False)
    monkeypatch.setattr(
        shutil, "disk_usage",
        lambda _p: type("U", (), {"free": 1 * 1024 ** 3, "total": 0, "used": 0})(),
    )

    texto = gui._temp_apertado()

    assert "1.0 GB livres" in texto
    assert "2 pasta(s) _MEI*" in texto


def test_temp_folgado_nao_inventa_causa(monkeypatch, tmp_path):
    import shutil
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        shutil, "disk_usage",
        lambda _p: type("U", (), {"free": 90 * 1024 ** 3, "total": 0, "used": 0})(),
    )

    assert gui._temp_apertado() == ""


# ---------------------------------------------------------------------------
# A pasta de extração saiu do %TEMP%
# ---------------------------------------------------------------------------
# O `select.pyd` sumia da pasta extraída COM O APP ABERTO, e a busca online
# estourava em `ModuleNotFoundError: No module named 'select'` — 27/08/2026 e
# 31/08/2026. Quem apagava era faxina de temporários alheia ao app (o Sensor de
# Armazenamento do Windows apaga o que "os apps não estão usando", ou seja o
# arquivo NÃO TRAVADO). Duas defesas, testadas aqui e abaixo:
#   1. `runtime_tmpdir` fora do %TEMP% (OrganizadorProcessos.spec);
#   2. importar as extensões tardias na abertura, o que TRAVA a imagem delas.


def test_spec_extrai_fora_do_temp():
    """Trava do conserto nº 1: voltar o `runtime_tmpdir` para None (=%TEMP%)
    reabre a falha inteira, e é uma linha fácil de perder num merge."""
    spec = (Path(__file__).resolve().parents[1] / "OrganizadorProcessos.spec").read_text(
        encoding="utf-8"
    )
    assert "runtime_tmpdir=_RUNTIME_TMPDIR" in spec, (
        "o EXE voltou a extrair no %TEMP%, onde limpadores de temporários apagam "
        ".pyd ainda não importado"
    )
    assert "%LOCALAPPDATA%" in spec


def test_raizes_mei_cobre_bundle_e_temp_legado(monkeypatch, tmp_path):
    """As órfãs das versões até a 1.15.0 ficaram no %TEMP%: continuar varrendo
    lá é o que faz alguém recolher esses ~99 MB por pasta."""
    bundle = tmp_path / "bundle"
    temp = tmp_path / "temp"
    (bundle / "_MEIatual").mkdir(parents=True)
    temp.mkdir()
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(temp))
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle / "_MEIatual"), raising=False)

    raizes = [os.path.normcase(r) for r in gui._raizes_mei()]

    assert raizes[0] == os.path.normcase(str(bundle)), "a raiz do bundle vem 1º"
    assert os.path.normcase(str(temp)) in raizes, "parou de varrer o %TEMP% legado"


def test_raizes_mei_nao_repete_quando_coincidem(monkeypatch, tmp_path):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_MEIatual"), raising=False)
    assert len(gui._raizes_mei()) == 1


def test_limpa_orfa_na_raiz_do_bundle_e_no_temp(monkeypatch, tmp_path):
    """Sem isto, as órfãs passariam a acumular em %LOCALAPPDATA% sem ninguém
    olhando — o problema que a limpeza existe para evitar, de mudança."""
    bundle = tmp_path / "bundle"
    temp = tmp_path / "temp"
    bundle.mkdir()
    temp.mkdir()
    atual = _criar_mei(bundle, "_MEI_ATUAL", orfa=False)
    orfa_bundle = _criar_mei(bundle, "_MEI_ORFA_NOVA")
    orfa_temp = _criar_mei(temp, "_MEI_ORFA_LEGADA")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(temp))
    monkeypatch.setattr(sys, "_MEIPASS", str(atual), raising=False)

    assert _limpar_temp_orfaos() == 2
    assert atual.exists(), "apagou a pasta do próprio processo"
    assert not orfa_bundle.exists()
    assert not orfa_temp.exists()


def test_raiz_do_bundle_inexistente_nao_levanta(monkeypatch, tmp_path):
    """1ª abertura da versão nova: a raiz pode não existir ainda."""
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        sys, "_MEIPASS", str(tmp_path / "nao" / "existe" / "_MEIatual"), raising=False
    )
    assert _limpar_temp_orfaos() == 0


# ---------------------------------------------------------------------------
# Conserto nº 2: importar as extensões tardias TRAVA o arquivo
# ---------------------------------------------------------------------------
# `select` entra pelo urllib3 (`urllib3/util/wait.py`: `import select`), e o
# urllib3 só é carregado quando o selenium é — no PRIMEIRO clique em "Buscar
# online". Até ali `select.pyd` nunca foi carregado, e o que não está carregado
# o Windows não protege. Importar na abertura fecha essa janela.


@pytest.fixture(autouse=True)
def _esquecer_memoria_dos_tardios():
    """A resposta é memorizada em produção (é do momento da abertura); nos
    testes cada caso precisa medir de novo."""
    gui._tardios_faltando = None
    yield
    gui._tardios_faltando = None


def test_modulos_tardios_ficam_carregados_depois_da_checagem():
    """O ponto do conserto: não é só CONFERIR, é deixar CARREGADO — imagem
    carregada é imagem que o Windows trava e ninguém apaga.

    Descarrega o `select` antes (extensão C: reimportar é barato e seguro, o
    CPython guarda a inicializada) para que o teste meça o import de verdade e
    não o resíduo de quem importou urllib3 antes."""
    sys.modules.pop("select", None)

    assert gui._modulos_binarios_faltando() == []

    for nome in gui._MODULOS_TARDIOS:
        assert nome in sys.modules, f"{nome} não ficou carregado (logo, apagável)"


def test_select_esta_na_lista():
    """O módulo que estourou na máquina do usuário, duas vezes."""
    assert "select" in gui._MODULOS_TARDIOS


def test_modulo_que_nao_importa_e_denunciado(monkeypatch):
    monkeypatch.setattr(gui, "_MODULOS_TARDIOS", ("select", "modulo_que_nao_existe_xyz"))
    assert gui._modulos_binarios_faltando() == ["modulo_que_nao_existe_xyz"]


def test_resposta_e_memorizada(monkeypatch):
    """A resposta é do momento da abertura: repetir os imports não mudaria nada,
    e o `_avisar_bundle_incompleto` precisa ver o mesmo diagnóstico da abertura."""
    monkeypatch.setattr(gui, "_MODULOS_TARDIOS", ("modulo_que_nao_existe_xyz",))
    primeira = gui._modulos_binarios_faltando()
    assert primeira == ["modulo_que_nao_existe_xyz"]

    # Lista trocada DEPOIS: se houvesse reimport, a resposta mudaria.
    monkeypatch.setattr(gui, "_MODULOS_TARDIOS", ("select",))
    assert gui._modulos_binarios_faltando() is primeira


# ---------------------------------------------------------------------------
# As tarefas de abertura: fora do caminho crítico, e na ordem certa
# ---------------------------------------------------------------------------


class _AppFalso:
    """O mínimo de App que `_tarefas_de_abertura` toca."""

    def __init__(self) -> None:
        self.agendado: list = []
        self.notificacoes: list[tuple] = []

    def after(self, _ms, funcao=None):
        self.agendado.append(funcao)
        return "id"

    def _notificar(self, titulo, texto, nivel="info"):
        self.notificacoes.append((titulo, texto, nivel))


def test_tarefas_de_abertura_trava_antes_de_limpar(monkeypatch):
    """A defesa (travar os .pyd) tem de vir antes da faxina — e as duas antes do
    aviso, que precisa do diagnóstico das duas na mão."""
    ordem: list[str] = []
    monkeypatch.setattr(gui, "_modulos_binarios_faltando",
                        lambda: ordem.append("travar") or [])
    monkeypatch.setattr(gui, "_limpar_temp_orfaos",
                        lambda: ordem.append("limpar") or 0)
    monkeypatch.setattr(gui, "_dados_do_bundle_faltando",
                        lambda: ordem.append("conferir") or [])

    gui._tarefas_de_abertura(_AppFalso())

    assert ordem == ["travar", "limpar", "conferir"]


def test_aviso_de_bundle_incompleto_vai_para_a_main_thread(monkeypatch):
    """Tkinter só é seguro na main thread: o aviso tem de passar por `after`."""
    monkeypatch.setattr(gui, "_modulos_binarios_faltando", lambda: ["select"])
    monkeypatch.setattr(gui, "_limpar_temp_orfaos", lambda: 0)
    monkeypatch.setattr(gui, "_dados_do_bundle_faltando", lambda: ["partes do Python"])
    monkeypatch.setattr(gui, "_atualizacao", lambda: type(
        "A", (), {"registrar": staticmethod(lambda _t: None)})())
    app = _AppFalso()

    gui._tarefas_de_abertura(app)

    assert len(app.agendado) == 1, "avisou fora da main thread"
    app.agendado[0]()          # o que o Tk faria
    assert app.notificacoes and app.notificacoes[0][2] == "error"


def test_tarefas_de_abertura_nunca_levanta(monkeypatch):
    """Nada aqui pode impedir o app de abrir."""
    def explode():
        raise RuntimeError("disco pegando fogo")

    monkeypatch.setattr(gui, "_modulos_binarios_faltando", explode)
    monkeypatch.setattr(gui, "_limpar_temp_orfaos", explode)
    monkeypatch.setattr(gui, "_dados_do_bundle_faltando", explode)

    gui._tarefas_de_abertura(_AppFalso())    # sem exceção
