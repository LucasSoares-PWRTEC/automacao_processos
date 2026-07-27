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
import time
from abc import ABC, abstractmethod
from typing import Optional

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager


def _opcoes_chrome(headless: bool, user_data_dir: Optional[str]) -> "webdriver.ChromeOptions":
    """Monta as ChromeOptions comprovadas do projeto (com perfil opcional)."""
    options = webdriver.ChromeOptions()
    options.page_load_strategy = "eager"
    options.add_argument("--window-size=1920,1080")

    if user_data_dir:
        options.add_argument(f"--user-data-dir={user_data_dir}")
        # Reduz a flakiness do 1º uso de um perfil dedicado (sem telas de
        # boas-vindas / verificação de navegador padrão que atrapalham o launch).
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")

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
    return options


def _matar_chrome_orfaos(user_data_dir: str) -> None:
    """Mata processos chrome.exe presos NESTE perfil persistente (órfãos de uma
    execução anterior que não fechou — travam o diretório e fazem o launch
    seguinte crashar com 'Chrome instance exited').

    Só no Windows e filtrando pela LINHA DE COMANDO que cita exatamente este
    perfil — portanto NUNCA toca o Chrome pessoal do usuário (que usa outro
    perfil). Best-effort: qualquer erro é ignorado.
    """
    if os.name != "nt" or not user_data_dir:
        return
    import subprocess

    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    alvo = os.path.normpath(user_data_dir).lower().replace("'", "''")
    # 1) Lista os PIDs de chrome.exe cuja linha de comando cita este perfil.
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        "Where-Object {{ $_.CommandLine -and "
        "$_.CommandLine.ToLower().Contains('{alvo}') }} | "
        "Select-Object -ExpandProperty ProcessId"
    ).format(alvo=alvo)
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=20, creationflags=no_window,
        ).stdout
        pids = [p.strip() for p in out.split() if p.strip().isdigit()]
        # 2) Mata cada um (com a árvore de filhos) via taskkill.
        for pid in pids:
            subprocess.run(
                ["taskkill", "/PID", pid, "/F", "/T"],
                capture_output=True, timeout=15, creationflags=no_window,
            )
    except Exception:
        pass


def _iniciar_chrome(options: "webdriver.ChromeOptions") -> webdriver.Chrome:
    """Sobe o Chrome (remoto se SELENIUM_REMOTE_URL; senão ChromeDriver local)."""
    remote_url = os.environ.get("SELENIUM_REMOTE_URL")
    if remote_url:
        return webdriver.Remote(command_executor=remote_url, options=options)

    if os.path.exists("/usr/bin/chromedriver"):
        service = Service("/usr/bin/chromedriver")
    else:
        service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


def criar_chrome_driver(
    headless: bool = False, user_data_dir: Optional[str] = None
) -> webdriver.Chrome:
    """
    Cria o driver Chrome com as opções comprovadas do projeto
    (mesmas de src/scraper.py). Suporta Selenium remoto via
    SELENIUM_REMOTE_URL; senão usa ChromeDriver local.

    `user_data_dir`: quando informado, usa um perfil PERSISTENTE do Chrome
    nesse diretório (mantém cookies/sessão entre execuções). Usado só pelo
    Autojur para pular o 2FA. Um perfil só pode ser aberto por UM processo por
    vez — se o perfil estiver travado (outro Chrome aberto no mesmo diretório,
    ou lock de uma instância que não fechou), o launch falha com
    "Chrome instance exited". Nesse caso caímos para um perfil temporário para
    o app não travar (o 2FA pode ser pedido de novo, mas o navegador abre).
    """
    if user_data_dir:
        # Libera o perfil de órfãos de execuções anteriores (a causa nº 1 do
        # "Chrome instance exited" no perfil persistente).
        _matar_chrome_orfaos(user_data_dir)
    try:
        return _iniciar_chrome(_opcoes_chrome(headless, user_data_dir))
    except Exception as exc:
        if not user_data_dir:
            raise
        # 2ª tentativa: mata órfãos de novo (o lock pode ter demorado a soltar,
        # ex.: relogin por mudança de visibilidade) e tenta mais uma vez.
        _matar_chrome_orfaos(user_data_dir)
        time.sleep(1.5)
        try:
            return _iniciar_chrome(_opcoes_chrome(headless, user_data_dir))
        except Exception:
            print(
                f"Chrome: perfil persistente indisponível ({type(exc).__name__}); "
                "usando perfil temporário. Feche outros navegadores abertos nesse "
                "perfil para reaproveitar a sessão (e pular o 2FA)."
            )
            return _iniciar_chrome(_opcoes_chrome(headless, None))


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
