# -*- coding: utf-8 -*-
"""Extrator estruturado da página de processo do e-SAJ TJSP (cpopg/show.do).

Espelha o contrato de `parser_processo.extrair_processo()` (eproc): devolve um
dict aninhado pronto para `organizacao._dict_para_processo()`. Diferente do
eproc, o e-SAJ público (cpopg) NÃO expõe CPF/CNPJ das partes, número da OAB
nem teor dos atos — então `documento`, `oab` e `atos` saem vazios. O que o
e-SAJ entrega bem: cabeçalho (classe, assunto, foro, vara, juiz, valor),
partes com papel + advogados (nomes) e a lista completa de movimentações.

Estrutura confirmada no HTML real (processo 1102493-63.2022.8.26.0100):
  - cabeçalho por id: #classeProcesso, #assuntoProcesso, #foroProcesso,
    #varaProcesso, #juizProcesso, #areaProcesso, #valorAcaoProcesso,
    #dataHoraDistribuicaoProcesso, #situacaoProcesso (às vezes ausente).
  - partes em #tableTodasPartes (mais completa que #tablePartesPrincipais):
    cada <tr> tem <span class="tipoDeParticipacao"> (abreviação do papel) e
    uma <td class="nomeParteEAdvogado"> com o nome seguido de rótulos
    "Advogado:"/"Advogada:"/"Soc. Advogados:" e os respectivos nomes.
  - movimentações em #tabelaTodasMovimentacoes: <tr> com td.dataMovimentacao
    e td.descricaoMovimentacao (a tabela já vem do mais recente p/ o antigo).
"""

from __future__ import annotations

import re
from typing import Optional

from bs4 import BeautifulSoup, Tag
from unidecode import unidecode

from .parser_processo import _inferir_tribunal_uf, _normaliza


# Abreviações de papel do e-SAJ → forma canônica usada pelo sistema
# (organizador.padroes.PAPEIS_ATIVO / PAPEIS_PASSIVO). A chave é normalizada
# (sem acento, só letras, minúscula) antes da busca.
_PAPEL_ESAJ = {
    "reqte": "REQUERENTE", "reqda": "REQUERIDO", "reqdo": "REQUERIDO",
    "exeqte": "EXEQUENTE", "exeqdo": "EXECUTADO",
    "exectdo": "EXECUTADO", "exectda": "EXECUTADO",
    "autor": "AUTOR", "autora": "AUTOR",
    "reu": "RÉU", "re": "RÉU",
    "embargte": "EMBARGANTE", "embargdo": "EMBARGADO", "embargda": "EMBARGADO",
    "apte": "APELANTE", "apelante": "APELANTE",
    "apdo": "APELADO", "apda": "APELADO", "apelado": "APELADO",
    "agravante": "AGRAVANTE", "agravado": "AGRAVADO", "agravada": "AGRAVADO",
    "imptte": "IMPETRANTE", "imptdo": "IMPETRADO",
    "recte": "RECORRENTE", "recdo": "RECORRIDO",
    "terintcer": "TERCEIRO INTERESSADO", "terint": "TERCEIRO INTERESSADO",
    "interessado": "INTERESSADO", "interessada": "INTERESSADO",
    "vitima": "VÍTIMA",
}

# Rótulos que, na célula da parte, introduzem nome(s) de advogado.
_LABELS_ADV = {"advogado:", "advogada:", "advogados:", "advogadas:",
               "soc. advogados:", "sociedade de advogados:"}

_RX_VALOR = re.compile(r"R\$\s*([\d\.\,]+)")


def _txt_por_id(soup: BeautifulSoup, elem_id: str) -> str:
    el = soup.find(id=elem_id)
    return _normaliza(el.get_text(" ", strip=True)) if el else ""


def _canonizar_papel(abrev: str) -> str:
    """'Exeqte' → 'EXEQUENTE'; desconhecido volta como veio (sem alterar)."""
    chave = re.sub(r"[^a-z]", "", unidecode(abrev or "").lower())
    return _PAPEL_ESAJ.get(chave, _normaliza(abrev).upper())


def _normaliza_valor(bruto: str) -> str:
    """'R$         77.508,14' → 'R$ 77.508,14'."""
    m = _RX_VALOR.search(bruto or "")
    return f"R$ {m.group(1)}" if m else ""


# ---------------------------------------------------------------------------
# Partes
# ---------------------------------------------------------------------------
def _parse_celula_parte(cell: Tag) -> tuple[str, list[dict]]:
    """Devolve (nome_da_parte, advogados). A célula traz o nome e, após os
    rótulos "Advogado(a):"/"Soc. Advogados:", os nomes dos advogados. O e-SAJ
    público não expõe OAB, então oab/uf saem vazios."""
    linhas = [s.strip() for s in cell.get_text("\n", strip=True).split("\n") if s.strip()]
    nome_partes: list[str] = []
    advogados: list[dict] = []
    vistos: set[str] = set()
    modo = "nome"
    for ln in linhas:
        low = ln.lower()
        if low.endswith(":"):
            modo = "adv" if low in _LABELS_ADV else "outro"
            continue
        if modo == "nome":
            nome_partes.append(ln)
        elif modo == "adv":
            chave = unidecode(ln).upper()
            if chave not in vistos:
                vistos.add(chave)
                advogados.append({"nome": ln, "oab": "", "uf": ""})
    return _normaliza(" ".join(nome_partes)), advogados


def _extrair_partes(soup: BeautifulSoup) -> list[dict]:
    # #tableTodasPartes é mais completa; cai para #tablePartesPrincipais.
    tabela = soup.find(id="tableTodasPartes") or soup.find(id="tablePartesPrincipais")
    if not tabela:
        return []
    partes: list[dict] = []
    for tr in tabela.find_all("tr"):
        sp = tr.find("span", class_="tipoDeParticipacao")
        cell = tr.find("td", class_="nomeParteEAdvogado")
        if not sp or not cell:
            continue
        nome, advogados = _parse_celula_parte(cell)
        if not nome:
            continue
        partes.append({
            "papel": _canonizar_papel(sp.get_text(" ", strip=True)),
            "nome": nome,
            "documento": "",
            "tipo": "",
            "advogados": advogados,
        })
    return partes


# ---------------------------------------------------------------------------
# Movimentações
# ---------------------------------------------------------------------------
def _extrair_eventos(soup: BeautifulSoup) -> list[dict]:
    tabela = soup.find(id="tabelaTodasMovimentacoes") or soup.find(id="tabelaMovimentacoes")
    if not tabela:
        return []
    eventos: list[dict] = []
    for tr in tabela.find_all("tr"):
        td_data = tr.find("td", class_="dataMovimentacao")
        td_desc = tr.find("td", class_="descricaoMovimentacao")
        if td_desc is None:  # linha de cabeçalho / sem descrição
            continue
        data = _normaliza(td_data.get_text(" ", strip=True)) if td_data else ""
        desc = _normaliza(td_desc.get_text(" ", strip=True))
        if not (data or desc):
            continue
        eventos.append({
            "numero": "",
            "data": data,
            "hora": "",
            "descricao": desc,
            "usuario": "",
            "documentos": [],
        })
    return eventos


# ---------------------------------------------------------------------------
# Função principal
# ---------------------------------------------------------------------------
def extrair_processo_esaj(html: str, numero_cnj: str = "") -> dict:
    """HTML da página do processo no e-SAJ (cpopg) → dict no mesmo formato de
    `parser_processo.extrair_processo()`, pronto para `_dict_para_processo()`."""
    soup = BeautifulSoup(html or "", "html.parser")

    numero = _txt_por_id(soup, "numeroProcesso") or numero_cnj
    classe = _txt_por_id(soup, "classeProcesso")
    assunto = _txt_por_id(soup, "assuntoProcesso")
    area = _txt_por_id(soup, "areaProcesso")
    foro = _txt_por_id(soup, "foroProcesso")
    vara = _txt_por_id(soup, "varaProcesso")
    juiz = _txt_por_id(soup, "juizProcesso")
    situacao = _txt_por_id(soup, "situacaoProcesso")
    valor = _normaliza_valor(_txt_por_id(soup, "valorAcaoProcesso"))
    distribuicao = _txt_por_id(soup, "dataHoraDistribuicaoProcesso")

    orgao = " — ".join(p for p in (vara, foro) if p)
    tribunal, uf = _inferir_tribunal_uf(numero_cnj or numero)

    partes = _extrair_partes(soup)
    eventos = _extrair_eventos(soup)

    # A tabela do e-SAJ vem do mais recente para o mais antigo.
    datas = [ev["data"] for ev in eventos if ev.get("data")]
    primeira_mov = datas[-1] if datas else ""
    ultima_mov_data = datas[0] if datas else ""
    ultima_mov_desc = eventos[0]["descricao"] if eventos else ""

    return {
        "numero": numero,
        "classe": classe,
        "assunto": assunto,
        "competencia": area,
        "data_autuacao": distribuicao,
        "situacao": situacao,
        "orgao_julgador": orgao,
        "juiz": juiz,
        "tribunal": tribunal,
        "uf": uf,
        "comarca": foro,
        "vara": vara,
        "valor_causa": valor,
        "partes": partes,
        "eventos": eventos,
        "total_eventos": len(eventos),
        "primeira_movimentacao": primeira_mov,
        "ultima_movimentacao": {"data": ultima_mov_data, "descricao": ultima_mov_desc},
    }
