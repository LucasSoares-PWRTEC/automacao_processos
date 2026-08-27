# -*- coding: utf-8 -*-
"""Expressões regulares e mapas usados no parsing de PDFs do eproc."""

from __future__ import annotations

import re

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
# Inclui as formas FEMININAS (EXECUTADA/REQUERIDA/RÉ…): o e-SAJ/PJe trazem o papel
# flexionado por gênero e, sem elas, o réu (mulher) não era reconhecido em _autor/
# _reus_joined → processo aparecia "sem réu" mesmo tendo o polo passivo preenchido.
# Fase de RECURSO e RECONVENÇÃO entram aqui pelo mesmo critério que já valia para
# AGRAVANTE/AGRAVADO: quem recorre ocupa o polo ativo DAQUELA fase. Ficaram de
# fora por omissão, e a omissão era invisível porque os parsers já canonizam para
# elas: `parser_esaj._PAPEL_ESAJ` mapeia "apte"→APELANTE e "apdo"→APELADO
# declarando no comentário que o destino são estas listas, e
# `parser_pje._PAPEIS_ATIVO_PJE` já as trata como polo para deduplicar partes.
# Só `padroes` não sabia — então `_autor`/`_reus_joined` liam o processo como se
# ele não tivesse parte nenhuma. Medido em 27/08/2026: 4 processos do TJPA/TJSP
# tinham as duas partes salvas e apareciam como "sem réu E sem credor".
# NÃO acrescente aqui papel de TERCEIRO (interessado, arrematante, perito,
# testemunha, administrador, fiador, credor hipotecário, inventariado): eles não
# são polo, e virar "réu" no endereçamento da petição é pior que ficar vazio.
PAPEIS_ATIVO = ["AUTOR", "AUTORA", "EXEQUENTE", "REQUERENTE", "EMBARGANTE",
                "AGRAVANTE", "IMPETRANTE", "RECLAMANTE", "SUSCITANTE",
                "APELANTE", "RECORRENTE", "RECONVINTE", "INTERPELANTE"]
# "REVEL" é polo passivo: o TJDFT rotula assim o executado/réu declarado revel
# (em geral com "DP - CURADORIA ESPECIAL" como representante). Sem ele o réu não
# era reconhecido em _reus_joined e o processo ficava só com o autor.
# Os papéis LONGOS de recurso vêm ANTES de "RE"/"RÉ": estas listas viram
# alternação de regex (RE_PARTE_ATO, RE_EXTRATO_PARTE, RE_CABECALHO_PARTES,
# _evento_parte_papel) e, em alternação, o primeiro alternativo que casa ganha.
# "RE" casaria o prefixo de "RECORRIDO"/"RECONVINDO"; aqui o backtracking ainda
# salvaria pelos âncoras, mas depender disso é frágil — ordem explícita é grátis.
PAPEIS_PASSIVO = ["RECORRIDO", "RECORRIDA", "RECONVINDO", "RECONVINDA",
                  "RÉU", "REU", "RÉ", "RE", "EXECUTADO", "EXECUTADA",
                  "REQUERIDO", "REQUERIDA", "EMBARGADO", "EMBARGADA",
                  "AGRAVADO", "AGRAVADA", "IMPETRADO", "IMPETRADA",
                  "RECLAMADO", "RECLAMADA", "SUSCITADO", "SUSCITADA",
                  "APELADO", "APELADA", "INTERPELADO", "INTERPELADA",
                  "REVEL"]

# Versões em maiúsculas, como conjunto, para teste de pertinência por parte.
# As listas acima já estão em maiúsculas, mas o código as normalizava de novo em
# tempo de execução: `papel in [pp.upper() for pp in PAPEIS_PASSIVO]` reconstruía
# uma lista de 18 strings PARA CADA PARTE de CADA processo. Use estes conjuntos.
PAPEIS_ATIVO_UP = frozenset(p.upper() for p in PAPEIS_ATIVO)
PAPEIS_PASSIVO_UP = frozenset(p.upper() for p in PAPEIS_PASSIVO)

# Classes em que o polo passivo é CONSTITUTIVO: não existe execução sem
# executado, nem monitória sem requerido. Serve para separar "o tribunal não
# tem réu" de "a raspagem perdeu o réu" — ver `classe_exige_polo_passivo`.
#
# Lista de INCLUSÃO, de propósito: só afirma o que é certo. Classes de rito não
# adversarial (recuperação judicial, falência, habilitação de crédito, carta
# precatória, petição cível) ficam de fora e nunca são acusadas de dado faltante.
# Casada por SUBSTRING sobre a classe sem acento e em maiúsculas, porque os
# tribunais sufixam à vontade ("EXECUÇÃO DE TÍTULO EXTRAJUDICIAL - CPC",
# "CUMPRIMENTO DE SENTENÇA - CENTRASE CÍVEL BH").
CLASSES_COM_POLO_PASSIVO = (
    "EXECUCAO",           # execução, execução de título extrajudicial (+ CPC)
    "PROCEDIMENTO COMUM",
    "PROCEDIMENTO SUMARIO",
    "CUMPRIMENTO DE SENTENCA",
    "CUMPRIMENTO PROVISORIO",
    "MONITORIA",
    "COBRANCA",
    "DESPEJO",
    "REINTEGRACAO",       # reintegração / manutenção de posse
    "MANUTENCAO DE POSSE",
    "EMBARGOS",           # à execução, de terceiro, parciais — todos têm embargado
    "BUSCA E APREENSAO",
)

# Rótulos/ruído que o PDF cola ao nome da parte (sem separador) e que precisam
# ser removidos do início do nome capturado. Ex.: o eproc emite
# "Histórico de RepresentantesITAU UNIBANCO S.A. (...)".
RE_RUIDO_NOME_PARTE = re.compile(
    r"^(?:"
    r"Hist[óo]rico\s+de\s+Representantes"
    r"|Hist[óo]rico\s+de\s+Retifica[çc][ãa]o\s+das\s+Partes"
    r"|Parte\s+ativa\s+no\s+Domic[íi]lio\s+Judicial\s+Eletr[ôo]nico[^\n]*?DJE"
    r")\s*",
    re.IGNORECASE,
)

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
