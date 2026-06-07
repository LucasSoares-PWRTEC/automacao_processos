# -*- coding: utf-8 -*-
"""Leitura de credenciais locais do projeto.

Formato principal: ``config/credenciais.json`` com uma lista em ``credenciais``:

{
  "credenciais": [
    {"tj": "TJSP - Esaj", "login": "...", "senha": "..."}
  ]
}

Fallback temporário: ``config/senhas.xlsx`` no layout legado.
"""

from __future__ import annotations

import json
import re
import shutil
from functools import lru_cache
from pathlib import Path

from unidecode import unidecode

from ...config import CONFIG_DIR, CREDENCIAIS_EXAMPLE_PATH, CREDENCIAIS_PATH, LEGACY_SENHAS_XLSX_PATH

SENHAS_XLSX_PATH = CONFIG_DIR / "senhas.xlsx"


def _norm(s: str) -> str:
    """minúsculo, sem acento, espaços/hífens colapsados."""
    s = unidecode(str(s or "")).lower()
    s = re.sub(r"[\s\-–—]+", " ", s)
    return s.strip()


def _to_str(valor) -> str:
    """Normaliza o valor de login/senha para string utilizável."""
    if valor is None:
        return ""
    s = str(valor).strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def _bootstrap_credenciais_locais() -> None:
    """Importa credenciais legadas para `config/` quando possível."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CREDENCIAIS_PATH.exists() or SENHAS_XLSX_PATH.exists():
        return
    if LEGACY_SENHAS_XLSX_PATH.exists():
        shutil.copy2(LEGACY_SENHAS_XLSX_PATH, SENHAS_XLSX_PATH)


@lru_cache(maxsize=1)
def _carregar_credenciais() -> list[dict[str, str]]:
    _bootstrap_credenciais_locais()
    if CREDENCIAIS_PATH.exists():
        payload = json.loads(CREDENCIAIS_PATH.read_text(encoding="utf-8"))
        itens = payload.get("credenciais", payload)
        if not isinstance(itens, list):
            raise ValueError(
                f"Formato inválido em {CREDENCIAIS_PATH}. "
                "Use uma lista de credenciais ou a chave 'credenciais'."
            )
        saida: list[dict[str, str]] = []
        for item in itens:
            if not isinstance(item, dict):
                continue
            tj = _to_str(item.get("tj"))
            if not tj:
                continue
            saida.append(
                {
                    "tj": tj,
                    "login": _to_str(item.get("login")),
                    "senha": _to_str(item.get("senha")),
                    "_tj_norm": _norm(tj),
                }
            )
        return saida

    if SENHAS_XLSX_PATH.exists():
        import pandas as pd

        df = pd.read_excel(SENHAS_XLSX_PATH)
        df.columns = [str(c).strip() for c in df.columns]
        df = df.dropna(subset=["tj"]).copy()
        return [
            {
                "tj": _to_str(linha["tj"]),
                "login": _to_str(linha["Login"]),
                "senha": _to_str(linha["Senha"]),
                "_tj_norm": _norm(linha["tj"]),
            }
            for _, linha in df.iterrows()
        ]

    raise FileNotFoundError(
        "Credenciais não encontradas. Configure "
        f"{CREDENCIAIS_PATH} (use {CREDENCIAIS_EXAMPLE_PATH} como modelo) "
        f"ou {SENHAS_XLSX_PATH}."
    )


def obter_credenciais(tj_chave: str) -> tuple[str, str]:
    """Retorna ``(login, senha)`` para a chave de tribunal informada."""
    itens = _carregar_credenciais()
    alvo = _norm(tj_chave)

    exatos = [item for item in itens if item["_tj_norm"] == alvo]
    if len(exatos) == 1:
        linha = exatos[0]
    else:
        contidos = [item for item in itens if alvo in item["_tj_norm"]]
        if len(contidos) == 1:
            linha = contidos[0]
        elif len(contidos) == 0:
            raise KeyError(f"Nenhuma credencial casa com tj='{tj_chave}'.")
        else:
            achados = ", ".join(item["tj"] for item in contidos)
            raise KeyError(
                f"Ambiguidade para tj='{tj_chave}'. Candidatos: {achados}. "
                "Use uma chave mais específica."
            )

    return linha["login"], linha["senha"]


def cpf_formatado(login: str) -> str:
    """Formata CPF quando o login vier apenas com dígitos."""
    d = re.sub(r"\D", "", str(login or "")).zfill(11)
    if len(d) != 11:
        return login
    return f"{d[0:3]}.{d[3:6]}.{d[6:9]}-{d[9:11]}"
