# -*- coding: utf-8 -*-
"""Configurações: caminhos, mapa de TJ/sistema, OAB do escritório e layout."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import unicodedata
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


# --- atualizar senhas dos tribunais a partir da planilha do escritório -----
# `semear_config` de propósito NUNCA sobrescreve, para o update não apagar a
# senha que o usuário corrigiu na máquina. O efeito colateral é que uma senha
# trocada no escritório NÃO chegava em quem já usa o app: o arquivo local vence
# para sempre. A saída é a PLANILHA de senhas do escritório (ver
# `organizador.sheets`): o usuário pede, vê quais tribunais mudam e confirma.
# Nada aqui roda sozinho, e nada aqui apaga credencial.


def _valor_credencial(v) -> str:
    """Célula/campo → string utilizável, sem o `.0` que o Excel põe em campo
    numérico.

    Os DOIS lados do diff (local e planilha) têm de passar por aqui: se um lado
    normalizasse e o outro não, `123.0` × `123` viraria "alterada" para sempre.
    É a mesma função que `credenciais._to_str` usa — de propósito, uma só.
    """
    if v is None:
        return ""
    s = str(v).strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def _login_credencial(v) -> str:
    """`_valor_credencial` + recuperação do zero à esquerda do CPF.

    Login só-dígitos de 10 ou 11 caracteres é CPF, e célula NUMÉRICA de planilha
    come o zero da frente ("03015257650" vira "3015257650"). Sem isto, todo
    tribunal com CPF começado em zero apareceria como "alterada" para sempre e o
    login gravado falharia o e-SAJ. Com 9 dígitos ou menos não mexe: pode ser
    número de OAB, que o zfill corromperia.

    Aplicado só no lado da PLANILHA, nunca no arquivo local: é lá que a célula
    numérica come o zero. Rodar no local também poderia inflar para 11 dígitos um
    login de 10 que o escritório escreveu assim de propósito.
    """
    s = _valor_credencial(v)
    if s.isdigit() and 10 <= len(s) <= 11:
        return s.zfill(11)
    return s


def _chave_credencial(rotulo: str) -> str:
    """Chave de casamento de rótulo entre a planilha e o arquivo local.

    Equivale a `credenciais._norm` sem depender do unidecode (este módulo é
    importado na abertura e fica só na stdlib). É o que faz
    "TJMA – PJE – 1º Grau" (travessão) casar com "TJMA - PJE - 1o Grau" em vez
    de virar "nova" + "só local" — os rótulos reais misturam - e –.
    """
    s = unicodedata.normalize("NFKD", str(rotulo or ""))
    s = s.encode("ascii", "ignore").decode().lower()
    return re.sub(r"[\s\-]+", " ", s).strip()


def _itens_credenciais(caminho: Path) -> list[dict[str, str]]:
    """Lê um credenciais.json e devolve a lista normalizada, na ordem do arquivo.

    Tolerante de propósito (arquivo ausente/ilegível vira lista vazia): quem
    decide o que fazer com "não tem nada local" é o chamador.
    """
    try:
        payload = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    itens = payload.get("credenciais", payload) if isinstance(payload, dict) else payload
    if not isinstance(itens, list):
        return []
    saida: list[dict[str, str]] = []
    for i in itens:
        if not isinstance(i, dict):
            continue
        tj = _valor_credencial(i.get("tj"))
        if not tj:
            continue
        saida.append({
            "tj": tj,
            "login": _valor_credencial(i.get("login")),
            "senha": _valor_credencial(i.get("senha")),
        })
    return saida


def _mesclar_credenciais(
    locais: list[dict[str, str]], remotas: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Aplica a planilha sobre o que existe na máquina. NUNCA apaga.

    Duas defesas contra planilha meio preenchida (filtro ligado, coluna
    renomeada, download truncado) apagar senha que funcionava:

    * célula remota VAZIA não sobrescreve valor local preenchido;
    * rótulo que só existe aqui é mantido — a planilha acrescenta e corrige.

    Itera a LISTA local em vez de um dict por rótulo porque o credenciais.json
    real tem rótulo repetido ("TRF6 1º Grau" aparece duas vezes, com logins
    diferentes): um merge com dict colapsaria as duas linhas em uma e destruiria
    uma credencial de verdade. O texto do rótulo mantido é o local, para não
    churnar a grafia de travessão da máquina a cada sincronização.
    """
    remoto = {_chave_credencial(r["tj"]): r for r in remotas}
    saida: list[dict[str, str]] = []
    for local in locais:
        item = dict(local)
        r = remoto.get(_chave_credencial(local["tj"]))
        if r:
            if r["login"]:
                item["login"] = r["login"]
            if r["senha"]:
                item["senha"] = r["senha"]
        saida.append(item)
    vistos = {_chave_credencial(l["tj"]) for l in locais}
    saida.extend(r for r in remotas if _chave_credencial(r["tj"]) not in vistos)
    return saida


def planejar_credenciais(
    remotas: list[dict[str, str]],
) -> tuple[dict[str, list[str]], list[dict[str, str]]]:
    """Devolve ``(diff_de_rótulos, lista_final_a_gravar)``.

    O diff é derivado da lista final JÁ CALCULADA, não da comparação bruta com a
    planilha: assim o que o diálogo de confirmação mostra é provadamente o que o
    disco vai receber, sem deriva possível entre o aviso e a gravação.

    O diff traz SÓ RÓTULOS — nunca login, nunca senha — porque ele vai parar numa
    janela e num log. Chaves: ``alteradas`` (senha e/ou login muda), ``novas``
    (passa a existir aqui), ``so_local`` (só existe nesta máquina; fica).

    Sem guarda de ``CONFIG_DIR == CONFIG_SEMENTE_DIR``: aquela regra era do
    bundle (rodando do fonte não havia semente separada para comparar). Uma
    planilha remota existe e difere do local mesmo rodando do código-fonte.
    """
    locais = _itens_credenciais(CREDENCIAIS_PATH)
    final = _mesclar_credenciais(locais, remotas)
    antes = {_chave_credencial(l["tj"]): (l["login"], l["senha"]) for l in locais}
    remoto = {_chave_credencial(r["tj"]) for r in remotas}

    alteradas, novas, so_local = [], [], []
    for item in final:
        chave = _chave_credencial(item["tj"])
        anterior = antes.get(chave)
        if anterior is None:
            novas.append(item["tj"])
        elif chave not in remoto:
            so_local.append(item["tj"])
        elif anterior != (item["login"], item["senha"]):
            alteradas.append(item["tj"])
    return (
        {
            "alteradas": sorted(set(alteradas)),
            "novas": sorted(set(novas)),
            "so_local": sorted(set(so_local)),
        },
        final,
    )


def planejar_credenciais_da_planilha(
    log=print,
) -> tuple[dict[str, list[str]], list[dict[str, str]]]:
    """Lê a planilha de senhas do escritório e planeja o que mudaria aqui.

    Não grava nada. Propaga o ``RuntimeError`` de `sheets` (sem acesso, sem
    internet, planilha sem as colunas, nenhuma senha preenchida) — quem chama
    traduz com `sheets.explicar_falha_de_acesso`. Falha NÃO pode virar "está em
    dia" nem gravação de campo vazio.
    """
    from . import sheets  # tardio: sheets importa .config (ciclo no topo)

    return planejar_credenciais(sheets.coletar_credenciais_da_planilha(log=log))


def aplicar_credenciais(itens: list[dict[str, str]]) -> Path | None:
    """Grava a lista final em credenciais.json, guardando o anterior no .bak.

    Devolve o caminho do backup (``None`` se não havia arquivo). Levanta
    ``OSError`` se a gravação falhar: falhar em silêncio faria o app afirmar que
    atualizou a senha sem ter atualizado.

    Grava pelo `CREDENCIAIS_PATH` DESTE módulo, sem delegar para
    `credenciais.salvar_credenciais` — aquele módulo faz o bind do caminho no
    import, então o monkeypatch dos testes não o alcançaria e eles escreveriam na
    config real do usuário. O cache é invalidado aqui dentro (e não só na GUI)
    para nenhum chamador esquecer: sem isso a busca em curso continuaria tentando
    logar com a senha velha que já está em memória.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    backup = _backup_credenciais()
    payload = {
        "credenciais": [
            {
                "tj": _valor_credencial(i.get("tj")),
                "login": _valor_credencial(i.get("login")),
                "senha": _valor_credencial(i.get("senha")),
            }
            for i in itens
            if _valor_credencial(i.get("tj"))
        ]
    }
    CREDENCIAIS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    try:
        from .integracoes.tjsp_scraper.credenciais import _carregar_credenciais

        _carregar_credenciais.cache_clear()
    except Exception:
        pass
    return backup


def _backup_credenciais() -> Path | None:
    """Copia o credenciais.json atual para ``credenciais.json.bak``.

    Backup rolante: sobrescreve o da vez anterior. O usuário pode estar
    descartando uma edição local, então tem de haver volta de UM passo.
    """
    if not CREDENCIAIS_PATH.exists():
        return None
    backup = CREDENCIAIS_PATH.with_suffix(".json.bak")
    shutil.copy2(CREDENCIAIS_PATH, backup)
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
    # A consulta pública SÓ tem rota de detalhe para o sistema LEGADO do TJRJ
    # (`tipoProcesso: 1`, número antigo AAAA.CCC.NNNNNN-D). Os processos de
    # numeração 08* são do PJe (`13`) ou do eproc (`16`) e nela devolvem só um
    # cabeçalho stub — `processos/por-numero/publica` responde 412 para eles com
    # qualquer tipoProcesso. Daí esta segunda rota: o eproc do TJRJ (migração de
    # 2025), com Keycloak próprio. O PJe do TJRJ (tipo 13) continua sem rota:
    # exige certificado digital.
    "TJRJ — eproc":  ("eproc", "RIO DE JANEIRO",     "https://eproc1g.tjrj.jus.br/eproc/", None),
    # PJe do TJRJ (tipoProcesso 13). O host NÃO segue o padrão dos outros
    # tribunais: `pje.tjrj.jus.br` nem resolve em DNS — é `tjrj.pje.jus.br/1g/`,
    # que cai no mesmo PDPJ (sso.cloud.pje.jus.br, client_id pje-tjrj-1g) usado
    # pelas outras rotas PJe. O certificado digital é UMA das opções da tela,
    # não requisito.
    "TJRJ — PJe":    ("pje",   "RIO DE JANEIRO",     "https://tjrj.pje.jus.br/1g/", None),
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

# Sugestão da tela por UF, quando o palpite padrão (preferir eproc) não é o
# melhor. NÃO confundir com `_PRIORIDADE_SITE_POR_UF`, que ordena o FALLBACK do
# monitor: aqui é o "trocar para X?" que aparece quando o número digitado é de
# outra UF (ver gui.py `_buscar_online`), uma escolha só, feita por uma pessoa
# que vai olhar o resultado.
#
# RJ → PJe: dos três sistemas do RJ, o PJe é o que mais aparece na carteira, e
# quem digita um número avulso quase sempre quer esse. A consulta pública segue
# na frente no fallback automático (é a rota sem login), mas ali o custo de
# errar é uma requisição, não a atenção de alguém.
_SUGESTAO_POR_UF = {"RJ": "TJRJ — PJe"}


def rotulos_tj_selecionaveis() -> list[str]:
    """Rótulos que valem a pena oferecer na tela (sem as rotas comprovadamente
    inoperantes, que só gastariam login + 2FA para falhar)."""
    return [lbl for lbl in TJ_SISTEMAS if lbl not in ROTAS_INOPERANTES]


def sugerir_tj_para_uf(uf: str) -> str | None:
    """Rótulo de TJ_SISTEMAS cuja UF efetiva == uf, para pré-selecionar na tela.

    Ordem: override explícito da UF (`_SUGESTAO_POR_UF`), depois eproc (o sistema
    com extrator estruturado), depois o 1º candidato.

    NÃO confundir com a ordem do fallback do monitor (`candidatos_tj_para_uf`):
    lá MG tenta PJe antes do eproc e RJ tenta a consulta pública primeiro, mas
    aqui MG segue sugerindo eproc e RJ sugere PJe. São perguntas diferentes — o
    fallback erra de graça e segue para o próximo; a sugestão gasta a atenção de
    uma pessoa. A divergência de MG é decisão de projeto, coberta por
    tests/test_projudi_rota.py.
    """
    candidatos = candidatos_tj_para_uf(uf)
    if not candidatos:
        return None
    preferida = _SUGESTAO_POR_UF.get((uf or "").upper())
    if preferida in candidatos:  # ignora override que apontou p/ rota removida
        return preferida
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
    # RJ: a consulta pública primeiro porque é uma requisição HTTP sem login,
    # sem 2FA e sem navegador (~1 s) — ela resolve sozinha todo processo do
    # sistema legado. Só o que ela recusar (numeração 08*, do eproc) paga o
    # custo de abrir o Chrome e logar no eproc do TJRJ.
    "RJ": {"tjrj": 0, "eproc": 1, "pje": 2},
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
