# -*- coding: utf-8 -*-
"""Extrator do TJRJ — consulta pública (www3.tjrj.jus.br/consultaprocessual).

O `TJRJScraper` devolve um JSON `{"resumo": <por-numeracao-unica>, "detalhe":
<por-numero/publica>}`. Aqui mapeamos o `detalhe` para o dict aninhado que
`organizacao._dict_para_processo()` entende (mesmo contrato de parser_pje/esaj).

O TJRJ tem login PRÓPRIO + certificado para o sistema de autos, MAS a consulta
PÚBLICA é uma API REST aberta (sem login/captcha) — é dela que vêm estes dados.
Não há CPF/CNPJ das partes na consulta pública (igual e-SAJ): documento fica "".
"""

from __future__ import annotations

import re

from unidecode import unidecode

from .parser_processo import _inferir_tribunal_uf, _normaliza

# descPers que NÃO são partes (advogados, peritos, MP…): fora do polo ativo/passivo.
_NAO_PARTE = (
    "advog", "escrit", "perito", "testemunha", "ministerio publico", "defensor",
    "procurador", "contador", "leiloeiro", "curador", "custos legis", "autoridade",
    "interprete", "assistente tecnico", "representante",
)
# Polo ativo (p/ anexar os advogados do processo às partes do autor/exequente).
_ATIVO = {
    "EXEQUENTE", "AUTOR", "AUTORA", "REQUERENTE", "EMBARGANTE", "AGRAVANTE",
    "APELANTE", "IMPETRANTE", "RECLAMANTE", "RECORRENTE", "SUSCITANTE",
}
# sitProc (código) → texto.
_SIT = {"A": "Em andamento", "B": "Baixado", "S": "Suspenso",
        "F": "Arquivado/Findo", "J": "Julgado"}


def _adv(a: dict) -> dict:
    """{'nomeAdv','numOab':'RJ186328'} → {'nome','oab','uf'}."""
    oab_raw = (a.get("numOab") or "").strip()
    m = re.match(r"([A-Za-z]{2})\s*(\d+\w*)", oab_raw)
    if m:
        uf_oab, num = m.group(1).upper(), m.group(2)
        return {"nome": _normaliza(a.get("nomeAdv") or ""), "oab": uf_oab + num, "uf": uf_oab}
    return {"nome": _normaliza(a.get("nomeAdv") or ""), "oab": oab_raw, "uf": ""}


def extrair_processo_tjrj(detalhe: dict, resumo: dict | None, numero_cnj: str = "") -> dict:
    detalhe = detalhe or {}
    resumo = resumo or {}
    tribunal, uf = _inferir_tribunal_uf(numero_cnj)

    comarca = _normaliza(detalhe.get("nome") or resumo.get("nomeComarca") or "")
    comarca = re.sub(r"^Comarca de\s+", "", comarca, flags=re.I).strip()
    vara = _normaliza(detalhe.get("descVara") or "")
    orgao = _normaliza(detalhe.get("descServ") or "") or " — ".join(p for p in (vara, comarca) if p)

    endereco = " ".join(
        str(detalhe.get(k) or "").strip()
        for k in ("rua", "numero", "complemento", "bairro", "cidade", "cep")
    )
    endereco = _normaliza(re.sub(r"\s+", " ", endereco)).strip(" ,")

    advs = [_adv(a) for a in (detalhe.get("advogados") or []) if a.get("nomeAdv")]

    partes: list[dict] = []
    for p in (detalhe.get("personagensProcesso") or []):
        desc = (p.get("descPers") or "").strip()
        if not desc or any(t in unidecode(desc).lower() for t in _NAO_PARTE):
            continue  # advogado/escritório/perito/MP — não é parte
        nome = _normaliza(p.get("nome") or "")
        nome = re.sub(r"\s+e\s+outro\(?s?\)?\.*\s*$", "", nome, flags=re.I).strip()
        if not nome:
            continue
        papel = desc.upper()
        partes.append({
            "papel": papel, "nome": nome, "documento": "", "tipo": "",
            "advogados": advs if papel in _ATIVO else [],
        })

    # Último movimento → 1 evento (a consulta pública traz a lista completa noutro
    # endpoint; aqui o essencial p/ o mapeamento são as partes).
    eventos: list[dict] = []
    ult = detalhe.get("ultMovimentoProc") or {}
    if ult.get("descrMov"):
        eventos.append({
            "numero": str(ult.get("ordem") or ""), "data": ult.get("dtAlt") or "",
            "hora": "", "descricao": _normaliza(ult.get("descrMov") or ""),
            "usuario": "", "documentos": [],
        })

    return {
        "numero": numero_cnj,
        "classe": _normaliza(resumo.get("classe") or detalhe.get("descricaoTipAutos")
                             or detalhe.get("txtAcao") or ""),
        "assunto": _normaliza(detalhe.get("txtAssunto") or resumo.get("assunto") or ""),
        "competencia": "",
        "data_autuacao": detalhe.get("dataDis") or "",
        "situacao": _SIT.get((detalhe.get("sitProc") or "").upper(), detalhe.get("sitProc") or ""),
        "orgao_julgador": orgao,
        "juiz": "",
        "tribunal": tribunal,
        "uf": uf,
        "comarca": comarca,
        "vara": vara,
        "foro_endereco": endereco,
        "valor_causa": "",
        "partes": partes,
        "eventos": eventos,
        "atos": [],
    }
