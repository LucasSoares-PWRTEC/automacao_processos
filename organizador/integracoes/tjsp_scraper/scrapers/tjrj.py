# -*- coding: utf-8 -*-
"""Scraper do TJRJ via CONSULTA PÚBLICA (www3.tjrj.jus.br/consultaprocessual).

Diferente dos outros, NÃO usa navegador nem login: a consulta pública do TJRJ é
uma API REST aberta (o PJe do TJRJ exige certificado digital, mas esta não). O
reCAPTCHA está desligado (recuperar-site-key → 204; Authorization: Bearer false).

Fluxo (descoberto via DevTools/bundle da SPA):
  1) POST processos/por-numeracao-unica  {tipoProcesso:"1", codigoProcesso:<CNJ>}
       → resumo (classe, comarca e o NÚMERO ANTIGO `numProcesso`)
  2) POST processos/por-numero/publica   {tipoProcesso:"1", codigoProcesso:<nº antigo>}
       → detalhe (personagensProcesso = partes dos dois polos, advogados, etc.)

`tipoProcesso:"1"` = 1ª instância. `consultar_processo` devolve um JSON
`{"resumo":…, "detalhe":…}` que o parser_tjrj transforma em Processo.
"""

from __future__ import annotations

import json
from typing import Optional

from .base import TribunalScraper

_API_PADRAO = "https://www3.tjrj.jus.br/consultaprocessual/api/"
_TIMEOUT = 45

# Valores de `tipoProcesso` vistos ao vivo no endpoint por-numeracao-unica.
# 1 é o único que a consulta pública detalha (ver consultar_processo).
_ERRO_FORA_DO_LEGADO = {
    13: ("processo do PJe do TJRJ: a consulta pública só expõe o cabeçalho "
         "(sem partes). O PJe do TJRJ exige certificado digital — não há rota "
         "automatizada hoje."),
    16: ("processo do eproc do TJRJ: a consulta pública só expõe o cabeçalho "
         "(sem partes). Use a rota 'TJRJ — eproc', que loga no "
         "eproc1g.tjrj.jus.br."),
}


def _erro_generico(tipo) -> str:
    return (f"tipoProcesso={tipo}: a consulta pública do TJRJ só detalha o "
            f"sistema legado (tipoProcesso=1); este processo está em outro "
            f"sistema e precisa da rota própria dele.")


class TJRJScraper(TribunalScraper):
    def __init__(self, estado: str, url: str = "", headless: bool = False):
        super().__init__(estado, url or _API_PADRAO, headless)
        self.api = (url or _API_PADRAO).rstrip("/") + "/"
        self._sess = None
        self._verify = True  # cai p/ False on-the-fly se o cert do TJRJ falhar

    # Sem navegador: a sessão é um requests.Session.
    def _session(self):
        if self._sess is None:
            import requests
            s = requests.Session()
            s.headers.update({
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/123 Safari/537.36"),
                "Content-Type": "application/json",
                "Authorization": "Bearer false",  # captcha off → token "false"
                "Origin": "https://www3.tjrj.jus.br",
                "Referer": "https://www3.tjrj.jus.br/consultaprocessual/",
            })
            self._sess = s
        return self._sess

    def _post(self, path: str, payload: dict):
        import requests
        s = self._session()
        try:
            return s.post(self.api + path, json=payload, timeout=_TIMEOUT, verify=self._verify)
        except requests.exceptions.SSLError:
            # cadeia de certificado do TJRJ às vezes incompleta — degrada p/ verify=False
            self._verify = False
            import urllib3
            urllib3.disable_warnings()
            return s.post(self.api + path, json=payload, timeout=_TIMEOUT, verify=False)

    def login(self) -> None:
        self._logado = True  # consulta pública: nada a logar

    def consultar_processo(self, numero_processo: str) -> Optional[str]:
        numero = str(numero_processo).strip()
        # 1) resumo pelo número único (CNJ) → número antigo do TJRJ
        try:
            r = self._post("processos/por-numeracao-unica",
                           {"tipoProcesso": "1", "codigoProcesso": numero})
        except Exception as e:
            print(f"⚠️ TJRJ: erro na busca por número único: {e}")
            return None
        if r.status_code != 200:
            print(f"ℹ️ TJRJ: busca devolveu HTTP {r.status_code} para {numero}.")
            return None
        try:
            lista = r.json()
        except Exception:
            return None
        if not isinstance(lista, list) or not lista:
            print(f"ℹ️ TJRJ: processo {numero} não encontrado na consulta pública.")
            return None
        resumo = lista[0]

        # `tipoProcesso` do RESUMO diz em que sistema o processo mora. A consulta
        # pública só tem rota de detalhe para o LEGADO (1): para 13/16 o
        # `por-numero/publica` responde 412 "Número do processo inválido." com
        # QUALQUER tipoProcesso, número (CNJ pontuado, 20 dígitos) ou idProcesso
        # — e `por-id/publica` nem existe (404). O que volta aqui é só um
        # cabeçalho stub: sem comarca, sem serventia, sem partes.
        #
        # Sem este corte o usuário via o genérico "Scraper não retornou dados
        # para esse número" e não tinha como saber que era rota errada, não
        # processo inexistente.
        try:
            tipo = int(resumo.get("tipoProcesso"))
        except (TypeError, ValueError):
            tipo = 1  # campo ausente/estranho: segue o fluxo antigo, não inventa erro
        if tipo != 1:
            raise RuntimeError(_ERRO_FORA_DO_LEGADO.get(tipo, _erro_generico(tipo)))

        num_antigo = (resumo.get("numProcesso") or "").strip()
        if not num_antigo:
            return None

        # 2) detalhe pelo número antigo → partes/movimentos
        try:
            r2 = self._post("processos/por-numero/publica",
                            {"tipoProcesso": "1", "codigoProcesso": num_antigo})
        except Exception as e:
            print(f"⚠️ TJRJ: erro no detalhe: {e}")
            return None
        if r2.status_code != 200:
            print(f"ℹ️ TJRJ: detalhe devolveu HTTP {r2.status_code} para {numero}.")
            return None
        try:
            detalhe = r2.json()
        except Exception:
            return None
        if not isinstance(detalhe, dict) or not detalhe.get("personagensProcesso"):
            print(f"ℹ️ TJRJ: detalhe de {numero} sem partes.")
            return None

        print(f"📋 TJRJ: consulta pública OK ({len(detalhe.get('personagensProcesso') or [])} personagens).")
        return json.dumps({"resumo": resumo, "detalhe": detalhe}, ensure_ascii=False)

    def fechar(self) -> None:
        if self._sess is not None:
            try:
                self._sess.close()
            except Exception:
                pass
            self._sess = None
