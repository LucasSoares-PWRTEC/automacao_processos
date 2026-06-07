# -*- coding: utf-8 -*-
"""Extração de partes no eproc 9.20.x (markup novo).

Cobre duas regressões reais do update do eproc (2026-06):
  1. nome da parte em <span class="infraNomeParte"> (antes era <a>);
  2. partes adicionais ("e outros") injetadas como nova <tr> cuja célula
     NÃO tem a classe autorReu — mapeadas pelo índice da coluna.
"""

from __future__ import annotations

from organizador.integracoes.tjsp_scraper.parser_processo import _extrair_partes
from bs4 import BeautifulSoup


# 1 exequente + 2 executados; o 2º executado vem numa <tr> extra com a 1ª
# célula vazia e a 2ª como <td> simples (sem class="autorReu"), como o AJAX
# de carregarPartes() injeta. O nome vem em <span class="infraNomeParte">.
_HTML = """
<table id="tblPartesERepresentantes">
  <tr><th>EXEQUENTE</th><th>EXECUTADO</th></tr>
  <tr>
    <td class="autorReu">
      <span class="infraNomeParte" data-parte="AUTOR">BVEP VILA PARQUE SPE LTDA.</span>
      (<span id="spnCpfParteAutor0">16.993.208/0001-99</span>) <span>- Pessoa Jurídica</span>
      MARCELO CANDIOTTO FREIRE <a>MG104784</a>
    </td>
    <td class="autorReu">
      <span class="infraNomeParte" data-parte="REU">ELAINE RODRIGUES DA SILVA</span>
      (<span id="spnCpfParteReu0">408.961.898-30</span>) <span>- Pessoa Física</span>
      MARCIO CANUTO VIEIRA JUNIOR <a>SP242634</a>
      <span id="carregarOutrosR"></span>
    </td>
  </tr>
  <tr>
    <td></td>
    <td>
      <span class="infraNomeParte" data-parte="REU">GENESIA RODRIGUES DAS VIRGENS</span>
      (<span id="spnCpfParteReu1">153.345.528-71</span>) <span>- Pessoa Física</span>
      MARCIO CANUTO VIEIRA JUNIOR <a>SP242634</a>
    </td>
  </tr>
</table>
"""


def _partes():
    return _extrair_partes(BeautifulSoup(_HTML, "html.parser"))


def test_extrai_nome_em_span_e_multiplas_partes_por_polo():
    partes = _partes()
    papeis = [(p["papel"], p["nome"]) for p in partes]
    assert ("EXEQUENTE", "BVEP VILA PARQUE SPE LTDA.") in papeis
    assert ("EXECUTADO", "ELAINE RODRIGUES DA SILVA") in papeis
    assert ("EXECUTADO", "GENESIA RODRIGUES DAS VIRGENS") in papeis


def test_dois_executados_no_polo_passivo():
    executados = [p for p in _partes() if p["papel"] == "EXECUTADO"]
    assert len(executados) == 2


def test_documento_e_advogado_por_parte():
    por_nome = {p["nome"]: p for p in _partes()}
    genesia = por_nome["GENESIA RODRIGUES DAS VIRGENS"]
    assert genesia["documento"] == "153.345.528-71"
    assert genesia["advogados"] == [{"nome": "MARCIO CANUTO VIEIRA JUNIOR", "oab": "SP242634", "uf": "SP"}]


def test_celula_vazia_nao_vira_parte():
    # A 1ª célula da <tr> extra é vazia (placeholder do polo ativo) — não
    # deve gerar uma parte fantasma sem nome.
    assert all(p["nome"] for p in _partes())
    assert len(_partes()) == 3
