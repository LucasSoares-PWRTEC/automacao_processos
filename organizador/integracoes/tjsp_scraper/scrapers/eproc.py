# src/scrapers/eproc.py
"""
Scraper eproc — TJMG e TJSP (1º grau).

Fluxo (mesmo passo-a-passo para MG e SP, confirmado por prints):

  1. /eproc/                       -> tela login (Usuário + Senha + Entrar)
  2. /eproc/index.php              -> 2FA TOTP (6 dígitos + Validar)
  3. .../externo_controlador.php?acao=entrar
                                   -> Seleção de perfil (clicar a lotação
                                      compatível com o estado)
  4. .../controlador.php?acao=painel_adv_listar
                                   -> Painel do Advogado (campo "Nº de
                                      processo" no topo direito + lupa)
  5. .../controlador.php?acao=processo_selecionar&num_processo=...
                                   -> Detalhes do processo + tabela Eventos
                                      com coluna Documentos.

Esta primeira versão devolve o HTML da página de detalhes (Eventos).
A abertura dos documentos da coluna Documentos fica para a próxima
iteração (vamos olhar a precisão do parser antes — alguns eventos já
trazem descrição rica suficiente para detecção).
"""

import html
import os
import re
import time
from pathlib import Path
from datetime import datetime
from typing import Optional

from selenium.common.exceptions import (
    InvalidElementStateException,
    StaleElementReferenceException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from .base import TribunalScraper, criar_chrome_driver
from ..credenciais import obter_credenciais
from ..two_factor import gerar_codigo_totp
from ..parser_processo import _classificar_tipo

# ----------------------------------------------------------------------
# Configuração por estado
# ----------------------------------------------------------------------
# Mapeia o estado para:
#   tj_chave   -> linha em data/senhas.xlsx
#   totp_env   -> variável TOTP no .env
#   sigla      -> prefixo usado para identificar a lotação correta na tela
#                 "Seleção de perfil" (linha começa com "MG..." / "SP...").
_CONFIG_POR_ESTADO = {
    "MINAS GERAIS": {
        "tj_chave": "TJMG - eproc 1 e 2 graus",
        "totp_env": "TOTP_EPROC_TJMG",
        "sigla":    "MG",
        # Login interno do eproc (sem SSO)
        "login_user_id":   "txtUsuario",
        "login_pass_id":   "pwdSenha",
        "login_submit_id": "sbmEntrar",
        # Tela 2FA interna do eproc
        "totp_input_id":  "txtAcessoCodigo",
        "totp_submit_id": "btnValidar",
    },
    "SAO PAULO": {
        "tj_chave": "TJSP - Eproc",
        "totp_env": "TOTP_EPROC_TJSP",
        "sigla":    "SP",
        # Login via Keycloak SSO (sso.tjsp.jus.br/realms/eproc)
        "login_user_id":   "username",
        "login_pass_id":   "password",
        "login_submit_id": "kc-login",
        # Tela 2FA do Keycloak (form kc-otp-login-form)
        "totp_input_id":  "otp",
        "totp_submit_id": "kc-login",
    },
}

_DEBUG_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "_debug"
EPROC_DOC_TIMEOUT = int(os.getenv("EPROC_DOC_TIMEOUT", "20"))


class EprocScraper(TribunalScraper):

    def __init__(self, estado: str, url: str, headless: bool = False):
        super().__init__(estado=estado, url=url, headless=headless)
        if estado not in _CONFIG_POR_ESTADO:
            raise ValueError(f"eproc: estado não suportado: {estado}")
        cfg = _CONFIG_POR_ESTADO[estado]
        self._tj_chave        = cfg["tj_chave"]
        self._totp_env        = cfg["totp_env"]
        self._sigla           = cfg["sigla"]
        self._login_user_id   = cfg["login_user_id"]
        self._login_pass_id   = cfg["login_pass_id"]
        self._login_submit_id = cfg["login_submit_id"]
        self._totp_input_id   = cfg["totp_input_id"]
        self._totp_submit_id  = cfg["totp_submit_id"]

        self.driver = criar_chrome_driver(headless=headless)
        self.wait = WebDriverWait(self.driver, 20)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _first(self, locators, timeout=10, exigir_visivel=True):
        """Tenta uma lista de (By, valor); devolve o 1º elemento presente."""
        for by, val in locators:
            try:
                el = WebDriverWait(self.driver, timeout).until(
                    EC.presence_of_element_located((by, val))
                )
                if not exigir_visivel or el.is_displayed():
                    return el
            except Exception:
                continue
        return None

    def _screenshot(self, nome: str):
        try:
            _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            caminho = _DEBUG_DIR / f"eproc_{self._sigla.lower()}_{nome}_{ts}.png"
            self.driver.save_screenshot(str(caminho))
            print(f"🖼️  Screenshot de debug salvo: {caminho}")
        except Exception:
            pass

    def _cpf_digitos(self, login: str) -> str:
        """11 dígitos com zero à esquerda (eproc aceita só dígitos no Usuário)."""
        return re.sub(r"\D", "", str(login or "")).zfill(11)

    # ------------------------------------------------------------------
    # LOGIN
    # ------------------------------------------------------------------
    def login(self) -> None:
        if self._logado:
            return

        login_raw, senha = obter_credenciais(self._tj_chave)
        usuario = self._cpf_digitos(login_raw)
        print(f"🔐 eproc/{self._sigla}: logando com usuário {usuario} ...")

        self.driver.get(self.url)

        # --- Tela 1: usuário + senha ---
        # MG (login interno do eproc):    #txtUsuario / #pwdSenha / #sbmEntrar
        # SP (Keycloak SSO sso.tjsp...):  #username   / #password / #kc-login
        # Em ambos o input de senha visível é o "masked" (type=text com classe
        # 'masked'); o type=password real fica hidden ao lado.
        try:
            WebDriverWait(self.driver, 30).until(
                EC.visibility_of_element_located((By.ID, self._login_user_id))
            )
        except Exception:
            self._screenshot("login_form_nao_renderizou")
            raise RuntimeError(
                f"eproc/{self._sigla}: form de login (#{self._login_user_id}) "
                f"não renderizou em 30s."
            )

        campo_user = self.driver.find_element(By.ID, self._login_user_id)
        campo_pass = self.driver.find_element(By.ID, self._login_pass_id)

        campo_user.clear()
        campo_user.send_keys(usuario)
        campo_pass.clear()
        campo_pass.send_keys(senha)

        try:
            botao_entrar = WebDriverWait(self.driver, 5).until(
                EC.element_to_be_clickable((By.ID, self._login_submit_id))
            )
        except Exception:
            self._screenshot("login_botao_entrar_nao_encontrado")
            raise RuntimeError(
                f"eproc/{self._sigla}: botão Entrar (#{self._login_submit_id}) "
                f"não encontrado/clicável."
            )
        botao_entrar.click()
        print(f"➡️  eproc/{self._sigla}: credenciais enviadas, aguardando 2FA...")

        # --- Tela 2: 2FA TOTP ---
        # IDs por estado (config):
        #   MG (eproc interno): #txtAcessoCodigo / #btnValidar
        #   SP (Keycloak SSO):  #otp             / #kc-login
        try:
            campo_codigo = WebDriverWait(self.driver, 30).until(
                EC.visibility_of_element_located((By.ID, self._totp_input_id))
            )
        except Exception:
            campo_codigo = None

        if not campo_codigo:
            # Possível login sem 2FA (sessão lembrada) — segue
            print(f"ℹ️  eproc/{self._sigla}: tela 2FA não apareceu (sessão lembrada?).")
        else:
            codigo = gerar_codigo_totp(self._totp_env)
            print(f"🔢 eproc/{self._sigla}: TOTP gerado ({codigo}), enviando...")
            campo_codigo.clear()
            campo_codigo.send_keys(codigo)

            try:
                botao_validar = WebDriverWait(self.driver, 5).until(
                    EC.element_to_be_clickable((By.ID, self._totp_submit_id))
                )
            except Exception:
                self._screenshot("2fa_botao_validar_nao_encontrado")
                raise RuntimeError(
                    f"eproc/{self._sigla}: botão de envio do 2FA "
                    f"(#{self._totp_submit_id}) não encontrado/clicável."
                )
            botao_validar.click()

            # Detecta rejeição explícita do código (não vale a pena esperar 25s pelo painel)
            time.sleep(1.2)
            page = (self.driver.page_source or "").lower()
            if "erro verificando chave" in page or "erro na autenticação" in page:
                self._screenshot("2fa_codigo_rejeitado")
                raise RuntimeError(
                    "eproc: código 2FA rejeitado ('Erro verificando chave'). "
                    "Verifique TOTP_EPROC_* no .env e o relógio."
                )

        # --- Tela 3: Seleção de perfil (se aparecer) ---
        try:
            WebDriverWait(self.driver, 15).until(
                lambda d: (
                    "selec" in (d.current_url or "").lower()
                    or "selec" in (d.page_source or "").lower()
                    or "painel" in (d.current_url or "").lower()
                )
            )
        except Exception:
            pass  # segue — o próximo wait confirma

        if self._tela_selecao_perfil():
            print(f"👤 eproc/{self._sigla}: tela de seleção de perfil — escolhendo lotação...")
            if not self._clicar_lotacao_do_estado():
                self._screenshot("selecao_perfil_lotacao_nao_encontrada")
                raise RuntimeError(
                    f"eproc/{self._sigla}: não achei lotação compatível com '{self._sigla}' "
                    f"na tela de seleção de perfil."
                )

        # --- Confirma chegada no Painel do Advogado ---
        try:
            WebDriverWait(self.driver, 25).until(
                lambda d: self._parece_logado()
            )
        except Exception:
            self._screenshot("pos_login_sem_painel")
            raise RuntimeError("eproc: não cheguei ao Painel após o login/2FA.")

        self._logado = True
        print(f"✅ eproc/{self._sigla}: login concluído.")

    def _tela_selecao_perfil(self) -> bool:
        try:
            txt = (self.driver.page_source or "").lower()
            return "seleção de perfil" in txt or "selecao de perfil" in txt
        except Exception:
            return False

    def _clicar_lotacao_do_estado(self) -> bool:
        """
        Procura a lotação cuja sigla começa com self._sigla (ex MG, SP)
        e clica nela. Estrutura confirmada via teste_eproc_perfil_dom.py:
        cada lotação é um <button class="list-group-item ..." onclick=
        "acaoLogar('...')"> cujo texto começa com a sigla (MG104784, ...).
        """
        botoes = self.driver.find_elements(
            By.CSS_SELECTOR, "button.list-group-item"
        )
        for btn in botoes:
            try:
                if not (btn.is_displayed() and btn.is_enabled()):
                    continue
                texto = (btn.text or "").strip().upper()
                if texto.startswith(self._sigla.upper()):
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", btn
                    )
                    btn.click()
                    return True
            except Exception:
                continue
        return False

    def _parece_logado(self) -> bool:
        """Heurística: URL no painel OU campo de busca 'Nº de processo' presente."""
        try:
            url = (self.driver.current_url or "").lower()
            if "painel_adv" in url or "controlador.php?acao=painel" in url:
                return True
            return bool(self._campo_busca_processo(timeout=1))
        except Exception:
            return False

    # ------------------------------------------------------------------
    # CONSULTA
    # ------------------------------------------------------------------
    def _campo_busca_processo(self, timeout: int = 10):
        return self._first(
            [
                (By.ID, "txtNumProcesso"),
                (By.ID, "txtNumProc"),
                (By.NAME, "txtNumProcesso"),
                (By.XPATH, '//input[@placeholder="Nº de processo"]'),
                (By.XPATH, '//input[contains(@placeholder,"processo")]'),
                (By.XPATH, '//input[contains(@aria-label,"processo")]'),
            ],
            timeout=timeout,
        )

    def consultar_processo(
        self, numero_processo: str, modo_docs: str = "atos"
    ) -> Optional[str]:
        """
        modo_docs controla quais documentos da coluna 'Documentos' sao abertos
        para montar a secao 'atos' do JSON:
          - "atos"  (default): so ATO ORDINATORIO/DESPACHO/DECISAO/SENTENCA.
          - "todos": abre todos os documentos listados.
          - "nenhum": nao abre documento algum (JSON sai com "atos": []).
        """
        if not self._logado:
            self.login()

        # Tenta digitar o número no campo de busca. Estados ruins comuns:
        # - campo de busca ausente → sessão caiu, reloga e tenta de novo
        # - InvalidElementStateException / StaleElementReference → a página
        #   anterior deixou o input bloqueado/destacado do DOM; recarrega
        #   o painel e pega um campo fresco antes de tentar de novo.
        sucesso = False
        for tentativa in range(2):
            campo = self._campo_busca_processo(timeout=2)
            if not campo:
                if tentativa == 0:
                    print(f"♻️  eproc/{self._sigla}: campo de busca ausente, tentando relogar...")
                    self._logado = False
                    self.login()
                    continue
                self._screenshot("busca_campo_processo_ausente")
                return None

            try:
                campo.clear()
                campo.send_keys(str(numero_processo))
                campo.send_keys(Keys.ENTER)
                sucesso = True
                break
            except (InvalidElementStateException, StaleElementReferenceException) as e:
                if tentativa == 0:
                    print(
                        f"♻️  eproc/{self._sigla} {numero_processo}: "
                        f"campo em estado inválido ({type(e).__name__}), "
                        f"recarregando painel e tentando de novo..."
                    )
                    try:
                        self.driver.get(self.url)
                        time.sleep(0.6)
                    except Exception:
                        pass
                    continue
                self._screenshot("busca_campo_estado_invalido")
                return None

        if not sucesso:
            return None

        # Aguarda a página de detalhes carregar OU um indicador de "não encontrado".
        # A mensagem real do eproc é "Processo não encontrado." (sem o "foi").
        try:
            WebDriverWait(self.driver, 2).until(
                lambda d: (
                    "processo_selecionar" in (d.current_url or "").lower()
                    or "Detalhes do Processo" in (d.page_source or "")
                    or "não encontrado" in (d.page_source or "").lower()
                    or "nao encontrado" in (d.page_source or "").lower()
                )
            )
        except Exception:
            self._screenshot("busca_sem_resposta")
            return None

        pagina = self.driver.page_source or ""
        if "processo não encontrado" in pagina.lower() or "processo nao encontrado" in pagina.lower():
            print(f"   eproc/{self._sigla} {numero_processo}: processo não encontrado.")
            return pagina

        # Dá uma pequena folga para o JS popular a tabela de eventos
        time.sleep(0.5)

        # Aguarda o AJAX de "Informações Adicionais" popular
        # (#fldInformacoesAdicionais_content). Esse campo traz valor da
        # causa, foro/endereco e tags (Idoso/Sigilo/etc.). Se nao chegar
        # em 5s, segue mesmo assim — esses campos sao opcionais.
        try:
            WebDriverWait(self.driver, 2).until(
                lambda d: bool(
                    (d.find_element(By.ID, "fldInformacoesAdicionais_content").text or "").strip()
                )
            )
        except Exception:
            pass

        html_detalhes = self.driver.page_source or ""
        if modo_docs == "nenhum":
            print(f"   eproc/{self._sigla}: modo_docs='nenhum' — pulando abertura de documentos.")
            return html_detalhes
        textos_docs = self._abrir_documentos(html_detalhes, modo_docs=modo_docs)
        if not textos_docs:
            return html_detalhes
        return html_detalhes + "\n\n<!-- DOCUMENTOS DO PROCESSO -->\n\n" + "\n\n".join(textos_docs)

    # ------------------------------------------------------------------
    # DOCUMENTOS DO PROCESSO
    # ------------------------------------------------------------------
    def _abrir_documentos(self, html_detalhes: str, modo_docs: str = "atos") -> list[str]:
        """
        Lê os links da coluna 'Documentos' da tabela #tblEventos e abre
        cada documento via driver.get(href). Retorna o page_source de cada
        um — depois é tudo concatenado e passado ao parser.

        Os hrefs são lidos via Selenium (não BeautifulSoup) porque o BS4
        devolve entidades HTML (&amp;) cruas — passar isso para driver.get
        gera 'invalid argument'.

        Cada link é <a class="infraLinkDocumento" href="...
        controlador.php?acao=acessar_documento&doc=NN" title="Nome\\nSigilo\\nTamanho">
        """
        # Detecta a tabela só para abortar cedo se a busca cair em página estranha
        if 'id="tblEventos"' not in html_detalhes and "tblEventos" not in html_detalhes:
            print(f"   eproc/{self._sigla}: tabela #tblEventos não encontrada (sem docs).")
            return []

        url_detalhes = self.driver.current_url
        # Selenium devolve href já decodificado (sem &amp;) e absolutizado.
        # Iteramos as LINHAS da tabela #tblEventos para também extrair o
        # número do evento (td[0]) e a data/hora (td[1]) — esses metadados
        # entram no marcador DOCUMENTO_ORIGEM e depois no banco.
        docs = []
        try:
            linhas = self.driver.find_elements(
                By.CSS_SELECTOR, "table#tblEventos tbody tr"
            )
        except Exception:
            linhas = []

        for tr in linhas:
            try:
                tds = tr.find_elements(By.CSS_SELECTOR, "td")
                if len(tds) < 5:
                    continue
                evento_num = (tds[0].text or "").strip()
                evento_data = (tds[1].text or "").strip()
                # td[2] = descricao do evento (mesmo indice usado pelo parser
                # em _parse_tr_evento). Serve para o fallback de classificacao.
                evento_desc = (tds[2].text or "").strip() if len(tds) > 2 else ""
                anchors_linha = tds[4].find_elements(
                    By.CSS_SELECTOR, "a.infraLinkDocumento"
                )
            except Exception:
                continue

            for a in anchors_linha:
                href = (a.get_attribute("href") or "").strip()
                if not href or href.startswith("javascript:"):
                    continue
                title = (a.get_attribute("title") or "").split("\n")[0].strip()
                label = (a.text or "").strip()
                docs.append({
                    "href": href,
                    "title": title,
                    "label": label,
                    "evento": evento_num,
                    "data": evento_data,
                    "descricao": evento_desc,
                })

        if not docs:
            print(f"   eproc/{self._sigla}: nenhum documento na lista de eventos.")
            return []

        # No modo "atos" (default) abrimos apenas os documentos que o parser de
        # fato usa na secao "atos" (ATO ORDINATORIO/DESPACHO/DECISAO/SENTENCA).
        # Os demais (contestacao, procuracao, AR, eCARTA, etc.) sao baixados e
        # descartados pelo parser — abri-los so custa tempo. _classificar_tipo
        # e a MESMA regra aplicada depois em parser_processo._extrair_atos.
        if modo_docs == "atos":
            total = len(docs)
            docs = [
                d for d in docs
                if _classificar_tipo(d["title"] or d["label"], d.get("descricao", ""))
            ]
            ignorados = total - len(docs)
            print(
                f"   eproc/{self._sigla}: filtro atos — {len(docs)}/{total} "
                f"documento(s) serao abertos ({ignorados} ignorado(s))."
            )
            if not docs:
                return []

        return self._baixar_documentos_selenium(docs, url_detalhes)

    def _baixar_documentos_selenium(self, docs: list[dict], url_detalhes: str) -> list[str]:
        """
        Abre cada documento pelo Selenium (serializado) e captura o HTML
        APOS o AJAX preencher `#divdochtml`. O href padrao do eproc abre
        um wrapper que chama `acessar_documento_implementacao` via AJAX;
        baixar so o wrapper (como `requests.get` fazia antes) devolve um
        esqueleto sem o teor do ato. Por isso usamos o navegador.

        Mais lento que o download direto, mas eh a unica forma de pegar
        teor, signatario, codigo verificador e CRC dos atos processuais.
        """
        print(
            f"   📂 eproc/{self._sigla}: abrindo {len(docs)} documento(s) "
            f"via Selenium (seriado)..."
        )

        textos: list[str] = []
        for i, doc in enumerate(docs, 1):
            nome = doc["title"] or doc["label"] or f"documento_{i}"
            try:
                self.driver.get(doc["href"])
                self._aguardar_conteudo_documento()
                conteudo = self.driver.page_source or ""
                textos.append(
                    self._doc_html_com_origem(
                        nome, conteudo, doc.get("evento", ""), doc.get("data", "")
                    )
                )
                print(f"      [{i:02d}/{len(docs)}] {nome} ({len(conteudo)} chars)")
            except Exception as e:
                print(f"      [{i:02d}/{len(docs)}] {nome} — ERRO: {e}")
                continue

        # Volta para a pagina de detalhes: chamadas subsequentes (proximo
        # consultar_processo na mesma sessao) esperam estar logadas no
        # painel/detalhes, nao na visualizacao de um documento.
        try:
            self.driver.get(url_detalhes)
        except Exception:
            pass

        return textos

    def _aguardar_conteudo_documento(self, timeout: int = EPROC_DOC_TIMEOUT) -> None:
        """
        Espera o AJAX do wrapper popular `#divdochtml` com texto. Se nao
        houver `#divdochtml` (alguns documentos sao PDFs puros que abrem
        em outra rota), cai pra esperar o <body> ter texto qualquer.
        """
        try:
            WebDriverWait(self.driver, timeout).until(
                lambda d: (
                    bool((d.find_element(By.ID, "divdochtml").text or "").strip())
                )
            )
            return
        except Exception:
            pass
        # Fallback: body com algum conteudo
        try:
            WebDriverWait(self.driver, 5).until(
                lambda d: bool((d.find_element(By.TAG_NAME, "body").text or "").strip())
            )
        except Exception:
            pass

    @staticmethod
    def _doc_html_com_origem(
        nome: str,
        conteudo: str,
        evento: str = "",
        data: str = "",
    ) -> str:
        """
        Marca o início do bloco de um documento com NOME, EVENTO e DATA
        (vindos da linha da tabela #tblEventos). O parser identifica essas
        três informações pela regex em parser._RX_DOCUMENTO_ORIGEM.

        EVENTO e DATA podem ser strings vazias (ex.: doc fora da tabela).
        """
        nome_marker = html.escape(nome or "documento", quote=False)
        evento_marker = html.escape((evento or "").strip(), quote=False)
        data_marker = html.escape((data or "").strip(), quote=False)
        return (
            f"<!-- DOCUMENTO_ORIGEM: {nome_marker} "
            f"| EVENTO: {evento_marker} "
            f"| DATA: {data_marker} -->\n"
            f"{conteudo}"
        )
