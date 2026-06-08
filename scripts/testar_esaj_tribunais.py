# -*- coding: utf-8 -*-
"""Testa login no e-SAJ de vários tribunais (sequencial).

Para cada (tribunal, url): cria o ESAJScraper, faz login real (CPF+senha+2FA)
e confirma que a tela de consulta (campo numeroDigitoAnoUnificado) carrega.
NÃO consulta processo (não temos números válidos por tribunal) — login +
tela de busca é o que prova que o sistema funciona para aquele e-SAJ.

Sequencial de propósito: o código 2FA cai num inbox compartilhado e o leitor
pega o mais recente; rodar um de cada vez evita pegar o código do tribunal
errado. Timeout de 2FA encurtado via ESAJ_2FA_TIMEOUT_SEG.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("ESAJ_2FA_TIMEOUT_SEG", "150")

from dotenv import load_dotenv
from selenium.webdriver.common.by import By

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from organizador.config import CONFIG_ENV_PATH
from organizador.integracoes.tjsp_scraper.scrapers.esaj import ESAJScraper

load_dotenv(CONFIG_ENV_PATH)

CANDIDATOS = [
    ("TJAL", "ALAGOAS",            "https://www2.tjal.jus.br/cpopg/open.do"),
    ("TJAM", "AMAZONAS",           "https://consultasaj.tjam.jus.br/cpopg/open.do"),
    ("TJCE", "CEARA",              "https://esaj.tjce.jus.br/cpopg/open.do"),
    ("TJMS", "MATO GROSSO DO SUL", "https://esaj.tjms.jus.br/cpopg/open.do"),
]


def testar(tribunal, estado, url):
    print(f"\n===== {tribunal} — {url} =====", flush=True)
    scraper = None
    try:
        scraper = ESAJScraper(estado=estado, url=url, headless=False)
        scraper.login()
        # Confirma a tela de consulta do cpopg.
        scraper.driver.get(url)
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        try:
            WebDriverWait(scraper.driver, 15).until(
                EC.presence_of_element_located((By.ID, "numeroDigitoAnoUnificado"))
            )
            print(f"RESULTADO {tribunal}: PASSOU (login OK + tela de consulta carregou)", flush=True)
            return True
        except Exception:
            print(f"RESULTADO {tribunal}: PARCIAL (login OK, mas campo de busca não apareceu)", flush=True)
            return False
    except Exception as e:
        print(f"RESULTADO {tribunal}: FALHOU — {type(e).__name__}: {str(e)[:160]}", flush=True)
        return False
    finally:
        if scraper is not None:
            try:
                scraper.fechar()
            except Exception:
                pass


def main():
    resultados = {}
    for tribunal, estado, url in CANDIDATOS:
        resultados[tribunal] = testar(tribunal, estado, url)
    print("\n========== RESUMO ==========", flush=True)
    for tj, ok in resultados.items():
        print(f"  {tj}: {'PASSOU' if ok else 'FALHOU'}", flush=True)


if __name__ == "__main__":
    main()
