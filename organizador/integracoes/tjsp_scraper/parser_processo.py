# src/parser_processo.py
"""
Extrator estruturado da pagina de detalhes do processo no eproc.

A funcao principal eh `extrair_processo(html_detalhes, blocos_documentos,
numero_cnj)` que devolve um dict aninhado com cabecalho, partes, eventos,
prazos, atos processuais, agregados e dicas linguisticas.

Esse modulo eh INDEPENDENTE do parser de penhora/bloqueio em src/parser.py
(aquele continua tratando keywords). Aqui o foco eh estrutura do processo.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from bs4 import BeautifulSoup, Tag
from unidecode import unidecode


# ---------------------------------------------------------------------------
# Tabela CNJ -> tribunal/uf. O segmento TR do CNJ NNNNNNN-DD.AAAA.J.TR.OOOO
# eh um codigo de 2 digitos por tribunal. Aqui mapeamos a Justica Estadual
# (J=8). Justicas Federal/Trabalho/Eleitoral nao sao alvo deste projeto.
# ---------------------------------------------------------------------------
_TJ_POR_TR = {
    "01": ("TJAC", "AC"), "02": ("TJAL", "AL"), "03": ("TJAP", "AP"),
    "04": ("TJAM", "AM"), "05": ("TJBA", "BA"), "06": ("TJCE", "CE"),
    "07": ("TJDFT", "DF"), "08": ("TJES", "ES"), "09": ("TJGO", "GO"),
    "10": ("TJMA", "MA"), "11": ("TJMS", "MS"), "12": ("TJMT", "MT"),
    "13": ("TJMG", "MG"), "14": ("TJPA", "PA"), "15": ("TJPB", "PB"),
    "16": ("TJPR", "PR"), "17": ("TJPE", "PE"), "18": ("TJPI", "PI"),
    "19": ("TJRJ", "RJ"), "20": ("TJRN", "RN"), "21": ("TJRS", "RS"),
    "22": ("TJRO", "RO"), "23": ("TJRR", "RR"), "24": ("TJSC", "SC"),
    "25": ("TJSE", "SE"), "26": ("TJSP", "SP"), "27": ("TJTO", "TO"),
}

# Siglas dos documentos do eproc -> tipo legivel do ato.
_TIPO_DOC = {
    "ATOORD": "ATO ORDINATÓRIO",
    "DESP":   "DESPACHO",
    "DEC":    "DECISÃO",
    "SENT":   "SENTENÇA",
}

# ---------------------------------------------------------------------------
# REGEX
# ---------------------------------------------------------------------------
_RX_CNJ = re.compile(r"(\d{7})-?(\d{2})\.?(\d{4})\.?(\d)\.?(\d{2})\.?(\d{4})")
_RX_DATA_HORA = re.compile(r"(\d{2}/\d{2}/\d{4})(?:\s+(\d{2}:\d{2}:\d{2}))?")
_RX_REFER_EVENTO = re.compile(r"Refer\.\s*ao\s*Evento:\s*(\d+)", re.I)
_RX_PRAZO_DIAS = re.compile(r"Prazo:\s*(\d+)\s*dia", re.I)
_RX_DATA_INICIAL = re.compile(r"Data\s+Inicial\s+da\s+Contagem\s+do\s+Prazo:.*?(\d{2}/\d{2}/\d{4}(?:\s+\d{2}:\d{2}:\d{2})?)", re.I | re.S)
_RX_DATA_FINAL = re.compile(r"Data\s+Final\s+do\s+Prazo:.*?(\d{2}/\d{2}/\d{4}(?:\s+\d{2}:\d{2}:\d{2})?)", re.I | re.S)
_RX_STATUS_PRAZO = re.compile(r"Status\s+do\s+Prazo:.*?(ABERTO|FECHADO)", re.I | re.S)
_RX_COMARCA = re.compile(r"Comarca\s+de\s+([^/]+?)(?:\s*$|\s*-|\s*\(|\s*/)", re.I)
_RX_VARA = re.compile(r"(.+?Vara[^-/]*?)(?:\s+da\s+Comarca|\s*$|\s*-)", re.I)
_RX_OAB = re.compile(r"\b([A-Z]{2})(\d{4,6})\b")
_RX_ASSINATURA = re.compile(
    r"ASSINADO\s+ELETRONICAMENTE\s+POR\s+(.+?)\s+(?:EM|,)\s+(\d{2}/\d{2}/\d{4}(?:\s+\d{2}:\d{2}:\d{2})?)",
    re.I | re.S,
)
_RX_VERIFICADOR = re.compile(r"(?:c[óo]digo\s+verificador|verificador):\s*([0-9A-Za-z]+)", re.I)
_RX_CRC = re.compile(r"CRC:\s*([0-9a-fA-F]+)", re.I)
_RX_VALOR_CAUSA = re.compile(r"Valor\s+da\s+Causa[:\s]*R\$?\s*([\d\.\,]+)", re.I)
_RX_TAG_KNOWN = ("Idoso", "Processo Migrado", "Prioridade", "Sigilo", "Segredo de Justiça")

# Heuristica simples de genero pelo nome (best-effort, sem dicionario externo).
# Nomes terminados em 'a' tendem a ser femininos; em 'o', 'os', consoante,
# tendem a ser masculinos. Excecoes comuns: 'Andrea', 'Lia', 'Joshua' etc.
# Mantemos heuristica conservadora — se nao for clara, devolvemos "".
def _genero_pelo_nome(nome: str) -> str:
    if not nome:
        return ""
    primeiro = unidecode(nome.strip()).split()[0].lower()
    excecoes_masc = {"luca", "joshua", "joel", "daniel", "elias", "isaias", "andrea"}
    excecoes_fem = {"ines", "beatriz", "leonor", "isabel", "raquel", "miriam"}
    if primeiro in excecoes_masc:
        return "m"
    if primeiro in excecoes_fem:
        return "f"
    if primeiro.endswith("a") or primeiro.endswith("ana") or primeiro.endswith("ele"):
        return "f"
    if primeiro.endswith("o") or primeiro.endswith("os") or primeiro.endswith("io"):
        return "m"
    return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _txt(node: Optional[Tag], default: str = "") -> str:
    if node is None:
        return default
    return node.get_text(" ", strip=True)


def _txt_por_id(soup: BeautifulSoup, elem_id: str, default: str = "") -> str:
    el = soup.find(id=elem_id)
    return _txt(el, default)


def _normaliza(txt: str) -> str:
    return re.sub(r"\s+", " ", (txt or "").strip())


# ---------------------------------------------------------------------------
# Cabecalho
# ---------------------------------------------------------------------------
def _extrair_cabecalho(soup: BeautifulSoup) -> dict:
    return {
        "numero":         _normaliza(_txt_por_id(soup, "txtNumProcesso")),
        "classe":         _normaliza(_txt_por_id(soup, "txtClasse")),
        "competencia":    _normaliza(_txt_por_id(soup, "txtCompetencia")),
        "data_autuacao":  _normaliza(_txt_por_id(soup, "txtAutuacao")),
        "situacao":       _normaliza(_txt_por_id(soup, "txtSituacao")),
        "orgao_julgador": _normaliza(_txt_por_id(soup, "txtOrgaoJulgador")),
        "juiz":           _normaliza(_txt_por_id(soup, "txtMagistrado")),
        "chave_consulta": _normaliza(_txt_por_id(soup, "spnChaveProcesso")),
    }


def _extrair_assuntos(soup: BeautifulSoup) -> str:
    """O fieldset #fldAssuntos lista assuntos numa table; o principal tem
    `data-assunto-principal='true'`. Retorna o assunto principal; se nao
    achar, devolve o primeiro assunto encontrado."""
    fld = soup.find(id="fldAssuntos")
    if not fld:
        return ""
    principal = fld.find("tr", attrs={"data-assunto-principal": "true"})
    if principal:
        tds = principal.find_all("td")
        if len(tds) >= 2:
            return _normaliza(_txt(tds[1]))
    # Fallback: primeiro <tr> de dados (pula header)
    rows = fld.find_all("tr")
    for tr in rows[1:]:
        tds = tr.find_all("td")
        if len(tds) >= 2:
            return _normaliza(_txt(tds[1]))
    return ""


# ---------------------------------------------------------------------------
# Comarca / vara (derivados do orgao_julgador)
# ---------------------------------------------------------------------------
def _split_comarca_vara(orgao: str) -> tuple[str, str]:
    """
    Recebe algo como 'Juízo da 11ª Vara Cível da Comarca de Belo Horizonte'.
    Devolve (comarca, vara).
    """
    if not orgao:
        return "", ""
    comarca = ""
    m_c = _RX_COMARCA.search(orgao)
    if m_c:
        comarca = "Comarca de " + _normaliza(m_c.group(1))
    vara = ""
    # Tenta capturar a 'X Vara Y' antes do 'da Comarca'.
    m_v = re.search(r"((?:Ju[íi]zo[^\-/]*?)?\d+ª\s*Vara[^-/]*?)(?:\s+da\s+Comarca|\s*$)", orgao, re.I)
    if m_v:
        vara = _normaliza(m_v.group(1))
    elif "Vara" in orgao:
        # Fallback: pega tudo antes de 'da Comarca'
        vara = _normaliza(orgao.split("da Comarca")[0])
    return comarca, vara


# ---------------------------------------------------------------------------
# Tribunal / UF (inferido do CNJ)
# ---------------------------------------------------------------------------
def _inferir_tribunal_uf(numero_cnj: str) -> tuple[str, str]:
    m = _RX_CNJ.search(numero_cnj or "")
    if not m:
        return "", ""
    justica = m.group(4)  # J
    tr = m.group(5)       # TR
    if justica != "8":
        return "", ""     # so estadual por enquanto
    return _TJ_POR_TR.get(tr, ("", ""))


# ---------------------------------------------------------------------------
# Partes e advogados
# ---------------------------------------------------------------------------
def _extrair_partes(soup: BeautifulSoup) -> list[dict]:
    """
    Cada <td class='autorReu'> contem 1 parte + N advogados. Estrutura:
        <a class='infraNomeParte' data-parte='AUTOR'>NOME</a>
        (<span id='spnCpfParteAutor0'>CPF/CNPJ</span>) <span>- Pessoa Jurídica</span>
        ADVOGADO_NOME    UF+OAB (ex: MG104784)
        ADVOGADO_NOME    UF+OAB
    O cabecalho da tabela (#tblPartesERepresentantes) traz os PAPEIS
    visiveis (ex.: SUSCITANTE | SUSCITADO). Usamos esses como `papel`,
    com fallback para o atributo data-parte (AUTOR/REU/INTERNO).
    """
    tabela = soup.find(id="tblPartesERepresentantes")
    if not tabela:
        return []

    # Cabecalho com papeis humanos (ex.: SUSCITANTE / SUSCITADO).
    papeis_humanos: list[str] = []
    primeiro_tr = tabela.find("tr")
    if primeiro_tr:
        for th in primeiro_tr.find_all("th"):
            papeis_humanos.append(_normaliza(_txt(th)).upper())

    partes: list[dict] = []
    # Linhas de dados (todas exceto a do header).
    for tr in tabela.find_all("tr"):
        if tr is primeiro_tr:
            continue
        tds = tr.find_all("td", class_="autorReu")
        for idx, td in enumerate(tds):
            parte = _parse_td_parte(td, papeis_humanos[idx] if idx < len(papeis_humanos) else "")
            if parte and parte.get("nome"):
                partes.append(parte)
    return partes


def _parse_td_parte(td: Tag, papel_humano: str) -> Optional[dict]:
    nome_a = td.find("a", class_="infraNomeParte")
    if not nome_a:
        return None
    nome = _normaliza(_txt(nome_a))
    papel_attr = (nome_a.get("data-parte") or "").upper()

    # Documento (CPF/CNPJ)
    documento = ""
    span_cpf = td.find("span", id=re.compile(r"^spnCpfParte"))
    if span_cpf:
        documento = _normaliza(_txt(span_cpf))

    # Tipo (PJ/PF) — heuristica: span vizinho com "Pessoa Jurídica"/"Pessoa Física"
    tipo = ""
    for sp in td.find_all("span"):
        t = _txt(sp)
        if "Pessoa Jurídica" in t or "Pessoa Juridica" in t:
            tipo = "Pessoa Jurídica"
            break
        if "Pessoa Física" in t or "Pessoa Fisica" in t:
            tipo = "Pessoa Física"
            break

    # Advogados: dentro do TD, o nome do advogado aparece em texto puro e
    # logo apos vem o link com a OAB (ex.: 'MG104784'). Vamos varrer o
    # texto/anchors em ordem.
    advogados = _parse_advogados(td, nome_parte=nome)

    return {
        "papel": (papel_humano or papel_attr).strip(),
        "nome": nome,
        "documento": documento,
        "tipo": tipo,
        "advogados": advogados,
    }


def _parse_advogados(td: Tag, nome_parte: str) -> list[dict]:
    """
    Extrai pares (nome_advogado, OAB) do bloco da parte. A OAB esta em
    anchors com texto no formato `UF + digitos` (ex.: 'MG104784'). O nome
    do advogado aparece imediatamente antes desse anchor, num text node.
    """
    advogados = []
    # Anchors candidatos = qualquer <a> cujo texto bata com a regex OAB.
    for a in td.find_all("a"):
        texto = _normaliza(_txt(a))
        m = _RX_OAB.fullmatch(texto)
        if not m:
            continue
        uf, num = m.group(1), m.group(2)
        # Nome do advogado = texto imediato anterior (ignora &nbsp; e tags
        # internas tipo <div class='sr-only'>).
        nome_adv = _texto_anterior_significativo(a)
        if not nome_adv or nome_adv.upper() == nome_parte.upper():
            continue
        advogados.append({
            "nome": nome_adv,
            "oab":  f"{uf}{num}",
            "uf":   uf,
        })
    return advogados


def _texto_anterior_significativo(node: Tag, max_chars: int = 200) -> str:
    """
    Anda para tras pelos siblings/parents pegando texto cru ate encontrar
    algo nao vazio. Usado para casar o NOME do advogado com seu link OAB.
    """
    pedacos = []
    cur = node
    # Sobe ate o pai imediato com texto util.
    for _ in range(20):
        cur = cur.previous_sibling
        if cur is None:
            break
        if isinstance(cur, str):
            t = cur.replace("\xa0", " ").strip()
            if t:
                pedacos.append(t)
                if len(" ".join(pedacos)) >= 3:
                    break
        else:
            # Pula spans 'sr-only' e elementos invisiveis.
            classes = " ".join(cur.get("class", []) or []) if hasattr(cur, "get") else ""
            if "sr-only" in classes:
                continue
            t = _txt(cur)
            if t:
                pedacos.append(t)
                break
    nome = " ".join(reversed(pedacos)).strip()
    nome = re.sub(r"\s+", " ", nome)
    # Tira pontuacao/parenteses que sobraram
    nome = nome.strip(" -–,;:()\t")
    return nome[:max_chars]


# ---------------------------------------------------------------------------
# Eventos
# ---------------------------------------------------------------------------
def _extrair_eventos(soup: BeautifulSoup) -> list[dict]:
    tabela = soup.find(id="tblEventos")
    if not tabela:
        return []
    eventos: list[dict] = []
    for tr in tabela.find_all("tr"):
        # Pula header
        if tr.find("th"):
            continue
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 4:
            continue
        evento = _parse_tr_evento(tr, tds)
        if evento:
            eventos.append(evento)
    return eventos


def _parse_tr_evento(tr: Tag, tds: list[Tag]) -> Optional[dict]:
    # td[0] numero do evento (texto + lupa de prazo)
    num_txt = _txt(tds[0])
    m_num = re.match(r"(\d+)", num_txt)
    numero = m_num.group(1) if m_num else ""

    # data/hora em td[1]
    data_hora = _normaliza(_txt(tds[1]))
    data, hora = "", ""
    m_dh = _RX_DATA_HORA.search(data_hora)
    if m_dh:
        data = m_dh.group(1)
        hora = m_dh.group(2) or ""

    # descricao em td[2]
    desc_node = tds[2] if len(tds) > 2 else None
    descricao = _normaliza(_txt(desc_node)) if desc_node else ""

    # usuario em td[3] (label.infraEventoUsuario)
    usuario = ""
    if len(tds) > 3:
        lbl = tds[3].find("label", class_="infraEventoUsuario")
        if lbl:
            usuario = _normaliza(_txt(lbl))

    # documentos em td[4] (a.infraLinkDocumento / a.infraLinkDocumentoSigiloso)
    documentos: list[str] = []
    if len(tds) > 4:
        for a in tds[4].find_all("a"):
            data_nome = (a.get("data-nome") or "").strip()
            label = _normaliza(_txt(a))
            if data_nome:
                documentos.append(label or data_nome)
            elif label and not label.lower().startswith("visualizar"):
                documentos.append(label)

    # refer_evento e prazo (na descricao OU no tooltip data-infoevento da lupa)
    refer_evento = ""
    m_ref = _RX_REFER_EVENTO.search(descricao)
    if m_ref:
        refer_evento = m_ref.group(1)

    prazo_dias = ""
    m_pd = _RX_PRAZO_DIAS.search(descricao)
    if m_pd:
        prazo_dias = m_pd.group(1)

    # Tooltip do prazo (so existe em eventos de intimacao/citacao)
    prazo_status, prazo_inicio, prazo_fim = "", "", ""
    info_lupa = tds[0].find(attrs={"data-infoevento": True})
    tooltip = info_lupa.get("data-infoevento", "") if info_lupa else ""
    if tooltip:
        # O tooltip eh HTML com entidades — basta passar pra BeautifulSoup
        # para virar texto plano.
        tooltip_txt = BeautifulSoup(tooltip, "html.parser").get_text(" ", strip=True)
        m_s = _RX_STATUS_PRAZO.search(tooltip_txt)
        if m_s:
            prazo_status = m_s.group(1).upper()
        m_i = _RX_DATA_INICIAL.search(tooltip_txt)
        if m_i:
            prazo_inicio = m_i.group(1)
        m_f = _RX_DATA_FINAL.search(tooltip_txt)
        if m_f:
            prazo_fim = m_f.group(1)

    # Fallback via classes CSS quando nao houver tooltip
    if not prazo_status:
        classes = " ".join(tr.get("class", []))
        if "infraEventoPrazoFechado" in classes:
            prazo_status = "FECHADO"
        elif "infraEventoPrazoAberto" in classes or "infraEventoPrazoEmAberto" in classes:
            prazo_status = "ABERTO"

    return {
        "numero":        numero,
        "data":          data,
        "hora":          hora,
        "descricao":     descricao,
        "usuario":       usuario,
        "documentos":    documentos,
        "refer_evento":  refer_evento,
        "prazo_dias":    prazo_dias,
        "prazo_status":  prazo_status,
        "prazo_inicio":  prazo_inicio,
        "prazo_fim":     prazo_fim,
    }


# ---------------------------------------------------------------------------
# Prazos em aberto (derivado dos eventos)
# ---------------------------------------------------------------------------
def _extrair_prazos_abertos(eventos: list[dict], partes: list[dict]) -> list[dict]:
    """
    Um evento eh "prazo em aberto" quando tem prazo_status='ABERTO' e
    prazo_fim no futuro. Aqui marcamos status pelo que veio no tooltip;
    quem chama decide se filtra por data.
    """
    abertos = []
    for ev in eventos:
        if (ev.get("prazo_status") or "").upper() != "ABERTO":
            continue
        # tenta achar a parte alvo pela mencao na descricao
        parte_nome, parte_papel = _associar_parte(ev.get("descricao", ""), partes)
        abertos.append({
            "parte_nome":     parte_nome,
            "parte_papel":    parte_papel,
            "dias":           ev.get("prazo_dias", ""),
            "inicio":         ev.get("prazo_inicio", ""),
            "fim":            ev.get("prazo_fim", ""),
            "status":         "ABERTO",
            "evento_origem":  ev.get("numero", ""),
        })
    return abertos


def _associar_parte(descricao: str, partes: list[dict]) -> tuple[str, str]:
    desc_norm = unidecode(descricao or "").upper()
    for p in partes:
        nome_n = unidecode(p.get("nome", "") or "").upper()
        if nome_n and nome_n in desc_norm:
            return p["nome"], p.get("papel", "")
    return "", ""


# ---------------------------------------------------------------------------
# Atos processuais (dos documentos individuais ja com conteudo AJAX)
# ---------------------------------------------------------------------------
def _classificar_tipo(arquivo: str, descricao_evento: str = "") -> str:
    """Sigla do arquivo (ex.: 'ATOORD1', 'DEC1') -> tipo legivel."""
    sigla = re.sub(r"\d+$", "", (arquivo or "").upper())
    if sigla in _TIPO_DOC:
        return _TIPO_DOC[sigla]
    # heuristica pela descricao do evento
    d = unidecode((descricao_evento or "")).upper()
    if "ATO ORDINATORIO" in d:
        return "ATO ORDINATÓRIO"
    if "DESPACHO" in d:
        return "DESPACHO"
    if "DECISAO" in d or "DECIS" in d:
        return "DECISÃO"
    if "SENTENCA" in d or "SENTEN" in d:
        return "SENTENÇA"
    return ""


def _extrair_atos(
    blocos_documentos: Iterable[tuple[str, str, str, str, str]],
    eventos: list[dict],
) -> list[dict]:
    """
    blocos_documentos vem de src.parser._extrair_blocos_com_origem(html):
        [(arquivo, evento_num, data, doc_id, html_do_bloco), ...]
    Aqui filtramos os que sao ATO ORDINATÓRIO / DESPACHO / DECISÃO /
    SENTENÇA e extraimos teor, signatario, codigos.
    """
    atos = []
    # Indexa eventos por numero para puxar descricao quando necessario
    ev_por_num = {ev["numero"]: ev for ev in eventos if ev.get("numero")}

    for arquivo, evento_num, data, doc_id, html in blocos_documentos:
        if not html or not html.strip():
            continue
        desc_evento = (ev_por_num.get(evento_num) or {}).get("descricao", "")
        tipo = _classificar_tipo(arquivo, desc_evento)
        if not tipo:
            continue

        soup_doc = BeautifulSoup(html, "html.parser")
        corpo_node = soup_doc.find(id="divdochtml") or soup_doc
        teor = _normaliza(corpo_node.get_text(" ", strip=True))[:4000]
        if not teor:
            continue

        # signatario + data_assinatura
        signatario, data_assinatura = "", ""
        m_sig = _RX_ASSINATURA.search(teor)
        if m_sig:
            signatario = _normaliza(m_sig.group(1))
            data_assinatura = _normaliza(m_sig.group(2))

        # codigos
        m_v = _RX_VERIFICADOR.search(teor)
        m_c = _RX_CRC.search(teor)
        codigo_verificador = m_v.group(1) if m_v else ""
        codigo_crc = m_c.group(1) if m_c else ""

        # classe_acao: tipicamente aparece no cabecalho do PDF como
        # 'PROCEDIMENTO COMUM CIVEL' ou similar; pegamos a 1a linha
        # totalmente em caixa-alta como heuristica.
        classe_acao = ""
        for linha in teor.split("\n"):
            linha = linha.strip()
            if 10 <= len(linha) <= 80 and linha == linha.upper() and any(c.isalpha() for c in linha):
                classe_acao = linha
                break

        # local: cidade antes da data de assinatura (heuristica simples)
        local = ""
        if data_assinatura:
            pre = teor.split(data_assinatura)[0]
            m_local = re.search(r"([A-Z][^\n,]+),\s*$", pre[-200:])
            if m_local:
                local = _normaliza(m_local.group(1))

        atos.append({
            "tipo":               tipo,
            "classe_acao":        classe_acao,
            "teor":               teor,
            "signatario":         signatario,
            "data_assinatura":    data_assinatura,
            "codigo_verificador": codigo_verificador,
            "codigo_crc":         codigo_crc,
            "local":              local,
            "arquivo":            arquivo,
        })
    return atos


# ---------------------------------------------------------------------------
# Informacoes adicionais (carregadas via AJAX no fldInformacoesAdicionais)
# ---------------------------------------------------------------------------
def _extrair_info_adicionais(soup: BeautifulSoup) -> dict:
    """
    Le valor_causa, foro_endereco e tags se o conteudo de
    #fldInformacoesAdicionais_content estiver populado (HTML pos-AJAX).
    """
    out = {"valor_causa": "", "foro_endereco": "", "tags": []}
    fld = soup.find(id="fldInformacoesAdicionais") or soup.find(id="fldInformacoesAdicionais_content")
    if not fld:
        return out

    texto = fld.get_text(" ", strip=True)

    m_v = _RX_VALOR_CAUSA.search(texto)
    if m_v:
        out["valor_causa"] = f"R$ {_normaliza(m_v.group(1))}"

    # foro_endereco: linha que contenha 'Endereço' ou CEP / telefone
    m_end = re.search(
        r"(Endere[çc]o[:\s]+.+?)(?:\s+CEP|\s+Fone|\s+Tel|\s+Email|$)",
        texto, re.I | re.S,
    )
    if m_end:
        out["foro_endereco"] = _normaliza(m_end.group(1))

    # tags conhecidas
    for tag in _RX_TAG_KNOWN:
        if tag.lower() in texto.lower():
            out["tags"].append(tag)
    return out


# ---------------------------------------------------------------------------
# Inferencia linguistica
# ---------------------------------------------------------------------------
def _inferir_linguistica(partes: list[dict]) -> dict:
    autores = [p for p in partes if _eh_papel(p.get("papel", ""), {"AUTOR", "SUSCITANTE", "EXEQUENTE", "REQUERENTE", "EMBARGANTE", "AGRAVANTE", "IMPETRANTE", "RECLAMANTE"})]
    reus    = [p for p in partes if _eh_papel(p.get("papel", ""), {"REU", "RÉU", "SUSCITADO", "EXECUTADO", "REQUERIDO", "EMBARGADO", "AGRAVADO", "IMPETRADO", "RECLAMADO"})]

    def _g(p: dict) -> str:
        if (p.get("tipo") or "").startswith("Pessoa Jur"):
            return "m"  # tratamento neutro masculino p/ PJ
        return _genero_pelo_nome(p.get("nome", ""))

    return {
        "genero_autor":  _g(autores[0]) if autores else "",
        "numero_autor":  "p" if len(autores) > 1 else ("s" if autores else ""),
        "genero_reu":    _g(reus[0]) if reus else "",
        "numero_reu":    "p" if len(reus) > 1 else ("s" if reus else ""),
    }


def _eh_papel(papel: str, alvo: set[str]) -> bool:
    return unidecode((papel or "").upper()) in {unidecode(a.upper()) for a in alvo}


# ---------------------------------------------------------------------------
# Funcao principal
# ---------------------------------------------------------------------------
def extrair_processo(
    html_detalhes: str,
    blocos_documentos: Optional[list[tuple[str, str, str, str, str]]] = None,
    numero_cnj: str = "",
) -> dict:
    """
    Recebe o HTML da pagina de detalhes do processo no eproc e (opcional)
    a lista de blocos de documentos ja parseada por
    src.parser._extrair_blocos_com_origem(). Devolve dict aninhado pronto
    para serializar como JSON.
    """
    soup = BeautifulSoup(html_detalhes or "", "html.parser")

    cabecalho = _extrair_cabecalho(soup)
    assunto = _extrair_assuntos(soup)
    partes = _extrair_partes(soup)
    eventos = _extrair_eventos(soup)
    prazos = _extrair_prazos_abertos(eventos, partes)
    info_adicionais = _extrair_info_adicionais(soup)
    tribunal, uf = _inferir_tribunal_uf(numero_cnj or cabecalho.get("numero", ""))
    comarca, vara = _split_comarca_vara(cabecalho.get("orgao_julgador", ""))
    linguistica = _inferir_linguistica(partes)

    atos = _extrair_atos(blocos_documentos or [], eventos)
    arquivos_origem = sorted({a["arquivo"] for a in atos if a.get("arquivo")})

    # Agregados temporais
    datas_eventos = [ev.get("data", "") for ev in eventos if ev.get("data")]
    primeira_mov = datas_eventos[-1] if datas_eventos else ""  # tabela vem do mais recente
    ultima_mov_data = datas_eventos[0] if datas_eventos else ""
    ultima_mov_desc = eventos[0].get("descricao", "") if eventos else ""

    return {
        **cabecalho,
        "assunto":           assunto,
        "tribunal":          tribunal,
        "uf":                uf,
        "comarca":           comarca,
        "vara":              vara,
        **info_adicionais,
        "partes":            partes,
        "eventos":           eventos,
        "prazos_abertos":    prazos,
        "atos":              atos,
        "total_eventos":     len(eventos),
        "primeira_movimentacao": primeira_mov,
        "ultima_movimentacao":   {"data": ultima_mov_data, "descricao": ultima_mov_desc},
        "arquivos_origem":   arquivos_origem,
        **linguistica,
    }
