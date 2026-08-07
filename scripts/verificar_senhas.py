# -*- coding: utf-8 -*-
"""Verifica se as senhas guardadas em config/credenciais.json ainda valem.

Ideia central: NÃO precisamos completar o 2FA para saber se a senha está certa.
Se o sistema aceita usuário+senha e avança para a tela do código (e-mail ou
TOTP), a senha está válida. Se recusa, mostra "usuário/senha inválidos",
"senha expirada" ou "conta bloqueada" — que é exatamente o que queremos saber.
Isso torna a verificação rápida (~20-40s por sistema) e não consome códigos.

Famílias cobertas:
  esaj       — cada tribunal tem conta própria → 1 tentativa por tribunal.
  eproc      — cada instância tem conta própria → 1 tentativa por instância.
  pje (SSO)  — PJe/PJe-JT é login nacional único (PDPJ/Keycloak, sso.cloud.pje).
               Os ~45 registros PJe do arquivo apontam para a MESMA conta, mas
               guardam 5 senhas diferentes; no máximo uma pode estar certa.
               Por isso testamos cada SENHA DISTINTA uma vez (em ordem de
               frequência), parando no primeiro sucesso e respeitando um teto
               de falhas — do contrário a conta é bloqueada por brute force.

Não cobertas (sem automação de login no projeto): Projudi, OAB Nacional,
E-CINT, TJRJ-PJe (exige certificado digital A1/A3).

Uso:
    python scripts/verificar_senhas.py                 # tudo
    python scripts/verificar_senhas.py --familia esaj  # só uma família
    python scripts/verificar_senhas.py --headless
    python scripts/verificar_senhas.py --max-falhas-sso 2
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from organizador.config import CONFIG_ENV_PATH, CREDENCIAIS_PATH
from organizador.integracoes.tjsp_scraper.scrapers.base import criar_chrome_driver

load_dotenv(CONFIG_ENV_PATH)

RELATORIO_PATH = ROOT / "data" / "_debug" / "verificacao_senhas.json"

# ----------------------------------------------------------------------
# Mapa: chave em credenciais.json -> (família, url de login)
# ----------------------------------------------------------------------
# e-SAJ: URL do cpopg; a tela de login (sajcas) é derivada dela.
ESAJ = {
    "TJSP - Esaj": "https://esaj.tjsp.jus.br/cpopg/open.do",
    "TJAM - Esaj": "https://consultasaj.tjam.jus.br/cpopg/open.do",
    "TJMS - Esaj": "https://esaj.tjms.jus.br/cpopg/open.do",
    "TJAL - Esaj": "https://www2.tjal.jus.br/cpopg/open.do",
    "TJCE - Esaj": "https://esaj.tjce.jus.br/cpopg/open.do",
}

# eproc: instâncias independentes (conta e 2FA próprios por instalação).
# As três primeiras vêm de TJ_SISTEMAS (confirmadas em produção); as demais são
# as URLs públicas conhecidas — se o form não renderizar, o resultado sai como
# NAO_VERIFICADO (URL a confirmar), nunca como "senha errada".
EPROC = {
    "TJSP - Eproc":             "https://eproc1g.tjsp.jus.br/eproc/",
    "TJMG - eproc 1 e 2 graus": "https://eproc1g.tjmg.jus.br/eproc/",
    "TJSC - Eproc 1º Grau":     "https://eproc1g.tjsc.jus.br/eproc/",
    "TJSC - Eproc 2º Grau":     "https://eproc2g.tjsc.jus.br/eproc/",
    "TJRS - E-Proc 1º":         "https://eproc1g.tjrs.jus.br/eproc/",
    "TJRS - E-Proc 2º":         "https://eproc2g.tjrs.jus.br/eproc/",
    "TJRJ - Eproc":             "https://eproc1g.tjrj.jus.br/eproc/",
    "TJAC - EPROC":             "https://eproc1g.tjac.jus.br/eproc/",
    "TJTO - E-Proc 1º":         "https://eproc1.tjto.jus.br/eprocV2_prod_1grau/",
    "TJTO - E-Proc 2º":         "https://eproc2.tjto.jus.br/eprocV2_prod_2grau/",
    "TRF2 (JFRJ)":              "https://eproc.trf2.jus.br/eproc/",
    "TRF6 1º Grau":             "https://eproc1g.trf6.jus.br/eproc/",
    "TRF6 2º Grau":             "https://eproc2g.trf6.jus.br/eproc/",
}

# PJe / PJe-JT — login nacional único (PDPJ). Testado num host só.
PJE_LOGIN_URL = "https://pje.tjmg.jus.br/pje/login.seam"

# Ordem de teste das senhas do SSO: primeiro as chaves que o app usa em
# produção (_CRED_POR_ESTADO em scrapers/pje.py) — são as mais prováveis de
# estarem atualizadas. Cada tentativa errada aproxima o bloqueio da conta,
# então a ordem importa mais que a frequência no arquivo.
PJE_PRIORIDADE = ["TJMG - PJE", "TJDF - 1º Grau", "TJCE - PJE - 1º Grau",
                  "TJMA – PJE – 1º Grau", "TJPA - PJE – 1º Grau",
                  "TJPE - PJE – 1º Grau"]

# Sem automação de login disponível hoje.
SEM_COBERTURA = {
    "TJMG Projudi":              "Projudi (sem scraper)",
    "TJPR - Projudi":            "Projudi (sem scraper)",
    "TJGO - Projudi":            "Projudi (sem scraper)",
    "TJAM - Projudi":            "Projudi (sem scraper)",
    "TJBA – Projudi – 1º Grau":  "Projudi (sem scraper)",
    "TJRJ - Projudi":            "Projudi (sem scraper)",
    "TJRJ - 1ª instância":       "PJe TJRJ exige certificado digital",
    "OAB Nacional":              "portal da OAB (sem scraper)",
    "E-CINT":                    "sistema e-CINT (sem scraper)",
}

# Linhas que não são credencial de sistema (anotações coladas na planilha).
LIXO = {"senha robo", "emails possíveis", "senha suporte tjsp",
        "SENHA CERTIFICADO A1: 1718MCSA88"}

# ----------------------------------------------------------------------
# Classificação da resposta da tela de login
# ----------------------------------------------------------------------
RX_INVALIDA = re.compile(
    r"usu[áa]rio\s*(ou|e)\s*senha|senha\s*ou\s*usu[áa]rio|usu[áa]rio\s*inv[áa]lid|"
    r"senha\s*inv[áa]lid|credenciais?\s*inv[áa]lid|"
    r"invalid\s+username|dados\s*de\s*acesso\s*inv[áa]lid|login\s*inv[áa]lid|"
    r"senha\s*incorret|n[ãa]o\s*confere", re.I)
RX_EXPIRADA = re.compile(r"senha\s*expirad|senha\s*venci|expirou|"
                         r"necess[áa]rio\s*(alterar|redefinir)\s*a?\s*senha", re.I)
RX_BLOQUEADA = re.compile(
    r"bloquead|temporariamente\s*desabilitad|conta\s*desativad|"
    r"excedeu\s*o?\s*n[úu]mero|muitas\s*tentativas|inativ[oa]", re.I)

# Campos de código 2FA (e-mail no SAJ; TOTP no Keycloak/eproc).
CAMPOS_2FA = ("tokenInformado", "otp", "txtAcessoCodigo",
              "totp", "codigoAcesso", "acessoCodigo")


def _texto(driver) -> str:
    """Texto da página + URL decodificada.

    A URL entra porque alguns sistemas não mostram o erro no corpo: o eproc do
    TRF2 redireciona para ...&msg=Senha%20ou%20usuário%20inválido. Sem ler a
    URL, uma senha errada saía como INDETERMINADO."""
    from urllib.parse import unquote
    try:
        corpo = driver.find_element(By.TAG_NAME, "body").text or ""
    except Exception:
        corpo = driver.page_source or ""
    try:
        return f"{unquote(driver.current_url or '')}\n{corpo}"
    except Exception:
        return corpo


def _tem_campo_2fa(driver) -> bool:
    for cid in CAMPOS_2FA:
        if driver.find_elements(By.ID, cid):
            return True
    return bool(driver.find_elements(
        By.XPATH, '//input[contains(translate(@id,"OTP","otp"),"otp") or '
                  'contains(translate(@name,"OTP","otp"),"otp")]'))


def _classificar(driver, url_login: str, marcador_logado=None) -> tuple[str, str]:
    """(status, detalhe) após submeter usuário+senha."""
    fim = time.time() + 25
    while time.time() < fim:
        txt = _texto(driver)
        if RX_EXPIRADA.search(txt):
            return "SENHA_EXPIRADA", _trecho(txt, RX_EXPIRADA)
        if RX_BLOQUEADA.search(txt):
            return "CONTA_BLOQUEADA", _trecho(txt, RX_BLOQUEADA)
        if RX_INVALIDA.search(txt):
            return "SENHA_INVALIDA", _trecho(txt, RX_INVALIDA)
        if _tem_campo_2fa(driver):
            return "OK", "senha aceita (chegou na tela do código 2FA)"
        if marcador_logado and marcador_logado(driver):
            return "OK", "senha aceita (entrou sem 2FA — sessão lembrada)"
        time.sleep(1)
    return "INDETERMINADO", f"sem veredito em 25s (url={driver.current_url[:120]})"


def _trecho(txt: str, rx) -> str:
    m = rx.search(txt)
    if not m:
        return ""
    ini = max(0, m.start() - 60)
    return " ".join(txt[ini:m.end() + 60].split())


def _cpf11(login: str) -> str:
    return re.sub(r"\D", "", str(login or "")).zfill(11)


def _cpf_fmt(login: str) -> str:
    d = _cpf11(login)
    return f"{d[0:3]}.{d[3:6]}.{d[6:9]}-{d[9:11]}"


# ----------------------------------------------------------------------
# Verificadores por família
# ----------------------------------------------------------------------
def verificar_esaj(driver, url_cpopg: str, login: str, senha: str) -> tuple[str, str]:
    from urllib.parse import quote, urlsplit
    p = urlsplit(url_cpopg)
    url = f"{p.scheme}://{p.netloc}/sajcas/login?service={quote(url_cpopg, safe='')}"
    driver.get(url)
    try:
        WebDriverWait(driver, 40).until(
            EC.visibility_of_element_located((By.ID, "passwordForm")))
    except Exception:
        return "NAO_VERIFICADO", "form de login (SPA) não renderizou em 40s"

    driver.find_element(By.ID, "usernameForm").send_keys(_cpf_fmt(login))
    driver.find_element(By.ID, "passwordForm").send_keys(senha)
    botao = driver.find_elements(By.ID, "pbEntrar") or driver.find_elements(
        By.XPATH, '//button[normalize-space()="Entrar"]')
    if not botao:
        return "NAO_VERIFICADO", "botão Entrar não encontrado"
    botao[0].click()

    # ATENÇÃO: o campo de busca do cpopg (#numeroDigitoAnoUnificado) existe
    # TAMBÉM sem login — usá-lo como prova de sessão dava falso "OK". A prova
    # real é ter saído do sajcas e o portal não oferecer mais "Identificar-se".
    def _autenticado(d):
        try:
            u = (d.current_url or "").lower()
            if "sajcas" in u:
                return False
            corpo = d.find_element(By.TAG_NAME, "body").text or ""
            return "identificar-se" not in corpo.lower()
        except Exception:
            return False

    return _classificar(driver, url, marcador_logado=_autenticado)


def verificar_eproc(driver, url_base: str, login: str, senha: str) -> tuple[str, str]:
    driver.get(url_base)
    # Duas variantes: login interno do eproc e Keycloak SSO.
    par = None
    for uid, pid, sid in (("txtUsuario", "pwdSenha", "sbmEntrar"),
                          ("username", "password", "kc-login")):
        try:
            WebDriverWait(driver, 20).until(
                EC.visibility_of_element_located((By.ID, uid)))
            par = (uid, pid, sid)
            break
        except Exception:
            continue
    if not par:
        return "NAO_VERIFICADO", f"form de login não renderizou (url={driver.current_url[:120]})"

    uid, pid, sid = par
    driver.find_element(By.ID, uid).send_keys(_cpf11(login))
    driver.find_element(By.ID, pid).send_keys(senha)
    alvo = driver.find_elements(By.ID, sid) or driver.find_elements(
        By.XPATH, '//button[@type="submit"] | //input[@type="submit"]')
    if not alvo:
        return "NAO_VERIFICADO", "botão Entrar não encontrado"
    alvo[0].click()
    return _classificar(driver, url_base)


def verificar_pje(driver, login: str, senha: str) -> tuple[str, str]:
    driver.get(PJE_LOGIN_URL)
    try:
        WebDriverWait(driver, 40).until(
            EC.visibility_of_element_located((By.ID, "password")))
    except Exception:
        return "NAO_VERIFICADO", "Keycloak do PJe não renderizou em 40s"
    driver.find_element(By.ID, "username").send_keys(_cpf11(login))
    driver.find_element(By.ID, "password").send_keys(senha)
    driver.find_element(By.ID, "kc-login").click()
    return _classificar(
        driver, PJE_LOGIN_URL,
        marcador_logado=lambda d: "pje.tjmg.jus.br" in (d.current_url or "")
        and "sso.cloud.pje" not in (d.current_url or ""))


# ----------------------------------------------------------------------
# Orquestração
# ----------------------------------------------------------------------
def carregar() -> list[dict]:
    dados = json.loads(CREDENCIAIS_PATH.read_text(encoding="utf-8"))["credenciais"]
    return [c for c in dados if str(c.get("tj", "")).strip() not in LIXO]


def familia(chave: str) -> str:
    if chave in ESAJ:
        return "esaj"
    if chave in EPROC:
        return "eproc"
    if chave in SEM_COBERTURA:
        return "sem_cobertura"
    return "pje"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--familia", choices=["esaj", "eproc", "pje"], action="append")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--max-falhas-sso", type=int, default=3,
                    help="teto de senhas erradas testadas no SSO do PJe "
                         "(proteção contra bloqueio da conta)")
    ap.add_argument("--pausa", type=float, default=8.0,
                    help="segundos entre tentativas")
    args = ap.parse_args()
    familias = set(args.familia or ["esaj", "eproc", "pje"])

    creds = carregar()
    por_chave = {str(c["tj"]).strip(): c for c in creds}
    resultados: list[dict] = []

    def registra(chave, fam, status, detalhe, extra=None):
        linha = {"sistema": chave, "familia": fam, "status": status,
                 "detalhe": detalhe}
        if extra:
            linha.update(extra)
        resultados.append(linha)
        icone = {"OK": "✅", "SENHA_INVALIDA": "❌", "SENHA_EXPIRADA": "⚠️",
                 "CONTA_BLOQUEADA": "🚫"}.get(status, "❔")
        print(f"{icone} {chave}: {status} — {detalhe}", flush=True)

    driver = criar_chrome_driver(headless=args.headless)
    try:
        # ---------- e-SAJ: uma conta por tribunal ----------
        if "esaj" in familias:
            print("\n===== e-SAJ =====", flush=True)
            for chave, url in ESAJ.items():
                c = por_chave.get(chave)
                if not c:
                    registra(chave, "esaj", "SEM_CREDENCIAL", "não está no arquivo")
                    continue
                try:
                    st, det = verificar_esaj(driver, url, c["login"], c["senha"])
                except Exception as e:
                    st, det = "ERRO", f"{type(e).__name__}: {str(e)[:140]}"
                registra(chave, "esaj", st, det)
                driver.delete_all_cookies()
                time.sleep(args.pausa)

        # ---------- eproc: uma conta por instância ----------
        if "eproc" in familias:
            print("\n===== eproc =====", flush=True)
            for chave, url in EPROC.items():
                c = por_chave.get(chave)
                if not c:
                    registra(chave, "eproc", "SEM_CREDENCIAL", "não está no arquivo")
                    continue
                try:
                    st, det = verificar_eproc(driver, url, c["login"], c["senha"])
                except Exception as e:
                    st, det = "ERRO", f"{type(e).__name__}: {str(e)[:140]}"
                registra(chave, "eproc", st, det, {"url": url})
                driver.delete_all_cookies()
                time.sleep(args.pausa)

        # ---------- PJe: conta ÚNICA (PDPJ) ----------
        if "pje" in familias:
            print("\n===== PJe / PJe-JT (login nacional PDPJ) =====", flush=True)
            pje_itens = [c for c in creds if familia(str(c["tj"]).strip()) == "pje"]
            grupos: dict[tuple[str, str], list[str]] = {}
            for c in pje_itens:
                grupos.setdefault((str(c["login"]).strip(), str(c["senha"]).strip()),
                                  []).append(str(c["tj"]).strip())
            # ordem: senha usada em produção primeiro; depois a mais frequente.
            def _rank(item):
                (_login, _senha), sistemas = item
                pri = min((PJE_PRIORIDADE.index(s) for s in sistemas
                           if s in PJE_PRIORIDADE), default=len(PJE_PRIORIDADE))
                return (pri, -len(sistemas))

            ordem = sorted(grupos.items(), key=_rank)
            print(f"{len(pje_itens)} registros PJe → {len(ordem)} senhas distintas; "
                  f"testando no máximo {args.max_falhas_sso} erradas.", flush=True)

            senha_boa = None
            falhas = 0
            veredito: dict[tuple[str, str], tuple[str, str]] = {}
            for (login, senha), sistemas in ordem:
                if senha_boa is not None or falhas >= args.max_falhas_sso:
                    veredito[(login, senha)] = (
                        "NAO_TESTADA",
                        "não testada (senha correta já encontrada)" if senha_boa
                        else "não testada (teto de tentativas p/ evitar bloqueio)")
                    continue
                print(f"\n-- testando senha usada em {len(sistemas)} registros "
                      f"(ex.: {sistemas[0]}) --", flush=True)
                try:
                    st, det = verificar_pje(driver, login, senha)
                except Exception as e:
                    st, det = "ERRO", f"{type(e).__name__}: {str(e)[:140]}"
                veredito[(login, senha)] = (st, det)
                print(f"   → {st}: {det}", flush=True)
                if st == "OK":
                    senha_boa = (login, senha)
                elif st in ("SENHA_INVALIDA", "SENHA_EXPIRADA", "CONTA_BLOQUEADA"):
                    falhas += 1
                if st == "CONTA_BLOQUEADA":
                    print("🚫 conta bloqueada — interrompendo o PJe.", flush=True)
                    break
                driver.delete_all_cookies()
                time.sleep(max(args.pausa, 15))

            for (login, senha), sistemas in ordem:
                st, det = veredito.get((login, senha), ("NAO_TESTADA", "não testada"))
                if senha_boa and (login, senha) != senha_boa and st == "NAO_TESTADA":
                    st = "SENHA_DESATUALIZADA"
                    det = ("senha diferente da que funcionou no SSO nacional — "
                           "registro desatualizado no arquivo")
                for s in sistemas:
                    registra(s, "pje", st, det)

        # ---------- sem cobertura ----------
        for chave, motivo in SEM_COBERTURA.items():
            if chave in por_chave:
                registra(chave, "sem_cobertura", "NAO_VERIFICADO", motivo)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    print("\n========== RESUMO ==========", flush=True)
    cont = Counter(r["status"] for r in resultados)
    for st, n in cont.most_common():
        print(f"  {st}: {n}", flush=True)
    ruins = [r for r in resultados
             if r["status"] in ("SENHA_INVALIDA", "SENHA_EXPIRADA",
                                "CONTA_BLOQUEADA", "SENHA_DESATUALIZADA")]
    if ruins:
        print("\nSistemas com senha a corrigir:", flush=True)
        for r in ruins:
            print(f"  - {r['sistema']} ({r['status']})", flush=True)

    RELATORIO_PATH.parent.mkdir(parents=True, exist_ok=True)
    RELATORIO_PATH.write_text(json.dumps(
        {"gerado_em": datetime.now().isoformat(timespec="seconds"),
         "resultados": resultados}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nRelatório: {RELATORIO_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
