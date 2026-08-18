# -*- coding: utf-8 -*-
"""Extrator estruturado do PJe (TJMG 1º grau).

Espelha o contrato de `parser_processo.extrair_processo()` (eproc) e
`parser_esaj.extrair_processo_esaj()`: devolve um dict aninhado pronto para
`organizacao._dict_para_processo()`.

Diferente do eproc/e-SAJ, a "página" do PJe NÃO é HTML estruturado: o
`PJEScraper` devolve um BLOB DE TEXTO montado em
`_coletar_timeline_e_documentos()` —

    <texto da timeline (newest-first)>
    <!-- DOCUMENTO_ORIGEM: NOME | EVENTO: | DATA: 09 fev 2026 | ID: 123 -->
    <corpo do documento aberto>
    <!-- DOCUMENTO_ORIGEM: ... -->
    ...

Então este parser tem duas fontes:
  • a timeline (trecho ANTES do 1º marcador) → eventos cronológicos;
  • os corpos dos documentos abertos (Despacho/Decisão/Sentença/Juntada
    SISBAJUD…) → cabeçalho (classe, assunto, comarca/vara, endereço),
    partes com CPF/CNPJ, e os atos com teor.

Cabeçalhos confirmados no blob real (5000432-19.2021.8.13.0079):
    PODER JUDICIÁRIO DO ESTADO DE MINAS GERAIS
     Comarca de Contagem / 4ª Vara Cível da Comarca de Contagem
     Avenida Maria da Glória Rocha, 425, Centro, Contagem - MG - CEP: 32010-375
    PROCESSO Nº 5000432-19.2021.8.13.0079
    [CÍVEL] CUMPRIMENTO DE SENTENÇA (156)
    REQUERENTE: ANAPU EMPREENDIMENTOS IMOBILIÁRIOS LTDA CPF: 10.923.840/0001-80
    REQUERIDO(A): LINDIOMAR NASCIMENTO DE JESUS CPF: 085.118.676-94
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup
from unidecode import unidecode

from .parser import _RX_DOCUMENTO_ORIGEM
from .parser_processo import _inferir_tribunal_uf, _normaliza

# Mês textual do PJe → número (datas vêm como "18 fev 2026").
_MESES = {
    "jan": "01", "fev": "02", "mar": "03", "abr": "04", "mai": "05", "jun": "06",
    "jul": "07", "ago": "08", "set": "09", "out": "10", "nov": "11", "dez": "12",
}
_RX_DATA_PJE = re.compile(r"^(\d{1,2})\s+([a-zç]{3})\s+(\d{4})$", re.I)
_RX_HORA = re.compile(r"^\d{2}:\d{2}$")
_RX_DOC_LINHA = re.compile(r"^(\d{6,})\s*-\s*(.+)$")  # "10629074581 - Petição (…)"

# Linha de parte no cabeçalho do documento (ou sintetizada do painel de detalhes):
#   "REQUERENTE: NOME ... CPF: 10.923.840/0001-80"
#   "RÉU: NOME ... CPF: 085.118.676-94 e outros"
#   "REQUERIDO(A): NOME"  ← SEM documento (réu pessoa física com CPF sob sigilo)
# O CPF/CNPJ é OPCIONAL: o réu costuma vir sem documento e, antes, era descartado
# aqui — deixando o processo só com o autor. Ancorado em fim-de-linha; só casa
# linhas que COMEÇAM com um papel conhecido (evita falso-positivo em corpo de ato).
_RX_PARTE = re.compile(
    r"^\s*(?P<papel>REQUERENTE|REQUERIDO\(A\)|REQUERIDO|REQUERENTE\(A\)|"
    r"AUTOR|AUTORA|R[ÉE]U|EXEQUENTE|EXECUTADO\(A\)|EXECUTADO|EXECUTADA|"
    r"EMBARGANTE|EMBARGADO\(A\)|EMBARGADO|AGRAVANTE|AGRAVADO\(A\)|AGRAVADO|"
    r"APELANTE|APELADO\(A\)|APELADO|IMPETRANTE|IMPETRADO\(A\)|IMPETRADO|"
    r"RECORRENTE|RECORRIDO\(A\)|RECORRIDO|TERCEIRO INTERESSADO|INTERESSADO\(A\)|"
    r"INTERESSADO|REVEL|V[ÍI]TIMA)\s*:\s*"
    r"(?P<nome>.+?)"
    r"(?:"
    r"\s*(?:CPF|CNPJ)\s*:\s*(?P<doc>[\d./-]+)"  # com doc: para no documento (como antes)
    r"|"
    r"\s*(?:e\s+outros?)?\s*$"                   # sem doc: nome vai até o fim da linha
    r")",
    re.I,
)

# Papel textual do PJe → forma canônica (PAPEIS_ATIVO/PASSIVO do sistema).
_PAPEL_PJE = {
    "REQUERENTE": "REQUERENTE", "REQUERIDO": "REQUERIDO",
    "AUTOR": "AUTOR", "AUTORA": "AUTOR", "RÉU": "RÉU", "REU": "RÉU",
    "EXEQUENTE": "EXEQUENTE", "EXECUTADO": "EXECUTADO", "EXECUTADA": "EXECUTADO",
    "EMBARGANTE": "EMBARGANTE", "EMBARGADO": "EMBARGADO",
    "AGRAVANTE": "AGRAVANTE", "AGRAVADO": "AGRAVADO",
    "APELANTE": "APELANTE", "APELADO": "APELADO",
    "IMPETRANTE": "IMPETRANTE", "IMPETRADO": "IMPETRADO",
    "RECORRENTE": "RECORRENTE", "RECORRIDO": "RECORRIDO",
    "TERCEIRO INTERESSADO": "TERCEIRO INTERESSADO", "INTERESSADO": "INTERESSADO",
    "VÍTIMA": "VÍTIMA", "VITIMA": "VÍTIMA",
    # TJDFT: executado/réu declarado revel (representado pela Curadoria Especial).
    "REVEL": "REVEL",
}

# Tipo de ato por palavra-chave no nome do documento da timeline.
_RX_TIPO_ATO = re.compile(
    r"\b(SENTEN[ÇC]A|DECIS[ÃA]O|DESPACHO|ATO ORDINAT[ÓO]RIO)\b", re.I
)

# Cabeçalho do documento
_RX_CLASSE = re.compile(r"CLASSE\s*:\s*(.+)", re.I)
_RX_CLASSE_SOLTA = re.compile(r"^\s*(\[[^\]]+\].+?\(\d+\))\s*$")  # "[CÍVEL] ... (156)"
_RX_ASSUNTO = re.compile(r"ASSUNTO\s*:\s*(.+)", re.I)
_RX_COMARCA_VARA = re.compile(r"Comarca de\s+(?P<comarca>.+?)\s*/\s*(?P<vara>.+)", re.I)
_RX_ENDERECO = re.compile(r"^.*CEP\s*:\s*[\d.-]+.*$", re.I | re.M)
_RX_VALOR = re.compile(r"valor da causa[^R]*R\$\s*([\d.,]+)", re.I)


def _converter_data(bruto: str) -> str:
    """'18 fev 2026' → '18/02/2026'. Devolve '' se não casar."""
    m = _RX_DATA_PJE.match((bruto or "").strip())
    if not m:
        return ""
    dia, mes_txt, ano = m.groups()
    mes = _MESES.get(unidecode(mes_txt).lower()[:3])
    return f"{int(dia):02d}/{mes}/{ano}" if mes else ""


def _tipo_documento(doc: str) -> str:
    """CPF (11 díg) → Pessoa Física; CNPJ (14 díg) → Pessoa Jurídica."""
    digitos = re.sub(r"\D", "", doc or "")
    if len(digitos) == 14:
        return "Pessoa Jurídica"
    if len(digitos) == 11:
        return "Pessoa Física"
    return ""


def _canonizar_papel(bruto: str) -> str:
    chave = re.sub(r"\(.\)", "", unidecode(bruto or "")).strip().upper()
    chave_acentos = re.sub(r"\(.\)", "", (bruto or "")).strip().upper()
    return _PAPEL_PJE.get(chave_acentos) or _PAPEL_PJE.get(chave) or chave_acentos


# Polo de cada papel canônico — para deduplicar partes por POLO, não por papel
# exato (ver _extrair_partes).
_PAPEIS_ATIVO_PJE = {"REQUERENTE", "AUTOR", "EXEQUENTE", "EMBARGANTE",
                     "AGRAVANTE", "APELANTE", "IMPETRANTE", "RECORRENTE"}
_PAPEIS_PASSIVO_PJE = {"REQUERIDO", "RÉU", "REU", "EXECUTADO", "EMBARGADO",
                       "AGRAVADO", "APELADO", "IMPETRADO", "RECORRIDO", "REVEL"}


def _polo_de(papel: str) -> str:
    """'A' (ativo), 'P' (passivo) ou o próprio papel (terceiros/vítima/etc.)."""
    if papel in _PAPEIS_ATIVO_PJE:
        return "A"
    if papel in _PAPEIS_PASSIVO_PJE:
        return "P"
    return papel


# ---------------------------------------------------------------------------
# Partes — varre os cabeçalhos de TODOS os documentos, dedupe por nome.
# ---------------------------------------------------------------------------
def _extrair_partes(blob: str) -> list[dict]:
    partes: list[dict] = []
    vistos: set[str] = set()
    for linha in (blob or "").splitlines():
        m = _RX_PARTE.match(linha)
        if not m:
            continue
        nome = _normaliza(m.group("nome"))
        # remove sufixos que às vezes grudam no nome ("... e outros")
        nome = re.sub(r"\s+e outros?$", "", nome, flags=re.I).strip()
        if not nome:
            continue
        papel = _canonizar_papel(m.group("papel"))
        # Dedupe por (POLO, nome), não pelo nome cru: a MESMA parte pode aparecer
        # como REQUERENTE num doc e AUTOR noutro (mesmo polo → 1 entrada). Mas
        # alguns processos a listam nos DOIS polos (poloAtivo=AUTOR e
        # poloPassivo=RÉU) — aí mantém as duas, senão o réu sumia (deixava o
        # processo "sem réu" mesmo com o polo passivo preenchido no painel).
        chave = (_polo_de(papel), unidecode(nome).upper())
        if chave in vistos:
            continue
        vistos.add(chave)
        doc = (m.group("doc") or "").strip()  # réu sob sigilo vem sem documento
        partes.append({
            "papel": papel,
            "nome": nome,
            "documento": doc,
            "tipo": _tipo_documento(doc),
            "advogados": [],
        })
    return partes


# ---------------------------------------------------------------------------
# Painel "Mais detalhes" do PJe (no DOM da página de autos): cabeçalho + polos
# já estruturados (classe, assunto, jurisdição, órgão julgador, valor, partes).
# Reescreve no formato de BLOB que _extrair_cabecalho/_extrair_partes entendem,
# para dispensar a abertura de documentos na busca.
# ---------------------------------------------------------------------------
# "NOME - CPF: 123 (EXECUTADO(A))" → papel é o ÚLTIMO parêntese (pode ter "(A)"
# aninhado, ex.: TJMG usa "EXECUTADO(A)"/"REQUERIDO(A)"). Ancorado no fim.
# O trecho "- CPF/CNPJ: doc" é OPCIONAL: o polo passivo costuma vir só como
# "NOME (REQUERIDO(A))" (CPF sob sigilo) e antes era ignorado — sumindo o réu.
_RX_PARTE_DETALHE = re.compile(
    r"^(?P<nome>.+?)\s*(?:-\s*(?P<tipo>CPF|CNPJ)\s*:\s*(?P<doc>[\d./-]+)\s*)?\((?P<papel>.+)\)\s*$",
    re.I,
)


# Papel canônico de cada polo, quando o rótulo do tribunal não serve.
_PAPEL_POLO = {"poloAtivo": "REQUERENTE", "poloPassivo": "REQUERIDO"}


def _papel_utilizavel(papel: str, div_id: str) -> str:
    """Papel que `_RX_PARTE` reconhece — nunca devolve algo que faça a parte sumir.

    O painel emite o papel entre parênteses e cada tribunal inventa o seu. Se o
    rótulo não estiver em `_PAPEL_PJE`, a linha montada aqui não casa `_RX_PARTE`
    e a parte é DESCARTADA em silêncio: foi assim que 7 processos do TJDFT ficaram
    só com o autor por rotularem o polo passivo como "REVEL" e
    "EXECUTADO ESPÓLIO DE".

    Ordem de tentativa, da mais fiel à mais grosseira:
      1. o próprio papel, se conhecido ("REVEL", "EXECUTADO"…);
      2. a 1ª palavra ("EXECUTADO ESPÓLIO DE" → "EXECUTADO") — perde o
         qualificador, preserva o polo e o nome, que é o que alimenta o réu;
      3. o papel canônico do polo — o container (#poloAtivo/#poloPassivo) diz o
         polo com certeza, então parte visível no painel sempre sobrevive.
    """
    bruto = (papel or "").strip()
    if bruto and _canonizar_papel(bruto) in _PAPEL_PJE.values():
        return bruto
    primeira = bruto.split()[0] if bruto.split() else ""
    if primeira and _canonizar_papel(primeira) in _PAPEL_PJE.values():
        return primeira
    return _PAPEL_POLO[div_id]


def _campo_detalhe(campos: dict[str, str], *rotulos: str) -> str:
    """Busca um valor por rótulo, tolerante a acento/caixa (varia por tribunal)."""
    for rot in rotulos:
        val = campos.get(unidecode(rot).lower().strip())
        if val:
            return val
    return ""


def _formatar_detalhe_pje(html: str) -> str:
    """Do painel 'Mais detalhes' (#maisDetalhes/#poloAtivo/#poloPassivo), monta um
    bloco-documento no formato esperado pelo parser. '' se o painel não existir.

    Tolerante às diferenças sutis entre tribunais (TJCE, TJMG, …): rótulos sem
    depender de acento/caixa, comarca derivada da Jurisdição OU do Órgão julgador,
    e papéis (EXEQUENTE/EXECUTADO, REQUERENTE/REQUERIDO…) lidos do '(PAPEL)'."""
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return ""
    det = soup.find(id="maisDetalhes")
    if det is None:
        return ""

    campos: dict[str, str] = {}
    for dl in det.find_all("dl"):
        dts, dds = dl.find_all("dt"), dl.find_all("dd")
        for dt, dd in zip(dts, dds):
            rot = unidecode(_normaliza(dt.get_text(" ", strip=True))).lower().rstrip(":").strip()
            val = _normaliza(dd.get_text(" ", strip=True))
            if rot and val:
                campos[rot] = val

    def _sem_codigo(s: str) -> str:
        s = re.sub(r"\s*\(\d+\)", "", s)          # tira "(12154)" da classe/assunto
        s = re.sub(r"^\s*\[[^\]]*\]\s*", "", s)   # tira prefixo "[CÍVEL] " (TJMG)
        return s.strip()

    classe = _sem_codigo(_campo_detalhe(campos, "Classe judicial", "Classe"))
    assunto = _sem_codigo(_campo_detalhe(campos, "Assunto"))
    jurisdicao = _campo_detalhe(campos, "Jurisdição", "Jurisdicao")
    orgao = _campo_detalhe(campos, "Órgão julgador", "Orgao julgador")
    valor = _campo_detalhe(campos, "Valor da causa")

    linhas = ["<!-- DOCUMENTO_ORIGEM: Detalhes do processo | EVENTO: | DATA: | ID: -->"]
    if classe:
        linhas.append(f"CLASSE: {classe}")
    if assunto:
        linhas.append(f"ASSUNTO: {assunto}")
    # Nome da comarca: da Jurisdição ("Comarca de X") ou, em fallback, do próprio
    # Órgão julgador ("Nª Vara ... da Comarca de X").
    m = re.search(r"Comarca de\s+(.+)", jurisdicao, re.I) or re.search(
        r"Comarca de\s+([^/]+)", orgao, re.I
    )
    comarca_nome = (m.group(1).strip() if m else jurisdicao.strip())
    if comarca_nome and orgao:
        linhas.append(f"Comarca de {comarca_nome} / {orgao}")
    elif orgao:
        linhas.append(orgao)
    if valor:
        linhas.append(f"valor da causa {valor}")

    # Partes dos polos (ignora os advogados, que ficam na sub-árvore <ul.tree>).
    # O réu costuma vir SEM CPF/CNPJ (sigilo) — emitir mesmo assim, senão o polo
    # passivo some e o processo fica só com o autor. Papel ausente ou desconhecido
    # cai no polo do container (ver `_papel_utilizavel`).
    for div_id in ("poloAtivo", "poloPassivo"):
        bloco = soup.find(id=div_id)
        if bloco is None:
            continue
        for td in bloco.select("tbody td"):
            for tree in td.find_all("ul", class_="tree"):
                tree.extract()
            texto = _normaliza(td.get_text(" ", strip=True))
            if not texto:
                continue
            mp = _RX_PARTE_DETALHE.match(texto)
            if mp:
                doc = (mp.group("doc") or "").strip()
                tipo = (mp.group("tipo") or "").upper()
                # TJMG rotula o polo com barra de gênero ("RÉU/RÉ", "AUTOR/AUTORA",
                # "EXEQUENTE/EXEQUENTE"); fica só com o 1º termo, que _RX_PARTE
                # reconhece (senão a parte é descartada ao reparsear a linha).
                papel = _papel_utilizavel(mp.group("papel").split("/")[0].strip(), div_id)
                cab = f"{papel}: {mp.group('nome').strip()}"
                linhas.append(f"{cab} {tipo}: {doc}" if doc else cab)
            elif "(" not in texto:
                # td só com o nome (sem "(PAPEL)"): usa o polo como papel.
                linhas.append(f"{_PAPEL_POLO[div_id]}: {texto}")

    return "\n".join(linhas) if len(linhas) > 1 else ""


# ---------------------------------------------------------------------------
# Cabeçalho — primeiro documento que tiver cada campo "vence".
# ---------------------------------------------------------------------------
def _extrair_cabecalho(blob: str) -> dict:
    classe = assunto = comarca = vara = endereco = valor = ""

    m = _RX_CLASSE.search(blob)
    if m:
        classe = _normaliza(m.group(1))
    else:
        for linha in blob.splitlines():
            ms = _RX_CLASSE_SOLTA.match(linha)
            if ms:
                classe = _normaliza(ms.group(1))
                break

    m = _RX_ASSUNTO.search(blob)
    if m:
        assunto = _normaliza(m.group(1)).strip("[]")

    m = _RX_COMARCA_VARA.search(blob)
    if m:
        comarca = _normaliza(m.group("comarca"))
        vara = _normaliza(m.group("vara"))

    m = _RX_ENDERECO.search(blob)
    if m:
        endereco = _normaliza(m.group(0))

    m = _RX_VALOR.search(blob)
    if m:
        valor = f"R$ {m.group(1)}"

    return {
        "classe": classe,
        "assunto": assunto,
        "comarca": comarca,
        "vara": vara,
        "foro_endereco": endereco,
        "valor_causa": valor,
    }


# ---------------------------------------------------------------------------
# Eventos — parse da timeline (trecho ANTES do 1º marcador de documento).
# ---------------------------------------------------------------------------
def _timeline_texto(blob: str) -> str:
    idx = blob.find("<!-- DOCUMENTO_ORIGEM:")
    return blob[:idx] if idx != -1 else blob


def _extrair_eventos(blob: str) -> tuple[list[dict], str]:
    """Devolve (eventos, data_autuacao). A timeline vem newest-first; cada
    entrada termina num horário HH:MM. Acumula linhas de descrição/documentos
    até o horário e então emite um evento."""
    texto = _timeline_texto(blob)
    eventos: list[dict] = []
    datas_vistas: list[str] = []
    data_atual = ""
    desc_buffer: list[str] = []
    docs_buffer: list[str] = []

    def _flush(hora: str = ""):
        if not (desc_buffer or docs_buffer):
            return
        eventos.append({
            "numero": "",
            "data": data_atual,
            "hora": hora,
            "descricao": _normaliza(" ".join(desc_buffer)),
            "usuario": "",
            "documentos": list(docs_buffer),
        })
        desc_buffer.clear()
        docs_buffer.clear()

    for linha in texto.splitlines():
        linha = linha.strip()
        if not linha:
            continue
        m_data = _RX_DATA_PJE.match(linha)
        if m_data:
            iso = _converter_data(linha)
            if iso:
                datas_vistas.append(iso)
                data_atual = iso
            continue
        if _RX_HORA.match(linha):
            _flush(hora=linha)
            continue
        m_doc = _RX_DOC_LINHA.match(linha)
        if m_doc:
            docs_buffer.append(_normaliza(m_doc.group(2)))
            continue
        desc_buffer.append(linha)
    _flush()  # eventual cauda sem horário

    # data de autuação = a mais antiga vista na timeline (DD/MM/AAAA → chave)
    def _chave(d: str) -> tuple[int, int, int]:
        try:
            dd, mm, aa = d.split("/")
            return (int(aa), int(mm), int(dd))
        except Exception:
            return (0, 0, 0)

    data_autuacao = min(datas_vistas, key=_chave) if datas_vistas else ""
    # descarta entradas sem conteúdo útil (descrição vazia e sem documentos)
    eventos = [ev for ev in eventos if ev["descricao"] or ev["documentos"]]
    return eventos, data_autuacao


# ---------------------------------------------------------------------------
# Atos — documentos abertos cujo tipo é Despacho/Decisão/Sentença/Ato Ord.
# ---------------------------------------------------------------------------
def _extrair_atos(blob: str, classe: str) -> list[dict]:
    atos: list[dict] = []
    for m in _RX_DOCUMENTO_ORIGEM.finditer(blob or ""):
        nome = (m.group("arquivo") or "").strip()
        data = (m.group("data") or "").strip()
        corpo = m.group("corpo") or ""
        m_tipo = _RX_TIPO_ATO.search(nome)
        if not m_tipo:
            continue
        tipo = unidecode(m_tipo.group(1)).upper()
        tipo = {"DECISAO": "DECISÃO", "SENTENCA": "SENTENÇA",
                "ATO ORDINATORIO": "ATO ORDINATÓRIO"}.get(tipo, tipo)
        atos.append({
            "tipo": tipo,
            "classe_acao": classe,
            "teor": _normaliza(corpo)[:4000],
            "signatario": "",
            "data_assinatura": _converter_data(data),
            "codigo_verificador": "",
            "codigo_crc": "",
            "local": "",
            "arquivo": nome,
        })
    return atos


# ---------------------------------------------------------------------------
# Função principal
# ---------------------------------------------------------------------------
def extrair_processo_pje(blob: str, numero_cnj: str = "") -> dict:
    """Blob de texto do `PJEScraper` → dict no formato de
    `parser_processo.extrair_processo()`, pronto para `_dict_para_processo()`."""
    blob = blob or ""
    cab = _extrair_cabecalho(blob)
    partes = _extrair_partes(blob)
    eventos, data_autuacao = _extrair_eventos(blob)
    atos = _extrair_atos(blob, cab["classe"])
    tribunal, uf = _inferir_tribunal_uf(numero_cnj)

    orgao = " — ".join(p for p in (cab["vara"], cab["comarca"]) if p)

    # timeline é newest-first → 1º evento é o mais recente, último o mais antigo.
    datas = [ev["data"] for ev in eventos if ev.get("data")]
    primeira_mov = datas[-1] if datas else data_autuacao
    ultima_mov_data = datas[0] if datas else ""
    ultima_mov_desc = eventos[0]["descricao"] if eventos else ""

    return {
        "numero": numero_cnj,
        "classe": cab["classe"],
        "assunto": cab["assunto"],
        "competencia": "",
        "data_autuacao": data_autuacao,
        "situacao": "",
        "orgao_julgador": orgao,
        "juiz": "",
        "tribunal": tribunal,
        "uf": uf,
        "comarca": cab["comarca"],
        "vara": cab["vara"],
        "foro_endereco": cab["foro_endereco"],
        "valor_causa": cab["valor_causa"],
        "partes": partes,
        "eventos": eventos,
        "atos": atos,
        "total_eventos": len(eventos),
        "primeira_movimentacao": primeira_mov,
        "ultima_movimentacao": {"data": ultima_mov_data, "descricao": ultima_mov_desc},
    }
