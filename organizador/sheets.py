# -*- coding: utf-8 -*-
"""Ingestão dos processos a partir das planilhas Google (somente leitura).

Adaptado do ``sync_sheets.py`` de referência: usa uma CONTA DE SERVIÇO
(``token_google_api.json`` na raiz, apontado por ``GOOGLE_SA_PATH``) com escopos
*.readonly — as planilhas NUNCA são alteradas. Cada planilha precisa estar
compartilhada como *Leitor* com o e-mail da conta de serviço (impresso ao rodar).

Suporta planilhas nativas do Google (exporta .xlsx) e .xlsx subidos ao Drive,
com múltiplas abas (escolhe a de mais CNJs) e cabeçalho deslocado. Devolve os
números já no formato canônico do app (``RE_NUMERO_CNJ``), deduplicados.

Uso programático:
    from organizador import sheets
    regs = sheets.coletar_processos_das_planilhas()   # [{"numero", "campanha"}]
"""
from __future__ import annotations

import io
import re
import unicodedata
from typing import Callable

import pandas as pd

from .config import GOOGLE_SA_PATH
from .padroes import RE_NUMERO_CNJ

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]
MIME_GSHEET = "application/vnd.google-apps.spreadsheet"
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_HDR = 8  # linhas do topo em que procuramos o cabeçalho (cabeçalho deslocado)

# campanha (rótulo) -> (file_id do Drive, dica do nome da coluna de processo).
# A dica é só um chute para achar a linha de cabeçalho; a detecção da coluna é
# tolerante (contém 'processo'/'cnj' e, no limite, a coluna com mais CNJs), então
# pequenas variações no cabeçalho não quebram a leitura.
SHEETS: dict[str, tuple[str, str]] = {
    "EMCCAMP Judicial":           ("1GZ1N-dBF1G9FcK_9qj7t-MBy-eEhZW25", "Nº PROCESSO"),
    "Longitude Judicial":         ("1E-WgMiPClhPq9hb6PXvo7PqkCHmZgnq6f2pk2RoEkRw", "Nº PROCESSO"),
    "Cyrela Judicial":            ("17sHrE8igYgTgXHmQVlnbLXKbod6RvOzPq5CfqbMOPC4", "PROCESSO"),
    "Arbore - Judicial":          ("1VbYKZo4tM9_Tg0R68vwpRi4FO2_xVJi_", "Nº PROCESSO"),
    "RSF Judicial":               ("1y3Mm1xkOPLR2BT2uUvciKm6ms0ItB5kY", "Nº PROCESSO"),
    "Movida Judicial":            ("1ECNr7qtztpZPLaXWogGazk9PeO7NhknYS3Q3nsj0MF8", "Nº PROCESSO"),
    "Grupo Super Nosso Judicial": ("1PkdyMHHAXe8w3yeFhmXZOF9kLckoLcRvJk9R6Asm3EA", "PROCESSO"),
    "VIC Judicial":               ("1wlpxK0RZGkLevObMwkPhe-vQQUnco7NwEBBkcvjVct4", "Processo"),
    "BVEP":                       ("1c4_qfDnS0b2O7kwgTz5BJI4ZU10GQJgi", "PROCESSO"),
    "São José Judicial":          ("1n6HDw79nF_m8p0xAI0lWrCkVecRTzZJI-2GXGJr8M_A", "PROCESSO"),
    "Level/Cantupneus":           ("1X8WH_u75OuqrczNlgbFJRdZbAYY99qc5RMBN8LYTIYA", "PROCESSO"),
}


# ---------------------------------------------------------------------
# Acesso ao Google (somente leitura) — import tardio do google-auth
# ---------------------------------------------------------------------
def _sessao():
    if not GOOGLE_SA_PATH.exists():
        raise RuntimeError(
            f"credencial da conta de serviço não encontrada em {GOOGLE_SA_PATH} "
            "(defina GOOGLE_CREDENTIALS_FILE ou coloque token_google_api.json na raiz)."
        )
    from google.oauth2.service_account import Credentials
    from google.auth.transport.requests import AuthorizedSession

    creds = Credentials.from_service_account_file(str(GOOGLE_SA_PATH), scopes=SCOPES)
    return AuthorizedSession(creds), creds.service_account_email


# ---------------------------------------------------------------------
# Normalização de nomes/valores
# ---------------------------------------------------------------------
def _norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().casefold()


def _normalizar_cnj(texto) -> str:
    """Devolve o CNJ no formato canônico do app (RE_NUMERO_CNJ) ou '' se a célula
    não contém um número de processo. Aceita o canônico pontuado e também 20
    dígitos crus (reconstrói a pontuação)."""
    s = str(texto or "")
    m = RE_NUMERO_CNJ.search(s)
    if m:
        return m.group(0)
    d = re.sub(r"\D", "", s)
    if len(d) == 20:
        return f"{d[:7]}-{d[7:9]}.{d[9:13]}.{d[13]}.{d[14:16]}.{d[16:20]}"
    return ""


def _achar_col(df: pd.DataFrame, alvo: str):
    alvo_n = _norm(alvo)
    for c in df.columns:
        if _norm(c) == alvo_n:
            return c
    for c in df.columns:  # fallback: contém
        if alvo_n and (alvo_n in _norm(c) or _norm(c) in alvo_n):
            return c
    return None


def _conta_cnj(serie: pd.Series) -> int:
    return sum(1 for v in serie.dropna() if _normalizar_cnj(v))


def _detectar_col_processo(df: pd.DataFrame, hint: str):
    """Coluna de número de processo: 1) casa a dica; 2) cabeçalho contém
    'processo'/'cnj'; 3) a coluna com mais valores em formato CNJ."""
    c = _achar_col(df, hint)
    if c is not None and _conta_cnj(df[c]) > 0:
        return c
    for col in df.columns:
        cab = _norm(col)
        if ("processo" in cab or "cnj" in cab) and _conta_cnj(df[col]) > 0:
            return col
    melhor, melhor_q = None, 0
    for col in df.columns:
        q = _conta_cnj(df[col])
        if q > melhor_q:
            melhor, melhor_q = col, q
    return melhor if melhor_q >= 2 else None


# ---------------------------------------------------------------------
# Download + leitura da planilha
# ---------------------------------------------------------------------
def _baixar_bytes(s, fid: str) -> io.BytesIO:
    """Baixa o binário .xlsx (planilha nativa é exportada; arquivo é get_media)."""
    meta = s.get(
        f"https://www.googleapis.com/drive/v3/files/{fid}",
        params={"fields": "name,mimeType", "supportsAllDrives": "true"},
    ).json()
    if "error" in meta:
        raise RuntimeError(meta["error"].get("message", str(meta["error"]))[:120])
    if meta.get("mimeType") == MIME_GSHEET:
        r = s.get(
            f"https://www.googleapis.com/drive/v3/files/{fid}/export",
            params={"mimeType": MIME_XLSX},
        )
    else:
        r = s.get(
            f"https://www.googleapis.com/drive/v3/files/{fid}",
            params={"alt": "media", "supportsAllDrives": "true"},
        )
    r.raise_for_status()
    return io.BytesIO(r.content)


def _parse_aba(xl: pd.ExcelFile, sheet: str, hint: str):
    """Acha a linha de cabeçalho (que casa a dica OU contém 'processo'/'cnj') e
    devolve o df; None se a aba não parece ter coluna de processo."""
    bruto = xl.parse(sheet, dtype=str, header=None)
    alvo = _norm(hint)
    for i in range(min(MAX_HDR, len(bruto))):
        linha = [_norm(x) for x in bruto.iloc[i].tolist()]
        casa_hint = any(alvo and (alvo == c or alvo in c or c in alvo) for c in linha)
        casa_generico = any(("processo" in c or "cnj" in c) for c in linha if c)
        if casa_hint or casa_generico:
            df = xl.parse(sheet, dtype=str, header=i)
            df.columns = [str(c) for c in df.columns]
            return df
    return None


def ler_planilha(s, fid: str, hint: str):
    """Varre as abas e devolve (df, col_processo) da que tem mais CNJs válidos
    (ignora capas/dashboards e trata cabeçalho deslocado)."""
    xl = pd.ExcelFile(_baixar_bytes(s, fid))
    melhor_df, melhor_col, melhor_n = None, None, -1
    for sheet in xl.sheet_names:
        df = _parse_aba(xl, sheet, hint)
        if df is None:
            continue
        col = _detectar_col_processo(df, hint)
        if col is None:
            continue
        n = _conta_cnj(df[col])
        if n > melhor_n:
            melhor_df, melhor_col, melhor_n = df, col, n
    if melhor_df is None:
        raise RuntimeError(f"nenhuma aba com coluna de processo (abas: {xl.sheet_names})")
    return melhor_df, melhor_col


# ---------------------------------------------------------------------
# API principal
# ---------------------------------------------------------------------
def coletar_processos_das_planilhas(
    log: Callable[[str], None] = print,
) -> list[dict]:
    """Lê todas as planilhas de SHEETS e devolve ``[{"numero", "campanha"}]``
    deduplicado por CNJ (1ª campanha vence). Somente leitura."""
    s, robot = _sessao()
    log(f"🔑 conta de serviço: {robot} (compartilhe cada planilha como Leitor com ele)")
    registros: list[dict] = []
    vistos: set[str] = set()
    ok = 0
    primeiro_erro = ""
    for campanha, (fid, hint) in SHEETS.items():
        try:
            df, col = ler_planilha(s, fid, hint)
        except Exception as exc:
            if not primeiro_erro:
                primeiro_erro = str(exc)
            log(f"   ❌ {campanha}: {str(exc)[:100]}")
            continue
        ok += 1
        n = 0
        for v in df[col].tolist():
            cnj = _normalizar_cnj(v)
            if not cnj or cnj in vistos:
                continue
            vistos.add(cnj)
            registros.append({"numero": cnj, "campanha": campanha})
            n += 1
        log(f"   📄 {campanha}: {n} processos (coluna '{col}')")
    # Se NENHUMA planilha pôde ser lida, é falha de config (API desabilitada,
    # credencial sem acesso, planilhas não compartilhadas) — não um "em dia".
    # Estoura para o chamador (GUI/CLI) mostrar o erro real em vez de sucesso vazio.
    if ok == 0 and SHEETS:
        raise RuntimeError(
            "nenhuma planilha pôde ser lida — verifique se as APIs Google Drive/"
            f"Sheets estão habilitadas no projeto e se cada planilha foi compartilhada "
            f"como Leitor com {robot}. Primeiro erro: {primeiro_erro[:160]}"
        )
    log(f"📋 total: {len(registros)} processos únicos nas planilhas.")
    return registros
