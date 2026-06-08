# -*- coding: utf-8 -*-
"""Runner de busca em lote: roteia cada processo p/ o sistema suportado,
checa se já está no app (Desktop/Processos), busca online, salva e acumula
resultados num JSON. Gera relatório de sucessos/falhas ao final.

Uso:
    python scripts/busca_lote.py seed              # registra suportados/não-sup.
    python scripts/busca_lote.py run <numero>      # busca+salva um processo
    python scripts/busca_lote.py relatorio         # imprime markdown
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))
load_dotenv(_RAIZ / "config" / ".env")

from organizador.config import PASTA_PADRAO
from organizador.organizacao import _nome_pasta, _resumo_txt
from organizador.parsing import _derivar_agregados
from organizador.scraper import buscar_processo_online

# numero -> (sistema_declarado, tj_label | None, motivo_se_nao_suportado)
PROCESSOS: list[tuple[str, str, str | None, str]] = [
    ("4018574-23.2025.8.26.0100", "eproc SP",     "TJSP — eproc (SP)", ""),
    ("1102493-63.2022.8.26.0100", "e-SAJ SP",     "TJSP — e-SAJ",      ""),
    ("5000432-19.2021.8.13.0079", "PJe MG",       "TJMG — PJe",        ""),
    ("5014752-74.2021.8.13.0079", "PJe MG",       "TJMG — PJe",        ""),
    ("122044-93.2017.8.09.0100",  "TJGO Projudi", None, "Projudi não implementado no scraper"),
    ("0000262-41.2021.8.19.0023", "TJRJ",         None, "TJRJ não está em TJ_SISTEMAS (sem rota/URL)"),
    ("0668503-38.2020.8.04.0001", "TJAM e-SAJ",   "TJAM — e-SAJ",      ""),
    ("0575508-64.2024.8.04.0001", "TJAM Projudi", None, "Projudi não implementado no scraper"),
    ("0008537-81.2019.8.06.0064", "TJCE PJe",     "TJCE — PJe", ""),
    ("5036717-94.2022.8.24.0038", "TJSC eproc",   "TJSC — eproc", ""),
    ("0821821-97.2025.8.10.0001", "TJMA PJe",     "TJMA — PJe", ""),
    ("0001461-25.2018.8.14.0094", "TJPA PJe",     "TJPA — PJe", ""),
    ("0003730-88.2020.8.17.2420", "TJPE PJe",     "TJPE — PJe", ""),
]

RESULT_FILE = _RAIZ / "data" / "_batch_resultado.json"


def _carregar() -> dict:
    if RESULT_FILE.exists():
        return json.loads(RESULT_FILE.read_text(encoding="utf-8"))
    return {}


def _gravar(d: dict) -> None:
    RESULT_FILE.parent.mkdir(parents=True, exist_ok=True)
    RESULT_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _registrar(numero: str, **kw) -> None:
    d = _carregar()
    d[numero] = {**d.get(numero, {}), **kw}
    _gravar(d)


def _ja_no_app(numero: str) -> bool:
    return (Path(PASTA_PADRAO) / _nome_pasta(numero)).exists()


def _salvar_no_app(proc) -> Path:
    pasta = Path(PASTA_PADRAO) / _nome_pasta(proc.numero)
    pasta.mkdir(parents=True, exist_ok=True)
    _derivar_agregados(proc)
    (pasta / "metadados.json").write_text(
        json.dumps(asdict(proc), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (pasta / "resumo.txt").write_text(_resumo_txt(proc), encoding="utf-8")
    return pasta


def _partes_resumo(proc) -> list[str]:
    return [
        f"{p.papel}: {p.nome}" + (f" ({p.documento})" if p.documento else "")
        for p in proc.partes
    ]


def cmd_seed() -> None:
    for numero, sistema, tj_label, motivo in PROCESSOS:
        if tj_label is None:
            _registrar(numero, sistema=sistema, status="nao_suportado", motivo=motivo)
        elif _ja_no_app(numero):
            _registrar(numero, sistema=sistema, status="ja_no_app", tj_label=tj_label)
        else:
            _registrar(numero, sistema=sistema, status="pendente", tj_label=tj_label)
    print("seed concluído.")


def cmd_run(numero: str) -> None:
    info = next((p for p in PROCESSOS if p[0] == numero), None)
    if not info:
        print(f"número desconhecido: {numero}")
        return
    _sistema, sistema, tj_label, _motivo = info
    if tj_label is None:
        print(f"{numero}: não suportado, pulando.")
        return
    if _ja_no_app(numero):
        _registrar(numero, status="ja_no_app")
        print(f"{numero}: já no app, pulando.")
        return

    logs: list[str] = []
    try:
        proc = buscar_processo_online(
            numero, tj_label, headless=False, log=lambda s: logs.append(s),
        )
        partes = _partes_resumo(proc)
        # Login pode dar certo mas a extração vir vazia (ex.: versão diferente
        # do PJe, cujo DOM da timeline não bate com os seletores do TJMG).
        # 0 partes = não trouxe as pessoas → falha honesta, não salva no app.
        if not proc.partes:
            _registrar(
                numero, status="falha", tj_label=tj_label,
                erro="login OK, mas extração vazia (0 partes) — provável versão "
                     "diferente do PJe (DOM da timeline incompatível com o TJMG)",
            )
            print(f"❌ {numero}: login OK, mas 0 partes extraídas (versão diferente do PJe?)")
            return
        pasta = _salvar_no_app(proc)
        _registrar(
            numero, status="sucesso", tj_label=tj_label,
            classe=proc.classe, partes=partes, n_partes=len(proc.partes),
            n_eventos=proc.total_eventos, pasta=str(pasta),
        )
        print(f"✅ {numero}: {len(partes)} parte(s) salvas em {pasta}")
        for p in partes:
            print("   -", p)
    except Exception as exc:
        _registrar(numero, status="falha", tj_label=tj_label, erro=str(exc))
        print(f"❌ {numero}: FALHA — {exc}")


def cmd_relatorio() -> None:
    d = _carregar()
    sucesso, ja, falha, nsup = [], [], [], []
    for numero, sistema, tj_label, motivo in PROCESSOS:
        r = d.get(numero, {})
        st = r.get("status", "pendente")
        if st == "sucesso":
            sucesso.append((numero, r))
        elif st == "ja_no_app":
            ja.append((numero, r))
        elif st == "nao_suportado":
            nsup.append((numero, r))
        else:
            falha.append((numero, r))

    print("\n# Relatório de busca em lote\n")
    print(f"- ✅ Sucessos (adicionados ao app): **{len(sucesso)}**")
    print(f"- ⏭️  Já estavam no app: **{len(ja)}**")
    print(f"- ❌ Falhas: **{len(falha)}**")
    print(f"- 🚫 Não suportados: **{len(nsup)}**\n")

    if sucesso:
        print("## ✅ Sucessos\n")
        for numero, r in sucesso:
            print(f"### {numero} — {r.get('sistema','')}")
            print(f"- Classe: {r.get('classe') or '—'}")
            print(f"- Partes ({r.get('n_partes',0)}):")
            for p in r.get("partes", []):
                print(f"  - {p}")
            print(f"- Eventos: {r.get('n_eventos','—')} | Pasta: `{r.get('pasta','')}`\n")

    if falha:
        print("## ❌ Falhas\n")
        for numero, r in falha:
            print(f"- **{numero}** ({r.get('sistema','')}): {r.get('erro') or 'pendente/não executado'}")
        print()

    if nsup:
        print("## 🚫 Não suportados pelo sistema\n")
        for numero, r in nsup:
            print(f"- **{numero}** ({r.get('sistema','')}): {r.get('motivo','')}")
        print()

    if ja:
        print("## ⏭️ Já estavam no app\n")
        for numero, r in ja:
            print(f"- {numero} ({r.get('sistema','')})")
        print()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "relatorio"
    if cmd == "seed":
        cmd_seed()
    elif cmd == "run":
        cmd_run(sys.argv[2])
    elif cmd == "relatorio":
        cmd_relatorio()
    else:
        print(f"comando desconhecido: {cmd}")
