# -*- coding: utf-8 -*-
"""Botão "Atualizar processo": relê UM processo sem confiar em cache nenhum.

O QUE ELE RESOLVE
-----------------
Entre o metadados.json no disco e o que aparece na tela há dois caches, e os
dois servem dado velho sem reclamar:

1. ``App._full_cache`` (memória): Processo completo por número, só descartado
   quando a lista recarrega inteira.
2. ``processos_indice.json`` (disco): reaproveita o resumo enquanto o ``mtime``
   do arquivo não muda. Um arquivo corrigido por fora — copiar por cima PRESERVA
   o mtime da origem — ou um resumo gravado por versão anterior do parser
   sobrevive a QUALQUER recarga da lista.

O caso 2 é o que não tinha saída pela interface: recarregar não resolvia, porque
a recarga confia no mtime. O teste `test_resumo_errado_sobrevive_a_recarga`
abaixo prova o problema, e o seguinte prova que `esquecer_processo` o desfaz.
"""

from __future__ import annotations

import json
import os

import pytest

from organizador.indice_processos import (
    carregar_resumos_pesquisa,
    esquecer_processo,
)


def _escrever(pasta, numero, **campos):
    alvo = pasta / numero
    alvo.mkdir(parents=True, exist_ok=True)
    meta = alvo / "metadados.json"
    meta.write_text(json.dumps({"numero": numero, **campos}, ensure_ascii=False),
                    encoding="utf-8")
    return meta


def _sobrescrever_preservando_mtime(meta, **campos):
    """Reescreve o arquivo e devolve o mtime ORIGINAL — é o que acontece ao
    copiar um arquivo por cima (o copy2/robocopy preserva o mtime da origem)."""
    antes = os.stat(meta).st_mtime
    d = json.loads(meta.read_text(encoding="utf-8"))
    d.update(campos)
    meta.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    os.utime(meta, (antes, antes))
    return antes


@pytest.fixture
def cenario(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    meta = _escrever(base, "1", classe="ERRADA", valor_causa="R$ 1,00")
    indice = tmp_path / "indice.json"
    gz = tmp_path / "nao_existe.json.gz"
    return base, meta, indice, gz


def _carregar(base, indice, gz, tmp_path):
    return carregar_resumos_pesquisa(base, tmp_path / "vazio", gz, indice)


def test_resumo_errado_sobrevive_a_recarga(cenario, tmp_path):
    """O problema, provado: com o mtime intacto, recarregar a lista NÃO relê o
    arquivo — o resumo velho volta do índice."""
    base, meta, indice, gz = cenario
    primeiro = _carregar(base, indice, gz, tmp_path)
    assert primeiro[0].classe == "ERRADA"

    _sobrescrever_preservando_mtime(meta, classe="CERTA", valor_causa="R$ 2,00")

    segundo = _carregar(base, indice, gz, tmp_path)
    assert segundo[0].classe == "ERRADA", (
        "o índice deixou de confiar no mtime — este teste documenta o "
        "comportamento que o botão existe para contornar"
    )


def test_esquecer_processo_forca_a_releitura(cenario, tmp_path):
    """A saída do botão: esquecida a entrada, a carga seguinte reparseia."""
    base, meta, indice, gz = cenario
    _carregar(base, indice, gz, tmp_path)
    _sobrescrever_preservando_mtime(meta, classe="CERTA", valor_causa="R$ 2,00")

    assert esquecer_processo(indice, "1") == 1

    depois = _carregar(base, indice, gz, tmp_path)
    assert depois[0].classe == "CERTA"
    assert depois[0].valor_causa == "R$ 2,00"


def test_esquece_todas_as_fontes_do_mesmo_numero(tmp_path):
    """O mesmo número pode estar na pasta base E no cache online: esquecer só
    uma das entradas deixaria a outra servir resumo velho."""
    base, online = tmp_path / "base", tmp_path / "online"
    base.mkdir(); online.mkdir()
    _escrever(base, "1", classe="A")
    _escrever(online, "1", classe="B")
    indice, gz = tmp_path / "i.json", tmp_path / "x.json.gz"
    carregar_resumos_pesquisa(base, online, gz, indice)

    assert esquecer_processo(indice, "1") == 2


def test_nao_toca_nos_outros_processos(tmp_path):
    """Atualizar um processo não pode custar o reparse dos outros 3.000."""
    base = tmp_path / "base"; base.mkdir()
    for n in ("1", "2", "3"):
        _escrever(base, n, classe=f"C{n}")
    indice, gz = tmp_path / "i.json", tmp_path / "x.json.gz"
    carregar_resumos_pesquisa(base, tmp_path / "vazio", gz, indice)

    esquecer_processo(indice, "2")

    restantes = json.loads(indice.read_text(encoding="utf-8"))["disco"]
    numeros = sorted(e["resumo"]["numero"] for e in restantes.values())
    assert numeros == ["1", "3"], "esqueceu processo que ninguém pediu"


def test_numero_inexistente_e_no_op(tmp_path):
    base = tmp_path / "base"; base.mkdir()
    _escrever(base, "1", classe="A")
    indice, gz = tmp_path / "i.json", tmp_path / "x.json.gz"
    carregar_resumos_pesquisa(base, tmp_path / "vazio", gz, indice)
    assert esquecer_processo(indice, "9999") == 0
    assert esquecer_processo(indice, "") == 0
    assert esquecer_processo(indice, None) == 0


def test_indice_ausente_ou_ilegivel_nao_levanta(tmp_path):
    """É ação de botão: falhar aqui só significa que o resumo velho fica."""
    assert esquecer_processo(tmp_path / "nunca_existiu.json", "1") == 0
    ruim = tmp_path / "ruim.json"
    ruim.write_text("{ isto não é json", encoding="utf-8")
    assert esquecer_processo(ruim, "1") == 0


def test_derruba_o_bloco_do_snapshot_embutido(tmp_path):
    """Os resumos do .json.gz (fallback do fonte, sem SQLite) são um bloco só,
    sem chave por processo: o jeito de não servir o velho é derrubar o bloco."""
    base = tmp_path / "base"; base.mkdir()
    _escrever(base, "1", classe="A")
    indice, gz = tmp_path / "i.json", tmp_path / "x.json.gz"
    carregar_resumos_pesquisa(base, tmp_path / "vazio", gz, indice)

    dados = json.loads(indice.read_text(encoding="utf-8"))
    dados["embutidos"] = [{"numero": "1", "classe": "VELHA"}]
    dados["gz_mtime"] = 123.0
    indice.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")

    esquecer_processo(indice, "1")

    depois = json.loads(indice.read_text(encoding="utf-8"))
    assert depois["embutidos"] == []
    assert depois["gz_mtime"] is None


# ---------------------------------------------------------------------------
# A parte da GUI, sem subir Tkinter
# ---------------------------------------------------------------------------
# O que interessa testar aqui é a LIGAÇÃO: o botão descarta o cache em memória,
# esquece o índice, pede a recarga, e o fim da recarga (assíncrona) reseleciona o
# processo. Subir a janela de verdade custaria segundos por teste e não provaria
# mais que isto.

class _AppMinima:
    """Só os atributos/métodos que `_atualizar_processo_selecionado` toca."""

    from collections import OrderedDict as _OD

    def __init__(self, numero_selecionado="1"):
        import queue as _q
        self._sel = (numero_selecionado,) if numero_selecionado else ()
        self._full_cache = self._OD()
        self._proc_selecionado = None
        self._pos_carga_selecionar = None
        self._pos_carga_campos_antes = {}
        self._pesquisa_log_queue = _q.Queue()
        self._det_ordem = ["numero", "classe", "valor_causa"]
        self._det_rotulos = {"classe": "Classe:", "valor_causa": "Valor da causa:"}
        self.cargas = 0
        self.avisos = []
        self.tree = self
        # `tree.selection()` é chamado no objeto self.tree
        self.selection = lambda: self._sel

    def _notificar(self, titulo, texto, nivel="info"):
        self.avisos.append((titulo, nivel))

    def _iniciar_carregamento_pesquisa(self, silencioso=False):
        self.cargas += 1


def _metodos_da_gui(app):
    """Amarra os métodos reais da App ao objeto mínimo."""
    from organizador.gui import App
    app._atualizar_processo_selecionado = App._atualizar_processo_selecionado.__get__(app)
    app._instantaneo_processo = App._instantaneo_processo.__get__(app)
    return app


def test_botao_descarta_cache_esquece_indice_e_pede_recarga(monkeypatch, tmp_path):
    import organizador.gui as gui
    from organizador.models import Processo

    app = _metodos_da_gui(_AppMinima("1"))
    app._full_cache["1"] = Processo(numero="1", classe="ERRADA")
    # o que está na tela quando o botão é clicado (posto por _on_select_processo)
    app._proc_selecionado = Processo(numero="1", classe="ERRADA")
    esquecidos = []
    monkeypatch.setattr(gui, "esquecer_processo",
                        lambda _p, n: esquecidos.append(n) or 2)

    app._atualizar_processo_selecionado()

    assert "1" not in app._full_cache, "não descartou o cache em memória"
    assert esquecidos == ["1"], "não esqueceu o índice em disco"
    assert app.cargas == 1, "não pediu a recarga"
    assert app._pos_carga_selecionar == "1", "não agendou a reseleção"
    assert app._pos_carga_campos_antes.get("detalhe · Classe") == "ERRADA", (
        "não guardou o estado anterior para dizer o que mudou"
    )


def test_sem_selecao_avisa_e_nao_recarrega(monkeypatch):
    import organizador.gui as gui
    monkeypatch.setattr(gui, "esquecer_processo", lambda _p, _n: 0)
    app = _metodos_da_gui(_AppMinima(None))

    app._atualizar_processo_selecionado()

    assert app.cargas == 0
    assert app.avisos and app.avisos[0][1] == "warning"


def test_instantaneo_ignora_autor_e_reu():
    """`autor`/`reu` não são atributos do Processo (derivam de `partes`) — entrar
    com eles no comparador geraria "mudou" fantasma em todo clique."""
    from organizador.models import Processo

    app = _metodos_da_gui(_AppMinima("1"))
    app._det_ordem = ["numero", "classe", "autor", "reu"]
    app._det_rotulos = {"numero": "Nº do processo:", "classe": "Classe:"}
    app._proc_selecionado = Processo(numero="1", classe="X")

    campos = app._instantaneo_processo("1")

    assert set(campos) == {"detalhe · Nº do processo", "detalhe · Classe"}
    assert campos["detalhe · Classe"] == "X"


def test_instantaneo_cobre_a_linha_da_lista():
    """O caso que o 1º teste ao vivo pegou: cache velho aparece na COLUNA da
    lista, não no painel de detalhes. Sem isto o botão consertava a coluna e
    avisava "nenhum campo mudou"."""
    app = _metodos_da_gui(_AppMinima("1"))
    app._proc_selecionado = None

    class _Tree:
        def __init__(self):
            self._cols = ("numero", "classe")
        def exists(self, _n): return True
        def item(self, _n, _o): return ("1", "LIXO DE CACHE")
        def heading(self, col): return {"text": {"numero": "Nº", "classe": "Classe"}[col]}
        def __getitem__(self, k): return self._cols
        def selection(self): return ("1",)

    app.tree = _Tree()
    campos = app._instantaneo_processo("1")
    assert campos == {"lista · Nº": "1", "lista · Classe": "LIXO DE CACHE"}
