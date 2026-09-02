# -*- coding: utf-8 -*-
"""Botão "Atualizar processo": consulta o processo NO TRIBUNAL de novo.

É o botão de quando a pessoa olha a tela e vê algo errado. O "Buscar online" não
serve para isso: ele é cache-first — com o processo já na lista, só seleciona a
linha e nem vai à web, a menos que "Forçar busca online" esteja marcado. Aqui a
força é implícita (o sentido do botão É atualizar) e o número vem da LINHA
SELECIONADA, sem mexer no que o usuário digitou na barra — que também é o filtro
da lista, então escrever nela mudaria a tela dele.

Antes de ir à web ele limpa o que a máquina lembra do processo, porque há dois
caches que sobrevivem a uma recarga comum e serviriam dado velho se a busca
falhar (ou se o problema nunca tiver sido do tribunal):

1. ``App._full_cache`` (memória): alimenta o painel de detalhes; só cai quando a
   lista recarrega inteira.
2. ``processos_indice.json`` (disco): alimenta as COLUNAS da lista, e é
   reaproveitado enquanto o ``mtime`` do metadados.json não muda. Um arquivo
   corrigido por fora — copiar por cima PRESERVA o mtime da origem — sobrevive a
   QUALQUER recarga. ``test_resumo_errado_sobrevive_a_recarga`` prova isso, e o
   seguinte prova que ``esquecer_processo`` desfaz.
"""

from __future__ import annotations

import contextlib
import json
import os
import time

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
    copiar um arquivo por cima (copy2/robocopy preservam o mtime da origem)."""
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
    return base, meta, tmp_path / "indice.json", tmp_path / "nao_existe.json.gz"


def _carregar(base, indice, gz, tmp_path):
    return carregar_resumos_pesquisa(base, tmp_path / "vazio", gz, indice)


# ---------------------------------------------------------------------------
# esquecer_processo: a metade "limpa o cache local" do botão
# ---------------------------------------------------------------------------

def test_resumo_errado_sobrevive_a_recarga(cenario, tmp_path):
    """O problema, provado: com o mtime intacto, recarregar a lista NÃO relê o
    arquivo — o resumo velho volta do índice."""
    base, meta, indice, gz = cenario
    assert _carregar(base, indice, gz, tmp_path)[0].classe == "ERRADA"

    _sobrescrever_preservando_mtime(meta, classe="CERTA", valor_causa="R$ 2,00")

    assert _carregar(base, indice, gz, tmp_path)[0].classe == "ERRADA", (
        "o índice deixou de confiar no mtime — este teste documenta o "
        "comportamento que o botão existe para contornar"
    )


def test_esquecer_processo_forca_a_releitura(cenario, tmp_path):
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
    base.mkdir()
    online.mkdir()
    _escrever(base, "1", classe="A")
    _escrever(online, "1", classe="B")
    indice, gz = tmp_path / "i.json", tmp_path / "x.json.gz"
    carregar_resumos_pesquisa(base, online, gz, indice)

    assert esquecer_processo(indice, "1") == 2


def test_nao_toca_nos_outros_processos(tmp_path):
    """Atualizar um processo não pode custar o reparse dos outros 3.000."""
    base = tmp_path / "base"
    base.mkdir()
    for n in ("1", "2", "3"):
        _escrever(base, n, classe=f"C{n}")
    indice, gz = tmp_path / "i.json", tmp_path / "x.json.gz"
    carregar_resumos_pesquisa(base, tmp_path / "vazio", gz, indice)

    esquecer_processo(indice, "2")

    restantes = json.loads(indice.read_text(encoding="utf-8"))["disco"]
    assert sorted(e["resumo"]["numero"] for e in restantes.values()) == ["1", "3"]


def test_numero_inexistente_e_no_op(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
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
    base = tmp_path / "base"
    base.mkdir()
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
# O que interessa aqui é a LIGAÇÃO: o botão pega o número da linha selecionada,
# limpa os caches, vai à web FORÇANDO, e o fim da busca compara e relata. Subir a
# janela custaria segundos por teste e não provaria mais que isto — a janela de
# verdade é exercitada à parte, ao vivo.


class _AppMinima:
    """Só os atributos/métodos que os métodos sob teste tocam."""

    from collections import OrderedDict as _OD

    def __init__(self, numero_selecionado="1"):
        import queue as _q

        self._sel = (numero_selecionado,) if numero_selecionado else ()
        self._full_cache = self._OD()
        self._proc_selecionado = None
        self._comparar_apos_busca = None
        self._pesquisa_log_queue = _q.Queue()
        self._det_ordem = ["numero", "classe", "valor_causa"]
        self._det_rotulos = {
            "numero": "Nº do processo:", "classe": "Classe:",
            "valor_causa": "Valor da causa:",
        }
        self.buscas: list[tuple] = []
        self.avisos: list[tuple] = []
        self.tree = self
        self.selection = lambda: self._sel

    def _notificar(self, titulo, texto, nivel="info"):
        self.avisos.append((titulo, nivel, texto))

    def _buscar_online(self, numero=None, forcar=None):
        self.buscas.append((numero, forcar))

    def update(self):
        pass


def _metodos_da_gui(app):
    from organizador.gui import App

    app._atualizar_processo_selecionado = App._atualizar_processo_selecionado.__get__(app)
    app._instantaneo_processo = App._instantaneo_processo.__get__(app)
    app._relatar_atualizacao = App._relatar_atualizacao.__get__(app)
    return app


def test_botao_vai_ao_tribunal_forcando(monkeypatch):
    """O ponto do botão: consulta a web mesmo com o processo já em cache, que é
    justamente quando o "Buscar online" NÃO iria."""
    import organizador.gui as gui
    from organizador.models import Processo

    app = _metodos_da_gui(_AppMinima("1"))
    app._full_cache["1"] = Processo(numero="1", classe="ERRADA")
    app._proc_selecionado = Processo(numero="1", classe="ERRADA")
    monkeypatch.setattr(gui, "esquecer_processo", lambda _p, _n: 2)

    app._atualizar_processo_selecionado()

    assert app.buscas == [("1", True)], "não foi à web forçando"
    assert "1" not in app._full_cache, "não descartou o cache em memória"
    assert app._comparar_apos_busca[0] == "1"
    assert app._comparar_apos_busca[1]["detalhe · Classe"] == "ERRADA", (
        "não guardou o estado anterior para comparar depois da busca"
    )


def test_numero_vem_da_linha_selecionada(monkeypatch):
    """Sem isto o usuário teria de digitar o número de novo — e a barra de busca
    também é o filtro da lista, então mexer nela mudaria a tela dele."""
    import organizador.gui as gui

    monkeypatch.setattr(gui, "esquecer_processo", lambda _p, _n: 0)
    app = _metodos_da_gui(_AppMinima("0700000-00.2020.8.26.0100"))

    app._atualizar_processo_selecionado()

    assert app.buscas[0][0] == "0700000-00.2020.8.26.0100"


def test_sem_selecao_avisa_e_nao_busca(monkeypatch):
    import organizador.gui as gui

    monkeypatch.setattr(gui, "esquecer_processo", lambda _p, _n: 0)
    app = _metodos_da_gui(_AppMinima(None))

    app._atualizar_processo_selecionado()

    assert app.buscas == []
    assert app.avisos and app.avisos[0][1] == "warning"


def test_relata_o_que_o_tribunal_mudou():
    from organizador.models import Processo

    app = _metodos_da_gui(_AppMinima("1"))
    app._comparar_apos_busca = ("1", {"detalhe · Classe": "ERRADA"})
    app._proc_selecionado = Processo(numero="1", classe="CERTA")

    app._relatar_atualizacao("1")

    titulo, _nivel, texto = app.avisos[-1]
    assert titulo == "Processo atualizado"
    assert "'ERRADA' → 'CERTA'" in texto, texto


def test_sem_mudanca_diz_que_o_tribunal_confirma():
    """Silêncio seria pior: a pessoa clicou porque viu algo errado e precisa
    saber se a consulta trouxe o mesmo dado."""
    from organizador.models import Processo

    app = _metodos_da_gui(_AppMinima("1"))
    app._proc_selecionado = Processo(numero="1", classe="X")
    app._comparar_apos_busca = ("1", {
        "detalhe · Nº do processo": "1",
        "detalhe · Classe": "X",
        "detalhe · Valor da causa": "",
    })

    app._relatar_atualizacao("1")

    assert "nenhum campo mudou" in app.avisos[-1][2]


def test_pedido_e_consumido_uma_vez_so():
    """Uma busca comum depois não pode herdar a comparação do botão."""
    from organizador.models import Processo

    app = _metodos_da_gui(_AppMinima("1"))
    app._proc_selecionado = Processo(numero="1", classe="X")
    app._comparar_apos_busca = ("1", {"detalhe · Classe": "Y"})

    app._relatar_atualizacao("1")
    assert app._comparar_apos_busca is None
    quantos = len(app.avisos)
    app._relatar_atualizacao("1")
    assert len(app.avisos) == quantos, "relatou de novo sem pedido"


def test_busca_de_outro_processo_nao_dispara_o_relato():
    from organizador.models import Processo

    app = _metodos_da_gui(_AppMinima("1"))
    app._proc_selecionado = Processo(numero="2", classe="X")
    app._comparar_apos_busca = ("1", {"detalhe · Classe": "Y"})

    app._relatar_atualizacao("2")

    assert app.avisos == []


def test_instantaneo_ignora_autor_e_reu():
    """`autor`/`reu` não são atributos do Processo (derivam de `partes`) — entrar
    com eles no comparador geraria "mudou" fantasma em toda busca."""
    from organizador.models import Processo

    app = _metodos_da_gui(_AppMinima("1"))
    app._det_ordem = ["numero", "classe", "autor", "reu"]
    app._proc_selecionado = Processo(numero="1", classe="X")

    assert set(app._instantaneo_processo("1")) == {
        "detalhe · Nº do processo", "detalhe · Classe",
    }


def test_instantaneo_cobre_a_linha_da_lista():
    """Cache velho do índice aparece na COLUNA da lista, não no painel de
    detalhes. Sem isto o botão consertaria a coluna e avisaria "nada mudou"."""
    app = _metodos_da_gui(_AppMinima("1"))
    app._proc_selecionado = None

    class _Tree:
        def exists(self, _n):
            return True

        def item(self, _n, _o):
            return ("1", "LIXO DE CACHE")

        def heading(self, col):
            return {"text": {"numero": "Nº", "classe": "Classe"}[col]}

        def __getitem__(self, _k):
            return ("numero", "classe")

        def selection(self):
            return ("1",)

    app.tree = _Tree()

    assert app._instantaneo_processo("1") == {
        "lista · Nº": "1", "lista · Classe": "LIXO DE CACHE",
    }


# ---------------------------------------------------------------------------
# Fim a fim na janela de verdade
# ---------------------------------------------------------------------------
# Um teste com Tkinter, e só um: a ligação entre o botão, a busca e a aplicação
# do resultado passa por `_drain_busca_result`, que toca a árvore, os campos e o
# cache. Fingir tudo isso provaria o fingimento. O tribunal é simulado — o que se
# testa aqui é o app, não o scraper.


@pytest.fixture
def app_isolada(tmp_path, monkeypatch):
    """App real, com APP_DATA e pasta base em tmp_path (nada do usuário é lido
    ou escrito), e o tribunal substituído."""
    tk = pytest.importorskip("tkinter")
    import organizador.gui as gui

    # `organizador.config` resolve PROCESSOS_ONLINE_DIR no IMPORT, e a coleta do
    # pytest já importou o pacote — mexer só em ORGANIZADOR_APP_DATA aqui não
    # redireciona nada. Sem redirecionar os nomes que a gui usa, este teste GRAVA
    # NO CACHE REAL do usuário: aconteceu, e o processo falso foi parar em
    # ~/.automacao_processos/processos_online.
    online = tmp_path / "appdata" / "processos_online"
    online.mkdir(parents=True)
    monkeypatch.setattr(gui, "PROCESSOS_ONLINE_DIR", online)
    monkeypatch.setattr(gui, "PROCESSOS_INDICE_JSON", tmp_path / "appdata" / "indice.json")
    monkeypatch.setenv("ORGANIZADOR_APP_DATA", str(tmp_path / "appdata"))
    base = tmp_path / "base"
    _escrever(
        base, "0000001-11.2020.8.26.0100",
        classe="CLASSE ERRADA", valor_causa="R$ 1,00",
        juiz="DRA. FULANA DE TAL", orgao_julgador="1a Vara Civel",
        partes=[{"nome": "BANCO ALFA", "papel": "EXEQUENTE"}],
    )
    try:
        app = gui.App()
    except tk.TclError as exc:                       # sem display/sessão gráfica
        pytest.skip(f"Tk indisponível: {exc}")
    app.var_pesquisa_base.set(str(base))
    app._recarregar_pesquisa()
    fim = time.time() + 60
    while time.time() < fim and not app.tree.get_children():
        app.update()
        time.sleep(0.05)
    yield gui, app, base
    with contextlib.suppress(Exception):
        app.destroy()


def test_botao_corrige_o_errado_sem_apagar_o_que_o_tribunal_nao_trouxe(app_isolada):
    """O achado que motivou o preenchimento de lacuna no fim da busca.

    Uma consulta traz o que AQUELA página mostra, quase nunca o mesmo conjunto de
    campos da vez anterior. Sem preencher a lacuna, quem clicava para corrigir a
    classe via `juiz` e `orgao_julgador` esvaziarem na tela."""
    gui, app, _base = app_isolada
    from organizador.models import Parte, Processo

    def tribunal(numero, tj, headless, log=None, modo_docs=None, modo_debug=None,
                 reter_scraper=None):
        # corrige classe e valor; NÃO devolve juiz nem órgão julgador
        return Processo(
            numero=numero, classe="Procedimento Comum Cível",
            valor_causa="R$ 60.304,81", tribunal="TJSP",
            partes=[Parte(nome="BANCO ALFA", papel="EXEQUENTE")],
        )

    gui.buscar_processo_online = tribunal
    alvo = app.tree.get_children()[0]
    app.tree.selection_set(alvo)
    app.update()
    assert app._det_vars["classe"].get() == "CLASSE ERRADA"

    avisos = _clicar_e_esperar(app)

    assert app.tree.item(alvo, "values")[1] == "Procedimento Comum Cível", (
        "a coluna da lista não recebeu o dado do tribunal"
    )
    assert app._det_vars["classe"].get() == "Procedimento Comum Cível"
    assert app._det_vars["valor_causa"].get() == "R$ 60.304,81"
    assert app._det_vars["juiz"].get() == "DRA. FULANA DE TAL", (
        "apagou o juiz, que o tribunal não trouxe"
    )
    assert app._det_vars["orgao_julgador"].get() == "1a Vara Civel", (
        "apagou o órgão julgador, que o tribunal não trouxe"
    )
    assert app.tree.selection() and app.tree.selection()[0] == alvo
    assert "'CLASSE ERRADA' → 'Procedimento Comum Cível'" in avisos[-1][2]

    # A marca é o que faz a correção sobreviver a fechar e abrir o app: sem ela
    # o resultado cai em processos_online e perde para a pasta base na mescla.
    salvo = json.loads(
        (gui.PROCESSOS_ONLINE_DIR / "0000001-11.2020.8.26.0100"
         / "metadados.json").read_text(encoding="utf-8")
    )
    assert salvo.get("atualizado_em"), "não marcou o registro como atualizado"

    # 2º cenário na MESMA janela: tribunal fora do ar. Duas App() no mesmo
    # processo quebram o registro de estilos do ttkbootstrap ("Layout
    # warning.Round.Toggle not found"), então não dá para separar em outro teste.
    def tribunal_quebrado(*_a, **_k):
        raise RuntimeError("tribunal fora do ar")

    gui.buscar_processo_online = tribunal_quebrado
    avisos = _clicar_e_esperar(app)

    assert avisos and avisos[-1][1] == "error"
    assert app._comparar_apos_busca is None, (
        "pedido pendurado: a PRÓXIMA busca comum herdaria esta comparação"
    )
    assert app._det_vars["classe"].get() == "Procedimento Comum Cível", (
        "a falha na busca estragou o que já estava na tela"
    )


def _clicar_e_esperar(app):
    """Clica em "Atualizar processo" e espera o aviso do fim da busca."""
    avisos: list = []
    app._notificar = lambda t, x, nivel="info": avisos.append((t, nivel, x))
    app._btn_atualizar_processo.invoke()
    fim = time.time() + 60
    while time.time() < fim and not avisos:
        app.update()
        time.sleep(0.05)
    app.update()
    return avisos
