# src/scrapers/pje.py
"""
Scraper PJe TJMG — 1º grau, com login Keycloak + 2FA TOTP (TOTP_PDPJ).

Fluxo confirmado por 13 prints (docs/sites_spec.md):
  login.seam -> Keycloak (sso.cloud.pje.jus.br, client pje-tjmg-1g)
  -> CPF/senha -> TOTP (app autenticador) -> Quadro de avisos
  -> Consulta processos (nº em 6 campos) -> resultado -> autos digitais
  -> timeline (esquerda) + ABRIR TODOS os documentos (decisão do usuário)

IDs CONFIRMADOS (diagnóstico read-only do Keycloak):
  CPF=#username  Senha=#password  Entrar=#kc-login

IDs NÃO confirmados (atrás do login — JSF/RichFaces, ids gerados):
  tela TOTP, form de busca (6 campos), timeline, navegador de documentos.
  → uso fallbacks + dump de DOM em falha; o 1º teste ao vivo revela os reais.
"""

import html
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from unidecode import unidecode

from .base import TribunalScraper, criar_chrome_driver
from .popup import AtribuicaoDetectadaError, detectar_atribuicao
from ..credenciais import obter_credenciais
from ..two_factor import gerar_codigo_totp

# Extrai o ID do documento no início do texto do span (ex: "10554398891 - Despacho")
RX_ID_DOC_PJE = re.compile(r"^\s*(\d+)\s*[-—]")

# JS que sobe do span até .media.interno e devolve a DATA do grupo (header
# 06 out 2025) imediatamente antes desse evento. Estrutura confirmada via
# src/teste_pje_timeline_dom_v2.py — o header é <div class="media data"> com
# um <span class="data-interna"> dentro.
_JS_DATA_GRUPO = """
var span = arguments[0];
if (!span) return '';
var media = span.closest('.media.interno');
if (!media) return '';
var sib = media.previousElementSibling;
while (sib) {
    if (sib.classList && sib.classList.contains('media') && sib.classList.contains('data')) {
        var el = sib.querySelector('.data-interna');
        return (el ? el.textContent : sib.innerText || '').trim();
    }
    sib = sib.previousElementSibling;
}
return '';
"""

# Rótulos da timeline que disparam a abertura do documento.
# Medição empírica (2026-05-19): abrir TODOS os 98 docs custou ~11 min e
# não acrescentou nada além da timeline (PDFs são imagem sem texto).
# Estes termos são onde penhora/bloqueio efetivamente aparece.
TRIGGER_DOC = re.compile(
    r"\b(SISBAJUD|BACENJUD|RENAJUD|DESPACHO|DECISAO|SENTENCA|"
    r"ALVARA|LEVANTAMENTO|PENHORA|BLOQUEIO)\b"
)

# Teto de documentos a abrir: só uma guarda contra loop. Na prática o loop para
# bem antes, assim que aparece o CABEÇALHO (ver _cabecalho_suficiente). Nunca
# abre mais do que o comportamento antigo (que abria todos os candidatos).
_MAX_DOCS_ABRIR = 30


def _cabecalho_suficiente(blob: str) -> bool:
    """Já abrimos o documento com o CABEÇALHO do processo? Sinal: linha 'CLASSE:'
    (timbre TJCE) OU 'classe solta' "[CÍVEL] ... (156)" (timbre TJMG). Achar a
    classe = pegamos o cabeçalho → dá para parar de abrir documentos."""
    blob = blob or ""
    from ..parser_pje import _RX_CLASSE, _RX_CLASSE_SOLTA
    if _RX_CLASSE.search(blob):
        return True
    return any(_RX_CLASSE_SOLTA.match(linha) for linha in blob.splitlines())

# PJe é nacional (login único via PDPJ/Keycloak em sso.cloud.pje.jus.br): o
# código do app autenticador (TOTP) é o MESMO em todos os tribunais — muda só
# a URL base do PJe e a chave de credencial. Por isso o TOTP fica fixo aqui e
# a URL/credencial são parametrizadas por estado (ver _CRED_POR_ESTADO e a URL
# vinda de TJ_SISTEMAS, repassada ao __init__).
TOTP_ENV = "TOTP_PDPJ"

# estado (forma usada em TJ_SISTEMAS) -> chave de credencial em credenciais.json.
# Sem entrada aqui, cai no TJMG (compatibilidade).
_CRED_POR_ESTADO = {
    "MINAS GERAIS": "TJMG - PJE",
    "CEARA":        "TJCE - PJE - 1º Grau",
    "MARANHAO":     "TJMA – PJE – 1º Grau",
    "PARA":         "TJPA - PJE – 1º Grau",
    "PERNAMBUCO":   "TJPE - PJE – 1º Grau",
}

# Defaults TJMG (fallback quando a url não é informada).
LOGIN_URL = "https://pje.tjmg.jus.br/pje/login.seam"
BUSCA_URL = "https://pje.tjmg.jus.br/pje/Processo/ConsultaProcesso/listView.seam"

# CNJ: NNNNNNN-DD.AAAA.J.TR.OOOO  → 6 campos do form do PJe
RX_CNJ_PJE = re.compile(r"(\d{7})-?(\d{2})\.?(\d{4})\.?(\d)\.?(\d{2})\.?(\d{4})")

_DEBUG_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "_debug"


class PJEScraper(TribunalScraper):

    def __init__(self, estado: str, url: str, headless: bool = False):
        super().__init__(estado=estado, url=url, headless=headless)
        # Deriva as URLs do PJe a partir da base vinda de TJ_SISTEMAS
        # (ex.: "https://pje.tjmg.jus.br/pje/"). Sem url, usa o TJMG.
        base = url or "https://pje.tjmg.jus.br/pje/"
        if not base.endswith("/"):
            base += "/"
        self.login_url = urljoin(base, "login.seam")
        self.busca_url = urljoin(base, "Processo/ConsultaProcesso/listView.seam")
        self.host = urlparse(base).netloc  # ex.: "pje.tjmg.jus.br"
        self.tj_chave = _CRED_POR_ESTADO.get(estado, "TJMG - PJE")
        self.driver = criar_chrome_driver(headless=headless)
        self.wait = WebDriverWait(self.driver, 30)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _first(self, locators, timeout=10):
        for by, val in locators:
            try:
                el = WebDriverWait(self.driver, timeout).until(
                    EC.presence_of_element_located((by, val))
                )
                if el.is_displayed():
                    return el
            except Exception:
                continue
        return None

    @staticmethod
    def _xp_campo_numero(sufixo: str) -> str:
        """XPath de um campo do número do processo casando o SUFIXO do id SEM
        diferenciar maiúsc./minúsc. O PJe varia a caixa entre tribunais (TJMG usa
        'fPP:numeroProcesso:Ano'; o TJPA usa ':ano') — fixar o id quebrava o TJPA."""
        minus, maius = "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        return (
            "//input[starts-with(@id,'fPP:numeroProcesso:') and "
            f"translate(substring-after(@id,'fPP:numeroProcesso:'),'{maius}','{minus}')"
            f"='{sufixo.lower()}']"
        )

    def _campo_numero(self, sufixo: str, timeout: int = 8):
        """Acha o campo do número pelo sufixo do id (case-insensitive)."""
        return self._first([(By.XPATH, self._xp_campo_numero(sufixo))], timeout=timeout)

    def _screenshot(self, nome: str):
        try:
            _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            caminho = _DEBUG_DIR / f"pje_{nome}_{ts}.png"
            self.driver.save_screenshot(str(caminho))
            print(f"🖼️  Screenshot de debug: {caminho}")
        except Exception:
            pass

    def _dump_dom(self, contexto: str):
        """Imprime inputs/botões visíveis — acelera achar os ids reais."""
        print(f"\n--- DOM DUMP ({contexto}) | url={self.driver.current_url} ---")
        try:
            for el in self.driver.find_elements(By.TAG_NAME, "input"):
                if el.is_displayed():
                    print("INPUT", {
                        "id": el.get_attribute("id"),
                        "name": el.get_attribute("name"),
                        "type": el.get_attribute("type"),
                        "placeholder": el.get_attribute("placeholder"),
                    })
            for el in self.driver.find_elements(By.CSS_SELECTOR, "button, a.btn, input[type=submit]"):
                if el.is_displayed():
                    print("BTN", {
                        "id": el.get_attribute("id"),
                        "texto": (el.text or el.get_attribute("value") or "").strip()[:40],
                    })
        except Exception as e:
            print(f"(falha no dump: {e})")
        print("--- fim do dump ---\n")

    @staticmethod
    def _cpf_digits(login: str) -> str:
        """PJe usa CPF só com dígitos, com zero à esquerda (11 dígitos)."""
        return re.sub(r"\D", "", str(login or "")).zfill(11)

    # ------------------------------------------------------------------
    # LOGIN (Keycloak + TOTP)
    # ------------------------------------------------------------------
    def login(self) -> None:
        if self._logado:
            return

        login_raw, senha = obter_credenciais(self.tj_chave)
        cpf = self._cpf_digits(login_raw)
        print(f"🔐 PJe ({self.host}): logando (CPF {cpf}) ...")

        self.driver.get(self.login_url)

        # Keycloak — ids CONFIRMADOS por diagnóstico
        try:
            WebDriverWait(self.driver, 40).until(
                EC.visibility_of_element_located((By.ID, "password"))
            )
        except Exception:
            self._screenshot("login_keycloak_nao_renderizou")
            raise RuntimeError("PJe: tela de login (Keycloak) não renderizou.")

        self.driver.find_element(By.ID, "username").send_keys(cpf)
        self.driver.find_element(By.ID, "password").send_keys(senha)
        self.driver.find_element(By.ID, "kc-login").click()
        print("➡️  PJe: credenciais enviadas, aguardando TOTP...")

        # Tela TOTP — "Entre no seu aplicativo de autenticação..."
        # ids não confirmados → fallback por contexto + dump se falhar.
        campo_totp = self._first(
            [
                (By.ID, "otp"),
                (By.NAME, "otp"),
                (By.XPATH, '//input[contains(@id,"otp") or contains(@name,"otp")]'),
                (By.XPATH, '//label[contains(.,"código")]/following::input[1]'),
                (By.XPATH, '//input[@type="text" and not(@hidden)]'),
            ],
            timeout=15,
        )
        if not campo_totp:
            if self._parece_logado():
                self._logado = True
                print("✅ PJe: logado sem TOTP (sessão lembrada).")
                return
            self._dump_dom("tela TOTP")
            self._screenshot("totp_campo_nao_encontrado")
            raise RuntimeError("PJe: tela de TOTP não reconhecida (ver dump/screenshot).")

        codigo = gerar_codigo_totp(TOTP_ENV)
        print(f"🔢 PJe: TOTP gerado ({codigo}), validando...")
        campo_totp.clear()
        campo_totp.send_keys(codigo)

        botao_validar = self._first(
            [
                (By.XPATH, '//button[normalize-space()="VALIDAR"]'),
                (By.XPATH, '//input[@type="submit" and (@value="VALIDAR" or @value="Validar")]'),
                (By.ID, "kc-login"),
                (By.XPATH, '//button[@type="submit"]'),
            ]
        )
        if not botao_validar:
            self._dump_dom("botão VALIDAR")
            self._screenshot("totp_botao_validar_nao_encontrado")
            raise RuntimeError("PJe: botão VALIDAR do TOTP não encontrado.")
        botao_validar.click()

        try:
            WebDriverWait(self.driver, 30).until(lambda d: self._parece_logado())
        except Exception:
            self._screenshot("pos_totp_sem_confirmacao")
            raise RuntimeError("PJe: login não confirmado após o TOTP.")

        self._logado = True
        print("✅ PJe: login concluído.")

    def _parece_logado(self) -> bool:
        try:
            url = (self.driver.current_url or "").lower()
            if self.host.lower() in url and "sso.cloud.pje.jus.br" not in url:
                return True
            # nome do usuário no topo após login
            return bool(
                self.driver.find_elements(
                    By.XPATH, '//*[contains(translate(.,"MARCELO","marcelo"),"marcelo freire")]'
                )
            )
        except Exception:
            return False

    def _sessao_expirada(self) -> bool:
        """True quando a URL atual aponta para o Keycloak SSO do PJe.

        Acontece quando a sessão expirou no meio de uma corrida longa —
        driver.get(BUSCA_URL) é redirecionado pra
        sso.cloud.pje.jus.br/auth/realms/pje/... em vez de carregar o
        form de busca. Sem detectar isso, o scraper entrava em loop
        fútil esperando 25s pelo id que nunca aparece.
        """
        try:
            url = (self.driver.current_url or "").lower()
            return (
                "sso.cloud.pje.jus.br" in url
                or "openid-connect/auth" in url
                or "/auth/realms/" in url
            )
        except Exception:
            return False

    # ------------------------------------------------------------------
    # CONSULTA
    # ------------------------------------------------------------------
    def consultar_processo(self, numero_processo: str) -> Optional[str]:
        if not self._logado:
            self.login()

        m = RX_CNJ_PJE.search(str(numero_processo))
        if not m:
            print(f"⚠️ PJe: número fora do padrão CNJ: {numero_processo}")
            return None
        partes = list(m.groups())  # [NNNNNNN, DD, AAAA, J, TR, OOOO]

        # 6 campos do número, na ordem do CNJ. Guardamos só o SUFIXO do id; a
        # localização ignora a caixa (translate no XPath), porque ela varia entre
        # tribunais: TJMG usa ':Ano', o TJPA usa ':ano' — antes o TJPA falhava no
        # preenchimento e todos os 54 processos do PA vinham "não encontrado".
        sufixos_numero = [
            "numeroSequencial",        # NNNNNNN
            "numeroDigitoVerificador", # DD
            "Ano",                     # AAAA
            "ramoJustica",             # J
            "respectivoTribunal",      # TR
            "NumeroOrgaoJustica",      # OOOO
        ]

        # Acessa o form de busca; se a sessão expirou (Keycloak SSO),
        # reloga até 2x e tenta de novo. Tempo reduzido (12s) — quando o
        # redirect SSO acontece, o id nunca vai aparecer mesmo.
        # Corrida noturna (2026-05-21→22) acumulou 122 screenshots de
        # "form_nao_renderizou" — sessão SSO instável; 2 retries cobrem
        # melhor essas instabilidades intermitentes.
        MAX_TENTATIVAS_RELOGIN = 3  # 1 tentativa normal + até 2 retries
        form_pronto = False
        for tentativa in range(MAX_TENTATIVAS_RELOGIN):
            self.driver.get(self.busca_url)
            time.sleep(0.4)  # folga p/ eventual redirect Keycloak

            if self._sessao_expirada():
                if tentativa < MAX_TENTATIVAS_RELOGIN - 1:
                    print(
                        f"♻️ PJe: sessão expirou (redirect Keycloak), "
                        f"relogando (tent. {tentativa+1}/{MAX_TENTATIVAS_RELOGIN-1})..."
                    )
                    self._logado = False
                    try:
                        self.login()
                    except Exception as e:
                        print(f"⚠️ PJe: relogin tent. {tentativa+1} falhou ({e}).")
                        time.sleep(2)  # folga antes do próximo retry
                        continue
                    continue
                else:
                    self._screenshot("sessao_persistentemente_expirada")
                    print("❌ PJe: sessão continua inválida após múltiplos relogins.")
                    return None

            try:
                WebDriverWait(self.driver, 12).until(
                    EC.visibility_of_element_located(
                        (By.XPATH, self._xp_campo_numero(sufixos_numero[0]))
                    )
                )
                form_pronto = True
                break
            except Exception:
                # Algum redirect SSO pode ter acontecido durante a espera
                if (
                    tentativa < MAX_TENTATIVAS_RELOGIN - 1
                    and self._sessao_expirada()
                ):
                    print(
                        f"♻️ PJe: redirect SSO durante a espera, "
                        f"relogando (tent. {tentativa+1}/{MAX_TENTATIVAS_RELOGIN-1})..."
                    )
                    self._logado = False
                    try:
                        self.login()
                    except Exception as e:
                        print(f"⚠️ PJe: relogin tent. {tentativa+1} falhou ({e}).")
                        time.sleep(2)
                        continue
                    continue
                self._dump_dom("form de busca")
                self._screenshot("busca_form_nao_renderizou")
                return None

        if not form_pronto:
            return None

        for sufixo, valor in zip(sufixos_numero, partes):
            campo = self._campo_numero(sufixo, timeout=6)
            if campo is None:
                self._dump_dom(f"campo do numero ausente ({sufixo})")
                self._screenshot("busca_campo_numero_ausente")
                print(f"❌ PJe: campo '{sufixo}' do número não encontrado (variação de DOM).")
                return None
            campo.clear()
            campo.send_keys(valor)

        # Botão de pesquisa CONFIRMADO: fPP:searchProcessos (input type=button)
        try:
            botao = self.driver.find_element(By.ID, "fPP:searchProcessos")
        except Exception:
            self._dump_dom("botão Pesquisar")
            self._screenshot("busca_botao_pesquisar_ausente")
            return None
        self.driver.execute_script("arguments[0].click();", botao)

        # Aguarda EITHER o link do número do processo aparecer (sucesso)
        # OU o texto de "0 resultados" / "nenhum registro" (processo não
        # está no PJe — comum se ele está em eproc/outro sistema).
        xp_link = (
            '//a[starts-with(@id,"fPP:processosTable:") and '
            'contains(normalize-space(.),"-") and contains(normalize-space(.),".")]'
        )

        def _estado_busca(d):
            if d.find_elements(By.XPATH, xp_link):
                return "found"
            corpo = (d.page_source or "").lower()
            if "0 resultados" in corpo or "nenhum registro" in corpo:
                return "empty"
            return False

        try:
            estado = WebDriverWait(self.driver, 20).until(_estado_busca)
        except Exception:
            self._dump_dom("busca: timeout sem link nem 0-resultados")
            self._screenshot("busca_timeout")
            return None

        if estado == "empty":
            print(f"ℹ️ PJe: 0 resultados para {numero_processo}.")
            return None

        abrir = self.driver.find_element(By.XPATH, xp_link)

        janelas_antes = set(self.driver.window_handles)
        self.driver.execute_script("arguments[0].click();", abrir)

        # Abrir o processo pode disparar um modal de "atribuir/vincular ao
        # advogado" (habilitação). NÃO confirma: fecha com segurança e aborta
        # este processo (pedido do usuário). Checa na janela atual antes de
        # esperar a nova aba (o modal pode impedir a nova janela de abrir).
        time.sleep(0.5)
        obs = detectar_atribuicao(self.driver)
        if obs:
            raise AtribuicaoDetectadaError(obs)

        # PJe abre os autos em NOVA janela/aba — trocar o foco.
        try:
            WebDriverWait(self.driver, 25).until(
                lambda d: len(d.window_handles) > len(janelas_antes)
            )
            nova = (set(self.driver.window_handles) - janelas_antes).pop()
            self.driver.switch_to.window(nova)
            print("🪟 PJe: troquei para a janela dos autos.")
        except Exception:
            # Não abriu nova janela — talvez navegou na mesma aba (ajax)
            print("ℹ️ PJe: sem nova janela; seguindo na mesma aba.")

        # Reverifica já na janela dos autos (o aviso pode aparecer só aqui).
        obs = detectar_atribuicao(self.driver)
        if obs:
            raise AtribuicaoDetectadaError(obs)

        return self._coletar_timeline_e_documentos()

    # ------------------------------------------------------------------
    # EXTRAÇÃO: timeline + TODOS os documentos
    # ------------------------------------------------------------------
    # IDs CONFIRMADOS via diagnóstico da tela de autos (2026-05-19):
    _ID_TIMELINE = "divTimeLine:divEventosTimeLine"
    _ID_IFRAME_DOC = "frameHtml"
    _ID_NAV_PRIMEIRO = "detalheDocumento:primeiroDocumento"
    _ID_NAV_PROXIMO = "detalheDocumento:proximoDocumento"
    MAX_DOCS = 400  # trava de segurança

    # Elementos clicáveis de documento da timeline, por VERSÃO do PJe:
    #   antigo (TJMG): <span id$=":idDocumentoTimeLine"> com texto "ID - Tipo"
    #   novo (TJMA/TJCE/TJPA/TJPE): <a id^="divTimeLine:"> com texto "ID - Tipo"
    # Em ambos o texto é "NNNNN - Tipo (descrição)" e o documento abre via AJAX
    # no mesmo iframe (frameHtml). Por isso o resto da coleta é o mesmo.
    _SEL_DOC_SPAN = 'span[id$=":idDocumentoTimeLine"]'
    _SEL_DOC_ANCHOR = '#divTimeLine\\:divEventosTimeLine a[id^="divTimeLine:"]'

    def _doc_elements(self) -> list:
        """Elementos de documento da timeline (span antigo OU âncora nova),
        filtrando só os cujo texto casa 'NNNNN - Tipo'."""
        spans = [
            e for e in self.driver.find_elements(By.CSS_SELECTOR, self._SEL_DOC_SPAN)
            if RX_ID_DOC_PJE.search(e.text or "")
        ]
        if spans:
            return spans
        return [
            a for a in self.driver.find_elements(By.CSS_SELECTOR, self._SEL_DOC_ANCHOR)
            if RX_ID_DOC_PJE.search(a.text or "")
        ]

    def _iframe_doc_el(self):
        """Iframe do visualizador (frameHtml no TJMG; fallback p/ o 1º iframe
        em versões que usem outro id)."""
        try:
            return self.driver.find_element(By.ID, self._ID_IFRAME_DOC)
        except Exception:
            iframes = self.driver.find_elements(By.TAG_NAME, "iframe")
            return iframes[0] if iframes else None

    def _ler_iframe_documento(self) -> str:
        """Lê o texto do documento atual (iframe do visualizador). '' se for
        PDF/imagem sem camada de texto (limitação aceita pelo usuário)."""
        try:
            fr = self._iframe_doc_el()
            if fr is None:
                return ""
            self.driver.switch_to.frame(fr)
            txt = self.driver.find_element(By.TAG_NAME, "body").text or ""
        except Exception:
            txt = ""
        finally:
            try:
                self.driver.switch_to.default_content()
            except Exception:
                pass
        return txt

    def _ler_doc_via_http(self) -> str:
        """Fallback p/ o PJe novo (TJMA/TJCE/TJPA/TJPE): nessas versões o
        documento abre no iframe via AJAX e o switch_to.frame falha (o frame é
        recriado). Aqui baixamos o doc direto da URL do iframe usando os
        cookies da sessão e extraímos o texto (HTML via BeautifulSoup, PDF via
        pypdf). Não afeta o TJMG, onde a leitura via iframe já funciona."""
        fr = self._iframe_doc_el()
        src = (fr.get_attribute("src") if fr is not None else "") or ""
        if not src:
            return ""
        try:
            import io
            from urllib.parse import parse_qs, unquote, urljoin, urlparse

            import requests
            from bs4 import BeautifulSoup
            from pypdf import PdfReader

            # Alguns tribunais (ex.: TJCE) renderizam o doc num PDF.js viewer:
            # o src é o viewer e o PDF real fica no parâmetro ?file=. Resolve.
            q = parse_qs(urlparse(src).query)
            if q.get("file"):
                src = urljoin(src, unquote(q["file"][0]))

            sess = requests.Session()
            for c in self.driver.get_cookies():
                sess.cookies.set(c["name"], c["value"], domain=c.get("domain"))
            r = sess.get(src, timeout=60, headers={"Referer": self.driver.current_url})
            ct = (r.headers.get("Content-Type") or "").lower()
            if "pdf" in ct or r.content[:5] == b"%PDF-":
                reader = PdfReader(io.BytesIO(r.content), strict=False)
                return "\n".join((p.extract_text() or "") for p in reader.pages)
            return BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
        except Exception:
            return ""

    def _proximo_desabilitado(self) -> bool:
        try:
            el = self.driver.find_element(By.ID, self._ID_NAV_PROXIMO)
            return "disabled" in (el.get_attribute("class") or "").lower()
        except Exception:
            return True  # sem botão = não há como avançar

    def _coletar_timeline_e_documentos(self) -> Optional[str]:
        """
        Estratégia OTIMIZADA (2026-05-20): lê a timeline e abre SÓ os
        documentos cujo rótulo contém termos do TRIGGER_DOC (Despacho,
        Decisão, Sentença, SISBAJUD/BACENJUD/RENAJUD, Alvará,
        Levantamento, Penhora, Bloqueio). A medição com "abrir todos
        os 98" custou 674s e não detectou mais nada que a timeline.
        """
        try:
            WebDriverWait(self.driver, 40).until(
                EC.presence_of_element_located((By.ID, self._ID_TIMELINE))
            )
        except Exception:
            self._dump_dom("autos digitais")
            self._screenshot("autos_nao_renderizou")
            return None

        # O polo passivo do painel "Mais detalhes" é populado por AJAX um instante
        # DEPOIS da timeline. Numa corrida em lote, ler cedo demais traz o autor
        # mas perde o réu. Espera curta até o #poloPassivo ter conteúdo além do
        # rótulo; tolera ausência (processos de um polo só) sem travar.
        def _polo_passivo_populado(d):
            els = d.find_elements(By.ID, "poloPassivo")
            if not els:
                return True  # sem o elemento: não há o que esperar
            txt = (els[0].text or "").strip()
            return len(txt) > len("Polo passivo") + 3
        try:
            WebDriverWait(self.driver, 6).until(_polo_passivo_populado)
        except Exception:
            pass

        # CAMINHO RÁPIDO: o painel "Mais detalhes" (cabeçalho + polos) já está no
        # DOM da página de autos. Se presente, lê dali e NÃO abre nenhum documento
        # (era a varredura lenta que o usuário pediu para evitar). A timeline (topo,
        # sem rolar) dá a última movimentação.
        from ..parser_pje import _formatar_detalhe_pje
        bloco_detalhe = _formatar_detalhe_pje(self.driver.page_source)
        if bloco_detalhe:
            try:
                timeline_txt = self.driver.find_element(By.ID, self._ID_TIMELINE).text or ""
            except Exception:
                timeline_txt = ""
            print("📋 PJe: cabeçalho/partes do painel de detalhes (0 documentos abertos).")
            blob = "\n".join([timeline_txt, bloco_detalhe])
            return blob if blob.strip() else None
        # Painel não veio no DOM dos autos: guarda um dump p/ diagnóstico e cai
        # no fluxo de documentos (com parada na Classe).
        print("⚠️ PJe: painel 'Mais detalhes' ausente no DOM dos autos; abrindo documentos.")
        self._dump_dom("autos sem painel de detalhes")

        # Carrega a timeline (lazy do RichFaces) rolando até a contagem de docs
        # estabilizar. Isto NÃO é o gargalo (é só carregar spans); o custo grande
        # era ABRIR cada documento — controlado no loop adiante (para na Classe).
        scroll_js = (
            "var el = document.getElementById(arguments[0]);"
            "if (!el) return 0;"
            "el.scrollTop = el.scrollHeight;"
            "el.dispatchEvent(new Event('scroll', {bubbles:true}));"
            "return el.scrollHeight;"
        )
        spans_anterior = 0
        estaveis = 0
        tentativa = 0
        for tentativa in range(60):
            self.driver.execute_script(scroll_js, self._ID_TIMELINE)
            time.sleep(0.7)
            spans_agora = len(self._doc_elements())
            if spans_agora == spans_anterior:
                estaveis += 1
                if estaveis >= 3:  # 3 ciclos sem crescer = chegou ao fim
                    break
            else:
                estaveis = 0
                spans_anterior = spans_agora
        print(f"⏬ PJe: timeline carregou {spans_anterior} docs após {tentativa+1} rolagens.")

        # Texto da timeline (re-busca aqui — referência sempre fresca)
        partes_texto = [
            self.driver.find_element(By.ID, self._ID_TIMELINE).text or ""
        ]

        # Snapshot dos docs da timeline — agora com (id, texto, doc_id, data).
        # doc_id (ex.: '10554398891') vem por regex no texto do elemento.
        # data (ex.: '06 out 2025') vem do header .media.data IMEDIATAMENTE
        # antes do .media.interno do elemento (JS _JS_DATA_GRUPO).
        spans = self._doc_elements()
        candidatos = []
        for s in spans:
            try:
                sid = s.get_attribute("id")
                txt = s.text or ""
            except Exception:
                continue
            if not sid or not TRIGGER_DOC.search(unidecode(txt).upper()):
                continue

            m_id = RX_ID_DOC_PJE.search(txt)
            doc_id = m_id.group(1) if m_id else ""

            try:
                data_grupo = self.driver.execute_script(_JS_DATA_GRUPO, s) or ""
            except Exception:
                data_grupo = ""

            candidatos.append({
                "sid": sid,
                "txt": txt,
                "doc_id": doc_id,
                "data": data_grupo,
            })

        print(
            f"📑 PJe: {len(spans)} doc(s) carregado(s); "
            f"{len(candidatos)} candidato(s); abrindo até ter cabeçalho + partes "
            f"(máx. {_MAX_DOCS_ABRIR})."
        )

        abertos = 0
        for cand in candidatos:
            # Para assim que os dados básicos estiverem completos (ou no teto).
            if abertos >= _MAX_DOCS_ABRIR or _cabecalho_suficiente(
                "\n".join(p for p in partes_texto if p)
            ):
                break
            sid = cand["sid"]
            fr = self._iframe_doc_el()
            src_old = fr.get_attribute("src") if fr is not None else None

            # Re-busca o elemento por ID a cada iteração (evita stale)
            try:
                span = self.driver.find_element(By.ID, sid)
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", span
                )
                self.driver.execute_script("arguments[0].click();", span)
            except Exception:
                continue

            try:
                WebDriverWait(self.driver, 10).until(
                    lambda d: (
                        (self._iframe_doc_el() or None)
                        and self._iframe_doc_el().get_attribute("src") != src_old
                    )
                )
            except Exception:
                time.sleep(0.6)

            texto_doc = self._ler_iframe_documento()
            if not texto_doc:
                # PJe novo: iframe via AJAX não é legível por switch_to.frame;
                # baixa o doc pela URL com os cookies da sessão.
                texto_doc = self._ler_doc_via_http()
            if not texto_doc:
                continue

            # Marcador padrão do projeto — parser identifica NOME/DATA/ID.
            # EVENTO fica vazio (é específico do eproc).
            nome = (cand["txt"] or "documento").strip()
            partes_texto.append(
                f"<!-- DOCUMENTO_ORIGEM: {html.escape(nome, quote=False)} "
                f"| EVENTO: "
                f"| DATA: {html.escape(cand['data'], quote=False)} "
                f"| ID: {html.escape(cand['doc_id'], quote=False)} -->\n"
                f"{texto_doc}"
            )
            abertos += 1

        print(f"📑 PJe: {abertos} documento(s) aberto(s) para extrair os dados.")
        blob = "\n".join(p for p in partes_texto if p)
        if not blob.strip():
            self._screenshot("autos_blob_vazio")
            return None
        return blob
