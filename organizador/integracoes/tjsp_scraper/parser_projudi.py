# -*- coding: utf-8 -*-
"""Extrator estruturado da página de processo do Projudi TJGO.

Espelha o contrato de `parser_processo.extrair_processo()` (eproc): devolve um
dict aninhado pronto para `organizacao._dict_para_processo()`.

Estrutura confirmada no HTML real (processo 5238949-70.2018.8.09.0162, sessão
autenticada de 18/08/2026). O Projudi não usa ids nos campos: quase tudo é o par
``<div>Rótulo</div> <span>Valor</span>`` dentro de ``<fieldset class="VisualizaDados">``,
o que torna a extração por RÓTULO (e não por posição) a única forma estável.

  - número:     ``<span id="span_proc_numero">``  (único id útil do cabeçalho)
  - área:       fieldset "Autos", par ``Área``
  - partes:     um fieldset por polo, com ``<legend>Polo Ativo | Autor</legend>``;
                dentro, os pares ``Nome`` / ``CPF/CNPJ`` e um
                ``<fieldset class="fieldsetEndereco">`` com o endereço.
                ATENÇÃO: esses fieldsets são ANINHADOS dentro do fieldset
                "Dados do Processo", que por sua vez também casa o seletor —
                por isso filtramos pela legend começando em "Polo".
  - cabeçalho:  fieldset "Outras Informações" — Serventia, Classe, Assunto(s),
                Valor da Causa, Fase Processual, Dt. Distribuição, Status,
                Segredo de Justiça, Prioridade, Custas, Penhora no Rosto...
  - movimentos: ``<tbody id="tabListaProcesso">``, linhas ``tr.filtro-entrada``
                (as ``tr#linha_*`` intercaladas são placeholders OCULTOS do
                carregamento sob demanda dos arquivos — se contadas, dobram o
                total de eventos com linhas vazias). Colunas: nº, tipo +
                complemento, data, usuário. Vêm do mais RECENTE para o antigo.

O Projudi não expõe advogados nem OAB no corpo da página (ficam atrás do menu
"Ver Advogados", que é uma ida extra ao servidor por processo) — como no e-SAJ
público, `advogados` sai vazio. O juiz também não é campo de cabeçalho: aparece
apenas no texto da movimentação "Processo Redistribuído", e por isso não é
extraído (seria um palpite frágil).
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from .parser_processo import _inferir_tribunal_uf, _normaliza

# "Polo Ativo | Autor" -> ("AUTOR"); "Polo Passivo | Réu" -> ("RÉU").
_RX_POLO = re.compile(r"polo\s+(ativo|passivo)\s*\|\s*(.+)", re.I)

# "23/05/2018 00:00:00" -> data e hora separadas.
_RX_DATA_HORA = re.compile(r"(\d{2}/\d{2}/\d{4})(?:\s+(\d{2}:\d{2}(?::\d{2})?))?")

# Rótulos cujo valor "Sim"/"Não" vira tag quando afirmativo.
_TAGS_POR_ROTULO = {
    "segredo de justiça": "Segredo de Justiça",
    "efeito suspensivo": "Efeito Suspensivo",
    "penhora no rosto": "Penhora no Rosto dos Autos",
    "julgado 2º grau": "Julgado em 2º Grau",
}


def _rotulo(txt: str) -> str:
    """Chave de comparação de rótulo: sem espaços extras, minúsculo, sem ':'."""
    return _normaliza(txt).rstrip(":").strip().lower()


def _pares(escopo: Tag) -> dict[str, str]:
    """Mapa ``rótulo -> valor`` dos pares ``<div>Rótulo</div><span>Valor</span>``.

    Só considera ``div`` FOLHA (sem div dentro): os wrappers de layout
    (``div.aEsquerda``, ``div.divBotoesDireita``) casariam como rótulo e
    roubariam o ``<span>`` do par seguinte. Também ignora os ``div.DivInvisivel``,
    que carregam formulários vazios preenchidos por AJAX.

    Não desce em fieldsets aninhados: cada bloco (Autos, cada polo, Outras
    Informações) é lido no seu próprio escopo, senão os rótulos de um polo
    apareceriam no mapa do outro.
    """
    saida: dict[str, str] = {}
    for div in escopo.find_all("div"):
        classes = div.get("class") or []
        if "DivInvisivel" in classes or div.find("div") is not None:
            continue
        if div.find_parent("fieldset") is not escopo:
            continue
        chave = _rotulo(div.get_text(" ", strip=True))
        if not chave:
            continue
        span = div.find_next_sibling("span")
        if span is None:
            continue
        valor = _normaliza(span.get_text(" ", strip=True))
        # 1ª ocorrência vence: o Projudi repete alguns rótulos em blocos ocultos.
        saida.setdefault(chave, valor)
    return saida


def _folha_hierarquia(valor: str) -> str:
    """'40 - PROCESSO CÍVEL -> ... -> Monitória' -> 'Monitória'.

    Classe e assunto vêm como caminho completo da tabela do CNJ. O último nível
    é o que identifica o processo; o caminho inteiro (200+ chars) não cabe nos
    campos que a petição usa."""
    partes = [p.strip(" -–") for p in re.split(r"->", valor or "")]
    partes = [p for p in partes if p]
    return partes[-1] if partes else ""


def _fieldset_por_legenda(soup: BeautifulSoup, alvo: str) -> Tag | None:
    for fs in soup.find_all("fieldset"):
        leg = fs.find("legend")
        if leg is not None and _rotulo(leg.get_text(" ", strip=True)) == alvo:
            return fs
    return None


# ---------------------------------------------------------------------------
# Partes
# ---------------------------------------------------------------------------
def _extrair_partes(soup: BeautifulSoup) -> list[dict]:
    partes: list[dict] = []
    for fs in soup.find_all("fieldset"):
        leg = fs.find("legend")
        if leg is None:
            continue
        m = _RX_POLO.match(_normaliza(leg.get_text(" ", strip=True)))
        if not m:
            continue
        papel = _normaliza(m.group(2)).upper()
        # O bloco de cada parte é o fieldset interno; um polo pode ter vários.
        internos = fs.find_all("fieldset") or [fs]
        for bloco in internos:
            if (bloco.get("class") or []) == ["fieldsetEndereco"]:
                continue
            campos = _pares(bloco)
            nome = campos.get("nome", "")
            if not nome:
                continue
            documento = campos.get("cpf/cnpj", "")
            partes.append({
                "papel": papel,
                "nome": nome,
                "documento": documento,
                # O Projudi não rotula o tipo; o CNPJ tem 14 dígitos, o CPF 11.
                "tipo": _tipo_por_documento(documento),
                "advogados": [],
            })
    return partes


def _tipo_por_documento(documento: str) -> str:
    digitos = re.sub(r"\D", "", documento or "")
    if len(digitos) == 14:
        return "Pessoa Jurídica"
    if len(digitos) == 11:
        return "Pessoa Física"
    return ""


# ---------------------------------------------------------------------------
# Movimentações
# ---------------------------------------------------------------------------
def _extrair_eventos(soup: BeautifulSoup) -> list[dict]:
    corpo = soup.find("tbody", id="tabListaProcesso")
    if corpo is None:
        return []
    eventos: list[dict] = []
    for tr in corpo.find_all("tr"):
        classes = tr.get("class") or []
        if "filtro-entrada" not in classes:
            continue  # placeholder oculto de arquivos (tr#linha_*)
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 4:
            continue
        numero = _normaliza(tds[0].get_text(" ", strip=True))
        tipo_span = tds[1].find("span", class_="filtro_tipo_movimentacao")
        tipo = _normaliza(tipo_span.get_text(" ", strip=True)) if tipo_span else ""
        texto_celula = _normaliza(tds[1].get_text(" ", strip=True))
        # O complemento é o que sobra da célula depois do tipo.
        complemento = _normaliza(texto_celula[len(tipo):]) if tipo else texto_celula
        descricao = f"{tipo} — {complemento}" if (tipo and complemento) else (tipo or complemento)

        m = _RX_DATA_HORA.search(_normaliza(tds[2].get_text(" ", strip=True)))
        data, hora = (m.group(1), m.group(2) or "") if m else ("", "")

        eventos.append({
            "numero": numero,
            "data": data,
            "hora": hora,
            "descricao": descricao,
            "usuario": _normaliza(tds[3].get_text(" ", strip=True)),
            "documentos": [],
        })
    return eventos


# ---------------------------------------------------------------------------
# Função principal
# ---------------------------------------------------------------------------
def extrair_processo_projudi(html: str, numero_cnj: str = "") -> dict:
    """HTML da página do processo no Projudi TJGO → dict no mesmo formato de
    `parser_processo.extrair_processo()`, pronto para `_dict_para_processo()`."""
    soup = BeautifulSoup(html or "", "html.parser")

    span_num = soup.find("span", id="span_proc_numero")
    numero = _normaliza(span_num.get_text(" ", strip=True)) if span_num else ""
    numero = numero or numero_cnj

    autos = _fieldset_por_legenda(soup, "autos")
    area = _pares(autos).get("área", "") if autos else ""

    outras = _fieldset_por_legenda(soup, "outras informações")
    info = _pares(outras) if outras else {}

    serventia = info.get("serventia", "")
    # "Valparaíso de Goiás - UPJ Varas Cíveis: 1ª, 2ª e 4ª" -> comarca / vara.
    comarca, _, vara = (p.strip() for p in serventia.partition(" - "))

    valor = info.get("valor da causa", "")
    valor_causa = f"R$ {valor}" if valor else ""

    m = _RX_DATA_HORA.search(info.get("dt. distribuição", ""))
    data_autuacao = m.group(1) if m else ""

    tribunal, uf = _inferir_tribunal_uf(numero_cnj or numero)

    tags: list[str] = []
    for rotulo, tag in _TAGS_POR_ROTULO.items():
        if _rotulo(info.get(rotulo, "")) == "sim":
            tags.append(tag)
    prioridade = info.get("prioridade", "")
    # "Metas CNJ / Normal" é o estado default — só vira tag se houver prioridade
    # real (Idoso, Doença Grave, Menor...).
    for pedaco in (p.strip() for p in prioridade.split("/")):
        if pedaco and _rotulo(pedaco) not in ("normal", "metas cnj", ""):
            tags.append(pedaco)

    partes = _extrair_partes(soup)
    eventos = _extrair_eventos(soup)

    # A tabela do Projudi vem do mais recente para o mais antigo.
    datas = [ev["data"] for ev in eventos if ev.get("data")]
    return {
        "numero": numero,
        "classe": _folha_hierarquia(info.get("classe", "")),
        "assunto": _folha_hierarquia(info.get("assunto(s)", "")),
        "competencia": area,
        "data_autuacao": data_autuacao,
        "situacao": info.get("status", ""),
        "orgao_julgador": serventia,
        "juiz": "",
        "tribunal": tribunal,
        "uf": uf,
        "comarca": comarca,
        "vara": vara,
        "valor_causa": valor_causa,
        "tags": tags,
        "partes": partes,
        "eventos": eventos,
        "total_eventos": len(eventos),
        "primeira_movimentacao": datas[-1] if datas else "",
        "ultima_movimentacao": {
            "data": datas[0] if datas else "",
            "descricao": eventos[0]["descricao"] if eventos else "",
        },
    }
