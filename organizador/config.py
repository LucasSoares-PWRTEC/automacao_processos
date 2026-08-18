# -*- coding: utf-8 -*-
"""Configurações: caminhos, mapa de TJ/sistema, OAB do escritório e layout."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path


def _raiz_app() -> Path:
    """Raiz onde ficam os dados do app (config/, Petições/).

    - Em execução normal: a pasta que contém o pacote ``organizador/``.
    - Empacotado com PyInstaller (onefile): ``sys._MEIPASS`` (pasta temporária
      onde o exe extrai os dados embutidos). Assim o exe acha config/.env,
      credenciais.json e os modelos de petição embutidos, sem instalação.
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent.parent


# Raiz do projeto = pasta que contém o pacote organizador/ (ou o bundle).
_RAIZ = _raiz_app()

def _resolver_pasta_padrao() -> str:
    env = os.getenv("ORGANIZADOR_PASTA_PADRAO")
    if env:
        return str(Path(env).expanduser())
    desktop = Path.home() / "Desktop" / "Processos"
    if desktop.exists():
        return str(desktop)
    return str((Path.home() / "Processos").expanduser())


PASTA_PADRAO = _resolver_pasta_padrao()
PASTA_NAO_IDENTIFICADOS = "_nao_identificados"
CACHE_DIR = ".cache_texto"

# Dados aprendidos do app, persistidos em local estável do usuário (NÃO no
# bundle/_MEIPASS, que é temporário no exe). Hoje guarda só o cache de
# código-de-origem CNJ → comarca, aprendido dos processos já vistos.
APP_DATA_DIR = Path(
    os.getenv("ORGANIZADOR_APP_DATA") or (Path.home() / ".automacao_processos")
)
COMARCAS_CACHE_PATH = APP_DATA_DIR / "comarcas_por_origem.json"
# Estado da UI lembrado entre execuções (pastas base/destino da aba Pesquisa),
# para pré-carregar os processos já trabalhados ao reabrir o app.
UI_STATE_PATH = APP_DATA_DIR / "ui_state.json"
# Perfil persistente do Chrome usado SÓ pelo Autojur: guarda cookies/dispositivo
# confiável entre sessões para pular o 2FA "novo dispositivo" nas próximas vezes
# (login cai de ~3-4 min para segundos). A busca online usa perfil temporário
# próprio — um perfil do Chrome só pode ser aberto por um processo por vez.
AUTOJUR_PROFILE_DIR = APP_DATA_DIR / "autojur_profile"
# Processos trazidos pela busca online são persistidos aqui (um
# <numero>/metadados.json por processo) para reaparecerem na lista ao reabrir.
PROCESSOS_ONLINE_DIR = APP_DATA_DIR / "processos_online"
# Índice de RESUMO (só campos de exibição/filtro) de todos os processos já no
# sistema, atualizado incrementalmente por mtime. Torna a carga da lista da aba
# Pesquisa quase instantânea: sem ele, todos os metadados.json seriam relidos e
# parseados a cada abertura. O Processo completo é carregado sob demanda.
PROCESSOS_INDICE_JSON = APP_DATA_DIR / "processos_indice.json"
# Placeholders extraídos de cada modelo .docx, por (nome, mtime, tamanho). Abrir
# os ~39 modelos com python-docx custava ~3 s de CPU em toda abertura para um
# resultado que só muda quando alguém edita um modelo.
TEMPLATES_CACHE_PATH = APP_DATA_DIR / "templates_placeholders.json"
# Processos pré-carregados EMBUTIDOS na release (JSON consolidado, gzip), para
# aparecerem já populados em qualquer máquina onde o app for instalado. Fica em
# _RAIZ (=_MEIPASS no exe), como config/ e Petições/. Prioridade menor que a
# pasta base e que o cache de buscas online do próprio usuário.
PROCESSOS_BASE_JSON = _RAIZ / "data" / "processos_base.json.gz"
PASTA_PETICOES = _RAIZ / "Petições"
# Ícone da janela (mesmo desenho do ícone do .exe, ver *.spec). Fica em _RAIZ
# (=_MEIPASS no exe) como os demais assets embutidos.
CAMINHO_ICONE = _RAIZ / "organizador" / "assets" / "icone.ico"
# Guia de configuração das credenciais do 2FA, aberto no navegador pelo botão da
# aba Configurações. HTML autocontido (sem CSS/JS externo) de propósito: abre sem
# internet. Fica em _RAIZ (=_MEIPASS no exe) como os demais assets embutidos.
CAMINHO_GUIA_CREDENCIAIS = _RAIZ / "docs" / "guia-credenciais.html"
# Conta de serviço do Google (somente leitura das planilhas de credores). Fica na
# raiz do projeto (=_MEIPASS no exe); override por GOOGLE_CREDENTIALS_FILE. Usada
# por organizador.sheets para o monitoramento periódico planilhas → mapeamento.
GOOGLE_SA_PATH = Path(
    os.getenv("GOOGLE_CREDENTIALS_FILE") or (_RAIZ / "token_google_api.json")
)
# --------------------------------------------------------------------------
# Config editável (senhas dos tribunais + segredos de 2FA)
# --------------------------------------------------------------------------
# A config VIAJA no bundle (ver OrganizadorProcessos.spec), mas o bundle é
# somente-leitura na prática: no onefile ele é extraído em `sys._MEIPASS`, uma
# pasta temporária APAGADA quando o app fecha. Gravar ali fazia a aba
# Configurações parecer funcionar e perder a edição — trocar uma senha vencida
# não surtia efeito nenhum em nenhuma máquina que rodasse o .exe.
#
# Solução: o bundle é a SEMENTE; a config viva mora em APP_DATA_DIR, que persiste.
# Na 1ª execução copiamos uma para a outra (`semear_config`), e daí em diante só
# APP_DATA_DIR é lido e escrito. Rodando do código-fonte nada muda de lugar: as
# duas raízes coincidem e a semeadura não faz nada.
CONFIG_SEMENTE_DIR = _RAIZ / "config"

def _dir_config_ativo() -> Path:
    """Onde a config viva mora. Igual à semente quando não empacotado."""
    if getattr(sys, "frozen", False):
        return APP_DATA_DIR / "config"
    return CONFIG_SEMENTE_DIR


CONFIG_DIR = _dir_config_ativo()
CONFIG_ENV_PATH = CONFIG_DIR / ".env"
CONFIG_ENV_EXAMPLE_PATH = CONFIG_DIR / ".env.example"
CREDENCIAIS_PATH = CONFIG_DIR / "credenciais.json"
CREDENCIAIS_EXAMPLE_PATH = CONFIG_DIR / "credenciais.example.json"
# NB: `senhas.xlsx` é derivado de CONFIG_DIR em integracoes/.../credenciais.py, e
# portanto acompanha esta mudança automaticamente.

# Arquivos copiados do bundle na 1ª execução, se ainda não existirem no destino.
_ARQUIVOS_CONFIG = (
    ".env", ".env.example",
    "credenciais.json", "credenciais.example.json",
    "senhas.xlsx",
)


def semear_config() -> list[str]:
    """Copia a config do bundle para APP_DATA_DIR quando ainda não há uma lá.

    Devolve os nomes copiados. NUNCA sobrescreve: o que o usuário editou na
    máquina dele vence o que veio compilado — inclusive depois de uma
    atualização, senão o update apagaria a senha que ele acabou de corrigir.

    Best-effort: falha de disco não pode impedir o app de abrir (ele ainda
    consegue ler a semente, só não persiste a edição)."""
    if CONFIG_DIR == CONFIG_SEMENTE_DIR:
        return []  # rodando do fonte: mesma pasta, nada a copiar
    copiados: list[str] = []
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return copiados
    for nome in _ARQUIVOS_CONFIG:
        origem, destino = CONFIG_SEMENTE_DIR / nome, CONFIG_DIR / nome
        if destino.exists() or not origem.is_file():
            continue
        try:
            shutil.copy2(origem, destino)
            copiados.append(nome)
        except OSError:
            pass
    return copiados


# --- atualizar senhas dos tribunais a partir do bundle ---------------------
# `semear_config` de propósito NUNCA sobrescreve, para o update não apagar a
# senha que o usuário corrigiu na máquina. O efeito colateral é que uma senha
# trocada no escritório NÃO chega em quem já usa o app: o arquivo local vence
# para sempre. As duas funções abaixo dão a saída explícita — o usuário pede,
# vê o que muda e confirma. Nada aqui roda sozinho.
def diff_credenciais_do_bundle() -> dict[str, list[str]]:
    """Compara credenciais local × bundle. Só RÓTULOS, nunca senhas.

    Devolve ``{"alteradas": [...], "novas": [...], "somem": [...]}``. Dicionário
    vazio em todas as chaves = nada a fazer. Rodando do fonte (bundle == local)
    devolve tudo vazio, porque não existe semente separada para comparar.
    """
    vazio: dict[str, list[str]] = {"alteradas": [], "novas": [], "somem": []}
    if CONFIG_DIR == CONFIG_SEMENTE_DIR:
        return vazio

    def _mapa(caminho: Path) -> dict[str, str]:
        try:
            payload = json.loads(caminho.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        itens = payload.get("credenciais", payload) if isinstance(payload, dict) else payload
        if not isinstance(itens, list):
            return {}
        # rótulo -> "login\x00senha"; comparar o par evita alarme falso quando só
        # o login foi normalizado (ex.: CPF com/sem zero à esquerda).
        return {
            str(i.get("tj") or ""): f"{i.get('login') or ''}\x00{i.get('senha') or ''}"
            for i in itens
            if isinstance(i, dict) and i.get("tj")
        }

    local = _mapa(CREDENCIAIS_PATH)
    bundle = _mapa(CONFIG_SEMENTE_DIR / "credenciais.json")
    if not bundle:
        return vazio
    return {
        "alteradas": sorted(r for r, v in bundle.items() if r in local and local[r] != v),
        "novas": sorted(r for r in bundle if r not in local),
        "somem": sorted(r for r in local if r not in bundle),
    }


def atualizar_credenciais_do_bundle() -> Path | None:
    """Sobrescreve o credenciais.json local pelo que veio no .exe.

    Guarda o anterior como ``credenciais.json.bak`` e devolve o caminho do
    backup — o usuário está descartando edição local, então tem de haver volta.
    Devolve ``None`` quando não há o que fazer (rodando do fonte, ou sem
    credenciais.json no bundle). Levanta ``OSError`` se a cópia falhar: aqui,
    diferente de `semear_config`, falhar em silêncio faria o app afirmar que
    atualizou a senha sem ter atualizado.
    """
    if CONFIG_DIR == CONFIG_SEMENTE_DIR:
        return None
    origem = CONFIG_SEMENTE_DIR / "credenciais.json"
    if not origem.is_file():
        return None
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    backup = CREDENCIAIS_PATH.with_suffix(".json.bak")
    if CREDENCIAIS_PATH.exists():
        shutil.copy2(CREDENCIAIS_PATH, backup)
    shutil.copy2(origem, CREDENCIAIS_PATH)
    return backup if backup.exists() else None


LEGACY_SCRAPER_DIR = _RAIZ.parent / "TJSP-Scraper"
LEGACY_ENV_PATH = LEGACY_SCRAPER_DIR / ".env"
LEGACY_SENHAS_XLSX_PATH = LEGACY_SCRAPER_DIR / "data" / "senhas.xlsx"

# Rótulo do seletor de TJ/Sistema -> (site, estado, url, perfil). O e-SAJ deriva
# a URL de login (sajcas) e a chave de credencial do host da `url` do cpopg, então
# a url IMPORTA para o e-SAJ (e atende vários estados); o PJe ainda usa url fixa na
# classe. e-SAJ e eproc têm extrator estruturado (auto-preenchem a petição); o PJe
# entra com Processo mínimo + palavras-chave de penhora.
#
# `perfil` (4º elemento, só eproc): sigla da OAB a selecionar na tela "Seleção de
# perfil". A conta do TJSP eproc dá acesso a 6 perfis de OAB do mesmo advogado
# (SP, MG, RJ, PR, RN, GO) — o login/2FA é o mesmo; muda só qual botão clicar.
# Quando None, o scraper usa a sigla padrão do estado (ex.: TJMG → "MG").
TJ_SISTEMAS = {
    # ── SP ──────────────────────────────────────────────────────────────────
    "TJSP — eproc":  ("eproc", "SAO PAULO", "https://eproc1g.tjsp.jus.br/eproc/", "SP"),
    "TJSP — e-SAJ":  ("esaj",  "SAO PAULO", "https://esaj.tjsp.jus.br/cpopg/open.do", None),
    # ── AM ──────────────────────────────────────────────────────────────────
    "TJAM — e-SAJ":  ("esaj",  "AMAZONAS",           "https://consultasaj.tjam.jus.br/cpopg/open.do", None),
    # ── DF ──────────────────────────────────────────────────────────────────
    # PJe nacional (login.seam → sso.cloud.pje.jus.br, client pje-tjdft-1g).
    # Validado ao vivo: a conta tem acesso ao TJDFT. (TJBA é PJe nacional também,
    # mas a conta NÃO está habilitada lá — login não confirma após o TOTP.)
    "TJDFT — PJe":   ("pje",   "DISTRITO FEDERAL",   "https://pje.tjdft.jus.br/pje/", None),
    # ── CE ──────────────────────────────────────────────────────────────────
    # PJe é nacional (login único via PDPJ): o scraper deriva login.seam e a
    # busca interna da URL base abaixo (cada tribunal tem seu host/path: /pje/,
    # /pje1grau/, /1g/...). O TOTP é o mesmo (TOTP_PDPJ) em todos.
    "TJCE — PJe":    ("pje",   "CEARA",              "https://pje.tjce.jus.br/pje1grau/", None),
    # ── GO ──────────────────────────────────────────────────────────────────
    # ROTA REAL do TJGO: os processos de GO estão no Projudi, não no eproc.
    # Login próprio (credencial "TJGO - Projudi") + OTP de 6 caracteres por
    # e-mail; depois escolhe-se a serventia da OAB-GO. Ver scrapers/projudi.py.
    "TJGO — Projudi": ("projudi", "GOIAS",            "https://projudi.tjgo.jus.br/", "GO"),
    # A plataforma eproc do TJSP aceita o perfil OAB-GO no login, mas NÃO
    # hospeda processos do TJGO — toda consulta volta "não encontrado"
    # (confirmado ao vivo). Mantida só para não quebrar seleções salvas; o
    # fallback por UF a coloca depois do Projudi (ver _PRIORIDADE_SITE).
    "TJGO — eproc":  ("eproc", "SAO PAULO",           "https://eproc1g.tjsp.jus.br/eproc/", "GO"),
    # ── MA ──────────────────────────────────────────────────────────────────
    "TJMA — PJe":    ("pje",   "MARANHAO",            "https://pje.tjma.jus.br/pje/", None),
    # ── MG ──────────────────────────────────────────────────────────────────
    "TJMG — eproc":  ("eproc", "MINAS GERAIS",        "https://eproc1g.tjmg.jus.br/eproc/", None),
    "TJMG — PJe":    ("pje",   "MINAS GERAIS",        "https://pje.tjmg.jus.br/pje/", None),
    # ── MS ──────────────────────────────────────────────────────────────────
    "TJMS — e-SAJ":  ("esaj",  "MATO GROSSO DO SUL",  "https://esaj.tjms.jus.br/cpopg/open.do", None),
    # ── PA ──────────────────────────────────────────────────────────────────
    "TJPA — PJe":    ("pje",   "PARA",                "https://pje.tjpa.jus.br/pje/", None),
    # ── PE ──────────────────────────────────────────────────────────────────
    "TJPE — PJe":    ("pje",   "PERNAMBUCO",          "https://pje.cloud.tjpe.jus.br/1g/", None),
    # ── PR ──────────────────────────────────────────────────────────────────
    # Acesso via perfil OAB-PR na plataforma eproc do TJSP (mesmo login/2FA).
    "TJPR — eproc":  ("eproc", "SAO PAULO",           "https://eproc1g.tjsp.jus.br/eproc/", "PR"),
    # ── RJ ──────────────────────────────────────────────────────────────────
    # CONSULTA PÚBLICA do TJRJ (www3.tjrj.jus.br/consultaprocessual): API REST
    # ABERTA — sem login, sem certificado, sem captcha (recuperar-site-key → 204).
    # O PJe do TJRJ exige certificado digital e o eproc/perfil-RJ do TJSP não
    # hospeda processos do TJRJ; esta consulta pública é a rota viável. 1ª inst.
    "TJRJ — Consulta": ("tjrj", "RIO DE JANEIRO", "https://www3.tjrj.jus.br/consultaprocessual/api/", None),
    # ── RN ──────────────────────────────────────────────────────────────────
    # Acesso via perfil OAB-RN na plataforma eproc do TJSP (mesmo login/2FA).
    "TJRN — eproc":  ("eproc", "SAO PAULO",           "https://eproc1g.tjsp.jus.br/eproc/", "RN"),
    # ── SC ──────────────────────────────────────────────────────────────────
    "TJSC — eproc":  ("eproc", "SANTA CATARINA",      "https://eproc1g.tjsc.jus.br/eproc/", "SC"),
}
_ESTADO_TJ_UF = {
    "SAO PAULO": ("TJSP", "SP"),
    "GOIAS": ("TJGO", "GO"),
    "MINAS GERAIS": ("TJMG", "MG"),
    "RIO DE JANEIRO": ("TJRJ", "RJ"),
    "DISTRITO FEDERAL": ("TJDFT", "DF"),
    "AMAZONAS": ("TJAM", "AM"),
    "MATO GROSSO DO SUL": ("TJMS", "MS"),
    "CEARA": ("TJCE", "CE"),
    "MARANHAO": ("TJMA", "MA"),
    "PARA": ("TJPA", "PA"),
    "PERNAMBUCO": ("TJPE", "PE"),
    "SANTA CATARINA": ("TJSC", "SC"),
}


def uf_efetiva_tj(tj_label: str) -> str:
    """UF que o sistema efetivamente atende (perfil da OAB ou UF do estado)."""
    if tj_label not in TJ_SISTEMAS:
        return ""
    _site, estado, _url, perfil = TJ_SISTEMAS[tj_label]
    return (perfil or _ESTADO_TJ_UF[estado][1]).upper()


# Rotas que LOGAM mas NUNCA acham processo: a plataforma eproc do TJSP aceita o
# perfil OAB de GO/PR/RN, mas não hospeda processos desses tribunais (confirmado
# ao vivo — toda consulta volta "não encontrado"). Continuam em TJ_SISTEMAS para
# não invalidar uma seleção salva em ui_state.json nem uma escolha manual, mas
# nenhuma sugestão ou fallback automático deve cair nelas.
ROTAS_INOPERANTES = frozenset({"TJGO — eproc", "TJPR — eproc", "TJRN — eproc"})


def rotulos_tj_selecionaveis() -> list[str]:
    """Rótulos que valem a pena oferecer na tela (sem as rotas comprovadamente
    inoperantes, que só gastariam login + 2FA para falhar)."""
    return [lbl for lbl in TJ_SISTEMAS if lbl not in ROTAS_INOPERANTES]


def sugerir_tj_para_uf(uf: str) -> str | None:
    """Rótulo de TJ_SISTEMAS cuja UF efetiva == uf. Prefere eproc; senão o 1º."""
    candidatos = candidatos_tj_para_uf(uf)
    if not candidatos:
        return None
    eproc = [lbl for lbl in candidatos if TJ_SISTEMAS[lbl][0] == "eproc"]
    return (eproc or candidatos)[0]


# Ordem de preferência por sistema no fallback entre tribunais da MESMA UF.
# Projudi, eproc e PJe expõem partes COM documento (necessário p/ pegar o réu);
# o e-SAJ público não traz CPF/CNPJ → fica por último. Hoje o Projudi só existe
# em GO, que não tem outra rota viva — a posição dele só passa a pesar quando
# outro estado de Projudi (PR, AM, BA...) ganhar scraper.
_PRIORIDADE_SITE = {"projudi": 0, "eproc": 1, "pje": 2, "esaj": 3}
# Override por UF: em MG os processos-alvo estão quase todos no PJe (o eproc tem
# pouquíssimos), então tentar PJe primeiro evita uma consulta eproc inútil.
_PRIORIDADE_SITE_POR_UF = {
    "MG": {"pje": 0, "eproc": 1, "esaj": 2},
}


def candidatos_tj_para_uf(uf: str) -> list[str]:
    """Todos os rótulos de TJ_SISTEMAS que atendem a UF, em ordem de preferência
    para enriquecer (sistemas que trazem partes com documento primeiro).

    Base do fallback pedido pelo usuário: se o processo não está num sistema,
    tenta o próximo (Projudi × eproc × e-SAJ × PJe). As rotas de
    `ROTAS_INOPERANTES` ficam de fora: tentá-las é login + 2FA jogados fora.
    Lista vazia = UF sem rota suportada."""
    uf = (uf or "").upper()
    prioridade = _PRIORIDADE_SITE_POR_UF.get(uf, _PRIORIDADE_SITE)
    candidatos = [
        lbl for lbl in TJ_SISTEMAS
        if uf_efetiva_tj(lbl) == uf and lbl not in ROTAS_INOPERANTES
    ]
    return sorted(candidatos, key=lambda lbl: prioridade.get(TJ_SISTEMAS[lbl][0], 99))

# OAB do advogado do escritório (Felipe Machado) por UF — usado para
# autopreencher os campos OAB UF / OAB Nº ao selecionar um processo.
OAB_ESCRITORIO_POR_UF: dict[str, str] = {
    "AM": "A 1543",
    "BA": "49.510",
    "CE": "37.928-A",
    "DF": "55.703",
    "ES": "26.487",
    "GO": "39.882-A",
    "MG": "104.784",
    "MS": "22.345-A",
    "MT": "21.457-A",
    "PE": "45.442",
    "PR": "70.339",
    "RJ": "186.328",
    "RN": "01.049-A",
    "SC": "51.064",
    "SP": "346.433",
}

# Padronização de layout do consolidado.
SPACE_AFTER_PT = 6
LINE_SPACING = 1.5

# Petições cujo corpo (zona BODY) tem menos que este total de caracteres são
# consideradas "curtas": ganham mais linhas em branco entre os blocos para
# ocupar melhor a folha, em vez do padrão de 1 linha em branco.
LIMITE_CHARS_PETICAO_CURTA = 700
LINHAS_VAZIAS_PETICAO_CURTA = 3
