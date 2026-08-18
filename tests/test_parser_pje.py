# -*- coding: utf-8 -*-
"""Cobertura do parser estruturado do PJe (TJMG 1º grau).

O blob abaixo reproduz a estrutura real confirmada no processo
5000432-19.2021.8.13.0079: timeline em texto (newest-first, com cabeçalhos
de data "DD mmm AAAA", descrições, linhas de documento "ID - Tipo" e horários
HH:MM) seguida dos corpos dos documentos abertos, cada um precedido pelo
marcador <!-- DOCUMENTO_ORIGEM: ... -->.
"""

from __future__ import annotations

from organizador.integracoes.tjsp_scraper.parser_pje import (
    extrair_processo_pje,
    _canonizar_papel,
    _converter_data,
    _formatar_detalhe_pje,
    _tipo_documento,
)
from organizador.organizacao import _dict_para_processo


_BLOB = """22 jan 2021
18 fev 2026
JUNTADA DE PETIÇÃO DE PETIÇÃO
10629074581 - Petição
10629074582 - Petição (manifestacao enderecos)
16:32
09 fev 2026
EXPEDIÇÃO DE COMUNICAÇÃO VIA SISTEMA.
10623477594 - Intimação
08:59
31 mai 2022
10578543024 - Sentença
14:06
11 jan 2021
DISTRIBUÍDO POR SORTEIO
16:54
<!-- DOCUMENTO_ORIGEM: 10623476141 - Juntada (Pesquisa Sisbajud) | EVENTO:  | DATA: 09 fev 2026 | ID: 10623476141 -->
PODER JUDICIÁRIO DO ESTADO DE MINAS GERAIS
Comarca de Contagem / 4ª Vara Cível da Comarca de Contagem
Avenida Maria da Glória Rocha, 425, Centro, Contagem - MG - CEP: 32010-375
PROCESSO Nº 5000432-19.2021.8.13.0079
[CÍVEL] CUMPRIMENTO DE SENTENÇA (156)
REQUERENTE: ANAPU EMPREENDIMENTOS IMOBILIÁRIOS LTDA CPF: 10.923.840/0001-80
REQUERIDO(A): LINDIOMAR NASCIMENTO DE JESUS CPF: 085.118.676-94
REQUERIDO(A): CARMEN DA SILVA SOUZA CPF: 114.120.726-54
<!-- DOCUMENTO_ORIGEM: 10578543024 - Sentença | EVENTO:  | DATA: 31 mai 2022 | ID: 10578543024 -->
PROCESSO Nº: 5000432-19.2021.8.13.0079
CLASSE: [CÍVEL] CUMPRIMENTO DE SENTENÇA (156)
ASSUNTO: [Compra e Venda]
AUTOR: ANAPU EMPREENDIMENTOS IMOBILIÁRIOS LTDA CPF: 10.923.840/0001-80
RÉU: LINDIOMAR NASCIMENTO DE JESUS CPF: 085.118.676-94 e outros
Homologo o acordo celebrado pelas partes, suspendendo o feito.
"""


def _proc():
    return _dict_para_processo(
        extrair_processo_pje(_BLOB, numero_cnj="5000432-19.2021.8.13.0079")
    )


# --- helpers -----------------------------------------------------------------
def test_converter_data():
    assert _converter_data("18 fev 2026") == "18/02/2026"
    assert _converter_data("9 jan 2021") == "09/01/2021"
    assert _converter_data("lixo") == ""


def test_tipo_documento_por_mascara():
    assert _tipo_documento("10.923.840/0001-80") == "Pessoa Jurídica"
    assert _tipo_documento("085.118.676-94") == "Pessoa Física"
    assert _tipo_documento("") == ""


def test_papeis_canonizados():
    assert _canonizar_papel("REQUERIDO(A)") == "REQUERIDO"
    assert _canonizar_papel("RÉU") == "RÉU"
    assert _canonizar_papel("Exequente") == "EXEQUENTE"


# --- cabeçalho ---------------------------------------------------------------
def test_cabecalho_basico():
    p = _proc()
    assert p.numero == "5000432-19.2021.8.13.0079"
    assert p.classe == "[CÍVEL] CUMPRIMENTO DE SENTENÇA (156)"
    assert p.assunto == "Compra e Venda"
    assert p.comarca == "Contagem"
    assert p.vara == "4ª Vara Cível da Comarca de Contagem"
    assert p.orgao_julgador == "4ª Vara Cível da Comarca de Contagem — Contagem"
    assert "CEP: 32010-375" in p.foro_endereco


def test_tribunal_uf_inferidos_do_cnj():
    p = _proc()
    assert p.tribunal == "TJMG"
    assert p.uf == "MG"


# --- partes ------------------------------------------------------------------
def test_partes_com_documento_e_tipo_dedupe_por_nome():
    partes = {(p.papel, p.nome, p.documento, p.tipo) for p in _proc().partes}
    assert ("REQUERENTE", "ANAPU EMPREENDIMENTOS IMOBILIÁRIOS LTDA",
            "10.923.840/0001-80", "Pessoa Jurídica") in partes
    assert ("REQUERIDO", "LINDIOMAR NASCIMENTO DE JESUS",
            "085.118.676-94", "Pessoa Física") in partes
    assert ("REQUERIDO", "CARMEN DA SILVA SOUZA",
            "114.120.726-54", "Pessoa Física") in partes
    # ANAPU aparece como REQUERENTE (juntada) e AUTOR (sentença) → 1 entrada.
    nomes = [p.nome for p in _proc().partes]
    assert nomes.count("ANAPU EMPREENDIMENTOS IMOBILIÁRIOS LTDA") == 1
    # "e outros" não vaza para o nome do réu.
    assert all(not n.endswith("e outros") for n in nomes)


# --- painel "Mais detalhes" (poloAtivo/poloPassivo) --------------------------
def _partes_do_painel(html: str):
    """Roda o painel pelo mesmo caminho do scraper: formata o bloco e extrai."""
    return _dict_para_processo(extrair_processo_pje(_formatar_detalhe_pje(html))).partes


def test_painel_reu_com_barra_de_genero_e_sem_cpf():
    """TJMG rotula o réu como 'RÉU/RÉ' e às vezes sem CPF (sigilo). Antes ambos
    eram descartados, deixando o processo só com o autor."""
    html = """
    <div id="maisDetalhes"><dl>
      <dt>Classe judicial</dt><dd>[CÍVEL] PROCEDIMENTO COMUM CÍVEL (7)</dd>
      <dt>Órgão julgador</dt><dd>1ª Vara Cível da Comarca de Vespasiano</dd>
    </dl></div>
    <div id="poloAtivo"><table><tbody>
      <tr><td>VILLE PARK MADRI SPE LTDA - CNPJ: 24.997.166/0001-92 (AUTOR)</td></tr>
    </tbody></table></div>
    <div id="poloPassivo"><table><tbody>
      <tr><td>HIGOR HENRIQUE DE SOUZA - CPF: 139.818.146-30 (RÉU/RÉ)</td></tr>
      <tr><td>MARIA SEM DOCUMENTO (REQUERIDO(A))</td></tr>
    </tbody></table></div>
    """
    partes = _partes_do_painel(html)
    papeis = {(p.papel, p.nome) for p in partes}
    assert ("AUTOR", "VILLE PARK MADRI SPE LTDA") in papeis
    assert ("RÉU", "HIGOR HENRIQUE DE SOUZA") in papeis           # 'RÉU/RÉ' normalizado
    # réu sem CPF também entra, com documento vazio
    maria = [p for p in partes if p.nome == "MARIA SEM DOCUMENTO"]
    assert maria and maria[0].papel == "REQUERIDO" and maria[0].documento == ""


def test_painel_parte_nos_dois_polos_mantem_os_dois_papeis():
    """Alguns processos listam a MESMA parte no polo ativo E no passivo; manter
    cada polo evita perder o réu (dedupe por polo, não pelo nome cru)."""
    html = """
    <div id="maisDetalhes"><dl><dt>Órgão julgador</dt><dd>1ª Vara</dd></dl></div>
    <div id="poloAtivo"><table><tbody>
      <tr><td>MASTER TOWER SPE LTDA - CNPJ: 24.555.886/0001-06 (AUTOR)</td></tr>
      <tr><td>MARCOS FELIPE - CPF: 138.561.166-99 (AUTOR)</td></tr>
    </tbody></table></div>
    <div id="poloPassivo"><table><tbody>
      <tr><td>MARCOS FELIPE - CPF: 138.561.166-99 (RÉU/RÉ)</td></tr>
      <tr><td>MASTER TOWER SPE LTDA - CNPJ: 24.555.886/0001-06 (RÉU/RÉ)</td></tr>
    </tbody></table></div>
    """
    papeis = {(p.papel, p.nome) for p in _partes_do_painel(html)}
    assert ("AUTOR", "MARCOS FELIPE") in papeis
    assert ("RÉU", "MARCOS FELIPE") in papeis  # mesmo nome nos dois polos → 2 entradas


# --- eventos -----------------------------------------------------------------
def test_eventos_e_agregados():
    p = _proc()
    # 4 entradas na timeline (cada uma fechada por um HH:MM).
    assert p.total_eventos == 4
    # newest-first: 1º é o mais recente; autuação = data mais antiga vista.
    assert p.eventos[0].data == "18/02/2026"
    assert p.eventos[0].descricao == "JUNTADA DE PETIÇÃO DE PETIÇÃO"
    assert p.eventos[0].documentos == [
        "Petição", "Petição (manifestacao enderecos)"
    ]
    assert p.data_autuacao == "11/01/2021"
    assert p.primeira_movimentacao == "11/01/2021"
    assert p.ultima_movimentacao.startswith("18/02/2026 —")


# --- atos --------------------------------------------------------------------
def test_atos_com_teor_e_data():
    atos = _proc().atos
    assert len(atos) == 1
    sentenca = atos[0]
    assert sentenca.tipo == "SENTENÇA"
    assert sentenca.data_assinatura == "31/05/2022"
    assert "Homologo o acordo" in sentenca.teor
    assert sentenca.arquivo == "10578543024 - Sentença"


# --- robustez ----------------------------------------------------------------
def test_blob_vazio_nao_quebra():
    d = extrair_processo_pje("", numero_cnj="5000432-19.2021.8.13.0079")
    assert d["partes"] == []
    assert d["eventos"] == []
    assert d["atos"] == []
    assert d["numero"] == "5000432-19.2021.8.13.0079"
    assert d["tribunal"] == "TJMG"


# --------------------------------------------------------------------------
# Parada antecipada da coleta de documentos (PJe): _cabecalho_suficiente
# --------------------------------------------------------------------------

def test_pje_cabecalho_suficiente_para_na_classe():
    from organizador.integracoes.tjsp_scraper.scrapers.pje import _cabecalho_suficiente

    # Só timeline (movimentações), sem "CLASSE:" — segue abrindo documentos.
    assert _cabecalho_suficiente("06 out 2025\nJUNTADA DE CERTIDÃO\nDESPACHO") is False
    assert _cabecalho_suficiente("") is False
    # Documento com o timbre/cabeçalho (linha CLASSE) — pode parar de abrir.
    assert _cabecalho_suficiente("CLASSE: Execução de Título Extrajudicial") is True
    doc = (
        "CLASSE: Execução de Título Extrajudicial\n"
        "Comarca de Fortaleza / 6ª Vara Cível\n"
        "REQUERENTE: Banco XYZ S/A CNPJ: 00.000.000/0001-00"
    )
    assert _cabecalho_suficiente(doc) is True


# --------------------------------------------------------------------------
# Painel "Mais detalhes" do PJe → bloco do parser (sem abrir documentos)
# --------------------------------------------------------------------------

_HTML_DETALHE_PJE = """
<div id="maisDetalhes">
  <dl class="dl-horizontal">
    <dt>Classe judicial</dt><dd>EXECUÇÃO DE TÍTULO EXTRAJUDICIAL (12154)</dd>
    <dt>Assunto</dt><dd><ul><li>Pagamento (7703)</li></ul></dd>
    <dt>Jurisdição</dt><dd>Comarca de Caucaia</dd>
    <dt>Valor da causa</dt><dd>R$ 14.550,42</dd>
  </dl>
  <div><dl class="dl-horizontal">
    <dt>Órgão julgador</dt><dd>1ª Vara Cível da Comarca de Caucaia</dd>
  </dl></div>
</div>
<div id="poloAtivo"><table><tbody>
  <tr><td><span><span>BELLA TRINDADE EMPREENDIMENTOS IMOBILIARIOS LTDA. - CNPJ: 24.518.456/0001-06 (EXEQUENTE)</span></span>
    <ul class="tree"><li>MARCELO CANDIOTTO FREIRE - OAB MG104784 - CPF: 030.152.576-50 (ADVOGADO)</li></ul>
  </td></tr>
</tbody></table></div>
<div id="poloPassivo"><table><tbody>
  <tr><td><span><span>JOAO CARLOS DE SOUSA - CPF: 389.595.263-04 (EXECUTADO)</span></span>
    <ul class="tree"><li>SHIRLEY CHAVES BRAGA BEZERRA - OAB CE53768 - CPF: 548.604.743-68 (ADVOGADO)</li></ul>
  </td></tr>
  <tr><td><span><span>CARLOS LEVI SILVA DE SOUSA - CPF: 080.162.783-45 (EXECUTADO)</span></span></td></tr>
</tbody></table></div>
"""


def test_formatar_detalhe_pje_monta_bloco():
    from organizador.integracoes.tjsp_scraper.parser_pje import _formatar_detalhe_pje

    bloco = _formatar_detalhe_pje(_HTML_DETALHE_PJE)
    assert "CLASSE: EXECUÇÃO DE TÍTULO EXTRAJUDICIAL" in bloco  # sem o (12154)
    assert "ASSUNTO: Pagamento" in bloco
    assert "Comarca de Caucaia / 1ª Vara Cível da Comarca de Caucaia" in bloco
    assert "valor da causa R$ 14.550,42" in bloco
    assert "EXEQUENTE: BELLA TRINDADE EMPREENDIMENTOS IMOBILIARIOS LTDA. CNPJ: 24.518.456/0001-06" in bloco
    assert "EXECUTADO: JOAO CARLOS DE SOUSA CPF: 389.595.263-04" in bloco
    assert "EXECUTADO: CARLOS LEVI SILVA DE SOUSA CPF: 080.162.783-45" in bloco
    # Advogados (sub-árvore .tree) NÃO viram partes.
    assert "ADVOGADO" not in bloco
    # Sem o painel → vazio.
    assert _formatar_detalhe_pje("<div>nada</div>") == ""


def test_detalhe_pje_extrai_processo_completo():
    from organizador.integracoes.tjsp_scraper.parser_pje import (
        _formatar_detalhe_pje, extrair_processo_pje,
    )
    blob = "JUNTADA DE CERTIDÃO\n27 abr 2026\n\n" + _formatar_detalhe_pje(_HTML_DETALHE_PJE)
    d = extrair_processo_pje(blob, numero_cnj="0008537-81.2019.8.06.0064")
    assert d["classe"] == "EXECUÇÃO DE TÍTULO EXTRAJUDICIAL"
    assert d["assunto"] == "Pagamento"
    assert d["comarca"] == "Caucaia"
    assert d["vara"] == "1ª Vara Cível da Comarca de Caucaia"
    assert d["valor_causa"] == "R$ 14.550,42"
    assert d["tribunal"] == "TJCE"
    papeis = {(p["papel"], p["nome"]) for p in d["partes"]}
    assert ("EXEQUENTE", "BELLA TRINDADE EMPREENDIMENTOS IMOBILIARIOS LTDA.") in papeis
    assert ("EXECUTADO", "JOAO CARLOS DE SOUSA") in papeis
    assert ("EXECUTADO", "CARLOS LEVI SILVA DE SOUSA") in papeis


_HTML_DETALHE_PJE_MG = """
<div id="maisDetalhes"><dl class="dl-horizontal">
<dt>Classe judicial</dt><dd>[CÍVEL] EXECUÇÃO DE TÍTULO EXTRAJUDICIAL (12154)</dd>
<dt>Assunto</dt><dd><ul><li>Compra e Venda (9587)</li></ul></dd>
<dt>Jurisdição</dt><dd>Contagem</dd>
<dt>Valor da causa</dt><dd>R$ 4.618,20</dd>
</dl><div><dl class="dl-horizontal"><dt>Órgão julgador</dt><dd>4ª Vara Cível da Comarca de Contagem</dd></dl></div></div>
<div id="poloAtivo"><table><tbody><tr><td><span><span>DIRECIONAL ENGENHARIA S.A - CNPJ: 16.614.075/0001-00 (EXEQUENTE)</span></span>
<ul class="tree"><li>MARCELO CANDIOTTO FREIRE - OAB MG104784 - CPF: 030.152.576-50 (ADVOGADO)</li></ul>
<ul class="tree"><li>Procuradoria - Direcional Engenharia</li></ul></td></tr></tbody></table></div>
<div id="poloPassivo"><table><tbody><tr><td><span><span>LUIZ ALBERTO MARAZZI - CPF: 745.590.106-25 (EXECUTADO(A))</span></span>
<ul class="tree"><li>LUIZ FERNANDO OLIVEIRA DE MORAIS - OAB MG229264 - CPF: 068.155.666-84 (ADVOGADO)</li></ul></td></tr></tbody></table></div>
"""


def test_detalhe_pje_tjmg_classe_civel_e_papel_com_parenteses():
    from organizador.integracoes.tjsp_scraper.parser_pje import (
        _formatar_detalhe_pje, extrair_processo_pje,
    )
    blob = "MOV\n01 jun 2026\n\n" + _formatar_detalhe_pje(_HTML_DETALHE_PJE_MG)
    d = extrair_processo_pje(blob, numero_cnj="5014752-74.2021.8.13.0079")
    # "[CÍVEL]" e código "(12154)" removidos da classe.
    assert d["classe"] == "EXECUÇÃO DE TÍTULO EXTRAJUDICIAL"
    assert d["comarca"] == "Contagem"          # derivada do órgão julgador
    assert d["vara"] == "4ª Vara Cível da Comarca de Contagem"
    assert d["valor_causa"] == "R$ 4.618,20"
    assert d["tribunal"] == "TJMG"
    papeis = {(p["papel"], p["nome"]) for p in d["partes"]}
    assert ("EXEQUENTE", "DIRECIONAL ENGENHARIA S.A") in papeis
    # "EXECUTADO(A)" (parênteses aninhados) é reconhecido como parte passiva.
    assert any("LUIZ ALBERTO MARAZZI" == nome for _, nome in papeis)
    # Advogados/procuradoria NÃO viram partes.
    assert not any("ADVOGADO" in pap for pap, _ in papeis)


# --------------------------------------------------------------------------
# Papéis de polo que o tribunal inventa (TJDFT: REVEL, EXECUTADO ESPÓLIO DE)
# --------------------------------------------------------------------------
_DETALHE_TJDFT_REVEL = """
<div id="maisDetalhes">
  <dl><dt>Classe judicial:</dt><dd>EXECUÇÃO DE TÍTULO EXTRAJUDICIAL (12154)</dd></dl>
</div>
<div id="poloAtivo"><table><tbody><tr><td>
  <span>OURO BRANCO EMPREENDIMENTOS IMOBILIARIOS LTDA - CNPJ: 13.466.398/0001-06 (EXEQUENTE)</span>
</td></tr></tbody></table></div>
<div id="poloPassivo"><table><tbody><tr><td>
  <span>ARLISON TULIO DE SOUZA DA SILVA - CPF: 057.208.821-38 (REVEL)</span>
  <ul class="tree"><li><small>DP - CURADORIA ESPECIAL</small></li></ul>
</td></tr></tbody></table></div>
"""


def test_revel_e_polo_passivo_e_nao_some():
    """O caso real dos 7 processos do TJDFT que ficavam só com o autor.

    O painel mostrava "(REVEL)" — papel que não existia em _RX_PARTE, então a
    linha montada não casava e a parte era descartada em silêncio.
    """
    from organizador.integracoes.tjsp_scraper.parser_pje import (
        _formatar_detalhe_pje,
        extrair_processo_pje,
    )
    from organizador.organizacao import _dict_para_processo, _reus_joined

    proc = extrair_processo_pje(
        _formatar_detalhe_pje(_DETALHE_TJDFT_REVEL), "0003737-89.2017.8.07.0019"
    )
    papeis = {p["papel"]: p["nome"] for p in proc["partes"]}
    assert papeis.get("REVEL") == "ARLISON TULIO DE SOUZA DA SILVA"
    assert papeis.get("EXEQUENTE") == "OURO BRANCO EMPREENDIMENTOS IMOBILIARIOS LTDA"
    # e o revel tem de contar como RÉU no agregado que alimenta a carteira
    assert _reus_joined(_dict_para_processo(proc)) == "ARLISON TULIO DE SOUZA DA SILVA"


def test_papel_desconhecido_cai_no_polo_do_container():
    """Parte visível no painel nunca deve desaparecer por causa do rótulo.

    O container (#poloAtivo/#poloPassivo) já prova o polo, então papel novo de
    tribunal degrada para o canônico do polo em vez de custar a parte.
    """
    from organizador.integracoes.tjsp_scraper.parser_pje import _papel_utilizavel

    # conhecido → preservado como está
    assert _papel_utilizavel("REVEL", "poloPassivo") == "REVEL"
    assert _papel_utilizavel("EXEQUENTE", "poloAtivo") == "EXEQUENTE"
    # qualificado → mantém a 1ª palavra, que já fixa o polo e preserva o nome
    assert _papel_utilizavel("EXECUTADO ESPÓLIO DE", "poloPassivo") == "EXECUTADO"
    # totalmente novo → papel canônico do polo
    assert _papel_utilizavel("PAPEL QUE NAO EXISTE", "poloPassivo") == "REQUERIDO"
    assert _papel_utilizavel("PAPEL QUE NAO EXISTE", "poloAtivo") == "REQUERENTE"
    assert _papel_utilizavel("", "poloPassivo") == "REQUERIDO"
