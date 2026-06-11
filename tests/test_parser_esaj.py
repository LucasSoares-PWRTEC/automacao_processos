# -*- coding: utf-8 -*-
"""Cobertura do parser estruturado do e-SAJ TJSP (cpopg/show.do).

O HTML abaixo reproduz a estrutura real confirmada no processo
1102493-63.2022.8.26.0100: cabeçalho por id, #tableTodasPartes com
abreviações de papel + rótulos "Advogado:"/"Soc. Advogados:", e
#tabelaTodasMovimentacoes (mais recente → mais antigo).
"""

from __future__ import annotations

from organizador.integracoes.tjsp_scraper.parser_esaj import (
    extrair_processo_esaj,
    _canonizar_papel,
    _normaliza_valor,
)
from organizador.organizacao import _dict_para_processo


_HTML = """
<html><body>
  <span id="numeroProcesso">1102493-63.2022.8.26.0100</span>
  <span id="classeProcesso">Execução de Título Extrajudicial</span>
  <span id="assuntoProcesso">Confissão/Composição de Dívida</span>
  <span id="areaProcesso">Cível</span>
  <span id="foroProcesso">Foro Regional VI - Penha de França</span>
  <span id="varaProcesso">1ª Vara Cível</span>
  <span id="juizProcesso">José Luiz de Jesus Vieira</span>
  <span id="valorAcaoProcesso">R$         77.508,14</span>
  <span id="dataHoraDistribuicaoProcesso">15/12/2022 às 09:04 - Livre</span>

  <table id="tableTodasPartes">
    <tr>
      <td class="label"><span class="tipoDeParticipacao">Exeqte&nbsp;</span></td>
      <td class="nomeParteEAdvogado">
        Ns Empreendimento Imobiliário 23 Ltda.
        <br/><span class="mensagemExibindo">Advogado:</span>
        Marcelo Candiotto Freire
        <br/><span class="mensagemExibindo">Soc. Advogados:</span>
        Marcelo Candiotto Freire
      </td>
    </tr>
    <tr>
      <td class="label"><span class="tipoDeParticipacao">Exectdo</span></td>
      <td class="nomeParteEAdvogado">Robson Matos Mascena da Silva</td>
    </tr>
    <tr>
      <td class="label"><span class="tipoDeParticipacao">Exectda</span></td>
      <td class="nomeParteEAdvogado">Danielle Oliveira da Silva Matos</td>
    </tr>
    <tr>
      <td class="label"><span class="tipoDeParticipacao">TerIntCer</span></td>
      <td class="nomeParteEAdvogado">
        Alelo Instituicao de Pagamento Sa
        <br/><span class="mensagemExibindo">Advogado:</span>
        Lucas Menicelli Lagonegro
      </td>
    </tr>
  </table>

  <table id="tabelaTodasMovimentacoes">
    <tr>
      <td class="dataMovimentacao">08/05/2026</td><td></td>
      <td class="descricaoMovimentacao">Conclusos para Despacho</td>
    </tr>
    <tr>
      <td class="dataMovimentacao">07/05/2026</td><td></td>
      <td class="descricaoMovimentacao">Petição Juntada Tipo: Intermediária</td>
    </tr>
    <tr>
      <td class="dataMovimentacao">21/09/2022</td><td></td>
      <td class="descricaoMovimentacao">Distribuído Livre</td>
    </tr>
  </table>
</body></html>
"""


def _proc():
    return _dict_para_processo(
        extrair_processo_esaj(_HTML, numero_cnj="1102493-63.2022.8.26.0100")
    )


# --- cabeçalho ---------------------------------------------------------------
def test_cabecalho_basico():
    p = _proc()
    assert p.numero == "1102493-63.2022.8.26.0100"
    assert p.classe == "Execução de Título Extrajudicial"
    assert p.assunto == "Confissão/Composição de Dívida"
    assert p.competencia == "Cível"
    assert p.juiz == "José Luiz de Jesus Vieira"
    assert p.comarca == "Foro Regional VI - Penha de França"
    assert p.vara == "1ª Vara Cível"
    assert p.orgao_julgador == "1ª Vara Cível — Foro Regional VI - Penha de França"


def test_tribunal_uf_inferidos_do_cnj():
    p = _proc()
    assert p.tribunal == "TJSP"
    assert p.uf == "SP"


def test_valor_normalizado():
    assert _normaliza_valor("R$         77.508,14") == "R$ 77.508,14"
    assert _normaliza_valor("") == ""
    assert _proc().valor_causa == "R$ 77.508,14"


# --- partes ------------------------------------------------------------------
def test_papeis_canonizados():
    assert _canonizar_papel("Exeqte") == "EXEQUENTE"
    assert _canonizar_papel("Exectdo") == "EXECUTADO"
    assert _canonizar_papel("Exectda") == "EXECUTADO"
    assert _canonizar_papel("TerIntCer") == "TERCEIRO INTERESSADO"
    # Desconhecido: volta em caixa-alta, sem quebrar.
    assert _canonizar_papel("Perito") == "PERITO"


def test_extrai_todas_as_partes_inclusive_segundo_polo_passivo():
    partes = {(p.papel, p.nome) for p in _proc().partes}
    assert ("EXEQUENTE", "Ns Empreendimento Imobiliário 23 Ltda.") in partes
    assert ("EXECUTADO", "Robson Matos Mascena da Silva") in partes
    assert ("EXECUTADO", "Danielle Oliveira da Silva Matos") in partes
    assert ("TERCEIRO INTERESSADO", "Alelo Instituicao de Pagamento Sa") in partes


def test_advogado_extraido_e_deduplicado():
    por_nome = {p.nome: p for p in _proc().partes}
    exeqte = por_nome["Ns Empreendimento Imobiliário 23 Ltda."]
    # "Advogado:" e "Soc. Advogados:" repetem o mesmo nome → uma entrada só.
    assert [a.nome for a in exeqte.advogados] == ["Marcelo Candiotto Freire"]
    # e-SAJ público não expõe OAB.
    assert exeqte.advogados[0].oab == ""


def test_parte_sem_advogado_fica_com_lista_vazia():
    por_nome = {p.nome: p for p in _proc().partes}
    assert por_nome["Robson Matos Mascena da Silva"].advogados == []


def test_autor_e_reus_detectados_pelos_papeis_canonicos():
    from organizador.organizacao import _autor, _reus_joined

    p = _proc()
    assert _autor(p) == "Ns Empreendimento Imobiliário 23 Ltda."
    # Mais de um réu: conectivo "e" entre os dois (em vez de ";").
    assert _reus_joined(p) == (
        "Robson Matos Mascena da Silva e Danielle Oliveira da Silva Matos"
    )


# --- movimentações -----------------------------------------------------------
def test_movimentacoes_e_agregados():
    p = _proc()
    assert p.total_eventos == 3
    assert len(p.eventos) == 3
    # Tabela vem do mais recente p/ o antigo.
    assert p.ultima_movimentacao == "08/05/2026 — Conclusos para Despacho"
    assert p.primeira_movimentacao == "21/09/2022"
    assert p.eventos[0].data == "08/05/2026"
    assert p.eventos[0].descricao == "Conclusos para Despacho"


def test_html_vazio_nao_quebra():
    d = extrair_processo_esaj("", numero_cnj="1102493-63.2022.8.26.0100")
    assert d["partes"] == []
    assert d["eventos"] == []
    assert d["numero"] == "1102493-63.2022.8.26.0100"
    assert d["tribunal"] == "TJSP"
