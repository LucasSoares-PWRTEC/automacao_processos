# -*- coding: utf-8 -*-
"""Busca online de processos via scraper internalizado (imports tardios, isolados).

IMPORTS TARDIOS — NÃO PROMOVA PARA O TOPO
-----------------------------------------
A GUI importa este módulo no topo (por ``_LogWriter``, ``CHAVES_2FA`` e
``ler_config_2fa``, que são baratos). Se ``selenium``/``webdriver_manager``/``bs4``
forem importados aqui em nível de módulo, eles entram no caminho de ABERTURA da
janela — 2,5 s numa máquina rápida (dos quais 1,5 s são só o
``charset_normalizer`` que o bs4 puxa), e bem mais num i3.

Por isso ``criar_scraper`` (selenium) e os extratores do ``parser`` (bs4) são
resolvidos sob demanda por ``__getattr__`` de módulo (PEP 562): o código abaixo
os usa como nomes globais normais, mas o import só acontece na primeira chamada —
e o nome fica sendo um atributo de módulo, que continua sendo o ponto de injeção
dos testes (``monkeypatch.setattr(scraper, "criar_scraper", ...)``).

Medir com:
    python -X importtime -c "import organizador.gui"
"""

from __future__ import annotations

import io
import queue
import shutil
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    # NUNCA executado: existe só para linters e type-checkers verem os nomes que
    # `__getattr__` resolve em tempo de execução (sem isto, um pyflakes acusa
    # "undefined name 'criar_scraper'").
    from .integracoes.tjsp_scraper.parser import (  # noqa: F401
        _extrair_blocos_com_origem, extrair_penhora_bloqueio_tribunais,
    )
    from .integracoes.tjsp_scraper.scrapers import criar_scraper  # noqa: F401

from .config import (
    CONFIG_DIR,
    CONFIG_ENV_EXAMPLE_PATH,
    CONFIG_ENV_PATH,
    LEGACY_ENV_PATH,
    TJ_SISTEMAS,
    _ESTADO_TJ_UF,
    candidatos_tj_para_uf,
    rota_exclusiva_para_numero,
)
from .models import Processo
from .organizacao import _dict_para_processo
from .padroes import MAPA_UF_CNJ, RE_CNJ_TR, RE_NUMERO_CNJ

# nome exposto -> (módulo de origem, nome no módulo). Ver cabeçalho.
_TARDIOS = {
    "criar_scraper": (".integracoes.tjsp_scraper.scrapers", "criar_scraper"),
    "extrair_penhora_bloqueio_tribunais": (
        ".integracoes.tjsp_scraper.parser", "extrair_penhora_bloqueio_tribunais",
    ),
    "_extrair_blocos_com_origem": (
        ".integracoes.tjsp_scraper.parser", "_extrair_blocos_com_origem",
    ),
    # Tardio como os outros: `popup` mora no pacote `scrapers`, cujo __init__
    # importa TODOS os scrapers (e o selenium junto) — importá-lo no topo
    # colocaria o selenium no caminho de abertura do app.
    "AtribuicaoDetectadaError": (
        ".integracoes.tjsp_scraper.scrapers.popup", "AtribuicaoDetectadaError",
    ),
}


def __getattr__(nome: str):
    """Resolve os símbolos pesados na 1ª utilização (PEP 562).

    Depois de resolvido, o objeto é gravado nos globais do módulo: as chamadas
    seguintes são busca normal de global, sem passar por aqui."""
    alvo = _TARDIOS.get(nome)
    if alvo is None:
        raise AttributeError(f"module {__name__!r} has no attribute {nome!r}")
    from importlib import import_module

    modulo, atributo = alvo
    valor = getattr(import_module(modulo, __package__), atributo)
    globals()[nome] = valor
    return valor


def _tardio(nome: str):
    """Devolve um símbolo tardio, para uso DENTRO das funções deste módulo.

    NÃO chame `criar_scraper(...)` direto: `__getattr__` de módulo (PEP 562) só
    é acionado no acesso como ATRIBUTO (`scraper.criar_scraper`). Uma referência
    a nome global dentro de uma função compila para LOAD_GLOBAL, que olha o dict
    do módulo e os builtins — e nunca chama `__getattr__`. O resultado era
    `NameError: name 'criar_scraper' is not defined` ao clicar em "Buscar
    online" (v1.9.0 a v1.9.5).

    Os testes não pegaram porque todos faziam
    `monkeypatch.setattr(scraper, "criar_scraper", ...)`, o que CRIA a entrada no
    dict do módulo e faz o LOAD_GLOBAL encontrar — exercitando um caminho que a
    produção nunca percorre. Ver tests/test_scraper_nomes_tardios.py.

    `getattr` no módulo resolve os dois casos: acha primeiro o que o teste
    injetou e, na ausência, cai no `__getattr__` que importa de verdade."""
    import sys as _sys

    return getattr(_sys.modules[__name__], nome)


def __dir__() -> list[str]:
    return sorted(list(globals()) + list(_TARDIOS))


class _LogWriter(io.TextIOBase):
    """Redireciona os print() do scraper para uma callback (o log da aba).
    Essencial sob pythonw, onde sys.stdout é None e print() quebraria."""

    def __init__(self, put: Callable[[str], None]) -> None:
        self._put = put

    def write(self, s: str) -> int:
        if s:
            self._put(s)
        return len(s)


def fila_de_log(maxsize: int = 5000) -> "queue.Queue[str]":
    """Fila de log com teto que DESCARTA O MAIS ANTIGO quando enche.

    As filas de log eram `queue.Queue()` sem limite, alimentadas por todo
    `print()` dos scrapers. Numa passada longa o produtor (thread worker) é mais
    rápido que o consumidor (a GUI, que drena a cada 200-300 ms), então a fila
    vira memória pura numa máquina que já está apertada.

    Descartar o mais antigo é o comportamento certo aqui: log de scraper é
    informativo, e o widget da tela também só mantém as últimas linhas
    (ver `App._append_log`). O que NÃO pode acontecer é `put` bloquear a thread
    worker — por isso `put_nowait` com descarte, nunca `put()`.
    """
    return _FilaDescartavel(maxsize)


class _FilaDescartavel(queue.Queue):
    def put(self, item, block=True, timeout=None):  # noqa: ARG002 - assinatura da base
        """Nunca bloqueia e nunca levanta: se estiver cheia, joga fora o mais
        antigo para abrir espaço.

        Chama `queue.Queue.put` explicitamente, e não `self.put_nowait`: o
        `put_nowait` da classe base é implementado como `self.put(block=False)`,
        o que voltaria para cá em recursão infinita."""
        for _ in range(3):  # 3 tentativas bastam; evita laço infinito sob corrida
            try:
                queue.Queue.put(self, item, block=False)
                return
            except queue.Full:
                try:
                    self.get_nowait()
                except queue.Empty:
                    return  # esvaziou entre as duas operações; desiste desta linha


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
    # Projudi: número inexistente abre um diálogo com esta frase e a página do
    # processo (body#corpo_dados_processo) nunca é montada.
    "projudi": ("nenhum processo foi localizado",),
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
    "TOTP eproc TJRJ (TOTP_EPROC_TJRJ)": "TOTP_EPROC_TJRJ",
}

# Login individual do Autojur, editável na aba Configurações (cada colaborador
# usa sua própria máquina, então cada instalação tem seu próprio config/.env).
CHAVES_AUTOJUR: dict[str, str] = {
    "Usuário do Autojur": "email_autojur",
    "Senha do Autojur": "senha_autojur",
}


def _ler_valores_env(chaves: dict[str, str]) -> dict[str, str]:
    """Lê as chaves informadas (rótulo -> chave .env) do config/.env."""
    from dotenv import dotenv_values
    _preparar_env_local()
    valores = dotenv_values(CONFIG_ENV_PATH)
    return {chave: (valores.get(chave) or "") for chave in chaves.values()}


def _salvar_valores_env(valores: dict[str, str], chaves_validas: set[str]) -> None:
    """Grava ``valores`` no config/.env preservando as outras chaves.

    Também atualiza ``os.environ`` para que as mudanças valham já na sessão
    atual (``load_dotenv`` não sobrescreve variáveis já carregadas)."""
    import os

    from dotenv import set_key
    _preparar_env_local()
    for chave, valor in valores.items():
        if chave not in chaves_validas:
            continue
        valor = valor or ""
        set_key(str(CONFIG_ENV_PATH), chave, valor)
        os.environ[chave] = valor


def ler_config_2fa() -> dict[str, str]:
    """Lê os segredos de 2FA do config/.env como ``{chave_env: valor}``."""
    return _ler_valores_env(CHAVES_2FA)


def salvar_config_2fa(valores: dict[str, str]) -> None:
    """Grava os segredos de 2FA no config/.env preservando as outras chaves."""
    _salvar_valores_env(valores, set(CHAVES_2FA.values()))


def ler_config_autojur() -> dict[str, str]:
    """Lê o login do Autojur do config/.env como ``{chave_env: valor}``."""
    return _ler_valores_env(CHAVES_AUTOJUR)


def salvar_config_autojur(valores: dict[str, str]) -> None:
    """Grava o login do Autojur no config/.env preservando as outras chaves."""
    _salvar_valores_env(valores, set(CHAVES_AUTOJUR.values()))


# Prefixo da tag que registra ONDE o processo foi achado.
#
# Essa informação existia só por acidente: o lote de junho gravou o sistema em
# `data/_naomapeados_estado.json`, e foi esse arquivo — não raspagem nova — que
# em 2026-08-27 respondeu "MG tem corte por sequencial?" (não tem) e revelou que
# os dois sistemas de SP se sobrepõem, derrubando uma regra já escrita. Guardar
# no próprio processo torna a pergunta respondível sempre, de graça, sem
# depender de um arquivo de estado de um lote específico.
_TAG_SISTEMA = "Sistema: "

# Sistemas que expõem CPF/CNPJ das partes. As consultas PÚBLICAS (e-SAJ do TJSP,
# consulta do TJRJ) não trazem documento — o que só aparece na hora de montar a
# petição, quando já é tarde. Ver `_PRIORIDADE_SITE` em config.py, que por isso
# deixa o e-SAJ por último no fallback do monitor.
_SITES_COM_DOCUMENTO = frozenset({"eproc", "pje", "projudi"})


def marcar_sistema(proc: Processo, tj_label: str) -> None:
    """Anota (ou reanota) em que rota o processo foi achado.

    Substitui em vez de acrescentar: um processo migra de sistema (o RJ inteiro
    está migrando), e duas tags `Sistema:` no mesmo processo fariam a antiga
    mentir sem ninguém notar."""
    if not tj_label:
        return
    proc.tags[:] = [t for t in proc.tags if not t.startswith(_TAG_SISTEMA)]
    proc.tags.append(f"{_TAG_SISTEMA}{tj_label}")


def _consultar_e_extrair(
    scraper,
    site: str,
    numero: str,
    tribunal: str,
    uf: str,
    modo_docs: str = "nenhum",
    log: Callable[[str], None] = lambda s: None,
    tj_label: str = "",
) -> Processo:
    """Consulta um processo num scraper JÁ LOGADO e devolve o ``Processo`` extraído.

    NÃO cria nem fecha o scraper — o chamador é dono da sessão. Isso permite
    reaproveitar o MESMO login em vários processos (1 login por sistema, dezenas
    de consultas), evitando o bloqueio que vem de relogar a cada processo.

    Levanta ``RuntimeError`` (número não encontrado ou sem dados) ou
    ``AtribuicaoDetectadaError`` (o scraper detectou o pop-up de "atribuir o
    processo ao advogado", fechou o modal sem confirmar e relançou)."""
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
            f"Processo {numero} não encontrado em {tribunal} ({site}). "
            "Confira o número e o foro/UF, e se o processo está mesmo nesse "
            "sistema (eproc × e-SAJ × PJe)."
        )

    if site == "eproc":
        from .integracoes.tjsp_scraper.parser_processo import extrair_processo
        proc = _dict_para_processo(
            extrair_processo(
                html, _tardio("_extrair_blocos_com_origem")(html), numero_cnj=numero
            )
        )
        log("Dados extraídos com sucesso.\n")
    elif site == "esaj":
        from .integracoes.tjsp_scraper.parser_esaj import extrair_processo_esaj
        proc = _dict_para_processo(extrair_processo_esaj(html, numero_cnj=numero))
        log("Dados extraídos do e-SAJ "
            f"({len(proc.partes)} parte(s), {proc.total_eventos} movimentação(ões)).\n")
    elif site == "projudi":
        from .integracoes.tjsp_scraper.parser_projudi import extrair_processo_projudi
        proc = _dict_para_processo(extrair_processo_projudi(html, numero_cnj=numero))
        log("Dados extraídos do Projudi "
            f"({len(proc.partes)} parte(s), {proc.total_eventos} movimentação(ões)).\n")
    elif site == "tjrj":
        import json as _json
        from .integracoes.tjsp_scraper.parser_tjrj import extrair_processo_tjrj
        blob = _json.loads(html)
        proc = _dict_para_processo(
            extrair_processo_tjrj(blob.get("detalhe") or {}, blob.get("resumo") or {}, numero_cnj=numero)
        )
        log("Dados extraídos do TJRJ (consulta pública) "
            f"({len(proc.partes)} parte(s), {len(proc.eventos)} movimentação(ões)).\n")
    else:  # pje
        from .integracoes.tjsp_scraper.parser_pje import extrair_processo_pje
        proc = _dict_para_processo(extrair_processo_pje(html, numero_cnj=numero))
        log("Dados extraídos do PJe "
            f"({len(proc.partes)} parte(s), {proc.total_eventos} movimentação(ões)).\n")

    # Fallback de tribunal/UF quando o parser não os inferiu do CNJ.
    if not proc.tribunal:
        proc.tribunal = tribunal
    if not proc.uf:
        proc.uf = uf
    if not proc.numero:
        proc.numero = numero

    # Palavras-chave de penhora/bloqueio (extrator genérico, todos os sites).
    keywords = list(dict.fromkeys(
        k["descricao"]
        for k in _tardio("extrair_penhora_bloqueio_tribunais")(html)
        if k.get("descricao")
    ))
    if keywords:
        log("Palavras-chave de penhora/bloqueio: " + ", ".join(keywords) + "\n")
        if site != "eproc":  # no eproc não polui as tags estruturadas
            for kw in keywords:
                if kw not in proc.tags:
                    proc.tags.append(kw)

    marcar_sistema(proc, tj_label)

    # Aviso de graça, não busca extra: em SP a rota sugerida para `1*` é o e-SAJ
    # justamente por ser pública, e o preço dela é este. Dizer qual rota tem o
    # documento é barato; ir buscá-lo sozinho custaria um login em toda consulta.
    if proc.partes and not any(p.documento for p in proc.partes):
        outras = [
            lbl for lbl in candidatos_tj_para_uf(uf)
            if TJ_SISTEMAS[lbl][0] in _SITES_COM_DOCUMENTO and lbl != tj_label
        ]
        if outras:
            log(f"Nenhuma parte veio com CPF/CNPJ — consulta pública não traz. "
                f"Se precisar do documento, refaça em {outras[0]}.\n")
    return proc


def _rotas_da_busca(tj_label: str, uf_num: str) -> list[str]:
    """Rotas a tentar, em ordem: a escolhida primeiro, depois as outras da UF.

    A escolhida vai na frente mesmo quando não é a mais provável — quem digitou
    escolheu, e a tela já ofereceu a troca antes (gui `_buscar_online`). As
    demais seguem a ordem de `candidatos_tj_para_uf`, que põe na frente a rota
    mais barata (no RJ, a consulta pública: sem navegador, sem senha, sem TOTP)
    e já exclui as rotas comprovadamente inoperantes.
    """
    rotas = [tj_label]
    for lbl in candidatos_tj_para_uf(uf_num):
        if lbl not in rotas:
            rotas.append(lbl)
    return rotas


def buscar_processo_online(
    numero: str, tj_label: str, headless: bool, log: Callable[[str], None],
    modo_docs: str = "nenhum",
    modo_debug: str = "off",
    reter_scraper: Callable[[object], None] | None = None,
    fallback: bool = True,
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

    `fallback` (padrão True): se o processo não estiver no sistema escolhido,
    tenta os OUTROS sistemas da mesma UF antes de desistir, na ordem de
    `_rotas_da_busca`. Desligado automaticamente nos modos de debug (lá se
    inspeciona uma rota específica). Cada rota extra custa um navegador e um
    login com 2FA, então o fallback PARA cedo em dois casos: quando o processo
    foi achado (óbvio) e quando o número só pode estar na rota que falhou
    (`rota_exclusiva_para_numero`) — aí tentar outra não teria como dar certo.

    Levanta ValueError (número/TJ inválidos), ImportError (libs do scraper
    ausentes) ou RuntimeError (login/consulta sem resultado em nenhuma rota).
    """
    m = RE_NUMERO_CNJ.search(numero or "")
    if not m:
        raise ValueError(f"Número fora do padrão CNJ: {numero!r}")
    numero = m.group(0)
    if tj_label not in TJ_SISTEMAS:
        raise ValueError(f"TJ/sistema inválido: {tj_label!r}")
    _site, estado, _url, _perfil = TJ_SISTEMAS[tj_label]
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

    rotas = _rotas_da_busca(tj_label, uf_num) if (fallback and not debug) else [tj_label]
    exclusiva = rota_exclusiva_para_numero(numero)
    if len(rotas) > 1:
        log("Se não estiver no primeiro, tento os outros: "
            + " → ".join(rotas) + ".\n")

    problemas: list[str] = []
    for i, rota in enumerate(rotas, 1):
        site, estado, url, perfil = TJ_SISTEMAS[rota]
        tribunal, uf = _ESTADO_TJ_UF[estado]
        passo = f"[{i}/{len(rotas)}] " if len(rotas) > 1 else ""
        log(f"{passo}Abrindo {rota} e logando...\n")
        scraper = None
        try:
            scraper = _tardio("criar_scraper")(
                site=site, estado=estado, url=url, headless=headless, perfil=perfil
            )
            # Em debug o navegador fica aberto após a função retornar; o chamador
            # precisa segurar a referência do scraper para o Chrome não ser fechado
            # pela coleta de lixo (e para conseguir fechá-lo depois).
            if debug and reter_scraper is not None:
                reter_scraper(scraper)
            scraper.login()
            if modo_debug == "pos_login":
                log("🐞 DEBUG: parado após o login (sem pesquisar o processo). "
                    "Navegador deixado aberto para inspeção.\n")
                return Processo(numero=numero, tribunal=tribunal, uf=uf)
            proc = _consultar_e_extrair(
                scraper, site, numero, tribunal, uf, modo_docs=modo_docs, log=log,
                tj_label=rota,
            )

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

            if i > 1:
                log(f"Achado em {rota} (o {i}º sistema tentado).\n")
            return proc
        except _tardio("AtribuicaoDetectadaError"):
            # Pop-up de "atribuir o processo ao advogado": o processo FOI achado.
            # Regra do projeto — não seguir para outro sistema depois dele.
            raise
        except ImportError:
            raise  # biblioteca do scraper faltando: trocar de rota não conserta
        except Exception as exc:
            problemas.append(f"{rota}: {exc}")
            if rota == exclusiva:
                log(f"   ↪ {exc}\n   {rota} é o único sistema que pode ter este "
                    f"número — não adianta tentar os outros.\n")
                break
            if i < len(rotas):
                log(f"   ↪ {exc}\n   Tentando o próximo sistema...\n")
        finally:
            # Em debug o navegador é mantido aberto (o chamador o fecha depois).
            if not debug and scraper is not None:
                try:
                    scraper.fechar()
                except Exception:
                    pass

    if len(problemas) == 1:
        raise RuntimeError(problemas[0].split(": ", 1)[-1])
    raise RuntimeError(
        f"Processo {numero} não foi obtido em nenhum dos {len(problemas)} "
        f"sistemas tentados:\n  - " + "\n  - ".join(problemas)
    )
