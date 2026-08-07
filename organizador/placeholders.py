# -*- coding: utf-8 -*-
"""Tabelas de placeholders dos modelos .docx — SEM dependências pesadas.

Vive separado de ``organizador.peticoes`` de propósito: a GUI precisa destas
tabelas para montar o formulário na abertura, mas ``peticoes`` importa
python-docx (e, com ele, lxml), o que atrasava a janela sem necessidade. Aqui só
há dados.

``peticoes`` reexporta os dois dicionários, então
``from .peticoes import PLACEHOLDERS_PETICAO`` continua válido.
"""

from __future__ import annotations

from .flexao import _strip_accents


# Placeholders reais usados nos modelos .docx da pasta "Petições".
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
    # Juiz/magistrado — preenchidos com proc.juiz (ou o que o usuário editar na GUI).
    # Match case-insensitive: cobre "(juiz)", "(Juiz)", "(JUIZ)" do cabeçalho.
    "juiz": "juiz",
    "juíza": "juiz",
    "juiza": "juiz",
    "magistrado": "juiz",
    "magistrada": "juiz",
    "nome do juiz": "juiz",
    "juiz da causa": "juiz",
    "réu": "reu",
    "reu": "reu",
    "ré": "reu",
    "re": "reu",
    "executado": "reu",
    "executada": "reu",
    "devedor": "reu",
    "devedora": "reu",
    "requerido": "reu",
    "requerida": "reu",
    "réu ou réus": "reu",
    "reu ou reus": "reu",
    "ré ou rés ou réu ou réus": "reu",
    # NB: "(nome ou nomes)" NÃO entra aqui de propósito — não deve virar o nome do
    # réu. É tratado só em PLACEHOLDERS_AUTO ("nome"/"nomes" por número de réus),
    # ex.: SERASAJUD "inclusão dos nomes dos executados".
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
# Formato: ph_lowercase -> (fonte, *formas). O nº de formas depende da fonte:
#   - "imovel"                    : (forma_sing, forma_plur)   -> flag multiplos_imoveis
#   - "reu_gen" / "autor_gen"     : (forma_masc, forma_fem)    -> gênero da parte
#   - "reu_num" / "autor_num"     : (forma_sing, forma_plur)   -> número da parte
#   - "reu_genum"                 : (masc_sing, masc_plur, fem_sing, fem_plur) -> gênero+número do réu
PLACEHOLDERS_AUTO: dict[str, tuple[str, ...]] = {
    # Imóvel propriamente dito (controlado pelo checkbox "Múltiplos imóveis").
    # Só aparecem em "Penhora imobiliária": o substantivo e o verbo "é/são".
    "imóvel ou imóveis": ("imovel", "imóvel", "imóveis"),
    "imovel ou imoveis": ("imovel", "imóvel", "imóveis"),
    "são":               ("imovel", "é", "são"),

    # Conectores (artigo/preposição) — concordam SEMPRE com o nº de réus que os segue,
    # nunca com imóvel: "(do ou dos) (executado ou executados)", "(da ou das) (parte ou partes)".
    "do ou dos":         ("reu_num", "do", "dos"),
    "da ou das":         ("reu_num", "da", "das"),
    "o ou os":           ("reu_num", "o", "os"),
    "a ou as":           ("reu_num", "a", "as"),

    # Réu — singular/plural por Contexto
    "réu ou ré":         ("reu_gen", "réu", "ré"),
    "reu ou re":         ("reu_gen", "réu", "ré"),
    "réus ou rés":       ("reu_gen", "réus", "rés"),
    "reus ou res":       ("reu_gen", "réus", "rés"),

    # Autor — singular/plural por Contexto
    "autor ou autora":      ("autor_gen", "autor", "autora"),
    "autores ou autoras":   ("autor_gen", "autores", "autoras"),

    # Concordância de NÚMERO pela quantidade de réus (singular vs. plural).
    # Usado nas petições de Citação/Juntada e em "Descumprimento de acordo".
    "parte ou partes":      ("reu_num", "parte", "partes"),
    "ré ou rés":            ("reu_num", "ré", "rés"),
    "citado ou citados":    ("reu_num", "citado", "citados"),
    "compôs ou compuseram": ("reu_num", "compôs", "compuseram"),
    "cumpriu ou cumpriram": ("reu_num", "cumpriu", "cumpriram"),
    "deu ou deram":         ("reu_num", "deu", "deram"),

    # Substantivo/verbo do polo passivo por NÚMERO de réus. Sao os
    # placeholders mais comuns dos modelos (ex.: "(réu ou réus)" aparece em
    # ~20 modelos, "(executado ou executados)" em ~8). Sem registro aqui o
    # placeholder ficava literal e a flexao gerava lixo ("(réus ou réus)").
    # O genero, quando aplicavel (réu/executado/devedor estao em FLEXOES), eh
    # ajustado depois por _inflexionar_texto na zona BODY/CLOSING.
    "réu ou réus":             ("reu_num", "réu", "réus"),
    "reu ou reus":             ("reu_num", "réu", "réus"),
    "executado ou executados": ("reu_num", "executado", "executados"),
    "devedor ou devedores":    ("reu_num", "devedor", "devedores"),
    "empresa ou empresas":     ("reu_num", "empresa", "empresas"),
    "nome ou nomes":           ("reu_num", "nome", "nomes"),
    "mantém ou mantêm":        ("reu_num", "mantém", "mantêm"),
    "mantem ou mantêm":        ("reu_num", "mantém", "mantêm"),

}


_PALAVRAS_IMOBILIARIO = ("imobiliári", "imovel", "imóvel", "matricula", "matrícula")


def _template_eh_imobiliario(nome_arquivo: str) -> bool:
    """Heurística: o template trata de imóvel(eis)? Olha o nome do arquivo."""
    n = _strip_accents(nome_arquivo).lower()
    alvos = [_strip_accents(p).lower() for p in _PALAVRAS_IMOBILIARIO]
    return any(p in n for p in alvos)
