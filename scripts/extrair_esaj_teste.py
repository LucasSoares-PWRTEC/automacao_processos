# -*- coding: utf-8 -*-
"""Teste manual: extrai um processo do e-SAJ TJSP e parseia o HTML.

Uso: python scripts/extrair_esaj_teste.py 1102493-63.2022.8.26.0100
Salva o HTML cru em data/_debug/ e imprime os dados estruturados.
"""
import sys
import json
import re
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from organizador.config import CONFIG_ENV_PATH, TJ_SISTEMAS, _ESTADO_TJ_UF
from organizador.integracoes.tjsp_scraper.scrapers import criar_scraper
from organizador.integracoes.tjsp_scraper.parser import extrair_penhora_bloqueio_tribunais

load_dotenv(CONFIG_ENV_PATH)

NUMERO = sys.argv[1] if len(sys.argv) > 1 else "1102493-63.2022.8.26.0100"
LABEL = "TJSP — e-SAJ"


def _txt(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def parse_esaj(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    out = {}

    def by_id(i):
        return _txt(soup.find(id=i))

    out["numero"] = by_id("numeroProcesso")
    out["classe"] = by_id("classeProcesso")
    out["assunto"] = by_id("assuntoProcesso")
    out["foro"] = by_id("foroProcesso")
    out["vara"] = by_id("varaProcesso")
    out["juiz"] = by_id("juizProcesso")
    out["area"] = by_id("areaProcesso")
    out["valor_acao"] = by_id("valorAcaoProcesso")
    out["distribuicao"] = by_id("dataHoraDistribuicaoProcesso")
    out["situacao"] = by_id("situacaoProcesso")

    # Partes (tabela principal + demais)
    partes = []
    for tab_id in ("tablePartesPrincipais", "tableTodasPartes"):
        tab = soup.find(id=tab_id)
        if not tab:
            continue
        for tr in tab.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) >= 2:
                tipo = _txt(tds[0])
                nome = _txt(tds[-1])
                if nome:
                    partes.append({"tipo": tipo, "nome": nome})
        if partes:
            break
    out["partes"] = partes

    # Movimentações
    movs = []
    mtab = soup.find(id="tabelaTodasMovimentacoes") or soup.find(id="tabelaMovimentacoes")
    if mtab:
        for tr in mtab.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) >= 2:
                data = _txt(tds[0])
                desc = _txt(tds[-1])
                if data or desc:
                    movs.append({"data": data, "descricao": desc})
    out["total_movimentacoes"] = len(movs)
    out["movimentacoes"] = movs

    return out


def main():
    site, estado, url, perfil = TJ_SISTEMAS[LABEL]
    tribunal, uf = _ESTADO_TJ_UF[estado]
    print(f"=== e-SAJ {tribunal}/{uf} — processo {NUMERO} ===")
    print(f"site={site} url={url}")

    scraper = criar_scraper(site=site, estado=estado, url=url, headless=False, perfil=perfil)
    try:
        scraper.login()
        print(">>> login OK, consultando...")
        html = scraper.consultar_processo(NUMERO)
        if not html:
            print("!!! consulta retornou None")
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dbg = ROOT / "data" / "_debug"
        dbg.mkdir(parents=True, exist_ok=True)
        html_path = dbg / f"esaj_{NUMERO.replace('.', '_').replace('-', '_')}_{ts}.html"
        html_path.write_text(html, encoding="utf-8")
        print(f">>> HTML salvo: {html_path}  ({len(html)} bytes)")

        dados = parse_esaj(html)
        kw = list(dict.fromkeys(
            k["descricao"] for k in extrair_penhora_bloqueio_tribunais(html) if k.get("descricao")
        ))
        dados["penhora_bloqueio"] = kw

        print("\n=== DADOS EXTRAÍDOS ===")
        for k in ("numero", "classe", "assunto", "area", "foro", "vara", "juiz",
                  "valor_acao", "distribuicao", "situacao"):
            print(f"  {k:14}: {dados.get(k) or '—'}")
        print(f"  partes        : {len(dados['partes'])}")
        for p in dados["partes"][:12]:
            print(f"      - {p['tipo']}: {p['nome']}")
        print(f"  movimentações : {dados['total_movimentacoes']}")
        for m in dados["movimentacoes"][:8]:
            print(f"      [{m['data']}] {m['descricao'][:90]}")
        print(f"  penhora/bloq  : {', '.join(kw) if kw else '—'}")

        json_path = dbg / f"esaj_{NUMERO.replace('.', '_').replace('-', '_')}_{ts}.json"
        json_path.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n>>> JSON salvo: {json_path}")
    finally:
        try:
            scraper.fechar()
        except Exception:
            pass


if __name__ == "__main__":
    main()
