# -*- coding: utf-8 -*-
"""Motor de mapeamento periódico de processos.

Reúne o núcleo reaproveitável do enriquecimento (antes só em
``scripts/reenriquecer.py``): pool de scrapers (1 login por sistema), fallback
entre sistemas da UF, merge+persistência no cache, estado JSON resumível e
relatórios. Expõe ``rodar_passada()``, que:

  1. coleta os processos das planilhas Google (``organizador.sheets``);
  2. descarta os que JÁ estão mapeados (no cache ou no estado); e
  3. raspa os pendentes p/ descobrir o sistema e enriquecer.

Usado pela GUI (botão "Sincronizar planilhas & mapear" + auto-disparo semanal)
e pelo CLI ``scripts/monitor.py``. O ``scripts/reenriquecer.py`` reimporta daqui
``PoolScrapers`` e ``_merge_e_salvar`` (deduplicação — mesma sessão/merge que o
lote de re-enriquecimento já usava).

Contrato do ``log``: recebe UMA linha SEM ``\\n`` no fim; quem chama formata
(o CLI usa ``print``; a GUI enfileira ``msg + "\\n"``).
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import (
    APP_DATA_DIR,
    PASTA_PADRAO,
    PROCESSOS_ONLINE_DIR,
    TJ_SISTEMAS,
    _ESTADO_TJ_UF,
    candidatos_tj_para_uf,
)
from .integracoes.tjsp_scraper.scrapers import criar_scraper
from .integracoes.tjsp_scraper.scrapers.popup import AtribuicaoDetectadaError
from .organizacao import (
    _autor,
    _dict_para_processo,
    _merge_processos,
    _nome_pasta,
    _resumo_txt,
    _reus_joined,
    salvar_processo_metadados,
)
from .padroes import RE_NUMERO_CNJ
from .parsing import _derivar_agregados, _uf_de_numero_cnj
from .scraper import _consultar_e_extrair

# --- caminhos -------------------------------------------------------------
# Estado/relatórios ficam em APP_DATA_DIR (local ESTÁVEL do usuário, onde já mora
# o cache PROCESSOS_ONLINE_DIR) — NÃO no _RAIZ, que no exe empacotado é a pasta
# temporária _MEIPASS e some a cada execução (perderia o estado resumível).
ESTADO_MONITOR_JSON = APP_DATA_DIR / "_monitor_estado.json"
RELATORIO_MONITOR_MD = APP_DATA_DIR / "_monitor_relatorio.md"
RELATORIO_MONITOR_CSV = APP_DATA_DIR / "_monitor_relatorio.csv"

PAUSA_ENTRE_CONSULTAS = 2.5  # s — folga anti-bloqueio (comprovado no lote anterior)

# Status "terminados" = já mapeados; pulados numa nova passada. nao_encontrado/
# falha voltam à fila só com retry_falhas (evita martelar os tribunais toda semana).
STATUS_DONE = {"enriquecido", "ainda_sem_reu", "popup_atribuicao", "sem_rota"}

# Rotas configuradas que NÃO atendem os processos da UF (confirmado ao vivo):
# a plataforma eproc do TJSP, via perfil OAB de GO/PR/RN, loga mas não hospeda
# esses processos. Pular as inoperantes evita consultas vãs.
_ROTAS_INOPERANTES = {"TJGO — eproc", "TJPR — eproc", "TJRN — eproc"}


def _candidatos(uf: str) -> list[str]:
    """Sistemas que atendem a UF, sem as rotas comprovadamente inoperantes."""
    return [c for c in candidatos_tj_para_uf(uf) if c not in _ROTAS_INOPERANTES]


# --- estado (JSON incremental) -------------------------------------------
def _carregar_estado(path: Path = ESTADO_MONITOR_JSON) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _gravar_estado(estado: dict, path: Path = ESTADO_MONITOR_JSON) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")


def _cnj_normalizado(texto: str) -> str:
    m = RE_NUMERO_CNJ.search(str(texto or ""))
    return m.group(0) if m else str(texto or "")


def _quando() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --- leitura do cache -----------------------------------------------------
def _numeros_no_cache() -> set[str]:
    """CNJs já presentes no cache de buscas online (um <numero>/metadados.json)."""
    return {
        _cnj_normalizado(meta.parent.name)
        for meta in PROCESSOS_ONLINE_DIR.glob("*/metadados.json")
    }


# --- pool de scrapers (1 login por sistema) -------------------------------
class PoolScrapers:
    """Mantém 1 scraper logado por sistema (reuso de sessão → evita bloqueio).

    ``obter`` cria+loga na 1ª vez e reusa depois; se o login de um sistema já
    falhou nesta corrida, levanta RuntimeError sem tentar de novo."""

    def __init__(self, headless: bool):
        self.headless = headless
        self._scrapers: dict[str, object] = {}
        self._login_falhou: set[str] = set()

    def obter(self, tj_label: str):
        if tj_label in self._login_falhou:
            raise RuntimeError(f"login de {tj_label} já falhou nesta corrida")
        if tj_label in self._scrapers:
            return self._scrapers[tj_label]
        site, estado, url, perfil = TJ_SISTEMAS[tj_label]
        print(f"   🔐 abrindo e logando em {tj_label} (1ª vez nesta corrida)...")
        try:
            scraper = criar_scraper(
                site=site, estado=estado, url=url,
                headless=self.headless, perfil=perfil,
            )
            scraper.login()
        except Exception as exc:
            self._login_falhou.add(tj_label)
            raise RuntimeError(f"falha ao logar em {tj_label}: {exc}") from exc
        self._scrapers[tj_label] = scraper
        return scraper

    def fechar_todos(self):
        for _tj_label, scraper in self._scrapers.items():
            try:
                scraper.fechar()
            except Exception:
                pass
        self._scrapers.clear()


# --- merge + persistência -------------------------------------------------
def _merge_e_salvar(numero: str, online_proc, existing: dict | None,
                    tags_extra=("Reenriquecido v2",)):
    """Mescla os dados online (base) com o cache atual (backfill) e grava no cache
    e em Desktop/Processos. ``existing=None`` (processo novo) salva o online direto.
    ``tags_extra`` são adicionadas (ex.: 'Mapeamento: <campanha>' p/ os novos).
    Devolve o Processo mesclado."""
    if existing:
        existing_proc = _dict_para_processo(existing)
        # base = online (fresco, traz o réu); novo = cache (backfill dos vazios,
        # preserva tag Mapeamento e valor_causa). _merge_processos troca as partes
        # pelo conjunto de maior "score" (o online agora inclui o polo passivo).
        merged = _merge_processos(online_proc, existing_proc, "")
    else:
        merged = online_proc
    for tag in tags_extra:
        if tag and tag not in merged.tags:
            merged.tags.append(tag)
    _derivar_agregados(merged)

    # 1) cache (mesmo caminho do metadados.json existente → sobrescreve/atualiza)
    salvar_processo_metadados(merged, PROCESSOS_ONLINE_DIR)
    # 2) Desktop/Processos: metadados.json + resumo.txt
    pasta = Path(PASTA_PADRAO) / _nome_pasta(numero)
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / "metadados.json").write_text(
        json.dumps(asdict(merged), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (pasta / "resumo.txt").write_text(_resumo_txt(merged), encoding="utf-8")
    return merged


# --- relatórios .md / .csv (conveniência; o estado.json é a fonte da verdade) --
_ORDEM_STATUS = [
    ("enriquecido", "✅ Mapeados e enriquecidos (autor+réu)"),
    ("ainda_sem_reu", "⚠️ Mapeados mas ainda sem réu"),
    ("popup_atribuicao", "🚫 Parados no pop-up de atribuição ao advogado"),
    ("nao_encontrado", "🔎 Não encontrados em nenhum sistema"),
    ("sem_rota", "🗺️ Sem rota (UF sem sistema suportado)"),
    ("falha", "❌ Falhas (erro técnico)"),
    ("pendente", "⏳ Pendentes (ainda não processados)"),
]


def _gerar_relatorios(estado: dict, md_path: Path = RELATORIO_MONITOR_MD,
                      csv_path: Path = RELATORIO_MONITOR_CSV) -> None:
    import csv as _csv

    md_path.parent.mkdir(parents=True, exist_ok=True)
    grupos: dict[str, list[tuple[str, dict]]] = {st: [] for st, _ in _ORDEM_STATUS}
    for numero, r in estado.items():
        grupos.setdefault(r.get("status", "pendente"), []).append((numero, r))

    linhas = [
        "# Relatório de monitoramento (planilhas → mapeamento de sistema)",
        "",
        f"_Atualizado: {datetime.now().isoformat(timespec='seconds')}_",
        "",
        f"- **Total no estado:** {len(estado)}",
        f"- ✅ Mapeados+enriquecidos: **{len(grupos.get('enriquecido', []))}**",
        f"- ⚠️ Mapeados sem réu: **{len(grupos.get('ainda_sem_reu', []))}**",
        f"- 🔎 Não encontrados: **{len(grupos.get('nao_encontrado', []))}**",
        f"- 🗺️ Sem rota: **{len(grupos.get('sem_rota', []))}**",
        f"- ⏳ Pendentes: **{len(grupos.get('pendente', []))}**",
        "",
    ]
    for st, titulo in _ORDEM_STATUS:
        itens = grupos.get(st) or []
        if not itens:
            continue
        linhas.append(f"## {titulo} — {len(itens)}")
        linhas.append("")
        for numero, r in sorted(itens):
            sistema = r.get("sistema") or "—"
            uf = r.get("uf") or "—"
            extra = ""
            if st == "enriquecido":
                extra = f" — réu: {r.get('reu') or '—'}"
            elif st in ("nao_encontrado", "popup_atribuicao"):
                extra = f" — {r.get('erro') or r.get('obs') or ''}"
            linhas.append(f"- `{numero}` ({uf} / {sistema}){extra}")
        linhas.append("")
    # Se o .csv estiver ABERTO (Excel trava no Windows), só avisa e segue — nunca
    # derruba a passada por causa do relatório.
    try:
        md_path.write_text("\n".join(linhas), encoding="utf-8")
        with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
            w = _csv.writer(f, delimiter=";")
            w.writerow(["numero", "uf", "sistema", "status", "campanha",
                        "tem_reu", "autor", "reu", "obs", "erro", "quando"])
            for numero, r in sorted(estado.items()):
                w.writerow([
                    numero, r.get("uf", ""), r.get("sistema", ""), r.get("status", ""),
                    r.get("campanha", ""), "sim" if r.get("tem_reu") else "nao",
                    r.get("autor", ""), r.get("reu", ""),
                    r.get("obs", ""), r.get("erro", ""), r.get("quando", ""),
                ])
    except (PermissionError, OSError):
        pass


# --- seleção: o que já está mapeado × o que falta -------------------------
def processos_ja_mapeados(estado: dict) -> set[str]:
    """CNJs que NÃO precisam ser raspados: presentes no cache OU com status
    terminado (``STATUS_DONE``) no estado do monitor."""
    mapeados = set(_numeros_no_cache())
    for numero, r in estado.items():
        if r.get("status") in STATUS_DONE:
            mapeados.add(_cnj_normalizado(numero))
    return mapeados


def selecionar_pendentes(alvos_planilhas: dict[str, str], estado: dict,
                         retry_falhas: bool = False) -> dict[str, dict]:
    """De ``{numero: campanha}`` das planilhas, devolve só os NÃO mapeados como
    ``{numero: {uf, candidatos, existing=None, campanha}}`` (pronto p/ a passada)."""
    mapeados = processos_ja_mapeados(estado)
    pendentes: dict[str, dict] = {}
    for numero, campanha in alvos_planilhas.items():
        if _cnj_normalizado(numero) in mapeados:
            continue
        st = estado.get(numero, {}).get("status")
        if st in ("nao_encontrado", "falha") and not retry_falhas:
            continue
        uf = (_uf_de_numero_cnj(numero) or "").upper()
        pendentes[numero] = {
            "uf": uf,
            "candidatos": _candidatos(uf),
            "existing": None,
            "campanha": campanha,
        }
    return pendentes


# --- passada principal ----------------------------------------------------
def rodar_passada(
    headless: bool = True,
    dry_run: bool = False,
    limit: int = 0,
    retry_falhas: bool = False,
    so_rapidos: bool = False,
    log: Callable[[str], None] = print,
    cancel: "threading.Event | None" = None,
) -> dict:
    """Uma passada de monitoramento: planilhas → filtra mapeados → raspa pendentes.

    Descobre para cada pendente qual sistema o hospeda (fallback ``candidatos_tj_
    para_uf``, 1 login por sistema via ``PoolScrapers``), enriquece e grava no
    cache + estado. Resumível: reabrir pula o que já terminou.

    ``cancel`` (Event) permite ao botão da GUI interromper entre processos.
    Devolve ``{total, ja_mapeados, mapeados_agora, nao_encontrados, pendentes}``.
    """
    from . import sheets  # import tardio: só exige o google-auth quando chamado

    log("🔑 coletando processos das planilhas Google...")
    registros = sheets.coletar_processos_das_planilhas(log=log)
    # {numero: campanha} — 1ª campanha vence (dedup já feito em sheets)
    alvos_planilhas: dict[str, str] = {}
    for r in registros:
        alvos_planilhas.setdefault(r["numero"], r.get("campanha", ""))

    estado = _carregar_estado()
    pendentes = selecionar_pendentes(alvos_planilhas, estado, retry_falhas=retry_falhas)
    ja = len(alvos_planilhas) - len(pendentes)
    log(f"📊 {len(alvos_planilhas)} nas planilhas | {ja} já mapeados | "
        f"{len(pendentes)} pendentes")

    resumo = {
        "total": len(alvos_planilhas), "ja_mapeados": ja,
        "mapeados_agora": 0, "nao_encontrados": 0, "pendentes": len(pendentes),
    }
    if dry_run:
        log("🔎 DRY-RUN: nada raspado.")
        return resumo
    if not pendentes:
        log("✅ nada a mapear — planilhas em dia com o cache.")
        return resumo

    # Semeia o estado (preserva os já processados).
    for numero, info in pendentes.items():
        estado.setdefault(numero, {"status": "pendente"})
        estado[numero]["uf"] = info["uf"]
        estado[numero]["campanha"] = info["campanha"]
    _gravar_estado(estado)

    if so_rapidos:  # só TOTP (PJe/eproc); deixa e-SAJ (2FA por e-mail, lento) p/ depois
        for info in pendentes.values():
            info["candidatos"] = [
                c for c in info["candidatos"] if TJ_SISTEMAS[c][0] in ("pje", "eproc")
            ]

    # Ordena por TIPO de sistema (eproc/PJe = TOTP, rápidos; e-SAJ por último) p/
    # agrupar o mesmo login e minimizar reaberturas de navegador.
    _rank = {"eproc": 0, "pje": 1, "esaj": 2}

    def _ordem(n: str):
        cand = pendentes[n]["candidatos"] or []
        if not cand:
            return (9, "~")
        return (_rank.get(TJ_SISTEMAS[cand[0]][0], 8), cand[0])

    fila = sorted(pendentes, key=_ordem)
    if limit:
        fila = fila[:limit]
    log(f"➡️  processando {len(fila)} (restantes ficam pendentes p/ retomar)")

    def _cancelado() -> bool:
        return cancel is not None and cancel.is_set()

    pool = PoolScrapers(headless=headless)
    try:
        for i, numero in enumerate(fila, 1):
            if _cancelado():
                log("⏹️  cancelado pelo usuário.")
                break
            info = pendentes[numero]
            uf, candidatos = info["uf"], info["candidatos"]
            campanha = info.get("campanha")
            tags_extra = ([f"Mapeamento: {campanha}", "Mapeado online"]
                          if campanha else ["Mapeado online"])
            log(f"[{i}/{len(fila)}] {numero} (UF {uf or '—'}"
                + (f", {campanha})" if campanha else ")"))

            if not candidatos:
                estado[numero] = {"status": "sem_rota", "uf": uf, "campanha": campanha,
                                  "obs": "UF sem sistema suportado", "quando": _quando()}
                _gravar_estado(estado)
                log("   🗺️ sem rota — pulando.")
                continue

            ultimo_erro = ""
            resolvido = False
            for tj_label in candidatos:
                if _cancelado():
                    break
                site, estado_tj, _url, _perfil = TJ_SISTEMAS[tj_label]
                tribunal = _ESTADO_TJ_UF.get(estado_tj, (tj_label, uf))[0]
                try:
                    scraper = pool.obter(tj_label)
                except RuntimeError as exc:
                    ultimo_erro = str(exc)
                    log(f"   ⚠️ {exc}")
                    continue
                try:
                    proc = _consultar_e_extrair(
                        scraper, site, numero, tribunal, uf,
                        modo_docs="nenhum",
                        log=lambda s: log("   " + s.rstrip()),
                    )
                except AtribuicaoDetectadaError as exc:
                    estado[numero] = {
                        "status": "popup_atribuicao", "uf": uf, "sistema": tj_label,
                        "campanha": campanha, "obs": exc.observacao, "quando": _quando(),
                    }
                    log(f"   🚫 pop-up de atribuição em {tj_label} — abortado e registrado.")
                    resolvido = True
                    break  # NÃO tenta outros sistemas (pedido do usuário)
                except RuntimeError as exc:
                    ultimo_erro = str(exc)
                    log(f"   🔎 {tj_label}: {exc}")
                    time.sleep(PAUSA_ENTRE_CONSULTAS)
                    continue
                except Exception as exc:  # erro técnico inesperado
                    ultimo_erro = f"{type(exc).__name__}: {exc}"
                    log(f"   ❌ {tj_label}: {ultimo_erro}")
                    time.sleep(PAUSA_ENTRE_CONSULTAS)
                    continue

                merged = _merge_e_salvar(numero, proc, None, tags_extra)
                autor, reu = _autor(merged), _reus_joined(merged)
                tem_reu = bool(reu.strip())
                estado[numero] = {
                    "status": "enriquecido" if tem_reu else "ainda_sem_reu",
                    "uf": uf, "sistema": tj_label, "campanha": campanha,
                    "tem_reu": tem_reu, "autor": autor, "reu": reu, "quando": _quando(),
                }
                log(f"   {'✅' if tem_reu else '⚠️'} {tj_label}: "
                    f"autor='{autor or '—'}' réu='{reu or '—'}'")
                resumo["mapeados_agora"] += 1
                resolvido = True
                time.sleep(PAUSA_ENTRE_CONSULTAS)
                break

            if not resolvido and not _cancelado():
                estado[numero] = {
                    "status": "nao_encontrado", "uf": uf, "campanha": campanha,
                    "sistema": (candidatos or ["—"])[-1],
                    "erro": ultimo_erro or "não encontrado em nenhum sistema",
                    "quando": _quando(),
                }
                resumo["nao_encontrados"] += 1
                log("   🔎 não encontrado em nenhum sistema.")

            _gravar_estado(estado)
    finally:
        pool.fechar_todos()
        _gravar_estado(estado)
        try:
            _gerar_relatorios(estado)
        except Exception:
            pass

    log(f"✅ passada concluída: {resumo['mapeados_agora']} mapeados agora, "
        f"{resumo['nao_encontrados']} não encontrados, {resumo['ja_mapeados']} já mapeados.")
    return resumo
