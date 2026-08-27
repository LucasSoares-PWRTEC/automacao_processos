# -*- coding: utf-8 -*-
"""Motor de mapeamento periódico de processos.

Reúne o núcleo reaproveitável do enriquecimento (antes só em
``scripts/reenriquecer.py``): pool de scrapers (1 login por sistema), fallback
entre sistemas da UF, merge+persistência no cache, estado JSON resumível e
relatórios. Expõe ``rodar_passada()``, que:

  1. coleta os processos das planilhas Google (``organizador.sheets``);
  2. descarta os que JÁ estão mapeados (no cache ou no estado); e
  3. raspa os pendentes p/ descobrir o sistema e enriquecer.

Cada processo tem UMA tentativa por dia (ver ``DIARIO_TENTATIVAS``): repetir no
mesmo dia não muda o resultado e só soma requisições no tribunal.

Usado pela GUI (botão "Sincronizar planilhas & mapear" — não há disparo
automático) e pelo CLI ``scripts/monitor.py``. O ``scripts/reenriquecer.py`` reimporta daqui
``PoolScrapers`` e ``_merge_e_salvar`` (deduplicação — mesma sessão/merge que o
lote de re-enriquecimento já usava).

Contrato do ``log``: recebe UMA linha SEM ``\\n`` no fim; quem chama formata
(o CLI usa ``print``; a GUI enfileira ``msg + "\\n"``).
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from .config import (
    APP_DATA_DIR,
    CONFIG_ENV_PATH,
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
from .parsing import _derivar_agregados, _uf_de_numero_cnj, classe_exige_polo_passivo
from .scraper import (
    _TAG_SISTEMA, _consultar_e_extrair, _preparar_env_local, marcar_sistema,
)

# --- caminhos -------------------------------------------------------------
# Estado/relatórios ficam em APP_DATA_DIR (local ESTÁVEL do usuário, onde já mora
# o cache PROCESSOS_ONLINE_DIR) — NÃO no _RAIZ, que no exe empacotado é a pasta
# temporária _MEIPASS e some a cada execução (perderia o estado resumível).
ESTADO_MONITOR_JSON = APP_DATA_DIR / "_monitor_estado.json"
RELATORIO_MONITOR_MD = APP_DATA_DIR / "_monitor_relatorio.md"
RELATORIO_MONITOR_CSV = APP_DATA_DIR / "_monitor_relatorio.csv"

PAUSA_ENTRE_CONSULTAS = 2.5  # s — folga anti-bloqueio (comprovado no lote anterior)

# Status "terminados" = já mapeados; pulados numa nova passada. Os de STATUS_RETRY
# voltam à fila só com retry_falhas (evita martelar os tribunais toda semana).
STATUS_DONE = {"enriquecido", "ainda_sem_reu", "popup_atribuicao", "sem_rota"}
# "reu_faltando" mora AQUI, não em STATUS_DONE: é raspagem incompleta (ver
# `_status_do_resultado`), e antes se disfarçava de "ainda_sem_reu" — terminado,
# nunca reprocessado.
STATUS_RETRY = {"nao_encontrado", "falha", "reu_faltando", "credor_faltando"}


def _status_do_resultado(merged, tem_reu: bool, tem_autor: bool = True) -> str:
    """Classifica o resultado de uma raspagem bem-sucedida.

    Sem réu numa classe adversarial não é "processo de um polo só": é dado
    perdido. No PJe o #poloPassivo é populado por AJAX depois da timeline e,
    quando a espera estoura, o processo é gravado só com o autor. Separar os dois
    casos é o que permite reprocessar um sem tocar no outro.

    O polo ATIVO é checado primeiro e SEM ressalva de classe: não existe processo
    cível sem quem o propôs, então ausência de autor é sempre raspagem
    incompleta — não há o equivalente a `classe_exige_polo_passivo` para decidir.
    Antes esta função só testava o réu, e um processo que voltava com o polo
    passivo mas sem o ativo saía marcado "enriquecido", caía em ``STATUS_DONE`` e
    nunca voltava à fila. Medido em 27/08/2026: 2 processos do TJSP raspados com
    sucesso no e-SAJ ficaram assim, sem credor e declarados prontos.
    """
    if not tem_autor:
        return "credor_faltando"
    if tem_reu:
        return "enriquecido"
    if classe_exige_polo_passivo(getattr(merged, "classe", "")):
        return "reu_faltando"
    return "ainda_sem_reu"


def _candidatos(uf: str) -> list[str]:
    """Sistemas que atendem a UF, sem as rotas comprovadamente inoperantes.

    A lista mora em `config.ROTAS_INOPERANTES` (era duplicada aqui e no
    scripts/reenriquecer.py) e já sai filtrada de `candidatos_tj_para_uf`."""
    return candidatos_tj_para_uf(uf)


def _teto_navegadores() -> int:
    """Quantos navegadores podem ficar abertos ao mesmo tempo.

    Lê o Modo leve do ``ui_state.json`` — a passada também roda pelo CLI
    (scripts/monitor.py), sem a GUI para consultar. Falha de leitura cai no
    padrão, nunca derruba a passada."""
    from .maquina import resolver_modo_leve

    try:
        from .config import UI_STATE_PATH

        salvo = json.loads(UI_STATE_PATH.read_text(encoding="utf-8")).get("modo_leve")
    except Exception:
        salvo = None  # sem estado salvo: resolver_modo_leve decide pela máquina
    return MAX_NAVEGADORES_MODO_LEVE if resolver_modo_leve(salvo) else MAX_NAVEGADORES


# --- estado (JSON incremental) -------------------------------------------
def _carregar_estado(path: Path = ESTADO_MONITOR_JSON) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _gravar_estado(estado: dict, path: Path = ESTADO_MONITOR_JSON) -> None:
    """Grava o estado da passada. Gravação atômica (tmp + replace) para uma
    interrupção no meio não deixar o arquivo truncado — ele é o que permite
    retomar a passada de onde parou.

    Sem `indent=2`: o estado tem uma entrada por processo e era regravado a cada
    processo (ver _GravadorThrottled); a indentação inflava o arquivo ~2x sem
    ninguém ler à mão."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(estado, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    os.replace(tmp, path)


# Intervalo/lote da gravação de estado durante a passada.
GRAVAR_ESTADO_CADA = 25      # processos
GRAVAR_ESTADO_SEGUNDOS = 30.0


class _GravadorThrottled:
    """Regula a frequência de gravação do estado da passada.

    O estado era regravado por INTEIRO depois de CADA processo. Com ~1.600
    processos e um estado que já passa de 500 KB, isso é centenas de MB escritos
    em disco por passada — em HD mecânico o custo aparece.

    Aqui a gravação acontece a cada `GRAVAR_ESTADO_CADA` processos ou
    `GRAVAR_ESTADO_SEGUNDOS`, o que vier primeiro, mais um `finalizar()`
    garantido no fim. O pior caso de perda por queda de energia é um lote de
    processos que teriam de ser reconsultados — nada é corrompido, porque a
    passada é idempotente por número."""

    def __init__(self, path: Path = ESTADO_MONITOR_JSON):
        self.path = path
        self._pendentes = 0
        self._ultimo = time.monotonic()

    def talvez(self, estado: dict) -> bool:
        self._pendentes += 1
        agora = time.monotonic()
        if (
            self._pendentes >= GRAVAR_ESTADO_CADA
            or (agora - self._ultimo) >= GRAVAR_ESTADO_SEGUNDOS
        ):
            self.finalizar(estado)
            return True
        return False

    def finalizar(self, estado: dict) -> None:
        """Gravação incondicional (fim da passada, cancelamento, erro)."""
        _gravar_estado(estado, self.path)
        self._pendentes = 0
        self._ultimo = time.monotonic()


def _cnj_normalizado(texto: str) -> str:
    m = RE_NUMERO_CNJ.search(str(texto or ""))
    return m.group(0) if m else str(texto or "")


def _quando() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --- cooldown: UMA tentativa por processo por dia -------------------------
# Repetir a consulta no mesmo dia não melhora o resultado: o processo não está
# naquele sistema, ou o tribunal está fora, ou o dado não existe mesmo. O que a
# repetição faz é somar requisições — e é volume que faz o tribunal bloquear a
# conta compartilhada. Então cada processo tem UMA tentativa por dia civil; a
# próxima é amanhã.
#
# O registro é um log append-only, e não um campo no estado, por dois motivos:
#   - ele é gravado ANTES de tocar no tribunal, então uma queda do app (ou um
#     "Parar") no meio da passada não devolve o processo à fila em minutos —
#     esse laço "abre, tenta, cai, reabre, tenta" é justamente o que geraria
#     flood sem ninguém notar;
#   - o estado inteiro passa de 500 KB e só é regravado em lote (ver
#     _GravadorThrottled), enquanto um append de ~30 bytes com fsync custa nada.
DIARIO_TENTATIVAS = APP_DATA_DIR / "_monitor_tentativas.log"


def _hoje() -> str:
    return date.today().isoformat()


def _regravar_diario(numeros: set[str], path: Path) -> None:
    hoje = _hoje()
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            "".join(f"{hoje}\t{n}\n" for n in sorted(numeros)), encoding="utf-8"
        )
        os.replace(tmp, path)
    except OSError:
        pass  # poda é cosmética; nunca vale derrubar a passada


def tentativas_de_hoje(path: Path | None = None) -> set[str]:
    """Números que já tiveram a tentativa de hoje.

    Poda de passagem as linhas de dias anteriores — o arquivo só precisa conter
    o dia corrente, então ele não cresce com o tempo."""
    path = path or DIARIO_TENTATIVAS
    try:
        linhas = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except (OSError, UnicodeDecodeError):
        return set()
    hoje = _hoje()
    de_hoje = {
        numero
        for dia, _, numero in (l.partition("\t") for l in linhas)
        if dia == hoje and numero
    }
    if len(de_hoje) != len(linhas):  # sobrou linha de outro dia (ou repetida)
        _regravar_diario(de_hoje, path)
    return de_hoje


def marcar_tentativa(numero: str, path: Path | None = None) -> None:
    """Registra a tentativa do dia. Chamar ANTES da consulta, não depois: é o que
    faz o cooldown valer mesmo se a passada morrer no meio."""
    path = path or DIARIO_TENTATIVAS
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{_hoje()}\t{numero}\n")
            f.flush()
            os.fsync(f.fileno())  # sobrevive a queda de energia/kill do processo
    except OSError:
        pass  # sem registro seguimos; o gate por `quando` no estado ainda pega


def _tentado_hoje(numero: str, registro: dict, tentadas_hoje: set[str]) -> bool:
    """Dois sinais independentes, para o gate não depender de um arquivo só:
    o log de tentativas (gravado antes da consulta) e o `quando` do estado
    (gravado no resultado, e que sobrevive a apagar o log)."""
    if numero in tentadas_hoje:
        return True
    return str((registro or {}).get("quando") or "")[:10] == _hoje()


def filtrar_tentados_hoje(
    pendentes: dict[str, dict], estado: dict, tentadas_hoje: set[str]
) -> tuple[dict[str, dict], list[str]]:
    """Tira da fila quem já gastou a tentativa do dia.

    Devolve ``(fila, adiados)``. Os adiados não são erro nem pendência nova:
    voltam sozinhos na próxima passada, amanhã."""
    fila: dict[str, dict] = {}
    adiados: list[str] = []
    for numero, info in pendentes.items():
        if _tentado_hoje(numero, estado.get(numero, {}), tentadas_hoje):
            adiados.append(numero)
        else:
            fila[numero] = info
    return fila, adiados


# --- leitura do cache -----------------------------------------------------
def _numeros_no_cache() -> set[str]:
    """CNJs já presentes no cache de buscas online (um <numero>/metadados.json)."""
    return {
        _cnj_normalizado(meta.parent.name)
        for meta in PROCESSOS_ONLINE_DIR.glob("*/metadados.json")
    }


# --- pool de scrapers (1 login por sistema) -------------------------------
# Teto de navegadores vivos ao mesmo tempo. Cada scraper é uma árvore de
# processos do Chrome (browser + renderer + rede + GPU): 300-500 MB cada.
# `TJ_SISTEMAS` tem 18 rótulos e, sem teto, uma passada larga chegava a manter
# ~15 abertos simultaneamente — em 4 GB isso é swap garantido, e a passada
# atolava. Reusar sessão continua sendo o objetivo (evita relogar e ser
# bloqueado); o teto só descarta a MENOS usada recentemente quando estoura.
MAX_NAVEGADORES = 4
MAX_NAVEGADORES_MODO_LEVE = 2


class PoolScrapers:
    """Mantém scrapers logados por sistema (reuso de sessão → evita bloqueio),
    com teto LRU de navegadores simultâneos.

    ``obter`` cria+loga na 1ª vez e reusa depois; se o login de um sistema já
    falhou nesta corrida, levanta RuntimeError sem tentar de novo."""

    def __init__(self, headless: bool, maximo: int = MAX_NAVEGADORES):
        self.headless = headless
        self.maximo = max(1, int(maximo))
        # OrderedDict como LRU: a ordem de inserção/uso define quem é descartado.
        self._scrapers: "OrderedDict[str, object]" = OrderedDict()
        self._login_falhou: set[str] = set()

    def _fechar(self, tj_label: str) -> None:
        scraper = self._scrapers.pop(tj_label, None)
        if scraper is None:
            return
        try:
            scraper.fechar()
        except Exception:
            pass

    def _respeitar_teto(self) -> None:
        while len(self._scrapers) >= self.maximo:
            antigo, _ = next(iter(self._scrapers.items()))
            print(
                f"   ♻️ fechando {antigo} para respeitar o teto de "
                f"{self.maximo} navegador(es) simultâneo(s)"
            )
            self._fechar(antigo)

    def obter(self, tj_label: str):
        if tj_label in self._login_falhou:
            raise RuntimeError(f"login de {tj_label} já falhou nesta corrida")
        if tj_label in self._scrapers:
            self._scrapers.move_to_end(tj_label)  # usado agora = mais recente
            return self._scrapers[tj_label]
        self._respeitar_teto()
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
        for tj_label in list(self._scrapers):
            self._fechar(tj_label)
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
    # `_merge_processos` faz UNIÃO de tags. Sem esta linha, um processo que mudou
    # de sistema ficaria com duas `Sistema:` — e a do cache, mais antiga, é
    # justamente a errada.
    sistema = next((t for t in online_proc.tags if t.startswith(_TAG_SISTEMA)), "")
    if sistema:
        marcar_sistema(merged, sistema[len(_TAG_SISTEMA):])
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
    ("reu_faltando", "❌ Réu PERDIDO na raspagem (classe exige polo passivo) — refazer"),
    ("credor_faltando", "❌ Credor PERDIDO na raspagem (sem polo ativo) — refazer"),
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
        f"- ❌ Réu perdido na raspagem (refazer): **{len(grupos.get('reu_faltando', []))}**",
        f"- ❌ Credor perdido na raspagem (refazer): **{len(grupos.get('credor_faltando', []))}**",
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
    terminado (``STATUS_DONE``) no estado do monitor.

    Exceto os marcados ``reu_faltando``/``credor_faltando``: esses ESTÃO no cache
    (foram salvos com um dos polos) e por isso a checagem de cache os declarava
    mapeados antes mesmo do gate de retry. Como o que temos deles é dado
    incompleto, voltam a ser elegíveis — ainda atrás do ``retry_falhas`` em
    `selecionar_pendentes`.
    """
    mapeados = set(_numeros_no_cache())
    incompletos = {
        _cnj_normalizado(numero)
        for numero, r in estado.items()
        if r.get("status") in ("reu_faltando", "credor_faltando")
    }
    for numero, r in estado.items():
        if r.get("status") in STATUS_DONE:
            mapeados.add(_cnj_normalizado(numero))
    return mapeados - incompletos


def selecionar_pendentes(alvos_planilhas: dict[str, str], estado: dict,
                         retry_falhas: bool = False) -> dict[str, dict]:
    """De ``{numero: campanha}`` das planilhas, devolve só os NÃO mapeados como
    ``{numero: {uf, candidatos, existing=None, campanha}}`` (pronto p/ a passada).

    Seleção por STATUS, sem noção de tempo: quem chamar precisa passar o
    resultado por ``filtrar_tentados_hoje`` para respeitar o cooldown de uma
    tentativa por processo por dia (``rodar_passada`` faz isso)."""
    mapeados = processos_ja_mapeados(estado)
    pendentes: dict[str, dict] = {}
    for numero, campanha in alvos_planilhas.items():
        if _cnj_normalizado(numero) in mapeados:
            continue
        st = estado.get(numero, {}).get("status")
        if st in STATUS_RETRY and not retry_falhas:
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

    # Garante que os segredos do .env (TOTP_* dos tribunais, IMAP_* do 2FA do
    # e-SAJ) estejam em os.environ ANTES de qualquer login — a busca online faz o
    # mesmo. Sem isto, numa máquina cujo processo ainda não carregou o .env, o
    # TOTP/e-SAJ falharia mesmo com o config/.env embutido no bundle.
    _preparar_env_local()
    try:
        from dotenv import load_dotenv
        load_dotenv(CONFIG_ENV_PATH)
    except Exception:
        pass

    log("🔑 coletando processos das planilhas Google...")
    registros = sheets.coletar_processos_das_planilhas(log=log)
    # {numero: campanha} — 1ª campanha vence (dedup já feito em sheets)
    alvos_planilhas: dict[str, str] = {}
    for r in registros:
        alvos_planilhas.setdefault(r["numero"], r.get("campanha", ""))

    estado = _carregar_estado()
    pendentes = selecionar_pendentes(alvos_planilhas, estado, retry_falhas=retry_falhas)
    # Cooldown: quem já foi tentado hoje sai da fila (ver DIARIO_TENTATIVAS).
    pendentes, adiados = filtrar_tentados_hoje(
        pendentes, estado, tentativas_de_hoje()
    )
    ja = len(alvos_planilhas) - len(pendentes) - len(adiados)
    log(f"📊 {len(alvos_planilhas)} nas planilhas | {ja} já mapeados | "
        f"{len(pendentes)} pendentes")
    if adiados:
        log(f"⏭️  {len(adiados)} já tiveram a tentativa de hoje — voltam amanhã "
            f"(1 tentativa por processo por dia, anti-bloqueio)")

    resumo = {
        "total": len(alvos_planilhas), "ja_mapeados": ja,
        "mapeados_agora": 0, "nao_encontrados": 0, "pendentes": len(pendentes),
        "adiados": len(adiados),
    }
    if dry_run:
        log("🔎 DRY-RUN: nada raspado.")
        return resumo
    if not pendentes:
        if adiados:
            log(f"⏸️  nada a fazer hoje: os {len(adiados)} que faltam já foram "
                f"tentados hoje. Rode de novo amanhã.")
        else:
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

    # Teto de navegadores: em modo leve fica em 2 (ver MAX_NAVEGADORES).
    pool = PoolScrapers(headless=headless, maximo=_teto_navegadores())
    gravador = _GravadorThrottled()
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
                gravador.talvez(estado)
                log("   🗺️ sem rota — pulando.")
                continue

            # A tentativa do dia é registrada ANTES de abrir/consultar qualquer
            # tribunal: se o app cair ou for fechado no meio deste processo, ele
            # não volta à fila hoje. É o que impede o laço cai-reabre-repete de
            # virar flood na conta compartilhada.
            marcar_tentativa(numero)

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
                        tj_label=tj_label,
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
                status = _status_do_resultado(merged, tem_reu, bool(autor.strip()))
                estado[numero] = {
                    "status": status,
                    "uf": uf, "sistema": tj_label, "campanha": campanha,
                    "tem_reu": tem_reu, "autor": autor, "reu": reu, "quando": _quando(),
                }
                if status in ("reu_faltando", "credor_faltando"):
                    estado[numero]["classe"] = getattr(merged, "classe", "")
                log(f"   {({'enriquecido': '✅', 'reu_faltando': '❌', 'credor_faltando': '❌'}).get(status, '⚠️')} "
                    f"{tj_label}: autor='{autor or '—'}' réu='{reu or '—'}'")
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

            gravador.talvez(estado)
    finally:
        pool.fechar_todos()
        gravador.finalizar(estado)
        try:
            _gerar_relatorios(estado)
        except Exception:
            pass

    log(f"✅ passada concluída: {resumo['mapeados_agora']} mapeados agora, "
        f"{resumo['nao_encontrados']} não encontrados, {resumo['ja_mapeados']} já mapeados.")
    return resumo
