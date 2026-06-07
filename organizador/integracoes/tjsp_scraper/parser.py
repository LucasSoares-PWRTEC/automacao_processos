from bs4 import BeautifulSoup
import re
from unidecode import unidecode

RX_DATA = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")
RX_AR_POSITIVO = re.compile(r"\bar\s*positivo\s*juntado\b", re.I)

# Texto EXATO que dispara o campo de penhora/bloqueio
# (já em minúsculas e sem acento, para casar com _norm)
TEXTO_BLOQUEIO_PENHORA = "bloqueio/penhora on line - positivo juntado"

def _norm(txt: str) -> str:
    return unidecode((txt or "").strip().lower())

def _data_da_mesma_linha(tr) -> str:
    td = tr.find("td", class_="dataMovimentacao") or tr.find("td", class_="data") or tr.find("td")
    if td:
        m = RX_DATA.search(td.get_text(" ", strip=True))
        if m:
            d, mm, a = m.groups()
            return f"{d}/{mm}/{a}"
    return ""

def extrair_dados(html):
    dados = {
        "citacao": "",
        "data_citacao": "",
        "penhora": "",
        "tipo_penhora": "",
        "penhora_bloqueio_data": "",
    }

    if not html:
        return dados

    soup = BeautifulSoup(html, "html.parser")
    container = soup.find(id="tabelaTodasMovimentacoes") or soup.find(id="tabelaMovimentacoes")
    if not container:
        return dados

    linhas = container.find_all("tr")

    # --- 1) Coletar todas as linhas com AR Positivo Juntado ---
    trs_ar_positivo = []

    # Coletar todas as linhas do evento exato de Bloqueio/Penhora on line - Positivo Juntado
    trs_bloqueio_penhora = []

    for tr in linhas:
        desc_td = tr.find("td", class_="descricaoMovimentacao") or tr.find("td")
        texto_desc = desc_td.get_text(" ", strip=True) if desc_td else tr.get_text(" ", strip=True)
        if not texto_desc:
            continue

        texto_norm = _norm(texto_desc)

        # Guardar linhas com AR Positivo Juntado
        if RX_AR_POSITIVO.search(texto_norm):
            trs_ar_positivo.append(tr)

        # ---- NOVA REGRA: Bloqueio/Penhora on line - Positivo Juntado ----
        # Só consideramos penhora/bloqueio se a descrição tiver EXATAMENTE esse texto
        if TEXTO_BLOQUEIO_PENHORA in texto_norm:
            trs_bloqueio_penhora.append(tr)

    # --- 2) Escolher a MAIS ANTIGA entre as AR Positivo Juntado ---
    if trs_ar_positivo:
        # se a tabela estiver do mais novo para o mais antigo,
        # a MAIS ANTIGA é o ÚLTIMO tr da lista
        tr_escolhido = trs_ar_positivo[-1]
        data_citacao = _data_da_mesma_linha(tr_escolhido)

        dados["citacao"] = "Positivo"
        dados["data_citacao"] = data_citacao or ""
    else:
        dados["citacao"] = "Não encontrado"
        dados["data_citacao"] = ""

    # --- 3) Consolidar penhora/bloqueio com a NOVA REGRA ---
    if trs_bloqueio_penhora:
        # Mesma lógica da citação: considerando a tabela em ordem do mais novo para o mais antigo,
        # a ocorrência mais antiga é a última encontrada.
        tr_bloqueio_escolhido = trs_bloqueio_penhora[-1]
        data_penhora_bloqueio = _data_da_mesma_linha(tr_bloqueio_escolhido)
        dados["penhora"] = "Sim"
        dados["tipo_penhora"] = "Bloqueio/Penhora on line - Positivo Juntado"
        dados["penhora_bloqueio_data"] = data_penhora_bloqueio or ""
    else:
        dados["penhora"] = "Não localizado"
        dados["tipo_penhora"] = ""
        dados["penhora_bloqueio_data"] = ""

    return dados


# ---------------------------------------------------------------------------
# PARSER GENÉRICO – Tribunais_automacao
# Busca palavras-chave de penhora/bloqueio no texto completo da página.
# Termos mais longos têm prioridade (match mais específico primeiro).
# ---------------------------------------------------------------------------

_PALAVRAS_PENHORA = sorted(
    [
        "Penhora realizada",
        "Penhora deferida",
        "Penhora positiva",
        "Bloqueio realizado",
        "Bloqueio deferido",
        "Bloqueio positivo",
        "Valores bloqueados",
        "Valor bloqueado",
        "Mandado de pagamento",
        "Mandado de levantamento",
        "MLE",
        "Penhora",
        "Bloqueio",
    ],
    key=lambda t: len(t),
    reverse=True,  # mais longos primeiro para evitar match parcial prematuro
)

# Pré-compila os padrões normalizados (sem acento, minúsculas)
_PADROES_PENHORA = [
    (re.compile(r"\b" + re.escape(_norm(p)) + r"\b"), p)
    for p in _PALAVRAS_PENHORA
]
# Marcador padrão usado por TODOS os scrapers que querem identificar a
# origem de uma keyword (qual arquivo / qual evento / qual data / qual id):
#   <!-- DOCUMENTO_ORIGEM: NOME | EVENTO: 56 | DATA: 26/03/2026 15:16:58 | ID: 10554398891 -->
# eproc emite EVENTO e DATA. PJe emite DATA e ID. Marcador antigo (sem
# pipes) era só o nome — a regex abaixo casa todos os formatos (EVENTO,
# DATA, ID ficam '' quando ausentes).
_RX_DOCUMENTO_ORIGEM = re.compile(
    r"<!--\s*DOCUMENTO_ORIGEM:\s*(?P<arquivo>.*?)\s*"
    r"(?:\|\s*EVENTO:\s*(?P<evento>.*?)\s*)?"
    r"(?:\|\s*DATA:\s*(?P<data>.*?)\s*)?"
    r"(?:\|\s*ID:\s*(?P<doc_id>.*?)\s*)?"
    r"-->\s*(?P<corpo>.*?)(?=<!--\s*DOCUMENTO_ORIGEM:|\Z)",
    re.S,
)


def _extrair_blocos_com_origem(html: str) -> list[tuple[str, str, str, str, str]]:
    """
    Se o scraper marcou blocos de documentos, retorna
    (arquivo, evento, data, doc_id, html_do_bloco). Caso contrario,
    preserva o comportamento antigo: analisa a pagina inteira sem origem.
    """
    blocos = []
    for m in _RX_DOCUMENTO_ORIGEM.finditer(html or ""):
        arquivo = (m.group("arquivo") or "").strip()
        evento = (m.group("evento") or "").strip()
        data = (m.group("data") or "").strip()
        doc_id = (m.group("doc_id") or "").strip()
        corpo = m.group("corpo") or ""
        blocos.append((arquivo, evento, data, doc_id, corpo))
    return blocos or [("", "", "", "", html)]


def extrair_penhora_bloqueio_tribunais(html: str) -> list:
    """
    Varre o texto completo da página e retorna uma lista de dicts,
    um por palavra-chave encontrada. Se nenhuma for encontrada,
    retorna uma lista com um único dict vazio.

    Cada dict:
      - penhora_bloqueio: "SIM"
      - descricao: a palavra-chave encontrada
      - arquivo:  nome do documento de origem (ex.: "ATOORD1") ou ""
      - evento:   número do evento (ex.: "56", só eproc) ou ""
      - data:     data/hora do evento (ex.: "26/03/2026 15:16:58" no
                  eproc, "06 out 2025" no PJe) ou ""
      - id:       id do documento (ex.: "10554398891", só PJe) ou ""

    Esses campos só são preenchidos para keywords detectadas DENTRO de
    um bloco marcado por <!-- DOCUMENTO_ORIGEM: ... -->.
    """
    vazio = {
        "penhora_bloqueio": "",
        "descricao":        "",
        "arquivo":          "",
        "evento":           "",
        "data":             "",
        "id":               "",
    }
    if not html:
        return [vazio.copy()]

    encontrados = []
    vistos = set()

    for arquivo, evento, data, doc_id, bloco_html in _extrair_blocos_com_origem(html):
        soup = BeautifulSoup(bloco_html, "html.parser")
        texto_pagina = _norm(soup.get_text(" ", strip=True))

        for padrao, termo_original in _PADROES_PENHORA:
            if not padrao.search(texto_pagina):
                continue
            chave = (termo_original, arquivo)
            if chave in vistos:
                continue
            vistos.add(chave)
            encontrados.append({
                "penhora_bloqueio": "SIM",
                "descricao":        termo_original,
                "arquivo":          arquivo,
                "evento":           evento,
                "data":             data,
                "id":               doc_id,
            })

    return encontrados if encontrados else [vazio.copy()]
