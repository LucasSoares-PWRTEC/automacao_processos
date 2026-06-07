# src/two_factor.py
"""
2FA em dois modos:

1. Email (IMAP): lê o código que chega na caixa de entrada.
   Genérico — remetente/assunto/regex são parâmetros, configurados
   por tribunal quando o fluxo manual de cada um for mapeado.

2. TOTP (app autenticador): gera o código a partir da chave secreta
   armazenada no .env (variáveis TOTP_*).
"""

import os
import re
import time
import email
import email.utils
import imaplib
import urllib.request
from datetime import datetime, timezone

import pyotp
from email.message import Message
from email.header import decode_header
from datetime import datetime, timezone, timedelta
from typing import Optional


# Regex padrão: 6 dígitos isolados. Ajuste por tribunal se o formato for outro.
RX_CODIGO_PADRAO = re.compile(r"\b(\d{6})\b")


def _imap_conectar() -> imaplib.IMAP4_SSL:
    host = os.getenv("IMAP_HOST", "imap.gmail.com")
    port = int(os.getenv("IMAP_PORT", "993"))
    user = os.getenv("IMAP_USER")
    senha = os.getenv("IMAP_PASS")

    if not user or not senha:
        raise RuntimeError(
            "IMAP_USER / IMAP_PASS ausentes no .env. "
            "Configure as credenciais antes de usar o 2FA por email."
        )

    conn = imaplib.IMAP4_SSL(host, port)
    conn.login(user, senha)
    return conn


def _decodificar(valor: str) -> str:
    """Decodifica cabeçalhos MIME (assunto/remetente com acento)."""
    partes = decode_header(valor or "")
    out = ""
    for texto, enc in partes:
        if isinstance(texto, bytes):
            out += texto.decode(enc or "utf-8", errors="ignore")
        else:
            out += texto
    return out


def _extrair_corpo(msg: Message) -> str:
    """Extrai o texto do email (prioriza text/plain, cai para text/html)."""
    if msg.is_multipart():
        plain, html = "", ""
        for parte in msg.walk():
            ctype = parte.get_content_type()
            disp = str(parte.get("Content-Disposition") or "")
            if "attachment" in disp:
                continue
            try:
                payload = parte.get_payload(decode=True)
                if payload is None:
                    continue
                charset = parte.get_content_charset() or "utf-8"
                conteudo = payload.decode(charset, errors="ignore")
            except Exception:
                continue
            if ctype == "text/plain":
                plain += conteudo
            elif ctype == "text/html":
                html += conteudo
        return plain or html

    payload = msg.get_payload(decode=True)
    if payload is None:
        return ""
    charset = msg.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="ignore")


def obter_codigo_2fa_email(
    desde: datetime,
    remetente: Optional[str] = None,
    assunto_contem: Optional[str] = None,
    regex_codigo: re.Pattern = RX_CODIGO_PADRAO,
    timeout_seg: int = 120,
    intervalo_seg: int = 5,
    pasta: str = "INBOX",
) -> Optional[str]:
    """
    Aguarda e retorna o código 2FA recebido por email.

    IMPORTANTE: `desde` deve ser o instante (UTC) capturado JÁ no momento
    em que o login foi disparado. Só emails que chegarem DEPOIS desse
    instante são considerados — isso evita pegar um código antigo/expirado.

    Parâmetros:
      desde          -> datetime (preferir timezone-aware UTC) do disparo do login
      remetente      -> filtra pelo From (substring, ex: "no-reply@tjsp.jus.br")
      assunto_contem -> filtra pelo Subject (substring, case-insensitive)
      regex_codigo   -> padrão para extrair o código do corpo
      timeout_seg    -> tempo máximo aguardando o email chegar
      intervalo_seg  -> intervalo entre tentativas de leitura
      pasta          -> pasta IMAP (padrão INBOX)

    Retorna o código (str) ou None se estourar o timeout.
    """
    if desde.tzinfo is None:
        desde = desde.replace(tzinfo=timezone.utc)

    fim = time.time() + timeout_seg
    conn = _imap_conectar()

    try:
        # IMAP SEARCH SINCE só tem granularidade de DIA; filtramos a data/hora
        # exata no Python comparando o header Date de cada mensagem.
        data_busca = (desde - timedelta(days=1)).strftime("%d-%b-%Y")

        while time.time() < fim:
            conn.select(pasta)
            status, dados = conn.search(None, f'(SINCE "{data_busca}")')
            if status != "OK":
                time.sleep(intervalo_seg)
                continue

            ids = dados[0].split()
            # Do mais recente para o mais antigo
            for msg_id in reversed(ids):
                status, raw = conn.fetch(msg_id, "(RFC822)")
                if status != "OK" or not raw or not raw[0]:
                    continue

                msg = email.message_from_bytes(raw[0][1])

                # Filtra por data real do email
                data_msg = email.utils.parsedate_to_datetime(msg.get("Date"))
                if data_msg is None:
                    continue
                if data_msg.tzinfo is None:
                    data_msg = data_msg.replace(tzinfo=timezone.utc)
                if data_msg < desde:
                    continue

                # Filtra remetente
                if remetente:
                    de = _decodificar(msg.get("From", "")).lower()
                    if remetente.lower() not in de:
                        continue

                # Filtra assunto
                if assunto_contem:
                    assunto = _decodificar(msg.get("Subject", "")).lower()
                    if assunto_contem.lower() not in assunto:
                        continue

                corpo = _extrair_corpo(msg)
                m = regex_codigo.search(corpo)
                if m:
                    return m.group(1) if m.groups() else m.group(0)

            time.sleep(intervalo_seg)

        return None  # timeout sem encontrar o código

    finally:
        try:
            conn.logout()
        except Exception:
            pass


# ---------------------------------------------------------------------
# 2FA via TOTP (app autenticador)
# ---------------------------------------------------------------------
# Correção de hora via rede: TOTP depende do relógio. Se o relógio da
# máquina dessincronizar (visto: -179s), todo código fica inválido.
# Calculamos o offset (hora_servidor - hora_local) uma vez e o
# recalculamos a cada _OFFSET_TTL_S. Falha de rede → offset 0 (usa hora
# local), sem derrubar a automação.
_OFFSET_TTL_S = 600  # revalida o offset a cada 10 min
_offset_cache = {"valor": 0.0, "ts": 0.0, "ok": False}

# Endpoints com header Date confiável (tenta em ordem)
_TIME_URLS = ("https://www.google.com", "https://www.cloudflare.com")


def _obter_offset_rede() -> float:
    """Retorna (hora_servidor - hora_local) em segundos; 0.0 se rede falhar."""
    agora = time.time()
    if _offset_cache["ok"] and (agora - _offset_cache["ts"]) < _OFFSET_TTL_S:
        return _offset_cache["valor"]

    for url in _TIME_URLS:
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=8) as r:
                servidor = email.utils.parsedate_to_datetime(r.headers["Date"])
            local = datetime.now(timezone.utc)
            offset = (servidor - local).total_seconds()
            _offset_cache.update(valor=offset, ts=agora, ok=True)
            if abs(offset) >= 10:
                print(
                    f"⏰ Relógio local desalinhado em {offset:+.0f}s — "
                    f"TOTP usando hora corrigida via rede."
                )
            return offset
        except Exception:
            continue

    # Sem rede: não quebra; usa hora local (melhor esforço)
    print("⚠️ Não consegui hora de rede; TOTP usará o relógio local.")
    _offset_cache.update(valor=0.0, ts=agora, ok=False)
    return 0.0


def gerar_codigo_totp(env_var: str) -> str:
    """
    Gera o código TOTP (6 dígitos) a partir da chave secreta no .env,
    usando hora corrigida via rede (resiliente a relógio desalinhado).

    env_var -> nome da variável no .env, ex: "TOTP_EPROC_TJSP"

    Levanta RuntimeError se a variável não existir ou a chave for inválida.
    """
    secret = os.getenv(env_var)
    if not secret:
        raise RuntimeError(
            f"Variável {env_var} ausente no .env. "
            f"Verifique o nome ou cadastre a chave TOTP correspondente."
        )

    try:
        offset = _obter_offset_rede()
        instante = datetime.now(timezone.utc).timestamp() + offset
        return pyotp.TOTP(secret.strip()).at(int(instante))
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(
            f"Chave TOTP inválida em {env_var}: {e}. "
            f"Confirme se é uma chave base32 válida."
        ) from e
