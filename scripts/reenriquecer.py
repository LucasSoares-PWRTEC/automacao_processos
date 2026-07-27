# -*- coding: utf-8 -*-
"""Re-enriquecimento dos processos do cache que estão SEM autor ou SEM réu.

O enriquecimento anterior (carteiras de Downloads/Mapeamento Processos) deixou
~150 processos sem o réu — em geral processos do PJe/TJMG em que o réu (pessoa
física) vem sem CPF e era descartado pelo parser. Com o parser corrigido, este
runner re-raspa esses processos e mescla o resultado de volta.

Princípios (pedidos do usuário):
  • Reuso de sessão: 1 login por sistema (pool de scrapers), dezenas de consultas
    na mesma sessão → evita bloqueio. Pausa entre consultas.
  • Fallback entre sistemas: se "não encontrado" num sistema, tenta os outros da
    mesma UF (eproc × e-SAJ × PJe), do mais rico (com documento) ao mais pobre.
  • Pop-up "atribuir/vincular ao advogado": o scraper detecta, fecha SEM confirmar
    e aborta o processo; aqui registramos a observação e seguimos.
  • Grava no cache (PROCESSOS_ONLINE_DIR) E em Desktop/Processos; preserva a tag
    "Mapeamento:" e o valor da causa; adiciona "Reenriquecido v2".
  • Relatório incremental e RETOMÁVEL: a cada processo regrava o estado (JSON) e
    regenera relatório .md + .csv. Reabrir o script pula o que já terminou.

Uso:
    python scripts/reenriquecer.py                 # roda os pendentes (visível)
    python scripts/reenriquecer.py --headless      # navegador invisível
    python scripts/reenriquecer.py --limit 3       # só os 3 primeiros (dry-run)
    python scripts/reenriquecer.py --numeros N1 N2  # só estes números
    python scripts/reenriquecer.py --retry-falhas  # reprocessa falha/nao_encontrado
    python scripts/reenriquecer.py --so-relatorio  # só regenera .md/.csv do estado
    python scripts/reenriquecer.py --excel         # (re)gera carteiras enriquecidas
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))
load_dotenv(_RAIZ / "config" / ".env")

# Console do Windows costuma ser cp1252; os logs usam emojis/acentos. Sem isto,
# um print poderia derrubar a corrida inteira com UnicodeEncodeError.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from organizador.config import (  # noqa: E402
    PASTA_PADRAO,
    PROCESSOS_ONLINE_DIR,
    TJ_SISTEMAS,
    _ESTADO_TJ_UF,
    candidatos_tj_para_uf,
)
from organizador.integracoes.tjsp_scraper.scrapers import criar_scraper  # noqa: E402
from organizador.integracoes.tjsp_scraper.scrapers.popup import (  # noqa: E402
    AtribuicaoDetectadaError,
)
from organizador.organizacao import (  # noqa: E402
    _autor,
    _dict_para_processo,
    _merge_processos,
    _nome_pasta,
    _resumo_txt,
    _reus_joined,
    salvar_processo_metadados,
)
from organizador.parsing import _derivar_agregados, _uf_de_numero_cnj  # noqa: E402
from organizador.padroes import (  # noqa: E402
    PAPEIS_ATIVO,
    PAPEIS_PASSIVO,
    RE_NUMERO_CNJ,
)
from organizador.scraper import _consultar_e_extrair  # noqa: E402
# Núcleo reaproveitável (pool de sessão + merge/persistência) mora agora no
# pacote, em organizador.monitoramento — o monitoramento periódico usa o mesmo.
from organizador.monitoramento import PoolScrapers, _merge_e_salvar  # noqa: E402

# --- caminhos -------------------------------------------------------------
ESTADO_JSON = _RAIZ / "data" / "_reenriquecimento_estado.json"
SAIDA_DIR = Path.home() / "Downloads" / "Mapeamento Processos - enriquecido"
RELATORIO_MD = SAIDA_DIR / "_relatorio.md"
RELATORIO_CSV = SAIDA_DIR / "_relatorio.csv"
CARTEIRAS_DIR = Path.home() / "Downloads" / "Mapeamento Processos"
# Modo "não mapeados": processos das carteiras que NUNCA entraram no app.
ESTADO_NM = _RAIZ / "data" / "_naomapeados_estado.json"
RELATORIO_NM_MD = SAIDA_DIR / "_relatorio_nao_mapeados.md"
RELATORIO_NM_CSV = SAIDA_DIR / "_relatorio_nao_mapeados.csv"

PAUSA_ENTRE_CONSULTAS = 2.5  # s — folga anti-bloqueio (comprovado no lote anterior)

# Status "terminados" (pulados numa nova execução). nao_encontrado/falha são
# reprocessados com --retry-falhas.
STATUS_DONE = {"enriquecido", "ainda_sem_reu", "popup_atribuicao", "sem_rota"}

# Rotas configuradas que NÃO atendem os processos da UF: a plataforma eproc do
# TJSP, acessada por perfil OAB de GO/PR/RN, loga mas NÃO hospeda processos
# desses tribunais (confirmado ao vivo: sempre "não encontrado"). GO e PR usam
# Projudi (sem scraper); RN usa PJe (volume ínfimo). O RJ JÁ foi migrado para a
# rota real "TJRJ — PJe" em config.py. Pular as inoperantes evita consultas vãs.
_ROTAS_INOPERANTES = {"TJGO — eproc", "TJPR — eproc", "TJRN — eproc"}


def _candidatos(uf: str) -> list[str]:
    """Sistemas que atendem a UF, sem as rotas comprovadamente inoperantes."""
    return [c for c in candidatos_tj_para_uf(uf) if c not in _ROTAS_INOPERANTES]


# --- estado (JSON incremental) -------------------------------------------
def _carregar_estado(path: Path = ESTADO_JSON) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _gravar_estado(estado: dict, path: Path = ESTADO_JSON) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _cnj_normalizado(texto: str) -> str:
    m = RE_NUMERO_CNJ.search(str(texto or ""))
    return m.group(0) if m else str(texto or "")


# --- leitura do cache -----------------------------------------------------
def _iter_cache():
    """Itera (numero, dict, caminho) de cada metadados.json do cache online."""
    for meta in sorted(PROCESSOS_ONLINE_DIR.glob("*/metadados.json")):
        try:
            d = json.loads(meta.read_text(encoding="utf-8"))
        except Exception:
            continue
        yield (d.get("numero") or meta.parent.name), d, meta


def _falta_autor_ou_reu(dados: dict) -> bool:
    proc = _dict_para_processo(dados)
    return not _autor(proc).strip() or not _reus_joined(proc).strip()


def _selecionar_alvos(numeros_filtro: set[str] | None) -> dict[str, dict]:
    """Mapa numero -> {uf, candidatos, existing} dos processos a re-enriquecer."""
    alvos: dict[str, dict] = {}
    for numero, dados, _meta in _iter_cache():
        if numeros_filtro is not None:
            if numero not in numeros_filtro:
                continue
        elif not _falta_autor_ou_reu(dados):
            continue
        uf = (dados.get("uf") or _uf_de_numero_cnj(numero) or "").upper()
        alvos[numero] = {
            "uf": uf,
            "candidatos": _candidatos(uf),
            "existing": dados,
        }
    return alvos


# --- alvos NÃO mapeados (das carteiras, ainda fora do app) ----------------
def _cliente_de_arquivo(nome: str) -> str:
    """Nome do cliente a partir do arquivo da carteira (vira a tag Mapeamento)."""
    s = Path(nome).stem
    for pref in ("PLANILHA - ", "JUDICIAL - ", "Base - "):
        if s.startswith(pref):
            s = s[len(pref):]
    s = re.sub(r"\s*\(.*?\)\s*$", "", s)  # tira "(Conferido)", "- auditoria" etc.
    return s.strip(" -") or Path(nome).stem


def _carteira_lookup() -> dict[str, str]:
    """{cnj: cliente} de todos os números nas carteiras .xlsx (1ª ocorrência vence)."""
    import pandas as pd
    lookup: dict[str, str] = {}
    for xlsx in sorted(CARTEIRAS_DIR.glob("*.xlsx")):
        if xlsx.name.startswith("~$"):
            continue
        try:
            sheets = pd.read_excel(xlsx, sheet_name=None, dtype=str, keep_default_na=False)
        except Exception:
            continue
        cliente = _cliente_de_arquivo(xlsx.name)
        for _, df in sheets.items():
            col = _detectar_coluna_processo_df(df)
            if col is None:
                continue
            for v in df[col].tolist():
                cnj = _cnj_normalizado(v)
                if RE_NUMERO_CNJ.fullmatch(cnj) and cnj not in lookup:
                    lookup[cnj] = cliente
    return lookup


def _numeros_no_cache() -> set[str]:
    return {_cnj_normalizado(meta.parent.name)
            for meta in PROCESSOS_ONLINE_DIR.glob("*/metadados.json")}


def _selecionar_nao_mapeados(carteira_filtro: str | None = None,
                             so_rapidos: bool = False) -> dict[str, dict]:
    """Números das carteiras que ainda NÃO estão no app (cache). existing=None
    (raspagem nova); a tag Mapeamento vem do nome da carteira."""
    cache_nums = _numeros_no_cache()
    alvos: dict[str, dict] = {}
    for cnj, cliente in _carteira_lookup().items():
        if cnj in cache_nums:
            continue
        if carteira_filtro and carteira_filtro.lower() not in cliente.lower():
            continue
        uf = (_uf_de_numero_cnj(cnj) or "").upper()
        cand = _candidatos(uf)
        if so_rapidos:  # só TOTP (PJe/eproc); deixa e-SAJ p/ uma 2ª rodada
            cand = [c for c in cand if TJ_SISTEMAS[c][0] in ("pje", "eproc")]
            if not cand:
                continue
        alvos[cnj] = {"uf": uf, "candidatos": cand, "existing": None, "cliente": cliente}
    return alvos


# PoolScrapers e _merge_e_salvar foram movidos para organizador.monitoramento
# (importados acima) — o monitoramento periódico reusa o mesmo pool de sessão e a
# mesma persistência no cache. Aqui ficam só o modo carteira/Excel e o CLI.


# --- relatórios .md / .csv ------------------------------------------------
_ORDEM_STATUS = [
    ("enriquecido", "✅ Enriquecidos (réu obtido)"),
    ("ainda_sem_reu", "⚠️ Achados mas AINDA sem réu (revisar manualmente)"),
    ("popup_atribuicao", "🚫 Parados no pop-up de atribuição ao advogado"),
    ("nao_encontrado", "🔎 Não encontrados em nenhum sistema"),
    ("sem_rota", "🗺️ Sem rota (UF sem sistema suportado)"),
    ("falha", "❌ Falhas (erro técnico)"),
    ("pendente", "⏳ Pendentes (ainda não processados)"),
]


def _gerar_relatorios(estado: dict, md_path: Path = RELATORIO_MD,
                      csv_path: Path = RELATORIO_CSV,
                      titulo: str = "Relatório de re-enriquecimento (autor/réu faltando)") -> None:
    SAIDA_DIR.mkdir(parents=True, exist_ok=True)
    grupos: dict[str, list[tuple[str, dict]]] = {st: [] for st, _ in _ORDEM_STATUS}
    for numero, r in estado.items():
        grupos.setdefault(r.get("status", "pendente"), []).append((numero, r))

    total = len(estado)
    linhas = [
        f"# {titulo}",
        "",
        f"_Atualizado: {datetime.now().isoformat(timespec='seconds')}_",
        "",
        f"- **Total de alvos:** {total}",
        f"- ✅ Enriquecidos (réu obtido): **{len(grupos.get('enriquecido', []))}**",
        f"- ⚠️ Achados sem réu: **{len(grupos.get('ainda_sem_reu', []))}**",
        f"- 🚫 Pop-up de atribuição: **{len(grupos.get('popup_atribuicao', []))}**",
        f"- 🔎 Não encontrados: **{len(grupos.get('nao_encontrado', []))}**",
        f"- 🗺️ Sem rota: **{len(grupos.get('sem_rota', []))}**",
        f"- ❌ Falhas: **{len(grupos.get('falha', []))}**",
        f"- ⏳ Pendentes (faltam processar): **{len(grupos.get('pendente', []))}**",
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
            elif st == "popup_atribuicao":
                extra = f" — obs: {r.get('obs') or ''}"
            elif st in ("nao_encontrado", "falha"):
                extra = f" — {r.get('erro') or ''}"
            linhas.append(f"- `{numero}` ({uf} / {sistema}){extra}")
        linhas.append("")
    # O relatório é conveniência; o estado.json é a fonte da verdade. Se o
    # arquivo estiver ABERTO (Excel trava o .csv no Windows), só avisa e segue —
    # NUNCA derruba o enriquecimento por causa do relatório.
    try:
        md_path.write_text("\n".join(linhas), encoding="utf-8")
        with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["numero", "uf", "sistema", "status", "tem_reu",
                        "autor", "reu", "obs", "erro", "quando"])
            for numero, r in sorted(estado.items()):
                w.writerow([
                    numero, r.get("uf", ""), r.get("sistema", ""), r.get("status", ""),
                    "sim" if r.get("tem_reu") else "nao",
                    r.get("autor", ""), r.get("reu", ""),
                    r.get("obs", ""), r.get("erro", ""), r.get("quando", ""),
                ])
    except (PermissionError, OSError) as exc:
        print(f"   ⚠️ relatório não regravado (arquivo aberto/bloqueado?): {exc}. "
              "Os dados foram salvos; regenere depois com --so-relatorio.")


# --- loop principal -------------------------------------------------------
def _quando() -> str:
    return datetime.now().isoformat(timespec="seconds")


def executar(args) -> None:
    nm = getattr(args, "nao_mapeados", False)
    numeros_filtro = set(args.numeros) if getattr(args, "numeros", None) else None
    if nm:
        alvos = _selecionar_nao_mapeados(getattr(args, "carteira", None),
                                         getattr(args, "so_rapidos", False))
        estado_path, md_path, csv_path = ESTADO_NM, RELATORIO_NM_MD, RELATORIO_NM_CSV
        titulo = "Relatório de enriquecimento — processos NÃO mapeados (novos no app)"
        print(f"Modo NÃO MAPEADOS. Alvos das carteiras fora do app: {len(alvos)}")
    else:
        alvos = _selecionar_alvos(numeros_filtro)
        estado_path, md_path, csv_path = ESTADO_JSON, RELATORIO_MD, RELATORIO_CSV
        titulo = "Relatório de re-enriquecimento (autor/réu faltando)"
        print(f"Alvos selecionados: {len(alvos)}")

    estado = _carregar_estado(estado_path)
    # Semeia os alvos no estado (preserva os já processados).
    for numero, info in alvos.items():
        if numero not in estado:
            estado[numero] = {"status": "pendente"}
        estado[numero]["uf"] = info["uf"]
        if info.get("cliente"):
            estado[numero]["cliente"] = info["cliente"]
    _gravar_estado(estado, estado_path)

    # Fila a processar. Com --numeros (lista explícita), FORÇA o reprocessamento.
    def _precisa(numero: str) -> bool:
        if numeros_filtro is not None:
            return True
        st = estado.get(numero, {}).get("status", "pendente")
        if st in STATUS_DONE:
            return False
        if st in ("nao_encontrado", "falha"):
            return args.retry_falhas
        return True  # pendente

    fila = [n for n in alvos if _precisa(n)]
    # Ordena por TIPO de sistema (eproc/PJe = TOTP, rápidos; e-SAJ = 2FA por
    # e-mail, lento → por último) e depois pelo rótulo, p/ agrupar o mesmo login.
    _rank = {"eproc": 0, "pje": 1, "esaj": 2}

    def _ordem(n: str):
        cand = alvos[n]["candidatos"] or []
        if not cand:
            return (9, "~")
        return (_rank.get(TJ_SISTEMAS[cand[0]][0], 8), cand[0])

    fila.sort(key=_ordem)
    if args.limit:
        fila = fila[: args.limit]
    print(f"A processar agora: {len(fila)} (restantes ficam pendentes p/ retomar)\n")

    def _persistir():
        _gravar_estado(estado, estado_path)
        _gerar_relatorios(estado, md_path, csv_path, titulo)

    pool = PoolScrapers(headless=args.headless)
    try:
        for i, numero in enumerate(fila, 1):
            info = alvos[numero]
            uf, candidatos = info["uf"], info["candidatos"]
            cliente = info.get("cliente")
            tags_extra = ([f"Mapeamento: {cliente}", "Enriquecido online"]
                          if cliente else ["Reenriquecido v2"])
            print(f"[{i}/{len(fila)}] {numero} (UF {uf or '—'}"
                  + (f", {cliente})" if cliente else ")"))

            if not candidatos:
                estado[numero] = {"status": "sem_rota", "uf": uf,
                                  "obs": "UF sem sistema suportado", "quando": _quando()}
                if cliente:
                    estado[numero]["cliente"] = cliente
                _persistir()
                print("   🗺️ sem rota — pulando.\n")
                continue

            ultimo_erro = ""
            resolvido = False
            for tj_label in candidatos:
                site, estado_tj, _url, _perfil = TJ_SISTEMAS[tj_label]
                tribunal = _ESTADO_TJ_UF.get(estado_tj, (tj_label, uf))[0]
                try:
                    scraper = pool.obter(tj_label)
                except RuntimeError as exc:
                    ultimo_erro = str(exc)
                    print(f"   ⚠️ {exc}")
                    continue
                try:
                    proc = _consultar_e_extrair(
                        scraper, site, numero, tribunal, uf,
                        modo_docs="nenhum",
                        log=lambda s: print("   " + s.rstrip()),
                    )
                except AtribuicaoDetectadaError as exc:
                    estado[numero] = {
                        "status": "popup_atribuicao", "uf": uf, "sistema": tj_label,
                        "obs": exc.observacao, "quando": _quando(),
                    }
                    if cliente:
                        estado[numero]["cliente"] = cliente
                    print(f"   🚫 pop-up de atribuição em {tj_label} — abortado e registrado.\n")
                    resolvido = True
                    break  # NÃO tenta outros sistemas (pedido do usuário)
                except RuntimeError as exc:
                    ultimo_erro = str(exc)
                    print(f"   🔎 {tj_label}: {exc}")
                    time.sleep(PAUSA_ENTRE_CONSULTAS)
                    continue
                except Exception as exc:  # erro técnico inesperado
                    ultimo_erro = f"{type(exc).__name__}: {exc}"
                    print(f"   ❌ {tj_label}: {ultimo_erro}")
                    time.sleep(PAUSA_ENTRE_CONSULTAS)
                    continue

                merged = _merge_e_salvar(numero, proc, info["existing"], tags_extra)
                autor, reu = _autor(merged), _reus_joined(merged)
                tem_reu = bool(reu.strip())
                estado[numero] = {
                    "status": "enriquecido" if tem_reu else "ainda_sem_reu",
                    "uf": uf, "sistema": tj_label, "tem_reu": tem_reu,
                    "autor": autor, "reu": reu, "quando": _quando(),
                }
                if cliente:
                    estado[numero]["cliente"] = cliente
                marca = "✅" if tem_reu else "⚠️"
                print(f"   {marca} {tj_label}: autor='{autor or '—'}' réu='{reu or '—'}'\n")
                resolvido = True
                time.sleep(PAUSA_ENTRE_CONSULTAS)
                break

            if not resolvido:
                estado[numero] = {"status": "nao_encontrado", "uf": uf,
                                  "sistema": (candidatos or ["—"])[-1],
                                  "erro": ultimo_erro or "não encontrado em nenhum sistema",
                                  "quando": _quando()}
                if cliente:
                    estado[numero]["cliente"] = cliente
                print("   🔎 não encontrado em nenhum sistema.\n")

            _persistir()
    finally:
        pool.fechar_todos()
        _persistir()

    print(f"\nConcluído. Relatório: {md_path}")


# --- write-back das carteiras (Excel) -------------------------------------
def _resumo_enriquecido(dados: dict) -> dict:
    proc = _dict_para_processo(dados)
    ativo_set = {x.upper() for x in PAPEIS_ATIVO}
    passivo_set = {x.upper() for x in PAPEIS_PASSIVO}

    def _docs(papel_set):
        return "; ".join(
            p.documento for p in proc.partes
            if p.papel.upper() in papel_set and (p.documento or "").strip()
        )

    adv_reu = "; ".join(
        a.nome for p in proc.partes if p.papel.upper() in passivo_set
        for a in p.advogados if a.nome
    )
    return {
        "AUTOR (online)": _autor(proc),
        "DOC AUTOR": _docs(ativo_set),
        "RÉU (online)": _reus_joined(proc),
        "DOC RÉU": _docs(passivo_set),
        "ADV RÉU": adv_reu,
        "ÓRGÃO JULGADOR (online)": proc.orgao_julgador,
        "SITUAÇÃO (online)": proc.situacao,
        "VALOR CAUSA (online)": proc.valor_causa,
    }


def _construir_lookup_cache() -> dict[str, dict]:
    lookup: dict[str, dict] = {}
    for numero, dados, _meta in _iter_cache():
        m = RE_NUMERO_CNJ.search(numero or "")
        if m:
            lookup[m.group(0)] = _resumo_enriquecido(dados)
    return lookup


def _resumo_enriquecido_keys():
    # ordem fixa das colunas novas
    return (
        "AUTOR (online)", "DOC AUTOR", "RÉU (online)", "DOC RÉU", "ADV RÉU",
        "ÓRGÃO JULGADOR (online)", "SITUAÇÃO (online)", "VALOR CAUSA (online)",
    )


def _detectar_coluna_processo_df(df) -> object | None:
    """Coluna de número de processo num DataFrame: por cabeçalho com 'PROCESSO'/
    'CNJ' ou, em fallback, a coluna com mais valores em formato CNJ."""
    from unidecode import unidecode
    for c in df.columns:
        cab = unidecode(str(c)).upper()
        if "PROCESSO" in cab or "CNJ" in cab:
            return c
    melhor, melhor_qtd = None, 0
    for c in df.columns:
        qtd = sum(1 for v in df[c].head(30) if RE_NUMERO_CNJ.search(str(v or "")))
        if qtd > melhor_qtd:
            melhor, melhor_qtd = c, qtd
    return melhor if melhor_qtd >= 2 else None


def gerar_carteiras_enriquecidas() -> None:
    """Para cada carteira .xlsx, escreve uma CÓPIA (em SAIDA_DIR) com colunas
    enriquecidas (AUTOR/DOC/RÉU/DOC/ADV/ÓRGÃO/SITUAÇÃO/VALOR) nas linhas cujo nº
    de processo está no cache. NÃO sobrescreve os originais.

    Usa pandas (escreve um arquivo NOVO): re-salvar os .xlsx originais via openpyxl
    falha por causa de pivot caches/partes embutidas. O preço é perder formatação
    e tabelas dinâmicas — aceitável para um entregável de dados."""
    try:
        import pandas as pd
    except Exception as exc:
        print(f"pandas indisponível: {exc}")
        return
    if not CARTEIRAS_DIR.is_dir():
        print(f"Pasta de carteiras não encontrada: {CARTEIRAS_DIR}")
        return

    lookup = _construir_lookup_cache()
    print(f"Cache com {len(lookup)} processos para casar nas carteiras.")
    SAIDA_DIR.mkdir(parents=True, exist_ok=True)
    cols_novas = list(_resumo_enriquecido_keys())

    for xlsx in sorted(CARTEIRAS_DIR.glob("*.xlsx")):
        if xlsx.name.startswith("~$"):
            continue
        try:
            sheets = pd.read_excel(xlsx, sheet_name=None, dtype=str, keep_default_na=False)
        except Exception as exc:
            print(f"  ! {xlsx.name}: erro ao ler ({exc})")
            continue
        total = 0
        for nome_aba, df in sheets.items():
            col = _detectar_coluna_processo_df(df)
            if col is None:
                continue
            novas = {c: [] for c in cols_novas}
            for val in df[col].tolist():
                m = RE_NUMERO_CNJ.search(str(val or ""))
                dados = lookup.get(m.group(0)) if m else None
                if dados:
                    total += 1
                for c in cols_novas:
                    novas[c].append((dados or {}).get(c, ""))
            for c in cols_novas:
                df[c] = novas[c]
        destino = SAIDA_DIR / xlsx.name
        try:
            with pd.ExcelWriter(destino, engine="openpyxl") as writer:
                for nome_aba, df in sheets.items():
                    df.to_excel(writer, sheet_name=str(nome_aba)[:31], index=False)
            print(f"  + {xlsx.name}: {total} linha(s) enriquecida(s) -> {destino.name}")
        except Exception as exc:
            print(f"  ! {xlsx.name}: erro ao salvar ({exc})")


def main():
    ap = argparse.ArgumentParser(description="(Re)enriquecimento de processos das carteiras")
    ap.add_argument("--headless", action="store_true", help="navegador invisível")
    ap.add_argument("--limit", type=int, default=0, help="processa só os N primeiros")
    ap.add_argument("--numeros", nargs="*", help="processa só estes números (ignora o filtro de faltantes)")
    ap.add_argument("--retry-falhas", action="store_true", help="reprocessa nao_encontrado/falha")
    ap.add_argument("--nao-mapeados", action="store_true",
                    help="enriquece processos das carteiras que ainda NÃO estão no app")
    ap.add_argument("--carteira", help="(com --nao-mapeados) filtra por nome de carteira (ex.: DIRECIONAL)")
    ap.add_argument("--so-rapidos", action="store_true",
                    help="(com --nao-mapeados) só sistemas TOTP (PJe/eproc); deixa o e-SAJ p/ depois")
    ap.add_argument("--so-relatorio", action="store_true", help="só regenera .md/.csv do estado atual")
    ap.add_argument("--excel", action="store_true", help="(re)gera as carteiras enriquecidas e sai")
    args = ap.parse_args()

    if args.so_relatorio:
        if args.nao_mapeados:
            _gerar_relatorios(_carregar_estado(ESTADO_NM), RELATORIO_NM_MD, RELATORIO_NM_CSV,
                              "Relatório de enriquecimento — processos NÃO mapeados (novos no app)")
            print(f"Relatório regenerado: {RELATORIO_NM_MD}")
        else:
            _gerar_relatorios(_carregar_estado())
            print(f"Relatório regenerado: {RELATORIO_MD}")
        return
    if args.excel:
        gerar_carteiras_enriquecidas()
        return

    executar(args)
    # Ao final de uma rodada completa (sem limite/filtro), atualiza as carteiras.
    if not args.limit and not args.numeros and not args.carteira:
        print("\nGerando carteiras enriquecidas (Excel)...")
        gerar_carteiras_enriquecidas()


if __name__ == "__main__":
    main()
