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
import warnings
from typing import Callable

from .config import GOOGLE_SA_PATH
from .padroes import RE_NUMERO_CNJ

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]
MIME_GSHEET = "application/vnd.google-apps.spreadsheet"
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_HDR = 8  # linhas do topo em que procuramos o cabeçalho (cabeçalho deslocado)
MAX_LINHAS = 50_000  # teto por aba (protege a RAM em máquinas de 4 GB)

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
    "BVEP":                       ("1c4_qfDnS0b2O7kwgTz5BJI4ZU10GQJgi", "Numero Processual"),
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


def _achar_col(cabecalho: list[str], alvo: str) -> int | None:
    """Índice da coluna cujo rótulo casa `alvo` (igual; senão, contém)."""
    alvo_n = _norm(alvo)
    normalizados = [_norm(c) for c in cabecalho]
    for i, c in enumerate(normalizados):
        if c == alvo_n:
            return i
    for i, c in enumerate(normalizados):  # fallback: contém
        if alvo_n and (alvo_n in c or c in alvo_n):
            return i
    return None


def _detectar_col_processo(
    cabecalho: list[str], conta_cnj: Callable[[int], int], hint: str
) -> int | None:
    """Coluna de número de processo: 1) casa a dica; 2) cabeçalho contém
    'processo'/'cnj'; 3) a coluna com mais valores em formato CNJ.

    `conta_cnj(i)` devolve quantos CNJs há na coluna `i` ABAIXO do cabeçalho."""
    i = _achar_col(cabecalho, hint)
    if i is not None and conta_cnj(i) > 0:
        return i
    for i, rotulo in enumerate(cabecalho):
        cab = _norm(rotulo)
        if ("processo" in cab or "cnj" in cab) and conta_cnj(i) > 0:
            return i
    melhor, melhor_q = None, 0
    for i in range(len(cabecalho)):
        q = conta_cnj(i)
        if q > melhor_q:
            melhor, melhor_q = i, q
    return melhor if melhor_q >= 2 else None


# ---------------------------------------------------------------------
# Download + leitura da planilha
# ---------------------------------------------------------------------
def _baixar_bytes(s, fid: str) -> io.BytesIO:
    """Baixa o binário .xlsx (planilha nativa é exportada; arquivo é get_media)."""
    meta = s.get(
        f"https://www.googleapis.com/drive/v3/files/{fid}",
        params={"fields": "name,mimeType", "supportsAllDrives": "true"},
        timeout=30,
    ).json()
    if "error" in meta:
        raise RuntimeError(meta["error"].get("message", str(meta["error"]))[:120])
    if meta.get("mimeType") == MIME_GSHEET:
        r = s.get(
            f"https://www.googleapis.com/drive/v3/files/{fid}/export",
            params={"mimeType": MIME_XLSX},
            timeout=30,
        )
    else:
        r = s.get(
            f"https://www.googleapis.com/drive/v3/files/{fid}",
            params={"alt": "media", "supportsAllDrives": "true"},
            timeout=30,
        )
    r.raise_for_status()
    return io.BytesIO(r.content)


def _valor_celula(v) -> str:
    """Célula → str, sem notação científica para inteiros exatos.

    CUIDADO com o limite do float: o openpyxl devolve célula numérica de 20
    dígitos como ``float``, e um float64 só representa inteiros exatos até
    2**53. Converter ``4.008846102025826e+19`` com ``int()`` produziria um número
    plausível mas ERRADO, que o ``_normalizar_cnj`` aceitaria como CNJ válido —
    pior que não ler nada. Acima do limite devolvemos a forma científica, que
    não casa como CNJ (é o que o pandas fazia, e por isso a troca de motor não
    mudou nenhum resultado nas 11 planilhas reais).

    Na prática os CNJs das planilhas do escritório são TEXTO; este ramo é defesa.
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v)) if abs(v) < 2 ** 53 else repr(v)
    return str(v)


def _grade(ws, log: Callable[[str], None]) -> list[list[str]]:
    """Materializa a aba como lista de linhas de str, UMA passada só."""
    grade: list[list[str]] = []
    for i, linha in enumerate(ws.iter_rows(values_only=True)):
        if i >= MAX_LINHAS:
            log(f"   ⚠️ aba '{ws.title}': truncada em {MAX_LINHAS} linhas.")
            break
        grade.append([_valor_celula(v) for v in linha])
    return grade


def _mapa_cnj(grade: list[list[str]]) -> tuple[int, list[int], list[list[int]]]:
    """Pré-computa, por coluna: o total de CNJs e quantos estão nas primeiras
    linhas (prefixo até MAX_HDR).

    Isto é o que substitui o custo do antigo laço com pandas: a regex de CNJ roda
    UMA vez por célula, e a contagem para cada cabeçalho candidato (0..MAX_HDR)
    sai por subtração de prefixo, em vez de reparsear a aba 9 vezes."""
    n_col = max((len(l) for l in grade), default=0)
    total = [0] * n_col
    # prefixo[i][h] = CNJs da coluna i nas primeiras h linhas.
    limite = min(MAX_HDR + 1, len(grade) + 1)
    prefixo = [[0] * limite for _ in range(n_col)]
    for l, linha in enumerate(grade):
        for i, cel in enumerate(linha):
            if not cel or not _normalizar_cnj(cel):
                continue
            total[i] += 1
            for h in range(l + 1, limite):
                prefixo[i][h] += 1
    return n_col, total, prefixo


def ler_planilha(s, fid: str, hint: str, log: Callable[[str], None] = print):
    """Varre TODAS as abas e, em cada uma, TODAS as linhas de cabeçalho plausíveis
    (0..MAX_HDR), escolhendo o par (aba, cabeçalho) cuja coluna de processo tem
    mais CNJs válidos. Devolve ``(valores_da_coluna, rótulo_da_coluna)``.

    A detecção é dirigida por CONTAGEM DE CNJ (não pelo texto do cabeçalho), então
    funciona com cabeçalhos fora do padrão (ex.: BVEP usa 'Numero Processual', que
    não contém a palavra 'processo') e com cabeçalho deslocado/capas/dashboards.

    Lê com openpyxl em modo ``read_only`` (streaming) — sem pandas, que arrastava
    numpy/pyarrow para o bundle e reparseava cada aba até 9 vezes."""
    from openpyxl import load_workbook

    dados = _baixar_bytes(s, fid)
    with warnings.catch_warnings():
        # As planilhas exportadas do Google trazem extensões que o openpyxl não
        # conhece; o aviso é ruído no log da GUI.
        warnings.simplefilter("ignore", UserWarning)
        wb = load_workbook(dados, read_only=True, data_only=True)
        try:
            abas = list(wb.sheetnames)
            melhor: tuple[list[str], str] | None = None
            melhor_n = -1
            for nome in abas:
                try:
                    grade = _grade(wb[nome], log)
                except Exception:
                    continue
                if not grade:
                    continue
                _n_col, total, prefixo = _mapa_cnj(grade)
                for hdr in range(min(MAX_HDR, len(grade))):
                    cabecalho = grade[hdr]

                    def conta_cnj(i: int, _hdr: int = hdr) -> int:
                        if i >= len(total):
                            return 0
                        return total[i] - prefixo[i][_hdr + 1]

                    col = _detectar_col_processo(cabecalho, conta_cnj, hint)
                    if col is None:
                        continue
                    n = conta_cnj(col)
                    if n > melhor_n:
                        rotulo = cabecalho[col] if col < len(cabecalho) else f"col {col}"
                        valores = [
                            linha[col] if col < len(linha) else ""
                            for linha in grade[hdr + 1:]
                        ]
                        melhor, melhor_n = (valores, rotulo), n
        finally:
            wb.close()
    if melhor is None:
        raise RuntimeError(f"nenhuma aba com coluna de processo (abas: {abas})")
    return melhor


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
            valores, col = ler_planilha(s, fid, hint, log=log)
        except Exception as exc:
            if not primeiro_erro:
                primeiro_erro = str(exc)
            log(f"   ❌ {campanha}: {str(exc)[:100]}")
            continue
        ok += 1
        n = 0
        for v in valores:
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
