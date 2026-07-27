# -*- coding: utf-8 -*-
"""Detecção de gênero/número e flexão de vocabulário controlado."""

from __future__ import annotations

import re
import unicodedata

from docx import Document

from .models import Contexto, Parte, Processo
from .padroes import PAPEIS_ATIVO, PAPEIS_PASSIVO


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


def _detectar_genero_juiz(nome: str) -> str:
    """Heurística para juiz: considera pessoa física; retorna 'f' ou 'm'."""
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
    # Gênero do juiz (se presente)
    g_juiz = _detectar_genero_juiz(proc.juiz) if proc.juiz else "m"
    return Contexto(
        genero_autor=g_autor, numero_autor=n_autor,
        genero_reu=g_reu, numero_reu=n_reu,
        genero_juiz=g_juiz,
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


# Padrões para tratamento judicial no HEADER
# Aceita: "EXCELENTÍSSIMO SENHOR DOUTOR JUIZ", "EXCELENTÍSSIMO SENHOR JUIZ",
#         "EXCELENTÍSSIMO(A) SENHOR(A) DOUTOR(A) JUIZ(A)", etc.
_MARK = r"(?:[OA](?:\(A\))?|\(A\)|)"
_RE_JUDICIAL_TREATMENT = re.compile(
    rf"(EXCELENT[IÍ]SSIM){_MARK}\s+(SENHOR){_MARK}\s+(DOUTOR){_MARK}\s+(JUIZ){_MARK}",
    re.IGNORECASE,
)
_RE_JUDICIAL_TREATMENT_SHORT = re.compile(
    rf"(EXCELENT[IÍ]SSIM){_MARK}\s+(SENHOR){_MARK}\s+(JUIZ){_MARK}",
    re.IGNORECASE,
)
_RE_JUDICIAL_TREATMENT_MERIT = re.compile(
    rf"(MERIT[IÍ]SSIM){_MARK}\s+(SENHOR){_MARK}\s+(DOUTOR){_MARK}\s+(JUIZ){_MARK}",
    re.IGNORECASE,
)
_RE_JUDICIAL_TREATMENT_MERIT_SHORT = re.compile(
    rf"(MERIT[IÍ]SSIM){_MARK}\s+(SENHOR){_MARK}\s+(JUIZ){_MARK}",
    re.IGNORECASE,
)


def _preservar_caso(original: str, novo: str) -> str:
    if not original:
        return novo
    # Só tratamos como CAIXA-ALTA quando há mais de uma letra: um artigo de
    # uma letra ("O"/"A" no início da frase) é maiúscula-inicial, não all-caps
    # — senão "O executado" virava "OS executados" em vez de "Os executados".
    if len(original) > 1 and original.isupper():
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


def _flex_judicial_treatment(texto: str, ctx: Contexto | None) -> str:
    """Ajusta marcadores de gênero no tratamento judicial (Ex.: EXCELENTÍSSIMO SENHOR DOUTOR JUIZ)."""
    if not ctx:
        return texto
    genero = ctx.genero_juiz  # 'm' ou 'f'

    def _repl_4(match: re.Match) -> str:
        e, s, d, j = match.group(1), match.group(2), match.group(3), match.group(4)
        if genero == 'f':
            parts = [f"{e}A", f"{s}A", f"{d}A", f"{j}A"]
        else:
            parts = [f"{e}O", s, d, j]
        return ' '.join(parts)

    def _repl_3(match: re.Match) -> str:
        e, s, j = match.group(1), match.group(2), match.group(3)
        if genero == 'f':
            parts = [f"{e}A", f"{s}A", f"{j}A"]
        else:
            parts = [f"{e}O", s, j]
        return ' '.join(parts)

    texto = _RE_JUDICIAL_TREATMENT.sub(_repl_4, texto)
    texto = _RE_JUDICIAL_TREATMENT_SHORT.sub(_repl_3, texto)
    texto = _RE_JUDICIAL_TREATMENT_MERIT.sub(_repl_4, texto)
    texto = _RE_JUDICIAL_TREATMENT_MERIT_SHORT.sub(_repl_3, texto)
    return texto


def _inflexionar_texto(texto: str, ctx: Contexto | None) -> str:
    """Aplica flexão sobre vocabulário controlado, preservando capitalização."""
    if not ctx or not texto:
        return texto
    if _RE_CITACAO.search(texto):
        return texto

    # Primeiro, ajusta o tratamento judicial (se houver)
    texto = _flex_judicial_treatment(texto, ctx)

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
# Disjunções "(X ou Y [ou Z ou W])" de gênero×número
# ---------------------------------------------------------------------------
# Modelos novos escrevem a concordância como uma disjunção que enumera as
# formas, ex.: "(do ou dos ou da ou das)", "(executado ou executada ou
# executados ou executadas)", "(o ou a ou os ou as)". A ordem das formas varia
# entre modelos e algumas vêm capitalizadas / com mais de 40 chars, então o
# regex de placeholder não as pega. Aqui identificamos a família morfológica
# (preposição/artigo ou substantivo de papel) pelo CONJUNTO de formas ofertadas
# — independente de ordem/caixa — e escolhemos a que casa o gênero+número do
# polo. Preposições/artigos concordam com o réu (execução contra o executado);
# substantivos de papel já sabem seu lado por ROLE_TO_SIDE.
_FAMILIAS_DISJUNCAO: list[tuple[dict[str, str], str]] = (
    [(fam, "reu") for fam in PREP_FAMILIAS]
    + [(FLEXOES[base], ROLE_TO_SIDE[base]) for base in FLEXOES]
)

# Parentética com ao menos um " ou " separando as formas (case-insensitive).
_RE_DISJUNCAO_FLEXAO = re.compile(r"\(([^()]{2,150})\)")
_RE_SEP_OU = re.compile(r"\s+ou\s+", re.IGNORECASE)


def _escolher_forma_disjuncao(alts: list[str], ctx: "Contexto") -> str | None:
    """Dada a lista de formas ofertadas, devolve a que concorda com o contexto
    (mantendo a caixa que o modelo escreveu), ou None se não for uma disjunção
    de concordância reconhecida (deixa o texto intacto nesse caso)."""
    norm = [_strip_accents(a).lower() for a in alts]
    for forms, side in _FAMILIAS_DISJUNCAO:
        fam_norm = {_strip_accents(v).lower(): k for k, v in forms.items()}
        if not all(n in fam_norm for n in norm):
            continue
        keys = [fam_norm[n] for n in norm]  # ex.: ['fs','fp','ms','mp']
        g = ctx.genero_autor if side == "autor" else ctx.genero_reu
        num = ctx.numero_autor if side == "autor" else ctx.numero_reu
        target = g + num
        if target in keys:
            return alts[keys.index(target)]
        # Alvo não ofertado (ex.: modelo só deu formas femininas): casa pelo
        # número desejado, mantendo o gênero que o modelo escolheu; senão gênero.
        for i, k in enumerate(keys):
            if k[1] == num:
                return alts[i]
        for i, k in enumerate(keys):
            if k[0] == g:
                return alts[i]
        return alts[0]
    return None


def _eh_disjuncao_flexao(texto: str) -> bool:
    """True se `texto` (sem parênteses) é uma disjunção de concordância cujas
    formas pertencem TODAS a uma mesma família conhecida (preposição/artigo ou
    substantivo de papel) — ou seja, o sistema resolve por gênero+número do polo
    sem precisar de input manual. Independe de contexto/caixa/ordem das formas.

    Ex.: "o ou a ou os ou as", "pela ou pelo ou pelos ou pelas",
    "do ou dos ou da ou das". NÃO casa "sim ou não" nem instruções livres.
    """
    partes = [p.strip() for p in _RE_SEP_OU.split((texto or "").strip())]
    if len(partes) < 2 or any(not p for p in partes):
        return False
    norm = [_strip_accents(a).lower() for a in partes]
    for forms, _side in _FAMILIAS_DISJUNCAO:
        fam_norm = {_strip_accents(v).lower() for v in forms.values()}
        if all(n in fam_norm for n in norm):
            return True
    return False


def _resolver_disjuncoes_flexao(texto: str, ctx: "Contexto | None") -> str:
    """Resolve as disjunções de concordância "(X ou Y ...)" pelo gênero+número
    do polo. Não toca parentéticas que não sejam disjunções de formas conhecidas
    (ex.: "(sim ou não)", instruções manuais)."""
    if ctx is None or not texto:
        return texto

    def _repl(m: re.Match) -> str:
        partes = [p.strip() for p in _RE_SEP_OU.split(m.group(1).strip())]
        if len(partes) < 2 or any(not p for p in partes):
            return m.group(0)
        escolhido = _escolher_forma_disjuncao(partes, ctx)
        return escolhido if escolhido is not None else m.group(0)

    return _RE_DISJUNCAO_FLEXAO.sub(_repl, texto)