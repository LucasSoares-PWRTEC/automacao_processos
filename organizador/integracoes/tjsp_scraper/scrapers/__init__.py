# src/scrapers/__init__.py
"""
Pacote de scrapers de tribunais.

Cada sistema (esaj, eproc, pje, ...) tem uma classe que herda de
TribunalScraper. A função criar_scraper() é a fábrica usada pelo
fluxo principal.
"""

from .base import TribunalScraper
from .esaj import ESAJScraper
from .pje import PJEScraper
from .eproc import EprocScraper


def criar_scraper(site: str, estado: str, url: str, headless: bool = False) -> TribunalScraper:
    """
    Fábrica: devolve a instância de scraper para o 'site'.

    Levanta NotImplementedError para sites ainda não implementados,
    para o fluxo principal poder pular o site sem derrubar a execução.
    """
    site = (site or "").strip().lower()

    if site == "esaj":
        return ESAJScraper(estado=estado, url=url, headless=headless)
    if site == "pje":
        return PJEScraper(estado=estado, url=url, headless=headless)
    if site == "eproc":
        return EprocScraper(estado=estado, url=url, headless=headless)

    raise NotImplementedError(
        f"Scraper para o site '{site}' ainda não implementado."
    )


__all__ = ["TribunalScraper", "ESAJScraper", "PJEScraper", "EprocScraper", "criar_scraper"]
