# -*- coding: utf-8 -*-
"""Geração de petições .docx: placeholders, flexão e consolidação."""

from __future__ import annotations

import re
from copy import deepcopy
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
from .flexao import _classificar_paragrafos, _flex_judicial_treatment, _inflexionar_texto, _norm_paragrafo, _strip_accents
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
}


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


def _extrair_placeholders_template(template_path: Path) -> set[str]:
    """Lê o template e devolve os placeholders crus (em minúsculo, sem parênteses)."""
    docx_path = _obter_docx_cacheado(template_path)
    doc = Document(str(docx_path))
    encontrados: set[str] = set()

    def _scan(texto: str) -> None:
        for m in RE_PLACEHOLDER_TEMPLATE.finditer(texto):
            ph = m.group(1).strip().lower()
            if ph in _PLACEHOLDERS_IGNORADOS:
                continue
            # "10 (dez) dias", "R$ 500 (quinhentos reais)": número por extenso após
            # dígito é literal, não campo — exceto se for placeholder mapeado.
            if ph not in PLACEHOLDERS_PETICAO and ph not in PLACEHOLDERS_AUTO:
                anterior = texto[: m.start()].rstrip()
                if anterior and anterior[-1].isdigit():
                    continue
            encontrados.add(ph)

    for p in doc.paragraphs:
        _scan(p.text)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    _scan(p.text)
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

    if sufixo != ".docx":
        raise ValueError(f"Formato de template não suportado: {sufixo}. Use apenas .docx.")

    doc = Document(str(template_path))
    zonas = _classificar_paragrafos(doc)
    _preencher_campos_docx(
        doc, dados, ctx=ctx, zonas_paragrafos=zonas,
        multiplos_imoveis=multiplos_imoveis,
    )
    doc.save(str(destino_path) + ".docx")


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
    closing_emitido = False
    for _, d, zonas in docs:
        if closing_emitido:
            break
        for p, zona in zip(d.paragraphs, zonas):
            if zona == "CLOSING":
                _append(p._element)
                novas_zonas.append("CLOSING")
                closing_emitido = True
                break
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
    # Penhora salarial — campos novos.
    "o empregador xxx": "empregador",
    "trinta por cento": "percentual",
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

    # Gênero + número (4 vias) pelo réu — o placeholder mais comum das novas petições.
    "ré ou rés ou réu ou réus": ("reu_genum", "réu", "réus", "ré", "rés"),
}


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
        texto_novo = _flex_judicial_treatment(texto_novo, ctx)
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
    raise ValueError(
        f"Template não suportado: {src.name}. "
        "Converta o modelo para .docx."
    )
