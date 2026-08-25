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
import shutil
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

from .... import navegadores

# --------------------------------------------------------------------------
# Qual navegador usar
# --------------------------------------------------------------------------
# O projeto nasceu assumindo o Google Chrome. Em máquina de escritório sem Chrome
# instalado, nada abria — nem a busca online nem o Autojur. O Edge resolve isso
# sem instalar nada: vem no Windows 10/11, é Chromium (aceita as MESMAS flags e
# `add_experimental_option`) e o Selenium tem driver próprio para ele. O Firefox
# entra como terceira opção, com ressalvas (ver `_opcoes_firefox`).
#
# A ordem é deliberada: Chrome primeiro porque é onde o fluxo foi validado ao
# vivo (2FA, pop-ups, iframes do Autojur).
NAVEGADORES = ("chrome", "edge", "firefox")

# Override manual: ORGANIZADOR_NAVEGADOR=edge força um navegador específico.
_ENV_NAVEGADOR = "ORGANIZADOR_NAVEGADOR"

# Onde procurar o executável de cada navegador, além do PATH. `%` é expandido.
_CAMINHOS_NAVEGADOR = {
    "chrome": (
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    ),
    "edge": (
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe",
    ),
    "firefox": (
        r"%ProgramFiles%\Mozilla Firefox\firefox.exe",
        r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe",
    ),
}
_EXECUTAVEL_NO_PATH = {"chrome": "chrome", "edge": "msedge", "firefox": "firefox"}


def caminho_navegador(nome: str) -> Optional[str]:
    """Caminho do executável do navegador `nome`, ou None se não estiver instalado."""
    no_path = shutil.which(_EXECUTAVEL_NO_PATH.get(nome, nome))
    if no_path:
        return no_path
    for bruto in _CAMINHOS_NAVEGADOR.get(nome, ()):
        caminho = os.path.expandvars(bruto)
        if "%" not in caminho and os.path.isfile(caminho):
            return caminho
    return None


def navegadores_disponiveis() -> list[str]:
    """Navegadores instalados nesta máquina, na ordem de preferência."""
    return [n for n in NAVEGADORES if caminho_navegador(n)]


def _ordem_de_tentativa() -> list[str]:
    forcado = (os.getenv(_ENV_NAVEGADOR) or "").strip().lower()
    if forcado in NAVEGADORES:
        return [forcado]
    disponiveis = navegadores_disponiveis()
    # Nenhum detectado: ainda tentamos a lista toda. A detecção olha caminhos
    # conhecidos e o PATH; uma instalação fora do padrão pode escapar dela, e
    # falhar ao subir é melhor que desistir sem tentar.
    return disponiveis or list(NAVEGADORES)


def _log_seguro(msg: str) -> None:
    """`print` que nunca derruba quem chamou.

    Num console cp1252 (o padrão do Windows fora do app), imprimir acento ou
    emoji levanta UnicodeEncodeError. Uma linha de log não pode impedir o
    navegador de abrir — foi o que aconteceu ao testar o fallback do Edge."""
    try:
        print(msg)
    except Exception:
        try:
            print(msg.encode("ascii", "replace").decode("ascii"))
        except Exception:
            pass


def _aplicar_flags_chromium(options, headless: bool, user_data_dir: Optional[str]):
    """Flags comuns a Chrome e Edge (os dois são Chromium)."""
    options.page_load_strategy = "eager"
    options.page_load_strategy = "eager"
    # 1280x800 em vez de 1920x1080: as páginas dos tribunais são consultadas, não
    # vistas; menos pixels = menos memória de raster. Importa nas máquinas de 4 GB,
    # onde a árvore de processos do Chrome é o maior consumidor.
    options.add_argument("--window-size=1280,800")

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
    # A pref acima é ignorada em parte dos cenários com --headless=new; esta flag
    # atua no próprio Blink e vale nos dois modos. Somadas, garantem que nenhuma
    # imagem seja baixada nem decodificada (banda e RAM).
    options.add_argument("--blink-settings=imagesEnabled=false")
    # Serviços de fundo que só gastam CPU/rede numa sessão de scraping.
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-default-apps")
    options.add_argument("--no-service-autorun")
    options.add_argument("--mute-audio")
    options.add_argument("--metrics-recording-only")
    options.add_argument(
        "--disable-features=Translate,MediaRouter,OptimizationHints,"
        "AcceptCHFrame,InterestFeedContentSuggestions"
    )
    options.add_argument("--lang=pt-BR")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"
    )
    return options


def _opcoes_chrome(headless: bool, user_data_dir: Optional[str]):
    return _aplicar_flags_chromium(webdriver.ChromeOptions(), headless, user_data_dir)


def _opcoes_edge(headless: bool, user_data_dir: Optional[str]):
    """Edge é Chromium: mesmas flags do Chrome.

    `use_chromium=True` não é mais necessário no Selenium 4, mas o objeto de
    opções é o `EdgeOptions`."""
    return _aplicar_flags_chromium(webdriver.EdgeOptions(), headless, user_data_dir)


def _opcoes_firefox(headless: bool, user_data_dir: Optional[str]):
    """Firefox: última alternativa, com duas ressalvas.

    1) Não aceita as flags do Chromium nem `add_experimental_option`; o
       equivalente vai por `set_preference`.
    2) `user_data_dir` (perfil persistente do Autojur, que evita repetir o 2FA)
       usa outro mecanismo (`-profile`) e não é replicado aqui — no Firefox a
       sessão não é reaproveitada entre execuções e o 2FA pode ser pedido de novo.
    """
    options = webdriver.FirefoxOptions()
    options.page_load_strategy = "eager"
    if headless:
        options.add_argument("--headless")
    options.add_argument("--width=1280")
    options.add_argument("--height=800")
    options.set_preference("permissions.default.image", 2)  # não carrega imagens
    options.set_preference("dom.webnotifications.enabled", False)
    options.set_preference("intl.accept_languages", "pt-BR, pt")
    options.set_preference("media.volume_scale", "0.0")
    options.set_preference("browser.tabs.remote.autostart", False)
    return options


_OPCOES_POR_NAVEGADOR = {
    "chrome": _opcoes_chrome,
    "edge": _opcoes_edge,
    "firefox": _opcoes_firefox,
}


_EXE_DO_NAVEGADOR = {"chrome": "chrome.exe", "edge": "msedge.exe", "firefox": "firefox.exe"}


def _matar_chrome_orfaos(user_data_dir: str, navegador: str = "chrome") -> None:
    """Mata processos do navegador presos NESTE perfil persistente (órfãos de uma
    execução anterior que não fechou — travam o diretório e fazem o launch
    seguinte crashar com 'Chrome instance exited').

    Só no Windows e filtrando pela LINHA DE COMANDO que cita exatamente este
    perfil — portanto NUNCA toca o navegador pessoal do usuário (que usa outro
    perfil). Best-effort: qualquer erro é ignorado.
    """
    if os.name != "nt" or not user_data_dir:
        return
    import subprocess

    exe = _EXE_DO_NAVEGADOR.get(navegador, "chrome.exe")
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    alvo = os.path.normpath(user_data_dir).lower().replace("'", "''")
    # 1) Lista os PIDs do navegador cuja linha de comando cita este perfil.
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='{exe}'\" | "
        "Where-Object {{ $_.CommandLine -and "
        "$_.CommandLine.ToLower().Contains('{alvo}') }} | "
        "Select-Object -ExpandProperty ProcessId"
    ).format(exe=exe, alvo=alvo)
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


# Caminho do chromedriver resolvido nesta execução. `ChromeDriverManager().install()`
# faz uma ida à REDE para descobrir a versão compatível a cada chamada — e ela era
# chamada a cada criação de driver (o monitoramento cria vários). O caminho também
# é persistido em disco, então nas próximas execuções nem a 1ª criação vai à rede.
_driver_path_memo: Optional[str] = None


def _caminho_chromedriver() -> str:
    """Caminho do chromedriver, reaproveitado agressivamente.

    Ordem: memória do processo → arquivo de cache → webdriver-manager (rede).
    Se o binário apontado pelo cache desapareceu, refaz a resolução."""
    global _driver_path_memo
    if _driver_path_memo and os.path.exists(_driver_path_memo):
        return _driver_path_memo

    from ....config import APP_DATA_DIR  # tardio: evita ciclo no import

    cache = APP_DATA_DIR / "chromedriver.json"
    try:
        import json

        salvo = json.loads(cache.read_text(encoding="utf-8")).get("path")
        if salvo and os.path.exists(salvo):
            _driver_path_memo = salvo
            return salvo
    except Exception:
        pass

    _limpar_lock_wdm_velho()
    try:
        caminho = ChromeDriverManager().install()
    except Exception as exc:
        # O webdriver-manager falhou (lock preso, sem rede, GitHub fora). Se já
        # existe um chromedriver baixado, use-o: o app não precisa da versão mais
        # nova, precisa de UMA que funcione.
        caminho = _chromedriver_ja_baixado()
        if not caminho:
            raise
        _log_seguro(
            f"webdriver-manager falhou ({type(exc).__name__}); usando o "
            f"chromedriver já baixado: {caminho}"
        )
    _driver_path_memo = caminho
    try:
        import json

        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"path": caminho}), encoding="utf-8")
    except OSError:
        pass
    return caminho


# Um processo morto à força (crash, "Finalizar tarefa", queda de energia)
# deixa o lock do webdriver-manager para trás. Com o lock preso, TODA criação de
# driver espera e estoura em TimeoutError — e o Chrome nunca mais sobe, mesmo
# havendo chromedriver baixado. Foi assim que uma execução caiu silenciosamente
# no Edge: o lock era de um processo morto no dia anterior.
_IDADE_LOCK_MORTO_S = 300  # 5 min: nenhuma resolução legítima demora tanto


def _limpar_lock_wdm_velho() -> None:
    """Apaga locks do webdriver-manager antigos o bastante para serem órfãos.

    `except OSError` e não `except Exception`: um `except` largo aqui já escondeu
    um NameError (Path não importado) e a limpeza ficou não fazendo nada em
    silêncio. Erro de programação deve aparecer; só falha de disco é tolerada."""
    pasta = Path.home() / ".wdm"
    if not pasta.is_dir():
        return
    agora = time.time()
    for lock in pasta.glob(".wdm-lock-*"):
        try:
            if agora - lock.stat().st_mtime > _IDADE_LOCK_MORTO_S:
                lock.unlink()
                _log_seguro(f"lock órfão do webdriver-manager removido: {lock.name}")
        except OSError:
            pass


def _chromedriver_ja_baixado() -> Optional[str]:
    """chromedriver.exe mais recente em ~/.wdm/drivers, ou None."""
    raiz = Path.home() / ".wdm" / "drivers"
    if not raiz.is_dir():
        return None
    try:
        achados = [p for p in raiz.rglob("chromedriver.exe") if p.is_file()]
    except OSError:
        return None
    if not achados:
        return None
    # Mais recente por data de modificação = a última versão baixada.
    return str(max(achados, key=lambda p: p.stat().st_mtime))


def _iniciar_chrome(options) -> webdriver.Chrome:
    """Sobe o Chrome (remoto se SELENIUM_REMOTE_URL; senão ChromeDriver local)."""
    remote_url = os.environ.get("SELENIUM_REMOTE_URL")
    if remote_url:
        return webdriver.Remote(command_executor=remote_url, options=options)

    if os.path.exists("/usr/bin/chromedriver"):
        service = Service("/usr/bin/chromedriver")
    else:
        service = Service(_caminho_chromedriver())
    return webdriver.Chrome(service=service, options=options)


def _iniciar_edge(options) -> "webdriver.Edge":
    """Sobe o Edge. O driver é resolvido pelo Selenium Manager (embutido no
    Selenium 4.6+), que baixa e cacheia o msedgedriver compatível sozinho — não
    precisamos do webdriver-manager aqui."""
    remote_url = os.environ.get("SELENIUM_REMOTE_URL")
    if remote_url:
        return webdriver.Remote(command_executor=remote_url, options=options)
    return webdriver.Edge(options=options)


def _iniciar_firefox(options) -> "webdriver.Firefox":
    remote_url = os.environ.get("SELENIUM_REMOTE_URL")
    if remote_url:
        return webdriver.Remote(command_executor=remote_url, options=options)
    return webdriver.Firefox(options=options)


_INICIADOR_POR_NAVEGADOR = {
    "chrome": _iniciar_chrome,
    "edge": _iniciar_edge,
    "firefox": _iniciar_firefox,
}

# Qual navegador funcionou nesta execução. Memoizado para o monitoramento não
# repetir a tentativa-e-erro a cada scraper que sobe.
_navegador_ok: Optional[str] = None


def _iniciar_navegador(nome: str, headless: bool, user_data_dir: Optional[str]):
    """Sobe UM navegador específico. `user_data_dir` só vale para Chromium."""
    perfil = user_data_dir if nome in ("chrome", "edge") else None
    if user_data_dir and perfil is None:
        _log_seguro(
            f"AVISO {nome}: perfil persistente não suportado nesta rota; a sessão "
            "não será reaproveitada (o 2FA pode ser pedido de novo)."
        )
    options = _OPCOES_POR_NAVEGADOR[nome](headless, perfil)
    return _INICIADOR_POR_NAVEGADOR[nome](options)


def _pid_do_driver(driver) -> Optional[int]:
    """PID do processo do DRIVER (chromedriver.exe), ou None.

    É o pai da árvore do navegador: matar a árvore dele leva o navegador junto.
    `webdriver.Remote` não tem `service` — daí o getattr em cadeia."""
    pid = getattr(getattr(getattr(driver, "service", None), "process", None), "pid", None)
    try:
        return int(pid) if pid else None
    except (TypeError, ValueError):
        return None


def _registrado(driver):
    """Anota o driver no registro de navegadores vivos e o devolve.

    Sem isto, um navegador cujo `quit()` não termina (o app fecha antes) fica
    órfão e invisível na máquina — ver organizador/navegadores.py."""
    pid = _pid_do_driver(driver)
    if pid:
        navegadores.registrar(pid)
    return driver


def criar_chrome_driver(
    headless: bool = False, user_data_dir: Optional[str] = None
):
    """Cria o driver do navegador, tentando Chrome → Edge → Firefox.

    O nome ficou por compatibilidade (os quatro scrapers já o importam), mas não
    é mais só Chrome: em máquina sem o Google Chrome instalado nada abria — nem a
    busca online nem o Autojur. O Edge cobre esse caso sem instalar nada (vem no
    Windows e é Chromium, então aceita as mesmas flags).

    `ORGANIZADOR_NAVEGADOR=edge|chrome|firefox` força um navegador específico.

    `user_data_dir`: perfil PERSISTENTE (mantém cookies/sessão entre execuções).
    Usado só pelo Autojur, para pular o 2FA. Um perfil só pode ser aberto por UM
    processo por vez — se estiver travado, o launch falha com "Chrome instance
    exited"; nesse caso limpamos os órfãos e, na pior hipótese, caímos para um
    perfil temporário (o 2FA pode ser pedido de novo, mas o navegador abre).
    Só vale para Chrome/Edge (ver `_iniciar_navegador`).
    """
    global _navegador_ok

    # Se um navegador já funcionou nesta execução, começa por ele.
    ordem = _ordem_de_tentativa()
    if _navegador_ok in ordem:
        ordem = [_navegador_ok] + [n for n in ordem if n != _navegador_ok]

    erros: list[str] = []
    for nome in ordem:
        # A varredura de órfãos NÃO roda preventivamente: ela sobe um
        # `powershell Get-CimInstance Win32_Process` que percorre a tabela inteira
        # de processos (1-3 s e ~60 MB num i3), e no caso normal não há órfão
        # nenhum. Só limpamos quando o launch falha COM perfil persistente, que é
        # exatamente o sintoma de perfil travado.
        try:
            driver = _iniciar_navegador(nome, headless, user_data_dir)
        except Exception as exc:
            if user_data_dir and nome in ("chrome", "edge"):
                _matar_chrome_orfaos(user_data_dir, nome)
                time.sleep(1.5)
                try:
                    driver = _iniciar_navegador(nome, headless, user_data_dir)
                except Exception:
                    _log_seguro(
                        f"{nome}: perfil persistente indisponível "
                        f"({type(exc).__name__}); tentando com perfil temporário. "
                        "Feche outros navegadores abertos nesse perfil para "
                        "reaproveitar a sessão (e pular o 2FA)."
                    )
                    try:
                        driver = _iniciar_navegador(nome, headless, None)
                    except Exception as exc2:
                        erros.append(f"{nome}: {type(exc2).__name__}: {exc2}")
                        continue
                else:
                    _navegador_ok = nome
                    return _registrado(driver)
            else:
                erros.append(f"{nome}: {type(exc).__name__}: {exc}")
                # Diz POR QUE o preferido não subiu. Sem isto o fallback era
                # silencioso: uma execução caiu no Edge por causa de um lock
                # órfão do webdriver-manager e nada indicava o motivo — o Edge
                # não tem o perfil persistente do Autojur (2FA de novo) e o
                # fluxo se comporta diferente, então isso precisa aparecer.
                _log_seguro(
                    f"{nome} não pôde ser aberto ({type(exc).__name__}: "
                    f"{str(exc).splitlines()[0][:160]}); tentando o próximo"
                )
                continue

        if _navegador_ok != nome:
            _log_seguro(f"Navegador em uso: {nome}")
        _navegador_ok = nome
        return _registrado(driver)

    instalados = navegadores_disponiveis()
    raise RuntimeError(
        "Nenhum navegador pôde ser aberto. "
        + (
            f"Detectados nesta máquina: {', '.join(instalados)}. "
            if instalados else
            "Nenhum navegador compatível foi encontrado — instale o Google Chrome "
            "ou o Microsoft Edge. "
        )
        + "Tentativas: " + " | ".join(erros)
    )


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
        """Encerra o navegador pelo caminho educado (`quit()`).

        O PID é lido ANTES do `quit()` — depois o `service` já não tem processo —
        e só sai do registro de vivos quando o `quit()` volta. Se ele nunca
        voltar (ou for interrompido pela saída do processo), o PID continua lá
        para o `taskkill` do fechamento pegar. Ver organizador/navegadores.py."""
        if self.driver is not None:
            pid = _pid_do_driver(self.driver)
            try:
                self.driver.quit()
            except Exception:
                pass
            navegadores.esquecer(pid)
            self.driver = None
