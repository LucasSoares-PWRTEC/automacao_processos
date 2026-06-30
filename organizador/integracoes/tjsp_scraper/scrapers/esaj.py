# src/scrapers/esaj.py
"""
Scraper e-SAJ TJSP — 1º grau (cpopg) com login + 2FA por email.

Fluxo confirmado por prints (docs/sites_spec.md):
  login (sajcas) -> 2FA email (csc01@candiottovalle.com, 6 dígitos)
  -> cpopg/open.do -> preenche número/foro -> Consultar
  -> show.do -> expande "Mais" -> HTML das movimentações

Reaproveita do src/scraper.py: split do CNJ, ids dos campos de busca,
clique repetido em "Mais", leitura de #tabelaTodasMovimentacoes.

⚠️ Seletores da tela de LOGIN (sajcas) e da tela de 2FA não foram
confirmados via F12 — uso os ids padrão históricos do e-SAJ com
fallbacks. Validar no primeiro teste real (há screenshot em falha).
"""

import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, quote

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from .base import TribunalScraper, criar_chrome_driver
from .popup import AtribuicaoDetectadaError, detectar_atribuicao
from ..credenciais import obter_credenciais, cpf_formatado
from ..two_factor import obter_codigo_2fa_email

# 2FA por email
# O código do e-SAJ chega encaminhado pelo Acessos Tribunais (mcsarc), NÃO
# do csc01. O mesmo remetente envia "Andamento Processual"/"Movimentações"
# (que também têm números de 6 dígitos), então filtramos TAMBÉM pelo assunto
# "Validação de identificação" para não pegar um código errado. O assunto é o
# padrão da plataforma SAJ — vale para o e-SAJ de qualquer estado.
REMETENTE_2FA = "acesso.tribunais@mcsarc.com.br"
ASSUNTO_2FA = "Validação de identificação"
# 6 min — cobre rate-limit/atraso de email do TJSP. Em corrida noturna
# (2026-05-21→22), 3 logins seguidos estouraram o limite de 240s e
# derrubaram o bloco SP/esaj sem gravar nada. Pode ser encurtado via env
# (ESAJ_2FA_TIMEOUT_SEG) em testes exploratórios para não pagar 6 min por falha.
TIMEOUT_2FA_SEG = int(os.getenv("ESAJ_2FA_TIMEOUT_SEG", "360"))

# CNJ genérico: separa "NNNNNNN-DD.AAAA" (campo numeroDigitoAnoUnificado) do
# foro "OOOO" (foroNumeroUnificado), para QUALQUER J.TR — o e-SAJ existe em
# vários estados (TJSP .8.26, TJAL .8.02, TJAM .8.04, TJCE .8.06, TJMS .8.12…),
# não só no TJSP.
RX_CNJ = re.compile(r"(\d{7}-\d{2}\.\d{4})\.\d\.\d{2}\.(\d{4})")


def _derivar_login_e_chave(url: str) -> tuple[str, str]:
    """Do `url` do cpopg (ex.: https://esaj.tjce.jus.br/cpopg/open.do) deriva:
      - a URL de login do sajcas com `service` apontando de volta ao cpopg;
      - a chave de credencial ("TJCE - Esaj") a partir do host tjXX.jus.br.
    Para o TJSP isso reproduz exatamente a URL/chave usadas historicamente."""
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    login_url = f"{base}/sajcas/login?service={quote(url, safe='')}"
    m = re.search(r"tj([a-z]{2})\.jus\.br", parts.netloc.lower())
    tj_chave = f"TJ{m.group(1).upper()} - Esaj" if m else "TJSP - Esaj"
    return login_url, tj_chave

_DEBUG_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "_debug"


class ESAJScraper(TribunalScraper):

    def __init__(self, estado: str, url: str, headless: bool = False):
        super().__init__(estado=estado, url=url, headless=headless)
        self._login_url, self._tj_chave = _derivar_login_e_chave(url)
        self.driver = criar_chrome_driver(headless=headless)
        self.wait = WebDriverWait(self.driver, 20)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _first(self, locators, timeout=10):
        """Tenta uma lista de (By, valor); devolve o 1º elemento presente."""
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

    def _screenshot(self, nome: str):
        try:
            _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            caminho = _DEBUG_DIR / f"esaj_{nome}_{ts}.png"
            self.driver.save_screenshot(str(caminho))
            print(f"🖼️  Screenshot de debug salvo: {caminho}")
        except Exception:
            pass

    def _count_mov_rows(self) -> int:
        rows = self.driver.find_elements(
            By.CSS_SELECTOR,
            "#tabelaTodasMovimentacoes tr, #tabelaMovimentacoes tr",
        )
        return len(rows)

    def _click_all_mais_mov(self, max_clicks: int = 15):
        """Clica em 'Mais' até esgotar as movimentações (igual scraper.py)."""
        clicks = 0
        while clicks < max_clicks:
            antes = self._count_mov_rows()
            btn = self._first(
                [
                    (By.ID, "linkMovimentacoes"),
                    (By.XPATH, '//a[normalize-space()="Mais"]'),
                    (By.XPATH, '//button[normalize-space()="Mais"]'),
                    (By.XPATH, '//a[contains(@onclick,"carregarMais") or contains(., "Mais")]'),
                ],
                timeout=3,
            )
            if not btn:
                break
            try:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", btn
                )
                WebDriverWait(self.driver, 3).until(
                    EC.element_to_be_clickable(btn)
                ).click()
            except Exception:
                try:
                    self.driver.execute_script("arguments[0].click();", btn)
                except Exception:
                    break
            time.sleep(0.3)
            clicks += 1
            if self._count_mov_rows() <= antes:
                time.sleep(0.2)
                if self._count_mov_rows() <= antes:
                    break

    # ------------------------------------------------------------------
    # LOGIN + 2FA
    # ------------------------------------------------------------------
    def login(self) -> None:
        if self._logado:
            return

        login_raw, senha = obter_credenciais(self._tj_chave)
        cpf = cpf_formatado(login_raw)
        print(f"🔐 e-SAJ [{self._tj_chave}]: logando com CPF {cpf} ...")

        self.driver.get(self._login_url)

        # e-SAJ é um SPA (Angular): o form de login demora a renderizar.
        # Espera explicitamente o campo de senha ficar VISÍVEL antes de
        # tentar preencher (ids confirmados via diagnóstico do DOM real).
        try:
            WebDriverWait(self.driver, 40).until(
                EC.visibility_of_element_located((By.ID, "passwordForm"))
            )
        except Exception:
            self._screenshot("login_spa_nao_renderizou")
            raise RuntimeError(
                "e-SAJ: form de login não renderizou em 40s (SPA). "
                "Veja o screenshot de debug."
            )

        campo_user = self.driver.find_element(By.ID, "usernameForm")
        campo_pass = self.driver.find_element(By.ID, "passwordForm")

        campo_user.clear()
        campo_user.send_keys(cpf)
        campo_pass.clear()
        campo_pass.send_keys(senha)

        # Marca o instante ANTES de submeter — o leitor de email só
        # considera códigos que chegarem depois daqui. Recuamos uma margem
        # porque o Date do email do TJSP costuma ficar ~1 min "antes" deste
        # instante (latência da SPA + relógio do servidor de email), o que
        # fazia o leitor descartar o código legítimo e estourar o timeout.
        # O leitor retorna o email MAIS RECENTE que casa remetente+assunto,
        # então a janela folgada não pega código antigo — pega o mais novo.
        desde = datetime.now(timezone.utc) - timedelta(seconds=180)

        botao_entrar = self._first(
            [
                (By.ID, "pbEntrar"),
                (By.NAME, "pbEntrar"),
                (By.XPATH, '//button[normalize-space()="Entrar"]'),
                (By.XPATH, '//input[@type="submit" and @value="Entrar"]'),
            ]
        )
        if not botao_entrar:
            self._screenshot("login_botao_entrar_nao_encontrado")
            raise RuntimeError("e-SAJ: botão 'Entrar' não encontrado.")
        botao_entrar.click()
        print("➡️  e-SAJ: credenciais enviadas, aguardando tela de 2FA...")

        # Tela "Validação de identificação" — campo do código.
        # id confirmado via diagnóstico do DOM real: tokenInformado.
        campo_codigo = None
        try:
            campo_codigo = WebDriverWait(self.driver, 20).until(
                EC.visibility_of_element_located((By.ID, "tokenInformado"))
            )
        except Exception:
            # Pode ter logado sem pedir 2FA (sessão lembrada) — confere
            if self._confirmar_sessao():
                self._logado = True
                print("✅ e-SAJ: logado sem 2FA (sessão lembrada).")
                return
            # Senha expirada é comum em contas pouco usadas: o SAJ não pede
            # 2FA, mostra "Senha expirada" e manda redefinir por e-mail.
            if "senha expirada" in (self.driver.page_source or "").lower():
                self._screenshot("login_senha_expirada")
                raise RuntimeError(
                    f"e-SAJ [{self._tj_chave}]: senha expirada — redefina a senha "
                    "pelo e-mail do tribunal antes de usar este e-SAJ."
                )
            self._screenshot("2fa_campo_codigo_nao_encontrado")
            raise RuntimeError("e-SAJ: tela de 2FA não reconhecida.")

        print(f"📧 e-SAJ: buscando código 2FA no email (de {REMETENTE_2FA})...")
        codigo = obter_codigo_2fa_email(
            desde=desde,
            remetente=REMETENTE_2FA,
            assunto_contem=ASSUNTO_2FA,
            timeout_seg=TIMEOUT_2FA_SEG,
            intervalo_seg=5,
        )
        if not codigo:
            self._screenshot("2fa_codigo_nao_recebido")
            raise RuntimeError(
                "e-SAJ: não recebi o código 2FA por email dentro do timeout."
            )

        print(f"🔢 e-SAJ: código recebido ({codigo}), enviando...")
        campo_codigo.clear()
        campo_codigo.send_keys(codigo)

        # id confirmado via diagnóstico do DOM real: btnEnviarToken.
        botao_enviar = self._first(
            [
                (By.ID, "btnEnviarToken"),
                (By.XPATH, '//button[normalize-space()="Enviar"]'),
            ]
        )
        if not botao_enviar:
            self._screenshot("2fa_botao_enviar_nao_encontrado")
            raise RuntimeError("e-SAJ: botão 'Enviar' do 2FA não encontrado.")
        botao_enviar.click()

        # Confirma que entrou. Atenção: alguns e-SAJ (ex.: TJMS) redirecionam
        # ao portal "Bem-vindo" após o 2FA, NÃO direto ao cpopg — por isso a
        # confirmação navega ao cpopg e checa o campo de busca (teste real da
        # sessão), em vez de depender da página de aterrissagem.
        if not self._confirmar_sessao():
            self._screenshot("pos_2fa_sem_confirmacao")
            raise RuntimeError(
                "e-SAJ: não confirmei o login após enviar o código 2FA."
            )

        self._logado = True
        print("✅ e-SAJ: login concluído.")

    def _parece_logado(self) -> bool:
        """Heurística rápida: campo de busca presente OU URL já no cpopg."""
        try:
            url = (self.driver.current_url or "").lower()
            if "cpopg" in url and "sajcas" not in url:
                return True
            return bool(
                self.driver.find_elements(By.ID, "numeroDigitoAnoUnificado")
            )
        except Exception:
            return False

    def _confirmar_sessao(self) -> bool:
        """Confirma sessão autenticada. Primeiro tenta abrir o cpopg e ver o
        campo de busca; se o e-SAJ aterrissar no portal "Bem-vindo" (ex.: TJMS)
        e o cpopg demorar, aceita o sinal de portal autenticado (ausência do
        botão "Identificar-se" fora do sajcas). A consulta valida o campo de
        busca de novo antes de pesquisar, então isto não mascara sessão morta."""
        if self._parece_logado():
            return True
        try:
            self.driver.get(self.url)
            WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.ID, "numeroDigitoAnoUnificado"))
            )
            return True
        except Exception:
            return self._autenticado_no_portal()

    def _autenticado_no_portal(self) -> bool:
        """Logado quando saímos do sajcas e o botão 'Identificar-se' some
        (o topo passa a exibir o nome do usuário)."""
        try:
            url = (self.driver.current_url or "").lower()
            if "sajcas" in url:
                return False
            ident = self.driver.find_elements(
                By.XPATH, "//*[normalize-space(text())='Identificar-se']"
            )
            return not any(e.is_displayed() for e in ident)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # CONSULTA
    # ------------------------------------------------------------------
    def consultar_processo(self, numero_processo: str) -> Optional[str]:
        if not self._logado:
            self.login()

        m = RX_CNJ.search(str(numero_processo))
        if not m:
            print(f"⚠️ e-SAJ: número fora do padrão CNJ 8.26: {numero_processo}")
            return None
        parte_num, parte_foro = m.group(1), m.group(2)

        self.driver.get(self.url)  # cpopg/open.do (sessão logada)

        campo_num = self._first(
            [(By.ID, "numeroDigitoAnoUnificado")], timeout=15
        )
        if not campo_num:
            # sessão pode ter expirado — tenta relogar uma vez
            print("♻️ e-SAJ: campo de busca ausente, tentando relogar...")
            self._logado = False
            self.login()
            self.driver.get(self.url)
            campo_num = self._first(
                [(By.ID, "numeroDigitoAnoUnificado")], timeout=15
            )
            if not campo_num:
                self._screenshot("busca_campo_numero_ausente")
                return None

        campo_num.clear()
        campo_num.send_keys(parte_num)

        campo_foro = self.driver.find_element(By.ID, "foroNumeroUnificado")
        campo_foro.clear()
        campo_foro.send_keys(parte_foro)

        botao = self._first(
            [
                (By.ID, "botaoConsultarProcessos"),
                (By.XPATH, '//*[normalize-space()="Consultar"]'),
            ]
        )
        if not botao:
            self._screenshot("busca_botao_consultar_ausente")
            return None
        botao.click()

        # Espera a página do processo OU indício de "não encontrado"
        try:
            WebDriverWait(self.driver, 15).until(
                EC.any_of(
                    EC.presence_of_element_located((By.ID, "tabelaTodasMovimentacoes")),
                    EC.presence_of_element_located((By.ID, "tabelaMovimentacoes")),
                    EC.presence_of_element_located((By.ID, "listagemDeProcessos")),
                    EC.presence_of_element_located((By.CLASS_NAME, "mensagemSemRegistros")),
                )
            )
        except Exception:
            self._screenshot("pos_consulta_sem_resultado")
            return self.driver.page_source  # devolve o que tiver; parser decide

        # Modal de "atribuir/vincular ao advogado"? NÃO confirma: fecha com
        # segurança e aborta este processo (pedido do usuário).
        obs = detectar_atribuicao(self.driver)
        if obs:
            raise AtribuicaoDetectadaError(obs)

        self._click_all_mais_mov()
        return self.driver.page_source
