# -*- coding: utf-8 -*-
"""Busca online de processos via scraper internalizado (imports tardios, isolados)."""

from __future__ import annotations

import io
import shutil
from typing import Callable

from .config import (
    CONFIG_DIR,
    CONFIG_ENV_EXAMPLE_PATH,
    CONFIG_ENV_PATH,
    LEGACY_ENV_PATH,
    TJ_SISTEMAS,
    _ESTADO_TJ_UF,
)
from .integracoes.tjsp_scraper.parser import _extrair_blocos_com_origem, extrair_penhora_bloqueio_tribunais
from .integracoes.tjsp_scraper.scrapers import criar_scraper
from .models import Processo
from .organizacao import _dict_para_processo
from .padroes import MAPA_UF_CNJ, RE_CNJ_TR, RE_NUMERO_CNJ


class _LogWriter(io.TextIOBase):
    """Redireciona os print() do scraper para uma callback (o log da aba).
    Essencial sob pythonw, onde sys.stdout é None e print() quebraria."""

    def __init__(self, put: Callable[[str], None]) -> None:
        self._put = put

    def write(self, s: str) -> int:
        if s:
            self._put(s)
        return len(s)


# Marcadores de "processo não encontrado" por site. Quando a página de
# resultado contém um destes (em minúsculas), o tribunal está dizendo que não
# há processo com aquele número/foro — devolver isso ao extrator só geraria um
# registro vazio, então sinalizamos com uma mensagem clara ao usuário.
_MARCADORES_NAO_ENCONTRADO = {
    "eproc": ("processo não encontrado", "processo nao encontrado"),
    "esaj": (
        "não existem informações disponíveis para os parâmetros informados",
        "nao existem informacoes disponiveis para os parametros informados",
        "não foram encontrados resultados",
    ),
    "pje": ("não foram encontrados", "nao foram encontrados", "nenhum registro encontrado"),
}


def _html_indica_nao_encontrado(html: str, site: str) -> bool:
    alvo = (html or "").lower()
    return any(marca in alvo for marca in _MARCADORES_NAO_ENCONTRADO.get(site, ()))


def _preparar_env_local() -> None:
    """Garante que exista um `.env` local, migrando do legado quando possível."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_ENV_PATH.exists():
        return
    if LEGACY_ENV_PATH.exists():
        shutil.copy2(LEGACY_ENV_PATH, CONFIG_ENV_PATH)
        return
    if CONFIG_ENV_EXAMPLE_PATH.exists():
        shutil.copy2(CONFIG_ENV_EXAMPLE_PATH, CONFIG_ENV_PATH)


# Segredos de 2FA editáveis na aba Configurações (rótulo amigável -> chave .env).
# Os valores ficam em config/.env; ``ler_config_2fa``/``salvar_config_2fa`` cuidam
# da leitura/escrita preservando as demais chaves do arquivo.
CHAVES_2FA: dict[str, str] = {
    "E-mail do 2FA (IMAP_USER)": "IMAP_USER",
    "Senha do e-mail do 2FA (IMAP_PASS)": "IMAP_PASS",
    "TOTP PJe (TOTP_PDPJ)": "TOTP_PDPJ",
    "TOTP eproc TJMG (TOTP_EPROC_TJMG)": "TOTP_EPROC_TJMG",
    "TOTP eproc TJSP (TOTP_EPROC_TJSP)": "TOTP_EPROC_TJSP",
}


def ler_config_2fa() -> dict[str, str]:
    """Lê os segredos de 2FA do config/.env como ``{chave_env: valor}``."""
    from dotenv import dotenv_values
    _preparar_env_local()
    valores = dotenv_values(CONFIG_ENV_PATH)
    return {chave: (valores.get(chave) or "") for chave in CHAVES_2FA.values()}


def salvar_config_2fa(valores: dict[str, str]) -> None:
    """Grava os segredos de 2FA no config/.env preservando as outras chaves.

    Também atualiza ``os.environ`` para que as mudanças valham já na sessão
    atual (``load_dotenv`` não sobrescreve variáveis já carregadas)."""
    import os

    from dotenv import set_key
    _preparar_env_local()
    chaves_validas = set(CHAVES_2FA.values())
    for chave, valor in valores.items():
        if chave not in chaves_validas:
            continue
        valor = valor or ""
        set_key(str(CONFIG_ENV_PATH), chave, valor)
        os.environ[chave] = valor


def buscar_processo_online(
    numero: str, tj_label: str, headless: bool, log: Callable[[str], None],
    modo_docs: str = "nenhum",
    modo_debug: str = "off",
    reter_scraper: Callable[[object], None] | None = None,
) -> Processo:
    """Loga no TJ/sistema escolhido via integração internalizada, consulta o processo e
    devolve um Processo.

    `tj_label` é uma das chaves de TJ_SISTEMAS (ex.: "TJSP — eproc (SP)"). Só o eproc
    tem extrator estruturado (auto-preenche os campos); para e-SAJ/PJe devolve um
    Processo mínimo (número + tribunal/UF) e anexa as palavras-chave de
    penhora/bloqueio encontradas.

    `modo_docs` (só eproc): "nenhum" (default — não abre documentos, bem mais
    rápido; a petição não usa o teor dos atos), "atos" ou "todos".

    `modo_debug` controla pontos de parada para inspeção manual (deixa o
    navegador ABERTO em vez de fechá-lo):
      - "off" (default): fluxo normal — loga, consulta, fecha o navegador.
      - "pos_login": para logo após o login, SEM pesquisar o processo.
        Devolve um Processo mínimo (só número/tribunal); use para conferir
        a tela pós-login (Painel do Advogado, perfil selecionado etc.).
      - "pos_consulta": consulta o processo e extrai os dados normalmente,
        mas mantém o navegador aberto e loga um resumo dos campos para você
        validar a extração contra a página real.
    Nos dois modos de debug a função NÃO fecha o navegador; passe
    `reter_scraper` para que o chamador segure a referência do scraper (evita
    que o navegador feche por coleta de lixo) e possa fechá-lo depois.

    Levanta ValueError (número/TJ inválidos), ImportError (libs do scraper
    ausentes) ou RuntimeError (login/consulta sem resultado).
    """
    m = RE_NUMERO_CNJ.search(numero or "")
    if not m:
        raise ValueError(f"Número fora do padrão CNJ: {numero!r}")
    numero = m.group(0)
    if tj_label not in TJ_SISTEMAS:
        raise ValueError(f"TJ/sistema inválido: {tj_label!r}")
    site, estado, url, perfil = TJ_SISTEMAS[tj_label]
    tribunal, uf = _ESTADO_TJ_UF[estado]

    # Aviso (não bloqueia): número parece ser de outra UF que não a do TJ escolhido.
    tr = RE_CNJ_TR.search(numero)
    uf_num = MAPA_UF_CNJ.get(f"{tr.group(1)}.{tr.group(2)}", "") if tr else ""
    if uf_num and uf_num != uf:
        log(f"Aviso: o número parece ser de {uf_num}, mas você selecionou {tribunal} ({uf}).\n")

    from dotenv import load_dotenv
    _preparar_env_local()
    load_dotenv(CONFIG_ENV_PATH)

    debug = modo_debug in ("pos_login", "pos_consulta")
    if debug and headless:
        log("Aviso: modo debug com 'Navegador invisível' ligado — nada a inspecionar.\n")

    log(f"Abrindo {tj_label} e logando...\n")
    scraper = criar_scraper(
        site=site, estado=estado, url=url, headless=headless, perfil=perfil
    )
    # Em debug o navegador fica aberto após a função retornar; o chamador
    # precisa segurar a referência do scraper para o Chrome não ser fechado
    # pela coleta de lixo (e para conseguir fechá-lo depois).
    if debug and reter_scraper is not None:
        reter_scraper(scraper)
    try:
        scraper.login()
        if modo_debug == "pos_login":
            log("🐞 DEBUG: parado após o login (sem pesquisar o processo). "
                "Navegador deixado aberto para inspeção.\n")
            return Processo(numero=numero, tribunal=tribunal, uf=uf)
        log(f"Consultando processo {numero}"
            + (f" (docs={modo_docs})" if site == "eproc" else "") + "...\n")
        if site == "eproc":
            html = scraper.consultar_processo(numero, modo_docs=modo_docs)
        else:
            html = scraper.consultar_processo(numero)  # esaj/pje não aceitam modo_docs
        if not html:
            raise RuntimeError("Scraper não retornou dados para esse número.")

        if _html_indica_nao_encontrado(html, site):
            raise RuntimeError(
                f"Processo {numero} não encontrado em {tj_label}. "
                "Confira o número e o foro/UF, e se o processo está mesmo nesse "
                "sistema (eproc × e-SAJ × PJe)."
            )

        if site == "eproc":
            from .integracoes.tjsp_scraper.parser_processo import extrair_processo
            proc = _dict_para_processo(
                extrair_processo(html, _extrair_blocos_com_origem(html), numero_cnj=numero)
            )
            log("Dados extraídos com sucesso.\n")
        elif site == "esaj":
            from .integracoes.tjsp_scraper.parser_esaj import extrair_processo_esaj
            proc = _dict_para_processo(extrair_processo_esaj(html, numero_cnj=numero))
            log("Dados extraídos do e-SAJ "
                f"({len(proc.partes)} parte(s), {proc.total_eventos} movimentação(ões)).\n")
        else:
            proc = Processo(numero=numero, tribunal=tribunal, uf=uf)
            log("Auto-preenchimento estruturado indisponível para PJe — "
                "preencha os campos manualmente.\n")

        # Fallback de tribunal/UF quando o parser não os inferiu do CNJ.
        if not proc.tribunal:
            proc.tribunal = tribunal
        if not proc.uf:
            proc.uf = uf

        if not proc.numero:
            proc.numero = numero

        # Palavras-chave de penhora/bloqueio (extrator genérico, todos os sites).
        keywords = list(dict.fromkeys(
            k["descricao"] for k in extrair_penhora_bloqueio_tribunais(html) if k.get("descricao")
        ))
        if keywords:
            log("Palavras-chave de penhora/bloqueio: " + ", ".join(keywords) + "\n")
            if site != "eproc":  # no eproc não polui as tags estruturadas
                for kw in keywords:
                    if kw not in proc.tags:
                        proc.tags.append(kw)

        if modo_debug == "pos_consulta":
            log("🐞 DEBUG: consulta concluída — valide os dados extraídos abaixo "
                "contra a página aberta no navegador.\n")
            log(f"   Nº: {proc.numero}\n")
            log(f"   Classe: {proc.classe or '—'}\n")
            log(f"   Tribunal/UF: {proc.tribunal or '—'}/{proc.uf or '—'}\n")
            log(f"   Órgão julgador: {proc.orgao_julgador or '—'}\n")
            log(f"   Juiz(a): {proc.juiz or '—'}\n")
            log(f"   Valor da causa: {proc.valor_causa or '—'}\n")
            log(f"   Comarca/Vara: {proc.comarca or '—'} / {proc.vara or '—'}\n")
            log(f"   Última mov.: {proc.ultima_movimentacao or '—'}\n")
            log(f"   Tags: {', '.join(proc.tags) if proc.tags else '—'}\n")
            log("   Navegador deixado aberto para conferência.\n")

        return proc
    finally:
        # Em debug o navegador é mantido aberto (o chamador o fecha depois).
        if not debug:
            try:
                scraper.fechar()
            except Exception:
                pass
