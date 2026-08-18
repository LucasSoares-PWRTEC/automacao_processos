# -*- coding: utf-8 -*-
"""Cobertura do parser estruturado do Projudi TJGO.

O HTML abaixo reproduz a estrutura real confirmada no processo
5238949-70.2018.8.09.0162 (sessão autenticada de 18/08/2026), incluindo as duas
armadilhas do Projudi:

  1. os fieldsets de polo são ANINHADOS dentro do fieldset "Dados do Processo",
     que também casa o seletor de fieldset — contar os dois duplicaria as partes;
  2. entre as linhas de movimentação há ``tr#linha_*`` OCULTAS (placeholders do
     carregamento sob demanda dos arquivos) — contá-las dobraria os eventos com
     linhas vazias.
"""

from __future__ import annotations

from organizador.integracoes.tjsp_scraper.parser_projudi import (
    _folha_hierarquia,
    _tipo_por_documento,
    extrair_processo_projudi,
)
from organizador.organizacao import _dict_para_processo


_HTML = """
<html><body id="corpo_dados_processo">
 <fieldset class="VisualizaDados" id="VisualizaDados">
   <legend> Autos </legend>
   <div class="aEsquerda">
     <div> Número </div>
     <span class="bold" id="span_proc_numero"> 5238949-70.2018.8.09.0162</span>
     <div> Área</div> <span class="bold"> Cível</span>
   </div>
 </fieldset>

 <fieldset class="VisualizaDados field_processo" id="VisualizaDados">
   <legend> Dados do Processo </legend>
   <fieldset class="VisualizaDados field_processo" id="VisualizaDados">
     <legend> Polo Ativo | Autor</legend>
     <fieldset class="VisualizaDados">
       <div>Nome</div> <span class="span1 nomes destaque-nome"> OURO BRANCO EMPREENDIMENTOS IMOBILIÁRIOS LTDA</span>
       <div class="divBotoesDireita"><button>x</button></div>
       <div> CPF/CNPJ </div> <span class="span2">13.466.398/0001-06</span>
       <div> Dt. Constituição</div> <span class="span3"></span>
       <div> Filiação </div> <span class="span1 nomes fonte-normal italico"> </span>
       <div class="DivInvisivel" id="sub7099269">
         <fieldset class="fieldsetEndereco"><legend> Endereço </legend>
           Rua Grão Pará nº 466 <br/> SANTA EFIGENIA BELO HORIZONTE MG<br/>
         </fieldset>
       </div>
     </fieldset>
   </fieldset>
   <fieldset class="VisualizaDados field_processo" id="VisualizaDados">
     <legend> Polo Passivo | Réu</legend>
     <fieldset class="VisualizaDados">
       <div>Nome</div> <span class="span1 nomes destaque-nome"> LAIS DE OLIVEIRA SILVA</span>
       <div> CPF/CNPJ </div><span class="span2">610.005.603-84</span>
       <div> Dt. Nascimento</div> <span class="span3"></span>
     </fieldset>
   </fieldset>
 </fieldset>

 <fieldset class="VisualizaDados field_processo" id="VisualizaDados">
   <legend> Outras Informações</legend>
   <div> Serventia </div>
   <span class="span1">Valparaíso de Goiás - UPJ Varas Cíveis: 1ª, 2ª e 4ª</span>
   <div> Classe </div>
   <span>40 - PROCESSO CÍVEL E DO TRABALHO -&gt; Processo de Conhecimento -&gt; Procedimentos Especiais -&gt; Monitória</span>
   <div> Assunto(s) </div>
   <span><table><tbody><tr><td>9587 - DIREITO CIVIL -&gt; Obrigações -&gt; Compra e Venda - Lei nº 10.406/02 (Código Civil) -</td></tr></tbody></table></span>
   <div> Valor da Causa</div> <span class="span1">6.075,36</span>
   <div> Valor Condenação</div> <span class="span2"></span>
   <div> Fase Processual</div> <span class="span1">Conhecimento</span>
   <div> Dt. Distribuição</div> <span class="span3">23/05/2018 00:00:00</span>
   <div> Segredo de Justiça</div> <span class="span2">Não</span>
   <div style="width:200px;">Dt. Trânsito em Julgado</div> <span class="span3"></span>
   <div> Status </div> <span class="span1">Ativo</span>
   <div> Prioridade</div> <span class="span3" id="span_prioridade"> Metas CNJ / Normal</span>
   <div> Efeito Suspensivo</div> <span class="span2">Não</span>
   <div> Custas</div> <span class="span1">Com Custas</span>
   <div> Penhora no Rosto</div> <span class="span2">Não</span>
 </fieldset>

 <table id="TabelaArquivos" class="Tabela"><tbody id="tabListaProcesso">
   <tr><td>Nº</td><td>Movimentação</td><td>Data</td><td>Usuário</td><td>Arquivo(s)</td><td>Opções</td></tr>
   <tr movi_codigo="260" class="filtro-entrada TabelaLinha2 pj-filtro-0">
     <td class="colunaMinima"> 130</td>
     <td class="filtro_coluna_movimentacao"><span class="filtro_tipo_movimentacao">
       Juntada -&gt; Petição</span><br/></td>
     <td align="center">17/08/2026 15:03:41</td>
     <td> \tMARCELO CANDIOTTO FREIRE</td>
     <td class="colunaMinima"></td><td class="colunaMinima"></td>
   </tr>
   <tr id="linha_613397836520594873161306537" carregado="não" style="display: none;">
     <td colspan="6" id="pai_613397836520594873161306537" class="Linha"></td>
   </tr>
   <tr movi_codigo="178" class="filtro-entrada TabelaLinha1 pj-filtro-1">
     <td class="colunaMinima"> 129</td>
     <td class="filtro_coluna_movimentacao"><span class="filtro_tipo_movimentacao">
       Intimação Efetivada</span><br/>
       Disponibilizada no primeiro e publicada no segundo dia útil - Adv(s). de OURO BRANCO</td>
     <td align="center">27/07/2026 14:06:06</td>
     <td> \tSISTEMA CNJ</td>
     <td class="colunaMinima"></td><td class="colunaMinima"></td>
   </tr>
   <tr id="linha_613597856520594873723737073" carregado="não" style="display: none;">
     <td colspan="6" class="Linha"></td>
   </tr>
   <tr movi_codigo="1" class="filtro-entrada TabelaLinha2 pj-filtro-5">
     <td class="colunaMinima"> 1</td>
     <td class="filtro_coluna_movimentacao"><span class="filtro_tipo_movimentacao">
       Peticão Enviada</span><br/></td>
     <td align="center">23/05/2018 11:07:58</td>
     <td> \tSERVIO TULIO DE BARCELOS</td>
     <td class="colunaMinima"></td><td class="colunaMinima"></td>
   </tr>
 </tbody></table>
</body></html>
"""


def _extrair():
    return extrair_processo_projudi(_HTML, numero_cnj="5238949-70.2018.8.09.0162")


def test_cabecalho_vem_dos_rotulos_e_nao_da_posicao():
    d = _extrair()
    assert d["numero"] == "5238949-70.2018.8.09.0162"
    assert d["competencia"] == "Cível"
    assert d["situacao"] == "Ativo"
    assert d["orgao_julgador"] == "Valparaíso de Goiás - UPJ Varas Cíveis: 1ª, 2ª e 4ª"
    assert d["comarca"] == "Valparaíso de Goiás"
    assert d["vara"] == "UPJ Varas Cíveis: 1ª, 2ª e 4ª"
    assert d["valor_causa"] == "R$ 6.075,36"
    # "23/05/2018 00:00:00" -> só a data (o resto dos parsers grava assim)
    assert d["data_autuacao"] == "23/05/2018"
    assert d["tribunal"] == "TJGO" and d["uf"] == "GO"


def test_classe_e_assunto_ficam_na_folha_da_hierarquia():
    d = _extrair()
    assert d["classe"] == "Monitória"
    assert d["assunto"] == "Compra e Venda - Lei nº 10.406/02 (Código Civil)"


def test_partes_nao_duplicam_apesar_do_fieldset_aninhado():
    """"Dados do Processo" ENVOLVE os dois polos e casa o mesmo seletor; se o
    parser não filtrasse pela legend "Polo ...", cada parte viria duas vezes."""
    d = _extrair()
    assert len(d["partes"]) == 2
    autor, reu = d["partes"]
    assert autor["papel"] == "AUTOR"
    assert autor["nome"] == "OURO BRANCO EMPREENDIMENTOS IMOBILIÁRIOS LTDA"
    assert autor["documento"] == "13.466.398/0001-06"
    assert autor["tipo"] == "Pessoa Jurídica"
    assert reu["papel"] == "RÉU"
    assert reu["nome"] == "LAIS DE OLIVEIRA SILVA"
    assert reu["documento"] == "610.005.603-84"
    assert reu["tipo"] == "Pessoa Física"


def test_endereco_do_polo_nao_vira_parte():
    """O <fieldset class="fieldsetEndereco"> mora dentro do bloco da parte."""
    d = _extrair()
    assert all("Rua Grão Pará" not in p["nome"] for p in d["partes"])


def test_eventos_ignoram_as_linhas_placeholder():
    d = _extrair()
    assert d["total_eventos"] == 3  # e não 5 (2 tr#linha_* ocultas no meio)
    assert all(ev["data"] for ev in d["eventos"])
    primeiro = d["eventos"][0]
    assert primeiro["numero"] == "130"
    assert primeiro["data"] == "17/08/2026"
    assert primeiro["hora"] == "15:03:41"
    assert primeiro["descricao"] == "Juntada -> Petição"
    assert primeiro["usuario"] == "MARCELO CANDIOTTO FREIRE"


def test_descricao_junta_tipo_e_complemento():
    d = _extrair()
    segundo = d["eventos"][1]
    assert segundo["descricao"].startswith("Intimação Efetivada — Disponibilizada")


def test_agregados_respeitam_a_ordem_recente_para_antigo():
    d = _extrair()
    assert d["primeira_movimentacao"] == "23/05/2018"
    assert d["ultima_movimentacao"] == {
        "data": "17/08/2026", "descricao": "Juntada -> Petição"
    }


def test_tags_so_saem_quando_afirmativas():
    """Segredo/Efeito Suspensivo/Penhora = "Não" e prioridade default não viram
    tag — senão TODO processo do TJGO nasceria etiquetado."""
    assert _extrair()["tags"] == []


def test_tags_saem_quando_o_processo_realmente_tem():
    html = _HTML.replace(
        '<div> Segredo de Justiça</div> <span class="span2">Não</span>',
        '<div> Segredo de Justiça</div> <span class="span2">Sim</span>',
    ).replace("> Metas CNJ / Normal<", "> Metas CNJ / Idoso<")
    d = extrair_processo_projudi(html, numero_cnj="5238949-70.2018.8.09.0162")
    assert "Segredo de Justiça" in d["tags"]
    assert "Idoso" in d["tags"]


def test_conversao_para_processo():
    proc = _dict_para_processo(_extrair())
    assert proc.numero == "5238949-70.2018.8.09.0162"
    assert proc.ultima_movimentacao == "17/08/2026 — Juntada -> Petição"
    assert [p.papel for p in proc.partes] == ["AUTOR", "RÉU"]
    assert len(proc.eventos) == 3


def test_html_vazio_nao_explode():
    d = extrair_processo_projudi("", numero_cnj="5238949-70.2018.8.09.0162")
    assert d["numero"] == "5238949-70.2018.8.09.0162"  # cai no CNJ informado
    assert d["partes"] == [] and d["eventos"] == []


def test_helpers():
    assert _folha_hierarquia("A -> B -> C") == "C"
    assert _folha_hierarquia("9587 - X -> Compra e Venda -") == "Compra e Venda"
    assert _folha_hierarquia("") == ""
    assert _tipo_por_documento("13.466.398/0001-06") == "Pessoa Jurídica"
    assert _tipo_por_documento("610.005.603-84") == "Pessoa Física"
    assert _tipo_por_documento("") == ""
