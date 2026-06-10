# -*- coding: utf-8 -*-
"""Organização de pastas, resumo/índice e carregamento de metadados."""

from __future__ import annotations

import concurrent.futures
import csv
import itertools
import json
import os
import shutil
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Iterable

from .config import CACHE_DIR, PASTA_NAO_IDENTIFICADOS
from .models import Advogado, AtoDocumento, Evento, Parte, Prazo, Processo
from .padroes import MAPA_UF_CNJ, PAPEIS_ATIVO, PAPEIS_PASSIVO, RE_CNJ_TR, RE_NUMERO_CNJ
from .parsing import _derivar_agregados, parse_pdf
from .texto import extrair_texto


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
    (ex.: dicas linguísticas `genero_autor`/`numero_autor` da integração interna). Trata a
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


def salvar_processo_metadados(proc: Processo, base: Path) -> None:
    """Persiste o Processo como ``<base>/<numero>/metadados.json`` (mesmo formato
    lido por _carregar_processos). Usado p/ lembrar processos buscados online."""
    numero = (proc.numero or "").strip()
    if not numero:
        return
    try:
        pasta = base / numero
        pasta.mkdir(parents=True, exist_ok=True)
        (pasta / "metadados.json").write_text(
            json.dumps(asdict(proc), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


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
