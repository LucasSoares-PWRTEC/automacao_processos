# src/scrapers/autojur.py
"""
Login no Autojur (gestão de processos do escritório) — JSF/PrimeFaces.

Fluxo mapeado do HTML real de https://mcsa.autojur.com.br/login.jsf:
  form `login-form-novo` (campos username/password + submit oculto
  `btn-login-hide` acionado via PrimeFaces AJAX) e, após as credenciais,
  modais que podem ou não aparecer, em qualquer ordem:
    - form-dupla-autenticacao  -> código de verificação por email (IMAP)
    - form-derrubar-usuario    -> já existe sessão ativa; confirmamos derrubar
    - form-unidade             -> escolher unidade; pegamos a primeira
    - form-motivo-login        -> motivo do acesso; preenchemos genérico
    - form-alterar-senha-obrigatorio -> ABORTA (troca de senha é decisão humana)

Credenciais: email_autojur / senha_autojur no config/.env. O código de
verificação chega no email do IMAP_USER (mesma caixa dos 2FA dos TJs).

Consulta de processos ainda não implementada — este módulo entrega só a
sessão logada, base da próxima função.
"""

import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Optional

from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    StaleElementReferenceException,
)

from .base import TribunalScraper, criar_chrome_driver
from ..diagnostico import salvar_screenshot
from ..two_factor import obter_codigo_2fa_email

URL_LOGIN = "https://mcsa.autojur.com.br/login.jsf"
URL_PROCESSOS = "https://mcsa.autojur.com.br/sistema/processos/processo.jsf"

# ── Seletores do fluxo "criar tarefa de Protocolo" (validados ao vivo) ──────
# Tela de processos (fora do iframe).
SEL_TOGGLE_NOVA_VERSAO = 'a[data-title="Nova versão"]'
SEL_BUSCA_INPUT = "form-pesquisa:componente-pesquisa:txt-conteudo"
SEL_BUSCA_LUPA = "form-pesquisa:componente-pesquisa:btn-pesquisar"
SEL_TIPO_PASTA = 'button[data-id="form-pesquisa:tipo-proc"]'
SEL_STATUS_TODOS = 'label[for="form-pesquisa:radio-status:0"]'
SEL_LINHAS_RESULTADO = "div.ui-datatable-tablewrapper tbody tr"
SEL_BTN_ADD_TAREFA = "btnAcoes:btnAddTarefa"
# Diálogo da tarefa (DENTRO do iframe mdAdicionarTarefa.jsf).
SEL_TIPO_EVENTO = '[data-id="formDetalhesTarefa:ff-tipo-evento:cmb-tipo-evento"]'
SEL_EVENTO_INPUT = "formDetalhesTarefa:cmb-evento:ac-evento:ac-evento_input"
SEL_EVENTO_PANEL = "formDetalhesTarefa:cmb-evento:ac-evento:ac-evento_panel"
SEL_LOCAL_INPUT = "formDetalhesTarefa:ff-local:pac-input"
SEL_CONTEUDO = "formDetalhesTarefa:ff-conteudo:conteudo"
SEL_LOTE = '[data-id="formDetalhesTarefa:ff-lote:cmb-lotes"]'
SEL_TIPO_DOC = '[data-id="ged-panel-component-tarefa:ged-body:form-upload:select-tipo-documento"]'
SEL_UPLOAD_INPUT = "ged-panel-component-tarefa:ged-body:form-upload:btn-upload_input"
SEL_PAINEL_GED = "ged-panel-component-tarefa:conteudo"
# Botão "Requer Protocolo" de cada documento anexado (um por linha; id termina
# em ":<linha>:btnProtocoloGed"). O estado é o ícone: fa-square (não marcado) ->
# fa-check-square (marcado). O title some ao marcar, por isso miramos pelo id.
SEL_BTN_PROTOCOLO_GED = 'a[id*="btnProtocoloGed"]'

# Valores fixos do fluxo de protocolo.
TIPO_EVENTO_PADRAO = "Controladoria Jurídica - CVA"
EVENTO_PADRAO = "Protocolo"
TIPO_DOC_PADRAO = "Peça Processual"
# Opções de "Lote" que a pessoa escolhe no app -> como achar na lista do Autojur
# (as opções têm a data do dia embutida, então casamos por sufixo/ausência dele).
LOTES = ("iniciais", "urgente", "dia_seguinte")

# Email real observado (2026-07-08): From "AUTOJUR <noreply@perceptvision.com.br>",
# assunto "Tentativa de login em um novo dispositivo", corpo só em HTML com
# "Retorne à página de login e digite o código abaixo: HXFSBBQJ" — chave
# ALFANUMÉRICA maiúscula (8 chars), não os 6 dígitos do regex padrão.
REMETENTE_2FA = "autojur"
ASSUNTO_2FA = "novo dispositivo"
TIMEOUT_2FA_SEG = int(os.getenv("AUTOJUR_2FA_TIMEOUT_SEG", "180"))

# Âncora na frase para não capturar tokens maiúsculos soltos do template.
RX_CHAVE_ACESSO = re.compile(
    r"(?i:digite\s+o\s+c[óo]digo\s+abaixo)\W{0,10}([A-Z0-9]{6,12})\b"
)


def _html_para_texto(html: str) -> str:
    """Corpo do email do Autojur não tem parte text/plain: remove style/script
    e tags para o regex enxergar 'digite o código abaixo: XXXXXXXX'."""
    import html as _html
    texto = re.sub(r"(?is)<(style|script)\b.*?</\1>", " ", html)
    texto = re.sub(r"(?s)<[^>]+>", " ", texto)
    return re.sub(r"\s+", " ", _html.unescape(texto))



class _LinhaEvento:
    """Par (linha, célula) do autocomplete de Evento, com a cara de um elemento.

    O TEXTO e o selo 'T' estão na linha (`tr`), mas o clique tem de ir na célula
    (`td`) — clicar no `tr` não seleciona no PrimeFaces. Isto deixa
    `_clicar_item` tratar os dois como um só alvo. Os acessos são repassados ao
    vivo de propósito: se a linha ficar obsoleta, o StaleElementReferenceException
    sobe e o `_clicar_item` refaz a busca."""

    __slots__ = ("linha", "alvo")

    def __init__(self, linha, alvo):
        self.linha = linha
        self.alvo = alvo

    @property
    def text(self) -> str:
        return self.linha.text

    def find_elements(self, *args, **kwargs):
        return self.linha.find_elements(*args, **kwargs)

    def click(self) -> None:
        self.alvo.click()


class AutojurScraper(TribunalScraper):

    def __init__(self, headless: bool = False, user_data_dir: Optional[str] = None):
        super().__init__(estado="", url=URL_LOGIN, headless=headless)
        self.driver = criar_chrome_driver(headless=headless, user_data_dir=user_data_dir)
        self.wait = WebDriverWait(self.driver, 20)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _screenshot(self, nome: str):
        """No-op sem SCRAPER_DEBUG=1 (ver ../diagnostico.py)."""
        salvar_screenshot(self.driver, "autojur", nome)

    def _visivel(self, by, valor):
        """Elemento presente E visível, ou None (sem espera)."""
        try:
            el = self.driver.find_element(by, valor)
            return el if el.is_displayed() else None
        except Exception:
            return None

    def _esperar_form_sumir(self, form_id: str, timeout: float = 10.0) -> None:
        """Espera o modal `form_id` sair da tela após um submit AJAX.

        Substitui os `time.sleep(2)` que ficavam depois de cada `_submit_form`:
        o que interessa não é "2 segundos", é "o modal foi embora". Na prática o
        PrimeFaces responde em algumas centenas de ms, então isto devolve o
        controle bem antes — e, quando o servidor demora mais que 2 s, espera o
        quanto for preciso em vez de seguir com a tela ainda aberta."""
        try:
            WebDriverWait(self.driver, timeout).until(
                lambda d: self._visivel(By.ID, form_id) is None
            )
        except Exception:
            pass  # segue o fluxo: quem chamou revalida o estado da tela

    def _esperar_dropdown_aberto(self, timeout: float = 8.0) -> bool:
        """Espera o dropdown do bootstrap-select abrir (substitui sleep fixo)."""
        try:
            return bool(WebDriverWait(self.driver, timeout).until(
                lambda d: d.find_elements(
                    By.CSS_SELECTOR, "div.bootstrap-select.open ul.dropdown-menu"
                )
            ))
        except Exception:
            return False

    def _esperar_dropdown_fechado(self, timeout: float = 8.0) -> None:
        """Espera o dropdown fechar E o AJAX que a seleção dispara terminar.

        Fechar o dropdown NÃO é o fim da história: escolher uma opção dispara um
        AJAX que cobre a tela com um spinner. Quem clicasse no elemento seguinte
        nesse intervalo levava
        `ElementClickInterceptedException: Other element would receive the click:
        <div class="google-spin-wrapper">`. O `sleep(1.2)` antigo cobria isso por
        acidente."""
        try:
            WebDriverWait(self.driver, timeout).until_not(
                lambda d: d.find_elements(By.CSS_SELECTOR, "div.bootstrap-select.open")
            )
        except Exception:
            pass
        self._esperar_sem_overlay(timeout=timeout)

    # Overlays que o Autojur põe na frente de tudo durante um AJAX. Clicar com um
    # destes visível resulta em ElementClickInterceptedException.
    _SEL_OVERLAYS = (
        "div.google-spin-wrapper",   # spinner do próprio Autojur
        "div.ui-blockui",            # PrimeFaces bloqueando o form
        "div.ui-widget-overlay",     # modal do PrimeFaces
        "div.blockUI",               # jQuery blockUI
    )

    def _esperar_sem_overlay(self, timeout: float = 10.0) -> bool:
        """Espera nenhum overlay de carregamento estar VISÍVEL. True se limpou.

        A visibilidade é medida no navegador numa única chamada: estes contêineres
        normalmente existem no DOM o tempo todo e só ficam visíveis durante o
        AJAX, então procurar pela presença deles daria falso positivo eterno."""
        js = """
        const sels = arguments[0];
        const vis = el => !!(el.offsetWidth || el.offsetHeight ||
                             el.getClientRects().length);
        for (const s of sels) {
            for (const el of document.querySelectorAll(s)) {
                if (vis(el)) return true;
            }
        }
        return false;
        """
        try:
            WebDriverWait(self.driver, timeout).until_not(
                lambda d: d.execute_script(js, list(self._SEL_OVERLAYS))
            )
            return True
        except Exception:
            return False

    def _esperar_input_upload(self, timeout: float = 30.0):
        """`input[type=file]` do painel GED, esperando o AJAX que o monta.

        Mira primeiro no id conhecido; se ele não vier, aceita qualquer
        `input[type=file]` dentro do painel do GED — o id do PrimeFaces carrega o
        caminho do componente e muda se a tela for reorganizada, e nesse caso
        falhar por id seria falhar por um detalhe cosmético."""
        self._esperar_sem_overlay()
        try:
            return WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((By.ID, SEL_UPLOAD_INPUT))
            )
        except Exception:
            pass
        try:
            return WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, 'input[type="file"]')
                )
            )
        except Exception:
            self._screenshot("input_upload_ausente")
            ids = []
            try:
                ids = self.driver.execute_script(
                    "return [...document.querySelectorAll('input[type=file]')]"
                    ".map(e => e.id);"
                ) or []
            except Exception:
                pass
            raise RuntimeError(
                "Autojur: não achei o campo de upload do documento. "
                f"inputs de arquivo na tela: {ids or '(nenhum)'}"
            )

    def _clicar_seguro(self, by, seletor, timeout: float = 20.0):
        """Clica esperando o elemento ficar clicável e a tela estar livre.

        Sequência: espera overlay sair → espera clicável → rola até o centro →
        clica. Se ainda for interceptado (tooltip que aparece no hover, spinner
        que voltou), espera de novo e, em último caso, clica por JS — que ignora
        quem está por cima. O JS fica por último de propósito: ele também
        contorna um elemento genuinamente desabilitado, e aí o erro apareceria
        mais adiante, longe da causa."""
        self._esperar_sem_overlay()
        el = WebDriverWait(self.driver, timeout).until(
            EC.element_to_be_clickable((by, seletor))
        )
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        for tentativa in range(3):
            try:
                el.click()
                return el
            except ElementClickInterceptedException:
                self._esperar_sem_overlay()
                time.sleep(0.3)
                el = self.driver.find_element(by, seletor)  # pode ter re-renderizado
            except StaleElementReferenceException:
                el = self.driver.find_element(by, seletor)
        self.driver.execute_script("arguments[0].click();", el)
        return el

    # ------------------------------------------------------------------
    # Estabilidade do DOM (PrimeFaces re-renderiza listas depois de exibi-las)
    # ------------------------------------------------------------------
    # O PrimeFaces mostra a primeira linha de uma lista e SUBSTITUI o bloco
    # inteiro logo depois (autocomplete carregando sugestões, datatable
    # paginando). Quem guarda uma referência de elemento nesse intervalo recebe
    # StaleElementReferenceException ao usá-la — foi o que passou a acontecer
    # quando as esperas fixas viraram esperas condicionais: a espera devolve o
    # controle no PRIMEIRO item, não no último.
    #
    # A resposta certa não é dormir mais: é (1) esperar a lista parar de mudar e
    # (2) reencontrar o elemento no momento de usá-lo. Os dois helpers abaixo.

    def _esperar_lista_estavel(
        self, by, seletor, ciclos: int = 2, intervalo: float = 0.2,
        timeout: float = 10.0,
    ) -> int:
        """Espera a QUANTIDADE de elementos casados parar de mudar.

        Devolve a contagem final. É a condição que os `time.sleep` de "deixa o
        PrimeFaces terminar" tentavam aproximar — só que verificada, e sem gastar
        o tempo todo quando a lista já está pronta."""
        estaveis, anterior, fim = 0, -1, time.time() + timeout
        agora = 0
        while time.time() < fim:
            try:
                agora = len(self.driver.find_elements(by, seletor))
            except Exception:
                agora = -1
            if agora == anterior:
                estaveis += 1
                if estaveis >= ciclos:
                    return agora
            else:
                estaveis, anterior = 0, agora
            time.sleep(intervalo)
        return max(agora, 0)

    def _clicar_item(
        self, achar, casa, descricao: str, tentativas: int = 4,
        ultimo: bool = False,
    ) -> str:
        """Encontra a lista, escolhe o item que casa e clica — tudo numa passada.

        `achar()` refaz a busca (não receba uma lista pronta: ela pode estar
        obsoleta). `casa(texto, elemento) -> bool` decide o item. Se o DOM for
        re-renderizado durante a leitura, tenta de novo com elementos frescos.

        `ultimo=True` escolhe a ÚLTIMA ocorrência em vez da primeira — é o que a
        seleção de Lote fazia (laço sem `break`), e a diferença importa quando mais
        de uma opção casa o critério.

        Devolve o texto do item clicado."""
        motivo = "lista vazia"
        for _ in range(tentativas):
            try:
                alvo, texto_alvo = None, ""
                for el in achar():
                    texto = (el.text or "").strip()
                    if not casa(texto, el):
                        continue
                    alvo, texto_alvo = el, texto
                    if not ultimo:
                        break
                if alvo is not None:
                    alvo.click()
                    return texto_alvo
                motivo = "nenhum item casou"
            except StaleElementReferenceException:
                # A lista foi trocada no meio da leitura: refaz do zero.
                motivo = "lista re-renderizada durante a leitura"
                time.sleep(0.3)
                continue
        raise RuntimeError(f"Autojur: {descricao} — {motivo}.")

    @staticmethod
    def _sem_stale(fn, tentativas: int = 4, espera: float = 0.3):
        """Executa `fn` de novo se ela pegar um elemento obsoleto.

        `fn` PRECISA reencontrar seus elementos a cada chamada, senão a repetição
        não resolve nada."""
        ultimo: Exception | None = None
        for _ in range(tentativas):
            try:
                return fn()
            except StaleElementReferenceException as exc:
                ultimo = exc
                time.sleep(espera)
        raise ultimo if ultimo else RuntimeError("Autojur: falha inesperada.")

    def _submit_form(self, form_id: str) -> bool:
        """Aciona o submit PrimeFaces de um form. Os botões de submit do
        Autojur são ocultos (class "hide") e disparados por JS da página;
        clicá-los via JS executa o mesmo PrimeFaces.ab() do fluxo manual."""
        try:
            btn = self.driver.find_element(
                By.CSS_SELECTOR, f'form[id="{form_id}"] button[type="submit"]'
            )
            self.driver.execute_script("arguments[0].click();", btn)
            return True
        except Exception:
            return False

    def _mensagens_erro(self) -> str:
        """Texto visível de mensagens de erro/aviso do PrimeFaces, se houver."""
        textos = []
        for sel in (".ui-messages-error", ".ui-message-error",
                    ".ui-growl-message", ".alert-danger", ".alert"):
            for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    if el.is_displayed() and el.text.strip():
                        textos.append(el.text.strip())
                except Exception:
                    continue
        return " | ".join(dict.fromkeys(textos))

    def _parece_logado(self) -> bool:
        # A autenticação é AJAX e a URL PERMANECE em login.jsf mesmo com o
        # Painel de Tarefas aberto (visto ao vivo). A evidência de sessão é o
        # form de login ter saído da tela.
        url = (self.driver.current_url or "").lower()
        if "login.jsf" not in url:
            return True
        if self._visivel(By.ID, "login-form-novo:username") is not None:
            return False
        for fid in ("form-dupla-autenticacao", "form-derrubar-usuario",
                    "form-unidade", "form-motivo-login",
                    "form-alterar-senha-obrigatorio"):
            if self._visivel(By.ID, fid) is not None:
                return False
        return True

    def _debug_estado(self) -> None:
        """Loga o que cada checagem de login está vendo (AUTOJUR_DEBUG=1)."""
        if os.getenv("AUTOJUR_DEBUG") != "1":
            return
        vis = {
            fid: self._visivel(By.ID, fid) is not None
            for fid in (
                "login-form-novo:username", "form-dupla-autenticacao",
                "form-derrubar-usuario", "form-unidade", "form-motivo-login",
                "form-alterar-senha-obrigatorio",
            )
        }
        print(f"🔎 url={self.driver.current_url} visiveis={vis}")

    def _fechar_avisos(self) -> None:
        """Fecha modais informativos pós-login (ex.: 'O que há de novo'),
        deixando a sessão pronta para navegação."""
        for _ in range(3):
            fechado = False
            for btn in self.driver.find_elements(
                By.CSS_SELECTOR, '.modal button.close[data-dismiss="modal"]'
            ):
                try:
                    if btn.is_displayed():
                        btn.click()
                        fechado = True
                        time.sleep(0.5)
                except Exception:
                    continue
            if not fechado:
                return

    # ------------------------------------------------------------------
    # Modais pós-credenciais
    # ------------------------------------------------------------------
    def _tratar_dupla_autenticacao(self, desde: datetime) -> bool:
        campo = self._visivel(By.ID, "form-dupla-autenticacao:ff-token:token")
        if campo is None:
            return False
        print("📧 Autojur: pediu código de verificação; buscando no email...")
        # intervalo curto: com o filtro FROM no servidor cada poll é leve, então
        # detectamos o código ~2s após ele chegar (o e-mail é praticamente imediato).
        codigo = obter_codigo_2fa_email(
            desde=desde,
            remetente=REMETENTE_2FA,
            assunto_contem=ASSUNTO_2FA,
            regex_codigo=RX_CHAVE_ACESSO,
            timeout_seg=TIMEOUT_2FA_SEG,
            intervalo_seg=2,
            preprocessar=_html_para_texto,
        )
        if not codigo:
            # Se remetente/assunto mudarem, tenta só pelo formato da chave.
            print("⚠️ Autojur: nada com remetente/assunto esperados; tentando sem filtro...")
            codigo = obter_codigo_2fa_email(
                desde=desde,
                regex_codigo=RX_CHAVE_ACESSO,
                timeout_seg=60,
                intervalo_seg=3,
                preprocessar=_html_para_texto,
            )
        if not codigo:
            self._screenshot("2fa_codigo_nao_recebido")
            raise RuntimeError(
                "Autojur: código de verificação não chegou por email dentro "
                "do timeout. Confira a caixa do IMAP_USER e o remetente."
            )
        print(f"🔢 Autojur: código recebido ({codigo}), enviando...")
        campo.clear()
        campo.send_keys(codigo)
        if not self._submit_form("form-dupla-autenticacao"):
            self._screenshot("2fa_submit_nao_encontrado")
            raise RuntimeError("Autojur: não achei o submit da dupla autenticação.")
        self._esperar_form_sumir("form-dupla-autenticacao")
        return True

    def _tratar_derrubar_usuario(self) -> bool:
        form = self._visivel(By.ID, "form-derrubar-usuario")
        if form is None:
            return False
        print("👥 Autojur: sessão ativa em outro lugar — derrubando para entrar...")
        if not self._submit_form("form-derrubar-usuario"):
            raise RuntimeError("Autojur: não consegui confirmar derrubar a sessão.")
        self._esperar_form_sumir("form-derrubar-usuario")
        return True

    def _tratar_unidade(self) -> bool:
        form = self._visivel(By.ID, "form-unidade")
        if form is None:
            return False
        # Pode haver um select nativo ou um selectOneMenu (div) do PrimeFaces.
        try:
            sel = form.find_element(By.TAG_NAME, "select")
            from selenium.webdriver.support.ui import Select
            opcoes = [o for o in Select(sel).options if o.get_attribute("value")]
            if opcoes:
                Select(sel).select_by_value(opcoes[0].get_attribute("value"))
                print(f"🏢 Autojur: unidade selecionada: {opcoes[0].text.strip()}")
        except Exception:
            try:
                widget = form.find_element(By.CSS_SELECTOR, ".ui-selectonemenu")
                widget.click()
                item = WebDriverWait(self.driver, 5).until(
                    EC.visibility_of_element_located(
                        (By.CSS_SELECTOR, ".ui-selectonemenu-item")
                    )
                )
                print(f"🏢 Autojur: unidade selecionada: {item.text.strip()}")
                item.click()
            except Exception:
                print("🏢 Autojur: modal de unidade sem seletor aparente; confirmando direto.")
        if not self._submit_form("form-unidade"):
            raise RuntimeError("Autojur: não consegui confirmar a unidade.")
        self._esperar_form_sumir("form-unidade")
        return True

    def _tratar_motivo_login(self) -> bool:
        form = self._visivel(By.ID, "form-motivo-login")
        if form is None:
            return False
        print("📝 Autojur: pediu motivo do login; preenchendo genérico...")
        try:
            campo = form.find_element(By.TAG_NAME, "textarea")
        except Exception:
            campo = None
        if campo is not None:
            campo.clear()
            campo.send_keys("Consulta processual automatizada")
        if not self._submit_form("form-motivo-login"):
            raise RuntimeError("Autojur: não consegui confirmar o motivo do login.")
        self._esperar_form_sumir("form-motivo-login")
        return True

    # ------------------------------------------------------------------
    # LOGIN
    # ------------------------------------------------------------------
    def login(self) -> None:
        if self._logado:
            return

        usuario = (os.getenv("email_autojur") or "").strip()
        senha = (os.getenv("senha_autojur") or "").strip()
        if not usuario or not senha:
            raise RuntimeError(
                "email_autojur / senha_autojur ausentes no config/.env."
            )

        print(f"🔐 Autojur: logando como {usuario} ...")
        self.driver.get(URL_LOGIN)

        # Perfil persistente já autenticado? O Autojur redireciona para fora do
        # login.jsf e NÃO renderiza o formulário — nesse caso pulamos credenciais
        # e 2FA. Espera curta por UMA das condições (já-logado OU form presente)
        # para não travar 30s esperando um form que não vai aparecer.
        fim_check = time.time() + 8
        while time.time() < fim_check:
            if self._parece_logado():
                self._fechar_avisos()
                self._logado = True
                print("✅ Autojur: sessão reaproveitada do perfil (sem novo 2FA).")
                return
            if self._visivel(By.ID, "login-form-novo:username") is not None:
                break
            time.sleep(0.5)

        try:
            campo_user = WebDriverWait(self.driver, 30).until(
                EC.visibility_of_element_located((By.ID, "login-form-novo:username"))
            )
        except Exception:
            self._screenshot("login_form_nao_renderizou")
            raise RuntimeError("Autojur: form de login não renderizou em 30s.")

        campo_pass = self.driver.find_element(By.ID, "login-form-novo:password")
        campo_user.clear()
        campo_user.send_keys(usuario)
        campo_pass.clear()
        campo_pass.send_keys(senha)

        # Instante de referência do leitor de email — com margem, como no
        # e-SAJ, porque o Date do email pode ficar "antes" do relógio local.
        desde = datetime.now(timezone.utc) - timedelta(seconds=120)

        # Botão visível se existir; senão o submit oculto via JS.
        botao = None
        for by, val in (
            (By.CSS_SELECTOR, 'form[id="login-form-novo"] button[type="submit"]:not(.hide)'),
            (By.XPATH, '//button[normalize-space()="Entrar"]'),
            (By.XPATH, '//a[normalize-space()="Entrar"]'),
        ):
            botao = self._visivel(by, val)
            if botao is not None:
                break
        if botao is not None:
            botao.click()
        elif not self._submit_form("login-form-novo"):
            self._screenshot("login_submit_nao_encontrado")
            raise RuntimeError("Autojur: não achei como submeter o login.")
        print("➡️  Autojur: credenciais enviadas; aguardando resposta/modais...")

        # Os modais chegam via AJAX em ordem imprevisível; tratamos o que
        # aparecer até o form de login sumir da tela (ou estourar o tempo).
        # O deadline é ESTENDIDO a cada modal tratado: a espera do email de
        # verificação (Gmail encaminha com ~1-2 min de atraso) consome quase
        # todo o orçamento, e sem a extensão o loop expirava logo depois de
        # submeter o código — com o login já concluído na tela.
        fim = time.time() + 60
        while time.time() < fim:
            self._debug_estado()
            if self._parece_logado():
                self._logado = True
                self._fechar_avisos()
                print(f"✅ Autojur: login concluído ({self.driver.current_url}).")
                return

            if self._visivel(By.ID, "form-alterar-senha-obrigatorio"):
                self._screenshot("troca_senha_obrigatoria")
                raise RuntimeError(
                    "Autojur: o sistema exige TROCA DE SENHA no login. "
                    "Troque manualmente e atualize senha_autojur no .env."
                )

            tratou = self._tratar_dupla_autenticacao(desde)
            tratou = self._tratar_derrubar_usuario() or tratou
            tratou = self._tratar_unidade() or tratou
            tratou = self._tratar_motivo_login() or tratou
            if tratou:
                fim = max(fim, time.time() + 60)

            erro = self._mensagens_erro()
            if erro and ("inválid" in erro.lower() or "incorret" in erro.lower()):
                self._screenshot("login_recusado")
                raise RuntimeError(f"Autojur: login recusado — {erro}")

            time.sleep(1.5)

        erro = self._mensagens_erro()
        self._screenshot("login_timeout")
        raise RuntimeError(
            "Autojur: login não concluiu no tempo esperado."
            + (f" Mensagens na tela: {erro}" if erro else "")
        )

    def consultar_processo(self, numero_processo: str) -> Optional[str]:
        raise NotImplementedError(
            "Autojur: consulta de processos ainda não implementada."
        )

    # ------------------------------------------------------------------
    # CRIAR TAREFA DE PROTOCOLO (fluxo validado ao vivo em 2026-07-09)
    # ------------------------------------------------------------------
    def _iframe_tarefa(self):
        """Entra no iframe do diálogo de tarefa (PrimeFaces dialog framework).
        Todo o form da tarefa vive dentro de mdAdicionarTarefa.jsf."""
        self.driver.switch_to.default_content()
        frame = WebDriverWait(self.driver, 30).until(
            EC.presence_of_element_located((By.TAG_NAME, "iframe"))
        )
        self.driver.switch_to.frame(frame)

    def _bootstrap_select(self, data_id_css: str, texto: str, log=print) -> None:
        """Abre um bootstrap-select (pelo seu botão data-id) e clica no item cujo
        span.text casa `texto`. Usa o contexto atual (doc ou iframe)."""
        self._clicar_seguro(By.CSS_SELECTOR, data_id_css, timeout=15)
        self._esperar_dropdown_aberto()

        xp_exato = (
            "//div[contains(@class,'bootstrap-select') and contains(@class,'open')]"
            f"//ul[contains(@class,'dropdown-menu')]//a[.//span[normalize-space()=\"{texto}\"]]"
        )
        xp_contem = (
            f"//ul[contains(@class,'dropdown-menu')]//a[contains(normalize-space(.),\"{texto}\")]"
        )

        def achar():
            itens = self.driver.find_elements(By.XPATH, xp_exato)
            return itens or self.driver.find_elements(By.XPATH, xp_contem)

        # Reencontra e clica na mesma passada: o dropdown do bootstrap-select é
        # remontado quando a lista termina de popular.
        self._clicar_item(
            achar, lambda _txt, _el: True,
            descricao=f"opção '{texto}' não encontrada no seletor",
        )
        self._esperar_dropdown_fechado()

    def _selecionar_lote(self, lote: str, log=print) -> None:
        """Lote = 'iniciais' | 'urgente' | 'dia_seguinte'. As opções trazem a
        data do dia; casamos por sufixo (INICIAIS/URGENTE) ou, para dia_seguinte,
        a opção que NÃO tem sufixo (a data de amanhã)."""
        self._clicar_seguro(By.CSS_SELECTOR, SEL_LOTE, timeout=15)
        self._esperar_dropdown_aberto()

        xp = (
            "//div[contains(@class,'bootstrap-select') and contains(@class,'open')]"
            "//ul[contains(@class,'dropdown-menu')]//li/a"
        )
        # Espera a lista de lotes parar de crescer antes de ler os rótulos: as
        # opções trazem a data do dia e chegam por AJAX.
        self._esperar_lista_estavel(By.XPATH, xp)

        def casa(texto: str, _el) -> bool:
            up = texto.upper()
            if not up or up.startswith("SELECIONE"):
                return False
            if lote == "iniciais":
                return "INICIAIS" in up
            if lote == "urgente":
                return "URGENTE" in up
            # dia_seguinte: a opção sem sufixo (a data de amanhã).
            return "INICIAIS" not in up and "URGENTE" not in up

        escolhido = self._clicar_item(
            lambda: self.driver.find_elements(By.XPATH, xp), casa,
            descricao=f"não achei a opção de Lote para '{lote}'",
            # `ultimo=True` preserva o comportamento original: o laço não tinha
            # `break`, então a ÚLTIMA opção que casava vencia. Muda o resultado
            # quando há mais de uma sem sufixo (caso 'dia_seguinte').
            ultimo=True,
        )
        log(f"🗂️  Lote: {escolhido}")
        self._esperar_dropdown_fechado()

    def _selecionar_evento(self, evento: str, log=print) -> None:
        """Autocomplete do Evento: digita `evento` e clica na linha cujo texto-base
        casa exatamente e que tem o selo 'T' (span.label-success = é tarefa)."""
        ev = self.driver.find_element(By.ID, SEL_EVENTO_INPUT)
        ev.click()
        ev.clear()
        for ch in evento:
            ev.send_keys(ch)
            time.sleep(0.12)

        # O id do painel tem ':' (escapar no CSS é frágil): busca via ID direto.
        WebDriverWait(self.driver, 15).until(
            lambda d: d.find_element(By.ID, SEL_EVENTO_PANEL).is_displayed()
            and d.find_element(By.ID, SEL_EVENTO_PANEL).find_elements(
                By.CSS_SELECTOR, "tr.ui-autocomplete-item")
        )

        def achar_linhas():
            painel = self.driver.find_element(By.ID, SEL_EVENTO_PANEL)
            return painel.find_elements(By.CSS_SELECTOR, "tr.ui-autocomplete-item")

        # A espera acima devolve o controle na PRIMEIRA sugestão; o PrimeFaces
        # troca o painel inteiro conforme o resto chega. Ler as linhas antes disso
        # dava StaleElementReferenceException no meio do laço.
        self._esperar_lista_estavel(By.CSS_SELECTOR, "tr.ui-autocomplete-item")

        def casa(texto: str, el) -> bool:
            # O selo 'T' (span.label-success) marca as linhas que são TAREFA; o
            # texto da linha termina com esse 'T', então tiramos antes de comparar.
            base = texto[:-1].strip() if texto.endswith("T") else texto
            if base.lower() != evento.lower():
                return False
            return bool(el.find_elements(By.CSS_SELECTOR, "span.label-success"))

        def achar_clicaveis():
            """A célula é o alvo do clique; cai na própria linha se não houver td."""
            alvos = []
            for ln in achar_linhas():
                tds = ln.find_elements(By.TAG_NAME, "td")
                alvos.append(_LinhaEvento(ln, tds[0] if tds else ln))
            return alvos

        self._clicar_item(
            achar_clicaveis, casa,
            descricao=f"evento '{evento}' (com selo T) não encontrado",
        )
        time.sleep(1.5)  # a seleção dispara auto-preenchimento (Responsável/prazos)

    def buscar_pasta(self, numero_processo: str, log=print) -> list[dict]:
        """Passos 1–2: garante a versão clássica, busca o processo (Todos/Todos)
        e RETORNA os resultados SEM abrir nenhum. Cada item:
        {'indice', 'texto', 'cnj', 'pasta', 'assunto'}.

        A busca pode trazer >1 linha (ex.: o processo + subpastas como um agravo).
        Quem chama decide qual abrir (a GUI mostra um modal de seleção)."""
        self.driver.switch_to.default_content()
        self.driver.get(URL_PROCESSOS)
        WebDriverWait(self.driver, 30).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )

        # 1.1: desliga o toggle "Nova versão" se estiver ativo (fa-check-square).
        toggles = self.driver.find_elements(By.CSS_SELECTOR, SEL_TOGGLE_NOVA_VERSAO)
        if toggles:
            ic = toggles[0].find_elements(By.TAG_NAME, "i")
            if ic and "fa-check-square" in (ic[0].get_attribute("class") or ""):
                log("🔀 Autojur: desativando 'Nova versão' (usa a versão clássica)...")
                toggles[0].click()
                WebDriverWait(self.driver, 30).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )

        # 2: número + Tipo de Pasta "Todos" + Status "Todos" + pesquisar.
        campo = WebDriverWait(self.driver, 20).until(
            EC.visibility_of_element_located((By.ID, SEL_BUSCA_INPUT))
        )
        campo.clear()
        campo.send_keys(numero_processo)
        self._bootstrap_select(SEL_TIPO_PASTA, "Todos", log=log)
        # Escolher "Tipo de Pasta" dispara AJAX e o spinner cobre a tela: este
        # clique era interceptado pelo div.google-spin-wrapper.
        self._clicar_seguro(By.CSS_SELECTOR, SEL_STATUS_TODOS)
        log(f"🔎 Autojur: pesquisando {numero_processo} ...")
        self._clicar_seguro(By.ID, SEL_BUSCA_LUPA)

        def _tem_resultado(d):
            linhas = d.find_elements(By.CSS_SELECTOR, SEL_LINHAS_RESULTADO)
            if not linhas:
                return False
            t = linhas[0].text.strip().lower()
            return bool(t) and "utilize os filtros" not in t

        WebDriverWait(self.driver, 40).until(_tem_resultado)
        # `_tem_resultado` satisfaz-se com a PRIMEIRA linha; o datatable do
        # PrimeFaces continua montando as demais e troca o corpo da tabela. Ler
        # `.text` de cada linha nesse meio dava StaleElementReferenceException.
        self._esperar_lista_estavel(By.CSS_SELECTOR, SEL_LINHAS_RESULTADO)

        def ler_resultados() -> list[dict]:
            itens = []
            for i, ln in enumerate(
                self.driver.find_elements(By.CSS_SELECTOR, SEL_LINHAS_RESULTADO)
            ):
                txt = ln.text.strip()
                itens.append({
                    "indice": i,
                    "texto": " | ".join(l.strip() for l in txt.splitlines() if l.strip()),
                    "cnj": self._extrair_rotulo(txt, ("CNJ", "Processo")),
                    "pasta": self._extrair_rotulo(txt, ("Pasta / Subpasta", "Pasta/Subpasta")),
                    "assunto": self._extrair_rotulo(txt, ("Assunto/Resumo", "Assunto / Resumo")),
                })
            return itens

        resultados = self._sem_stale(ler_resultados)
        log(f"📁 Autojur: {len(resultados)} resultado(s) para {numero_processo}.")
        return resultados

    @staticmethod
    def _extrair_rotulo(texto: str, rotulos: tuple) -> str:
        """Do texto de uma linha (rótulos+valores em linhas separadas), extrai o
        valor que segue um dos `rotulos` (ex.: 'CNJ5016622-...' -> '5016622-...')."""
        for linha in texto.splitlines():
            l = linha.strip()
            for r in rotulos:
                if l.startswith(r):
                    return l[len(r):].strip()
        return ""

    def _abrir_resultado(self, indice: int, log=print) -> None:
        """Abre a linha `indice` do resultado (duplo clique) e clica em Adicionar
        Tarefa, deixando o iframe do formulário pronto. Passo 3."""
        self._esperar_lista_estavel(By.CSS_SELECTOR, SEL_LINHAS_RESULTADO)

        def abrir():
            # Reencontra a linha DENTRO da tentativa: entre a leitura do texto e o
            # duplo clique o datatable pode ser remontado.
            linhas = self.driver.find_elements(By.CSS_SELECTOR, SEL_LINHAS_RESULTADO)
            if indice < 0 or indice >= len(linhas):
                raise IndexError(
                    f"Autojur: índice de resultado {indice} fora do intervalo."
                )
            alvo = linhas[indice]
            rotulo = alvo.text[:80].strip()
            ActionChains(self.driver).double_click(alvo).perform()
            return rotulo

        log(f"📂 Autojur: abrindo pasta — {self._sem_stale(abrir)}")
        WebDriverWait(self.driver, 30).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )

        # ESPERA CRÍTICA (medida ao vivo): sem uma pasta selecionada o botão
        # "Adicionar Tarefa" existe mas tem a classe `ui-state-disabled`. O duplo
        # clique acima dispara o AJAX que o habilita — e `readyState == complete`
        # NÃO espera esse AJAX. Clicar antes disso não faz nada: um clique por JS
        # num commandLink desabilitado do PrimeFaces é ignorado em silêncio, e o
        # sintoma aparecia longe daqui, como timeout esperando o iframe.
        self._esperar_sem_overlay()
        try:
            WebDriverWait(self.driver, 30).until(
                lambda d: "ui-state-disabled" not in (
                    d.find_element(By.ID, SEL_BTN_ADD_TAREFA).get_attribute("class") or ""
                )
            )
        except Exception:
            self._screenshot("add_tarefa_continuou_desabilitado")
            raise RuntimeError(
                "Autojur: o botão 'Adicionar Tarefa' continuou desabilitado — a "
                "pasta não foi selecionada pelo duplo clique."
            )

        # Clique via JS — o tooltip do botão intercepta o clique normal (visto ao
        # vivo: ElementClickIntercepted por .tooltipster).
        btn = self.driver.find_element(By.ID, SEL_BTN_ADD_TAREFA)
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        self.driver.execute_script("arguments[0].click();", btn)
        try:
            WebDriverWait(self.driver, 30).until(
                EC.presence_of_element_located((By.TAG_NAME, "iframe"))
            )
        except Exception:
            # Sem isto, o ponto mais provável de falha do fluxo não deixava
            # nenhum artefato para diagnosticar.
            self._screenshot("iframe_tarefa_nao_apareceu")
            raise
        # O iframe EXISTIR não quer dizer que o documento dentro dele carregou —
        # era isso que o `time.sleep(2)` daqui tentava cobrir. Esperar o
        # readyState interno é a condição real e normalmente resolve em bem menos
        # de 2 s (e em mais, quando o servidor está lento, em vez de seguir cedo).
        WebDriverWait(self.driver, 30).until(
            lambda d: d.execute_script(
                "const f = document.querySelector('iframe');"
                "try {"
                "  return !!f && !!f.contentDocument"
                "    && f.contentDocument.readyState === 'complete';"
                "} catch (e) { return true; }"  # cross-origin: não dá p/ inspecionar
            )
        )

    def criar_tarefa_protocolo(
        self,
        numero_processo: str,
        conteudo: str,
        local: str = "",
        lote: str = "dia_seguinte",
        pdf_path: Optional[str] = None,
        pdf_paths: Optional[list] = None,
        requer_protocolo: bool = True,
        salvar: bool = False,
        tipo_evento: str = TIPO_EVENTO_PADRAO,
        evento: str = EVENTO_PADRAO,
        tipo_documento: str = TIPO_DOC_PADRAO,
        escolher_resultado: Optional[Callable[[list], Optional[int]]] = None,
        log: Callable[[str], None] = print,
    ) -> None:
        """Cria UMA tarefa de Protocolo no processo, anexando o(s) PDF(s).

        Reproduz o fluxo validado ao vivo (11 passos). Requer login() feito.
        Com `salvar=False` (padrão) deixa o formulário preenchido e ABERTO para
        conferência humana; com `salvar=True` clica em "Salvar" ao final.

        Anexos: quando há MAIS DE UM arquivo, todos entram na MESMA tarefa —
        o passo de anexo é repetido por arquivo (NÃO se cria uma tarefa por
        PDF). Passe `pdf_paths` (lista) para múltiplos; `pdf_path` (str) é
        aceito para um único (compatibilidade).

        lote: 'iniciais' | 'urgente' | 'dia_seguinte'.

        Quando a busca traz >1 resultado (ex.: processo + subpasta de agravo),
        chama `escolher_resultado(resultados) -> índice` para o chamador decidir
        qual abrir (a GUI mostra um modal). Se não houver callback e vier >1,
        levanta erro (não adivinha). Com 1 resultado, abre direto.
        """
        if not self._logado:
            raise RuntimeError("Autojur: faça login() antes de criar a tarefa.")
        if lote not in LOTES:
            raise ValueError(f"lote deve ser um de {LOTES}, não {lote!r}.")
        if not conteudo or not conteudo.strip():
            raise ValueError("Autojur: 'conteudo' é obrigatório.")
        # Normaliza a lista de anexos (aceita `pdf_paths` lista OU `pdf_path`
        # único). Todos vão para a MESMA tarefa.
        pdfs: list[str] = []
        if pdf_paths:
            pdfs = [str(x) for x in pdf_paths if x]
        elif pdf_path:
            pdfs = [str(pdf_path)]
        for pp in pdfs:
            p = Path(pp)
            if not p.is_file():
                raise FileNotFoundError(f"PDF não encontrado: {pp}")
            if p.suffix.lower() != ".pdf":
                raise ValueError(f"O anexo deve ser PDF: {pp}")

        log(f"📝 Autojur: criando tarefa de {evento} em {numero_processo} ...")
        # Passos 1–2: busca (sem abrir).
        resultados = self.buscar_pasta(numero_processo, log=log)
        if not resultados:
            raise RuntimeError(f"Autojur: processo {numero_processo} não retornou resultados.")
        if len(resultados) == 1:
            indice = 0
        elif escolher_resultado is not None:
            indice = escolher_resultado(resultados)
            if indice is None:
                raise RuntimeError("Autojur: seleção de resultado cancelada.")
        else:
            resumo = "; ".join(f"[{r['indice']}] {r['pasta']} {r['cnj']}" for r in resultados)
            raise RuntimeError(
                f"Autojur: {len(resultados)} resultados para {numero_processo} "
                f"— informe qual abrir via 'escolher_resultado'. Opções: {resumo}"
            )
        # Passo 3: abre o resultado escolhido e o diálogo (deixa o iframe pronto).
        self._abrir_resultado(indice, log=log)

        # Passos 4–11 (dentro do iframe).
        self._iframe_tarefa()
        try:
            log(f"   • Tipo Evento: {tipo_evento}")
            self._bootstrap_select(SEL_TIPO_EVENTO, tipo_evento, log=log)

            log(f"   • Evento: {evento}")
            self._selecionar_evento(evento, log=log)

            if local:
                el = self.driver.find_element(By.ID, SEL_LOCAL_INPUT)
                el.click()
                el.clear()
                el.send_keys(local)
                log(f"   • Local: {local}")

            ta = self.driver.find_element(By.ID, SEL_CONTEUDO)
            ta.click()
            ta.clear()
            ta.send_keys(conteudo)
            self.driver.execute_script("arguments[0].blur();", ta)
            time.sleep(1)
            log("   • Conteúdo preenchido")

            self._selecionar_lote(lote, log=log)

            log(f"   • Tipo de Documento: {tipo_documento}")
            self._bootstrap_select(SEL_TIPO_DOC, tipo_documento, log=log)

            if pdfs:
                # Anexa TODOS os PDFs na mesma tarefa, repetindo o passo de
                # upload por arquivo (o tipo de documento já foi escolhido acima).
                for i, pp in enumerate(pdfs, 1):
                    log(f"   • Anexando PDF ({i}/{len(pdfs)}): {Path(pp).name}")
                    # Escolher o "Tipo de Documento" dispara AJAX que (re)monta o
                    # painel do GED: sem esperar, o input de upload ainda não
                    # existe e o find_element estourava NoSuchElement.
                    inp = self._esperar_input_upload()
                    self.driver.execute_script(
                        "arguments[0].style.display='block';"
                        "arguments[0].style.visibility='visible';"
                        "arguments[0].style.opacity=1;"
                        "arguments[0].removeAttribute('hidden');", inp
                    )
                    inp.send_keys(str(Path(pp).resolve()))

                    nome_base = Path(pp).stem.lower()[:12]

                    def _subiu(d, nb=nome_base):
                        txt = d.find_element(By.TAG_NAME, "body").text.lower()
                        return nb in txt and "nenhum arquivo encontrado" not in txt

                    WebDriverWait(self.driver, 60).until(_subiu)
                    time.sleep(1.5)
                    log("   • PDF anexado")

                if requer_protocolo:
                    # Marca "Requer Protocolo" de CADA documento anexado. O botão
                    # por linha (SEL_BTN_PROTOCOLO_GED) alterna o ícone
                    # fa-square -> fa-check-square e dispara AJAX que re-renderiza
                    # a lista (o title some). Por isso: identificamos os botões
                    # pelo id, checamos o estado pelo ÍCONE e clicamos só os ainda
                    # NÃO marcados — re-consultando a cada passo (idempotente).
                    def _botoes_protocolo():
                        painel = self.driver.find_element(By.ID, SEL_PAINEL_GED)
                        return painel.find_elements(
                            By.CSS_SELECTOR, SEL_BTN_PROTOCOLO_GED)

                    def _pendentes():
                        pend = []
                        for b in _botoes_protocolo():
                            try:
                                html = b.get_attribute("innerHTML") or ""
                            except StaleElementReferenceException:
                                continue
                            if "fa-check-square" not in html:
                                pend.append(b)
                        return pend

                    total_docs = len(_botoes_protocolo())
                    if total_docs == 0:
                        raise RuntimeError("Autojur: botão 'Requer Protocolo' não encontrado.")
                    for _ in range(total_docs * 3):  # limite de segurança
                        pend = _pendentes()
                        if not pend:
                            break
                        n_pend = len(pend)
                        try:
                            self.driver.execute_script(
                                "arguments[0].scrollIntoView({block:'center'});", pend[0])
                            pend[0].click()
                        except StaleElementReferenceException:
                            continue
                        # Espera o ícone virar check-square (nº de pendentes cai).
                        try:
                            WebDriverWait(self.driver, 10).until(
                                lambda d: len(_pendentes()) < n_pend)
                        except Exception:
                            time.sleep(1.0)
                    restantes = len(_pendentes())
                    log(f"   • 'Requer Protocolo' marcado "
                        f"({total_docs - restantes}/{total_docs} doc(s))")
            elif requer_protocolo:
                log("   ⚠️ requer_protocolo ignorado (nenhum PDF anexado).")

            if salvar:
                botao = None
                for by, sel in (
                    (By.XPATH, "//button[normalize-space()='Salvar']"),
                    (By.XPATH, "//button[contains(normalize-space(),'Salvar') and not(contains(.,'Criar')) and not(contains(.,'Visualizar'))]"),
                ):
                    els = [e for e in self.driver.find_elements(by, sel) if e.is_displayed()]
                    if els:
                        botao = els[0]
                        break
                if botao is None:
                    raise RuntimeError("Autojur: botão 'Salvar' não encontrado.")
                botao.click()
                time.sleep(3)
                log("✅ Autojur: tarefa salva.")
            else:
                log("✅ Autojur: tarefa PREENCHIDA (não salva — salvar=False).")
        finally:
            # Sai do iframe para não deixar o driver preso nele.
            self.driver.switch_to.default_content()


if __name__ == "__main__":
    # Teste ao vivo:  python -m organizador.integracoes.tjsp_scraper.scrapers.autojur
    from dotenv import load_dotenv
    from organizador.config import CONFIG_ENV_PATH

    load_dotenv(CONFIG_ENV_PATH)
    scraper = AutojurScraper(headless=False)
    try:
        scraper.login()
        print("URL final:", scraper.driver.current_url)
        time.sleep(5)
    finally:
        scraper.fechar()
