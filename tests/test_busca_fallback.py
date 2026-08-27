# -*- coding: utf-8 -*-
"""Fallback entre sistemas na busca da tela: se não está num, tenta os outros.

Antes, `buscar_processo_online` tentava UMA rota e desistia — e como a sugestão
da tela escolhia o sistema só pela UF, errar de sistema virava "não encontrado"
para um processo que existia. O fallback torna o erro de sugestão barato.

O que cada rota extra custa é um navegador e um login com 2FA numa conta
COMPARTILHADA. Por isso o laço para cedo em três situações, e é isso que a
maioria dos testes daqui prova: achou; o pop-up de atribuição apareceu; ou o
número só pode estar na rota que falhou.
"""

from __future__ import annotations

import importlib
import json
import sys
import types

import pytest

# Legado do RJ (sequencial < 800000): só existe na consulta pública.
NUM_LEGADO_RJ = "0024491-10.2021.8.19.0203"
# 08*: pode estar no PJe OU no eproc — nenhuma rota é exclusiva.
NUM_08_RJ = "0800123-45.2025.8.19.0001"

# Blob que o TJRJScraper devolve e o parser_tjrj entende.
_BLOB_TJRJ = json.dumps({
    "resumo": {"tipoProcesso": 1, "classe": "Cumprimento de sentença"},
    "detalhe": {
        "nome": "Regional de Jacarepaguá",
        "descVara": "5ª Vara Cível",
        "sitProc": "A",
        "personagensProcesso": [
            {"nome": "ALTO SANTO EMPREENDIMENTOS LTDA", "descPers": "Autor"},
            {"nome": "RENAN MORAIS LISBOA DA SILVA", "descPers": "Réu"},
        ],
    },
}, ensure_ascii=False)


@pytest.fixture()
def mod(monkeypatch):
    m = importlib.import_module("organizador.scraper")
    monkeypatch.setitem(
        sys.modules, "dotenv",
        types.SimpleNamespace(load_dotenv=lambda path=None: None),
    )
    return m


class _Fake:
    """Scraper de mentira; o comportamento vem do `site` com que foi criado."""

    def __init__(self, site, comportamento, visitados):
        self.site = site
        self.comportamento = comportamento
        self.visitados = visitados

    def login(self):
        self.visitados.append(self.site)
        if self.comportamento.get(self.site) == "login_falha":
            raise RuntimeError("não cheguei ao Painel")

    def consultar_processo(self, numero, modo_docs="nenhum"):
        acao = self.comportamento.get(self.site, "nao_encontrado")
        if acao == "acha":
            return _BLOB_TJRJ
        if acao == "rede":
            raise RuntimeError(
                "consulta pública do TJRJ inacessível: falha de REDE, não "
                "processo inexistente."
            )
        if acao == "popup":
            from organizador.integracoes.tjsp_scraper.scrapers.popup import (
                AtribuicaoDetectadaError,
            )
            raise AtribuicaoDetectadaError("pop-up de atribuição")
        return None  # -> "Scraper não retornou dados para esse número."

    def fechar(self):
        return None


def _instalar(mod, monkeypatch, comportamento):
    """Injeta a fábrica e devolve a lista de sites visitados, em ordem."""
    visitados: list[str] = []
    monkeypatch.setattr(
        mod, "criar_scraper",
        lambda site, estado, url, headless, perfil: _Fake(site, comportamento, visitados),
    )
    return visitados


# ---------------------------------------------------------------------------
# O fallback em si
# ---------------------------------------------------------------------------

def test_tenta_os_outros_sistemas_quando_nao_esta_no_escolhido(mod, monkeypatch):
    """O caso que motivou tudo: 'TJRJ — PJe' selecionado, processo no legado."""
    visitados = _instalar(mod, monkeypatch, {"pje": "nao_encontrado", "tjrj": "acha"})

    logs: list[str] = []
    proc = mod.buscar_processo_online(
        NUM_LEGADO_RJ, "TJRJ — PJe", True, logs.append,
    )

    assert proc.numero == NUM_LEGADO_RJ
    assert [p.nome for p in proc.partes] == [
        "ALTO SANTO EMPREENDIMENTOS LTDA", "RENAN MORAIS LISBOA DA SILVA",
    ]
    assert visitados == ["pje", "tjrj"], "devia ter parado ao achar no 2º"
    assert any("Tentando o próximo sistema" in l for l in logs)
    assert any("Achado em TJRJ — Consulta" in l for l in logs)


def test_para_no_primeiro_que_acha(mod, monkeypatch):
    visitados = _instalar(mod, monkeypatch, {"tjrj": "acha"})

    mod.buscar_processo_online(NUM_LEGADO_RJ, "TJRJ — Consulta", True, lambda s: None)

    assert visitados == ["tjrj"], "não devia abrir navegador para PJe/eproc"


def test_falha_em_todos_lista_o_que_foi_tentado(mod, monkeypatch):
    visitados = _instalar(mod, monkeypatch, {})   # ninguém acha

    with pytest.raises(RuntimeError) as exc:
        mod.buscar_processo_online(NUM_08_RJ, "TJRJ — PJe", True, lambda s: None)

    msg = str(exc.value)
    assert "TJRJ — PJe" in msg and "TJRJ — Consulta" in msg and "TJRJ — eproc" in msg
    assert len(visitados) == 3


def test_login_que_falha_nao_derruba_a_busca(mod, monkeypatch):
    """Uma rota com 2FA quebrado não pode impedir a rota que teria o processo."""
    visitados = _instalar(mod, monkeypatch, {"pje": "login_falha", "tjrj": "acha"})

    proc = mod.buscar_processo_online(
        NUM_LEGADO_RJ, "TJRJ — PJe", True, lambda s: None,
    )

    assert proc.numero == NUM_LEGADO_RJ
    assert visitados == ["pje", "tjrj"]


# ---------------------------------------------------------------------------
# Onde o fallback tem de PARAR
# ---------------------------------------------------------------------------

def test_rota_exclusiva_nao_gasta_login_das_outras(mod, monkeypatch):
    """Legado do RJ + consulta pública fora do ar: PJe e eproc NÃO podem ter
    esse número, então tentá-los só queimaria dois logins com TOTP da conta
    compartilhada. Este é o teste que protege a conta."""
    visitados = _instalar(mod, monkeypatch, {"tjrj": "rede"})

    logs: list[str] = []
    with pytest.raises(RuntimeError, match="REDE"):
        mod.buscar_processo_online(
            NUM_LEGADO_RJ, "TJRJ — Consulta", True, logs.append,
        )

    assert visitados == ["tjrj"]
    assert any("único sistema que pode ter este número" in l for l in logs)


def test_sem_exclusividade_o_fallback_vale(mod, monkeypatch):
    """O outro lado: `08*` pode estar no PJe ou no eproc, então uma falha na
    consulta pública NÃO encerra a busca."""
    visitados = _instalar(mod, monkeypatch, {"tjrj": "rede", "eproc": "acha"})

    mod.buscar_processo_online(NUM_08_RJ, "TJRJ — Consulta", True, lambda s: None)

    assert visitados[0] == "tjrj" and "eproc" in visitados


def test_popup_de_atribuicao_encerra_sem_tentar_outro(mod, monkeypatch):
    """Pop-up de atribuição = o processo FOI achado. Seguir para outro sistema
    contraria a regra do projeto (e repetiria o pop-up)."""
    from organizador.integracoes.tjsp_scraper.scrapers.popup import (
        AtribuicaoDetectadaError,
    )
    visitados = _instalar(mod, monkeypatch, {"pje": "popup", "tjrj": "acha"})

    with pytest.raises(AtribuicaoDetectadaError):
        mod.buscar_processo_online(NUM_LEGADO_RJ, "TJRJ — PJe", True, lambda s: None)

    assert visitados == ["pje"]


def test_debug_nao_faz_fallback(mod, monkeypatch):
    """Em debug se inspeciona UMA rota; pular para outra deixaria o usuário
    olhando um navegador que não é o que ele pediu."""
    visitados = _instalar(mod, monkeypatch, {})

    with pytest.raises(RuntimeError):
        mod.buscar_processo_online(
            NUM_LEGADO_RJ, "TJRJ — PJe", False, lambda s: None,
            modo_debug="pos_consulta",
        )

    assert visitados == ["pje"]


def test_fallback_desligado_explicitamente(mod, monkeypatch):
    visitados = _instalar(mod, monkeypatch, {})

    with pytest.raises(RuntimeError):
        mod.buscar_processo_online(
            NUM_LEGADO_RJ, "TJRJ — PJe", True, lambda s: None, fallback=False,
        )

    assert visitados == ["pje"]


# ---------------------------------------------------------------------------
# Ordem das rotas
# ---------------------------------------------------------------------------

def test_ordem_poe_a_escolhida_na_frente_e_depois_a_mais_barata(mod):
    """A escolhida primeiro (quem digitou escolheu, e a tela já ofereceu a
    troca); as demais na ordem de `candidatos_tj_para_uf`, que no RJ começa pela
    consulta pública — a única sem navegador, senha e TOTP."""
    assert mod._rotas_da_busca("TJRJ — PJe", "RJ") == [
        "TJRJ — PJe", "TJRJ — Consulta", "TJRJ — eproc",
    ]
    assert mod._rotas_da_busca("TJRJ — Consulta", "RJ") == [
        "TJRJ — Consulta", "TJRJ — eproc", "TJRJ — PJe",
    ]


def test_uf_sem_outras_rotas_nao_inventa_fallback(mod):
    assert mod._rotas_da_busca("TJGO — Projudi", "GO") == ["TJGO — Projudi"]
    # UF desconhecida: fica só com a escolhida, sem estourar.
    assert mod._rotas_da_busca("TJSP — eproc", "") == ["TJSP — eproc"]


# ---------------------------------------------------------------------------
# Registro do sistema onde o processo foi achado
# ---------------------------------------------------------------------------

def test_processo_guarda_em_que_sistema_foi_achado(mod, monkeypatch):
    """Essa informação só existia por acaso, no estado de um lote de junho — e
    foi ela que respondeu MG e derrubou a primeira regra de SP. Guardada no
    próprio processo, a pergunta fica respondível de graça, sempre."""
    _instalar(mod, monkeypatch, {"pje": "nao_encontrado", "tjrj": "acha"})

    proc = mod.buscar_processo_online(
        NUM_LEGADO_RJ, "TJRJ — PJe", True, lambda s: None,
    )

    assert "Sistema: TJRJ — Consulta" in proc.tags, (
        "tem de registrar a rota que ACHOU, não a que foi escolhida"
    )


def test_marcar_sistema_substitui_em_vez_de_acumular(mod):
    """Processo migra de sistema — o RJ inteiro está migrando. Duas tags
    `Sistema:` no mesmo processo fariam a antiga mentir sem ninguém notar."""
    from organizador.models import Processo

    p = Processo(numero=NUM_LEGADO_RJ, tags=["Mapeamento: X", "Sistema: TJRJ — PJe"])
    mod.marcar_sistema(p, "TJRJ — Consulta")

    assert [t for t in p.tags if t.startswith("Sistema: ")] == ["Sistema: TJRJ — Consulta"]
    assert "Mapeamento: X" in p.tags, "não pode levar as outras tags junto"


def test_sem_rotulo_nao_inventa_tag(mod):
    from organizador.models import Processo

    p = Processo(numero=NUM_LEGADO_RJ, tags=["Mapeamento: X"])
    mod.marcar_sistema(p, "")

    assert p.tags == ["Mapeamento: X"]


def test_avisa_quando_falta_o_documento_das_partes(mod, monkeypatch):
    """A consulta pública do RJ (como o e-SAJ de SP) não traz CPF/CNPJ. O aviso
    é de graça — ir buscar o documento sozinho custaria um login por consulta."""
    _instalar(mod, monkeypatch, {"tjrj": "acha"})

    logs: list[str] = []
    mod.buscar_processo_online(NUM_LEGADO_RJ, "TJRJ — Consulta", True, logs.append)

    aviso = [l for l in logs if "CPF/CNPJ" in l]
    assert aviso, "devia avisar que as partes vieram sem documento"
    assert "TJRJ — eproc" in aviso[0] or "TJRJ — PJe" in aviso[0], (
        "o aviso tem de dizer QUAL rota traz o documento"
    )
