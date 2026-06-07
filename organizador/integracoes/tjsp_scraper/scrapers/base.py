# src/scrapers/base.py
"""
Interface comum dos scrapers + helper de criação do Chrome.

Contrato:
  - login()                     : autentica (1x por sessão; reusa depois)
  - consultar_processo(numero)  : retorna o HTML da página do processo
                                  (ou None se não achou / erro)
  - fechar()                    : encerra o navegador

O parsing NÃO é feito aqui — quem chama passa o HTML para
src.parser.extrair_penhora_bloqueio_tribunais().
"""

import os
from abc import ABC, abstractmethod
from typing import Optional

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager


def criar_chrome_driver(headless: bool = False) -> webdriver.Chrome:
    """
    Cria o driver Chrome com as opções comprovadas do projeto
    (mesmas de src/scraper.py). Suporta Selenium remoto via
    SELENIUM_REMOTE_URL; senão usa ChromeDriver local.
    """
    options = webdriver.ChromeOptions()
    options.page_load_strategy = "eager"
    options.add_argument("--window-size=1920,1080")

    if headless:
        options.add_argument("--headless=new")

    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    # Não carregar imagens (ganho de velocidade)
    options.add_experimental_option(
        "prefs", {"profile.managed_default_content_settings.images": 2}
    )
    options.add_argument("--lang=pt-BR")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"
    )

    remote_url = os.environ.get("SELENIUM_REMOTE_URL")
    if remote_url:
        return webdriver.Remote(command_executor=remote_url, options=options)

    if os.path.exists("/usr/bin/chromedriver"):
        service = Service("/usr/bin/chromedriver")
    else:
        service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


class TribunalScraper(ABC):
    """Interface que todo scraper de tribunal deve implementar."""

    def __init__(self, estado: str, url: str, headless: bool = False):
        self.estado = estado
        self.url = url
        self.headless = headless
        self.driver = None
        self._logado = False

    @abstractmethod
    def login(self) -> None:
        """Autentica no sistema. Idempotente: se já logado, não refaz."""

    @abstractmethod
    def consultar_processo(self, numero_processo: str) -> Optional[str]:
        """Consulta o processo e retorna o HTML da página, ou None."""

    def fechar(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
