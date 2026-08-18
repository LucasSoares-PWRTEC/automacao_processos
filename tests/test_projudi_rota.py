# -*- coding: utf-8 -*-
"""Cobertura da rota TJGO — Projudi: config, fábrica e roteamento do parser.

O scraper em si (Selenium + OTP) não é exercitado aqui — o que se testa é que a
rota está ligada de ponta a ponta e que o desfecho "não encontrado" do Projudi
vira uma mensagem clara em vez de um registro vazio.
"""

from __future__ import annotations

import pytest

from organizador import scraper as scraper_mod
from organizador.config import (
    ROTAS_INOPERANTES,
    TJ_SISTEMAS,
    _ESTADO_TJ_UF,
    candidatos_tj_para_uf,
    rotulos_tj_selecionaveis,
    sugerir_tj_para_uf,
    uf_efetiva_tj,
)


def test_rota_projudi_esta_configurada():
    site, estado, url, perfil = TJ_SISTEMAS["TJGO — Projudi"]
    assert site == "projudi"
    assert perfil == "GO"
    assert url.startswith("https://projudi.tjgo.jus.br")
    assert _ESTADO_TJ_UF[estado] == ("TJGO", "GO")
    assert uf_efetiva_tj("TJGO — Projudi") == "GO"


def test_go_passa_a_ter_rota_e_o_eproc_morto_sai_da_frente():
    """Antes, GO só tinha "TJGO — eproc", que loga mas nunca acha nada."""
    candidatos = candidatos_tj_para_uf("GO")
    assert candidatos == ["TJGO — Projudi"]
    assert sugerir_tj_para_uf("GO") == "TJGO — Projudi"


def test_rotas_inoperantes_somem_das_sugestoes_mas_seguem_validas():
    """Continuam em TJ_SISTEMAS (seleção salva não pode virar erro), mas nenhum
    caminho automático as escolhe."""
    selecionaveis = rotulos_tj_selecionaveis()
    for rotulo in ROTAS_INOPERANTES:
        assert rotulo in TJ_SISTEMAS
        assert rotulo not in selecionaveis
        uf = uf_efetiva_tj(rotulo)
        assert rotulo not in candidatos_tj_para_uf(uf)


def test_sugestao_de_mg_e_sp_nao_muda():
    """Guarda contra regressão: o filtro de rotas mortas não pode mexer nas UFs
    que já funcionavam (MG prefere eproc na sugestão, PJe no fallback)."""
    assert sugerir_tj_para_uf("SP") == "TJSP — eproc"
    assert TJ_SISTEMAS[sugerir_tj_para_uf("MG")][0] == "eproc"
    assert TJ_SISTEMAS[candidatos_tj_para_uf("MG")[0]][0] == "pje"


def test_fabrica_devolve_o_scraper_do_projudi(monkeypatch):
    from organizador.integracoes.tjsp_scraper.scrapers import (
        ProjudiScraper, criar_scraper,
    )

    # Não sobe navegador nenhum: o driver é o único efeito colateral do __init__.
    monkeypatch.setattr(
        "organizador.integracoes.tjsp_scraper.scrapers.projudi.criar_chrome_driver",
        lambda **kwargs: object(),
    )
    s = criar_scraper(site="projudi", estado="GOIAS",
                      url="https://projudi.tjgo.jus.br/", perfil="GO")
    assert isinstance(s, ProjudiScraper)
    assert s.perfil == "GO"
    # A chave de credencial é derivada do perfil (config/credenciais.json).
    assert s._tj_chave == "TJGO - Projudi"
    assert s.base.endswith("/")


def test_marcador_de_nao_encontrado_do_projudi():
    assert scraper_mod._html_indica_nao_encontrado(
        "<div>Nenhum Processo foi localizado.</div>", "projudi"
    )
    # A página de um processo real não pode disparar o marcador.
    assert not scraper_mod._html_indica_nao_encontrado(
        "<body id='corpo_dados_processo'>Autos</body>", "projudi"
    )


def test_consulta_sem_resultado_vira_erro_explicativo(monkeypatch):
    """O HTML de "não localizado" tem de virar RuntimeError, e não um Processo
    vazio salvo no cache."""
    class _FakeScraper:
        def consultar_processo(self, numero):
            return "<html>Nenhum Processo foi localizado.</html>"

    with pytest.raises(RuntimeError, match="não encontrado"):
        scraper_mod._consultar_e_extrair(
            _FakeScraper(), "projudi", "5238949-70.2018.8.09.0162", "TJGO", "GO",
        )


def test_consulta_ok_roteia_para_o_parser_do_projudi(monkeypatch):
    html = """
    <body id="corpo_dados_processo">
      <span id="span_proc_numero">5238949-70.2018.8.09.0162</span>
      <fieldset><legend>Polo Ativo | Autor</legend><fieldset>
        <div>Nome</div><span class="span1">FULANO LTDA</span>
        <div>CPF/CNPJ</div><span class="span2">13.466.398/0001-06</span>
      </fieldset></fieldset>
      <fieldset><legend>Outras Informações</legend>
        <div>Serventia</div><span>Valparaíso de Goiás - 1ª Vara Cível</span>
        <div>Status</div><span>Ativo</span>
      </fieldset>
    </body>
    """

    class _FakeScraper:
        def consultar_processo(self, numero):
            return html

    monkeypatch.setattr(
        scraper_mod, "extrair_penhora_bloqueio_tribunais", lambda _h: []
    )
    proc = scraper_mod._consultar_e_extrair(
        _FakeScraper(), "projudi", "5238949-70.2018.8.09.0162", "TJGO", "GO",
    )
    assert proc.numero == "5238949-70.2018.8.09.0162"
    assert proc.tribunal == "TJGO" and proc.uf == "GO"
    assert proc.comarca == "Valparaíso de Goiás"
    assert [p.nome for p in proc.partes] == ["FULANO LTDA"]
