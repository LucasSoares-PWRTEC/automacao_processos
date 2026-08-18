# -*- coding: utf-8 -*-
"""Scraper do Projudi TJGO — rota AUTENTICADA (login da OAB do escritório).

POR QUE NÃO A CONSULTA PÚBLICA
------------------------------
`projudi.tjgo.jus.br/BuscaProcesso` sem login é protegido por Cloudflare
Turnstile em modo INTERATIVO ("Confirme que é humano") e o botão Buscar nasce
`disabled` até o token chegar; um navegador dirigido ainda recebe "O sistema de
proteção identificou um comportamento incomum e bloqueou a solicitação"
(verificado ao vivo em 18/08/2026). Contornar esse gate está fora de escopo —
e é desnecessário: a MESMA busca, autenticada, não tem Turnstile nenhum.

FLUXO (validado ao vivo, 18/08/2026, processo 5238949-70.2018.8.09.0162)
-----------------------------------------------------------------------
  1. GET /                      -> form ``#login`` / ``#senha``
  2. clique em ``input[name=entrar]``          (chama efetuarLoginAjax())
  3. tela de OTP ``#blocoOtp``: 6 caixas ``input.otp-box`` + botão
     "Validar Código e Entrar". O código chega por E-MAIL e vale 5 min.
  4. pós-login -> ``Usuario?PaginaAtual=9`` = "Serventias Disponíveis": uma
     ``<fieldset>`` por OAB estadual, com ``<legend>Ordem dos Advogados do
     Brasil GO - GO</legend>``. É preciso escolher a do estado do processo.
  5. GET /BuscaProcesso (SEM parâmetros) -> form com ``#ProcessoNumero`` e
     ``#btnBuscar``, já sem Turnstile.
  6. buscar pelo CNJ completo cai DIRETO na página do processo
     (``body#corpo_dados_processo``); número inexistente abre um diálogo
     "Nenhum Processo foi localizado.".

CUIDADO COM O OTP — NUNCA REENVIAR
----------------------------------
O Projudi limita os reenvios e a própria tela avisa: esgotado o limite, "seu
usuário será bloqueado temporariamente". Este scraper JAMAIS clica em "Reenviar
Código" e nunca repete o login em laço: se o código não vier, levanta erro e
para. Por isso também vale a regra geral do projeto — UM login por sessão,
dezenas de consultas (ver `scraper._consultar_e_extrair`).

O perfil persistente NÃO evita o OTP (testado): o Projudi pede o código a cada
login novo, então não adianta guardar sessão em disco.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from ..credenciais import obter_credenciais
from ..diagnostico import salvar_screenshot
from ..two_factor import gerar_codigo_totp, obter_codigo_2fa_email
from .base import TribunalScraper, criar_chrome_driver

_URL_PADRAO = "https://projudi.tjgo.jus.br/"

# --- OTP por e-mail (remetente/assunto/corpo confirmados ao vivo) -----------
# O código do Projudi chega ENCAMINHADO pelo Acessos Tribunais — o mesmo
# remetente do 2FA do e-SAJ (ver esaj.py). Por isso o filtro de assunto
# "[PROJUDI]" é obrigatório: sem ele, um código do e-SAJ pode ser usado aqui.
REMETENTE_2FA = "acesso.tribunais@mcsarc.com.br"
ASSUNTO_2FA = "[PROJUDI]"
# O corpo é "...concluir sua operação: 273131 Este código é válido...". Ancorar
# na frase importa: o corpo também traz a hora de expiração e o texto de aviso,
# e um `\d{6}` solto pega o número errado com facilidade.
RX_CODIGO = re.compile(
    r"conclui?r\s+sua\s+opera[çc][ãa]o[:\s]*([0-9A-Za-z]{6})\b", re.I
)
TIMEOUT_2FA_SEG = int(os.getenv("PROJUDI_2FA_TIMEOUT_SEG", "180"))
# Chave TOTP de reserva no config/.env, para contas que usem app autenticador
# em vez de e-mail.
ENV_TOTP = "TOTP_PROJUDI"
# Última reserva: com navegador VISÍVEL, dá tempo de digitar o código na tela.
# Em headless não faz sentido esperar por alguém, então o valor é ignorado lá.
ESPERA_OTP_MANUAL_SEG = int(os.getenv("PROJUDI_OTP_MANUAL_SEG", "120"))


def _sem_tags(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or ""))


class ProjudiScraper(TribunalScraper):
    """Projudi TJGO. `perfil` é a sigla da OAB a selecionar ("GO")."""

    def __init__(self, estado: str, url: str = "", headless: bool = False,
                 perfil: Optional[str] = None):
        super().__init__(estado=estado, url=(url or _URL_PADRAO), headless=headless)
        self.base = (url or _URL_PADRAO).rstrip("/") + "/"
        self.perfil = (perfil or "GO").upper()
        self._tj_chave = f"TJ{self.perfil} - Projudi"
        self.driver = criar_chrome_driver(headless=headless)
        self.wait = WebDriverWait(self.driver, 20)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _screenshot(self, nome: str) -> None:
        """No-op sem SCRAPER_DEBUG=1 (ver ../diagnostico.py)."""
        salvar_screenshot(self.driver, "projudi", nome)

    def _visivel(self, by, valor: str) -> bool:
        try:
            els = self.driver.find_elements(by, valor)
            return bool(els) and els[0].is_displayed()
        except Exception:
            return False

    def _esperar(self, condicao, timeout: float) -> bool:
        """True quando `condicao()` vira verdadeira dentro do timeout."""
        fim = time.time() + timeout
        while time.time() < fim:
            if condicao():
                return True
            time.sleep(0.5)
        return False

    # ------------------------------------------------------------------
    # LOGIN + OTP + perfil
    # ------------------------------------------------------------------
    def login(self) -> None:
        if self._logado:
            return

        login, senha = obter_credenciais(self._tj_chave)
        print(f"🔐 Projudi [{self._tj_chave}]: logando...")
        self.driver.get(self.base)

        try:
            WebDriverWait(self.driver, 30).until(
                EC.visibility_of_element_located((By.ID, "senha"))
            )
        except Exception:
            # Já autenticado (sessão viva no navegador) — segue para o perfil.
            if self._tela_de_serventias():
                self._escolher_serventia()
                self._logado = True
                return
            self._screenshot("login_form_nao_renderizou")
            raise RuntimeError(
                "Projudi: o formulário de login não apareceu em 30s."
            )

        self.driver.find_element(By.ID, "login").send_keys(login)
        self.driver.find_element(By.ID, "senha").send_keys(senha)
        # Marca o instante ANTES de submeter: o leitor de e-mail só aceita
        # códigos posteriores a ele. A folga de 90s cobre o atraso entre o
        # relógio do servidor de e-mail e o desta máquina.
        desde = datetime.now(timezone.utc) - timedelta(seconds=90)
        self.driver.find_element(By.CSS_SELECTOR, "input[name='entrar']").click()

        self._esperar(
            lambda: self._visivel(By.ID, "blocoOtp") or not self._visivel(By.ID, "senha"),
            45,
        )

        if self._visivel(By.ID, "blocoOtp"):
            self._resolver_otp(desde)

        if not self._esperar(self._tela_de_serventias, 45):
            if self._visivel(By.ID, "senha") or self._visivel(By.ID, "blocoOtp"):
                self._screenshot("login_nao_concluiu")
                raise RuntimeError(
                    "Projudi: login não concluiu (a tela de login/OTP continua "
                    "na frente). Confira usuário e senha em config/credenciais.json."
                )
            # Logou e caiu direto num perfil (conta com uma serventia só).
            self._logado = True
            print("✅ Projudi: login concluído.")
            return

        self._escolher_serventia()
        self._logado = True
        print("✅ Projudi: login concluído.")

    def _resolver_otp(self, desde: datetime) -> None:
        """Preenche e valida o código de 6 caracteres. NUNCA pede reenvio."""
        if self._visivel(By.ID, "textoBloqueioTotal"):
            texto = self.driver.find_element(By.ID, "textoBloqueioTotal").text
            self._screenshot("otp_usuario_bloqueado")
            raise RuntimeError(
                "Projudi: o usuário está BLOQUEADO temporariamente por excesso "
                f"de tentativas de OTP. Espere o prazo indicado pelo site. [{texto[:160]}]"
            )

        codigo = self._codigo_otp(desde)
        if not codigo:
            self._screenshot("otp_sem_codigo")
            raise RuntimeError(
                "Projudi: não obtive o código OTP (nem por e-mail, nem por "
                f"{ENV_TOTP}). NÃO pedi reenvio para não arriscar o bloqueio do "
                "usuário — tente de novo daqui a pouco."
            )

        caixas = self.driver.find_elements(By.CSS_SELECTOR, "input.otp-box")
        if len(caixas) < len(codigo):
            self._screenshot("otp_caixas_ausentes")
            raise RuntimeError("Projudi: campos do OTP não encontrados na tela.")
        for caixa, char in zip(caixas, codigo):
            caixa.send_keys(char)

        botao = self.driver.find_elements(
            By.XPATH, "//input[@value='Validar Código e Entrar']"
        )
        if not botao:
            self._screenshot("otp_botao_validar_ausente")
            raise RuntimeError("Projudi: botão de validação do OTP não encontrado.")
        botao[0].click()
        self._esperar(lambda: not self._visivel(By.ID, "blocoOtp"), 60)

    def _codigo_otp(self, desde: datetime) -> str:
        """E-mail (IMAP) → TOTP do .env → digitação manual (só com tela visível)."""
        try:
            print(f"📧 Projudi: buscando o código no e-mail (de {REMETENTE_2FA})...")
            codigo = obter_codigo_2fa_email(
                desde=desde,
                remetente=REMETENTE_2FA,
                assunto_contem=ASSUNTO_2FA,
                regex_codigo=RX_CODIGO,
                timeout_seg=TIMEOUT_2FA_SEG,
                intervalo_seg=5,
                preprocessar=_sem_tags,  # o e-mail do Projudi é só HTML
            )
            if codigo:
                print("🔢 Projudi: código recebido por e-mail.")
                return codigo
        except Exception as exc:
            print(f"⚠️ Projudi: leitura do e-mail falhou ({exc}).")

        if os.getenv(ENV_TOTP):
            try:
                codigo = gerar_codigo_totp(ENV_TOTP)
                print(f"🔢 Projudi: usando o TOTP de {ENV_TOTP}.")
                return codigo
            except Exception as exc:
                print(f"⚠️ Projudi: {ENV_TOTP} falhou ({exc}).")

        if not self.headless and ESPERA_OTP_MANUAL_SEG > 0:
            print(
                f"⌨️  Projudi: digite o código na janela do navegador — "
                f"aguardo {ESPERA_OTP_MANUAL_SEG}s."
            )
            if self._esperar(lambda: not self._visivel(By.ID, "blocoOtp"),
                             ESPERA_OTP_MANUAL_SEG):
                print("✅ Projudi: OTP validado manualmente.")
        return ""

    def _tela_de_serventias(self) -> bool:
        try:
            return "Serventias Disponíveis" in (self.driver.page_source or "")
        except Exception:
            return False

    def _escolher_serventia(self) -> None:
        """Clica na OAB do estado pedido ("Ordem dos Advogados do Brasil GO - GO").

        O link carrega um id de cadastro (``Usuario?PaginaAtual=7&a1=93304``) que
        é específico da CONTA — tem de ser descoberto na hora, nunca fixado."""
        alvo = re.compile(rf"\b{re.escape(self.perfil)}\s*-\s*{re.escape(self.perfil)}\b", re.I)
        for fieldset in self.driver.find_elements(By.TAG_NAME, "fieldset"):
            try:
                legenda = fieldset.find_element(By.TAG_NAME, "legend").text
            except Exception:
                continue
            if not alvo.search(legenda or ""):
                continue
            links = fieldset.find_elements(By.TAG_NAME, "a")
            if not links:
                continue
            print(f"👤 Projudi: perfil {legenda.strip()} — {links[0].text.strip()}")
            links[0].click()
            self._esperar(lambda: not self._tela_de_serventias(), 30)
            return

        self._screenshot("serventia_do_perfil_ausente")
        raise RuntimeError(
            f"Projudi: não achei a serventia da OAB-{self.perfil} na tela de "
            "'Serventias Disponíveis'. Confira se a conta tem inscrição nesse estado."
        )

    # ------------------------------------------------------------------
    # CONSULTA
    # ------------------------------------------------------------------
    def consultar_processo(self, numero_processo: str) -> Optional[str]:
        if not self._logado:
            self.login()

        numero = str(numero_processo).strip()
        self.driver.get(self.base + "BuscaProcesso")

        try:
            campo = WebDriverWait(self.driver, 20).until(
                EC.visibility_of_element_located((By.ID, "ProcessoNumero"))
            )
        except Exception:
            self._screenshot("busca_campo_numero_ausente")
            print("⚠️ Projudi: campo de busca não apareceu.")
            return None

        campo.clear()
        campo.send_keys(numero)

        botoes = self.driver.find_elements(By.ID, "btnBuscar")
        if not botoes:
            self._screenshot("busca_botao_ausente")
            print("⚠️ Projudi: botão Buscar não encontrado.")
            return None
        if botoes[0].get_attribute("disabled"):
            # Só a busca PÚBLICA nasce desabilitada (Turnstile). Se caímos aqui,
            # a sessão morreu e o Projudi devolveu a tela pública.
            self._screenshot("busca_botao_desabilitado")
            print("⚠️ Projudi: a busca voltou na versão pública (sessão expirada).")
            return None
        botoes[0].click()

        achou = self._esperar(
            lambda: bool(self.driver.find_elements(By.ID, "corpo_dados_processo"))
            or "nenhum processo foi localizado" in (self.driver.page_source or "").lower(),
            30,
        )
        if not achou:
            self._screenshot("busca_sem_desfecho")
            return self.driver.page_source  # devolve o que tiver; o parser decide

        if not self.driver.find_elements(By.ID, "corpo_dados_processo"):
            print(f"ℹ️ Projudi: processo {numero} não localizado no TJ{self.perfil}.")
            return self.driver.page_source  # contém o marcador de "não encontrado"

        print("📋 Projudi: página do processo carregada.")
        return self.driver.page_source
