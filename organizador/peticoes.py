# -*- coding: utf-8 -*-
"""Geração de petições .docx: placeholders, flexão e consolidação."""

from __future__ import annotations

import difflib
import re
from copy import deepcopy
from datetime import date as _date
from pathlib import Path
from typing import Callable

from docx import Document
from docx.enum.text import WD_COLOR_INDEX
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.shared import Pt

from .config import LINE_SPACING, SPACE_AFTER_PT
from .flexao import _classificar_paragrafos, _flex_judicial_treatment, _inflexionar_texto, _norm_paragrafo, _resolver_disjuncoes_flexao, _strip_accents
from .models import Contexto, Processo


# Placeholder em template: "(texto em minúsculo até 40 chars, sem pontuação)".
# Pega tanto os conhecidos ("(secretaria)") quanto novos ("(testemunha)"),
# mas ignora parentéticas naturais com pontuação ou maiúsculas mistas.
RE_PLACEHOLDER_TEMPLATE = re.compile(
    r"\(([a-záéíóúâêôãõçñü][a-záéíóúâêôãõçñü ]{1,38}[a-záéíóúâêôãõçñü])\)"
)

# Parentéticas que casam com o regex mas são texto literal, não campos a preencher.
# (Apenas afeta a detecção de "campos desconhecidos" da GUI; a geração já as preserva.)
_PLACEHOLDERS_IGNORADOS = {
    "assinado eletronicamente",
    # Princípio jurídico citado no corpo do texto ("registro de penhora
    # (cooperação e eficiência) nos respectivos autos"), não um campo a preencher.
    "cooperação e eficiência",
    # Anotações editoriais em citações de jurisprudência (ex.: "...súmula em
    # 04/07/2025) (destaquei)"). São literais, nunca campos a preencher.
    "destaquei", "destacamos", "destaquem",
    "grifei", "grifamos", "grifo nosso", "grifos nossos", "grifo meu",
    "sem grifo no original", "sem grifos no original",
    "negritei", "negritamos", "sublinhei", "sublinhamos",
}


# Campo editável dos modelos novos: "(Campo editável no sistema - <descrição>)".
# Ex.: "(Campo editável no sistema - adicionar o novo endereço localizado)".
# Diferente dos placeholders normais (tem maiúscula, hífen e passa de 40 chars),
# então precisa de regex própria. A <descrição> vira um campo na GUI; ao gerar,
# o valor digitado entra em NEGRITO no lugar da tag (ou a tag é removida se vazio).
RE_CAMPO_EDITAVEL = re.compile(
    r"\(\s*campo\s+edit[áa]vel(?:\s+no\s+sistema)?\s*[-–—:]\s*(.+?)\s*\)",
    re.IGNORECASE,
)

# Verbo+artigo de instrução no início da descrição ("adicionar o ...", "informar
# a ..."), removidos para o rótulo ficar limpo ("novo endereço localizado").
_RE_VERBO_INSTRUCAO = re.compile(
    r"^(?:adicionar|inserir|informar|preencher|incluir|colocar|digitar)\s+"
    r"(?:o|a|os|as|um|uma)\s+",
    re.IGNORECASE,
)


def _campo_editavel_rotulo(descricao: str) -> str:
    """Descrição da tag → rótulo/chave estável do campo (ex.: 'adicionar o novo
    endereço localizado' → 'novo endereço localizado'). É a chave usada tanto na
    GUI quanto no dict `dados`; por ser determinística, extração e substituição
    sempre coincidem."""
    base = re.sub(r"\s+", " ", descricao or "").strip()
    limpo = _RE_VERBO_INSTRUCAO.sub("", base).strip()
    return limpo or base


def _obter_docx_cacheado(template_path: Path) -> Path:
    """Valida que o template já está no formato .docx suportado."""
    if template_path.suffix.lower() != ".docx":
        raise ValueError(
            f"Template não suportado: {template_path.name}. "
            "Converta o modelo para .docx."
        )
    return template_path


_PALAVRAS_IMOBILIARIO = ("imobiliári", "imovel", "imóvel", "matricula", "matrícula")


def _template_eh_imobiliario(nome_arquivo: str) -> bool:
    """Heurística: o template trata de imóvel(eis)? Olha o nome do arquivo."""
    n = _strip_accents(nome_arquivo).lower()
    alvos = [_strip_accents(p).lower() for p in _PALAVRAS_IMOBILIARIO]
    return any(p in n for p in alvos)


def _texto_sem_vermelho(paragrafo) -> str:
    """Texto do parágrafo com os trechos VERMELHOS (preenchimento manual)
    trocados por espaços do mesmo tamanho.

    Vermelho é preenchido pelo advogado no Word (a geração o preserva verbatim,
    não substitui) — então NÃO deve virar campo no app. Ex.: em 'Penhora
    salarial', '...empregador (xxx), endereço: xxx' está em vermelho e o '(xxx)'
    não pode aparecer como campo obrigatório. Manter o tamanho preserva as
    posições p/ a heurística de 'número por extenso após dígito'.
    """
    partes: list[str] = []
    for r in paragrafo.runs:
        partes.append(" " * len(r.text) if _eh_run_vermelho(r) else r.text)
    return "".join(partes)


def _extrair_placeholders_template(template_path: Path) -> set[str]:
    """Lê o template e devolve os placeholders crus (em minúsculo, sem parênteses).
    Ignora o que está em vermelho (preenchimento manual — não é campo do app)."""
    docx_path = _obter_docx_cacheado(template_path)
    doc = Document(str(docx_path))
    encontrados: set[str] = set()

    def _scan(paragrafo) -> None:
        texto = _texto_sem_vermelho(paragrafo)
        for m in RE_PLACEHOLDER_TEMPLATE.finditer(texto):
            ph = m.group(1).strip().lower()
            if ph in _PLACEHOLDERS_IGNORADOS:
                continue
            # "10 (dez) dias", "R$ 500 (quinhentos reais)": número por extenso após
            # dígito é literal, não campo — exceto se for placeholder mapeado.
            if ph not in PLACEHOLDERS_PETICAO and ph not in PLACEHOLDERS_AUTO:
                anterior = texto[: m.start()].rstrip()
                # Número por extenso após dígito ou "%": "10 (dez) dias",
                # "30% (trinta por cento)" — literal, não campo.
                if anterior and (anterior[-1].isdigit() or anterior[-1] == "%"):
                    continue
            encontrados.add(ph)
        # Campos editáveis "(Campo editável no sistema - X)" viram um campo na GUI
        # cujo rótulo/chave é a descrição limpa.
        for m in RE_CAMPO_EDITAVEL.finditer(texto):
            encontrados.add(_campo_editavel_rotulo(m.group(1)))

    for p in doc.paragraphs:
        _scan(p)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    _scan(p)
    return encontrados


def _iterar_blocos_doc(doc: Document):
    """Percorre blocos do corpo em ordem real, preservando tabelas."""
    paragrafos = {p._element: p for p in doc.paragraphs}
    tabelas = {t._element: t for t in doc.tables}
    for child in doc.element.body.iterchildren():
        if isinstance(child, CT_P) and child in paragrafos:
            yield "paragraph", paragrafos[child]
        elif isinstance(child, CT_Tbl) and child in tabelas:
            yield "table", tabelas[child]


def gerar_peticoes(
    proc: Processo,
    templates: list[Path],
    destino: Path,
    dados: dict,
    log: Callable[[str], None] = print,
    documento_unico: bool = False,
    ctx: Contexto | None = None,
    multiplos_imoveis: bool = False,
) -> list[Path]:
    destino.mkdir(parents=True, exist_ok=True)
    gerados: list[Path] = []

    if documento_unico and len(templates) > 1:
        try:
            saida = _gerar_peticao_consolidada(
                templates, destino, dados, ctx,
                multiplos_imoveis=multiplos_imoveis,
            )
            log(f"  OK: documento consolidado -> {saida.name}")
            gerados.append(saida)
        except Exception as exc:
            log(f"  ERRO ao consolidar: {exc}")
        return gerados

    for tmpl in templates:
        try:
            saida = _gerar_peticao_unica(
                tmpl, destino, dados, ctx,
                multiplos_imoveis=multiplos_imoveis,
            )
            log(f"  OK: {tmpl.stem}")
            gerados.append(saida)
        except Exception as exc:
            log(f"  ERRO em {tmpl.name}: {exc}")
    return gerados


def _gerar_peticao_unica(
    template_path: Path,
    destino: Path,
    dados: dict,
    ctx: Contexto | None = None,
    multiplos_imoveis: bool = False,
) -> Path:
    sufixo = template_path.suffix.lower()
    nome_base = template_path.stem
    destino_path = destino / f"{nome_base} - {dados.get('numero', 'sem_numero')}"

    if sufixo != ".docx":
        raise ValueError(f"Formato de template não suportado: {sufixo}. Use apenas .docx.")

    doc = Document(str(template_path))
    zonas = _classificar_paragrafos(doc)
    _preencher_campos_docx(
        doc, dados, ctx=ctx, zonas_paragrafos=zonas,
        multiplos_imoveis=multiplos_imoveis,
    )
    out = Path(str(destino_path) + ".docx")
    doc.save(str(out))
    return out


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

    # Carrega todos os templates .docx e classifica zonas.
    docs: list[tuple[Path, Document, list[str]]] = []
    for tmpl in templates:
        docx_path = _obter_docx_cacheado(tmpl)
        d = Document(str(docx_path))
        zonas = _classificar_paragrafos(d)
        docs.append((tmpl, d, zonas))

    # Documento destino herda estilos do 1º template.
    primeiro_path, primeiro_doc, primeiro_zonas = docs[0]
    primeiro_docx = _obter_docx_cacheado(primeiro_path)
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
    idx_paragrafo = 0
    for tipo_bloco, bloco in _iterar_blocos_doc(primeiro_doc):
        if tipo_bloco != "paragraph":
            continue
        zona = primeiro_zonas[idx_paragrafo]
        idx_paragrafo += 1
        if zona == "HEADER":
            _append(bloco._element)
            novas_zonas.append("HEADER")

    # 2) BODYs com dedup. Parágrafos curtos (<25 chars normalizados) sempre passam.
    vistos: set[str] = set()
    for _, d, zonas in docs:
        idx_paragrafo = 0
        for tipo_bloco, bloco in _iterar_blocos_doc(d):
            if tipo_bloco == "table":
                _append(bloco._element)
                continue

            zona = zonas[idx_paragrafo]
            idx_paragrafo += 1
            if zona != "BODY":
                continue
            chave = _norm_paragrafo(bloco.text)
            if chave and len(chave) >= 25:
                if chave in vistos:
                    continue
                vistos.add(chave)
            _append(bloco._element)
            novas_zonas.append("BODY")

    # 3) CLOSING do 1º template que tenha (ou injeta fechamento default).
    # Emite TODOS os parágrafos de fechamento desse template (fórmula + local/
    # data + assinatura + OAB), não só o primeiro — o estado CLOSING engloba o
    # bloco inteiro até o fim do documento.
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


_RE_DO_S_EXECUTADO_S = re.compile(
    r"(?<!\w)do\(s\)\s+executado\(s\)(?!\w)",
    re.IGNORECASE,
)
_RE_EXECUTADO_S = re.compile(r"(?<!\w)executado\(s\)(?!\w)", re.IGNORECASE)


def _preservar_caso_local(original: str, novo: str) -> str:
    if original.isupper():
        return novo.upper()
    if original[:1].isupper():
        return novo[:1].upper() + novo[1:]
    return novo


def _resolver_parenteticos_numero(
    texto: str,
    ctx: "Contexto | None",
    multiplos_imoveis: bool,
) -> str:
    """Resolve formas legadas como "do(s) executado(s)" dos modelos .docx.

    Mantemos isto restrito aos termos que precisam de decisão automática; outras
    parentéticas, especialmente instruções manuais como "passível(is)", ficam
    preservadas.
    """
    genero_reu = ctx.genero_reu if ctx else "m"
    numero_reu = ctx.numero_reu if ctx else "s"
    key_reu = genero_reu + numero_reu
    prep_executado = {
        "ms": "do", "fs": "da", "mp": "dos", "fp": "das",
    }.get(key_reu, "do")
    forma_executado = {
        "ms": "executado", "fs": "executada",
        "mp": "executados", "fp": "executadas",
    }.get(key_reu, "executado")

    def _repl_do_executado(m: re.Match) -> str:
        return _preservar_caso_local(m.group(0), f"{prep_executado} {forma_executado}")

    def _repl_executado(m: re.Match) -> str:
        return _preservar_caso_local(m.group(0), forma_executado)

    texto = _RE_DO_S_EXECUTADO_S.sub(_repl_do_executado, texto)
    texto = _RE_EXECUTADO_S.sub(_repl_executado, texto)
    return texto


def _resolver_auto(
    ph: str,
    ctx: "Contexto | None",
    multiplos_imoveis: bool,
) -> str | None:
    spec = PLACEHOLDERS_AUTO.get(ph)
    if not spec:
        return None
    fonte = spec[0]
    if fonte == "imovel":
        _, sing, plur = spec
        return plur if multiplos_imoveis else sing
    # Sem contexto não há como flexionar por gênero/número: usa a 1ª forma (sing./masc.).
    if ctx is None:
        return spec[1]
    if fonte == "reu_gen":
        # sing/plur já têm a forma masc/fem; escolhemos por gênero do réu
        _, sing, plur = spec
        return plur if ctx.genero_reu == "f" else sing
    if fonte == "autor_gen":
        _, sing, plur = spec
        return plur if ctx.genero_autor == "f" else sing
    if fonte == "reu_num":
        _, sing, plur = spec
        return plur if ctx.numero_reu == "p" else sing
    if fonte == "autor_num":
        _, sing, plur = spec
        return plur if ctx.numero_autor == "p" else sing
    if fonte == "reu_genum":
        _, masc_sing, masc_plur, fem_sing, fem_plur = spec
        if ctx.genero_reu == "f":
            return fem_plur if ctx.numero_reu == "p" else fem_sing
        return masc_plur if ctx.numero_reu == "p" else masc_sing
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
        if chave in {"autor", "reu"}:
            valor = valor.upper()
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

    resultado = RE_PLACEHOLDER_TEMPLATE.sub(_repl, resultado)
    resultado = _resolver_parenteticos_numero(resultado, ctx, multiplos_imoveis)
    # Disjunções de concordância "(do ou dos ou da ou das)", "(executado ou
    # executada ou executados ou executadas)" etc. — resolvidas por último, pois
    # as disjunções mapeadas ao NOME do réu (PLACEHOLDERS_PETICAO, ex.: "(réu ou
    # réus)") já foram trocadas acima; aqui sobra só a concordância gramatical.
    return _resolver_disjuncoes_flexao(resultado, ctx)


def _cobertura_resolvivel(
    texto: str,
    dados: dict,
    ctx: "Contexto | None" = None,
    multiplos_imoveis: bool = False,
) -> list[bool]:
    """Marca quais caracteres de `texto` pertencem a um placeholder que será de
    fato substituído.

    Usado para resgatar placeholders escritos em vermelho (ex.:
    "(do ou dos) (executado ou executados)" nos modelos de penhora). Sem isto, o
    vermelho inteiro seria tratado como preenchimento manual e o placeholder
    ficaria literal. Só o vermelho SEM placeholder resolvível é mascarado.
    """
    coberto = [False] * len(texto)

    def _marcar(ini: int, fim: int) -> None:
        for i in range(ini, fim):
            coberto[i] = True

    # Placeholders mapeados a dados (case-insensitive: cobre "(Executado ou Executados)").
    for placeholder, chave in PLACEHOLDERS_PETICAO.items():
        if not (dados.get(chave) or "").strip():
            continue
        padrao = re.compile(r"\(" + re.escape(placeholder) + r"\)", re.IGNORECASE)
        for m in padrao.finditer(texto):
            _marcar(m.start(), m.end())

    # Placeholders de concordância "(X ou Y)" e demais campos preenchidos por
    # `dados`. Restringe o auto-resgate às disjunções ("X ou Y"): formas avulsas
    # como "(são)"/"(é)" colidem com parentéticas de preenchimento manual
    # (ex.: "passível(is)") e NÃO devem ser resgatadas do vermelho.
    for m in RE_PLACEHOLDER_TEMPLATE.finditer(texto):
        nome = m.group(1).strip().lower()
        if " ou " in nome and _resolver_auto(nome, ctx, multiplos_imoveis) is not None:
            _marcar(m.start(), m.end())
        elif str(dados.get(nome) or "").strip():
            _marcar(m.start(), m.end())

    for padrao in (
        _RE_DO_S_EXECUTADO_S,
        _RE_EXECUTADO_S,
    ):
        for m in padrao.finditer(texto):
            _marcar(m.start(), m.end())

    return coberto


# "De <origem> para <destino>, <data>" — quando a comarca de destino coincide
# com a origem (sede do escritório, "Belo Horizonte/MG"), o "para <destino>"
# vira redundante: mantemos só o primeiro local.
_RE_LOCAL_DUPLICADO = re.compile(r"(?i)\bDe\s+(.+?)\s+para\s+(.+?)(\s*,)")


def _colapsar_local_duplicado(texto: str) -> str:
    def _repl(m: re.Match) -> str:
        origem = m.group(1).strip()
        destino = m.group(2).strip()
        if _strip_accents(origem).casefold() == _strip_accents(destino).casefold():
            return f"De {origem}{m.group(3)}"
        return m.group(0)

    return _RE_LOCAL_DUPLICADO.sub(_repl, texto)


_MESES = (
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)

# Data por extenso ao fim da linha de fechamento "De <local> para <local>, DATA".
# Ancorada no "De ... ," para NÃO tocar datas do corpo da petição.
_RE_DATA_FECHAMENTO = re.compile(
    r"(?i)(\bDe\s+.+?,\s*)\d{1,2}\s+de\s+(?:" + "|".join(_MESES) + r")\s+de\s+\d{4}\.?"
)

# Identifica a linha de local + data do fecho JÁ transformada (com ou sem "De")
# pela terminação em data por extenso. Usada para tirar o negrito indevido da
# UF nessa linha; só aplicada na zona CLOSING, então não toca datas do corpo.
_RE_LINHA_LOCAL_DATA = re.compile(
    r"(?i)\d{1,2}\s+de\s+(?:" + "|".join(_MESES) + r")\s+de\s+\d{4}\.?\s*$"
)


def _data_por_extenso(data: "_date | None" = None) -> str:
    """Data no formato brasileiro 'D de mês de AAAA' (sem zero à esquerda)."""
    d = data or _date.today()
    return f"{d.day} de {_MESES[d.month - 1]} de {d.year}"


def _atualizar_data_fechamento(texto: str, data: "_date | None" = None) -> str:
    """Troca a data fixa da linha de fechamento pela data de emissão (hoje).
    Quando há apenas um local (sem "para"), remove o "De " inicial."""
    def _repl(m: re.Match) -> str:
        prefixo = m.group(1)
        if not re.search(r"\bpara\b", prefixo, re.IGNORECASE):
            prefixo = re.sub(r"(?i)^\bDe\s+", "", prefixo)
        return f"{prefixo}{_data_por_extenso(data)}"
    return _RE_DATA_FECHAMENTO.sub(_repl, texto)


_RE_HEADER_JUDICIAL = re.compile(
    r"(?i)\b(?:excelent|merit[ií]ssim).*\bju[ii]z(?:a)?(?:\s+de\s+direito)?\b"
)
_RE_HEADER_SAUDACAO = re.compile(
    r"(?is)^\s*(.*?\bju[ii]z(?:a)?)(?:\s+de\s+direito)?\b.*$"
)


def _montar_header_enderecamento(texto: str, dados: dict, ctx: Contexto | None = None) -> str:
    """Monta o endereçamento completo quando o modelo traz só a saudação judicial."""
    base = _flex_judicial_treatment(texto, ctx).strip()
    if not _RE_HEADER_JUDICIAL.search(base):
        return base.upper()
    m = _RE_HEADER_SAUDACAO.match(base)
    saudacao = (m.group(1).strip() if m else base).rstrip(" ,.;:-")

    vara = (dados.get("vara") or "").strip()
    comarca = (dados.get("comarca") or "").strip()
    estado = (dados.get("estado") or dados.get("uf") or "").strip().upper()
    if not vara:
        return saudacao.upper()

    _VARA_MASC = ("JUÍZO", "JUIZO", "JUIZADO", "FORO")
    prep_vara = "DO" if vara.upper().startswith(_VARA_MASC) else "DA"
    header = f"{saudacao} DE DIREITO {prep_vara} {vara}"
    if comarca:
        prep = "DA" if comarca.upper().startswith("COMARCA") else "DO"
        header += f" {prep} {comarca}"
        if estado:
            header += f"/{estado}"
    return header.upper()


def _eh_run_vermelho(run) -> bool:
    """O run usa fonte vermelha? (texto de preenchimento manual nos modelos)."""
    cor = run.font.color
    if cor is None or cor.type is None or cor.rgb is None:
        return False
    try:
        r, g, b = cor.rgb
    except (TypeError, ValueError):
        return False
    return r >= 0x80 and g <= 0x60 and b <= 0x60


def _eh_run_roxo(run) -> bool:
    """O run usa marca-texto ROXO/ROSA?

    Convenção dos modelos: dentro do trecho roxo, as VARIÁVEIS de conteúdo
    (placeholders de PLACEHOLDERS_PETICAO, ex.: "(nome ou nomes)") ficam
    LITERAIS — o advogado as preenche à mão — enquanto as conjugações ao redor
    ("(do ou dos)", "(executado ou executados ...)") são resolvidas normalmente.
    """
    return run.font.highlight_color in (WD_COLOR_INDEX.PINK, WD_COLOR_INDEX.VIOLET)


def _aplicar_formato_base(destino_run, base_run) -> None:
    """Copia formatação de caractere do run base (sem highlight nem cor)."""
    destino_run.bold = base_run.bold
    destino_run.italic = base_run.italic
    destino_run.underline = base_run.underline
    if base_run.font.size is not None:
        destino_run.font.size = base_run.font.size
    if base_run.font.name is not None:
        destino_run.font.name = base_run.font.name


def _fmt_basico(run) -> tuple:
    """Formatação de caractere relevante do run (sem cor: a cor é inerente ao
    run reaproveitado ou herdada do estilo nos runs novos)."""
    return (run.bold, run.italic, run.underline, run.font.size, run.font.name)


def _aplicar_fmt(run, fmt: tuple) -> None:
    bold, italic, underline, size, name = fmt
    run.bold = bold
    run.italic = italic
    run.underline = underline
    if size is not None:
        run.font.size = size
    if name is not None:
        run.font.name = name
    run.font.highlight_color = None


def _reescrever_preservando_formato(paragrafo, texto_original: str, texto_final: str) -> None:
    """Reescreve o parágrafo preservando a formatação de caractere do modelo.

    Diferente do colapso ingênuo (que jogava tudo no 1º run e fazia o parágrafo
    inteiro herdar o negrito de um placeholder como "(autor)"), aqui alinhamos o
    texto original ao final: trechos inalterados mantêm seu formato original e o
    valor que substitui um placeholder herda o formato do placeholder substituído
    (negrito continua negrito; corpo continua corpo).
    """
    fmts_orig: list[tuple] = []
    for run in paragrafo.runs:
        fmts_orig.extend([_fmt_basico(run)] * len(run.text))
    default = fmts_orig[0] if fmts_orig else (None, None, None, None, None)

    sm = difflib.SequenceMatcher(None, texto_original, texto_final, autojunk=False)
    fmts_final: list[tuple] = [default] * len(texto_final)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(j2 - j1):
                if i1 + k < len(fmts_orig):
                    fmts_final[j1 + k] = fmts_orig[i1 + k]
        elif tag in ("replace", "insert"):
            # Valor inserido/substituído herda o formato do início da região
            # original correspondente (o do placeholder, quando é uma troca).
            if i1 < len(fmts_orig):
                ref = fmts_orig[i1]
            elif fmts_orig:
                ref = fmts_orig[-1]
            else:
                ref = default
            for j in range(j1, j2):
                fmts_final[j] = ref
        # "delete": nada a inserir.

    # Agrupa caracteres consecutivos de mesmo formato em pedaços.
    pedacos: list[list] = []
    for ch, f in zip(texto_final, fmts_final):
        if pedacos and pedacos[-1][1] == f:
            pedacos[-1][0].append(ch)
        else:
            pedacos.append([[ch], f])
    if not pedacos:
        pedacos = [[[], default]]

    runs = paragrafo.runs
    base = runs[0]
    base.text = "".join(pedacos[0][0])
    _aplicar_fmt(base, pedacos[0][1])
    for run in runs[1:]:
        run.text = ""
    for chars, f in pedacos[1:]:
        novo = paragrafo.add_run("".join(chars))
        _aplicar_fmt(novo, f)


# Sentinelas em área de uso privado do Unicode: mascaram os trechos vermelhos
# (preenchimento manual) durante substituição/flexão para que passem intactos —
# nem o regex de placeholders nem a flexão os tocam — e voltem verbatim depois.
_SENT_ABRE = ""
_SENT_FECHA = ""
_RE_SENTINELA = re.compile("(\\d+)")


def _reescrever_runs(
    paragrafo,
    texto_mascarado: str,
    preservados: list[tuple[str, object, object]],
) -> None:
    """Reescreve o parágrafo (ainda com sentinelas), trocando cada sentinela pelo
    trecho preservado original: os vermelhos voltam com sua cor (preenchimento
    manual); os roxos/rosas voltam com o marca-texto (variável a preencher). Cada
    entrada de `preservados` é (texto, cor_rgb | None, marca_texto | None)."""
    # Usa um run não-vermelho como base de formatação (cor padrão do corpo).
    base = next(
        (r for r in paragrafo.runs if not _eh_run_vermelho(r)),
        paragrafo.runs[0],
    )

    pedacos: list[tuple[str, object, object]] = []
    pos = 0
    for m in _RE_SENTINELA.finditer(texto_mascarado):
        if m.start() > pos:
            pedacos.append((texto_mascarado[pos:m.start()], None, None))
        pedacos.append(preservados[int(m.group(1))])
        pos = m.end()
    if pos < len(texto_mascarado):
        pedacos.append((texto_mascarado[pos:], None, None))
    if not pedacos:
        pedacos.append((texto_mascarado, None, None))

    def _aplica_cor_hl(run, rgb, hl) -> None:
        run.font.highlight_color = hl  # None limpa; PINK/VIOLET preserva o roxo
        if rgb is not None:
            run.font.color.rgb = rgb

    for run in paragrafo.runs:
        run.text = ""
    base.text = pedacos[0][0]
    _aplica_cor_hl(base, pedacos[0][1], pedacos[0][2])
    for texto_pedaco, rgb, hl in pedacos[1:]:
        novo = paragrafo.add_run(texto_pedaco)
        _aplicar_formato_base(novo, base)
        _aplica_cor_hl(novo, rgb, hl)


def _aplicar_campo_editavel(paragrafo, dados: dict) -> None:
    """Substitui as tags "(Campo editável no sistema - X)" do parágrafo.

    O valor digitado pelo usuário herda o formato do modelo (sem forçar negrito).
    Sem valor, a tag é removida; se o parágrafo ficar vazio,
    `_colapsar_paragrafos_vazios` o descarta depois.
    """
    texto = paragrafo.text
    if not paragrafo.runs:
        return
    # Run-base para herdar tamanho/fonte; ignora vermelhos (preenchimento manual).
    base = next(
        (r for r in paragrafo.runs if not _eh_run_vermelho(r)),
        paragrafo.runs[0],
    )
    base_bold = base.bold
    base_italic = base.italic
    base_underline = base.underline
    base_size = base.font.size
    base_name = base.font.name

    # Pedaços (texto, é_variável?). Ambos herdam o formato base do modelo.
    pedacos: list[tuple[str, bool]] = []
    pos = 0
    for m in RE_CAMPO_EDITAVEL.finditer(texto):
        if m.start() > pos:
            pedacos.append((texto[pos:m.start()], False))
        valor = (dados.get(_campo_editavel_rotulo(m.group(1))) or "").strip()
        if valor:
            pedacos.append((valor, True))
        pos = m.end()
    if pos < len(texto):
        pedacos.append((texto[pos:], False))
    if not pedacos:
        pedacos = [("", False)]

    def _fmt(run, variavel: bool) -> None:
        run.bold = False if variavel else base_bold
        run.italic = base_italic
        run.underline = base_underline
        if base_size is not None:
            run.font.size = base_size
        if base_name is not None:
            run.font.name = base_name
        run.font.highlight_color = None

    for run in paragrafo.runs:
        run.text = ""
    base.text = pedacos[0][0]
    _fmt(base, pedacos[0][1])
    for texto_pedaco, variavel in pedacos[1:]:
        novo = paragrafo.add_run(texto_pedaco)
        _fmt(novo, variavel)


def _substituir_paragrafo(
    paragrafo,
    dados: dict,
    ctx: Contexto | None = None,
    zona: str = "BODY",
    multiplos_imoveis: bool = False,
) -> None:
    if not paragrafo.runs:
        return
    # Campo editável "(Campo editável no sistema - X)": tratado à parte porque o
    # texto não casa o regex genérico de placeholders nem é vermelho, e porque só
    # a variável deve ficar em negrito (o mecanismo de runs abaixo não faz isso).
    if RE_CAMPO_EDITAVEL.search(paragrafo.text):
        _aplicar_campo_editavel(paragrafo, dados)
        return
    # Base de trabalho = concatenação dos runs, NÃO paragrafo.text. Runs dentro de
    # hyperlink (ex.: e-mails clicáveis) entram em paragrafo.text mas não em
    # paragrafo.runs; usar paragrafo.text deixaria texto_original mais longo que o
    # que o loop de mascaramento e a cobertura percorrem, fazendo texto_final
    # divergir sempre e a reescrita estourar (IndexError) ou duplicar o link.
    # Com a concatenação dos runs, parágrafos assim caem no early-return e ficam
    # intactos; a substituição só age no que os runs de fato contêm.
    texto_original = "".join(run.text for run in paragrafo.runs)

    # Mascara os runs vermelhos (instruções de preenchimento manual) com
    # sentinelas, para que a substituição/flexão não os altere.
    coberto = _cobertura_resolvivel(
        texto_original, dados, ctx=ctx, multiplos_imoveis=multiplos_imoveis,
    )
    # Runs com marca-texto ROXO/ROSA: as VARIÁVEIS de conteúdo dentro deles
    # (placeholders de PLACEHOLDERS_PETICAO, ex.: "(nome ou nomes)") ficam
    # LITERAIS; só as conjugações ao redor são resolvidas. Marca os caracteres
    # dessas variáveis para mascará-las como preenchimento manual.
    roxo_arr: list[bool] = []
    for run in paragrafo.runs:
        roxo_arr.extend([_eh_run_roxo(run)] * len(run.text))
    preservar_roxo = [False] * len(texto_original)
    if any(roxo_arr):
        for _ph in PLACEHOLDERS_PETICAO:
            for m in re.finditer(r"\(" + re.escape(_ph) + r"\)", texto_original, re.IGNORECASE):
                if all(roxo_arr[i] for i in range(m.start(), m.end())):
                    for i in range(m.start(), m.end()):
                        preservar_roxo[i] = True

    # Cada preservado é (texto, cor_rgb | None, marca_texto | None): vermelho leva
    # cor; roxo leva marca-texto; ambos passam intactos pela substituição/flexão.
    preservados: list[tuple[str, object, object]] = []
    partes: list[str] = []
    seg_chars: list[str] = []
    seg_rgb: object = None
    seg_hl: object = None
    seg_tipo: object = None

    def _flush_segmento() -> None:
        nonlocal seg_rgb, seg_hl, seg_tipo
        if not seg_chars:
            return
        segmento = "".join(seg_chars)
        # Segmento só de espaços não vira sentinela: evita colar o dígito da
        # sentinela ao placeholder resolvido vizinho.
        if segmento.strip():
            partes.append(f"{_SENT_ABRE}{len(preservados)}{_SENT_FECHA}")
            preservados.append((segmento, seg_rgb, seg_hl))
        else:
            partes.append(segmento)
        seg_chars.clear()
        seg_rgb = None
        seg_hl = None
        seg_tipo = None

    pos = 0
    for run in paragrafo.runs:
        vermelho = _eh_run_vermelho(run)
        rgb = run.font.color.rgb if vermelho else None
        hl = run.font.highlight_color
        for ch in run.text:
            # Vermelho coberto por placeholder resolvível passa intacto (literal),
            # para ser substituído normalmente; o resto do vermelho é mascarado.
            if vermelho and not coberto[pos]:
                if seg_tipo != "vermelho":
                    _flush_segmento()
                    seg_tipo = "vermelho"
                    seg_rgb = rgb
                seg_chars.append(ch)
            elif preservar_roxo[pos]:
                # Variável roxa: mascarada p/ ficar literal; as conjugações ao
                # redor (fora da variável) seguem o caminho normal e resolvem.
                if seg_tipo != "roxo":
                    _flush_segmento()
                    seg_tipo = "roxo"
                    seg_hl = hl
                seg_chars.append(ch)
            else:
                _flush_segmento()
                partes.append(ch)
            pos += 1
    _flush_segmento()
    texto_mascarado = "".join(partes)

    texto_novo = _aplicar_substituicoes(
        texto_mascarado, dados, ctx=ctx, multiplos_imoveis=multiplos_imoveis,
    )
    # Flexão só em BODY e CLOSING (HEADER tem montagem própria).
    if ctx is not None and zona in ("BODY", "CLOSING"):
        texto_novo = _inflexionar_texto(texto_novo, ctx)
    if zona == "HEADER":
        texto_novo = _montar_header_enderecamento(texto_novo, dados, ctx)
    texto_novo = _colapsar_local_duplicado(texto_novo)
    texto_novo = _atualizar_data_fechamento(texto_novo)

    # Texto final desmascarado, para comparação e para o caminho sem vermelho.
    texto_final = _RE_SENTINELA.sub(lambda m: preservados[int(m.group(1))][0], texto_novo)
    if texto_final == texto_original:
        # Nada mudou: preserva os runs originais (e suas cores) intactos.
        return
    if not preservados:
        # Reescreve preservando o formato de caractere do modelo: o valor que
        # substitui um placeholder bold (ex.: "(autor)") continua bold, e o corpo
        # ao redor continua sem negrito — sem o parágrafo inteiro herdar o
        # negrito do 1º run.
        _reescrever_preservando_formato(paragrafo, texto_original, texto_final)
        return
    _reescrever_runs(paragrafo, texto_novo, preservados)


def _remover_highlight_amarelo(paragrafo) -> None:
    for run in paragrafo.runs:
        if run.font.highlight_color == WD_COLOR_INDEX.YELLOW:
            run.font.highlight_color = None


def _aplicar_negrito(paragrafo) -> None:
    """Força o parágrafo inteiro a negrito (usado no endereçamento/HEADER)."""
    for run in paragrafo.runs:
        run.font.bold = True


def _forcar_maiusculas(paragrafo) -> None:
    """Força o texto de todos os runs a caixa alta (endereçamento/HEADER).

    Garante o cabeçalho 100% em maiúsculas mesmo quando parte do texto veio da
    reescrita de trechos vermelhos/sentinela — que são reinseridos verbatim e
    escapariam do .upper() feito em _montar_header_enderecamento.
    """
    for run in paragrafo.runs:
        if run.text and run.text != run.text.upper():
            run.text = run.text.upper()


def _desnegritar_local_data(paragrafo) -> None:
    """Remove o negrito da linha de local + data do fecho (ex.: 'Comarca/UF, 23
    de julho de 2026').

    A UF dessa linha não deve ficar em negrito; a reescrita pesada da linha
    (colapso de local + troca de data) pode fazê-la herdar o negrito do
    contexto. Escopo restrito à linha de local+data (não toca a fórmula de
    fecho nem a assinatura/OAB).
    """
    for run in paragrafo.runs:
        if run.text.strip():
            run.font.bold = False


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


# Linha de assinatura que carrega a OAB (ex.: "OAB/MG 104.784").
_RX_LINHA_OAB = re.compile(r"\boab\b", re.I)


def _chave_oab(texto: str) -> str:
    """Normaliza uma linha de OAB p/ comparação: sem espaços/pontos/traços,
    minúscula. 'OAB/MG 104.784' e 'OAB / MG 104784' viram a mesma chave."""
    return re.sub(r"[\s./-]", "", texto or "").lower()


def _deduplicar_oab(doc: Document) -> None:
    """Remove linhas de OAB repetidas no fecho da petição.

    O modelo traz a OAB da casa como literal ('OAB/MG 104.784') e, logo abaixo,
    o placeholder '(OAB do estado de tramitação do processo)'. Quando o processo
    tramita no mesmo estado, o placeholder resolve para o MESMO texto e a OAB
    aparece duas vezes. Aqui mantemos só a primeira; quando os estados diferem
    (ex.: OAB/MG + OAB/SP), as duas linhas são preservadas.
    """
    vistos: set[str] = set()
    a_remover: list = []
    for p in doc.paragraphs:
        texto = p.text.strip()
        if not texto or not _RX_LINHA_OAB.search(texto):
            continue
        chave = _chave_oab(texto)
        if chave in vistos:
            a_remover.append(p)
        else:
            vistos.add(chave)
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
        if zona == "HEADER":
            _aplicar_negrito(paragrafo)
            _forcar_maiusculas(paragrafo)
        elif zona == "CLOSING" and _RE_LINHA_LOCAL_DATA.search(paragrafo.text):
            _desnegritar_local_data(paragrafo)
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

    _deduplicar_oab(doc)
    _colapsar_paragrafos_vazios(doc)


def _converter_doc_para_docx(
    src: Path,
    destino_path: Path,
    dados: dict,
    ctx: Contexto | None = None,
    multiplos_imoveis: bool = False,
) -> None:
    raise ValueError(
        f"Template não suportado: {src.name}. "
        "Converta o modelo para .docx."
    )
