# -*- coding: utf-8 -*-
"""
Organizador de Processos - eproc/TJSP
Lê PDFs em uma pasta, identifica o Nº do processo (padrão CNJ),
cria subpastas por processo, move os documentos e extrai metadados
(cabeçalho, partes, eventos) para arquivos JSON e TXT.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import csv
import hashlib
import io
import itertools
import json
import os
import queue
import re
import shutil
import sys
import threading
import tkinter as tk
import unicodedata
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Iterable

from docx import Document
from docx.enum.text import WD_COLOR_INDEX
from docx.shared import Pt
from pypdf import PdfReader


PASTA_PADRAO = r"C:\Users\Usuario\Desktop\Processos"
PASTA_NAO_IDENTIFICADOS = "_nao_identificados"
PASTA_PETICOES = Path(__file__).parent / "Petições"
CACHE_DIR = ".cache_texto"
CACHE_TEMPLATES_DOCX = Path(__file__).parent / ".cache_templates_docx"

# Projeto TJSP-Scraper (busca online de processos). Importado de forma tardia
# em buscar_processo_online() — a falta das libs do scraper não derruba o app.
TJSP_SCRAPER_DIR = Path(r"C:\Users\Usuario\Desktop\Workspace\TJSP-Scraper")

# Rótulo do seletor de TJ/Sistema -> (site, estado, url). e-SAJ e PJe têm a URL
# de login fixa dentro da própria classe do scraper (ignoram a url passada); só
# o eproc usa a url. Apenas o eproc tem extrator estruturado (auto-preenche a
# petição); e-SAJ/PJe entram com Processo mínimo + palavras-chave de penhora.
TJ_SISTEMAS = {
    "TJSP — eproc": ("eproc", "SAO PAULO",    "https://eproc1g.tjsp.jus.br/eproc/"),
    "TJSP — e-SAJ": ("esaj",  "SAO PAULO",    "https://esaj.tjsp.jus.br/cpopg/open.do"),
    "TJMG — eproc": ("eproc", "MINAS GERAIS", "https://eproc1g.tjmg.jus.br/eproc/"),
    "TJMG — PJe":   ("pje",   "MINAS GERAIS", "https://pje.tjmg.jus.br/pje/"),
}
_ESTADO_TJ_UF = {"SAO PAULO": ("TJSP", "SP"), "MINAS GERAIS": ("TJMG", "MG")}

# Placeholder em template: "(texto em minúsculo até 40 chars, sem pontuação)".
# Pega tanto os conhecidos ("(secretaria)") quanto novos ("(testemunha)"),
# mas ignora parentéticas naturais com pontuação ou maiúsculas mistas.
RE_PLACEHOLDER_TEMPLATE = re.compile(
    r"\(([a-záéíóúâêôãõçñü][a-záéíóúâêôãõçñü ]{1,38}[a-záéíóúâêôãõçñü])\)"
)

# Código do tribunal no CNJ (.J.TR.) → sigla UF. Cobre justiça estadual (J=8).
# Outras justiças (federal, trabalhista) ficam vazias — usuário preenche manual.
MAPA_UF_CNJ = {
    "8.01": "AC", "8.02": "AL", "8.03": "AP", "8.04": "AM", "8.05": "BA",
    "8.06": "CE", "8.07": "DF", "8.08": "ES", "8.09": "GO", "8.10": "MA",
    "8.11": "MT", "8.12": "MS", "8.13": "MG", "8.14": "PA", "8.15": "PB",
    "8.16": "PR", "8.17": "PE", "8.18": "PI", "8.19": "RJ", "8.20": "RN",
    "8.21": "RS", "8.22": "RO", "8.23": "RR", "8.24": "SC", "8.25": "SE",
    "8.26": "SP", "8.27": "TO",
}

# Mapa de tribunal a partir do CNJ (J=8 → estadual; mesmo TR. da MAPA_UF_CNJ).
MAPA_TRIBUNAL_CNJ = {uf_code: f"TJ{uf}" for uf_code, uf in MAPA_UF_CNJ.items()}

# OAB do advogado do escritório (Felipe Machado) por UF — usado para
# autopreencher os campos OAB UF / OAB Nº ao selecionar um processo.
OAB_ESCRITORIO_POR_UF: dict[str, str] = {
    "AM": "A 1543",
    "BA": "49.510",
    "CE": "37.928-A",
    "DF": "55.703",
    "ES": "26.487",
    "GO": "39.882-A",
    "MG": "104.784",
    "MS": "22.345-A",
    "MT": "21.457-A",
    "PE": "45.442",
    "PR": "70.339",
    "RJ": "186.328",
    "RN": "01.049-A",
    "SC": "51.064",
    "SP": "346.433",
}

# Padrão CNJ: NNNNNNN-DD.AAAA.J.TR.OOOO  (ex.: 4004459-04.2026.8.26.0248)
RE_NUMERO_CNJ = re.compile(r"\b\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}\b")

# Segmento .J.TR. do CNJ usado para inferir tribunal/UF.
RE_CNJ_TR = re.compile(r"\.(\d)\.(\d{2})\.")

# Cabeçalho: cada campo aparece na forma "Rótulo:\nValor" ou "Rótulo\nValor".
CAMPOS_CABECALHO = [
    ("numero", r"N[º°]\s*do\s*processo"),
    ("classe", r"Classe\s+da\s+a[çc][ãa]o:?"),
    ("competencia", r"Compet[êe]ncia:?"),
    ("data_autuacao", r"Data\s+de\s+autua[çc][ãa]o:?"),
    ("situacao", r"Situa[çc][ãa]o:?"),
    ("orgao_julgador", r"[ÓO]rg[ãa]o\s+Julgador:?"),
    ("juiz", r"Juiz\(a\):?"),
]

# Tags "decorativas" que aparecem no topo dos detalhes (Idoso, Processo Migrado etc.)
TAGS_CONHECIDAS = ["Idoso", "Processo Migrado", "Prioridade", "Sigilo", "Segredo de Justiça"]

# Cabeçalho do bloco "Partes e Representantes" varia em função da classe:
# AUTOR/RÉU (comum cível), EXEQUENTE/EXECUTADO (execução), REQUERENTE/REQUERIDO, EMBARGANTE/EMBARGADO.
PAPEIS_ATIVO = ["AUTOR", "EXEQUENTE", "REQUERENTE", "EMBARGANTE", "AGRAVANTE", "IMPETRANTE", "RECLAMANTE"]
PAPEIS_PASSIVO = ["RÉU", "REU", "EXECUTADO", "REQUERIDO", "EMBARGADO", "AGRAVADO", "IMPETRADO", "RECLAMADO"]

RE_CABECALHO_PARTES = re.compile(
    r"\b(" + "|".join(PAPEIS_ATIVO) + r")\b\s+\b(" + "|".join(PAPEIS_PASSIVO) + r")\b",
    re.IGNORECASE,
)

# Marcadores de seção. Adicionamos |\Z como fallback no terminador para evitar
# backtracking exponencial em PDFs sem "Informações Adicionais"/"Ações".
RE_BLOCO_PARTES = re.compile(
    r"Partes\s+e\s+Representantes\s*\n(.*?)(?:\n\s*(?:Informa[çc][õo]es\s+Adicionais|A[çc][õo]es\b)|\Z)",
    re.DOTALL | re.IGNORECASE,
)
RE_CABECALHO_TABELA_EVENTOS = re.compile(
    r"Evento\s+Data/Hora\s+Descri[çc][ãa]o\s+Usu[áa]rio\s+Documentos\s*\n",
    re.IGNORECASE,
)
RE_FIM_TABELA_EVENTOS = re.compile(
    r"\n\s*(?:N[º°]\s*do\s*processo|Filtros\s+Pesquisar|Carregar\s+TODOS|"
    r"Carregar\s+os\s+eventos\s+da|https?://eproc|\d+\s*/\s*\d+\s*$)",
    re.IGNORECASE | re.MULTILINE,
)

RE_LINHA_EVENTO = re.compile(
    r"^(?P<num>\d+)\s+(?P<data>\d{2}/\d{2}/\d{4})\s+(?P<hora>\d{2}:\d{2}:\d{2})\s+(?P<resto>.*)$"
)
RE_DATA_HORA_SOZINHA = re.compile(r"^(\d{2}/\d{2}/\d{4})\s*$|^(\d{2}:\d{2}:\d{2})\s*$")

# Advogado dentro do bloco de partes: "MARCELO CANDIOTTO FREIRE   MG104784" (UF + 4-7 dígitos + sufixo opcional).
RE_ADVOGADO = re.compile(
    r"([A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-ZÁÉÍÓÚÂÊÔÃÕÇa-záéíóúâêôãõç.' -]{4,}?)"
    r"\s+([A-Z]{2})(\d{4,7}[A-Z]?)\b"
)

# Bloco "Prazo: N dias Status: ABERTO/FECHADO" no início de uma linha do evento.
# Mantida sem `.*?` para evitar catastrophic backtracking — datas inicial/final
# são extraídas separadamente em linhas seguintes.
RE_PRAZO_HEAD = re.compile(
    r"Prazo:\s*(?P<dias>\d+)\s*dias?\s*Status:\s*(?P<status>ABERTO|FECHADO)",
    re.IGNORECASE,
)
RE_PRAZO_DATA_INICIAL = re.compile(r"Data\s+inicial[^:]*:\s*(\d{2}/\d{2}/\d{4})", re.IGNORECASE)
RE_PRAZO_DATA_FINAL = re.compile(r"Data\s+final:\s*(\d{2}/\d{2}/\d{4})", re.IGNORECASE)

RE_REFER_EVENTO = re.compile(r"Refer\.\s*ao\s*Evento[s]?\s*:?\s*([\d,\s]+)", re.IGNORECASE)

# Token de documento (uma linha inteira ou um token isolado).
# Ex.: "PET1", "OUTDOC2", "DOCCOMPROV3", "EXTRGRERJ19", "OUT258".
# 2+ letras iniciais, depois letras/dígitos, terminando em 1-4 dígitos.
RE_LINHA_DOC = re.compile(r"^[A-Z]{2,}[A-Z0-9]*\d{1,4}$")
# OAB (precisa ser excluída — formato 2 letras + 4-7 dígitos + sufixo opcional).
RE_LINHA_OAB = re.compile(r"^[A-Z]{2}\d{4,7}[A-Z]?$")

# Usuário (linha isolada ou final de descrição): OAB, código de sistema, sec*, ou sigla CAPS.
RE_LINHA_USUARIO = re.compile(
    r"^(?:[A-Z]{2}\d{4,8}[A-Z]?|[a-zA-Z]\d{5,8}|sec[a-z]+|[A-Z]{3,20})$"
)
RE_USUARIO_TAIL = re.compile(
    r"\s+(?P<user>"
    r"[A-Z]{2}\d{4,8}[A-Z]?"
    r"|[a-zA-Z]\d{5,8}"
    r"|sec[a-z]+"
    r"|[A-Z]{3,20}"
    r")\s*$"
)

# Caracteres "Private Use Area" do Unicode — ícones de fontes Material Symbols
# embarcados no PDF do eproc. Precisam ser removidos antes de qualquer regex sobre
# o texto, senão geram catastrophic backtracking nas regexes de token.
RE_PUA = re.compile(r"[-]")
RE_MULTI_ESPACO = re.compile(r"\s+")
RE_NAO_GEROU_DOC = re.compile(r"Evento\s+n[ãa]o\s+gerou\s+documento", re.IGNORECASE)

# Tipo do ato individual (PDF tipo C). Termo em maiúsculas, fora de blocos de assinatura.
RE_TIPO_ATO = re.compile(
    r"^\s*(ATO ORDIN[ÁA]TÓRIO|DESPACHO|DECIS[ÃA]O|SENTEN[ÇC]A|MANDADO|CERTID[ÃA]O|OF[ÍI]CIO|EDITAL)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
RE_CABECALHO_ATO = re.compile(
    r"^([A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-ZÁÉÍÓÚÂÊÔÃÕÇ ]+?)\s*N[º°]\s*(\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4})(?:/([A-Z]{2}))?\s*$",
    re.MULTILINE,
)
RE_ASSUNTO_ATO = re.compile(r"^Assunto:\s*(.+)$", re.MULTILINE | re.IGNORECASE)
RE_CODIGO_VERIFICADOR = re.compile(r"c[óo]digo\s+verificador\s+(\d{8,}v\d+)", re.IGNORECASE)
RE_CODIGO_CRC = re.compile(r"\bCRC\s+([a-f0-9]{6,})\b", re.IGNORECASE)
RE_SIGNATARIO = re.compile(
    r"(?:Documento\s+eletr[ôo]nico\s+assinado\s+por"
    r"|Signat[áa]rio\s*\(a\)?\s*:?"
    r"|Respons[áa]vel\s+pela\s+regra\s+de\s+automatiza[çc][ãa]o\s*:?)"
    r"\s*([A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-ZÁÉÍÓÚÂÊÔÃÕÇa-záéíóúâêôãõç.' -]+)",
    re.IGNORECASE,
)
RE_DATA_ASSINATURA = re.compile(
    r"Data\s+e\s+Hora:\s*(\d{2}/\d{2}/\d{4}[^\n]*?\d{2}:\d{2}(?::\d{2})?)",
    re.IGNORECASE,
)
RE_LOCAL_ATO = re.compile(r"^\s*Local:\s*(.+)$", re.MULTILINE | re.IGNORECASE)
RE_ENDERECO_VARA = re.compile(
    r"^([^\n]*?(?:CEP|Bairro|Avenida|Rua|Pra[çc]a|Travessa|Alameda)[^\n]{5,}?)$",
    re.MULTILINE | re.IGNORECASE,
)
RE_PARTE_ATO = re.compile(
    r"^\s*(?P<papel>" + "|".join(PAPEIS_ATIVO + PAPEIS_PASSIVO) + r")\s*:\s*(?P<nome>[^\n]+?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# Extrato de distribuição (PDF tipo B).
RE_EXTRATO_CHAVE = re.compile(r"Chave\s+para\s+consulta:\s*(\d+)", re.IGNORECASE)
RE_EXTRATO_VALOR_CAUSA = re.compile(r"Valor\s+da\s+Causa:\s*R\$\s*([\d.,]+)", re.IGNORECASE)
RE_EXTRATO_DATA_ENVIO = re.compile(r"Data\s+Envio:\s*(\d{2}/\d{2}/\d{4})", re.IGNORECASE)
RE_EXTRATO_HORA_ENVIO = re.compile(r"Hora\s+de\s+Envio:\s*(\d{2}:\d{2}(?::\d{2})?)", re.IGNORECASE)
RE_EXTRATO_NOME = re.compile(r"^Nome:\s*(.+)$", re.MULTILINE | re.IGNORECASE)
RE_EXTRATO_OAB = re.compile(r"OAB/Sigla:\s*([A-Z]{2})(\d{4,7}[A-Z]?)", re.IGNORECASE)
RE_EXTRATO_EVENTO = re.compile(r"^Evento:\s*(.+)$", re.MULTILINE | re.IGNORECASE)
RE_EXTRATO_ORGAO = re.compile(r"Org[ãa]o\s+Julgador:\s*(.+)", re.IGNORECASE)
RE_EXTRATO_MAGISTRADO = re.compile(r"Magistrado:\s*(.+)", re.IGNORECASE)
RE_EXTRATO_PARTE = re.compile(
    r"^(.+?)\s*-\s*(" + "|".join(PAPEIS_ATIVO + PAPEIS_PASSIVO) + r")\s*$",
    re.MULTILINE | re.IGNORECASE,
)


# Heurística de seções da petição. HEADER = endereçamento; CLOSING = fórmula de
# fechamento ("Termos em que pede deferimento"); BODY = todo o resto.
RE_ENDERECAMENTO = re.compile(
    r"^\s*(EXCELENT[IÍ]SSIM|MERIT[IÍ]SSIM|AO\s+JU[IÍ]ZO|JU[IÍ]Z(\(A\))?|VARA\s+|"
    r"FORO\s+|COMARCA\s+|TRIBUNAL\s+)",
    re.IGNORECASE,
)
RE_FECHAMENTO = re.compile(
    r"(termos\s+em\s+que|nestes\s+termos|por\s+todo\s+o\s+exposto|"
    r"pede\s+deferimento|p\.\s*deferimento|pede\s+e\s+espera\s+deferimento)",
    re.IGNORECASE,
)

# Heurística de gênero. Overrides para nomes que quebram a regra do "termina em a".
OVERRIDES_FEMININOS: set[str] = {
    "isabel", "raquel", "carmen", "ester", "esther", "miriam", "ines",
    "soledad", "beatriz", "iris", "agar",
}
OVERRIDES_MASCULINOS: set[str] = {
    "joshua", "elias", "tobias", "isaias", "jeremias", "matias", "barnabas",
    "nicolas", "lucas", "jonas", "ezequias", "josias",
}

# Vocabulário de papéis processuais flexionáveis.
FLEXOES: dict[str, dict[str, str]] = {
    "autor":      {"ms": "autor",      "fs": "autora",      "mp": "autores",      "fp": "autoras"},
    "réu":        {"ms": "réu",        "fs": "ré",          "mp": "réus",         "fp": "rés"},
    "executado":  {"ms": "executado",  "fs": "executada",   "mp": "executados",   "fp": "executadas"},
    "requerido":  {"ms": "requerido",  "fs": "requerida",   "mp": "requeridos",   "fp": "requeridas"},
    "devedor":    {"ms": "devedor",    "fs": "devedora",    "mp": "devedores",    "fp": "devedoras"},
    "requerente": {"ms": "requerente", "fs": "requerente",  "mp": "requerentes",  "fp": "requerentes"},
    "exequente":  {"ms": "exequente",  "fs": "exequente",   "mp": "exequentes",   "fp": "exequentes"},
}
# Família de preposições/artigos. Trocadas em conjunto com a forma flexionada
# para manter regência ("do executado" ↔ "da executada"; "pelo autor" ↔ "pela autora").
PREP_FAMILIAS: list[dict[str, str]] = [
    {"ms": "o",    "fs": "a",    "mp": "os",   "fp": "as"},
    {"ms": "do",   "fs": "da",   "mp": "dos",  "fp": "das"},
    {"ms": "no",   "fs": "na",   "mp": "nos",  "fp": "nas"},
    {"ms": "ao",   "fs": "à",    "mp": "aos",  "fp": "às"},
    {"ms": "pelo", "fs": "pela", "mp": "pelos","fp": "pelas"},
]
TODAS_PREPS: set[str] = {p for fam in PREP_FAMILIAS for p in fam.values()}

# Mapeia cada role-base para o "lado" do processo (autor/réu) usado no contexto.
ROLE_TO_SIDE: dict[str, str] = {
    "autor": "autor", "requerente": "autor", "exequente": "autor",
    "réu": "reu", "requerido": "reu", "executado": "reu", "devedor": "reu",
}

# Padronização de layout do consolidado.
SPACE_AFTER_PT = 6
LINE_SPACING = 1.5


@dataclass
class Advogado:
    nome: str = ""
    oab: str = ""   # ex.: "MG104784", "SP423256B"
    uf: str = ""    # ex.: "MG"


@dataclass
class Parte:
    papel: str = ""             # AUTOR / RÉU / EXEQUENTE / EXECUTADO / REQUERENTE / REQUERIDO / ...
    nome: str = ""
    documento: str = ""         # CNPJ/CPF (com máscara preservada)
    tipo: str = ""              # "Pessoa Jurídica" / "Pessoa Física"
    advogados: list[Advogado] = field(default_factory=list)


@dataclass
class Contexto:
    """Gênero/número derivados das partes processuais, usado para flexão."""
    genero_autor: str = "m"   # "m" | "f"
    numero_autor: str = "s"   # "s" | "p"
    genero_reu: str = "m"
    numero_reu: str = "s"


@dataclass
class Evento:
    numero: str = ""
    data: str = ""
    hora: str = ""
    descricao: str = ""
    usuario: str = ""
    documentos: list[str] = field(default_factory=list)
    refer_evento: str = ""              # "Refer. ao Evento N"  (apenas N — pode ter vírgulas)
    prazo_dias: int | None = None
    prazo_status: str = ""              # ABERTO / FECHADO
    prazo_inicio: str = ""              # DD/MM/AAAA
    prazo_fim: str = ""                 # DD/MM/AAAA


@dataclass
class Prazo:
    parte_nome: str = ""
    parte_papel: str = ""
    dias: int = 0
    inicio: str = ""
    fim: str = ""
    status: str = ""
    evento_origem: str = ""


@dataclass
class AtoDocumento:
    tipo: str = ""                  # "ATO ORDINATÓRIO" / "DESPACHO" / "DECISÃO" / "SENTENÇA"
    classe_acao: str = ""           # "PROCEDIMENTO COMUM CÍVEL"
    teor: str = ""                  # texto do corpo do ato (truncado a ~4000 chars)
    signatario: str = ""
    data_assinatura: str = ""
    codigo_verificador: str = ""    # ex.: "610009680897v1"
    codigo_crc: str = ""            # ex.: "9c4507c5"
    local: str = ""                 # cidade
    arquivo: str = ""               # nome do PDF de origem


@dataclass
class Processo:
    # --- cabeçalho ---
    numero: str = ""
    classe: str = ""
    assunto: str = ""
    competencia: str = ""
    data_autuacao: str = ""
    situacao: str = ""
    orgao_julgador: str = ""
    juiz: str = ""
    # --- identificação extra ---
    tribunal: str = ""              # "TJSP" / "TJMG" / "TJRJ"
    uf: str = ""                    # "SP" / "MG" / "RJ"
    comarca: str = ""
    vara: str = ""
    foro_endereco: str = ""         # endereço/CEP/fone/email da vara (dos atos)
    valor_causa: str = ""           # R$ 44.924,15
    chave_consulta: str = ""        # do extrato de distribuição
    tags: list[str] = field(default_factory=list)
    # --- conteúdo ---
    partes: list[Parte] = field(default_factory=list)
    eventos: list[Evento] = field(default_factory=list)
    prazos_abertos: list[Prazo] = field(default_factory=list)
    atos: list[AtoDocumento] = field(default_factory=list)
    # --- agregados (derivados) ---
    total_eventos: int = 0
    primeira_movimentacao: str = ""     # "DD/MM/AAAA"
    ultima_movimentacao: str = ""       # "DD/MM/AAAA — descrição curta"
    # --- arquivos ---
    arquivos_origem: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Extração de texto
# ---------------------------------------------------------------------------

def _hash_pdf(pdf_path: Path) -> str:
    """Gera hash MD5 do path + tamanho + mtime para identificar o PDF."""
    try:
        st = pdf_path.stat()
        raw = f"{pdf_path.resolve()}|{st.st_size}|{int(st.st_mtime)}"
        return hashlib.md5(raw.encode()).hexdigest()
    except OSError:
        return ""


def extrair_texto(pdf_path: Path, cache_dir: Path | None = None) -> str:
    """Extrai o texto bruto de todas as páginas do PDF, usando cache quando possível."""
    if cache_dir:
        h = _hash_pdf(pdf_path)
        if h:
            cache_file = cache_dir / f"{h}.txt"
            if cache_file.exists():
                try:
                    return cache_file.read_text(encoding="utf-8")
                except Exception:
                    pass

    reader = PdfReader(str(pdf_path), strict=False)
    blocos: list[str] = []
    for page in reader.pages:
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        blocos.append(t)
    texto = "\n".join(blocos)

    if cache_dir and h:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(texto, encoding="utf-8")
        except Exception:
            pass

    return texto


def detectar_tipo_pdf(texto: str) -> str:
    """
    Classifica o PDF em "extrato" (Justiça Estadual / distribuição),
    "ato" (despacho/decisão/sentença individual), "detalhes" (capa do processo)
    ou "desconhecido". A ordem importa: o extrato e o ato podem conter
    "Nº do processo" no corpo, mas só "detalhes" tem o layout tabular.
    """
    t = texto or ""
    if "Chave para consulta" in t and "Processo Eletr" in t:
        return "extrato"
    if RE_CODIGO_VERIFICADOR.search(t) and "Poder Judici" in t:
        return "ato"
    if "Consulta Processual" in t and "Detalhes do Processo" in t:
        return "detalhes"
    if "Partes e Representantes" in t and "Eventos" in t and RE_NUMERO_CNJ.search(t):
        return "detalhes"
    return "desconhecido"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _valor_apos_rotulo(texto: str, rotulo_regex: str) -> str:
    """Pega a primeira linha não-vazia logo após o rótulo."""
    m = re.search(rotulo_regex + r"\s*\n+\s*(?P<val>[^\n]+)", texto, re.IGNORECASE)
    if not m:
        return ""
    return m.group("val").strip()


def _inferir_tribunal(numero_cnj: str) -> tuple[str, str]:
    """Retorna (tribunal, uf) a partir do CNJ. Vazio se desconhecido."""
    m = RE_CNJ_TR.search(numero_cnj or "")
    if not m:
        return "", ""
    chave = f"{m.group(1)}.{m.group(2)}"
    return MAPA_TRIBUNAL_CNJ.get(chave, ""), MAPA_UF_CNJ.get(chave, "")


def _municipio_de_comarca(comarca: str) -> str:
    """Extrai o nome do município de uma string tipo 'Comarca de Carapicuíba'."""
    if not comarca:
        return ""
    s = comarca.strip()
    for prefixo in ("Comarca de ", "Foro Regional de ", "Foro de "):
        if s.lower().startswith(prefixo.lower()):
            return s[len(prefixo):].strip()
    return s


def _oab_completa(uf: str, numero: str) -> str:
    """Formata a OAB completa no padrão 'OAB/UF NÚMERO' (ex.: 'OAB/SP 346.433')."""
    uf = (uf or "").strip().upper()
    numero = (numero or "").strip()
    if uf and numero:
        return f"OAB/{uf} {numero}"
    return numero or (f"OAB/{uf}" if uf else "")


def _parse_orgao_julgador(orgao: str) -> tuple[str, str]:
    if not orgao:
        return "", ""
    m = re.search(
        r"(?P<vara>(?:\d+[ªº]\s+)?(?:UPJ[^,]*?|Ju[íi]zo[^,]*?|Vara[^,]*?))"
        r"\s+(?:da|de|do)\s+(?P<comarca>(?:Comarca|Regional|Foro|Subse[çc][ãa]o|Se[çc][ãa]o)\s+(?:Judici[áa]ria\s+)?(?:de|da|do)?\s*[^,\n]+)",
        orgao,
        re.IGNORECASE,
    )
    if m:
        vara = re.sub(r"\s+", " ", m.group("vara")).strip()
        comarca = re.sub(r"\s+", " ", m.group("comarca")).strip()
        return vara, comarca
    m2 = re.search(r"(?P<vara>\d+[ªº]\s+Vara[^,\n]*?)\s+(?:da\s+)?Comarca\s+de\s+(?P<comarca>[^,\n]+)", orgao, re.IGNORECASE)
    if m2:
        return m2.group("vara").strip(), f"Comarca de {m2.group('comarca').strip()}"
    return orgao.strip(), ""


def _extrair_advogados(tail: str) -> list[Advogado]:
    """Extrai advogados (nome + OAB) de um trecho de texto após uma parte."""
    advs: list[Advogado] = []
    vistos: set[str] = set()
    for m in RE_ADVOGADO.finditer(tail):
        nome = re.sub(r"\s+", " ", m.group(1)).strip()
        uf = m.group(2).strip().upper()
        num = m.group(3).strip()
        oab = f"{uf}{num}"
        if oab in vistos:
            continue
        vistos.add(oab)
        advs.append(Advogado(nome=nome, oab=oab, uf=uf))
    return advs


def _parse_cabecalho(texto: str, proc: Processo) -> None:
    for atributo, padrao in CAMPOS_CABECALHO:
        valor = _valor_apos_rotulo(texto, padrao)
        if valor:
            setattr(proc, atributo, valor)
    m = RE_NUMERO_CNJ.search(proc.numero or texto)
    if m:
        proc.numero = m.group(0)
    topo = texto[:1500]
    for tag in TAGS_CONHECIDAS:
        if re.search(r"\b" + re.escape(tag) + r"\b", topo, re.IGNORECASE):
            if tag not in proc.tags:
                proc.tags.append(tag)
    m_assunto = re.search(r"Assunto[s]?:\s*\n*\s*([^\n]+)", texto, re.IGNORECASE)
    if m_assunto:
        proc.assunto = m_assunto.group(1).strip()
    tribunal, uf = _inferir_tribunal(proc.numero)
    if tribunal:
        proc.tribunal = tribunal
    if uf:
        proc.uf = uf
    if proc.orgao_julgador:
        vara, comarca = _parse_orgao_julgador(proc.orgao_julgador)
        if vara:
            proc.vara = vara
        if comarca:
            proc.comarca = comarca


def _detectar_papeis(texto: str) -> tuple[str, str]:
    m_bloco = re.search(r"Partes\s+e\s+Representantes(.{0,300})", texto, re.DOTALL | re.IGNORECASE)
    janela = m_bloco.group(1) if m_bloco else texto[:500]
    m = RE_CABECALHO_PARTES.search(janela)
    if m:
        return m.group(1).upper(), m.group(2).upper()
    return "AUTOR", "RÉU"


def _parse_partes(texto: str) -> list[Parte]:
    m = RE_BLOCO_PARTES.search(texto)
    if not m:
        return []
    bloco = m.group(1)
    papel_ativo, papel_passivo = _detectar_papeis(texto)

    parte_pattern = re.compile(
        r"(?P<nome>[A-ZÁÉÍÓÚÂÊÔÃÕÇ][^\n()]{2,}?)"
        r"\s*\(\s*(?P<doc>[\d./-]{11,})\s*\)\s*-\s*"
        r"(?P<tipo>Pessoa\s+(?:Jur[íi]dica|F[íi]sica))",
        re.IGNORECASE,
    )
    matches = list(parte_pattern.finditer(bloco))
    if not matches:
        return []

    partes: list[Parte] = []
    for i, mp in enumerate(matches):
        nome = re.sub(r"\s+", " ", mp.group("nome")).strip()
        doc = mp.group("doc").strip()
        tipo = re.sub(r"\s+", " ", mp.group("tipo")).strip()

        ini = mp.end()
        fim = matches[i + 1].start() if i + 1 < len(matches) else len(bloco)
        tail = bloco[ini:fim]

        advs = _extrair_advogados(tail)
        papel = papel_ativo if i % 2 == 0 else papel_passivo
        partes.append(Parte(papel=papel, nome=nome, documento=doc, tipo=tipo, advogados=advs))
    return partes


def _extrair_bloco_eventos(texto: str) -> str:
    cabecalhos = list(RE_CABECALHO_TABELA_EVENTOS.finditer(texto))
    if not cabecalhos:
        return ""
    pedacos: list[str] = []
    for i, cab in enumerate(cabecalhos):
        inicio = cab.end()
        fim_cab = cabecalhos[i + 1].start() if i + 1 < len(cabecalhos) else len(texto)
        m_fim = RE_FIM_TABELA_EVENTOS.search(texto, pos=inicio, endpos=fim_cab)
        fim = m_fim.start() if m_fim else fim_cab
        pedacos.append(texto[inicio:fim])
    return "\n".join(p.strip() for p in pedacos if p.strip())


def _limpa_linha(linha: str) -> str:
    """Remove icones PUA, normaliza non-breaking space e colapsa whitespace.

    Linhas do PDF terminam com chars PUA (icones de fonte Material Symbols).
    Sem limpar, regexes que procuram tokens entram em catastrophic backtracking
    em eventos com muitos documentos."""
    s = RE_PUA.sub("", linha).replace(" ", " ")
    return RE_MULTI_ESPACO.sub(" ", s).strip()


_RE_EVENTO_HEAD_INLINE = re.compile(
    r"^(\d+)\s+(\d{2}/\d{2}/\d{4})(?:\s+(\d{2}:\d{2}:\d{2}))?\s*(.*)$"
)
_RE_EVENTO_NUM_SO = re.compile(r"^(\d+)$")
_RE_EVENTO_DATA = re.compile(
    r"^(\d{2}/\d{2}/\d{4})(?:\s+(\d{2}:\d{2}:\d{2}))?\s*(.*)$"
)
_RE_EVENTO_HORA = re.compile(r"^(\d{2}:\d{2}:\d{2})\s*(.*)$")


def _parse_evento_linhas(linhas: list[str]) -> Evento | None:
    """Parseia um unico evento a partir de suas linhas.

    Classifica linha a linha (documento, prazo, usuario, descricao) - nunca
    aplica regex sobre o texto concatenado. Elimina o backtracking que
    travava o programa em eventos como "Juntada de integra" com 200+ docs."""
    if not linhas:
        return None

    ev = Evento()
    idx = 0
    descricao_inline = ""

    primeira = _limpa_linha(linhas[idx])
    m = _RE_EVENTO_HEAD_INLINE.match(primeira)
    if m:
        ev.numero = m.group(1)
        ev.data = m.group(2)
        if m.group(3):
            ev.hora = m.group(3)
        descricao_inline = (m.group(4) or "").strip()
        idx += 1
    else:
        m_num = _RE_EVENTO_NUM_SO.match(primeira)
        if not m_num:
            return None
        ev.numero = m_num.group(1)
        idx += 1
        if idx < len(linhas):
            l2 = _limpa_linha(linhas[idx])
            m_d = _RE_EVENTO_DATA.match(l2)
            if m_d:
                ev.data = m_d.group(1)
                if m_d.group(2):
                    ev.hora = m_d.group(2)
                descricao_inline = (m_d.group(3) or "").strip()
                idx += 1

    if not ev.hora and idx < len(linhas):
        l_h = _limpa_linha(linhas[idx])
        m_h = _RE_EVENTO_HORA.match(l_h)
        if m_h:
            ev.hora = m_h.group(1)
            resto = m_h.group(2).strip()
            if resto:
                descricao_inline = f"{descricao_inline} {resto}".strip()
            idx += 1

    desc_parts: list[str] = []
    if descricao_inline:
        desc_parts.append(descricao_inline)
    docs: list[str] = []
    nao_gerou_doc = False
    usuario = ""

    for raw in linhas[idx:]:
        l = _limpa_linha(raw)
        if not l:
            continue

        if RE_LINHA_DOC.match(l) and not RE_LINHA_OAB.match(l):
            if l not in docs:
                docs.append(l)
            continue

        if RE_NAO_GEROU_DOC.search(l):
            nao_gerou_doc = True
            cleaned = RE_NAO_GEROU_DOC.sub("", l).strip()
            if cleaned:
                desc_parts.append(cleaned)
            continue

        m_pz = RE_PRAZO_HEAD.search(l)
        if m_pz:
            try:
                ev.prazo_dias = int(m_pz.group("dias"))
            except (TypeError, ValueError):
                pass
            ev.prazo_status = (m_pz.group("status") or "").upper()
            m_ini = RE_PRAZO_DATA_INICIAL.search(l)
            if m_ini:
                ev.prazo_inicio = m_ini.group(1)
            m_fim = RE_PRAZO_DATA_FINAL.search(l)
            if m_fim:
                ev.prazo_fim = m_fim.group(1)
            continue

        if not ev.prazo_inicio:
            m_ini = RE_PRAZO_DATA_INICIAL.search(l)
            if m_ini:
                ev.prazo_inicio = m_ini.group(1)
                continue
        if not ev.prazo_fim:
            m_fim = RE_PRAZO_DATA_FINAL.search(l)
            if m_fim:
                ev.prazo_fim = m_fim.group(1)
                continue

        m_ref = RE_REFER_EVENTO.search(l)
        if m_ref:
            ev.refer_evento = re.sub(r"\s+", "", m_ref.group(1)).rstrip(",")
            desc_parts.append(l)
            continue

        if RE_LINHA_USUARIO.match(l):
            usuario = l
            continue

        desc_parts.append(l)

    descricao = RE_MULTI_ESPACO.sub(" ", " ".join(desc_parts)).strip()

    if not usuario:
        m_u = RE_USUARIO_TAIL.search(descricao)
        if m_u:
            usuario = m_u.group("user").strip()
            descricao = descricao[:m_u.start()].rstrip()

    ev.descricao = descricao
    ev.usuario = usuario
    if docs:
        ev.documentos = docs
    elif nao_gerou_doc:
        ev.documentos = ["Evento nao gerou documento"]
    return ev


def _parse_eventos(texto: str) -> list[Evento]:
    bloco = _extrair_bloco_eventos(texto)
    if not bloco:
        return []
    linhas = bloco.splitlines()

    inicios: list[int] = []
    for i, l in enumerate(linhas):
        ls = _limpa_linha(l)
        if re.match(r"^\d+\s+\d{2}/\d{2}/\d{4}", ls):
            inicios.append(i)
        elif re.match(r"^\d+$", ls) and i + 1 < len(linhas):
            if re.match(r"^\d{2}/\d{2}/\d{4}", _limpa_linha(linhas[i + 1])):
                inicios.append(i)

    if not inicios:
        return []
    inicios.append(len(linhas))

    eventos: list[Evento] = []
    for k in range(len(inicios) - 1):
        i, j = inicios[k], inicios[k + 1]
        ev = _parse_evento_linhas(linhas[i:j])
        if ev is not None:
            eventos.append(ev)
    return eventos


# ---------------------------------------------------------------------------
# Parsers de outros tipos de PDF
# ---------------------------------------------------------------------------

def _parse_extrato_distribuicao(texto: str, proc: Processo) -> None:
    m_num = RE_NUMERO_CNJ.search(texto)
    if m_num and not proc.numero:
        proc.numero = m_num.group(0)
    m = RE_EXTRATO_CHAVE.search(texto)
    if m and not proc.chave_consulta:
        proc.chave_consulta = m.group(1).strip()
    m = RE_EXTRATO_VALOR_CAUSA.search(texto)
    if m and not proc.valor_causa:
        proc.valor_causa = f"R$ {m.group(1).strip()}"
    m = RE_EXTRATO_ORGAO.search(texto)
    if m and not proc.orgao_julgador:
        proc.orgao_julgador = m.group(1).strip()
    m = RE_EXTRATO_MAGISTRADO.search(texto)
    if m and not proc.juiz:
        proc.juiz = m.group(1).strip()
    if not proc.partes:
        partes_ext: list[Parte] = []
        for mp in RE_EXTRATO_PARTE.finditer(texto):
            nome = mp.group(1).strip()
            papel = mp.group(2).strip().upper()
            if len(nome) < 3 or nome.lower().startswith(("nome", "evento", "orgao", "magistrado")):
                continue
            partes_ext.append(Parte(papel=papel, nome=nome))
        if partes_ext:
            proc.partes = partes_ext
    m_nome = RE_EXTRATO_NOME.search(texto)
    m_oab = RE_EXTRATO_OAB.search(texto)
    if m_nome and m_oab and proc.partes:
        nome_adv = m_nome.group(1).strip()
        uf = m_oab.group(1).strip().upper()
        num = m_oab.group(2).strip()
        adv = Advogado(nome=nome_adv, oab=f"{uf}{num}", uf=uf)
        for p in proc.partes:
            if p.papel.upper() in [pp.upper() for pp in PAPEIS_ATIVO]:
                if not any(a.oab == adv.oab for a in p.advogados):
                    p.advogados.append(adv)
                break
    tribunal, uf = _inferir_tribunal(proc.numero)
    if tribunal and not proc.tribunal:
        proc.tribunal = tribunal
    if uf and not proc.uf:
        proc.uf = uf
    if proc.orgao_julgador and not proc.vara:
        vara, comarca = _parse_orgao_julgador(proc.orgao_julgador)
        if vara:
            proc.vara = vara
        if comarca and not proc.comarca:
            proc.comarca = comarca


def _parse_ato(texto: str, arquivo: Path | None) -> AtoDocumento:
    ato = AtoDocumento()
    if arquivo:
        ato.arquivo = arquivo.name

    m_cab = RE_CABECALHO_ATO.search(texto)
    if m_cab:
        ato.classe_acao = m_cab.group(1).strip()

    for m in RE_TIPO_ATO.finditer(texto):
        tipo = re.sub(r"\s+", " ", m.group(1)).strip().upper()
        if tipo:
            ato.tipo = tipo
            break

    m_sig = RE_SIGNATARIO.search(texto)
    if m_sig:
        sig = re.sub(r"\s+", " ", m_sig.group(1)).strip()
        sig = re.split(r"\s*,\s*na forma", sig, maxsplit=1)[0]
        ato.signatario = sig.strip()

    m_data = RE_DATA_ASSINATURA.search(texto)
    if m_data:
        ato.data_assinatura = re.sub(r"\s+", " ", m_data.group(1)).strip()

    m_ver = RE_CODIGO_VERIFICADOR.search(texto)
    if m_ver:
        ato.codigo_verificador = m_ver.group(1).strip()
    m_crc = RE_CODIGO_CRC.search(texto)
    if m_crc:
        ato.codigo_crc = m_crc.group(1).strip()

    m_local = RE_LOCAL_ATO.search(texto)
    if m_local:
        ato.local = m_local.group(1).strip()

    if ato.tipo:
        idx_tipo = texto.lower().find(ato.tipo.lower())
        idx_assina = texto.lower().find("documento eletrônico")
        if idx_assina == -1:
            idx_assina = texto.lower().find("documento eletronico")
        if idx_tipo >= 0:
            inicio = idx_tipo + len(ato.tipo)
            fim = idx_assina if idx_assina > inicio else len(texto)
            corpo = texto[inicio:fim].strip()
            ato.teor = re.sub(r"\n{3,}", "\n\n", corpo)[:4000]

    return ato


def _ato_para_processo(ato: AtoDocumento, texto: str, arquivo: Path | None) -> Processo:
    proc = Processo()
    if arquivo:
        proc.arquivos_origem.append(arquivo.name)
    m = RE_NUMERO_CNJ.search(texto)
    if m:
        proc.numero = m.group(0)
    if ato.classe_acao:
        proc.classe = ato.classe_acao
    m_assunto = RE_ASSUNTO_ATO.search(texto)
    if m_assunto:
        proc.assunto = m_assunto.group(1).strip()
    partes_ato: list[Parte] = []
    for mp in RE_PARTE_ATO.finditer(texto):
        papel = mp.group("papel").strip().upper()
        nome = mp.group("nome").strip()
        if len(nome) < 3:
            continue
        partes_ato.append(Parte(papel=papel, nome=nome))
    if partes_ato:
        proc.partes = partes_ato
    m_orgao = re.search(
        r"((?:UPJ|Ju[íi]zo|Vara)[^,\n]+?(?:Comarca|Foro|Regional|Subse[çc][ãa]o)[^,\n]+)",
        texto,
        re.IGNORECASE,
    )
    if m_orgao:
        proc.orgao_julgador = re.sub(r"\s+", " ", m_orgao.group(1)).strip()
    enderecos = []
    for me in RE_ENDERECO_VARA.finditer(texto[:2000]):
        linha = re.sub(r"\s+", " ", me.group(1)).strip()
        if linha and linha not in enderecos:
            enderecos.append(linha)
        if len(enderecos) >= 2:
            break
    if enderecos:
        proc.foro_endereco = " | ".join(enderecos)
    tribunal, uf = _inferir_tribunal(proc.numero)
    if tribunal:
        proc.tribunal = tribunal
    if uf:
        proc.uf = uf
    if proc.orgao_julgador:
        vara, comarca = _parse_orgao_julgador(proc.orgao_julgador)
        if vara:
            proc.vara = vara
        if comarca:
            proc.comarca = comarca
    proc.atos.append(ato)
    return proc


# ---------------------------------------------------------------------------
# Agregados derivados (prazos abertos, primeira/última movimentação, etc.)
# ---------------------------------------------------------------------------

def _parse_data_br(d: str) -> date | None:
    try:
        return datetime.strptime(d.strip(), "%d/%m/%Y").date()
    except (ValueError, AttributeError):
        return None


def _evento_parte_papel(ev: Evento, partes: list[Parte]) -> tuple[str, str]:
    m = re.search(
        r"\((?P<papel>" + "|".join(PAPEIS_ATIVO + PAPEIS_PASSIVO) + r")\s*-\s*(?P<nome>[^)]+)\)",
        ev.descricao,
        re.IGNORECASE,
    )
    if m:
        return m.group("nome").strip(), m.group("papel").strip().upper()
    m2 = re.search(r"Decorrido\s+prazo\s+de\s+([A-ZÁÉÍÓÚÂÊÔÃÕÇ][^.]+?)\s+em\s+\d{2}/\d{2}", ev.descricao)
    if m2:
        nome = m2.group(1).strip()
        for p in partes:
            if p.nome.upper() == nome.upper():
                return p.nome, p.papel
        return nome, ""
    return "", ""


def _derivar_agregados(proc: Processo, hoje: date | None = None) -> None:
    hoje = hoje or date.today()
    proc.total_eventos = len(proc.eventos)
    if proc.eventos:
        primeiro = proc.eventos[-1]
        ultimo = proc.eventos[0]
        proc.primeira_movimentacao = primeiro.data
        desc_curta = (ultimo.descricao[:80] + "...") if len(ultimo.descricao) > 80 else ultimo.descricao
        proc.ultima_movimentacao = f"{ultimo.data} — {desc_curta}".strip(" —")
    prazos: list[Prazo] = []
    for ev in proc.eventos:
        if ev.prazo_status != "ABERTO":
            continue
        fim = _parse_data_br(ev.prazo_fim)
        if fim is not None and fim < hoje:
            continue
        nome, papel = _evento_parte_papel(ev, proc.partes)
        prazos.append(Prazo(
            parte_nome=nome,
            parte_papel=papel,
            dias=ev.prazo_dias or 0,
            inicio=ev.prazo_inicio,
            fim=ev.prazo_fim,
            status=ev.prazo_status,
            evento_origem=ev.numero,
        ))
    prazos.sort(key=lambda p: _parse_data_br(p.fim) or date.max)
    proc.prazos_abertos = prazos


# ---------------------------------------------------------------------------
# Dispatcher principal
# ---------------------------------------------------------------------------

def parse_processo(texto: str, arquivo_origem: Path | None = None) -> Processo:
    proc = Processo()
    if arquivo_origem:
        proc.arquivos_origem.append(arquivo_origem.name)
    _parse_cabecalho(texto, proc)
    proc.partes = _parse_partes(texto)
    proc.eventos = _parse_eventos(texto)
    _derivar_agregados(proc)
    return proc


def parse_pdf(texto: str, arquivo: Path | None = None) -> tuple[str, Processo]:
    tipo = detectar_tipo_pdf(texto)
    if tipo == "detalhes":
        proc = parse_processo(texto, arquivo)
        return "detalhes", proc
    if tipo == "extrato":
        proc = Processo()
        if arquivo:
            proc.arquivos_origem.append(arquivo.name)
        _parse_extrato_distribuicao(texto, proc)
        return "extrato", proc
    if tipo == "ato":
        ato = _parse_ato(texto, arquivo)
        proc = _ato_para_processo(ato, texto, arquivo)
        return "ato", proc
    proc = Processo()
    if arquivo:
        proc.arquivos_origem.append(arquivo.name)
    m = RE_NUMERO_CNJ.search(texto)
    if m:
        proc.numero = m.group(0)
    return "desconhecido", proc


# ---------------------------------------------------------------------------
# Saída
# ---------------------------------------------------------------------------

def _fmt_advogados(advs: list[Advogado]) -> str:
    return ", ".join(f"{a.nome} ({a.oab})" for a in advs if a.nome or a.oab)


_MAX_EVENTOS_RESUMO = 50


def _resumo_txt(proc: Processo) -> str:
    linhas: list[str] = [
        "RESUMO DO PROCESSO",
        "=" * 70,
        f"Nº do processo : {proc.numero}",
        f"Tribunal       : {proc.tribunal}  ({proc.uf})",
        f"Classe         : {proc.classe}",
        f"Assunto        : {proc.assunto}",
        f"Competência    : {proc.competencia}",
        f"Data autuação  : {proc.data_autuacao}",
        f"Situação       : {proc.situacao}",
        f"Órgão Julgador : {proc.orgao_julgador}",
        f"Comarca        : {proc.comarca}",
        f"Vara           : {proc.vara}",
        f"Juiz(a)        : {proc.juiz}",
        f"Valor da causa : {proc.valor_causa}",
        f"Chave consulta : {proc.chave_consulta}",
    ]
    if proc.foro_endereco:
        linhas.append(f"Endereço foro  : {proc.foro_endereco}")
    if proc.tags:
        linhas.append(f"Tags           : {', '.join(proc.tags)}")
    linhas.append(f"Total eventos  : {proc.total_eventos}")
    if proc.primeira_movimentacao:
        linhas.append(f"1ª movimentação: {proc.primeira_movimentacao}")
    if proc.ultima_movimentacao:
        linhas.append(f"Última mov.    : {proc.ultima_movimentacao}")
    linhas.append(f"Arquivos       : {', '.join(proc.arquivos_origem) or '—'}")

    linhas += ["", "PRAZOS ABERTOS", "-" * 70]
    if proc.prazos_abertos:
        for pz in proc.prazos_abertos:
            alvo = f"{pz.parte_papel} - {pz.parte_nome}".strip(" -") or "(parte não identificada)"
            linhas.append(
                f"  {pz.fim or '???'} ({pz.dias} dia(s), {pz.status})  →  {alvo}"
                + (f"   [evento {pz.evento_origem}]" if pz.evento_origem else "")
            )
    else:
        linhas.append("  (nenhum prazo aberto identificado)")

    linhas += ["", "PARTES", "-" * 70]
    if proc.partes:
        for p in proc.partes:
            doc = f"  ({p.documento})" if p.documento else ""
            tipo = f" - {p.tipo}" if p.tipo else ""
            linhas.append(f"  [{p.papel}] {p.nome}{doc}{tipo}")
            if p.advogados:
                linhas.append(f"           Advogado(s): {_fmt_advogados(p.advogados)}")
    else:
        linhas.append("  (nenhuma parte identificada)")

    linhas += ["", "ÚLTIMAS MOVIMENTAÇÕES (10)", "-" * 70]
    if proc.eventos:
        for ev in proc.eventos[:10]:
            desc = (ev.descricao[:120] + "...") if len(ev.descricao) > 120 else ev.descricao
            linhas.append(f"  #{ev.numero}  {ev.data} {ev.hora}  {desc}")
    else:
        linhas.append("  (nenhum evento)")

    if proc.atos:
        linhas += ["", "ATOS / DOCUMENTOS INDIVIDUAIS", "-" * 70]
        for ato in proc.atos:
            linhas.append(
                f"  [{ato.tipo or 'ATO'}] {ato.data_assinatura or '—'}  "
                f"signatário: {ato.signatario or '—'}"
            )
            if ato.codigo_verificador or ato.codigo_crc:
                linhas.append(f"        verificador: {ato.codigo_verificador}  CRC: {ato.codigo_crc}")
            if ato.arquivo:
                linhas.append(f"        arquivo: {ato.arquivo}")

    linhas += ["", "HISTÓRICO COMPLETO DE EVENTOS", "-" * 70]
    if proc.eventos:
        total = len(proc.eventos)
        eventos_mostrar = proc.eventos[:_MAX_EVENTOS_RESUMO]
        for ev in eventos_mostrar:
            linhas.append(f"  #{ev.numero}  {ev.data} {ev.hora}")
            if ev.descricao:
                linhas.append(f"        {ev.descricao}")
            if ev.refer_evento:
                linhas.append(f"        Refer. ao evento: {ev.refer_evento}")
            if ev.prazo_dias:
                linhas.append(
                    f"        Prazo: {ev.prazo_dias} dia(s) — {ev.prazo_status} "
                    f"({ev.prazo_inicio} a {ev.prazo_fim})"
                )
            if ev.usuario:
                linhas.append(f"        Usuário: {ev.usuario}")
            if ev.documentos:
                linhas.append(f"        Documentos: {', '.join(ev.documentos)}")
        if total > _MAX_EVENTOS_RESUMO:
            linhas.append(f"  ... e mais {total - _MAX_EVENTOS_RESUMO} evento(s) (total: {total})")
    else:
        linhas.append("  (nenhum evento identificado)")

    return "\n".join(linhas) + "\n"


def _nome_pasta(numero: str) -> str:
    return numero if numero else PASTA_NAO_IDENTIFICADOS


def _merge_processos(base: Processo, novo: Processo, tipo_pdf_novo: str) -> Processo:
    if not base.numero and novo.numero:
        base.numero = novo.numero

    for campo in (
        "classe", "assunto", "competencia", "data_autuacao", "situacao",
        "orgao_julgador", "juiz", "tribunal", "uf", "comarca", "vara",
        "foro_endereco", "valor_causa", "chave_consulta",
    ):
        if not getattr(base, campo) and getattr(novo, campo):
            setattr(base, campo, getattr(novo, campo))

    for tag in novo.tags:
        if tag not in base.tags:
            base.tags.append(tag)

    base_score = sum(1 + len(p.advogados) + (1 if p.documento else 0) for p in base.partes)
    novo_score = sum(1 + len(p.advogados) + (1 if p.documento else 0) for p in novo.partes)
    if novo_score > base_score:
        base.partes = novo.partes
    elif base.partes and novo.partes:
        for bp in base.partes:
            for np in novo.partes:
                if bp.nome.upper() == np.nome.upper():
                    if not bp.documento and np.documento:
                        bp.documento = np.documento
                    if not bp.tipo and np.tipo:
                        bp.tipo = np.tipo
                    for adv in np.advogados:
                        if not any(a.oab == adv.oab for a in bp.advogados):
                            bp.advogados.append(adv)

    if len(novo.eventos) > len(base.eventos):
        base.eventos = novo.eventos

    for ato in novo.atos:
        chave = (ato.codigo_verificador, ato.arquivo)
        if not any((a.codigo_verificador, a.arquivo) == chave for a in base.atos):
            base.atos.append(ato)

    for arq in novo.arquivos_origem:
        if arq not in base.arquivos_origem:
            base.arquivos_origem.append(arq)

    return base


def _mover_pdf(pdf: Path, destino_pasta: Path, prefixo: str = "") -> Path:
    destino_pasta.mkdir(parents=True, exist_ok=True)
    nome_alvo = (prefixo + pdf.name) if prefixo else pdf.name
    alvo = destino_pasta / nome_alvo
    if alvo.exists() and alvo.resolve() != pdf.resolve():
        base = alvo.stem
        ext = alvo.suffix
        n = 1
        while alvo.exists():
            alvo = destino_pasta / f"{base} ({n}){ext}"
            n += 1
    if pdf.resolve() != alvo.resolve():
        shutil.move(str(pdf), str(alvo))
    return alvo


def _prefixo_por_tipo(tipo: str) -> str:
    return {
        "ato": "ATO - ",
        "extrato": "EXTRATO - ",
        "detalhes": "",
        "desconhecido": "",
    }.get(tipo, "")


MAX_WORKERS = max(2, min(4, (os.cpu_count() or 4) // 2))


def _processar_pdf(
    pdf: Path,
    grupos: dict,
    pdfs_por_numero: dict,
    nao_identificados: list,
    lock: threading.Lock,
    log: Callable[[str], None],
    cancel_event: threading.Event | None = None,
    cache_dir: Path | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
    total: int = 0,
    processed_counter: Iterable[int] | None = None,
) -> None:
    def _marcar_progresso() -> None:
        if progress_cb and processed_counter is not None:
            try:
                atual = next(processed_counter)
            except StopIteration:
                return
            progress_cb(atual, total)

    if cancel_event and cancel_event.is_set():
        return
    log(f"→ Processando: {pdf.name}")
    try:
        texto = extrair_texto(pdf, cache_dir)
    except Exception as exc:
        log(f"  ! Erro ao ler PDF: {exc}")
        _marcar_progresso()
        return

    tipo, proc_atual = parse_pdf(texto, arquivo=pdf)
    log(f"  tipo: {tipo}")

    if not proc_atual.numero:
        m = RE_NUMERO_CNJ.search(pdf.name)
        if m:
            proc_atual.numero = m.group(0)

    numero = proc_atual.numero
    if not numero:
        log("  ! Nº do processo não encontrado — vai para _nao_identificados/")
        with lock:
            nao_identificados.append(pdf)
        _marcar_progresso()
        return

    log(f"  Nº do processo: {numero}")
    with lock:
        if numero not in grupos:
            grupos[numero] = proc_atual
        else:
            _merge_processos(grupos[numero], proc_atual, tipo)
        pdfs_por_numero.setdefault(numero, []).append((pdf, tipo))
    _marcar_progresso()


def organizar_pasta(
    origem: Path,
    destino: Path | None = None,
    log: Callable[[str], None] = print,
    cancel_event: threading.Event | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
) -> dict:
    destino = destino or origem
    destino.mkdir(parents=True, exist_ok=True)

    pdfs = sorted(p for p in origem.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
    log(f"Encontrados {len(pdfs)} PDF(s) em {origem}")

    if cancel_event and cancel_event.is_set():
        log("Operação cancelada antes de iniciar.")
        return {
            "total": len(pdfs),
            "organizados": 0,
            "nao_identificados": 0,
            "processos": [],
        }

    resumo = {
        "total": len(pdfs),
        "organizados": 0,
        "nao_identificados": 0,
        "processos": [],
    }

    grupos: dict[str, Processo] = {}
    pdfs_por_numero: dict[str, list[tuple[Path, str]]] = {}
    nao_identificados: list[Path] = []
    lock = threading.Lock()

    # 1ª passada PARALELA
    if cancel_event and cancel_event.is_set():
        log("Operação cancelada antes da 1ª passada.")
        return resumo

    cache_dir = destino / CACHE_DIR
    n_workers = min(MAX_WORKERS, len(pdfs)) if pdfs else 1
    log(f"Usando {n_workers} worker(s) em paralelo")
    total_pdfs = len(pdfs)
    processed_counter = itertools.count(1)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=n_workers)
    try:
        fut_map = {
            pool.submit(
                _processar_pdf, pdf, grupos, pdfs_por_numero, nao_identificados, lock, log,
                cancel_event, cache_dir, progress_cb, total_pdfs, processed_counter,
            ): pdf
            for pdf in pdfs
        }
        for fut in concurrent.futures.as_completed(fut_map):
            exc = fut.exception()
            if exc:
                pdf = fut_map[fut]
                log(f"! Erro em {pdf.name}: {exc}")
    finally:
        pool.shutdown(wait=True)

    if cancel_event and cancel_event.is_set():
        log("Operação cancelada após 1ª passada.")
        return resumo

    # 2ª passada: move arquivos, escreve metadados.
    log("2ª passada: movendo arquivos e escrevendo metadados...")
    processed = 0
    total = len(grupos) + len(nao_identificados) if nao_identificados else len(grupos)

    for numero, proc in list(grupos.items()):
        if cancel_event and cancel_event.is_set():
            log("Operação cancelada durante 2ª passada.")
            return resumo
        try:
            pasta = destino / _nome_pasta(numero)
            pasta.mkdir(parents=True, exist_ok=True)
            proc.arquivos_origem = []
            for pdf, tipo in pdfs_por_numero.get(numero, []):
                alvo = _mover_pdf(pdf, pasta, _prefixo_por_tipo(tipo))
                proc.arquivos_origem.append(alvo.name)
                if tipo == "ato" and proc.atos:
                    for ato in proc.atos:
                        if ato.arquivo == pdf.name:
                            ato.arquivo = alvo.name
                            break

            _derivar_agregados(proc)

            (pasta / "metadados.json").write_text(
                json.dumps(asdict(proc), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (pasta / "resumo.txt").write_text(_resumo_txt(proc), encoding="utf-8")
            resumo["organizados"] += 1
            resumo["processos"].append(asdict(proc))
            processed += 1
            if progress_cb:
                progress_cb(processed, total)
        except Exception as exc:
            log(f"  ! Erro ao processar {numero}: {exc}")

    # PDFs sem CNJ identificado: vão pra _nao_identificados/.
    if nao_identificados:
        pasta_ni = destino / PASTA_NAO_IDENTIFICADOS
        pasta_ni.mkdir(parents=True, exist_ok=True)
        for pdf in nao_identificados:
            if cancel_event and cancel_event.is_set():
                log("Operação cancelada durante movimentação de não identificados.")
                return resumo
            _mover_pdf(pdf, pasta_ni)
            resumo["nao_identificados"] += 1
            processed += 1
            if progress_cb:
                progress_cb(processed, total)

    # Índice CSV consolidado.
    if grupos:
        try:
            caminho_csv = gerar_indice_csv(list(grupos.values()), destino)
            log(f"\nÍndice CSV gerado em: {caminho_csv}")
        except Exception as exc:
            log(f"\n! Erro ao gerar índice CSV: {exc}")

    log(
        f"\nConcluído: {resumo['organizados']} organizado(s), "
        f"{resumo['nao_identificados']} não identificado(s)."
    )
    return resumo


# ---------------------------------------------------------------------------
# Índice CSV consolidado
# ---------------------------------------------------------------------------

def gerar_indice_csv(processos: list[Processo], destino: Path) -> Path:
    """Gera `índice.csv` na raiz do destino, uma linha por processo, separador `;`."""
    caminho = destino / "índice.csv"
    colunas = [
        "numero", "tribunal", "uf", "classe", "assunto", "valor_causa",
        "autor", "reu", "oabs_autor", "oabs_reu",
        "orgao_julgador", "comarca", "vara", "juiz", "situacao", "tags",
        "data_autuacao", "total_eventos", "primeira_movimentacao", "ultima_movimentacao",
        "prazos_abertos_qtd", "prazos_abertos_proxima_data", "qtd_arquivos",
    ]
    with caminho.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writerow(colunas)
        for proc in sorted(processos, key=lambda p: p.numero):
            autor = _autor(proc)
            reu = _reus_joined(proc)
            oabs_autor = "; ".join(
                a.oab for p in proc.partes
                if (p.papel or "").upper() in [pp.upper() for pp in PAPEIS_ATIVO]
                for a in p.advogados
            )
            oabs_reu = "; ".join(
                a.oab for p in proc.partes
                if (p.papel or "").upper() in [pp.upper() for pp in PAPEIS_PASSIVO]
                for a in p.advogados
            )
            proxima = proc.prazos_abertos[0].fim if proc.prazos_abertos else ""
            w.writerow([
                proc.numero, proc.tribunal, proc.uf, proc.classe, proc.assunto, proc.valor_causa,
                autor, reu, oabs_autor, oabs_reu,
                proc.orgao_julgador, proc.comarca, proc.vara, proc.juiz, proc.situacao,
                ", ".join(proc.tags),
                proc.data_autuacao, proc.total_eventos, proc.primeira_movimentacao, proc.ultima_movimentacao,
                len(proc.prazos_abertos), proxima, len(proc.arquivos_origem),
            ])
    return caminho


# ---------------------------------------------------------------------------
# Pesquisa de processo: carregamento e mapeamento de UF
# ---------------------------------------------------------------------------

def uf_do_cnj(numero_cnj: str) -> str:
    """Extrai a UF do segmento .J.TR. de um número CNJ. Vazio se desconhecido."""
    m = RE_CNJ_TR.search(numero_cnj or "")
    if not m:
        return ""
    chave = f"{m.group(1)}.{m.group(2)}"
    return MAPA_UF_CNJ.get(chave, "")


def _autor(proc: Processo) -> str:
    for p in proc.partes:
        if p.papel.upper() in [pp.upper() for pp in PAPEIS_ATIVO]:
            return p.nome
    return ""


def _reus_joined(proc: Processo) -> str:
    reus = [
        p.nome for p in proc.partes
        if p.papel.upper() in [pp.upper() for pp in PAPEIS_PASSIVO]
    ]
    return "; ".join(reus)


def _dict_para_processo(dados: dict) -> Processo:
    """Converte um dict (metadados.json OU saída de extrair_processo() do
    TJSP-Scraper) numa instância de Processo.

    O filtro `{k: ... if k in}` ignora chaves que não são campos da dataclass
    (ex.: dicas linguísticas `genero_autor`/`numero_autor` do scraper). Trata a
    única divergência de formato: o scraper devolve `ultima_movimentacao` como
    dict {"data","descricao"}, enquanto Processo espera uma str.
    """
    d = dict(dados)
    um = d.get("ultima_movimentacao")
    if isinstance(um, dict):
        data, desc = um.get("data", ""), um.get("descricao", "")
        d["ultima_movimentacao"] = f"{data} — {desc}".strip(" —")

    proc = Processo(**{k: d[k] for k in Processo.__dataclass_fields__ if k in d})
    if "partes" in d:
        proc.partes = []
        for p in d["partes"]:
            parte = Parte(**{k: p[k] for k in Parte.__dataclass_fields__ if k in p})
            parte.advogados = [
                Advogado(**{k: a[k] for k in Advogado.__dataclass_fields__ if k in a})
                for a in (p.get("advogados") or [])
            ]
            proc.partes.append(parte)
    if "eventos" in d:
        proc.eventos = [Evento(**{k: e[k] for k in Evento.__dataclass_fields__ if k in e}) for e in d["eventos"]]
    if "prazos_abertos" in d:
        proc.prazos_abertos = [Prazo(**{k: p[k] for k in Prazo.__dataclass_fields__ if k in p}) for p in d["prazos_abertos"]]
    if "atos" in d:
        proc.atos = [AtoDocumento(**{k: a[k] for k in AtoDocumento.__dataclass_fields__ if k in a}) for a in d["atos"]]
    return proc


def _carregar_processos(pasta: Path) -> list[Processo]:
    """Varre subpastas de `pasta` por metadados.json e devolve lista de Processo."""
    resultados: list[Processo] = []
    if not pasta.is_dir():
        return resultados
    for entry in sorted(pasta.iterdir()):
        if not entry.is_dir():
            continue
        meta = entry / "metadados.json"
        if not meta.exists():
            continue
        try:
            dados = json.loads(meta.read_text(encoding="utf-8"))
            resultados.append(_dict_para_processo(dados))
        except Exception:
            continue
    return resultados


# ---------------------------------------------------------------------------
# Busca online via TJSP-Scraper (eproc). Todo o acoplamento com o projeto
# externo fica isolado aqui; os imports são tardios para não exigir as libs
# do scraper (selenium etc.) quando a busca online não é usada.
# ---------------------------------------------------------------------------

class _LogWriter(io.TextIOBase):
    """Redireciona os print() do scraper para uma callback (o log da aba).
    Essencial sob pythonw, onde sys.stdout é None e print() quebraria."""

    def __init__(self, put: Callable[[str], None]) -> None:
        self._put = put

    def write(self, s: str) -> int:
        if s:
            self._put(s)
        return len(s)


def buscar_processo_online(
    numero: str, tj_label: str, headless: bool, log: Callable[[str], None],
    modo_docs: str = "nenhum",
) -> Processo:
    """Loga no TJ/sistema escolhido via TJSP-Scraper, consulta o processo e
    devolve um Processo.

    `tj_label` é uma das chaves de TJ_SISTEMAS (ex.: "TJSP — eproc"). Só o eproc
    tem extrator estruturado (auto-preenche os campos); para e-SAJ/PJe devolve um
    Processo mínimo (número + tribunal/UF) e anexa as palavras-chave de
    penhora/bloqueio encontradas.

    `modo_docs` (só eproc): "nenhum" (default — não abre documentos, bem mais
    rápido; a petição não usa o teor dos atos), "atos" ou "todos".

    Levanta ValueError (número/TJ inválidos), ImportError (libs do scraper
    ausentes) ou RuntimeError (login/consulta sem resultado).
    """
    m = RE_NUMERO_CNJ.search(numero or "")
    if not m:
        raise ValueError(f"Número fora do padrão CNJ: {numero!r}")
    numero = m.group(0)
    if tj_label not in TJ_SISTEMAS:
        raise ValueError(f"TJ/sistema inválido: {tj_label!r}")
    site, estado, url = TJ_SISTEMAS[tj_label]
    tribunal, uf = _ESTADO_TJ_UF[estado]

    # Aviso (não bloqueia): número parece ser de outra UF que não a do TJ escolhido.
    tr = RE_CNJ_TR.search(numero)
    uf_num = MAPA_UF_CNJ.get(f"{tr.group(1)}.{tr.group(2)}", "") if tr else ""
    if uf_num and uf_num != uf:
        log(f"Aviso: o número parece ser de {uf_num}, mas você selecionou {tribunal} ({uf}).\n")

    if str(TJSP_SCRAPER_DIR) not in sys.path:
        sys.path.insert(0, str(TJSP_SCRAPER_DIR))
    from dotenv import load_dotenv
    load_dotenv(TJSP_SCRAPER_DIR / ".env")
    from src.scrapers import criar_scraper
    from src.parser import _extrair_blocos_com_origem, extrair_penhora_bloqueio_tribunais

    log(f"Abrindo {tj_label} e logando...\n")
    scraper = criar_scraper(site=site, estado=estado, url=url, headless=headless)
    try:
        scraper.login()
        log(f"Consultando processo {numero}"
            + (f" (docs={modo_docs})" if site == "eproc" else "") + "...\n")
        if site == "eproc":
            html = scraper.consultar_processo(numero, modo_docs=modo_docs)
        else:
            html = scraper.consultar_processo(numero)  # esaj/pje não aceitam modo_docs
        if not html:
            raise RuntimeError("Scraper não retornou dados para esse número.")

        if site == "eproc":
            from src.parser_processo import extrair_processo
            proc = _dict_para_processo(
                extrair_processo(html, _extrair_blocos_com_origem(html), numero_cnj=numero)
            )
            log("Dados extraídos com sucesso.\n")
        else:
            proc = Processo(numero=numero, tribunal=tribunal, uf=uf)
            log("Auto-preenchimento estruturado indisponível para e-SAJ/PJe — "
                "preencha os campos manualmente.\n")

        if not proc.numero:
            proc.numero = numero

        # Palavras-chave de penhora/bloqueio (extrator genérico, todos os sites).
        keywords = list(dict.fromkeys(
            k["descricao"] for k in extrair_penhora_bloqueio_tribunais(html) if k.get("descricao")
        ))
        if keywords:
            log("Palavras-chave de penhora/bloqueio: " + ", ".join(keywords) + "\n")
            if site != "eproc":  # no eproc não polui as tags estruturadas
                for kw in keywords:
                    if kw not in proc.tags:
                        proc.tags.append(kw)
        return proc
    finally:
        try:
            scraper.fechar()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Cache de conversão .doc → .docx + extração de placeholders
# ---------------------------------------------------------------------------

def _obter_docx_cacheado(template_path: Path) -> Path:
    """Devolve um .docx pronto pra leitura via python-docx.
    Converte .doc → .docx via win32com e cacheia por mtime+size."""
    if template_path.suffix.lower() == ".docx":
        return template_path
    CACHE_TEMPLATES_DOCX.mkdir(parents=True, exist_ok=True)
    st = template_path.stat()
    raw = f"{template_path.name}|{st.st_size}|{int(st.st_mtime)}"
    h = hashlib.md5(raw.encode()).hexdigest()
    cached = CACHE_TEMPLATES_DOCX / f"{h}.docx"
    if cached.exists():
        return cached

    import win32com.client
    word = win32com.client.Dispatch("Word.Application")
    word.Visible = False
    doc = None
    try:
        doc = word.Documents.Open(str(template_path.resolve()))
        doc.SaveAs(str(cached), FileFormat=16)
        doc.Close()
        doc = None
    finally:
        if doc is not None:
            try:
                doc.Close(SaveChanges=False)
            except Exception:
                pass
        try:
            word.Quit()
        except Exception:
            pass
    return cached


_PALAVRAS_IMOBILIARIO = ("imobiliári", "imovel", "imóvel", "matricula", "matrícula")


def _template_eh_imobiliario(nome_arquivo: str) -> bool:
    """Heurística: o template trata de imóvel(eis)? Olha o nome do arquivo."""
    n = _strip_accents(nome_arquivo).lower()
    alvos = [_strip_accents(p).lower() for p in _PALAVRAS_IMOBILIARIO]
    return any(p in n for p in alvos)


def _extrair_placeholders_template(template_path: Path) -> set[str]:
    """Lê o template e devolve os placeholders crus (em minúsculo, sem parênteses)."""
    docx_path = _obter_docx_cacheado(template_path)
    doc = Document(str(docx_path))
    encontrados: set[str] = set()

    def _scan(texto: str) -> None:
        for m in RE_PLACEHOLDER_TEMPLATE.finditer(texto):
            encontrados.add(m.group(1).strip().lower())

    for p in doc.paragraphs:
        _scan(p.text)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    _scan(p.text)
    return encontrados


# ---------------------------------------------------------------------------
# Heurísticas de gênero/número e classificação de seções
# ---------------------------------------------------------------------------

def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def _detectar_genero(nome: str, tipo: str) -> str:
    """Heurística: PJ → m; PF feminino se primeiro nome termina em 'a' (com overrides)."""
    tipo_low = tipo.lower()
    if "jurídica" in tipo_low or "juridica" in tipo_low:
        return "m"
    nome_strip = nome.strip()
    if not nome_strip:
        return "m"
    primeiro = nome_strip.split()[0]
    primeiro_norm = _strip_accents(primeiro).lower()
    if primeiro_norm in OVERRIDES_FEMININOS:
        return "f"
    if primeiro_norm in OVERRIDES_MASCULINOS:
        return "m"
    return "f" if primeiro_norm.endswith("a") else "m"


def _construir_contexto(proc: Processo) -> Contexto:
    """Deriva Contexto (gênero/número de autor e réu) a partir das partes do processo."""
    ativo = {p.upper() for p in PAPEIS_ATIVO}
    passivo = {p.upper() for p in PAPEIS_PASSIVO}
    autores = [p for p in proc.partes if p.papel.upper() in ativo]
    reus = [p for p in proc.partes if p.papel.upper() in passivo]

    def _resolver(lista: list[Parte]) -> tuple[str, str]:
        if not lista:
            return ("m", "s")
        generos = [_detectar_genero(p.nome, p.tipo) for p in lista]
        numero = "p" if len(lista) > 1 else "s"
        # Plural masculino vence em gênero misto (norma do português).
        if numero == "p" and "m" in generos:
            return ("m", "p")
        return (generos[0], numero)

    g_autor, n_autor = _resolver(autores)
    g_reu, n_reu = _resolver(reus)
    return Contexto(
        genero_autor=g_autor, numero_autor=n_autor,
        genero_reu=g_reu, numero_reu=n_reu,
    )


def _norm_paragrafo(texto: str) -> str:
    """Chave de dedup: whitespace colapsado, lowercase, pontuação final removida."""
    return re.sub(r"\s+", " ", texto).strip().lower().rstrip(".,;:")


def _classificar_paragrafos(doc: Document) -> list[str]:
    """Marca cada parágrafo do doc como HEADER, BODY ou CLOSING (máquina de estados)."""
    zonas: list[str] = []
    estado = "HEADER"
    viu_endereco = False
    for p in doc.paragraphs:
        texto = p.text.strip()
        if not texto:
            zonas.append(estado)
            continue
        if estado == "HEADER":
            if RE_ENDERECAMENTO.match(texto):
                viu_endereco = True
                zonas.append("HEADER")
                continue
            # Primeiro parágrafo não-vazio sem padrão de endereçamento: vira BODY.
            estado = "BODY"
        if estado == "BODY" and RE_FECHAMENTO.search(texto):
            estado = "CLOSING"
        zonas.append(estado)
    _ = viu_endereco  # informativo; usar futuramente p/ injetar fechamento default.
    return zonas


# ---------------------------------------------------------------------------
# Flexão de gênero/número
# ---------------------------------------------------------------------------

_TODAS_FORMAS: set[str] = {f for variantes in FLEXOES.values() for f in variantes.values()}
_FORMA_PARA_BASE: dict[str, str] = {
    f.lower(): base
    for base, variantes in FLEXOES.items()
    for f in variantes.values()
}
_PREPS_RE = "|".join(re.escape(p) for p in sorted(TODAS_PREPS, key=len, reverse=True))
_FORMAS_RE = "|".join(re.escape(f) for f in sorted(_TODAS_FORMAS, key=len, reverse=True))
_RE_PREP_ROLE = re.compile(rf"\b({_PREPS_RE})\s+({_FORMAS_RE})\b", re.IGNORECASE)
_RE_BARE_ROLE = re.compile(rf"\b({_FORMAS_RE})\b", re.IGNORECASE)
# Marcadores de citação legal — pulamos o parágrafo inteiro para preservar voz original.
_RE_CITACAO = re.compile(
    r"(\bart\.|\bArt\.|§|\bincis[oa]\b|\bpar[áa]grafo\b|[\"“”])",
    re.IGNORECASE,
)


def _preservar_caso(original: str, novo: str) -> str:
    if not original:
        return novo
    if original.isupper():
        return novo.upper()
    if original[0].isupper():
        return novo[:1].upper() + novo[1:]
    return novo


def _ajustar_prep(prep: str, key: str) -> str:
    prep_low = prep.lower()
    for fam in PREP_FAMILIAS:
        if prep_low in fam.values():
            return fam[key]
    return prep


def _inflexionar_texto(texto: str, ctx: Contexto | None) -> str:
    """Aplica flexão sobre vocabulário controlado, preservando capitalização."""
    if not ctx or not texto:
        return texto
    if _RE_CITACAO.search(texto):
        return texto

    def _replace_prep(m: re.Match) -> str:
        prep_orig, forma_orig = m.group(1), m.group(2)
        base = _FORMA_PARA_BASE.get(forma_orig.lower())
        if not base:
            return m.group(0)
        side = ROLE_TO_SIDE.get(base)
        if not side:
            return m.group(0)
        g = ctx.genero_autor if side == "autor" else ctx.genero_reu
        n = ctx.numero_autor if side == "autor" else ctx.numero_reu
        key = g + n
        forma_nova = FLEXOES[base][key]
        prep_nova = _ajustar_prep(prep_orig, key)
        return f"{_preservar_caso(prep_orig, prep_nova)} {_preservar_caso(forma_orig, forma_nova)}"

    texto = _RE_PREP_ROLE.sub(_replace_prep, texto)

    def _replace_bare(m: re.Match) -> str:
        forma_orig = m.group(1)
        base = _FORMA_PARA_BASE.get(forma_orig.lower())
        if not base:
            return m.group(0)
        side = ROLE_TO_SIDE.get(base)
        if not side:
            return m.group(0)
        g = ctx.genero_autor if side == "autor" else ctx.genero_reu
        n = ctx.numero_autor if side == "autor" else ctx.numero_reu
        forma_nova = FLEXOES[base][g + n]
        return _preservar_caso(forma_orig, forma_nova)

    return _RE_BARE_ROLE.sub(_replace_bare, texto)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Organizador de Processos — eproc")
        self.geometry("980x720")
        self.minsize(820, 600)

        # Estado compartilhado / aba Organizador
        self.var_origem = tk.StringVar(value=PASTA_PADRAO)
        self.var_destino = tk.StringVar(value=PASTA_PADRAO)
        self.var_status = tk.StringVar(value="Pronto.")
        self._log_queue: queue.Queue[str] = queue.Queue()
        # Filas thread-safe para atualizações de UI a partir de threads worker.
        # Tkinter NÃO é thread-safe; toda mutação visual passa por essas filas
        # e é drenada em _drain_ui_queues (rodando na main thread).
        self._progress_queue: queue.Queue[tuple[int, int]] = queue.Queue()
        self._status_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._cancel_event = threading.Event()
        self._progress_var = tk.IntVar(value=0)
        self._progress_max = tk.IntVar(value=100)

        # Estado da aba Pesquisa de Processo
        self.var_pesquisa_base = tk.StringVar(value=PASTA_PADRAO)
        self.var_pesquisa_destino = tk.StringVar(value="")
        self.var_filtro = tk.StringVar(value="")
        self.var_busca_numero = tk.StringVar(value="")
        self.var_busca_tj = tk.StringVar(value="TJSP — eproc")
        self.var_busca_headless = tk.BooleanVar(value=False)
        self.var_busca_docs = tk.BooleanVar(value=False)  # False → modo_docs "nenhum"; True → "atos"
        self.var_secretaria = tk.StringVar(value="")
        self.var_municipio = tk.StringVar(value="")
        self.var_estado = tk.StringVar(value="")
        self.var_oab_uf = tk.StringVar(value="")
        self.var_oab_numero = tk.StringVar(value="")
        self.var_endereco_reu = tk.StringVar(value="")
        self.var_valor_causa = tk.StringVar(value="")
        self.var_comarca = tk.StringVar(value="")
        self.var_vara = tk.StringVar(value="")
        self.var_tribunal = tk.StringVar(value="")
        self.var_endereco_foro = tk.StringVar(value="")
        self.var_juiz = tk.StringVar(value="")
        self.var_assunto = tk.StringVar(value="")
        self._entries_auto: dict[str, tk.Entry] = {}
        self._processos_carregados: list[Processo] = []
        self._proc_selecionado: Processo | None = None
        self._templates_vars: dict[Path, tk.BooleanVar] = {}
        self.var_documento_unico = tk.BooleanVar(value=True)
        self.var_multiplos_imoveis = tk.BooleanVar(value=False)
        self._pesquisa_log_queue: queue.Queue[str] = queue.Queue()
        self._gen_worker: threading.Thread | None = None
        self._busca_worker: threading.Thread | None = None
        self._busca_result_queue: queue.Queue = queue.Queue()

        # Form inteligente: estado para destaque baseado nos templates marcados.
        self._templates_placeholders: dict[Path, set[str]] = {}
        self._campos_obrigatorios: set[str] = set()
        self._campos_desconhecidos: set[str] = set()
        self._labels_edit: dict[str, ttk.Label] = {}
        self._entries_edit: dict[str, ttk.Entry] = {}
        self._rotulos_edit: dict[str, str] = {}
        self._dinamicos: dict[str, tuple[ttk.Label, ttk.Entry, tk.StringVar]] = {}
        self._frame_dinamicos: ttk.LabelFrame | None = None
        self._frame_imoveis: ttk.Frame | None = None
        self._chk_imoveis: ttk.Checkbutton | None = None
        self._scan_tpls_thread: threading.Thread | None = None

        # Espelha Secretaria ↔ Vara: alterar um campo replica no outro.
        # Flag de guarda evita loop infinito quando o trace dispara reciprocamente.
        self._espelhando_sec_vara = False
        self.var_secretaria.trace_add("write", self._espelhar_secretaria_para_vara)
        self.var_vara.trace_add("write", self._espelhar_vara_para_secretaria)

        self._build_widgets()
        self.after(100, self._drain_ui_queues)
        self.after(100, self._drain_pesquisa_log_queue)
        self.after(150, self._drain_busca_result)

    def _build_widgets(self) -> None:
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))

        frm_org = ttk.Frame(nb)
        frm_pesq = ttk.Frame(nb)
        nb.add(frm_org, text="Organizador")
        nb.add(frm_pesq, text="Pesquisa de Processo")

        self._build_aba_organizador(frm_org)
        self._build_aba_pesquisa(frm_pesq)

        bar = ttk.Frame(self)
        bar.pack(fill="x", side="bottom")
        ttk.Label(bar, textvariable=self.var_status, anchor="w").pack(fill="x", padx=8, pady=4)

    def _build_aba_organizador(self, parent: ttk.Frame) -> None:
        pad = {"padx": 8, "pady": 4}

        frm = ttk.Frame(parent)
        frm.pack(fill="x", **pad)

        ttk.Label(frm, text="Pasta de origem:").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.var_origem).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(frm, text="Escolher...", command=self._escolher_origem).grid(row=0, column=2)

        ttk.Label(frm, text="Pasta de destino:").grid(row=1, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.var_destino).grid(row=1, column=1, sticky="ew", padx=4)
        ttk.Button(frm, text="Escolher...", command=self._escolher_destino).grid(row=1, column=2)

        frm.columnconfigure(1, weight=1)

        botoes = ttk.Frame(parent)
        botoes.pack(fill="x", **pad)
        self.btn_run = ttk.Button(botoes, text="Organizar", command=self._on_run)
        self.btn_run.pack(side="left")
        self.btn_cancel = ttk.Button(botoes, text="Cancelar", command=self._on_cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=6)
        ttk.Button(botoes, text="Abrir pasta de destino", command=self._abrir_destino).pack(
            side="left", padx=6
        )
        ttk.Button(botoes, text="Limpar log", command=self._limpar_log).pack(side="left")

        self.progress = ttk.Progressbar(
            parent, orient="horizontal",
            variable=self._progress_var, maximum=100,
            mode="determinate",
        )
        self.progress.pack(fill="x", padx=8, pady=(0, 4))

        ttk.Label(parent, text="Log:").pack(anchor="w", padx=8)
        self.txt = tk.Text(parent, height=18, wrap="word", state="disabled")
        self.txt.pack(fill="both", expand=True, padx=8, pady=4)

    def _build_aba_pesquisa(self, parent: ttk.Frame) -> None:
        pad = {"padx": 8, "pady": 4}
        f0 = ttk.Frame(parent)
        f0.pack(fill="x", **pad)
        ttk.Label(f0, text="Pasta base:").pack(side="left")
        ttk.Entry(f0, textvariable=self.var_pesquisa_base).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(f0, text="Escolher...", command=self._escolher_pesquisa_base).pack(side="left")
        ttk.Button(f0, text="Recarregar lista", command=self._recarregar_pesquisa).pack(side="left", padx=4)

        f_busca = ttk.Frame(parent)
        f_busca.pack(fill="x", **pad)
        ttk.Label(f_busca, text="Nº do processo (online):").pack(side="left")
        ttk.Entry(f_busca, textvariable=self.var_busca_numero).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Label(f_busca, text="TJ/Sistema:").pack(side="left", padx=(8, 0))
        ttk.Combobox(
            f_busca, textvariable=self.var_busca_tj, values=list(TJ_SISTEMAS),
            state="readonly", width=14,
        ).pack(side="left", padx=4)
        ttk.Checkbutton(
            f_busca, text="Baixar documentos", variable=self.var_busca_docs
        ).pack(side="left", padx=4)
        ttk.Checkbutton(
            f_busca, text="Navegador invisível", variable=self.var_busca_headless
        ).pack(side="left", padx=4)
        self._btn_buscar = ttk.Button(
            f_busca, text="Buscar online", command=self._buscar_online
        )
        self._btn_buscar.pack(side="left", padx=4)

        f_filtro = ttk.Frame(parent)
        f_filtro.pack(fill="x", **pad)
        ttk.Label(f_filtro, text="Filtro:").pack(side="left")
        ttk.Entry(f_filtro, textvariable=self.var_filtro).pack(side="left", fill="x", expand=True, padx=4)
        self.var_filtro.trace_add("write", lambda *_: self._filtrar_pesquisa())

        f_main = ttk.PanedWindow(parent, orient="horizontal")
        f_main.pack(fill="both", expand=True, **pad)

        f_lista = ttk.Frame(f_main)
        colunas = ("numero", "classe", "autor", "reu", "valor", "ultima_mov", "prazos")
        self.tree = ttk.Treeview(f_lista, columns=colunas, show="headings", height=15)
        for c in colunas:
            cab = {"numero": "Nº processo", "classe": "Classe", "autor": "Autor",
                   "reu": "Réu(s)", "valor": "Valor", "ultima_mov": "Última mov.",
                   "prazos": "Prazos"}[c]
            self.tree.heading(c, text=cab)
            larg = {"numero": 180, "classe": 130, "autor": 120, "reu": 120,
                    "valor": 80, "ultima_mov": 140, "prazos": 70}[c]
            self.tree.column(c, width=larg, minwidth=60)
        self.tree.bind("<<TreeviewSelect>>", self._on_select_processo)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll_tree = ttk.Scrollbar(f_lista, orient="vertical", command=self.tree.yview)
        scroll_tree.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll_tree.set)
        f_main.add(f_lista, weight=2)

        f_det_container = ttk.Frame(f_main)
        det_canvas = tk.Canvas(f_det_container, highlightthickness=0)
        det_scroll = ttk.Scrollbar(f_det_container, orient="vertical", command=det_canvas.yview)
        det_canvas.configure(yscrollcommand=det_scroll.set)
        det_canvas.pack(side="left", fill="both", expand=True)
        det_scroll.pack(side="right", fill="y")

        f_detalhes = ttk.Frame(det_canvas)
        det_window = det_canvas.create_window((0, 0), window=f_detalhes, anchor="nw")
        f_detalhes.bind(
            "<Configure>",
            lambda e: det_canvas.configure(scrollregion=det_canvas.bbox("all")),
        )
        det_canvas.bind(
            "<Configure>",
            lambda e: det_canvas.itemconfigure(det_window, width=e.width),
        )

        def _on_det_mousewheel(event: tk.Event) -> None:
            det_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        det_canvas.bind("<Enter>", lambda e: det_canvas.bind_all("<MouseWheel>", _on_det_mousewheel))
        det_canvas.bind("<Leave>", lambda e: det_canvas.unbind_all("<MouseWheel>"))

        self._detalhes_frame = ttk.LabelFrame(f_detalhes, text="Dados do processo / petição")
        self._detalhes_frame.pack(fill="both", expand=False, padx=4, pady=2)
        campos = [
            ("numero", "Nº do processo:"),
            ("tribunal", "Tribunal:"),
            ("classe", "Classe:"),
            ("orgao_julgador", "Órgão Julgador:"),
            ("juiz", "Juiz(a):"),
            ("valor_causa", "Valor da causa:"),
            ("tags", "Tags:"),
            ("autor", "Autor:"),
            ("reu", "Réu(s):"),
            ("ultima_movimentacao", "Última movimentação:"),
            ("proximo_prazo", "Próximo prazo:"),
        ]
        for i, (atr, rotulo) in enumerate(campos):
            ttk.Label(self._detalhes_frame, text=rotulo, anchor="e", width=18).grid(
                row=i, column=0, sticky="e", padx=(4, 2), pady=1
            )
            lbl = ttk.Label(self._detalhes_frame, text="", anchor="w", wraplength=200)
            lbl.grid(row=i, column=1, sticky="w", padx=(0, 4), pady=1)
            setattr(self, f"_lbl_{atr}", lbl)

        f_edicao = ttk.LabelFrame(f_detalhes, text="Campos editáveis")
        f_edicao.pack(fill="x", padx=4, pady=2)
        campos_edit = [
            ("secretaria", "Secretaria:"),
            ("municipio", "Município:"),
            ("estado", "Estado:"),
            ("oab_uf", "OAB UF:"),
            ("oab_numero", "OAB Nº:"),
            ("endereco_reu", "Endereço do réu:"),
            ("valor_causa", "Valor da causa:"),
            ("comarca", "Comarca:"),
            ("vara", "Vara:"),
            ("tribunal", "Tribunal:"),
            ("endereco_foro", "Endereço do foro:"),
            ("juiz", "Juiz:"),
            ("assunto", "Assunto:"),
        ]
        for i, (atr, rotulo) in enumerate(campos_edit):
            var = getattr(self, f"var_{atr}")
            lbl = ttk.Label(f_edicao, text=rotulo, anchor="e", width=18)
            lbl.grid(row=i, column=0, sticky="e", padx=(4, 2), pady=1)
            self._labels_edit[atr] = lbl
            self._rotulos_edit[atr] = rotulo
            entry = ttk.Entry(f_edicao, textvariable=var, width=28)
            entry.grid(row=i, column=1, sticky="w", padx=(0, 4), pady=1)
            self._entries_edit[atr] = entry
            if atr in ("valor_causa", "comarca", "vara", "tribunal", "endereco_foro",
                       "juiz", "assunto", "municipio", "estado", "oab_uf", "oab_numero"):
                self._entries_auto[str(var)] = entry
                entry.configure(foreground="gray")

        # Linha extra dentro de f_edicao para o checkbox "Múltiplos imóveis"
        # (aparece somente quando o template é imobiliário).
        self._frame_imoveis = ttk.Frame(f_edicao)
        self._chk_imoveis = ttk.Checkbutton(
            self._frame_imoveis,
            text="Múltiplos imóveis",
            variable=self.var_multiplos_imoveis,
        )
        self._chk_imoveis.pack(side="left")
        # Não usa .grid agora — será posicionado dinamicamente em _atualizar_campos_imoveis.

        self._frame_dinamicos = ttk.LabelFrame(
            f_detalhes, text="Campos adicionais (placeholders novos)"
        )
        # Não chamamos .pack() agora — o frame aparece só quando há campos dinâmicos.

        f_templates = ttk.LabelFrame(f_detalhes, text="Modelos de petição")
        f_templates.pack(fill="x", padx=4, pady=2)
        self._templates_canvas = tk.Canvas(f_templates, highlightthickness=0, height=160)
        scroll_t = ttk.Scrollbar(f_templates, orient="vertical", command=self._templates_canvas.yview)
        self._templates_inner = ttk.Frame(self._templates_canvas)
        self._templates_inner.bind("<Configure>",
            lambda e: self._templates_canvas.configure(scrollregion=self._templates_canvas.bbox("all")))
        self._templates_canvas.create_window((0, 0), window=self._templates_inner, anchor="nw")
        self._templates_canvas.configure(yscrollcommand=scroll_t.set)
        self._templates_canvas.pack(side="left", fill="both", expand=True)
        scroll_t.pack(side="right", fill="y")

        def _on_tpl_mousewheel(event: tk.Event) -> str:
            self._templates_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return "break"

        self._templates_canvas.bind(
            "<Enter>",
            lambda e: self._templates_canvas.bind_all("<MouseWheel>", _on_tpl_mousewheel),
        )
        self._templates_canvas.bind(
            "<Leave>",
            lambda e: det_canvas.bind_all("<MouseWheel>", _on_det_mousewheel),
        )

        f_btn_t = ttk.Frame(f_detalhes)
        f_btn_t.pack(fill="x", padx=4, pady=2)
        ttk.Button(f_btn_t, text="Marcar todos", command=self._marcar_todos_templates).pack(side="left")
        ttk.Button(f_btn_t, text="Desmarcar todos", command=self._desmarcar_todos_templates).pack(side="left", padx=4)
        ttk.Checkbutton(
            f_btn_t, text="Gerar documento único",
            variable=self.var_documento_unico,
        ).pack(side="left", padx=12)

        f_gerar = ttk.Frame(f_detalhes)
        f_gerar.pack(fill="x", padx=4, pady=2)
        ttk.Label(f_gerar, text="Pasta de destino:").pack(side="left")
        ttk.Entry(f_gerar, textvariable=self.var_pesquisa_destino).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(f_gerar, text="Escolher...", command=self._escolher_pesquisa_destino).pack(side="left")
        ttk.Button(f_gerar, text="Gerar petições selecionadas", command=self._gerar_peticoes).pack(side="left", padx=4)

        f_main.add(f_det_container, weight=1)

        ttk.Label(parent, text="Log:").pack(anchor="w", padx=8)
        self.txt_pesquisa = tk.Text(parent, height=5, wrap="word", state="disabled")
        self.txt_pesquisa.pack(fill="x", padx=8, pady=4)

    # -- ações da aba Organizador ------------------------------------------
    def _escolher_origem(self) -> None:
        path = filedialog.askdirectory(initialdir=self.var_origem.get() or ".")
        if path:
            self.var_origem.set(path)
            if not self.var_destino.get():
                self.var_destino.set(path)

    def _escolher_destino(self) -> None:
        path = filedialog.askdirectory(initialdir=self.var_destino.get() or ".")
        if path:
            self.var_destino.set(path)

    def _abrir_destino(self) -> None:
        path = self.var_destino.get() or self.var_origem.get()
        if path and Path(path).exists():
            os.startfile(path)

    def _limpar_log(self) -> None:
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    def _espelhar_secretaria_para_vara(self, *_args) -> None:
        if self._espelhando_sec_vara:
            return
        self._espelhando_sec_vara = True
        try:
            self.var_vara.set(self.var_secretaria.get())
        finally:
            self._espelhando_sec_vara = False

    def _espelhar_vara_para_secretaria(self, *_args) -> None:
        if self._espelhando_sec_vara:
            return
        self._espelhando_sec_vara = True
        try:
            self.var_secretaria.set(self.var_vara.get())
        finally:
            self._espelhando_sec_vara = False

    def _log(self, msg: str) -> None:
        self._log_queue.put(msg)

    def _drain_ui_queues(self) -> None:
        """Drena filas de log/progresso/status (main thread). Tkinter só é
        seguro nesta thread, então toda atualização vinda de workers passa por
        aqui."""
        # Logs: insere todas as mensagens acumuladas.
        try:
            mensagens: list[str] = []
            while True:
                mensagens.append(self._log_queue.get_nowait())
        except queue.Empty:
            pass
        if mensagens:
            self.txt.configure(state="normal")
            for msg in mensagens:
                self.txt.insert("end", msg + "\n")
            self.txt.see("end")
            self.txt.configure(state="disabled")

        # Progresso: só interessa o último valor — evita N redraws.
        ultimo_progress: tuple[int, int] | None = None
        try:
            while True:
                ultimo_progress = self._progress_queue.get_nowait()
        except queue.Empty:
            pass
        if ultimo_progress is not None:
            current, total = ultimo_progress
            pct = int(current / max(total, 1) * 100)
            self._progress_var.set(min(pct, 100))

        # Status: aplica todas as mensagens em ordem (status text, botões etc.).
        try:
            while True:
                tipo, valor = self._status_queue.get_nowait()
                if tipo == "status":
                    self.var_status.set(str(valor))
                elif tipo == "btn_run":
                    self.btn_run.configure(state=str(valor))
                elif tipo == "btn_cancel":
                    self.btn_cancel.configure(state=str(valor))
                elif tipo == "progress_set":
                    try:
                        self._progress_var.set(int(valor))
                    except (TypeError, ValueError):
                        pass
        except queue.Empty:
            pass

        self.after(100, self._drain_ui_queues)

    def _on_run(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        origem = Path(self.var_origem.get()).expanduser()
        destino = Path(self.var_destino.get() or self.var_origem.get()).expanduser()
        if not origem.is_dir():
            messagebox.showerror("Erro", f"Pasta de origem não existe:\n{origem}")
            return

        self._cancel_event.clear()
        self.btn_run.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.var_status.set("Organizando...")
        self._limpar_log()
        self._progress_var.set(0)
        self._progress_max.set(100)
        self.progress.configure(maximum=100)

        def run() -> None:
            try:
                def progress_cb(current: int, total: int) -> None:
                    self._progress_queue.put((current, total))

                resumo = organizar_pasta(
                    origem, destino, log=self._log,
                    cancel_event=self._cancel_event,
                    progress_cb=progress_cb,
                )
                if self._cancel_event.is_set():
                    self._log_queue.put("\nOperação cancelada pelo usuário.")
                    self._status_queue.put(("status", "Cancelado."))
                else:
                    self._log_queue.put(
                        f"\nResumo: {resumo['organizados']} organizado(s), "
                        f"{resumo['nao_identificados']} não identificado(s)."
                    )
                    self._status_queue.put((
                        "status",
                        f"Concluído — {resumo['organizados']} processo(s) organizado(s).",
                    ))
                self._status_queue.put(("progress_set", 100))
            except Exception as exc:
                self._log_queue.put(f"ERRO: {exc}")
                self._status_queue.put(("status", "Erro durante execução."))
            finally:
                self._status_queue.put(("btn_run", "normal"))
                self._status_queue.put(("btn_cancel", "disabled"))

        self._worker = threading.Thread(target=run, daemon=True)
        self._worker.start()

    def _on_cancel(self) -> None:
        self._cancel_event.set()
        self.btn_cancel.configure(state="disabled")
        self.var_status.set("Cancelando...")

    # -- ações da aba Pesquisa de Processo ---------------------------------
    def _escolher_pesquisa_base(self) -> None:
        path = filedialog.askdirectory(initialdir=self.var_pesquisa_base.get() or PASTA_PADRAO)
        if path:
            self.var_pesquisa_base.set(path)

    def _recarregar_pesquisa(self) -> None:
        pasta = Path(self.var_pesquisa_base.get()).expanduser()
        if not pasta.is_dir():
            messagebox.showerror("Erro", f"Pasta não existe:\n{pasta}")
            return
        self._processos_carregados = _carregar_processos(pasta)
        self._preencher_tree(self._processos_carregados)

    def _buscar_online(self) -> None:
        """Dispara a busca do processo no TJ/sistema escolhido numa thread."""
        numero = self.var_busca_numero.get().strip()
        if not numero:
            messagebox.showwarning("Busca online", "Informe o número do processo (CNJ).")
            return
        if self._busca_worker and self._busca_worker.is_alive():
            return
        self._btn_buscar.configure(state="disabled")
        tj_label = self.var_busca_tj.get()
        headless = self.var_busca_headless.get()
        modo_docs = "atos" if self.var_busca_docs.get() else "nenhum"
        self._pesquisa_log_queue.put(f"Buscando {numero} em {tj_label}...\n")

        def run() -> None:
            # COM precisa ser inicializado por thread (selenium/win32 podem usar).
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except Exception:
                pass
            writer = _LogWriter(self._pesquisa_log_queue.put)
            try:
                # Sob pythonw, sys.stdout é None; redireciona os print() do
                # scraper para o log da aba (e evita que print() quebre).
                with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                    proc = buscar_processo_online(
                        numero, tj_label, headless,
                        log=self._pesquisa_log_queue.put, modo_docs=modo_docs,
                    )
                self._busca_result_queue.put(("ok", proc))
            except Exception as exc:
                self._busca_result_queue.put(("erro", str(exc)))

        self._busca_worker = threading.Thread(target=run, daemon=True)
        self._busca_worker.start()

    def _drain_busca_result(self) -> None:
        """Drena resultados da busca online na main thread (Tkinter não é
        thread-safe). Mescla o Processo na lista e seleciona a linha."""
        try:
            while True:
                tipo, payload = self._busca_result_queue.get_nowait()
                if tipo == "ok":
                    proc = payload
                    self._processos_carregados = [
                        p for p in self._processos_carregados if p.numero != proc.numero
                    ]
                    self._processos_carregados.append(proc)
                    self._preencher_tree(self._processos_carregados)
                    if proc.numero and self.tree.exists(proc.numero):
                        self.tree.selection_set(proc.numero)
                        self.tree.see(proc.numero)
                    self._pesquisa_log_queue.put(f"Processo {proc.numero} carregado.\n")
                else:
                    self._pesquisa_log_queue.put(f"! Erro na busca online: {payload}\n")
                    messagebox.showerror("Busca online", str(payload))
                self._btn_buscar.configure(state="normal")
        except queue.Empty:
            pass
        finally:
            self.after(150, self._drain_busca_result)

    def _preencher_tree(self, processos: list[Processo]) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for proc in processos:
            autor = _autor(proc)
            reu = _reus_joined(proc)
            proxima = ""
            if proc.prazos_abertos:
                proxima = proc.prazos_abertos[0].fim or "?"
            self.tree.insert("", "end", iid=proc.numero, values=(
                proc.numero, proc.classe, autor, reu,
                proc.valor_causa, proc.ultima_movimentacao[:30], proxima,
            ))

    def _filtrar_pesquisa(self) -> None:
        filtro = self.var_filtro.get().lower().strip()
        if not filtro or not self._processos_carregados:
            self._preencher_tree(self._processos_carregados)
            return
        filtrados = [
            p for p in self._processos_carregados
            if filtro in p.numero.lower()
            or filtro in p.classe.lower()
            or filtro in _autor(p).lower()
            or filtro in _reus_joined(p).lower()
        ]
        self._preencher_tree(filtrados)

    def _on_select_processo(self, event: tk.Event) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        numero = sel[0]
        proc = next((p for p in self._processos_carregados if p.numero == numero), None)
        self._proc_selecionado = proc
        if not proc:
            return
        campos = [
            ("numero", proc.numero),
            ("tribunal", proc.tribunal or ""),
            ("classe", proc.classe or ""),
            ("orgao_julgador", proc.orgao_julgador or ""),
            ("juiz", proc.juiz or ""),
            ("valor_causa", proc.valor_causa or ""),
            ("tags", ", ".join(proc.tags) if proc.tags else ""),
            ("autor", _autor(proc)),
            ("reu", _reus_joined(proc)),
            ("ultima_movimentacao", proc.ultima_movimentacao or ""),
            ("proximo_prazo", proc.prazos_abertos[0].fim if proc.prazos_abertos else ""),
        ]
        for atr, val in campos:
            lbl = getattr(self, f"_lbl_{atr}", None)
            if lbl:
                lbl.configure(text=val)

        auto_map = {
            "valor_causa": proc.valor_causa,
            "comarca": proc.comarca,
            "vara": proc.vara,
            "tribunal": proc.tribunal,
            "endereco_foro": proc.foro_endereco,
            "juiz": proc.juiz,
            "assunto": proc.assunto,
            "municipio": _municipio_de_comarca(proc.comarca),
            "estado": proc.uf,
            "oab_uf": proc.uf,
            "oab_numero": OAB_ESCRITORIO_POR_UF.get((proc.uf or "").upper(), ""),
        }
        for atr, val in auto_map.items():
            var = getattr(self, f"var_{atr}", None)
            if var is not None:
                var.set(val)

        self._carregar_templates()

    def _carregar_templates(self) -> None:
        for w in self._templates_inner.winfo_children():
            w.destroy()
        self._templates_vars.clear()
        pasta = Path(self.var_pesquisa_base.get()).expanduser()
        templates_dir = pasta.parent / "Petições" if "Petições" in str(pasta.parent) else PASTA_PETICOES
        if not templates_dir.is_dir():
            templates_dir = PASTA_PETICOES
        if templates_dir.is_dir():
            for f in sorted(templates_dir.iterdir()):
                if f.suffix.lower() in (".doc", ".docx"):
                    var = tk.BooleanVar(value=False)
                    self._templates_vars[f] = var
                    cb = ttk.Checkbutton(
                        self._templates_inner, text=f.stem, variable=var,
                        command=self._on_template_toggle,
                    )
                    cb.pack(anchor="w")
        self._scan_templates_em_background()
        self._on_template_toggle()

    def _marcar_todos_templates(self) -> None:
        for var in self._templates_vars.values():
            var.set(True)
        self._on_template_toggle()

    def _desmarcar_todos_templates(self) -> None:
        for var in self._templates_vars.values():
            var.set(False)
        self._on_template_toggle()

    def _scan_templates_em_background(self) -> None:
        if self._scan_tpls_thread and self._scan_tpls_thread.is_alive():
            return
        pendentes = [
            p for p in self._templates_vars
            if p not in self._templates_placeholders
        ]
        if not pendentes:
            return

        self._pesquisa_log_queue.put(
            f"Escaneando placeholders de {len(pendentes)} template(s)...\n"
        )

        def run() -> None:
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except Exception:
                pass
            for p in pendentes:
                try:
                    phs = _extrair_placeholders_template(p)
                except Exception as exc:
                    self._pesquisa_log_queue.put(f"! Erro escaneando {p.name}: {exc}\n")
                    phs = set()
                self._templates_placeholders[p] = phs
                # Atualiza destaques na main thread sempre que um template é processado.
                self.after(0, self._on_template_toggle)
            self._pesquisa_log_queue.put("Escaneamento de templates concluído.\n")

        self._scan_tpls_thread = threading.Thread(target=run, daemon=True)
        self._scan_tpls_thread.start()

    def _on_template_toggle(self) -> None:
        obrigatorias: set[str] = set()
        desconhecidos: set[str] = set()
        algum_imobiliario = False
        for tmpl, var in self._templates_vars.items():
            if not var.get():
                continue
            if _template_eh_imobiliario(tmpl.name):
                algum_imobiliario = True
            for ph in self._templates_placeholders.get(tmpl, set()):
                if ph in PLACEHOLDERS_PETICAO:
                    obrigatorias.add(PLACEHOLDERS_PETICAO[ph])
                elif ph in PLACEHOLDERS_AUTO:
                    # Resolvido automaticamente — não pede ao usuário.
                    continue
                else:
                    desconhecidos.add(ph)
        # Secretaria e Vara são espelhadas: se uma é exigida, mostramos as duas.
        if "secretaria" in obrigatorias or "vara" in obrigatorias:
            obrigatorias.add("secretaria")
            obrigatorias.add("vara")
        self._campos_obrigatorios = obrigatorias
        self._campos_desconhecidos = desconhecidos
        self._templates_tem_imovel = algum_imobiliario
        self._atualizar_destaques_campos()
        self._atualizar_campos_dinamicos()
        self._atualizar_campo_imoveis()

    def _atualizar_destaques_campos(self) -> None:
        for atr, lbl in self._labels_edit.items():
            rotulo = self._rotulos_edit.get(atr, "")
            entry = self._entries_edit.get(atr)
            if atr in self._campos_obrigatorios:
                lbl.configure(text=f"* {rotulo}", foreground="#cc0000")
                lbl.grid()
                if entry is not None:
                    entry.grid()
            else:
                lbl.grid_remove()
                if entry is not None:
                    entry.grid_remove()

    def _atualizar_campo_imoveis(self) -> None:
        if self._frame_imoveis is None:
            return
        # Posiciona o checkbox em uma nova linha do f_edicao (logo abaixo dos campos).
        if getattr(self, "_templates_tem_imovel", False):
            n_linhas = len(self._labels_edit)
            self._frame_imoveis.grid(
                row=n_linhas, column=0, columnspan=2,
                sticky="w", padx=(4, 4), pady=(4, 1),
            )
        else:
            self._frame_imoveis.grid_remove()

    def _atualizar_campos_dinamicos(self) -> None:
        if self._frame_dinamicos is None:
            return
        # Cria entries para placeholders ainda não vistos.
        for ph in self._campos_desconhecidos:
            if ph in self._dinamicos:
                continue
            var = tk.StringVar()
            rotulo = ph.capitalize() + ":"
            lbl = ttk.Label(
                self._frame_dinamicos, text=f"* {rotulo}",
                anchor="e", width=18, foreground="#cc0000",
            )
            entry = ttk.Entry(self._frame_dinamicos, textvariable=var, width=28)
            self._dinamicos[ph] = (lbl, entry, var)

        # Reposiciona/esconde conforme estado atual.
        row = 0
        qualquer_visivel = False
        for ph, (lbl, entry, _) in self._dinamicos.items():
            if ph in self._campos_desconhecidos:
                lbl.grid(row=row, column=0, sticky="e", padx=(4, 2), pady=1)
                entry.grid(row=row, column=1, sticky="w", padx=(0, 4), pady=1)
                qualquer_visivel = True
                row += 1
            else:
                lbl.grid_remove()
                entry.grid_remove()

        empacotado = self._frame_dinamicos.winfo_manager() == "pack"
        if qualquer_visivel and not empacotado:
            self._frame_dinamicos.pack(fill="x", padx=4, pady=2)
        elif not qualquer_visivel and empacotado:
            self._frame_dinamicos.pack_forget()

    def _escolher_pesquisa_destino(self) -> None:
        path = filedialog.askdirectory(initialdir=self.var_pesquisa_destino.get() or ".")
        if path:
            self.var_pesquisa_destino.set(path)

    def _gerar_peticoes(self) -> None:
        if self._gen_worker and self._gen_worker.is_alive():
            return
        proc = self._proc_selecionado
        if not proc:
            messagebox.showwarning("Aviso", "Selecione um processo na lista.")
            return
        destino = Path(self.var_pesquisa_destino.get() or "").expanduser()
        if not destino:
            messagebox.showwarning("Aviso", "Escolha uma pasta de destino para as petições.")
            return
        templates_sel = [p for p, v in self._templates_vars.items() if v.get()]
        if not templates_sel:
            messagebox.showwarning("Aviso", "Selecione ao menos um modelo de petição.")
            return

        self._pesquisa_log_queue.put(
            f"Gerando {len(templates_sel)} petição(ões) para {proc.numero} ...\n"
        )

        # Secretaria e Vara são espelhadas — o mesmo valor alimenta ambos placeholders.
        vara_val = self.var_vara.get() or proc.vara
        dados = {
            "numero": proc.numero,
            "classe": proc.classe,
            "assunto": proc.assunto,
            "tribunal": self.var_tribunal.get() or proc.tribunal,
            "uf": proc.uf,
            "comarca": self.var_comarca.get() or proc.comarca,
            "vara": vara_val,
            "juiz": self.var_juiz.get() or proc.juiz,
            "autor": _autor(proc) or self.var_secretaria.get(),
            "reu": _reus_joined(proc),
            "valor_causa": self.var_valor_causa.get() or proc.valor_causa,
            "secretaria": vara_val,
            "municipio": self.var_municipio.get() or _municipio_de_comarca(proc.comarca),
            "estado": self.var_estado.get() or proc.uf,
            "oab_uf": self.var_oab_uf.get() or proc.uf,
            "oab_numero": self.var_oab_numero.get() or OAB_ESCRITORIO_POR_UF.get((proc.uf or "").upper(), ""),
            "oab_completa": _oab_completa(
                self.var_oab_uf.get() or proc.uf,
                self.var_oab_numero.get() or OAB_ESCRITORIO_POR_UF.get((proc.uf or "").upper(), ""),
            ),
            "endereco_reu": self.var_endereco_reu.get(),
            "vara_edit": vara_val,
            "tribunal_edit": self.var_tribunal.get() or proc.tribunal,
            "endereco_foro": self.var_endereco_foro.get() or proc.foro_endereco,
            "juiz_edit": self.var_juiz.get() or proc.juiz,
            "assunto_edit": self.var_assunto.get() or proc.assunto,
        }
        for ph, (_, _, var) in self._dinamicos.items():
            val = var.get().strip()
            if val:
                dados[ph] = val

        documento_unico = self.var_documento_unico.get()
        ctx = _construir_contexto(proc)
        multiplos_imoveis = bool(self.var_multiplos_imoveis.get())

        def run() -> None:
            gerar_peticoes(
                proc,
                templates_sel,
                destino,
                dados,
                log=lambda msg: self._pesquisa_log_queue.put(msg + "\n"),
                documento_unico=documento_unico,
                ctx=ctx,
                multiplos_imoveis=multiplos_imoveis,
            )
            self._pesquisa_log_queue.put("\nConcluído.\n")

        self._gen_worker = threading.Thread(target=run, daemon=True)
        self._gen_worker.start()

    def _drain_pesquisa_log_queue(self) -> None:
        try:
            while True:
                msg = self._pesquisa_log_queue.get_nowait()
                self.txt_pesquisa.configure(state="normal")
                self.txt_pesquisa.insert("end", msg)
                self.txt_pesquisa.see("end")
                self.txt_pesquisa.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._drain_pesquisa_log_queue)


# ---------------------------------------------------------------------------
# Geração de petições (Word)
# ---------------------------------------------------------------------------

def gerar_peticoes(
    proc: Processo,
    templates: list[Path],
    destino: Path,
    dados: dict,
    log: Callable[[str], None] = print,
    documento_unico: bool = False,
    ctx: Contexto | None = None,
    multiplos_imoveis: bool = False,
) -> None:
    destino.mkdir(parents=True, exist_ok=True)

    if documento_unico and len(templates) > 1:
        try:
            saida = _gerar_peticao_consolidada(
                templates, destino, dados, ctx,
                multiplos_imoveis=multiplos_imoveis,
            )
            log(f"  OK: documento consolidado -> {saida.name}")
        except Exception as exc:
            log(f"  ERRO ao consolidar: {exc}")
        return

    for tmpl in templates:
        try:
            _gerar_peticao_unica(
                tmpl, destino, dados, ctx,
                multiplos_imoveis=multiplos_imoveis,
            )
            log(f"  OK: {tmpl.stem}")
        except Exception as exc:
            log(f"  ERRO em {tmpl.name}: {exc}")


def _gerar_peticao_unica(
    template_path: Path,
    destino: Path,
    dados: dict,
    ctx: Contexto | None = None,
    multiplos_imoveis: bool = False,
) -> None:
    sufixo = template_path.suffix.lower()
    nome_base = template_path.stem
    destino_path = destino / f"{nome_base} - {dados.get('numero', 'sem_numero')}"

    if sufixo == ".docx":
        doc = Document(str(template_path))
        zonas = _classificar_paragrafos(doc)
        _preencher_campos_docx(
            doc, dados, ctx=ctx, zonas_paragrafos=zonas,
            multiplos_imoveis=multiplos_imoveis,
        )
        doc.save(str(destino_path) + ".docx")
    elif sufixo == ".doc":
        _converter_doc_para_docx(
            template_path, destino_path, dados, ctx=ctx,
            multiplos_imoveis=multiplos_imoveis,
        )
    else:
        raise ValueError(f"Formato de template não suportado: {sufixo}")


def _gerar_peticao_consolidada(
    templates: list[Path],
    destino: Path,
    dados: dict,
    ctx: Contexto | None,
    multiplos_imoveis: bool = False,
) -> Path:
    """Combina N templates em um único .docx, deduplicando parágrafos do BODY.

    Ordem final: HEADER (1x, do 1º template) → BODYs (únicos, por ordem de seleção)
    → CLOSING (1x, do 1º template que tiver fechamento).
    """
    if not templates:
        raise ValueError("Nenhum template selecionado.")

    # Carrega todos os templates (convertendo .doc se necessário) e classifica zonas.
    docs: list[tuple[Path, Document, list[str]]] = []
    for tmpl in templates:
        docx_path = _obter_docx_cacheado(tmpl) if tmpl.suffix.lower() == ".doc" else tmpl
        d = Document(str(docx_path))
        zonas = _classificar_paragrafos(d)
        docs.append((tmpl, d, zonas))

    # Documento destino herda estilos do 1º template.
    primeiro_path, primeiro_doc, primeiro_zonas = docs[0]
    primeiro_docx = _obter_docx_cacheado(primeiro_path) if primeiro_path.suffix.lower() == ".doc" else primeiro_path
    destino_doc = Document(str(primeiro_docx))
    body = destino_doc.element.body

    # Limpa o destino — vamos remontar do zero clonando parágrafos das fontes.
    for child in list(body):
        # Mantém sectPr no final (necessário p/ a configuração de página do Word).
        if child.tag.endswith("}sectPr"):
            continue
        body.remove(child)

    # Localiza o sectPr (se existir) para reinserir conteúdo antes dele.
    sectPr = None
    for child in list(body):
        if child.tag.endswith("}sectPr"):
            sectPr = child
            break

    def _append(elemento) -> None:
        clone = deepcopy(elemento)
        if sectPr is not None:
            sectPr.addprevious(clone)
        else:
            body.append(clone)

    # 1) HEADER do 1º template.
    novas_zonas: list[str] = []
    for p, zona in zip(primeiro_doc.paragraphs, primeiro_zonas):
        if zona == "HEADER":
            _append(p._element)
            novas_zonas.append("HEADER")

    # 2) BODYs com dedup. Parágrafos curtos (<25 chars normalizados) sempre passam.
    vistos: set[str] = set()
    for _, d, zonas in docs:
        for p, zona in zip(d.paragraphs, zonas):
            if zona != "BODY":
                continue
            chave = _norm_paragrafo(p.text)
            if chave and len(chave) >= 25:
                if chave in vistos:
                    continue
                vistos.add(chave)
            _append(p._element)
            novas_zonas.append("BODY")

    # 3) CLOSING do 1º template que tenha (ou injeta fechamento default).
    closing_emitido = False
    for _, d, zonas in docs:
        if closing_emitido:
            break
        for p, zona in zip(d.paragraphs, zonas):
            if zona == "CLOSING":
                _append(p._element)
                novas_zonas.append("CLOSING")
                closing_emitido = True
    if not closing_emitido:
        para = destino_doc.add_paragraph("Termos em que pede deferimento.")
        # add_paragraph adiciona ao fim — após sectPr. Se sectPr existir, move antes.
        if sectPr is not None:
            body.remove(para._element)
            sectPr.addprevious(para._element)
        novas_zonas.append("CLOSING")

    _preencher_campos_docx(
        destino_doc, dados, ctx=ctx, zonas_paragrafos=novas_zonas,
        multiplos_imoveis=multiplos_imoveis,
    )

    nome = f"Petição Consolidada - {dados.get('numero', 'sem_numero')}.docx"
    saida = destino / nome
    destino_doc.save(str(saida))
    return saida


# Placeholders reais usados nos modelos .doc da pasta "Petições".
# Cada chave é o texto que aparece entre parênteses no modelo; o valor é
# a chave correspondente no dict `dados` montado em _gerar_peticoes.
PLACEHOLDERS_PETICAO = {
    "secretaria": "secretaria",
    "vara": "vara",
    "município ou regional": "municipio",
    "municipio ou regional": "municipio",
    "abreviação do estado": "estado",
    "abreviacao do estado": "estado",
    "número do processo": "numero",
    "numero do processo": "numero",
    "autor": "autor",
    "réu ou réus": "reu",
    "reu ou reus": "reu",
    "nome ou nomes": "reu",
    "executado ou executados": "reu",
    "devedor ou devedores": "reu",
    "empresa ou empresas": "reu",
    "oab uf": "oab_uf",
    "uf da oab": "oab_uf",
    "oab numero": "oab_numero",
    "oab número": "oab_numero",
    "numero da oab": "oab_numero",
    "número da oab": "oab_numero",
    # OAB completa do estado de tramitação (ex.: "OAB/SP 346.433").
    # Match é case-insensitive; mantemos com e sem acento.
    "oab do estado de tramitação do processo": "oab_completa",
    "oab do estado de tramitacao do processo": "oab_completa",
}


# Placeholders "X ou Y" resolvidos por contexto (sem precisar de input manual).
# Formato: ph_lowercase -> (fonte, forma_singular, forma_plural)
# fonte: "imovel" usa flag multiplos_imoveis; "reu"/"autor" usam Contexto.
PLACEHOLDERS_AUTO: dict[str, tuple[str, str, str]] = {
    # Imóvel (controlado pelo checkbox "Múltiplos imóveis")
    "imóvel ou imóveis": ("imovel", "imóvel", "imóveis"),
    "imovel ou imoveis": ("imovel", "imóvel", "imóveis"),
    "do ou dos":         ("imovel", "do", "dos"),
    "da ou das":         ("imovel", "da", "das"),
    "o ou os":           ("imovel", "o", "os"),
    "a ou as":           ("imovel", "a", "as"),
    "são":               ("imovel", "é", "são"),

    # Réu — singular/plural por Contexto
    "réu ou ré":         ("reu_gen", "réu", "ré"),
    "reu ou re":         ("reu_gen", "réu", "ré"),
    "réus ou rés":       ("reu_gen", "réus", "rés"),
    "reus ou res":       ("reu_gen", "réus", "rés"),

    # Autor — singular/plural por Contexto
    "autor ou autora":      ("autor_gen", "autor", "autora"),
    "autores ou autoras":   ("autor_gen", "autores", "autoras"),
}


def _resolver_auto(
    ph: str,
    ctx: "Contexto | None",
    multiplos_imoveis: bool,
) -> str | None:
    spec = PLACEHOLDERS_AUTO.get(ph)
    if not spec:
        return None
    fonte, sing, plur = spec
    if fonte == "imovel":
        return plur if multiplos_imoveis else sing
    if ctx is None:
        return sing
    if fonte == "reu_gen":
        # sing/plur já têm a forma masc/fem; escolhemos por gênero do réu
        return plur if ctx.genero_reu == "f" else sing
    if fonte == "autor_gen":
        return plur if ctx.genero_autor == "f" else sing
    return None


def _aplicar_substituicoes(
    texto: str,
    dados: dict,
    ctx: "Contexto | None" = None,
    multiplos_imoveis: bool = False,
) -> str:
    resultado = texto
    for placeholder, chave in PLACEHOLDERS_PETICAO.items():
        valor = (dados.get(chave) or "").strip()
        if not valor:
            continue
        # Match case-insensitive: cobre "(REU)", "(Reu)", "(reu)" — templates
        # com small-caps em Word guardam o texto em caixa mista no .docx.
        padrao = re.compile(r"\(" + re.escape(placeholder) + r"\)", re.IGNORECASE)
        resultado = padrao.sub(lambda _m, v=valor: v, resultado)

    def _repl(m: re.Match) -> str:
        nome = m.group(1).strip().lower()
        auto = _resolver_auto(nome, ctx, multiplos_imoveis)
        if auto is not None:
            return auto
        v = dados.get(nome)
        if not v:
            return m.group(0)
        return str(v).strip() or m.group(0)

    return RE_PLACEHOLDER_TEMPLATE.sub(_repl, resultado)


def _substituir_paragrafo(
    paragrafo,
    dados: dict,
    ctx: Contexto | None = None,
    zona: str = "BODY",
    multiplos_imoveis: bool = False,
) -> None:
    if not paragrafo.runs:
        return
    texto_original = paragrafo.text
    texto_novo = _aplicar_substituicoes(
        texto_original, dados, ctx=ctx, multiplos_imoveis=multiplos_imoveis,
    )
    # Flexão só em BODY e CLOSING (HEADER vai virar UPPERCASE).
    if ctx is not None and zona in ("BODY", "CLOSING"):
        texto_novo = _inflexionar_texto(texto_novo, ctx)
    if zona == "HEADER":
        texto_novo = texto_novo.upper()
    if texto_novo == texto_original:
        return
    paragrafo.runs[0].text = texto_novo
    for run in paragrafo.runs[1:]:
        run.text = ""


def _remover_highlight_amarelo(paragrafo) -> None:
    for run in paragrafo.runs:
        if run.font.highlight_color == WD_COLOR_INDEX.YELLOW:
            run.font.highlight_color = None


def _padronizar_espacamento(paragrafo) -> None:
    pf = paragrafo.paragraph_format
    if not paragrafo.text.strip():
        # Parágrafo vazio: zera espaçamento e altura para não criar "buracos".
        pf.space_before = Pt(0)
        pf.space_after = Pt(0)
        pf.line_spacing = 1.0
        return
    pf.space_before = Pt(0)
    pf.space_after = Pt(SPACE_AFTER_PT)
    pf.line_spacing = LINE_SPACING


def _colapsar_paragrafos_vazios(doc: Document) -> None:
    """Remove parágrafos vazios consecutivos do corpo do documento.

    Mantém no máximo um parágrafo vazio entre blocos de texto. Também remove
    parágrafos vazios no início e no fim do documento.
    """
    paragrafos = list(doc.paragraphs)
    if not paragrafos:
        return
    a_remover: list = []
    anterior_vazio = True  # trata início como "vazio" -> remove vazios iniciais
    for p in paragrafos:
        eh_vazio = not p.text.strip()
        if eh_vazio and anterior_vazio:
            a_remover.append(p)
        else:
            anterior_vazio = eh_vazio
    # Remove vazios no fim (varre de trás).
    for p in reversed(paragrafos):
        if not p.text.strip() and p not in a_remover:
            a_remover.append(p)
        else:
            break
    for p in a_remover:
        elem = p._element
        parent = elem.getparent()
        if parent is not None:
            parent.remove(elem)


def _preencher_campos_docx(
    doc: Document,
    dados: dict,
    ctx: Contexto | None = None,
    zonas_paragrafos: list[str] | None = None,
    multiplos_imoveis: bool = False,
) -> None:
    paragrafos_top = list(doc.paragraphs)
    # Se nenhuma zona foi fornecida, classifica agora (caminho single-template).
    if zonas_paragrafos is None:
        zonas_paragrafos = _classificar_paragrafos(doc)
    # Alinha tamanhos defensivamente: se a lista de zonas estiver fora de sync
    # (por ex. parágrafos adicionados após classificação), trata excedentes como BODY.
    if len(zonas_paragrafos) < len(paragrafos_top):
        zonas_paragrafos = list(zonas_paragrafos) + ["BODY"] * (len(paragrafos_top) - len(zonas_paragrafos))

    for paragrafo, zona in zip(paragrafos_top, zonas_paragrafos):
        _substituir_paragrafo(
            paragrafo, dados, ctx=ctx, zona=zona,
            multiplos_imoveis=multiplos_imoveis,
        )
        _remover_highlight_amarelo(paragrafo)
        _padronizar_espacamento(paragrafo)

    for tabela in doc.tables:
        for linha in tabela.rows:
            for celula in linha.cells:
                for paragrafo in celula.paragraphs:
                    _substituir_paragrafo(
                        paragrafo, dados, ctx=ctx, zona="BODY",
                        multiplos_imoveis=multiplos_imoveis,
                    )
                    _remover_highlight_amarelo(paragrafo)
                    _padronizar_espacamento(paragrafo)

    _colapsar_paragrafos_vazios(doc)


def _converter_doc_para_docx(
    src: Path,
    destino_path: Path,
    dados: dict,
    ctx: Contexto | None = None,
    multiplos_imoveis: bool = False,
) -> None:
    cached_docx = _obter_docx_cacheado(src)
    document = Document(str(cached_docx))
    zonas = _classificar_paragrafos(document)
    _preencher_campos_docx(
        document, dados, ctx=ctx, zonas_paragrafos=zonas,
        multiplos_imoveis=multiplos_imoveis,
    )
    document.save(str(destino_path) + ".docx")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
