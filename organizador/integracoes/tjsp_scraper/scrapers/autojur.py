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
from selenium.common.exceptions import StaleElementReferenceException

from .base import TribunalScraper, criar_chrome_driver
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

_DEBUG_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "_debug"


class AutojurScraper(TribunalScraper):

    def __init__(self, headless: bool = False, user_data_dir: Optional[str] = None):
        super().__init__(estado="", url=URL_LOGIN, headless=headless)
        self.driver = criar_chrome_driver(headless=headless, user_data_dir=user_data_dir)
        self.wait = WebDriverWait(self.driver, 20)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _screenshot(self, nome: str):
        try:
            _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            caminho = _DEBUG_DIR / f"autojur_{nome}_{ts}.png"
            self.driver.save_screenshot(str(caminho))
            print(f"🖼️  Screenshot de debug salvo: {caminho}")
        except Exception:
            pass

    def _visivel(self, by, valor):
        """Elemento presente E visível, ou None (sem espera)."""
        try:
            el = self.driver.find_element(by, valor)
            return el if el.is_displayed() else None
        except Exception:
            return None

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
        time.sleep(2)
        return True

    def _tratar_derrubar_usuario(self) -> bool:
        form = self._visivel(By.ID, "form-derrubar-usuario")
        if form is None:
            return False
        print("👥 Autojur: sessão ativa em outro lugar — derrubando para entrar...")
        if not self._submit_form("form-derrubar-usuario"):
            raise RuntimeError("Autojur: não consegui confirmar derrubar a sessão.")
        time.sleep(2)
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
        time.sleep(2)
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
        time.sleep(2)
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
        btn = WebDriverWait(self.driver, 15).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, data_id_css))
        )
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        btn.click()
        time.sleep(0.8)
        itens = self.driver.find_elements(
            By.XPATH,
            "//div[contains(@class,'bootstrap-select') and contains(@class,'open')]"
            f"//ul[contains(@class,'dropdown-menu')]//a[.//span[normalize-space()=\"{texto}\"]]",
        )
        if not itens:
            itens = self.driver.find_elements(
                By.XPATH,
                f"//ul[contains(@class,'dropdown-menu')]//a[contains(normalize-space(.),\"{texto}\")]",
            )
        if not itens:
            raise RuntimeError(f"Autojur: opção '{texto}' não encontrada no seletor.")
        itens[0].click()
        time.sleep(1.2)

    def _selecionar_lote(self, lote: str, log=print) -> None:
        """Lote = 'iniciais' | 'urgente' | 'dia_seguinte'. As opções trazem a
        data do dia; casamos por sufixo (INICIAIS/URGENTE) ou, para dia_seguinte,
        a opção que NÃO tem sufixo (a data de amanhã)."""
        btn = WebDriverWait(self.driver, 15).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, SEL_LOTE))
        )
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        btn.click()
        time.sleep(0.8)
        itens = self.driver.find_elements(
            By.XPATH,
            "//div[contains(@class,'bootstrap-select') and contains(@class,'open')]"
            "//ul[contains(@class,'dropdown-menu')]//li/a",
        )
        escolhido = None
        for a in itens:
            up = a.text.strip().upper()
            if not up or up.startswith("SELECIONE"):
                continue
            if lote == "iniciais" and "INICIAIS" in up:
                escolhido = a
            elif lote == "urgente" and "URGENTE" in up:
                escolhido = a
            elif lote == "dia_seguinte" and "INICIAIS" not in up and "URGENTE" not in up:
                escolhido = a
        if escolhido is None:
            raise RuntimeError(f"Autojur: não achei a opção de Lote para '{lote}'.")
        log(f"🗂️  Lote: {escolhido.text.strip()}")
        escolhido.click()
        time.sleep(1.2)

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
        time.sleep(0.4)
        painel = self.driver.find_element(By.ID, SEL_EVENTO_PANEL)
        linhas = painel.find_elements(By.CSS_SELECTOR, "tr.ui-autocomplete-item")
        escolhido = None
        for ln in linhas:
            txt = ln.text.strip()
            base = txt[:-1].strip() if txt.endswith("T") else txt
            tem_T = bool(ln.find_elements(By.CSS_SELECTOR, "span.label-success"))
            if base.lower() == evento.lower() and tem_T:
                escolhido = ln
                break
        if escolhido is None:
            raise RuntimeError(f"Autojur: evento '{evento}' (com selo T) não encontrado.")
        tds = escolhido.find_elements(By.TAG_NAME, "td")
        (tds[0] if tds else escolhido).click()
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
        time.sleep(2)

        # 1.1: desliga o toggle "Nova versão" se estiver ativo (fa-check-square).
        toggles = self.driver.find_elements(By.CSS_SELECTOR, SEL_TOGGLE_NOVA_VERSAO)
        if toggles:
            ic = toggles[0].find_elements(By.TAG_NAME, "i")
            if ic and "fa-check-square" in (ic[0].get_attribute("class") or ""):
                log("🔀 Autojur: desativando 'Nova versão' (usa a versão clássica)...")
                toggles[0].click()
                time.sleep(2)
                WebDriverWait(self.driver, 30).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
                time.sleep(2)

        # 2: número + Tipo de Pasta "Todos" + Status "Todos" + pesquisar.
        campo = WebDriverWait(self.driver, 20).until(
            EC.visibility_of_element_located((By.ID, SEL_BUSCA_INPUT))
        )
        campo.clear()
        campo.send_keys(numero_processo)
        self._bootstrap_select(SEL_TIPO_PASTA, "Todos", log=log)
        self.driver.find_element(By.CSS_SELECTOR, SEL_STATUS_TODOS).click()
        time.sleep(0.5)
        log(f"🔎 Autojur: pesquisando {numero_processo} ...")
        self.driver.find_element(By.ID, SEL_BUSCA_LUPA).click()

        def _tem_resultado(d):
            linhas = d.find_elements(By.CSS_SELECTOR, SEL_LINHAS_RESULTADO)
            if not linhas:
                return False
            t = linhas[0].text.strip().lower()
            return bool(t) and "utilize os filtros" not in t

        WebDriverWait(self.driver, 40).until(_tem_resultado)
        time.sleep(1.5)
        linhas = self.driver.find_elements(By.CSS_SELECTOR, SEL_LINHAS_RESULTADO)
        resultados = []
        for i, ln in enumerate(linhas):
            txt = ln.text.strip()
            resultados.append({
                "indice": i,
                "texto": " | ".join(l.strip() for l in txt.splitlines() if l.strip()),
                "cnj": self._extrair_rotulo(txt, ("CNJ", "Processo")),
                "pasta": self._extrair_rotulo(txt, ("Pasta / Subpasta", "Pasta/Subpasta")),
                "assunto": self._extrair_rotulo(txt, ("Assunto/Resumo", "Assunto / Resumo")),
            })
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
        linhas = self.driver.find_elements(By.CSS_SELECTOR, SEL_LINHAS_RESULTADO)
        if indice < 0 or indice >= len(linhas):
            raise IndexError(f"Autojur: índice de resultado {indice} fora do intervalo.")
        alvo = linhas[indice]
        log(f"📂 Autojur: abrindo pasta — {alvo.text[:80].strip()}")
        ActionChains(self.driver).double_click(alvo).perform()
        time.sleep(3)
        WebDriverWait(self.driver, 30).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )

        # Adicionar Tarefa: clique via JS — o tooltip do botão intercepta o
        # clique normal (visto ao vivo: ElementClickIntercepted por .tooltipster).
        btn = WebDriverWait(self.driver, 20).until(
            EC.presence_of_element_located((By.ID, SEL_BTN_ADD_TAREFA))
        )
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        self.driver.execute_script("arguments[0].click();", btn)
        WebDriverWait(self.driver, 30).until(
            EC.presence_of_element_located((By.TAG_NAME, "iframe"))
        )
        time.sleep(2)

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
                    inp = self.driver.find_element(By.ID, SEL_UPLOAD_INPUT)
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
