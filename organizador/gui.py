# -*- coding: utf-8 -*-
"""Interface gráfica Tkinter (Organizador + Pesquisa de Processo).

ÍCONES DOS RÓTULOS: use só glifos do BMP (U+FFFF ou menos) que a fonte da
interface REALMENTE tenha — ↻ ▢ ◧ ▶ ▤ etc.
NÃO use emoji do plano astral (U+10000 e acima). O Windows não os tem na
fonte da interface, então o Tk faz fallback de fonte na PRIMEIRA vez que cada
glifo aparece, e isso custa DEZENAS de ms POR GLIFO — pago na construção do
widget, antes do primeiro desenho. Medido nesta janela: os 13 glifos astrais
custavam 622 ms dos 954 ms de ``App.__init__`` (65%); com os equivalentes BMP a
construção caiu para 332 ms. O custo é por glifo distinto e fica em cache
depois, então o preço é todo na abertura — exatamente onde as máquinas fracas
do escritório doem. ``tests/test_glifos_rotulos.py`` falha se um astral voltar.

CUIDADO ao escolher o substituto: "ser do BMP" não basta, a fonte tem de ter o
glifo. ``✏`` (U+270F) é BMP e custa 113 ms; ``◧`` custa 2,3 ms. Meça com
``python scripts/bench_glifos.py "◧↻▢"`` — UM processo por glifo, porque medir
vários no mesmo processo mente (o primeiro aquece o fallback e os seguintes
parecem baratos; foi assim que ``✏`` passou por "0,6 ms" numa primeira medição).
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from collections import OrderedDict
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox

from .perf import marca as _perf

_perf("import:inicio")

import ttkbootstrap as ttk
from ttkbootstrap.constants import *

from organizador import __version__

from . import navegadores
from .config import (
    APP_DATA_DIR, CAMINHO_GUIA_CREDENCIAIS, CAMINHO_ICONE, CONFIG_SEMENTE_DIR,
    OAB_ESCRITORIO_POR_UF, PASTA_PADRAO,
    PASTA_PETICOES, PROCESSOS_BASE_JSON, PROCESSOS_INDICE_JSON,
    PROCESSOS_ONLINE_DIR, TEMPLATES_CACHE_PATH, TJ_SISTEMAS, UI_STATE_PATH,
    rota_incompativel_com_numero, rotulos_tj_selecionaveis, semear_config,
    sugerir_tj_para_uf, uf_efetiva_tj,
)


def _abrir_no_navegador(url: str) -> None:
    """Abre `url` no navegador do sistema.

    Import tardio: `webbrowser` custa ~30 ms no import da GUI e só é usado em
    três botões (guia do 2FA e dois links do Google). Ninguém clica neles antes
    de a janela existir."""
    import webbrowser

    webbrowser.open(url)


def _atualizacao():
    """Módulo `atualizacao`, importado na primeira necessidade.

    Ele arrasta `urllib.request`/`ssl`/`http.client` — ~80 ms no import da GUI —
    e NADA disso é preciso para desenhar a janela: a verificação silenciosa roda
    em `after(1500)` e os outros usos (registrar no log, botão "Verificar
    atualização") são posteriores ou em ação do usuário."""
    from . import atualizacao

    return atualizacao


def _carregar_ui_state() -> dict:
    """Estado da UI (pastas da aba Pesquisa) lembrado entre execuções."""
    try:
        dados = json.loads(UI_STATE_PATH.read_text(encoding="utf-8"))
        return dados if isinstance(dados, dict) else {}
    except Exception:
        return {}


def _salvar_ui_state(**campos) -> None:
    """Mescla `campos` no estado persistido da UI. Descarta só None/'' (para não
    apagar pastas salvas); booleans False são persistidos (ex.: modo_dev=False)."""
    estado = _carregar_ui_state()
    estado.update({k: v for k, v in campos.items() if v not in (None, "")})
    try:
        UI_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        UI_STATE_PATH.write_text(
            json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


# -- cache dos placeholders dos modelos .docx -------------------------------
# Abrir os 39 modelos (13,5 MB) com python-docx custava ~3 s de CPU em toda
# abertura, para um resultado que só muda quando um modelo é editado. A chave
# inclui mtime e tamanho, então editar um .docx invalida só aquele.

def _chave_cache_template(caminho: Path) -> str:
    """Chave de cache de um modelo .docx.

    ATENÇÃO ao mtime: no .exe (onefile) os modelos são extraídos para
    ``sys._MEIPASS``, uma pasta nova a cada abertura, com timestamp novo. Uma
    chave com mtime NUNCA acertaria no executável — foi exatamente esse o bug que
    fazia o snapshot embutido ser reparseado toda vez (ver
    organizador/base_embutida.py).

    Então: empacotado, a chave usa o TAMANHO + a versão do app (os modelos vêm
    dentro da release, logo só mudam quando a versão muda). Rodando do fonte, o
    mtime é estável e continua sendo o sinal mais preciso.
    """
    try:
        st = caminho.stat()
        tamanho = st.st_size
        selo = __version__ if getattr(sys, "frozen", False) else int(st.st_mtime)
    except OSError:
        tamanho, selo = 0, 0
    return f"{caminho.name}|{tamanho}|{selo}"


def _carregar_cache_placeholders() -> dict[str, set[str]]:
    try:
        dados = json.loads(TEMPLATES_CACHE_PATH.read_text(encoding="utf-8"))
        if not isinstance(dados, dict):
            return {}
        return {
            str(k): set(v) for k, v in dados.items() if isinstance(v, (list, tuple))
        }
    except Exception:
        return {}


def _gravar_cache_placeholders(cache: dict[str, set[str]]) -> None:
    try:
        TEMPLATES_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = TEMPLATES_CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {k: sorted(v) for k, v in cache.items()}, ensure_ascii=False
            ),
            encoding="utf-8",
        )
        os.replace(tmp, TEMPLATES_CACHE_PATH)
    except Exception:
        pass
from .flexao import _construir_contexto, _eh_disjuncao_flexao
from .maquina import AJUSTES_MODO_LEVE, perfil_maquina, resolver_modo_leve
from .models import Parte, Processo
from .organizacao import (
    _autor, _reus_joined, organizar_pasta, salvar_processo_metadados, uf_do_cnj,
)
from .padroes import RE_NUMERO_CNJ
from .parsing import (
    _limpar_secretaria, _municipio_de_processo, _oab_completa, _uf_de_numero_cnj,
    aprender_comarcas, aprender_municipio,
)
# Só o módulo leve vem no topo (tabelas de placeholders + heurística de nome de
# arquivo, tudo lógica pura). As funções de organizador.peticoes puxam
# python-docx + lxml e são importadas dentro dos handlers que as usam — nada
# disso é necessário para abrir a janela.
from .placeholders import (
    PLACEHOLDERS_AUTO, PLACEHOLDERS_PETICAO, _template_eh_imobiliario,
)
from .pesquisa import (
    filtrar_processos_indexado, indexar_para_filtro, linhas_tree_processos,
    preencher_lacunas,
)
from .indice_processos import (
    ResumoProcesso, _resumo_de_processo, caminho_sqlite,
    carregar_processo_completo, carregar_resumos_pesquisa, esquecer_processo,
)
from .integracoes.tjsp_scraper.credenciais import listar_credenciais, salvar_credenciais
from .scraper import (
    CHAVES_2FA, CHAVES_AUTOJUR, _LogWriter, buscar_processo_online, fila_de_log,
    ler_config_2fa, ler_config_autojur, salvar_config_2fa, salvar_config_autojur,
)

_perf("import:fim")


# Mapeamento de templates combinados: chave = stem do template combinado,
# valores = lista de stems dos templates individuais que ele representa.
_GRUPOS_TEMPLATE: dict[str, list[str]] = {
    "Sisbajud, Renajud e Infojud": [
        "Sisbajud - modalidade teimosinha",
        "Renajud",
        "Infojud",
    ],
}

# Modos de debug da busca online (rótulo exibido -> valor passado ao scraper).
# "pos_login": para após logar, sem pesquisar o processo (inspecionar o painel).
# "pos_consulta": pesquisa o processo e extrai os dados, mas deixa o navegador
# aberto e loga um resumo para o usuário validar a extração.
_DEBUG_MODOS: dict[str, str] = {
    "Desligado": "off",
    "Pausar após login (sem pesquisar)": "pos_login",
    "Pausar após consulta (validar dados)": "pos_consulta",
}

def _word_disponivel() -> bool:
    """Existe algum conversor de PDF nesta máquina (Word OU LibreOffice)?

    ATENÇÃO ao que esta função NÃO pode voltar a ser: antes ela só testava
    `import win32com.client`. O pywin32 vem no bundle, então isso dava True até
    em máquina sem Office — o app marcava a opção "PDF" e a conversão só falhava
    na hora de gerar. Ver organizador/pdf.py."""
    from . import pdf

    return pdf.conversor_disponivel() is not None


def _converter_docx_para_pdf(docx_path: Path) -> Path:
    """Converte .docx para .pdf (Word via COM ou LibreOffice)."""
    from . import pdf

    return pdf.converter_para_pdf(docx_path)


def _converter_lote_docx_para_pdf(docx_paths: list, log=None) -> tuple[list, list]:
    """Converte vários .docx para .pdf reaproveitando um único Word aberto para
    o lote inteiro, em vez de abrir/fechar o Word a cada arquivo. Devolve
    (pdfs_gerados, mensagens_de_erro) — uma falha isolada não aborta o lote."""
    from . import pdf

    return pdf.converter_lote_para_pdf(docx_paths, log=log)


# -- nome dos anexos enviados ao Autojur ------------------------------------
# Convenção do escritório: o anexo chega ao Autojur JÁ NOMEADO e numerado na
# ordem em que será protocolado — "1. Petição - juntada de custas",
# "2. Comprovante de pagamento", ... O número é o que dá a ordem de leitura no
# protocolo; sem ele o campo vinha com o nome cru do arquivo gerado
# ("Comprovante de pagamento - 1234567-89.2020.8.26.0100.pdf") e a ordem no
# Autojur ficava por sorte.
_RE_CNJ_SUFIXO = re.compile(
    r"\s*[-–]\s*\d{7}-?\d{2}\.?\d{4}\.?\d\.?\d{2}\.?\d{4}\s*$"
)
# Numeração já presente no nome ("1. ", "01 - ", "2)"): removida antes de
# renumerar, senão reordenar/desmarcar ia empilhando prefixos ("2. 1. Petição").
_RE_NUM_PREFIXO = re.compile(r"^\s*\d{1,3}\s*[.)\-–]\s*")


def _nome_base_anexo_autojur(caminho, peticao: bool = False) -> str:
    """Nome legível do anexo: sem extensão, sem o nº do processo que a geração
    acrescenta e sem numeração. `peticao=True` (arquivo saído da geração de
    petições) prefixa "Petição - ", que é como a peça é identificada no
    protocolo — salvo quando o nome já começa por "Petição"."""
    bruto = Path(caminho).stem
    base = _RE_NUM_PREFIXO.sub("", _RE_CNJ_SUFIXO.sub("", bruto)).strip()
    if not base:
        return bruto.strip() or Path(caminho).name
    if peticao and not base.lower().startswith(("petição", "peticao")):
        base = f"Petição - {base}"
    return base


def _prenomear_anexo_autojur(indice: int, caminho, peticao: bool = False) -> str:
    """Nome pré-preenchido do anexo: "<n>. <nome>" (`indice` é 1-based)."""
    return f"{indice}. {_nome_base_anexo_autojur(caminho, peticao)}"


def _renumerar_anexos_autojur(nomes: list) -> list:
    """Reaplica a numeração 1..N na ordem dada, preservando o que a pessoa
    editou depois do prefixo. Necessário porque desmarcar um arquivo da lista
    deixaria a sequência furada ("2., 3." sem o 1)."""
    return [
        f"{i}. {_RE_NUM_PREFIXO.sub('', (nome or '').strip()).strip()}"
        for i, nome in enumerate(nomes, 1)
    ]


# Fontes e constantes visuais
_FONT_BASE = ("Segoe UI", 11)
_FONT_BOLD = ("Segoe UI", 11, "bold")
_FONT_MONO = ("Consolas", 10)
_FONT_TITLE = ("Segoe UI", 12, "bold")
_PAD_X = 12
_PAD_Y = 6
# Intervalo da animação das barras indeterminadas. 15 ms davam ~66 fps de
# barra listrada: puro desperdício de CPU num i3, e imperceptível a olho nu.
_PROGRESS_MS = 80

# Quanto o fechamento espera pelos `quit()` antes de partir para o `taskkill`.
# Um `quit()` limpo de Chrome headless levou 2,1 s aqui; 8 s cobre dois
# navegadores em paralelo numa máquina fraca. Passado o prazo, o que restou é
# morto à força — deixar um chrome.exe órfão de 300-500 MB é pior que o kill.
_PRAZO_QUIT_S = 8.0

# Sessão OCULTA do Autojur parada: quanto tempo até o navegador ser encerrado
# sozinho. Reabrir é barato (o perfil persistente dispensa novo 2FA), enquanto
# manter uma sessão invisível custa 300-500 MB de RAM até o app fechar.
_AUTOJUR_OCIOSO_LEVE_S = 300.0    # 5 min na máquina fraca
_AUTOJUR_OCIOSO_S = 900.0         # 15 min nas demais
# De quanto em quanto tempo a ociosidade é conferida (o pump roda a cada
# 200-300 ms; esta checagem não pode entrar em todo tique).
_CHECAGEM_OCIOSO_S = 30.0

# A varredura de órfãos de execuções ANTERIORES só começa alguns segundos depois
# de a janela aparecer: ela sobe um PowerShell que percorre a tabela de processos
# (0,9 s aqui, 1-3 s num i3) e não pode competir com o primeiro desenho.
_ATRASO_VARREDURA_MS = 4000


def _descrever_erro(exc: BaseException) -> str:
    """Texto de erro que sempre diz ALGO. `str(exc)` sozinho não serve.

    Exceção do Selenium formata `str()` como "Message: {msg}" — e
    `WebDriverWait.until()` sem `message=` levanta TimeoutException com msg
    vazia. O resultado chegava na tela como um diálogo com a palavra "Message:"
    e nada mais, sem nem dizer que era timeout (visto ao vivo em 27/08/2026, no
    fluxo de tarefa do Autojur). Prefixar o tipo garante um mínimo acionável
    mesmo quando quem levantou não escreveu mensagem.
    """
    texto = str(exc).strip()
    tipo = type(exc).__name__
    if not texto or texto in ("Message:", "Message: None"):
        return f"{tipo} (sem mensagem)"
    if texto.startswith("Message:"):
        texto = texto[len("Message:"):].strip()
    return f"{tipo}: {texto}" if texto else f"{tipo} (sem mensagem)"


class App(ttk.Window):
    def __init__(self) -> None:
        super().__init__(title=f"Organizador de Processos — eproc — v{__version__}", themename="litera")
        with contextlib.suppress(Exception):
            self.iconbitmap(str(CAMINHO_ICONE))
        # Geometria responsiva: ~90% da tela (com teto), centrada. Mantém a
        # janela redimensionável/maximizável; o layout interno usa fill/expand
        # e pesos de grid para fluir em qualquer tamanho.
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(1500, int(sw * 0.9))
        h = min(950, int(sh * 0.9))
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 3)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(900, 600)
        self.configure(background="#f0f2f5")
        self.option_add("*TCombobox*Listbox.font", _FONT_BASE)

        # Estado compartilhado / aba Organizador
        self.var_origem = tk.StringVar(value=PASTA_PADRAO)
        self.var_destino = tk.StringVar(value=PASTA_PADRAO)
        self.var_status = tk.StringVar(value="Pronto.")
        # Filas de log com teto (descartam o mais antigo): os workers de
        # scraping produzem mais rápido do que a GUI drena numa passada longa.
        self._log_queue: queue.Queue[str] = fila_de_log()
        # Filas thread-safe para atualizações de UI a partir de threads worker.
        # Tkinter NÃO é thread-safe; toda mutação visual passa por essas filas
        # e é drenada em _drain_ui_queues (rodando na main thread).
        self._progress_queue: queue.Queue[tuple[int, int]] = queue.Queue()
        self._status_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._cancel_event = threading.Event()
        self._progress_var = tk.IntVar(value=0)
        self._progress_max = tk.IntVar(value=100)

        # Estado da aba Pesquisa de Processo. Restaura as pastas usadas na última
        # sessão (para pré-carregar os processos já trabalhados ao reabrir).
        _ui_state = _carregar_ui_state()
        self.var_pesquisa_base = tk.StringVar(value=_ui_state.get("pesquisa_base") or PASTA_PADRAO)
        self.var_pesquisa_destino = tk.StringVar(value=_ui_state.get("pesquisa_destino") or "")
        # Modo desenvolvedor: quando ligado, revela a aba Organizador, as opções
        # avançadas da busca (baixar docs, navegador invisível, modo debug) e a
        # seção 2FA das configurações. Desligado por padrão; lembrado entre sessões.
        self.var_modo_dev = tk.BooleanVar(value=bool(_ui_state.get("modo_dev", False)))
        # Preferência global: salvar de fato as tarefas no Autojur. Desligada no
        # teste manual (só preenche p/ conferência); ligada em produção. É o
        # PADRÃO dos toggles de "Salvar" (modal de envio e aba Autojur); lembrada
        # entre sessões, configurada em Configurações.
        self.var_autojur_salvar_prod = tk.BooleanVar(
            value=bool(_ui_state.get("autojur_salvar", False))
        )
        # Visualizar o navegador do Autojur mesmo em produção (independe do modo
        # dev). O navegador é SEMPRE visível quando "salvar" está desligado
        # (conferência); este toggle força visível também com "salvar" ligado.
        self.var_autojur_visualizar = tk.BooleanVar(
            value=bool(_ui_state.get("autojur_visualizar", False))
        )
        # Modo leve: detectado na 1ª execução (RAM/núcleos) e depois lembrado.
        # Ver organizador/maquina.py para o que ele muda.
        self._perfil_maquina = perfil_maquina()
        self.var_modo_leve = tk.BooleanVar(
            value=resolver_modo_leve(_ui_state.get("modo_leve"))
        )
        # Persiste a decisão na 1ª execução, para a máquina não ser remedida (e
        # para o valor aparecer marcado em Configurações).
        if "modo_leve" not in _ui_state:
            _salvar_ui_state(modo_leve=bool(self.var_modo_leve.get()))
        self._modo_leve = bool(self.var_modo_leve.get())

        # Pré-login: loga no Autojur OCULTO em 2º plano ao abrir o app, para a
        # sessão já estar pronta na hora de gerar a tarefa (vira visível sob
        # demanda p/ conferência). Ligado por padrão em máquina capaz; DESLIGADO
        # em modo leve, onde subir um Chrome headless na abertura (300-500 MB)
        # é o maior gasto de memória do app.
        self.var_autojur_prelogin = tk.BooleanVar(
            value=bool(_ui_state.get("autojur_prelogin", not self._modo_leve))
        )
        self._dev_widgets_busca: list = []  # widgets avançados da aba Pesquisa
        # Barra única: filtra a lista ao vivo e, no Enter/botão, busca online
        # (só vai à web se o processo ainda não estiver carregado).
        self.var_filtro = tk.StringVar(value="")
        self.var_busca_tj = tk.StringVar(value="TJSP — eproc")
        self.var_busca_headless = tk.BooleanVar(value=True)
        self.var_busca_docs = tk.BooleanVar(value=False)  # False → modo_docs "nenhum"; True → "atos"
        self.var_busca_debug = tk.StringVar(value="Desligado")  # ver _DEBUG_MODOS
        # Forçar busca na web mesmo quando o processo já está no cache local
        # (planilhas importadas / buscas anteriores). Útil p/ atualizar um
        # andamento desatualizado. Não é lembrado entre sessões (padrão: desligado).
        self.var_busca_forcar = tk.BooleanVar(value=False)
        # Scraper retido em modo debug: mantém o navegador aberto (evita coleta
        # de lixo) e permite fechá-lo depois pelo botão "Fechar navegador debug".
        self._debug_scraper: object | None = None
        # Estado da aba Autojur (dev): o scraper logado é retido com o navegador
        # ABERTO (base para montar as próximas automações) até "Fechar navegador".
        self._autojur_scraper: object | None = None
        # Visibilidade com que o scraper retido foi criado (headless True/False):
        # se um envio precisar de visibilidade diferente, recriamos o navegador.
        self._autojur_scraper_headless: bool | None = None
        # Serializa a criação/relogin do scraper entre o worker de pré-login e o
        # de envio (evita abrir dois navegadores concorrentes no mesmo perfil).
        self._autojur_login_lock = threading.Lock()
        # Ociosidade da sessão retida (ver _fechar_autojur_ocioso). O relógio é
        # monotônico: mudança de horário/horário de verão não pode encurtá-lo.
        self._autojur_ultimo_uso = time.monotonic()
        self._autojur_ocioso_s = (
            _AUTOJUR_OCIOSO_LEVE_S if self._modo_leve else _AUTOJUR_OCIOSO_S
        )
        self._proxima_checagem_ocioso = time.monotonic() + _CHECAGEM_OCIOSO_S
        self._autojur_prelogin_worker: threading.Thread | None = None
        self._autojur_worker: threading.Thread | None = None
        self._autojur_log_queue: queue.Queue[str] = fila_de_log()
        self._autojur_result_queue: queue.Queue = queue.Queue()
        # Campos da tarefa de Protocolo (aba Autojur).
        self.var_autojur_processo = tk.StringVar(value="")
        self.var_autojur_local = tk.StringVar(value="")
        self.var_autojur_lote = tk.StringVar(value="dia seguinte")
        self.var_autojur_pdf = tk.StringVar(value="")
        # Default herdado da preferência global (Configurações → "Salvar no Autojur").
        self.var_autojur_salvar = tk.BooleanVar(value=bool(self.var_autojur_salvar_prod.get()))
        self._autojur_tarefa_worker: threading.Thread | None = None
        self._autojur_tarefa_result_queue: queue.Queue = queue.Queue()
        # Pedidos de escolha quando a busca traz >1 resultado: o worker enfileira
        # (resultados, Event, box) e bloqueia no Event; a main thread mostra o
        # modal e devolve o índice em box['idx'] (Tkinter só é seguro na main).
        self._autojur_escolha_queue: queue.Queue = queue.Queue()
        # Integração aba Petições -> Autojur: PDFs da última geração e o worker
        # que cria as tarefas de Protocolo (1 por PDF) no processo selecionado.
        self._ultimos_pdfs_gerados: list = []
        # Lista completa dos arquivos da última geração (docx + pdf), usada para
        # re-renderizar o painel após reconverter um Word editado para PDF.
        self._ultimos_arquivos_gerados: list = []
        self._pdf_reconv_worker: threading.Thread | None = None
        self._envio_autojur_worker: threading.Thread | None = None
        self._envio_autojur_result_queue: queue.Queue = queue.Queue()
        self.var_secretaria = tk.StringVar(value="")
        self.var_municipio = tk.StringVar(value="")
        self.var_estado = tk.StringVar(value="")
        self.var_oab_uf = tk.StringVar(value="")
        self.var_oab_numero = tk.StringVar(value="")
        self.var_endereco_reu = tk.StringVar(value="")
        self.var_valor_causa = tk.StringVar(value="")
        self.var_comarca = tk.StringVar(value="")
        self.var_vara = tk.StringVar(value="")
        self.var_tribunal = tk.StringVar(value="")
        self.var_endereco_foro = tk.StringVar(value="")
        self.var_juiz = tk.StringVar(value="")
        self.var_assunto = tk.StringVar(value="")
        self._entries_auto: dict[str, tk.Entry] = {}
        # Lista da aba Pesquisa: só RESUMOS (rápido de carregar). O Processo
        # completo é carregado sob demanda ao selecionar e memorizado em _full_cache.
        self._processos_carregados: list[ResumoProcesso] = []
        # Texto de busca pré-computado, alinhado a _processos_carregados: evita
        # remontar as strings de filtro da base inteira a cada tecla digitada.
        self._textos_filtro: list[str] = []
        # Teto de linhas inseridas na Treeview por vez (ela mostra 12). Inserir as
        # ~3.200 era uma chamada Tcl por linha, refeita a cada tecla.
        self._max_linhas_tree = 300 if self._modo_leve else 1000
        self._debounce_filtro_ms = 250 if self._modo_leve else 120
        self.var_contagem_lista = tk.StringVar(value="")
        # Teto de linhas nos logs da tela (ver _append_log): sem ele, uma passada
        # de monitoramento deixava 6-17 mil linhas presas nos widgets.
        self._max_linhas_log = 500 if self._modo_leve else 2000
        # Cache do Processo COMPLETO por linha selecionada. É um LRU: cada
        # Processo carrega todos os eventos (~50 em média, alguns centenas), e
        # antes o dict crescia sem limite — navegar por algumas centenas de
        # linhas retinha RAM até fechar o app. Recarregar custa 3,6 ms (SQLite)
        # ou uma leitura de metadados.json, barato demais para valer memória
        # ilimitada numa máquina de 4 GB.
        self._max_full_cache = 20 if self._modo_leve else 60
        self._full_cache: OrderedDict[str, Processo] = OrderedDict()
        # Pedido pendente do botão "Atualizar processo": (número, instantâneo de
        # como ele aparecia antes de ir ao tribunal). O fim da busca compara e
        # diz o que mudou. Ver _atualizar_processo_selecionado.
        self._comparar_apos_busca: "tuple[str, dict[str, str]] | None" = None
        self._proc_selecionado: Processo | None = None
        self._templates_vars: dict[Path, tk.BooleanVar] = {}
        self.var_documento_unico = tk.BooleanVar(value=True)
        self.var_multiplos_imoveis = tk.BooleanVar(value=False)
        # Detectado depois que a janela abre. Importar win32com/COM durante o
        # startup é custo fixo para quem só quer gerar Word.
        self.var_gerar_pdf = tk.BooleanVar(value=False)
        self._cb_pdf = None
        self._lbl_pdf_status = None
        self._pesquisa_log_queue: queue.Queue[str] = fila_de_log()
        self._pesquisa_load_queue: queue.Queue = queue.Queue()
        self._pesquisa_load_worker: threading.Thread | None = None
        self._filtro_after_id: str | None = None
        self._gen_worker: threading.Thread | None = None
        self._busca_worker: threading.Thread | None = None
        self._busca_result_queue: queue.Queue = queue.Queue()
        self._gen_result_queue: queue.Queue = queue.Queue()
        # Monitoramento (planilhas → mapeamento de sistema): worker em 2º plano,
        # fila de resultado e Event de cancelamento (botão "Parar"). O motor é
        # organizador.monitoramento.rodar_passada().
        self._monitor_worker: threading.Thread | None = None
        self._monitor_result_queue: queue.Queue = queue.Queue()
        self._monitor_cancel: threading.Event = threading.Event()
        # Conferir a planilha de senhas do escritório (aba Configurações).
        self._senhas_worker: threading.Thread | None = None
        self._senhas_result_queue: queue.Queue = queue.Queue()
        # Notificações de conclusão (popup + som + status). Fila thread-safe:
        # workers chamam _notificar(); o popup é mostrado na main thread.
        # _notify_busy evita empilhar diálogos durante o loop modal aninhado.
        self._notify_queue: queue.Queue[tuple[str, str, str, str | None]] = queue.Queue()
        self._notify_busy = False
        # Visibilidade dos logs: ocultos por padrão, alternáveis via "Ver detalhes".
        self._log_org_visivel = False
        self._log_pesq_visivel = False
        # Estado da barra de progresso indeterminada (animada) da aba Pesquisa.
        self._pesquisa_em_progresso = False

        # Form inteligente: estado para destaque baseado nos templates marcados.
        self._templates_placeholders: dict[Path, set[str]] = {}
        self._campos_obrigatorios: set[str] = set()
        self._campos_desconhecidos: set[str] = set()
        self._labels_edit: dict[str, ttk.Label] = {}
        self._entries_edit: dict[str, ttk.Entry] = {}
        self._rotulos_edit: dict[str, str] = {}
        self._dinamicos: dict[str, tuple[ttk.Label, ttk.Entry, tk.StringVar]] = {}
        self._frame_dinamicos: ttk.Labelframe | None = None
        # Seção "Gerar petições" — referência para manter os campos dinâmicos
        # ACIMA dela (o botão de gerar fica sempre por último).
        self._section_gen: ttk.Labelframe | None = None
        self._frame_gerados: ttk.Frame | None = None
        self._frame_imoveis: ttk.Frame | None = None
        self._chk_imoveis: ttk.Checkbutton | None = None
        self._scan_tpls_thread: threading.Thread | None = None
        self._templates_dir_atual: Path | None = None
        # Checkbuttons de petição + seu texto original (para requebrar ao redimensionar).
        self._templates_checkbtns: list[tuple[ttk.Checkbutton, str]] = []
        self._templates_ncols = 1

        # Espelha Secretaria ↔ Vara: alterar um campo replica no outro.
        # Flag de guarda evita loop infinito quando o trace dispara reciprocamente.
        self._espelhando_sec_vara = False
        # Flag de guarda para propagação de grupo de templates combinados.
        self._atualizando_grupo = False
        self.var_secretaria.trace_add("write", self._espelhar_secretaria_para_vara)
        self.var_vara.trace_add("write", self._espelhar_vara_para_secretaria)

        # Estado da aba Configurações (editor de senhas, persistido entre sessões).
        # _cred_rows: uma linha por credencial de tribunal; _autojur_rows: login
        # do Autojur; _2fa_rows: um campo por segredo de 2FA. Os Entry de senha
        # guardam a referência para o toggle de visibilidade ("Mostrar senhas").
        self._cred_rows: list[dict] = []
        self._autojur_rows: dict[str, dict] = {}
        self._2fa_rows: dict[str, dict] = {}
        self._entries_senha: list[ttk.Entry] = []
        self.var_mostrar_senhas = tk.BooleanVar(value=False)
        self._frame_cred: ttk.Frame | None = None

        _perf("app:estado_pronto")
        self._build_widgets()
        _perf("app:widgets_prontos")
        # UM temporizador só (ver _pump). Antes eram 13 laços de 100-200 ms se
        # reagendando para sempre: ~85 callbacks Tcl por segundo com o app
        # parado, o que num i3 é CPU queimada sem nada acontecendo.
        self._pump_ms = 300 if self._modo_leve else 200
        self.after(self._pump_ms, self._pump)
        # Pré-carrega os processos já trabalhados (pasta base lembrada), para
        # adicionar novas petições a um processo existente sem recarregar à mão.
        self.after(300, self._carregar_pesquisa_inicial)
        # Modelos de petição disponíveis já na abertura: permite escolher o
        # modelo e preencher tudo à mão (gerar sem buscar/selecionar processo).
        self.after(300, self._carregar_templates)
        # Verifica atualizações em silêncio na abertura: só mostra a janela se
        # houver versão nova (caso contrário não exibe nada). O próprio
        # Peticoes.exe se auto-atualiza — ver organizador/atualizacao.py.
        self.after(1500, self._verificar_atualizacao_silenciosa)
        # NÃO existe disparo automático de monitoramento. Havia um: 2,5 s depois
        # de abrir, se tivessem passado 7 dias, o app começava sozinho uma passada
        # que baixa 11 planilhas, sobe navegadores e consulta os tribunais por
        # horas — com a máquina ocupada e sem o usuário ter pedido. Hoje a passada
        # sai só pelo botão "Sincronizar planilhas & mapear" na aba Pesquisa.
        # Sem isso, fechar a janela (X) só destrói os widgets: navegadores do
        # Autojur/debug retidos e a passada de monitoramento em curso ficam
        # órfãos rodando em segundo plano (chrome.exe de 300-500MB cada).
        self.protocol("WM_DELETE_WINDOW", self._ao_fechar)

    def _ao_fechar(self) -> None:
        """Encerra navegadores retidos e sinaliza cancelamento aos workers
        antes de destruir a janela. Best-effort: nada aqui pode impedir o
        fechamento, mesmo se algum navegador já tiver sido morto por fora.

        POR QUE ESPERA (e por que mata no fim)
        --------------------------------------
        A versão anterior disparava os `fechar()` em threads DAEMON e chamava
        `destroy()` na linha seguinte. O interpretador mata thread daemon na
        saída do processo, e um `quit()` limpo leva ~2,1 s: o `quit()` morria no
        meio. Reproduzido ao vivo em 25/08/2026 — sobravam um chromedriver.exe
        com o pai morto e 8 chrome.exe pendurados nele, 491 MB INVISÍVEIS
        (headless não tem janela nem ícone), vivos até o reboot. Era a lentidão
        relatada "depois de usar o programa".

        As threads continuam daemon (um `quit()` travado não pode prender o app
        para sempre), mas agora são esperadas até `_PRAZO_QUIT_S`; o que passar
        do prazo — e todo navegador preso numa thread worker, que este método não
        tem como alcançar — morre por `taskkill` via o registro de vivos.
        """
        if self._monitor_worker and self._monitor_worker.is_alive():
            self._monitor_cancel.set()
        if self._worker and self._worker.is_alive():
            self._cancel_event.set()
        # A janela sai da tela AGORA: o resto é espera de processo morrendo e
        # não pode parecer travamento.
        with contextlib.suppress(Exception):
            self.withdraw()
            self.update_idletasks()
        threads = [
            t for t in (
                self._fechar_em_thread(self._autojur_scraper),
                self._fechar_em_thread(self._debug_scraper),
            ) if t is not None
        ]
        self._autojur_scraper = None
        self._debug_scraper = None
        prazo = time.monotonic() + _PRAZO_QUIT_S
        for t in threads:
            t.join(max(0.0, prazo - time.monotonic()))
        mortos = navegadores.matar_registrados()
        if mortos:
            with contextlib.suppress(Exception):
                _atualizacao().registrar(
                    f"fechamento: {mortos} navegador(es) encerrado(s) à força "
                    "(quit() não terminou no prazo ou estava preso em worker)"
                )
        self.destroy()

    @staticmethod
    def _fechar_em_thread(scraper: object) -> "threading.Thread | None":
        """Fecha o navegador de `scraper` fora da main thread (o `quit()` leva
        segundos e congelaria a janela). Devolve a thread — quem precisa esperar
        pelo fim, espera; quem não, ignora. None se não havia scraper."""
        if scraper is None:
            return None

        def run() -> None:
            with contextlib.suppress(Exception):
                scraper.fechar()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    def _build_widgets(self) -> None:
        nb = ttk.Notebook(self, bootstyle="primary")
        nb.pack(fill="both", expand=True, padx=_PAD_X, pady=(_PAD_X, 0))

        frm_org = ttk.Frame(nb, padding=8)
        frm_pesq = ttk.Frame(nb, padding=8)
        frm_autojur = ttk.Frame(nb, padding=8)
        frm_config = ttk.Frame(nb, padding=8)
        nb.add(frm_org, text="  Organizador  ")
        nb.add(frm_pesq, text="  Pesquisa de Processo  ")
        nb.add(frm_autojur, text="  Autojur  ")
        nb.add(frm_config, text="  Configurações  ")
        # Referências p/ mostrar/ocultar conforme o modo dev.
        self._nb = nb
        self._tab_org = frm_org
        self._tab_pesq = frm_pesq
        self._tab_autojur = frm_autojur

        # Construção TARDIA das abas que não são a inicial.
        #
        # Medido (App.__init__ = 1.696 ms nesta máquina):
        #   Pesquisa    995 ms  <- é a aba que abre; tem de existir já
        #   Config      381 ms  <- 311 ms disso é ler credenciais.json + .env
        #   Autojur      85 ms
        #   Organizador  50 ms  <- e fica ESCONDIDA fora do modo dev
        # Adiar Config e Organizador tira 431 ms da abertura (num i3 3-5× mais
        # lento, 1,5-2 s de janela travada) sem mudar nada do que o usuário vê:
        # a aba só é montada quando ele a seleciona.
        #
        # A Autojur continua sendo construída aqui de propósito: são só 85 ms e
        # os widgets dela (`progress_autojur`, `_btn_autojur_*`, `txt_autojur`)
        # são tocados pelo pré-login que dispara na ABERTURA e pelo envio
        # Petições → Autojur, que começa em outra aba.
        self._build_aba_pesquisa(frm_pesq)
        self._build_aba_autojur(frm_autojur)
        self._abas_tardias: dict[str, tuple[ttk.Frame, object]] = {
            str(frm_org): (frm_org, self._build_aba_organizador),
            str(frm_config): (frm_config, self._build_aba_config),
        }
        nb.bind("<<NotebookTabChanged>>", self._on_troca_de_aba)
        self._aplicar_modo_dev()

        # Barra de status moderna
        bar = ttk.Frame(self, bootstyle="secondary")
        bar.pack(fill="x", side="bottom")
        ttk.Label(
            bar, textvariable=self.var_status, anchor="w",
            font=_FONT_BASE, bootstyle="inverse-secondary",
        ).pack(fill="x", padx=_PAD_X, pady=5)

    def _on_troca_de_aba(self, _event=None) -> None:
        """Monta a aba selecionada, se ainda não foi montada."""
        with contextlib.suppress(Exception):
            self._garantir_aba(self._nb.select())

    def _garantir_aba(self, frame) -> None:
        """Garante que a aba `frame` esteja construída. Idempotente.

        Aceita o widget ou o nome dele (o que `Notebook.select()` devolve), para
        poder ser chamada tanto pelo evento quanto por código que precise mexer
        nos widgets de uma aba que talvez ainda não tenha sido aberta."""
        chave = str(frame)
        pendente = getattr(self, "_abas_tardias", {}).pop(chave, None)
        if pendente is None:
            return
        widget, construtor = pendente
        construtor(widget)
        # Os widgets recém-criados precisam receber as regras de visibilidade do
        # modo dev (ex.: a seção 2FA das Configurações).
        self._aplicar_modo_dev()

    def _build_aba_organizador(self, parent: ttk.Frame) -> None:
        # ── Seção: Pastas ──────────────────────────────────────────────
        section_folders = ttk.Labelframe(
            parent, text="  Pastas  ", bootstyle="default",
            padding=(12, 8),
        )
        section_folders.pack(fill="x", pady=(0, 8))

        frm = ttk.Frame(section_folders)
        frm.pack(fill="x")
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Origem:", font=_FONT_BASE, width=12, anchor="e").grid(
            row=0, column=0, sticky="e", padx=(0, 6), pady=4,
        )
        ttk.Entry(
            frm, textvariable=self.var_origem, font=_FONT_BASE,
            bootstyle="default",
        ).grid(row=0, column=1, sticky="ew", padx=(0, 6), pady=4)
        ttk.Button(
            frm, text="Escolher...", command=self._escolher_origem,
            bootstyle="outline", width=12,
        ).grid(row=0, column=2, pady=4)

        ttk.Label(frm, text="Destino:", font=_FONT_BASE, width=12, anchor="e").grid(
            row=1, column=0, sticky="e", padx=(0, 6), pady=4,
        )
        ttk.Entry(
            frm, textvariable=self.var_destino, font=_FONT_BASE,
            bootstyle="default",
        ).grid(row=1, column=1, sticky="ew", padx=(0, 6), pady=4)
        ttk.Button(
            frm, text="Escolher...", command=self._escolher_destino,
            bootstyle="outline", width=12,
        ).grid(row=1, column=2, pady=4)

        # ── Seção: Ações ───────────────────────────────────────────────
        section_actions = ttk.Labelframe(
            parent, text="  Ações  ", bootstyle="default",
            padding=(12, 8),
        )
        section_actions.pack(fill="x", pady=(0, 8))

        botoes = ttk.Frame(section_actions)
        botoes.pack(fill="x")
        self.btn_run = ttk.Button(
            botoes, text="▶  Organizar", command=self._on_run,
            bootstyle="success", width=16,
        )
        self.btn_run.pack(side="left")
        self.btn_cancel = ttk.Button(
            botoes, text="Cancelar", command=self._on_cancel,
            bootstyle="danger-outline", state="disabled", width=12,
        )
        self.btn_cancel.pack(side="left", padx=(8, 0))
        ttk.Button(
            botoes, text="Abrir pasta de destino", command=self._abrir_destino,
            bootstyle="outline", width=20,
        ).pack(side="left", padx=(12, 0))
        ttk.Button(
            botoes, text="Limpar log", command=self._limpar_log,
            bootstyle="outline-secondary", width=12,
        ).pack(side="left", padx=(8, 0))
        self.btn_detalhes_org = ttk.Button(
            botoes, text="Ver detalhes ▾", command=self._toggle_log_org,
            bootstyle="outline-secondary", width=16,
        )
        self.btn_detalhes_org.pack(side="left", padx=(8, 0))

        self.progress = ttk.Progressbar(
            section_actions, orient="horizontal",
            variable=self._progress_var, maximum=100,
            mode="determinate", bootstyle="success-striped",
        )
        self.progress.pack(fill="x", pady=(8, 0))

        # ── Seção: Log (oculta por padrão; alternada por "Ver detalhes") ─
        self._section_log_org = ttk.Labelframe(
            parent, text="  Log  ", bootstyle="default",
            padding=(12, 8),
        )
        # Não empacotada aqui: fica oculta até o usuário clicar em "Ver detalhes".
        section_log = self._section_log_org

        self.txt = tk.Text(
            section_log, height=14, wrap="word", state="disabled",
            font=_FONT_MONO, bg="#fafbfc", relief="flat",
            borderwidth=1, highlightthickness=1,
            highlightcolor="#d0d5dd", highlightbackground="#d0d5dd",
        )
        self.txt.pack(fill="both", expand=True)

    def _build_aba_pesquisa(self, parent: ttk.Frame) -> None:
        # Área rolável: com muitas categorias de modelo ou janela pequena, o
        # conteúdo da aba passa da altura disponível. Sem isto o excedente
        # simplesmente ficava inacessível — agora rola independente do
        # tamanho da tela, como na aba Configurações.
        pesq_canvas = tk.Canvas(parent, highlightthickness=0, bg="#f0f2f5")
        pesq_scroll = ttk.Scrollbar(
            parent, orient="vertical", command=pesq_canvas.yview, bootstyle="round",
        )
        pesq_canvas.configure(yscrollcommand=pesq_scroll.set)
        pesq_canvas.pack(side="left", fill="both", expand=True)
        pesq_scroll.pack(side="right", fill="y")
        inner = ttk.Frame(pesq_canvas, padding=4)
        pesq_window = pesq_canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: pesq_canvas.configure(scrollregion=pesq_canvas.bbox("all")))
        pesq_canvas.bind("<Configure>", lambda e: pesq_canvas.itemconfigure(pesq_window, width=e.width))

        def _on_pesq_mousewheel(event: tk.Event) -> None:
            pesq_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        # Canvases aninhados (modelos de petição, detalhes) tomam o wheel pra si
        # enquanto o mouse está sobre eles; ao saírem, devolvem para este canvas
        # externo em vez de simplesmente desligar a rolagem (bind_all é global e
        # só há um handler ativo por vez).
        self._pesq_mousewheel = _on_pesq_mousewheel
        pesq_canvas.bind("<Enter>", lambda e: pesq_canvas.bind_all("<MouseWheel>", _on_pesq_mousewheel))
        pesq_canvas.bind("<Leave>", lambda e: pesq_canvas.unbind_all("<MouseWheel>"))
        parent = inner

        # ── Seção: Pasta base ──────────────────────────────────────────
        section_base = ttk.Labelframe(
            parent, text="  Base de dados  ", bootstyle="default",
            padding=(12, 8),
        )
        section_base.pack(fill="x", pady=(0, 6))
        self._sec_base = section_base

        f0 = ttk.Frame(section_base)
        f0.pack(fill="x")
        ttk.Label(f0, text="Pasta base:", font=_FONT_BASE).pack(side="left")
        ttk.Entry(
            f0, textvariable=self.var_pesquisa_base, font=_FONT_BASE,
        ).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(
            f0, text="Escolher...", command=self._escolher_pesquisa_base,
            bootstyle="outline", width=12,
        ).pack(side="left")
        ttk.Button(
            f0, text="Recarregar lista", command=self._recarregar_pesquisa,
            bootstyle="outline-info", width=16,
        ).pack(side="left", padx=(8, 0))

        # ── Seção: Buscar processo (barra única) ───────────────────────
        section_busca = ttk.Labelframe(
            parent, text="  Buscar processo  ", bootstyle="default",
            padding=(12, 8),
        )
        section_busca.pack(fill="x", pady=(0, 6))
        self._sec_busca = section_busca

        # Barra única: filtra a lista ao vivo enquanto digita; no Enter ou no
        # botão, busca o processo — só vai à web se ainda não estiver carregado.
        # Grid em linhas para os controles não transbordarem em larguras
        # menores: só o Entry de busca absorve a folga; o resto mantém
        # largura natural e o botão "Buscar" fica sempre visível (linha 2).
        f_busca = ttk.Frame(section_busca)
        f_busca.pack(fill="x")
        f_busca.columnconfigure(1, weight=1)
        ttk.Label(f_busca, text="Buscar:", font=_FONT_BASE).grid(
            row=0, column=0, sticky="e", padx=(0, 6), pady=2,
        )
        ent_busca = ttk.Entry(
            f_busca, textvariable=self.var_filtro, font=_FONT_BASE,
        )
        ent_busca.grid(row=0, column=1, sticky="ew", padx=(0, 6), pady=2)
        # Filtra a lista a cada tecla; Enter dispara a busca (cache-first).
        self.var_filtro.trace_add("write", lambda *_: self._agendar_filtrar_pesquisa())
        ent_busca.bind("<Return>", lambda _e: self._buscar_online())
        ttk.Label(f_busca, text="TJ/Sistema:", font=_FONT_BASE).grid(
            row=0, column=2, sticky="e", padx=(8, 6), pady=2,
        )
        # `rotulos_tj_selecionaveis()` e não `list(TJ_SISTEMAS)`: as rotas de
        # config.ROTAS_INOPERANTES logam mas nunca acham processo, e oferecê-las
        # aqui custava login + 2FA para terminar em "não encontrado".
        ttk.Combobox(
            f_busca, textvariable=self.var_busca_tj,
            values=rotulos_tj_selecionaveis(),
            state="readonly", width=18, height=20, font=_FONT_BASE,
        ).grid(row=0, column=3, sticky="w", pady=2)
        # ── Linha 1: "Forçar busca online" (sempre visível) + botão Buscar ──
        chk_forcar = ttk.Checkbutton(
            f_busca, text="Forçar busca online", variable=self.var_busca_forcar,
            bootstyle="warning-round-toggle",
        )
        chk_forcar.grid(row=1, column=0, columnspan=2, sticky="w", padx=(0, 6), pady=(4, 0))
        self._btn_buscar = ttk.Button(
            f_busca, text="◉  Buscar online", command=self._buscar_online,
            bootstyle="info", width=16,
        )
        self._btn_buscar.grid(row=1, column=3, sticky="e", pady=(4, 0))
        # ── Linha 2: opções avançadas (dev) ─────────────────────────────
        chk_docs = ttk.Checkbutton(
            f_busca, text="Baixar documentos", variable=self.var_busca_docs,
            bootstyle="info",
        )
        chk_docs.grid(row=2, column=0, columnspan=2, sticky="w", padx=(0, 6), pady=(4, 0))
        chk_headless = ttk.Checkbutton(
            f_busca, text="Navegador invisível", variable=self.var_busca_headless,
            bootstyle="info",
        )
        chk_headless.grid(row=2, column=2, sticky="w", padx=(8, 6), pady=(4, 0))
        # ── Linha 3: modo de debug (dev) ────────────────────────────────
        lbl_debug = ttk.Label(f_busca, text="Modo debug:", font=_FONT_BASE)
        lbl_debug.grid(row=3, column=0, sticky="e", padx=(0, 6), pady=(4, 0))
        cbo_debug = ttk.Combobox(
            f_busca, textvariable=self.var_busca_debug,
            values=list(_DEBUG_MODOS), state="readonly", font=_FONT_BASE,
        )
        cbo_debug.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(0, 6), pady=(4, 0))
        self._btn_fechar_debug = ttk.Button(
            f_busca, text="Fechar navegador debug", command=self._fechar_debug,
            bootstyle="warning-outline", width=22, state="disabled",
        )
        self._btn_fechar_debug.grid(row=3, column=3, sticky="e", pady=(4, 0))
        # Opções avançadas — só visíveis no modo dev.
        self._dev_widgets_busca = [
            chk_forcar, chk_docs, chk_headless, lbl_debug, cbo_debug, self._btn_fechar_debug,
        ]

        # ── Seção: Monitoramento (planilhas → mapeamento de sistema) ────
        section_mon = ttk.Labelframe(
            parent, text="  Monitoramento (planilhas)  ", bootstyle="default",
            padding=(12, 8),
        )
        section_mon.pack(fill="x", pady=(0, 6))
        f_mon = ttk.Frame(section_mon)
        f_mon.pack(fill="x")
        ttk.Label(
            f_mon,
            text="Lê as planilhas de credores, pula os processos já mapeados e "
                 "busca os novos nos tribunais. Cada processo é tentado uma vez "
                 "por dia — clicar de novo hoje não repete as consultas.",
            font=_FONT_BASE, wraplength=360, justify="left",
        ).pack(side="left", fill="x", expand=True)
        self._btn_monitor_parar = ttk.Button(
            f_mon, text="Parar", command=self._parar_monitoramento,
            bootstyle="danger-outline", width=8, state="disabled",
        )
        self._btn_monitor_parar.pack(side="right")
        self._btn_monitor = ttk.Button(
            f_mon, text="↻  Sincronizar planilhas & mapear",
            command=self._iniciar_monitoramento, bootstyle="success", width=32,
        )
        self._btn_monitor.pack(side="right", padx=(0, 8))

        # ── Modelos de petição (full-width, acima do painel de processos) ─
        # Altura travada com scroll próprio: em janelas estreitas o número de
        # categorias força colunas finas, o texto quebra em várias linhas e a
        # seção crescia sem limite — sobrando 0px para a árvore de processos
        # abaixo (ela ficava com altura zero/negativa e "desaparecia").
        f_templates = ttk.Labelframe(
            parent, text="  Modelos de petição  ",
            bootstyle="default", padding=(12, 8),
        )
        f_templates.pack(fill="x", expand=False, pady=(0, 4))
        templates_canvas = tk.Canvas(
            f_templates, highlightthickness=0, bg="#f0f2f5", height=210,
        )
        templates_scroll = ttk.Scrollbar(
            f_templates, orient="vertical", command=templates_canvas.yview,
            bootstyle="round",
        )
        templates_canvas.configure(yscrollcommand=templates_scroll.set)
        templates_canvas.pack(side="left", fill="both", expand=True)
        templates_scroll.pack(side="right", fill="y")
        self._templates_inner = ttk.Frame(templates_canvas, padding=4)
        templates_window = templates_canvas.create_window(
            (0, 0), window=self._templates_inner, anchor="nw",
        )

        def _on_templates_inner_configure(event: tk.Event) -> None:
            templates_canvas.configure(scrollregion=templates_canvas.bbox("all"))
            self._requebrar_templates(event)

        # Requebra o texto dos checkbuttons de petição quando a área muda de largura.
        self._templates_inner.bind("<Configure>", _on_templates_inner_configure)
        templates_canvas.bind(
            "<Configure>",
            lambda e: templates_canvas.itemconfigure(templates_window, width=e.width),
        )
        templates_canvas.bind("<Enter>", lambda e: templates_canvas.bind_all(
            "<MouseWheel>", lambda ev: templates_canvas.yview_scroll(int(-ev.delta / 120), "units")))
        # Ao sair, devolve o wheel para o canvas externo da aba (não desliga).
        templates_canvas.bind("<Leave>", lambda e: templates_canvas.bind_all(
            "<MouseWheel>", self._pesq_mousewheel))

        f_btn_t = ttk.Frame(parent)
        f_btn_t.pack(fill="x", pady=(0, 6))
        ttk.Button(
            f_btn_t, text="Marcar todos", command=self._marcar_todos_templates,
            bootstyle="outline", width=14,
        ).pack(side="left")
        ttk.Button(
            f_btn_t, text="Desmarcar todos", command=self._desmarcar_todos_templates,
            bootstyle="outline-secondary", width=16,
        ).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(
            f_btn_t, text="Gerar documento único",
            variable=self.var_documento_unico,
            bootstyle="info",
        ).pack(side="left", padx=(16, 0))

        # ── Área principal: Treeview + Detalhes ────────────────────────
        f_main = ttk.Panedwindow(parent, orient="horizontal")
        f_main.pack(fill="both", expand=True, pady=(0, 6))

        # ── Painel esquerdo: Treeview ──────────────────────────────────
        f_lista = ttk.Frame(f_main)
        # Contagem/aviso de paginação. Empacotado ANTES da tree e com
        # side="bottom" para reservar a faixa de baixo; a tree e a barra de
        # rolagem dividem o resto. Sem isto o usuário não teria como saber que a
        # lista está recortada (ver _preencher_tree).
        ttk.Label(
            f_lista, textvariable=self.var_contagem_lista, anchor="w",
            font=("Segoe UI", 9), foreground="#666",
        ).pack(side="bottom", fill="x", pady=(2, 0))
        colunas = ("numero", "classe", "autor", "reu", "valor", "ultima_mov", "prazos")
        self.tree = ttk.Treeview(
            f_lista, columns=colunas, show="headings", height=12,
            bootstyle="info",
        )
        for c in colunas:
            cab = {"numero": "Nº processo", "classe": "Classe", "autor": "Autor",
                   "reu": "Réu(s)", "valor": "Valor", "ultima_mov": "Última mov.",
                   "prazos": "Prazos"}[c]
            self.tree.heading(c, text=cab, anchor="w")
            larg = {"numero": 180, "classe": 130, "autor": 120, "reu": 120,
                    "valor": 80, "ultima_mov": 140, "prazos": 70}[c]
            self.tree.column(c, width=larg, minwidth=60, anchor="w")
        self.tree.tag_configure("oddrow", background="#f8f9fa")
        self.tree.tag_configure("evenrow", background="#ffffff")
        self.tree.bind("<<TreeviewSelect>>", self._on_select_processo)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll_tree = ttk.Scrollbar(
            f_lista, orient="vertical", command=self.tree.yview,
            bootstyle="info-round",
        )
        scroll_tree.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll_tree.set)
        # A tree tem rolagem própria (barra + wheel nativo do Treeview); sem
        # isto o wheel sobre ela também rolaria o canvas externo da aba,
        # dando um scroll duplicado/​errático.
        self.tree.bind("<Enter>", lambda e: pesq_canvas.unbind_all("<MouseWheel>"))
        self.tree.bind("<Leave>", lambda e: pesq_canvas.bind_all("<MouseWheel>", self._pesq_mousewheel))
        f_main.add(f_lista, weight=1)

        # ── Painel direito: Detalhes (scrollável) ─────────────────────
        f_det_container = ttk.Frame(f_main)
        det_canvas = tk.Canvas(f_det_container, highlightthickness=0, bg="#f0f2f5")
        det_scroll = ttk.Scrollbar(
            f_det_container, orient="vertical", command=det_canvas.yview,
            bootstyle="round",
        )
        det_canvas.configure(yscrollcommand=det_scroll.set)
        det_canvas.pack(side="left", fill="both", expand=True)
        det_scroll.pack(side="right", fill="y")

        f_detalhes = ttk.Frame(det_canvas, padding=4)
        det_window = det_canvas.create_window((0, 0), window=f_detalhes, anchor="nw")
        f_detalhes.bind(
            "<Configure>",
            lambda e: det_canvas.configure(scrollregion=det_canvas.bbox("all")),
        )
        det_canvas.bind(
            "<Configure>",
            lambda e: det_canvas.itemconfigure(det_window, width=e.width),
        )

        def _on_det_mousewheel(event: tk.Event) -> None:
            det_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        det_canvas.bind("<Enter>", lambda e: det_canvas.bind_all("<MouseWheel>", _on_det_mousewheel))
        # Ao sair, devolve o wheel para o canvas externo da aba (não desliga).
        det_canvas.bind("<Leave>", lambda e: det_canvas.bind_all("<MouseWheel>", self._pesq_mousewheel))

        # ── Dados do processo ──────────────────────────────────────────
        self._detalhes_frame = ttk.Labelframe(
            f_detalhes, text="  Dados do processo / petição  ",
            bootstyle="default", padding=(12, 8),
        )
        self._detalhes_frame.pack(fill="both", expand=False, pady=(0, 6))
        self._detalhes_frame.columnconfigure(1, weight=1)
        campos = [
            ("numero", "Nº do processo:"),
            ("tribunal", "Tribunal:"),
            ("classe", "Classe:"),
            ("orgao_julgador", "Órgão Julgador:"),
            ("juiz", "Juiz(a):"),
            ("valor_causa", "Valor da causa:"),
            ("autor", "Autor:"),
            ("reu", "Réu(s):"),
            ("ultima_movimentacao", "Última movimentação:"),
        ]
        # Valores em Entry read-only (em vez de Label): o texto fica selecionável
        # e copiável (Ctrl+C / menu de contexto "Copiar"), mas não editável.
        # _det_ordem/_det_rotulos preservam a ordem e os rótulos para "Copiar dados".
        self._det_vars: dict[str, tk.StringVar] = {}
        self._det_rotulos: dict[str, str] = {atr: rot for atr, rot in campos}
        self._det_ordem: list[str] = [atr for atr, _ in campos]
        # Label+entry de cada linha, para poder ocultar (ex.: valor da causa vazio).
        self._det_widgets: dict[str, tuple[ttk.Label, ttk.Entry]] = {}
        self._det_menu = self._criar_menu_copiar()
        for i, (atr, rotulo) in enumerate(campos):
            lbl = ttk.Label(
                self._detalhes_frame, text=rotulo, anchor="e", width=18,
                font=_FONT_BASE,
            )
            lbl.grid(row=i, column=0, sticky="e", padx=(4, 6), pady=2)
            var = tk.StringVar(value="")
            self._det_vars[atr] = var
            # Editável: o usuário pode corrigir/preencher o que a consulta trouxe
            # (ou não trouxe). As edições fluem para a geração (ver _on_detalhe_edit).
            entry = ttk.Entry(
                self._detalhes_frame, textvariable=var, font=_FONT_BASE,
                bootstyle="info",
            )
            entry.grid(row=i, column=1, sticky="ew", padx=(0, 4), pady=2)
            entry.bind("<Button-3>", self._abrir_menu_copiar)
            var.trace_add("write", lambda *_a, a=atr: self._on_detalhe_edit(a))
            self._det_widgets[atr] = (lbl, entry)
        # Botões de ação do processo selecionado. "Atualizar processo" é a saída
        # para quando o dado na tela está errado/faltando: relê o processo do
        # zero, sem confiar em cache nenhum (ver _atualizar_processo_selecionado).
        f_btn_det = ttk.Frame(self._detalhes_frame)
        f_btn_det.grid(row=len(campos), column=1, sticky="e", padx=(0, 4), pady=(6, 2))
        self._btn_atualizar_processo = ttk.Button(
            f_btn_det, text="↻  Atualizar processo",
            command=self._atualizar_processo_selecionado,
            bootstyle="outline-warning", width=20,
        )
        self._btn_atualizar_processo.pack(side="left", padx=(0, 6))
        ttk.Button(
            f_btn_det, text="▤  Copiar dados", command=self._copiar_todos_dados,
            bootstyle="outline-info", width=16,
        ).pack(side="left")

        # ── Campos editáveis ───────────────────────────────────────────
        f_edicao = ttk.Labelframe(
            f_detalhes, text="  Campos editáveis  ",
            bootstyle="default", padding=(12, 8),
        )
        f_edicao.pack(fill="x", pady=(0, 6))
        campos_edit = [
            ("secretaria", "Secretaria:"),
            ("municipio", "Município:"),
            ("estado", "Estado:"),
            ("oab_uf", "OAB UF:"),
            ("oab_numero", "OAB Nº:"),
            ("endereco_reu", "Endereço do réu:"),
            ("valor_causa", "Valor da causa:"),
            ("comarca", "Comarca:"),
            ("vara", "Vara:"),
            ("tribunal", "Tribunal:"),
            ("endereco_foro", "Endereço do foro:"),
            ("juiz", "Juiz:"),
            ("assunto", "Assunto:"),
        ]
        # Layout em 2 colunas (4 colunas de grid: rótulo, entry, rótulo, entry)
        # para a seção ocupar ~metade da altura. As colunas de entry (1 e 3)
        # dividem a largura. A ordem é guardada em _ordem_edit; a posição de
        # cada índice visível é calculada por _pos_edit (reusado na
        # recompactação de _atualizar_destaques_campos / _atualizar_campo_imoveis).
        f_edicao.columnconfigure(1, weight=1)
        f_edicao.columnconfigure(3, weight=1)
        self._ordem_edit = [atr for atr, _ in campos_edit]
        self._n_linhas_visiveis = len(self._ordem_edit)
        for i, (atr, rotulo) in enumerate(campos_edit):
            var = getattr(self, f"var_{atr}")
            row, col_rot, col_ent = self._pos_edit(i)
            lbl = ttk.Label(f_edicao, text=rotulo, anchor="e", width=18, font=_FONT_BASE)
            lbl.grid(row=row, column=col_rot, sticky="e", padx=(4, 6), pady=2)
            self._labels_edit[atr] = lbl
            self._rotulos_edit[atr] = rotulo
            entry = ttk.Entry(f_edicao, textvariable=var, width=22, font=_FONT_BASE)
            entry.grid(row=row, column=col_ent, sticky="ew", padx=(0, 12), pady=2)
            self._entries_edit[atr] = entry
            # Ao digitar, remove o destaque de obrigatório-vazio (ver geração).
            var.trace_add("write", lambda *_a, a=atr: self._limpar_destaque_campo(a))
            # Município: ao sair do campo, memoriza o valor para o foro do CNJ.
            if atr == "municipio":
                entry.bind("<FocusOut>", self._memorizar_municipio)
            if atr in ("valor_causa", "comarca", "vara", "tribunal", "endereco_foro",
                       "juiz", "assunto", "municipio", "estado", "oab_uf", "oab_numero"):
                self._entries_auto[str(var)] = entry
                entry.configure(foreground="#868e96")

        # Linha extra dentro de f_edicao para o checkbox "Múltiplos imóveis"
        self._frame_imoveis = ttk.Frame(f_edicao)
        self._chk_imoveis = ttk.Checkbutton(
            self._frame_imoveis,
            text="Múltiplos imóveis",
            variable=self.var_multiplos_imoveis,
            bootstyle="info",
        )
        self._chk_imoveis.pack(side="left")

        # ── Campos dinâmicos (placeholders novos) ──────────────────────
        self._frame_dinamicos = ttk.Labelframe(
            f_detalhes, text="  Campos adicionais (placeholders novos)  ",
            bootstyle="warning", padding=(12, 8),
        )

        # ── Geração de petições ────────────────────────────────────────
        section_gen = ttk.Labelframe(
            f_detalhes, text="  Gerar petições  ",
            bootstyle="default", padding=(12, 8),
        )
        self._section_gen = section_gen
        section_gen.pack(fill="x")
        f_gerar = ttk.Frame(section_gen)
        f_gerar.pack(fill="x")
        f_gerar.columnconfigure(1, weight=1)
        ttk.Label(f_gerar, text="Destino:", font=_FONT_BASE).grid(
            row=0, column=0, sticky="e", padx=(0, 6), pady=4,
        )
        ttk.Entry(
            f_gerar, textvariable=self.var_pesquisa_destino, font=_FONT_BASE,
        ).grid(row=0, column=1, sticky="ew", padx=(0, 6), pady=4)
        ttk.Button(
            f_gerar, text="Escolher...", command=self._escolher_pesquisa_destino,
            bootstyle="outline", width=12,
        ).grid(row=0, column=2, pady=4)
        # Indicadores de formato de saída
        f_fmt = ttk.Frame(f_gerar)
        f_fmt.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Label(f_fmt, text="◧", font=("Segoe UI", 14)).pack(side="left", padx=(0, 4))
        ttk.Label(f_fmt, text="Word (.docx)", font=_FONT_BASE, bootstyle="secondary").pack(side="left", padx=(0, 20))
        ttk.Label(f_fmt, text="▢", font=("Segoe UI", 14)).pack(side="left", padx=(0, 4))
        _cb_pdf = ttk.Checkbutton(
            f_fmt, text="PDF", variable=self.var_gerar_pdf, bootstyle="info",
            state="disabled",
        )
        _cb_pdf.pack(side="left")
        self._cb_pdf = _cb_pdf
        self._lbl_pdf_status = ttk.Label(
            f_fmt,
            text="(verificando Microsoft Word...)",
            font=_FONT_BASE,
            bootstyle="secondary",
        )
        self._lbl_pdf_status.pack(side="left", padx=(6, 0))
        self.after(800, self._detectar_word_pdf)
        ttk.Button(
            f_gerar, text="▢  Gerar petições selecionadas",
            command=self._gerar_peticoes,
            bootstyle="success", width=28,
        ).grid(row=2, column=0, columnspan=3, pady=(8, 0))
        # Frame para exibir os arquivos gerados (oculto até gerar)
        self._frame_gerados = ttk.Frame(section_gen)

        f_main.add(f_det_container, weight=2)

        # Posição inicial do divisor: detalhes ~58% (precisam de espaço para
        # as 2 colunas + modelos que crescem). Aplicada uma única vez no
        # primeiro <Map>; depois disso o usuário pode arrastar livremente.
        self._sash_inicializado = False

        def _init_sash(_event: tk.Event) -> None:
            if self._sash_inicializado:
                return
            largura = f_main.winfo_width()
            if largura <= 1:
                return  # ainda não renderizado; aguarda próximo evento
            f_main.sashpos(0, int(largura * 0.42))
            self._sash_inicializado = True

        f_main.bind("<Map>", _init_sash, add="+")
        f_main.bind("<Configure>", _init_sash, add="+")

        # ── Barra de progresso (animada) + botão de detalhes ───────────
        f_status_pesq = ttk.Frame(parent)
        f_status_pesq.pack(fill="x", pady=(0, 6))
        self.progress_pesquisa = ttk.Progressbar(
            f_status_pesq, orient="horizontal",
            mode="indeterminate", bootstyle="info-striped",
        )
        self.progress_pesquisa.pack(side="left", fill="x", expand=True)
        self.btn_detalhes_pesq = ttk.Button(
            f_status_pesq, text="Ver detalhes ▾", command=self._toggle_log_pesq,
            bootstyle="outline-secondary", width=16,
        )
        self.btn_detalhes_pesq.pack(side="left", padx=(8, 0))

        # ── Log da pesquisa (oculto por padrão; alternado por "Ver detalhes") ─
        self._section_log_pesq = ttk.Labelframe(
            parent, text="  Log  ", bootstyle="default",
            padding=(12, 8),
        )
        # Não empacotado aqui: fica oculto até o usuário clicar em "Ver detalhes".
        self.txt_pesquisa = tk.Text(
            self._section_log_pesq, height=4, wrap="word", state="disabled",
            font=_FONT_MONO, bg="#fafbfc", relief="flat",
            borderwidth=1, highlightthickness=1,
            highlightcolor="#d0d5dd", highlightbackground="#d0d5dd",
        )
        self.txt_pesquisa.pack(fill="x")

    # -- aba Autojur (dev) --------------------------------------------------
    def _build_aba_autojur(self, parent: ttk.Frame) -> None:
        """Aba Autojur (visível só no modo dev): abre o navegador logado no
        Autojur e o MANTÉM aberto, para montar as próximas automações em cima
        da sessão viva. O login (2FA por e-mail automático) roda em thread
        worker; o scraper fica retido em self._autojur_scraper até o usuário
        clicar em "Fechar navegador"."""
        sec = ttk.Labelframe(
            parent, text="  Sessão  ", bootstyle="default", padding=(12, 8),
        )
        sec.pack(fill="x", pady=(0, 8))
        ttk.Label(
            sec,
            text=("Abre o Chrome, faz o login no Autojur (o código de verificação "
                  "é lido do e-mail automaticamente) e mantém o navegador aberto "
                  "para uso/inspeção."),
            font=_FONT_BASE, wraplength=900, justify="left",
        ).pack(anchor="w", pady=(0, 8))

        f_btn = ttk.Frame(sec)
        f_btn.pack(fill="x")
        self._btn_autojur_abrir = ttk.Button(
            f_btn, text="◈  Abrir Autojur (login)", command=self._autojur_abrir,
            bootstyle="success", width=24,
        )
        self._btn_autojur_abrir.pack(side="left")
        self._btn_autojur_fechar = ttk.Button(
            f_btn, text="Fechar navegador", command=self._autojur_fechar,
            bootstyle="warning-outline", width=18, state="disabled",
        )
        self._btn_autojur_fechar.pack(side="left", padx=(8, 0))
        self.progress_autojur = ttk.Progressbar(
            f_btn, orient="horizontal", mode="indeterminate",
            bootstyle="info-striped",
        )
        self.progress_autojur.pack(side="left", fill="x", expand=True, padx=(12, 0))

        # ── Criar tarefa de Protocolo ─────────────────────────────────────
        sec_t = ttk.Labelframe(
            parent, text="  Criar tarefa de Protocolo  ",
            bootstyle="default", padding=(12, 8),
        )
        sec_t.pack(fill="x", pady=(0, 8))
        grid = ttk.Frame(sec_t)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="Nº processo:", font=_FONT_BASE).grid(
            row=0, column=0, sticky="e", padx=(0, 6), pady=3)
        ttk.Entry(grid, textvariable=self.var_autojur_processo, font=_FONT_BASE).grid(
            row=0, column=1, columnspan=2, sticky="ew", pady=3)

        ttk.Label(grid, text="Local:", font=_FONT_BASE).grid(
            row=1, column=0, sticky="e", padx=(0, 6), pady=3)
        ttk.Entry(grid, textvariable=self.var_autojur_local, font=_FONT_BASE).grid(
            row=1, column=1, sticky="ew", pady=3)
        ttk.Label(grid, text="Lote:", font=_FONT_BASE).grid(
            row=1, column=2, sticky="e", padx=(8, 6), pady=3)
        ttk.Combobox(
            grid, textvariable=self.var_autojur_lote,
            values=["iniciais", "urgente", "dia seguinte"],
            state="readonly", width=14, font=_FONT_BASE,
        ).grid(row=1, column=3, sticky="w", pady=3)

        ttk.Label(grid, text="PDF (petição):", font=_FONT_BASE).grid(
            row=2, column=0, sticky="e", padx=(0, 6), pady=3)
        ttk.Entry(grid, textvariable=self.var_autojur_pdf, font=_FONT_BASE).grid(
            row=2, column=1, columnspan=2, sticky="ew", pady=3)
        ttk.Button(
            grid, text="Escolher...", command=self._autojur_escolher_pdf,
            bootstyle="outline-secondary", width=12,
        ).grid(row=2, column=3, sticky="w", padx=(8, 0), pady=3)

        ttk.Label(grid, text="Conteúdo:", font=_FONT_BASE).grid(
            row=3, column=0, sticky="ne", padx=(0, 6), pady=3)
        self.txt_autojur_conteudo = tk.Text(
            grid, height=4, wrap="word", font=_FONT_BASE,
            relief="flat", borderwidth=1, highlightthickness=1,
            highlightcolor="#d0d5dd", highlightbackground="#d0d5dd",
        )
        self.txt_autojur_conteudo.grid(
            row=3, column=1, columnspan=3, sticky="ew", pady=3)

        f_acao = ttk.Frame(sec_t)
        f_acao.pack(fill="x", pady=(6, 0))
        self._btn_autojur_tarefa = ttk.Button(
            f_acao, text="◧  Criar tarefa de Protocolo",
            command=self._autojur_criar_tarefa, bootstyle="primary", width=28,
            state="disabled",  # habilita após login
        )
        self._btn_autojur_tarefa.pack(side="left")
        ttk.Checkbutton(
            f_acao,
            text="Salvar automaticamente (senão só preenche p/ conferência)",
            variable=self.var_autojur_salvar, bootstyle="warning-round-toggle",
        ).pack(side="left", padx=(12, 0))

        # Log sempre visível: a aba é de desenvolvimento/exploração.
        sec_log = ttk.Labelframe(
            parent, text="  Log  ", bootstyle="default", padding=(12, 8),
        )
        sec_log.pack(fill="both", expand=True)
        self.txt_autojur = tk.Text(
            sec_log, height=14, wrap="word", state="disabled",
            font=_FONT_MONO, bg="#fafbfc", relief="flat",
            borderwidth=1, highlightthickness=1,
            highlightcolor="#d0d5dd", highlightbackground="#d0d5dd",
        )
        self.txt_autojur.pack(fill="both", expand=True)

    def _autojur_abrir(self) -> None:
        """Abre o navegador e loga no Autojur em thread worker. O navegador
        fica ABERTO (scraper retido), como no modo debug da busca online."""
        if self._autojur_worker is not None and self._autojur_worker.is_alive():
            return
        # Evita abrir um 2º Chrome no MESMO perfil persistente enquanto o
        # pré-login ainda loga (o perfil só aceita um processo por vez).
        pre = self._autojur_prelogin_worker
        if pre is not None and pre.is_alive():
            self._autojur_log_queue.put(
                "Autojur: pré-login em andamento — aguarde alguns segundos.\n"
            )
            return
        if self._autojur_scraper is not None:
            if self._sessao_autojur_viva(self._autojur_scraper):
                self._autojur_log_queue.put("O navegador do Autojur já está aberto.\n")
                return
            # Sessão retida morreu (navegador fechado por fora): descarta e reabre.
            with contextlib.suppress(Exception):
                self._autojur_scraper.fechar()
            self._autojur_scraper = None
            self._autojur_scraper_headless = None
        self._btn_autojur_abrir.configure(state="disabled")
        self.progress_autojur.start(_PROGRESS_MS)
        self.var_status.set("Autojur: abrindo navegador e logando...")

        def run() -> None:
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except Exception:
                pass
            writer = _LogWriter(self._autojur_log_queue.put)
            try:
                # Sob pythonw, sys.stdout é None; redireciona os print() do
                # scraper para o log da aba (e evita que print() quebre).
                with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                    # O .env é carregado sob demanda (como na busca online) —
                    # é dele que saem email_autojur/senha_autojur e o IMAP do 2FA.
                    from dotenv import load_dotenv
                    from .config import CONFIG_ENV_PATH, AUTOJUR_PROFILE_DIR
                    from .scraper import _preparar_env_local
                    _preparar_env_local()
                    load_dotenv(CONFIG_ENV_PATH)
                    # Import tardio: paga o custo do Selenium só ao usar a aba.
                    from .integracoes.tjsp_scraper.scrapers.autojur import AutojurScraper
                    # Aba Autojur é dev-only (ferramenta de inspeção): sempre
                    # visível. O headless de produção vale para o envio de
                    # petições (_garantir_autojur_logado), fora do modo dev.
                    # Mesmo perfil persistente do envio: pula o 2FA nas próximas.
                    AUTOJUR_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
                    scraper = AutojurScraper(
                        headless=False, user_data_dir=str(AUTOJUR_PROFILE_DIR),
                    )
                    try:
                        scraper.login()
                    except Exception:
                        scraper.fechar()
                        raise
                self._autojur_result_queue.put(("ok", scraper))
            except Exception as exc:
                self._autojur_result_queue.put(("erro", _descrever_erro(exc)))

        self._autojur_worker = threading.Thread(target=run, daemon=True)
        self._autojur_worker.start()

    def _autojur_fechar(self) -> None:
        """Fecha o navegador do Autojur (em thread, pois quit() pode demorar)."""
        scraper = self._autojur_scraper
        self._autojur_scraper = None
        self._autojur_scraper_headless = None
        self._btn_autojur_fechar.configure(state="disabled")
        self._btn_autojur_abrir.configure(state="normal")
        self._btn_autojur_tarefa.configure(state="disabled")
        if scraper is None:
            return
        self._autojur_log_queue.put("Fechando navegador do Autojur...\n")
        self._fechar_em_thread(scraper)

    def _drain_autojur_log_queue(self) -> None:
        mensagens: list[str] = []
        try:
            while True:
                mensagens.append(self._autojur_log_queue.get_nowait())
        except queue.Empty:
            pass
        self._append_log(self.txt_autojur, mensagens)

    def _drain_autojur_result(self) -> None:
        """Drena o resultado do login do Autojur na main thread."""
        try:
            while True:
                tipo, payload = self._autojur_result_queue.get_nowait()
                self.progress_autojur.stop()
                if tipo == "ok":
                    self._autojur_scraper = payload
                    # Aba Autojur abre sempre visível (headless=False): registra
                    # p/ o envio saber se precisa recriar por visibilidade.
                    self._autojur_scraper_headless = False
                    self._marcar_uso_autojur()
                    self._btn_autojur_fechar.configure(state="normal")
                    self._btn_autojur_tarefa.configure(state="normal")
                    self._autojur_log_queue.put(
                        "Sessão logada — navegador aberto para os próximos passos.\n"
                    )
                    self.var_status.set("Autojur: sessão logada (navegador aberto).")
                else:
                    self._btn_autojur_abrir.configure(state="normal")
                    self.var_status.set("Autojur: falha no login.")
                    self._autojur_log_queue.put(f"! Erro no login do Autojur: {payload}\n")
                    self._notificar("Autojur", str(payload), nivel="error")
        except queue.Empty:
            pass

    def _autojur_escolher_pdf(self) -> None:
        caminho = filedialog.askopenfilename(
            title="Escolha o PDF da petição",
            filetypes=[("PDF", "*.pdf"), ("Todos", "*.*")],
        )
        if caminho:
            self.var_autojur_pdf.set(caminho)

    def _autojur_criar_tarefa(self) -> None:
        """Dispara a criação da tarefa de Protocolo no scraper logado (thread)."""
        if self._autojur_scraper is None or not self._sessao_autojur_viva(self._autojur_scraper):
            if self._autojur_scraper is not None:
                with contextlib.suppress(Exception):
                    self._autojur_scraper.fechar()
                self._autojur_scraper = None
                self._autojur_scraper_headless = None
                self._btn_autojur_abrir.configure(state="normal")
                self._btn_autojur_tarefa.configure(state="disabled")
            self._autojur_log_queue.put(
                "! Sessão do Autojur indisponível — clique em 'Abrir navegador'.\n"
            )
            return
        if self._autojur_tarefa_worker is not None and self._autojur_tarefa_worker.is_alive():
            return

        numero = self.var_autojur_processo.get().strip()
        conteudo = self.txt_autojur_conteudo.get("1.0", "end").strip()
        local = self.var_autojur_local.get().strip()
        # rótulo da GUI ("dia seguinte") -> chave do scraper ("dia_seguinte")
        lote = self.var_autojur_lote.get().strip().lower().replace(" ", "_")
        pdf = self.var_autojur_pdf.get().strip() or None
        salvar = bool(self.var_autojur_salvar.get())

        if not numero:
            self._autojur_log_queue.put("! Informe o número do processo.\n")
            return
        if not conteudo:
            self._autojur_log_queue.put("! O campo Conteúdo é obrigatório.\n")
            return

        self._btn_autojur_tarefa.configure(state="disabled")
        self.progress_autojur.start(_PROGRESS_MS)
        self.var_status.set("Autojur: criando tarefa de Protocolo...")
        self._marcar_uso_autojur()
        scraper = self._autojur_scraper

        def run() -> None:
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except Exception:
                pass
            writer = _LogWriter(self._autojur_log_queue.put)
            try:
                with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                    scraper.criar_tarefa_protocolo(
                        numero_processo=numero,
                        conteudo=conteudo,
                        local=local,
                        lote=lote,
                        pdf_path=pdf,
                        requer_protocolo=True,
                        salvar=salvar,
                        escolher_resultado=self._autojur_escolher_resultado,
                        log=self._autojur_log_queue.put,
                    )
                self._autojur_tarefa_result_queue.put(("ok", salvar))
            except Exception as exc:
                self._autojur_tarefa_result_queue.put(("erro", _descrever_erro(exc)))

        self._autojur_tarefa_worker = threading.Thread(target=run, daemon=True)
        self._autojur_tarefa_worker.start()

    def _drain_autojur_tarefa_result(self) -> None:
        """Drena o resultado da criação de tarefa na main thread."""
        try:
            while True:
                tipo, payload = self._autojur_tarefa_result_queue.get_nowait()
                self.progress_autojur.stop()
                self._btn_autojur_tarefa.configure(state="normal")
                if tipo == "ok":
                    salvou = bool(payload)
                    msg = ("Tarefa salva no Autojur." if salvou
                           else "Tarefa preenchida (não salva) — confira no navegador.")
                    self.var_status.set(f"Autojur: {msg}")
                    self._notificar("Autojur", msg, nivel="info")
                else:
                    self.var_status.set("Autojur: falha ao criar tarefa.")
                    self._autojur_log_queue.put(f"! Erro ao criar tarefa: {payload}\n")
                    self._notificar("Autojur", str(payload), nivel="error")
        except queue.Empty:
            pass

    def _autojur_escolher_resultado(self, resultados: list) -> int | None:
        """Callback chamado pelo WORKER quando a busca traz >1 resultado.
        Enfileira o pedido e BLOQUEIA até a main thread resolver o modal.
        Retorna o índice escolhido, ou None se o usuário cancelar."""
        ev = threading.Event()
        box: dict = {}
        self._autojur_escolha_queue.put((resultados, ev, box))
        ev.wait(timeout=300)  # 5 min para a pessoa escolher
        return box.get("idx")

    def _drain_autojur_escolha(self) -> None:
        """Mostra o modal de seleção na main thread quando o worker pede."""
        try:
            while True:
                resultados, ev, box = self._autojur_escolha_queue.get_nowait()
                self._modal_escolha_resultado(resultados, ev, box)
        except queue.Empty:
            pass

    def _modal_escolha_resultado(self, resultados: list, ev, box: dict) -> None:
        """Modal: a busca trouxe vários resultados; a pessoa escolhe qual abrir.
        Ao confirmar, grava o índice em box['idx'] e libera o worker (ev.set())."""
        top = tk.Toplevel(self)
        top.title("Selecione a pasta")
        top.transient(self)
        top.grab_set()
        top.geometry("720x360")

        ttk.Label(
            top,
            text=(f"A busca retornou {len(resultados)} resultados. "
                  "Selecione qual pasta abrir:"),
            font=_FONT_BASE, wraplength=680,
        ).pack(anchor="w", padx=12, pady=(12, 6))

        var_sel = tk.IntVar(value=resultados[0]["indice"])
        frm = ttk.Frame(top)
        frm.pack(fill="both", expand=True, padx=12)
        for r in resultados:
            rotulo = f"Pasta {r['pasta']}  ·  {r['cnj']}"
            if r["assunto"]:
                rotulo += f"  ·  {r['assunto']}"
            ttk.Radiobutton(
                frm, text=rotulo, value=r["indice"], variable=var_sel,
                bootstyle="primary",
            ).pack(anchor="w", pady=3)

        def confirmar() -> None:
            box["idx"] = int(var_sel.get())
            ev.set()
            top.destroy()

        def cancelar() -> None:
            box["idx"] = None
            ev.set()
            top.destroy()

        f_b = ttk.Frame(top)
        f_b.pack(fill="x", padx=12, pady=12)
        ttk.Button(f_b, text="Abrir selecionada", command=confirmar,
                   bootstyle="success", width=18).pack(side="right")
        ttk.Button(f_b, text="Cancelar", command=cancelar,
                   bootstyle="secondary-outline", width=12).pack(side="right", padx=(0, 8))
        top.protocol("WM_DELETE_WINDOW", cancelar)

    # -- aba Configurações (editor de senhas) ------------------------------
    def _build_aba_config(self, parent: ttk.Frame) -> None:
        """Aba para editar as senhas (credenciais dos tribunais + segredos de
        2FA). Tudo é gravado em config/credenciais.json e config/.env, então
        persiste entre sessões."""
        # Área rolável: o conteúdo pode passar da altura da janela.
        canvas = tk.Canvas(parent, highlightthickness=0, bg="#f0f2f5")
        scroll = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview, bootstyle="round")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        inner = ttk.Frame(canvas, padding=4)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.bind("<Enter>", lambda e: canvas.bind_all(
            "<MouseWheel>", lambda ev: canvas.yview_scroll(int(-ev.delta / 120), "units")))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        ttk.Label(
            inner,
            text="Edite os logins e senhas usados na busca online. As alterações "
                 "são salvas no computador e mantidas entre sessões.",
            font=_FONT_BASE, wraplength=900, justify="left", bootstyle="secondary",
        ).pack(fill="x", pady=(0, 8))

        # Guia passo a passo. Fica no TOPO e fora do modo dev de propósito: é ele
        # que ensina a ligar o Modo desenvolvedor para revelar a seção 2FA, então
        # esconder o guia junto com a seção deixaria o usuário sem saída.
        f_guia = ttk.Frame(inner)
        f_guia.pack(fill="x", pady=(0, 10))
        ttk.Button(
            f_guia, text="§  Guia: configurar o e-mail dos códigos de acesso",
            command=self._abrir_guia_credenciais, bootstyle="outline-primary",
        ).pack(side="left")

        # ── Seção: Credenciais dos tribunais ───────────────────────────
        sec_cred = ttk.Labelframe(
            inner, text="  Credenciais dos tribunais  ", bootstyle="default", padding=(12, 8),
        )
        sec_cred.pack(fill="x", pady=(0, 8))
        self._frame_cred = ttk.Frame(sec_cred)
        self._frame_cred.pack(fill="x")

        # ── Seção: Acesso ao Autojur ────────────────────────────────────
        sec_autojur = ttk.Labelframe(
            inner, text="  Acesso ao Autojur  ", bootstyle="default", padding=(12, 8),
        )
        sec_autojur.pack(fill="x", pady=(0, 8))
        f_autojur = ttk.Frame(sec_autojur)
        f_autojur.pack(fill="x")
        f_autojur.columnconfigure(1, weight=1)
        self._autojur_rows.clear()
        for i, (rotulo, chave) in enumerate(CHAVES_AUTOJUR.items()):
            ttk.Label(f_autojur, text=rotulo + ":", font=_FONT_BASE, anchor="e", width=32).grid(
                row=i, column=0, sticky="e", padx=(0, 8), pady=3,
            )
            var = tk.StringVar(value="")
            secreto = chave == "senha_autojur"
            entry = ttk.Entry(f_autojur, textvariable=var, font=_FONT_BASE)
            if secreto:
                entry.configure(show="•")
                self._entries_senha.append(entry)
            entry.grid(row=i, column=1, sticky="ew", pady=3)
            self._autojur_rows[chave] = {"var": var, "entry": entry, "secreto": secreto}

        # ── Seção: 2FA ─────────────────────────────────────────────────
        sec_2fa = ttk.Labelframe(
            inner, text="  Autenticação em dois fatores (2FA)  ", bootstyle="default", padding=(12, 8),
        )
        sec_2fa.pack(fill="x", pady=(0, 8))
        self._sec_2fa = sec_2fa  # ocultada fora do modo dev
        f2 = ttk.Frame(sec_2fa)
        f2.pack(fill="x")
        f2.columnconfigure(1, weight=1)
        self._2fa_rows.clear()
        linha = 0
        for rotulo, chave in CHAVES_2FA.items():
            ttk.Label(f2, text=rotulo + ":", font=_FONT_BASE, anchor="e", width=32).grid(
                row=linha, column=0, sticky="e", padx=(0, 8), pady=3,
            )
            var = tk.StringVar(value="")
            # Tudo que não for o e-mail é segredo → mascarado por padrão.
            secreto = chave != "IMAP_USER"
            entry = ttk.Entry(f2, textvariable=var, font=_FONT_BASE)
            if secreto:
                entry.configure(show="•")
                self._entries_senha.append(entry)
            entry.grid(row=linha, column=1, sticky="ew", pady=3)
            self._2fa_rows[chave] = {"var": var, "entry": entry, "secreto": secreto}
            linha += 1
            if chave == "IMAP_PASS":
                # Numa linha própria (não numa 3ª coluna) para não ser cortado
                # em janelas estreitas — quebra naturalmente com wraplength.
                f_links = ttk.Frame(f2)
                f_links.grid(row=linha, column=0, columnspan=2, sticky="w", pady=(0, 6))
                ttk.Button(
                    f_links, text="↗ Ativar verificação em duas etapas",
                    command=lambda: _abrir_no_navegador(
                        "https://myaccount.google.com/signinoptions/twosv"),
                    bootstyle="link",
                ).pack(side="left")
                ttk.Button(
                    f_links, text="↗ Gerar senha de app (Google)",
                    command=lambda: _abrir_no_navegador(
                        "https://myaccount.google.com/apppasswords"),
                    bootstyle="link",
                ).pack(side="left", padx=(8, 0))
                linha += 1

        # ── Ações ──────────────────────────────────────────────────────
        # Botões principais numa linha; os toggles ficam abaixo, em grid que
        # quebra em várias linhas — evita que, em telas estreitas, widgets
        # empacotados com side="right" sejam empurrados para fora da área
        # visível (o sintoma era "opção sumindo" dependendo da resolução).
        f_acoes = ttk.Frame(inner)
        f_acoes.pack(fill="x", pady=(4, 0))
        self._f_acoes_config = f_acoes  # âncora p/ re-empacotar a seção 2FA na ordem
        ttk.Button(
            f_acoes, text="▣  Salvar configurações", command=self._salvar_config,
            bootstyle="success", width=24,
        ).pack(side="left")
        ttk.Button(
            f_acoes, text="Recarregar", command=self._carregar_config_na_ui,
            bootstyle="outline-secondary", width=14,
        ).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(
            f_acoes, text="Mostrar senhas", variable=self.var_mostrar_senhas,
            command=self._toggle_mostrar_senhas, bootstyle="info-round-toggle",
        ).pack(side="left", padx=(16, 0))

        sec_opcoes = ttk.Labelframe(
            inner, text="  Opções  ", bootstyle="default", padding=(12, 8),
        )
        sec_opcoes.pack(fill="x", pady=(8, 0))
        f_opcoes = ttk.Frame(sec_opcoes)
        f_opcoes.pack(fill="x")
        f_opcoes.columnconfigure(0, weight=1)
        f_opcoes.columnconfigure(1, weight=1)
        toggles = [
            ("Modo desenvolvedor", self.var_modo_dev, self._toggle_modo_dev, "warning-round-toggle"),
            ("Salvar tarefas no Autojur", self.var_autojur_salvar_prod, self._toggle_autojur_salvar, "danger-round-toggle"),
            ("Visualizar navegador do Autojur", self.var_autojur_visualizar, self._toggle_autojur_visualizar, "info-round-toggle"),
            ("Pré-logar no Autojur ao abrir (oculto)", self.var_autojur_prelogin, self._toggle_autojur_prelogin, "success-round-toggle"),
        ]
        for idx, (texto, var, cmd, estilo) in enumerate(toggles):
            linha, coluna = divmod(idx, 2)
            ttk.Checkbutton(
                f_opcoes, text=texto, variable=var, command=cmd, bootstyle=estilo,
            ).grid(row=linha, column=coluna, sticky="w", padx=(0, 16), pady=3)

        # ── Seção: Desempenho ──────────────────────────────────────────
        section_perf = ttk.Labelframe(
            parent, text="  Desempenho  ", bootstyle="default", padding=(12, 8),
        )
        section_perf.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(
            section_perf,
            text=f"Modo leve — máquina com pouca memória  "
                 f"(detectado: {self._perfil_maquina.descricao()})",
            variable=self.var_modo_leve, command=self._toggle_modo_leve,
            bootstyle="warning-round-toggle",
        ).pack(anchor="w")
        ttk.Label(
            section_perf,
            text="Quando ligado: " + "; ".join(AJUSTES_MODO_LEVE) + ".",
            font=("Segoe UI", 9), foreground="#666", wraplength=900, justify="left",
        ).pack(anchor="w", pady=(4, 0))
        ttk.Label(
            section_perf,
            text="Ligado automaticamente em máquinas fracas. Mudanças valem na "
                 "próxima abertura do app.",
            font=("Segoe UI", 9, "italic"), foreground="#888",
        ).pack(anchor="w", pady=(2, 0))

        # ── Seção: Manutenção ───────────────────────────────────────────
        # Existe porque atualizar o programa NÃO limpa o que a máquina guardou
        # (ver _resetar_dados_locais): sem este botão, consertar uma instalação
        # com cache errado exige apagar arquivos no %USERPROFILE% na mão.
        section_manut = ttk.Labelframe(
            parent, text="  Manutenção  ", bootstyle="default", padding=(12, 8),
        )
        section_manut.pack(fill="x", pady=(8, 0))
        self._btn_reset_dados = ttk.Button(
            section_manut, text="×  Restaurar dados do programa",
            command=self._restaurar_dados_locais,
            bootstyle="outline-danger", width=32,
        )
        self._btn_reset_dados.pack(anchor="w")
        ttk.Label(
            section_manut,
            text="Apaga o que ESTE computador guardou (processos trazidos por "
                 "busca online, índice da lista e cache dos modelos) e volta a "
                 "mostrar só os processos que vêm dentro do programa. Use quando "
                 "a lista aparecer com dados errados mesmo depois de atualizar. "
                 "Não mexe nas suas senhas nem nos processos da pasta base.",
            font=("Segoe UI", 9), foreground="#666", wraplength=900, justify="left",
        ).pack(anchor="w", pady=(4, 0))

        # O update NÃO troca senha (semear_config nunca sobrescreve, de propósito),
        # e esperar um release novo para cada senha trocada era o gargalo. Este
        # botão vai na PLANILHA de senhas do escritório: trocou lá, chega aqui.
        self._btn_atualizar_senhas = ttk.Button(
            section_manut, text="⇄  Atualizar senhas dos tribunais",
            command=self._atualizar_senhas_dos_tribunais,
            bootstyle="outline-primary", width=32,
        )
        self._btn_atualizar_senhas.pack(anchor="w", pady=(10, 0))
        ttk.Label(
            section_manut,
            text="Confere a planilha de senhas do escritório (no Google) e traz "
                 "para esta máquina os logins que mudaram — use quando o app "
                 "começar a falhar no login de um tribunal. Mostra quais tribunais "
                 "mudam antes de aplicar e guarda o arquivo anterior como "
                 "credenciais.json.bak. Nunca apaga um acesso que só existe aqui, "
                 "e campo em branco na planilha não apaga a senha desta máquina. "
                 "Precisa de internet. Não mexe no 2FA (.env) nem nos processos.",
            font=("Segoe UI", 9), foreground="#666", wraplength=900, justify="left",
        ).pack(anchor="w", pady=(4, 0))

        # ── Seção: Sobre / Atualização ──────────────────────────────────
        section_sobre = ttk.Labelframe(
            parent, text="  Sobre  ", bootstyle="default", padding=(12, 8),
        )
        section_sobre.pack(fill="x", pady=(8, 0))
        f_sobre = ttk.Frame(section_sobre)
        f_sobre.pack(fill="x")
        ttk.Label(
            f_sobre, text=f"Versão instalada: v{__version__}", font=_FONT_BASE,
        ).pack(side="left")
        ttk.Button(
            f_sobre, text="↻  Verificar atualização", command=self._verificar_atualizacao_manual,
            bootstyle="outline-info", width=24,
        ).pack(side="right")

        self._carregar_config_na_ui()

    def _carregar_config_na_ui(self) -> None:
        """(Re)carrega credenciais e segredos de 2FA dos arquivos para a tela."""
        # Credenciais: reconstrói as linhas a partir do arquivo atual.
        for w in self._frame_cred.winfo_children():
            w.destroy()
        self._cred_rows.clear()
        # Mantém só os Entry de Autojur/2FA na lista de senhas; os de credenciais
        # serão readicionados abaixo (evita referências a widgets destruídos).
        self._entries_senha = [
            r["entry"] for r in self._autojur_rows.values() if r["secreto"]
        ] + [
            r["entry"] for r in self._2fa_rows.values() if r["secreto"]
        ]

        creds = listar_credenciais()
        if not creds:
            ttk.Label(
                self._frame_cred,
                text="Nenhuma credencial encontrada em config/credenciais.json.",
                font=_FONT_BASE, bootstyle="warning",
            ).grid(row=0, column=0, sticky="w", pady=4)
        else:
            self._frame_cred.columnconfigure(1, weight=1)
            self._frame_cred.columnconfigure(2, weight=1)
            ttk.Label(self._frame_cred, text="Tribunal/Sistema", font=_FONT_BOLD).grid(
                row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 4))
            ttk.Label(self._frame_cred, text="Login", font=_FONT_BOLD).grid(
                row=0, column=1, sticky="w", padx=(0, 8), pady=(0, 4))
            ttk.Label(self._frame_cred, text="Senha", font=_FONT_BOLD).grid(
                row=0, column=2, sticky="w", pady=(0, 4))
            for i, cred in enumerate(creds, start=1):
                ttk.Label(
                    self._frame_cred, text=cred["tj"], font=_FONT_BASE, anchor="w", width=28,
                ).grid(row=i, column=0, sticky="w", padx=(0, 8), pady=3)
                var_login = tk.StringVar(value=cred["login"])
                ttk.Entry(self._frame_cred, textvariable=var_login, font=_FONT_BASE).grid(
                    row=i, column=1, sticky="ew", padx=(0, 8), pady=3)
                var_senha = tk.StringVar(value=cred["senha"])
                entry_senha = ttk.Entry(
                    self._frame_cred, textvariable=var_senha, font=_FONT_BASE, show="•",
                )
                entry_senha.grid(row=i, column=2, sticky="ew", pady=3)
                self._entries_senha.append(entry_senha)
                self._cred_rows.append(
                    {"tj": cred["tj"], "var_login": var_login, "var_senha": var_senha}
                )

        # Autojur: preenche os campos com os valores do .env.
        valores_autojur = ler_config_autojur()
        for chave, row in self._autojur_rows.items():
            row["var"].set(valores_autojur.get(chave, ""))

        # 2FA: preenche os campos com os valores do .env.
        valores_2fa = ler_config_2fa()
        for chave, row in self._2fa_rows.items():
            row["var"].set(valores_2fa.get(chave, ""))

        # Reaplica o estado do toggle "Mostrar senhas" aos Entry recriados.
        self._toggle_mostrar_senhas()

    def _toggle_mostrar_senhas(self) -> None:
        mostrar = self.var_mostrar_senhas.get()
        show = "" if mostrar else "•"
        for entry in self._entries_senha:
            with contextlib.suppress(Exception):
                entry.configure(show=show)

    def _salvar_config(self) -> None:
        """Grava credenciais (credenciais.json) e segredos de 2FA (.env)."""
        try:
            itens = [
                {
                    "tj": row["tj"],
                    "login": row["var_login"].get().strip(),
                    "senha": row["var_senha"].get(),
                }
                for row in self._cred_rows
            ]
            if itens:
                salvar_credenciais(itens)
            valores_autojur = {
                chave: row["var"].get().strip() for chave, row in self._autojur_rows.items()
            }
            salvar_config_autojur(valores_autojur)
            valores_2fa = {chave: row["var"].get().strip() for chave, row in self._2fa_rows.items()}
            salvar_config_2fa(valores_2fa)
        except Exception as exc:
            messagebox.showerror("Configurações", f"Não foi possível salvar:\n{exc}")
            return
        self.var_status.set("Configurações salvas.")
        messagebox.showinfo(
            "Configurações",
            "Senhas salvas com sucesso. As alterações já valem para as próximas buscas.",
        )

    # -- ações da aba Organizador ------------------------------------------
    def _escolher_origem(self) -> None:
        path = filedialog.askdirectory(initialdir=self.var_origem.get() or ".")
        if path:
            self.var_origem.set(path)
            if not self.var_destino.get():
                self.var_destino.set(path)

    def _escolher_destino(self) -> None:
        path = filedialog.askdirectory(initialdir=self.var_destino.get() or ".")
        if path:
            self.var_destino.set(path)

    def _abrir_destino(self) -> None:
        path = self.var_destino.get() or self.var_origem.get()
        if path and Path(path).exists():
            os.startfile(path)

    def _limpar_log(self) -> None:
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    def _toggle_log_org(self) -> None:
        """Mostra/oculta o log da aba Organizador."""
        if self._log_org_visivel:
            self._section_log_org.pack_forget()
            self._log_org_visivel = False
            self.btn_detalhes_org.configure(text="Ver detalhes ▾")
        else:
            self._section_log_org.pack(fill="both", expand=True)
            self._log_org_visivel = True
            self.btn_detalhes_org.configure(text="Ocultar detalhes ▴")

    def _espelhar_secretaria_para_vara(self, *_args) -> None:
        if self._espelhando_sec_vara:
            return
        self._espelhando_sec_vara = True
        try:
            self.var_vara.set(self.var_secretaria.get())
        finally:
            self._espelhando_sec_vara = False

    def _espelhar_vara_para_secretaria(self, *_args) -> None:
        if self._espelhando_sec_vara:
            return
        self._espelhando_sec_vara = True
        try:
            self.var_secretaria.set(self.var_vara.get())
        finally:
            self._espelhando_sec_vara = False

    def _log(self, msg: str) -> None:
        self._log_queue.put(msg)

    def _memorizar_processo(self, numero: str, proc: Processo) -> None:
        """Guarda o Processo completo no cache LRU, respeitando o teto."""
        if not numero:
            return
        self._full_cache[numero] = proc
        self._full_cache.move_to_end(numero)
        while len(self._full_cache) > self._max_full_cache:
            self._full_cache.popitem(last=False)  # descarta o menos usado

    def _append_log(self, widget: tk.Text, mensagens: list[str]) -> None:
        """Acrescenta linhas a um log da tela, em lote e com teto de tamanho.

        Dois problemas que isto resolve:

        1) **Crescimento sem limite.** Os `tk.Text` de log só recebiam `insert`;
           nada nunca era removido. Uma passada de monitoramento emite até 11
           linhas por processo — com 1.600 processos são 6-17 mil linhas presas
           no widget (cada uma com suas estruturas internas do Tk) até fechar o
           app. Numa máquina de 4 GB isso é memória que falta em outro lugar.

        2) **Um redesenho por mensagem.** O código anterior fazia
           `configure(state) → insert → see("end") → configure(state)` DENTRO do
           laço de drenagem, ou seja duas trocas de estado e uma rolagem para
           cada mensagem. Agora é uma vez por lote drenado.

        O corte é pelo começo (as linhas mais ANTIGAS somem) — quem está olhando
        o log quer o fim.
        """
        if not mensagens:
            return
        try:
            widget.configure(state="normal")
            widget.insert("end", "".join(mensagens))
            # `index("end-1c")` devolve "linha.coluna" da última posição real.
            total = int(widget.index("end-1c").split(".")[0])
            if total > self._max_linhas_log:
                sobra = total - self._max_linhas_log
                widget.delete("1.0", f"{sobra + 1}.0")
            widget.see("end")
        except tk.TclError:
            pass  # widget destruído (app fechando) — log não é crítico
        finally:
            with contextlib.suppress(tk.TclError):
                widget.configure(state="disabled")

    # -- bomba única de eventos --------------------------------------------
    # Cada worker fala com a GUI por uma queue.Queue própria (Tkinter só é seguro
    # na main thread). Antes cada fila tinha o SEU laço `after()` de 100-200 ms se
    # reagendando eternamente — 13 laços, ~85 callbacks/s mesmo com o app parado.
    # Agora um único `after` chama todos os drenos. Cada dreno já é barato quando
    # a fila está vazia (get_nowait levanta Empty na hora), e o par
    # (fila, dreno) abaixo evita até essa chamada quando não há nada.
    #
    # Ao acrescentar uma fila nova: registre o par aqui. NÃO volte a criar um
    # `after()` próprio dentro do dreno (ver tests/test_pump.py).
    def _pares_pump(self) -> tuple[tuple[object, object], ...]:
        return (
            (self._log_queue, self._drain_ui_queues),        # log + progresso + status
            (self._progress_queue, self._drain_ui_queues),
            (self._status_queue, self._drain_ui_queues),
            (self._pesquisa_log_queue, self._drain_pesquisa_log_queue),
            (self._pesquisa_load_queue, self._drain_pesquisa_load_result),
            (self._busca_result_queue, self._drain_busca_result),
            (self._monitor_result_queue, self._drain_monitor_result),
            (self._senhas_result_queue, self._drain_senhas_result),
            (self._notify_queue, self._drain_notify_queue),
            (self._gen_result_queue, self._drain_gen_result_queue),
            (self._autojur_log_queue, self._drain_autojur_log_queue),
            (self._autojur_result_queue, self._drain_autojur_result),
            (self._autojur_tarefa_result_queue, self._drain_autojur_tarefa_result),
            (self._autojur_escolha_queue, self._drain_autojur_escolha),
            (self._envio_autojur_result_queue, self._drain_envio_autojur_result),
        )

    def _pump(self) -> None:
        """Drena o que tem conteúdo e reagenda a si mesma. Nunca deixa de
        reagendar: um erro num dreno não pode congelar a comunicação
        worker → GUI."""
        try:
            chamados = set()
            for fila, dreno in self._pares_pump():
                if dreno in chamados or fila.empty():
                    continue
                chamados.add(dreno)
                try:
                    dreno()
                except Exception:
                    pass
            # A barra indeterminada é estado, não fila: precisa ser reavaliada
            # sempre, mas só faz trabalho enquanto há worker vivo.
            self._atualizar_progresso_pesquisa()
            # Ociosidade do Autojur: estado também, mas conferida de longe em
            # longe — a comparação de floats é o único custo por tique.
            agora = time.monotonic()
            if agora >= self._proxima_checagem_ocioso:
                self._proxima_checagem_ocioso = agora + _CHECAGEM_OCIOSO_S
                with contextlib.suppress(Exception):
                    self._fechar_autojur_ocioso()
        finally:
            self.after(self._pump_ms, self._pump)

    def _drain_ui_queues(self) -> None:
        """Drena filas de log/progresso/status (main thread). Tkinter só é
        seguro nesta thread, então toda atualização vinda de workers passa por
        aqui.

        Estas três filas pertencem à aba Organizador, que é construída sob
        demanda (ver `_build_widgets`). Elas só recebem mensagens quando aquele
        fluxo roda — e para rodar a aba precisa ter sido aberta. Mesmo assim o
        guarda abaixo existe: sem ele, uma mensagem inesperada viraria
        AttributeError dentro do `_pump`, a cada 200 ms, para sempre."""
        if not hasattr(self, "txt"):
            return
        # Logs: insere todas as mensagens acumuladas.
        try:
            mensagens: list[str] = []
            while True:
                mensagens.append(self._log_queue.get_nowait())
        except queue.Empty:
            pass
        self._append_log(self.txt, [msg + "\n" for msg in mensagens])

        # Progresso: só interessa o último valor — evita N redraws.
        ultimo_progress: tuple[int, int] | None = None
        try:
            while True:
                ultimo_progress = self._progress_queue.get_nowait()
        except queue.Empty:
            pass
        if ultimo_progress is not None:
            current, total = ultimo_progress
            pct = int(current / max(total, 1) * 100)
            self._progress_var.set(min(pct, 100))

        # Status: aplica todas as mensagens em ordem (status text, botões etc.).
        try:
            while True:
                tipo, valor = self._status_queue.get_nowait()
                if tipo == "status":
                    self.var_status.set(str(valor))
                elif tipo == "btn_run":
                    self.btn_run.configure(state=str(valor))
                elif tipo == "btn_cancel":
                    self.btn_cancel.configure(state=str(valor))
                elif tipo == "progress_set":
                    try:
                        self._progress_var.set(int(valor))
                    except (TypeError, ValueError):
                        pass
        except queue.Empty:
            pass


    # -- feedback de conclusão ---------------------------------------------
    def _notificar(
        self, titulo: str, mensagem: str,
        nivel: str = "info", pasta: str | None = None,
    ) -> None:
        """Enfileira uma notificação de conclusão (thread-safe).

        ``nivel``: "info" | "warning" | "error". Para "info" com ``pasta``
        existente, o popup oferece abrir a pasta. Pode ser chamado de
        qualquer thread; o popup aparece na main thread via _drain_notify_queue.
        """
        self._notify_queue.put((nivel, titulo, mensagem, pasta))

    def _drain_notify_queue(self) -> None:
        # Uma notificação por tick + guarda _notify_busy: o messagebox roda um
        # loop de eventos aninhado, então sem isso diálogos se empilhariam.
        if not self._notify_busy:
            try:
                nivel, titulo, mensagem, pasta = self._notify_queue.get_nowait()
            except queue.Empty:
                nivel = None  # type: ignore[assignment]
            if nivel is not None:
                self._notify_busy = True
                try:
                    self._mostrar_notificacao(nivel, titulo, mensagem, pasta)
                finally:
                    self._notify_busy = False

    def _mostrar_notificacao(
        self, nivel: str, titulo: str, mensagem: str, pasta: str | None,
    ) -> None:
        self.var_status.set(mensagem)
        with contextlib.suppress(Exception):
            self.bell()  # sinal sonoro de conclusão
        if nivel == "error":
            messagebox.showerror(titulo, mensagem)
        elif nivel == "warning":
            messagebox.showwarning(titulo, mensagem)
        elif pasta and Path(pasta).exists():
            if messagebox.askyesno(titulo, f"{mensagem}\n\nAbrir a pasta de destino?"):
                with contextlib.suppress(Exception):
                    os.startfile(pasta)
        else:
            messagebox.showinfo(titulo, mensagem)

    def _detectar_word_pdf(self) -> None:
        """Detecta o conversor de PDF fora do caminho crítico de abertura.

        Roda numa thread porque mexe em registro/disco. O resultado dita se a
        opção "PDF" fica habilitada — e o rótulo diz QUAL programa foi encontrado,
        para o usuário não descobrir só depois de gerar que o PDF não saiu."""
        def run() -> None:
            from . import pdf

            conversor = pdf.conversor_disponivel()

            def aplicar() -> None:
                if self._cb_pdf is None or self._lbl_pdf_status is None:
                    return
                if conversor:
                    self._cb_pdf.configure(state="normal")
                    self.var_gerar_pdf.set(True)
                    self._lbl_pdf_status.configure(
                        text=f"(via {pdf.rotulo(conversor)})", bootstyle="secondary"
                    )
                else:
                    self._cb_pdf.configure(state="disabled")
                    self.var_gerar_pdf.set(False)
                    self._lbl_pdf_status.configure(
                        text=f"({pdf.descricao_indisponivel()})",
                        bootstyle="danger",
                    )

            with contextlib.suppress(Exception):
                self.after(0, aplicar)

        threading.Thread(target=run, daemon=True).start()

    def _on_run(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        origem = Path(self.var_origem.get()).expanduser()
        destino = Path(self.var_destino.get() or self.var_origem.get()).expanduser()
        if not origem.is_dir():
            messagebox.showerror("Erro", f"Pasta de origem não existe:\n{origem}")
            return

        self._cancel_event.clear()
        self.btn_run.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.var_status.set("Organizando...")
        self._limpar_log()
        self._progress_var.set(0)
        self._progress_max.set(100)
        self.progress.configure(maximum=100)

        def run() -> None:
            try:
                def progress_cb(current: int, total: int) -> None:
                    self._progress_queue.put((current, total))

                resumo = organizar_pasta(
                    origem, destino, log=self._log,
                    cancel_event=self._cancel_event,
                    progress_cb=progress_cb,
                )
                if self._cancel_event.is_set():
                    self._log_queue.put("\nOperação cancelada pelo usuário.")
                    self._status_queue.put(("status", "Cancelado."))
                    self._notificar(
                        "Organização cancelada",
                        "A operação foi cancelada pelo usuário.",
                        nivel="warning",
                    )
                else:
                    self._log_queue.put(
                        f"\nResumo: {resumo['organizados']} organizado(s), "
                        f"{resumo['nao_identificados']} não identificado(s)."
                    )
                    self._status_queue.put((
                        "status",
                        f"Concluído — {resumo['organizados']} processo(s) organizado(s).",
                    ))
                    self._notificar(
                        "Organização concluída",
                        f"{resumo['organizados']} processo(s) organizado(s), "
                        f"{resumo['nao_identificados']} não identificado(s).",
                        pasta=str(destino),
                    )
                self._status_queue.put(("progress_set", 100))
            except Exception as exc:
                self._log_queue.put(f"ERRO: {exc}")
                self._status_queue.put(("status", "Erro durante execução."))
                self._notificar("Erro na organização", str(exc), nivel="error")
            finally:
                self._status_queue.put(("btn_run", "normal"))
                self._status_queue.put(("btn_cancel", "disabled"))

        self._worker = threading.Thread(target=run, daemon=True)
        self._worker.start()

    def _on_cancel(self) -> None:
        self._cancel_event.set()
        self.btn_cancel.configure(state="disabled")
        self.var_status.set("Cancelando...")

    # -- ações da aba Pesquisa de Processo ---------------------------------
    def _escolher_pesquisa_base(self) -> None:
        path = filedialog.askdirectory(initialdir=self.var_pesquisa_base.get() or PASTA_PADRAO)
        if path:
            self.var_pesquisa_base.set(path)
            _salvar_ui_state(pesquisa_base=path)

    # NOTA: aqui existia `_listar_processos`, que chamava
    # pesquisa.carregar_processos_pesquisa() — a implementação anterior ao índice
    # de resumos (v1.8.2). Ela lê e parseia TODOS os metadados.json (~150 MB em
    # 5.000 pastas nesta instalação) para objetos Processo completos. Nada a
    # chamava desde o refactor, mas continuava ali, a um autocomplete de distância
    # de voltar ao caminho de abertura. A carga correta é
    # `_iniciar_carregamento_pesquisa`, abaixo, que usa o índice incremental.

    def _iniciar_carregamento_pesquisa(self, silencioso: bool = False) -> None:
        if self._pesquisa_load_worker and self._pesquisa_load_worker.is_alive():
            return
        pasta = Path(self.var_pesquisa_base.get()).expanduser()
        online_dir = PROCESSOS_ONLINE_DIR
        base_json = PROCESSOS_BASE_JSON
        if not silencioso:
            self.var_status.set("Carregando processos...")
            self._pesquisa_log_queue.put("Carregando lista de processos...\n")

        def run() -> None:
            try:
                # Carrega só RESUMOS (índice incremental por mtime) — rápido mesmo
                # com milhares de processos. O completo é lido ao selecionar.
                resumos = carregar_resumos_pesquisa(
                    pasta, online_dir, base_json, PROCESSOS_INDICE_JSON,
                )
                aprender_comarcas(resumos)  # usa numero/comarca do resumo
                self._pesquisa_load_queue.put(("ok", resumos, silencioso))
            except Exception as exc:
                self._pesquisa_load_queue.put(("erro", str(exc), silencioso))

        self._pesquisa_load_worker = threading.Thread(target=run, daemon=True)
        self._pesquisa_load_worker.start()

    def _carregar_pesquisa_inicial(self) -> None:
        """Carga silenciosa no startup: popula a lista (base + buscados online),
        sem popup de erro quando a pasta base ainda não foi escolhida."""
        self._iniciar_carregamento_pesquisa(silencioso=True)

    def _atualizar_processo_selecionado(self) -> None:
        """Busca de novo NA WEB o processo selecionado e mostra o que mudou.

        É o botão para quando a pessoa olha a tela e vê algo errado. O caminho
        normal ("Buscar online") é cache-first: com o processo já na lista ele só
        seleciona a linha e nem vai à web, a menos que "Forçar busca online"
        esteja marcado. Aqui a força é implícita — o sentido do botão É atualizar.

        Antes de ir à web, limpa o que a máquina lembra deste processo, porque
        há dois caches que sobrevivem a uma recarga comum e serviriam dado velho
        se a busca falhar (ou se o problema nunca tiver sido do tribunal):

        1. ``self._full_cache`` (memória), que alimenta o painel de detalhes;
        2. a entrada dele no ``processos_indice.json``, que alimenta as COLUNAS
           da lista e é reaproveitada enquanto o ``mtime`` do metadados.json não
           muda — um arquivo corrigido por fora (copiar por cima preserva o mtime
           da origem) sobrevive a QUALQUER recarga. Conferido ao vivo: sujando o
           resumo à mão, a coluna Classe mostrava "LIXO DE CACHE" depois de
           recarregar a lista inteira.

        O resultado da busca é aplicado pelo caminho normal
        (``_drain_busca_result``), que salva em PROCESSOS_ONLINE_DIR, atualiza a
        linha e reseleciona. O que este botão acrescenta lá é a COMPARAÇÃO: sem
        ela o usuário fica sem saber se a ida ao tribunal mudou alguma coisa."""
        sel = self.tree.selection()
        numero = sel[0] if sel else ""
        if not numero:
            self._notificar(
                "Atualizar processo",
                "Selecione um processo na lista primeiro.",
                nivel="warning",
            )
            return

        # Guardado para o fim da busca dizer o que mudou (ver _drain_busca_result).
        self._comparar_apos_busca = (numero, self._instantaneo_processo(numero))

        self._full_cache.pop(numero, None)
        esquecidas = esquecer_processo(PROCESSOS_INDICE_JSON, numero)
        self._pesquisa_log_queue.put(
            f"Atualizando {numero}: cache em memória descartado, "
            f"{esquecidas} entrada(s) do índice esquecida(s). Indo ao tribunal...\n"
        )
        self._buscar_online(numero=numero, forcar=True)

    def _instantaneo_processo(self, numero: str) -> dict[str, str]:
        """Como o processo aparece agora, nas DUAS superfícies que divergem.

        A linha da lista vem do resumo (cacheado por mtime no índice); os campos
        de detalhe vêm do arquivo, relidos a cada seleção. Um índice velho só
        aparece na lista, e comparar só o detalhe faria o botão consertar a
        coluna e ainda avisar "nenhum campo mudou" — foi o que aconteceu no 1º
        teste ao vivo. Os rótulos das colunas saem do próprio Treeview, para não
        duplicar aqui a lista de colunas."""
        fora: dict[str, str] = {}
        with contextlib.suppress(Exception):
            if self.tree.exists(numero):
                valores = self.tree.item(numero, "values")
                for coluna, valor in zip(self.tree["columns"], valores):
                    rotulo = self.tree.heading(coluna).get("text") or coluna
                    fora[f"lista · {rotulo}"] = str(valor)
        proc = self._proc_selecionado
        if proc is not None:
            for atr in self._det_ordem:
                if atr in ("autor", "reu"):
                    continue     # derivados de `partes`, não atributos do Processo
                rotulo = self._det_rotulos.get(atr, atr).rstrip(":")
                fora[f"detalhe · {rotulo}"] = str(getattr(proc, atr, "") or "")
        return fora

    def _relatar_atualizacao(self, numero: str) -> None:
        """Fecha o ciclo do botão "Atualizar processo": compara com o que estava
        na tela antes da busca e diz o que o tribunal mudou.

        Chamado no fim da busca, DEPOIS de a linha ser reescrita e reselecionada.
        Consome o pedido: uma busca comum depois não relata nada."""
        pedido, self._comparar_apos_busca = self._comparar_apos_busca, None
        if not pedido or pedido[0] != numero:
            return
        _, antes = pedido
        with contextlib.suppress(Exception):
            self.update()     # deixa o <<TreeviewSelect>> reescrever os campos
        depois = self._instantaneo_processo(numero)
        mudou = [
            f"{rotulo}: {antes.get(rotulo, '')!r} → {valor!r}"
            for rotulo, valor in depois.items()
            if antes.get(rotulo, "") != valor
        ]
        if mudou:
            self._pesquisa_log_queue.put(
                "Atualizado:\n" + "".join(f"  • {m}\n" for m in mudou)
            )
            self._notificar(
                "Processo atualizado",
                f"{numero}\n\n" + "\n".join(f"  • {m}" for m in mudou),
            )
        else:
            self._pesquisa_log_queue.put(
                f"{numero}: consultado no tribunal, nenhum campo mudou.\n"
            )
            self._notificar(
                "Processo atualizado",
                f"{numero} foi consultado no tribunal agora e nenhum campo "
                f"mudou — o que está na tela é o que o tribunal informa.",
            )

    def _recarregar_pesquisa(self) -> None:
        pasta = Path(self.var_pesquisa_base.get()).expanduser()
        if not pasta.is_dir():
            messagebox.showerror("Erro", f"Pasta não existe:\n{pasta}")
            return
        _salvar_ui_state(pesquisa_base=str(pasta))
        self._iniciar_carregamento_pesquisa(silencioso=False)

    def _drain_pesquisa_load_result(self) -> None:
        try:
            while True:
                tipo, payload, silencioso = self._pesquisa_load_queue.get_nowait()
                if tipo == "ok":
                    procs = payload
                    self._processos_carregados = procs
                    self._textos_filtro = indexar_para_filtro(procs)
                    # Recarga completa: descarta o cache de Processos completos p/
                    # não servir detalhe desatualizado de algo que mudou no disco.
                    self._full_cache.clear()
                    self._filtrar_pesquisa()
                    if not silencioso:
                        self.var_status.set(f"{len(procs)} processo(s) carregado(s).")
                        self._pesquisa_log_queue.put(
                            f"Lista carregada: {len(procs)} processo(s).\n"
                        )
                elif not silencioso:
                    self.var_status.set("Erro ao carregar processos.")
                    self._notificar("Erro ao carregar processos", str(payload), nivel="error")
        except queue.Empty:
            pass

    def _buscar_online(
        self, numero: str | None = None, forcar: bool | None = None,
    ) -> None:
        """Dispara a busca do processo no TJ/sistema escolhido numa thread.

        Cache-first: se o número digitado já estiver carregado (pasta base ou
        cache de buscas anteriores), apenas seleciona a linha — não vai à web.

        `numero`/`forcar` existem para o botão "Atualizar processo", que busca o
        processo SELECIONADO na lista sem mexer no que o usuário digitou na barra
        e sem exigir que ele marque "Forçar busca online". Sem argumento (o caso
        do botão "Buscar online", que o Tk chama sem parâmetro) tudo vem das
        variáveis da tela, como antes."""
        if numero is None:
            texto = self.var_filtro.get().strip()
            if not texto:
                messagebox.showwarning("Buscar processo", "Informe o número do processo (CNJ).")
                return
            # Extrai o CNJ canônico do que foi digitado (a barra também filtra).
            m = RE_NUMERO_CNJ.search(texto)
            numero = m.group(0) if m else texto

        # Se o processo já foi carregado, não precisa buscar na web: seleciona.
        # Exceção: "Forçar busca online" marcado → atualiza o dado pela web.
        if forcar is None:
            forcar = self.var_busca_forcar.get()
        existente = next(
            (p for p in self._processos_carregados if p.numero == numero), None
        )
        if existente and not forcar:
            # `garantir`: a lista é paginada, e o processo procurado pode estar
            # além do teto de linhas — sem isto a seleção abaixo não acharia a
            # linha e a busca pareceria não ter funcionado.
            self._preencher_tree(self._processos_carregados, garantir=existente.numero)
            if self.tree.exists(existente.numero):
                self.tree.selection_set(existente.numero)
                self.tree.see(existente.numero)
            self._pesquisa_log_queue.put(
                f"Processo {numero} já carregado — sem busca na web "
                f"(marque 'Forçar busca online' para atualizar).\n"
            )
            self.var_status.set(f"Processo {numero} já carregado (sem busca na web).")
            return
        if existente and forcar:
            self._pesquisa_log_queue.put(
                f"Processo {numero} já em cache — forçando atualização pela web.\n"
            )

        tj_label = self.var_busca_tj.get()

        # Sugere trocar de sistema em dois casos, ambos resolvidos pela mesma
        # pergunta. (Antes de desabilitar o botão: "Cancelar" só faz return.)
        uf_num = uf_do_cnj(numero)
        uf_sel = uf_efetiva_tj(tj_label)
        sug = titulo = cabecalho = motivo = ""
        if uf_num and uf_sel and uf_num != uf_sel:
            # (a) o número é de outra UF que não a do TJ selecionado.
            sug = sugerir_tj_para_uf(uf_num, numero) or ""
            titulo = "Estado do processo"
            cabecalho = (f"O número parece ser de {uf_num}, mas o sistema "
                         f"selecionado é\n'{tj_label}' ({uf_sel}).\n\n")
            motivo = f"número de {uf_num}"
            # Sem sistema configurado p/ essa UF → segue (aviso no log do scraper).
        else:
            # (b) UF certa, sistema errado: no RJ os três sistemas são todos
            # "RJ", então o caso (a) nunca dispara e a busca falhava calada num
            # número do legado com "TJRJ — PJe" selecionado.
            sug = rota_incompativel_com_numero(tj_label, numero) or ""
            titulo = "Sistema do processo"
            cabecalho = (f"O sistema selecionado é '{tj_label}', mas este "
                         f"número\nnão fica nele.\n\n")
            motivo = "sistema incompatível com o número"
        if sug and sug != tj_label:
            resp = messagebox.askyesnocancel(
                titulo,
                cabecalho
                + f"Deseja trocar para '{sug}'?\n\n"
                + "Sim = trocar e buscar    •    Não = buscar assim mesmo    •    "
                + "Cancelar = não buscar",
            )
            if resp is None:  # Cancelar
                return
            if resp:  # Sim → troca o sistema
                self.var_busca_tj.set(sug)
                tj_label = sug
                self._pesquisa_log_queue.put(
                    f"Sistema trocado para {sug} ({motivo}).\n"
                )

        if self._busca_worker and self._busca_worker.is_alive():
            return
        # Fecha um navegador de debug remanescente da busca anterior antes de
        # abrir outro (evita acumular janelas do Chrome).
        if self._debug_scraper is not None:
            self._fechar_debug()
        self._btn_buscar.configure(state="disabled")
        headless = self.var_busca_headless.get()
        modo_docs = "atos" if self.var_busca_docs.get() else "nenhum"
        modo_debug = _DEBUG_MODOS.get(self.var_busca_debug.get(), "off")
        self._pesquisa_log_queue.put(f"Buscando {numero} em {tj_label}...\n")

        def run() -> None:
            # COM precisa ser inicializado por thread (selenium/win32 podem usar).
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except Exception:
                pass
            writer = _LogWriter(self._pesquisa_log_queue.put)
            try:
                # Sob pythonw, sys.stdout é None; redireciona os print() do
                # scraper para o log da aba (e evita que print() quebre).
                with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                    proc = buscar_processo_online(
                        numero, tj_label, headless,
                        log=self._pesquisa_log_queue.put, modo_docs=modo_docs,
                        modo_debug=modo_debug, reter_scraper=self._reter_debug_scraper,
                    )
                self._busca_result_queue.put(("ok", proc))
            except Exception as exc:
                self._busca_result_queue.put(("erro", _descrever_erro(exc)))

        self._busca_worker = threading.Thread(target=run, daemon=True)
        self._busca_worker.start()

    def _reter_debug_scraper(self, scraper: object) -> None:
        """Guarda o scraper aberto em modo debug (chamado da thread worker) e
        habilita o botão de fechar o navegador na main thread."""
        self._debug_scraper = scraper
        # Tkinter só é seguro na main thread; o resto do código já usa
        # self.after(0, ...) a partir de workers (ver _scan_templates_em_background).
        self.after(0, lambda: self._btn_fechar_debug.configure(state="normal"))

    def _fechar_debug(self) -> None:
        """Fecha o navegador retido em modo debug (em thread, pois quit() pode
        demorar) e desabilita o botão."""
        scraper = self._debug_scraper
        self._debug_scraper = None
        self._btn_fechar_debug.configure(state="disabled")
        if scraper is None:
            return
        self._pesquisa_log_queue.put("Fechando navegador de debug...\n")
        self._fechar_em_thread(scraper)

    def _drain_busca_result(self) -> None:
        """Drena resultados da busca online na main thread (Tkinter não é
        thread-safe). Mescla o Processo na lista e seleciona a linha."""
        try:
            while True:
                tipo, payload = self._busca_result_queue.get_nowait()
                if tipo == "ok":
                    proc = payload
                    # Ao atualizar pela web um processo já em cache (ex.: busca
                    # forçada sobre um caso vindo das planilhas), preserva a tag
                    # de origem "Mapeamento: ..." — não perde o que veio da
                    # planilha.
                    anterior = next(
                        (r for r in self._processos_carregados if r.numero == proc.numero), None
                    )
                    if anterior is not None:
                        for t in anterior.tags:
                            if str(t).startswith("Mapeamento:") and t not in proc.tags:
                                proc.tags.insert(0, t)
                        # O que o tribunal NÃO trouxe não pode APAGAR o que já se
                        # sabia. Uma consulta traz o que aquela página mostra, e
                        # quase nunca é o mesmo conjunto de campos da vez
                        # anterior. Visto ao vivo ao testar o botão "Atualizar
                        # processo": a busca voltou sem `juiz`, `orgao_julgador`
                        # e `ultima_movimentacao`, e os três SUMIRAM da tela —
                        # quem clicou para consertar um campo via três outros
                        # esvaziarem.
                        #
                        # Aqui só a LACUNA do resultado novo é preenchida com o
                        # registro anterior; tudo que o tribunal trouxe vence
                        # (ver pesquisa.preencher_lacunas). Isto generaliza o
                        # `if not proc.valor_causa` que existia antes, que era a
                        # mesma ideia para um campo só.
                        with contextlib.suppress(Exception):
                            completo_antes = carregar_processo_completo(
                                anterior, PROCESSOS_BASE_JSON,
                            )
                            if completo_antes is not None:
                                preencher_lacunas(proc, completo_antes)
                    # Marca de "o usuário mandou atualizar ESTE processo", só
                    # quando a busca veio do botão "Atualizar processo". É ela
                    # que faz o resultado do tribunal continuar valendo depois de
                    # fechar o app: sem a marca ele cai em PROCESSOS_ONLINE_DIR e
                    # PERDE para o registro antigo da pasta base na próxima
                    # abertura (medido: com "CLASSE ERRADA" na pasta base, a
                    # lista e a petição voltavam a mostrar a errada). Ver
                    # models.Processo.atualizado_em e pesquisa.prefere_atualizado.
                    #
                    # Só o botão marca: uma busca comum não deve inverter a
                    # prioridade das fontes por conta própria.
                    pedido = self._comparar_apos_busca
                    if pedido and pedido[0] == proc.numero:
                        from datetime import datetime
                        proc.atualizado_em = datetime.now().isoformat(timespec="seconds")
                    aprender_comarcas([proc])
                    # Persiste o processo buscado online para reaparecer ao reabrir.
                    salvar_processo_metadados(proc, PROCESSOS_ONLINE_DIR)
                    # A lista guarda RESUMOS; o completo vai pro cache p/ seleção
                    # imediata sem reler o disco. A origem aponta o metadados.json
                    # recém-gravado (o índice o pega por mtime na próxima carga).
                    origem = str(PROCESSOS_ONLINE_DIR / proc.numero / "metadados.json")
                    resumo = _resumo_de_processo(proc, "online", origem)
                    self._memorizar_processo(proc.numero, proc)
                    self._processos_carregados = [
                        r for r in self._processos_carregados if r.numero != proc.numero
                    ]
                    self._processos_carregados.append(resumo)
                    self._textos_filtro = indexar_para_filtro(self._processos_carregados)
                    self._preencher_tree(
                        self._processos_carregados, garantir=proc.numero
                    )
                    if proc.numero and self.tree.exists(proc.numero):
                        self.tree.selection_set(proc.numero)
                        self.tree.see(proc.numero)
                    self._pesquisa_log_queue.put(f"Processo {proc.numero} carregado.\n")
                    self.var_status.set(f"Processo {proc.numero} carregado.")
                    # Se a busca veio do botão "Atualizar processo", diz o que o
                    # tribunal mudou. Depois do _preencher_tree e da seleção, para
                    # comparar a linha já reescrita.
                    with contextlib.suppress(Exception):
                        self._relatar_atualizacao(proc.numero)
                    with contextlib.suppress(Exception):
                        self.bell()  # sinal sonoro de conclusão da busca
                else:
                    # Descarta o pedido do "Atualizar processo": sem isto a
                    # PRÓXIMA busca comum herdaria a comparação desta, que falhou.
                    self._comparar_apos_busca = None
                    self._pesquisa_log_queue.put(f"! Erro na busca online: {payload}\n")
                    self._notificar("Busca online", str(payload), nivel="error")
                self._btn_buscar.configure(state="normal")
        except queue.Empty:
            pass

    # -- monitoramento: planilhas → mapeamento de sistema ------------------
    def _iniciar_monitoramento(self) -> None:
        """Dispara uma passada de monitoramento (planilhas → mapeamento) numa
        thread. Lê as planilhas, pula os já mapeados e raspa os pendentes.

        Só o botão chama isto — não há disparo automático (ver ``__init__``).
        Cada processo tem uma tentativa por dia; clicar de novo no mesmo dia não
        reconsulta os tribunais (organizador.monitoramento.DIARIO_TENTATIVAS)."""
        if self._monitor_worker and self._monitor_worker.is_alive():
            self.var_status.set("Monitoramento já está rodando.")
            return
        self._monitor_cancel.clear()
        self._btn_monitor.configure(state="disabled")
        self._btn_monitor_parar.configure(state="normal")
        self.var_status.set("Sincronizando planilhas e mapeando processos...")
        self._pesquisa_log_queue.put(
            "\n=== Monitoramento: planilhas → mapeamento de sistema ===\n"
        )
        headless = self.var_busca_headless.get()

        def _log(msg: str) -> None:
            self._pesquisa_log_queue.put(msg + "\n")

        def run() -> None:
            # COM precisa ser inicializado por thread (selenium/win32 podem usar).
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except Exception:
                pass
            writer = _LogWriter(self._pesquisa_log_queue.put)
            try:
                # Sob pythonw, sys.stdout é None: redireciona os print() do motor/
                # scraper para o log da aba (e evita que print() quebre).
                with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                    from organizador import monitoramento
                    resumo = monitoramento.rodar_passada(
                        headless=headless, log=_log, cancel=self._monitor_cancel,
                    )
                self._monitor_result_queue.put(("ok", resumo))
            except Exception as exc:
                self._monitor_result_queue.put(("erro", _descrever_erro(exc)))

        self._monitor_worker = threading.Thread(target=run, daemon=True)
        self._monitor_worker.start()

    def _parar_monitoramento(self) -> None:
        """Sinaliza o cancelamento; a passada para no próximo processo (o atual
        termina para não deixar a sessão do navegador num estado sujo)."""
        self._monitor_cancel.set()
        self._btn_monitor_parar.configure(state="disabled")
        self.var_status.set("Parando o monitoramento após o processo atual...")
        self._pesquisa_log_queue.put(
            "⏹️ parada solicitada — aguardando o processo atual terminar...\n"
        )

    def _drain_monitor_result(self) -> None:
        """Drena o resultado do monitoramento na main thread (Tkinter não é
        thread-safe): reabilita os botões, avisa e recarrega a lista."""
        try:
            while True:
                tipo, payload = self._monitor_result_queue.get_nowait()
                self._btn_monitor.configure(state="normal")
                self._btn_monitor_parar.configure(state="disabled")
                if tipo == "ok":
                    r = payload
                    msg = (f"{r.get('mapeados_agora', 0)} mapeados agora • "
                           f"{r.get('nao_encontrados', 0)} não encontrados • "
                           f"{r.get('ja_mapeados', 0)} já estavam mapeados "
                           f"(de {r.get('total', 0)} nas planilhas).")
                    # Sem isto os números não fecham: os adiados pelo cooldown não
                    # são "já mapeados" nem foram tentados agora.
                    if r.get("adiados"):
                        msg += (f" {r['adiados']} já tiveram a tentativa de hoje "
                                f"— voltam amanhã.")
                    self.var_status.set("Monitoramento concluído: " + msg)
                    self._pesquisa_log_queue.put("✔️ " + msg + "\n")
                    # Novos processos no cache → recarrega a lista p/ aparecerem.
                    if r.get("mapeados_agora"):
                        with contextlib.suppress(Exception):
                            self._recarregar_pesquisa()
                    self._notificar("Monitoramento", msg, nivel="info")
                    # Registro da última passada (nada dispara por tempo; fica
                    # como referência para diagnóstico).
                    from datetime import datetime as _dt
                    _salvar_ui_state(
                        ultimo_monitoramento=_dt.now().isoformat(timespec="seconds")
                    )
                else:
                    self._pesquisa_log_queue.put(f"! Erro no monitoramento: {payload}\n")
                    self.var_status.set("Monitoramento: erro (ver detalhes).")
                    self._notificar("Monitoramento", str(payload), nivel="error")
        except queue.Empty:
            pass

    def _preencher_tree(self, processos: list, garantir: str = "") -> None:
        """Popula a lista, no máximo ``self._max_linhas_tree`` linhas por vez.

        A Treeview mostra 12 linhas (``height=12``), mas era preenchida com as
        ~3.200 dos processos, uma chamada Tcl por linha — e refeita do zero a cada
        tecla no filtro. Agora corta no teto e informa o corte, então o custo por
        tecla é constante em vez de proporcional à base.

        `garantir`: número de processo que PRECISA aparecer no recorte (ex.: a
        linha que a busca acabou de encontrar). Sem isto, um processo além do teto
        seria "encontrado" e a seleção falharia silenciosamente."""
        total = len(processos)
        teto = self._max_linhas_tree
        visiveis = processos[:teto]
        if garantir and total > teto:
            if not any((p.numero or "") == garantir for p in visiveis):
                achado = next((p for p in processos if (p.numero or "") == garantir), None)
                if achado is not None:
                    visiveis = [achado] + visiveis[: teto - 1]

        for item in self.tree.get_children():
            self.tree.delete(item)
        for iid, values, tag in linhas_tree_processos(visiveis):
            self.tree.insert("", "end", iid=iid, values=values, tags=(tag,))

        if total > len(visiveis):
            self.var_contagem_lista.set(
                f"mostrando {len(visiveis)} de {total} — refine o filtro para ver o resto"
            )
        else:
            self.var_contagem_lista.set(f"{total} processo(s)")
        _perf(f"tree:preenchida({len(visiveis)}de{total})")

    def _agendar_filtrar_pesquisa(self) -> None:
        if self._filtro_after_id is not None:
            with contextlib.suppress(Exception):
                self.after_cancel(self._filtro_after_id)
        self._filtro_after_id = self.after(self._debounce_filtro_ms, self._filtrar_pesquisa)

    def _filtrar_pesquisa(self, garantir: str = "") -> None:
        self._filtro_after_id = None
        self._preencher_tree(
            filtrar_processos_indexado(
                self._processos_carregados,
                self._textos_filtro,
                self.var_filtro.get(),
            ),
            garantir=garantir,
        )

    def _abrir_guia_credenciais(self) -> None:
        """Abre no navegador o guia de configuração do e-mail do 2FA.

        O HTML viaja no bundle e não tem dependência externa, então abre offline.
        `as_uri()` é necessário porque o caminho tem acentos e espaços (e, no exe,
        fica sob _MEIPASS) — passar a string crua faz o navegador tratar como
        termo de busca."""
        if not CAMINHO_GUIA_CREDENCIAIS.exists():
            messagebox.showwarning(
                "Guia não encontrado",
                "O guia não veio nesta instalação do programa.\n\n"
                "Você ainda consegue configurar tudo por esta tela: ligue o "
                "Modo desenvolvedor na seção Opções para revelar a seção de "
                "autenticação em dois fatores.",
                parent=self,
            )
            return
        try:
            _abrir_no_navegador(CAMINHO_GUIA_CREDENCIAIS.as_uri())
        except Exception as exc:
            messagebox.showerror(
                "Não foi possível abrir o guia",
                f"O guia está em:\n{CAMINHO_GUIA_CREDENCIAIS}\n\n"
                f"Abra esse arquivo no navegador manualmente.\n\nDetalhe: {exc}",
                parent=self,
            )

    def _toggle_modo_dev(self) -> None:
        """Persiste o modo dev e aplica a visibilidade na hora."""
        _salvar_ui_state(modo_dev=self.var_modo_dev.get())
        self._aplicar_modo_dev()

    def _toggle_modo_leve(self) -> None:
        """Persiste o Modo leve.

        Só o que é decidido na abertura (pré-login, monitoramento automático,
        paginação da lista, intervalo dos temporizadores) muda de verdade na
        próxima execução — por isso o aviso, em vez de tentar reconfigurar a
        janela viva."""
        ativo = bool(self.var_modo_leve.get())
        _salvar_ui_state(modo_leve=ativo)
        # Vale já para o que é consultado sob demanda (ex.: limite do PoolScrapers).
        self._modo_leve = ativo
        self._autojur_ocioso_s = (
            _AUTOJUR_OCIOSO_LEVE_S if ativo else _AUTOJUR_OCIOSO_S
        )
        self.var_status.set(
            "Modo leve LIGADO — vale integralmente na próxima abertura." if ativo
            else "Modo leve desligado — vale integralmente na próxima abertura."
        )

    def _toggle_autojur_salvar(self) -> None:
        """Persiste a preferência 'salvar no Autojur' e sincroniza o toggle da
        aba Autojur (o modal de envio lê a preferência ao abrir)."""
        ativo = bool(self.var_autojur_salvar_prod.get())
        _salvar_ui_state(autojur_salvar=ativo)
        with contextlib.suppress(Exception):
            self.var_autojur_salvar.set(ativo)
        self.var_status.set(
            "Autojur: tarefas serão SALVAS." if ativo
            else "Autojur: tarefas só preenchidas (não salvas)."
        )

    def _toggle_autojur_visualizar(self) -> None:
        """Persiste a preferência de visualizar o navegador do Autojur."""
        ativo = bool(self.var_autojur_visualizar.get())
        _salvar_ui_state(autojur_visualizar=ativo)
        self.var_status.set(
            "Autojur: navegador VISÍVEL." if ativo
            else "Autojur: navegador oculto (salvo em conferência)."
        )

    def _toggle_autojur_prelogin(self) -> None:
        """Persiste a preferência de pré-login; se ligada agora, aquece já."""
        ativo = bool(self.var_autojur_prelogin.get())
        _salvar_ui_state(autojur_prelogin=ativo)
        if ativo:
            self._iniciar_prelogin_autojur()
            self.var_status.set("Autojur: pré-login em 2º plano...")
        else:
            self.var_status.set("Autojur: pré-login desligado.")

    def _autojur_headless(self, salvar: bool, visualizar: bool | None = None) -> bool:
        """Regra única de visibilidade do navegador do Autojur.

        Fica VISÍVEL (headless=False) sempre que: `salvar` está desligado (a
        pessoa vai conferir os dados), OU "Visualizar navegador" está ligado,
        OU o modo desenvolvedor está ligado. Caso contrário roda oculto.
        """
        if visualizar is None:
            visualizar = bool(self.var_autojur_visualizar.get())
        visivel = (not salvar) or visualizar or bool(self.var_modo_dev.get())
        return not visivel

    def _aplicar_modo_dev(self) -> None:
        """Mostra/oculta os elementos avançados conforme o modo dev:
        aba Organizador, opções avançadas da busca e a seção 2FA das config."""
        dev = bool(self.var_modo_dev.get())

        # Abas Organizador e Autojur (preserva a posição usando o estado da aba).
        if getattr(self, "_nb", None) is not None:
            with contextlib.suppress(Exception):
                self._nb.tab(self._tab_org, state="normal" if dev else "hidden")
                self._nb.tab(self._tab_autojur, state="normal" if dev else "hidden")
                if not dev and self._nb.select() in (str(self._tab_org), str(self._tab_autojur)):
                    self._nb.select(self._tab_pesq)

        # Opções avançadas da busca (grid_remove preserva a posição ao reexibir).
        for w in getattr(self, "_dev_widgets_busca", []):
            with contextlib.suppress(Exception):
                w.grid() if dev else w.grid_remove()

        # Seção "Base de dados" da aba de pesquisa (pasta base + lista).
        sec_base = getattr(self, "_sec_base", None)
        sec_busca = getattr(self, "_sec_busca", None)
        if sec_base is not None and sec_busca is not None:
            with contextlib.suppress(Exception):
                if dev:
                    sec_base.pack(fill="x", pady=(0, 6), before=sec_busca)
                else:
                    sec_base.pack_forget()

        # Seção 2FA das configurações (re-empacota antes da barra de ações).
        sec = getattr(self, "_sec_2fa", None)
        if sec is not None:
            with contextlib.suppress(Exception):
                if dev:
                    sec.pack(fill="x", pady=(0, 8), before=self._f_acoes_config)
                else:
                    sec.pack_forget()

    # Campos de "Dados do processo" que mapeiam 1:1 para atributos do Processo.
    # (numero fica de fora: é a chave da linha na árvore — editá-lo na hora da
    # geração é lido direto da var, sem mexer no objeto.)
    _ATRS_DET_PROC = ("classe", "orgao_julgador", "juiz", "tribunal",
                      "valor_causa", "ultima_movimentacao")
    # Detalhe → campo editável equivalente (mantém a geração consistente quando o
    # usuário corrige pelo painel de dados).
    _ESPELHO_DET_EDIT = {"juiz": "var_juiz", "tribunal": "var_tribunal",
                         "valor_causa": "var_valor_causa"}

    def _on_detalhe_edit(self, atr: str) -> None:
        """Edição de um campo dos dados do processo: espelha no campo editável
        equivalente e grava de volta no Processo selecionado."""
        proc = self._proc_selecionado
        if proc is None:
            return
        val = self._det_vars[atr].get()
        nome_var = self._ESPELHO_DET_EDIT.get(atr)
        if nome_var is not None:
            espelho = getattr(self, nome_var)
            if espelho.get() != val:
                espelho.set(val)
        if atr in self._ATRS_DET_PROC:
            setattr(proc, atr, val.strip())

    def _mostrar_linha_detalhe(self, atr: str, visivel: bool) -> None:
        """Mostra/oculta uma linha (rótulo+valor) dos dados do processo. Linha
        oculta colapsa no grid (sem buraco)."""
        widgets = self._det_widgets.get(atr)
        if not widgets:
            return
        lbl, entry = widgets
        if visivel:
            lbl.grid()
            entry.grid()
        else:
            lbl.grid_remove()
            entry.grid_remove()

    def _memorizar_municipio(self, event: tk.Event | None = None) -> None:
        """Ao sair do campo Município, memoriza o valor para o foro do processo
        selecionado (auto-preenche processos futuros do mesmo foro)."""
        proc = self._proc_selecionado
        if proc is not None:
            aprender_municipio(proc.numero, self.var_municipio.get())

    def _on_select_processo(self, event: tk.Event) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        numero = sel[0]
        resumo = next((r for r in self._processos_carregados if r.numero == numero), None)
        if resumo is None:
            self._proc_selecionado = None
            return
        # Carga preguiçosa do Processo completo (com eventos/atos/partes), lida do
        # metadados.json só agora — e memorizada p/ próximas seleções.
        proc = self._full_cache.get(numero)
        if proc is not None:
            self._full_cache.move_to_end(numero)  # usado agora = mais recente
        else:
            proc = carregar_processo_completo(resumo, PROCESSOS_BASE_JSON)
            if proc is not None:
                self._memorizar_processo(numero, proc)
        if proc is None:
            # metadados.json sumiu desde a última carga: usa o resumo p/ não travar.
            proc = Processo(
                numero=resumo.numero, classe=resumo.classe,
                valor_causa=resumo.valor_causa,
            )
        self._proc_selecionado = proc
        campos = [
            ("numero", proc.numero),
            ("tribunal", proc.tribunal or ""),
            ("classe", proc.classe or ""),
            ("orgao_julgador", proc.orgao_julgador or ""),
            ("juiz", proc.juiz or ""),
            ("valor_causa", proc.valor_causa or ""),
            ("autor", _autor(proc)),
            ("reu", _reus_joined(proc)),
            ("ultima_movimentacao", proc.ultima_movimentacao or ""),
        ]
        for atr, val in campos:
            var = self._det_vars.get(atr)
            if var is not None:
                var.set(val)
        # Sem valor da causa: oculta a linha inteira (colapsa no grid).
        self._mostrar_linha_detalhe("valor_causa", bool((proc.valor_causa or "").strip()))

        auto_map = {
            "valor_causa": proc.valor_causa,
            "comarca": proc.comarca,
            "vara": _limpar_secretaria(proc.vara),
            "tribunal": proc.tribunal,
            "endereco_foro": proc.foro_endereco,
            "juiz": proc.juiz,
            "assunto": proc.assunto,
            "municipio": _municipio_de_processo(proc),
            "estado": proc.uf or _uf_de_numero_cnj(proc.numero),
            "oab_uf": proc.uf or _uf_de_numero_cnj(proc.numero),
            "oab_numero": OAB_ESCRITORIO_POR_UF.get(
                (proc.uf or _uf_de_numero_cnj(proc.numero) or "").upper(), ""
            ),
        }
        for atr, val in auto_map.items():
            var = getattr(self, f"var_{atr}", None)
            if var is not None:
                var.set(val)

        self._carregar_templates()

    # -- cópia dos dados do processo ---------------------------------------
    def _criar_menu_copiar(self) -> tk.Menu:
        """Menu de contexto (botão direito) dos campos de dados do processo."""
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Copiar", command=self._copiar_campo_atual)
        menu.add_command(label="Copiar todos os dados", command=self._copiar_todos_dados)
        return menu

    def _abrir_menu_copiar(self, event: tk.Event) -> None:
        self._det_menu_alvo = event.widget
        try:
            self._det_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._det_menu.grab_release()

    def _copiar_para_clipboard(self, texto: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(texto)
        self.var_status.set("Copiado para a área de transferência.")

    def _copiar_campo_atual(self) -> None:
        """Copia o trecho selecionado do campo clicado; se nada estiver
        selecionado, copia o valor inteiro."""
        alvo = getattr(self, "_det_menu_alvo", None)
        if alvo is None:
            return
        try:
            texto = alvo.selection_get()  # trecho selecionado
        except Exception:
            try:
                texto = alvo.get()  # valor inteiro do Entry
            except Exception:
                texto = ""
        if texto:
            self._copiar_para_clipboard(texto)

    def _copiar_todos_dados(self) -> None:
        """Copia todos os campos como 'Rótulo: valor', um por linha."""
        linhas = []
        for atr in self._det_ordem:
            valor = self._det_vars[atr].get().strip()
            if valor:
                linhas.append(f"{self._det_rotulos[atr]} {valor}")
        if linhas:
            self._copiar_para_clipboard("\n".join(linhas))
        else:
            self.var_status.set("Nenhum dado para copiar — selecione um processo.")

    def _carregar_templates(self) -> None:
        # Preserva as marcações já feitas (ex.: o usuário escolheu modelos antes
        # de selecionar um processo) para não zerá-las ao reconstruir a lista.
        marcados = {p for p, v in self._templates_vars.items() if v.get()}
        pasta = Path(self.var_pesquisa_base.get()).expanduser()
        templates_dir = pasta.parent / "Petições" if "Petições" in str(pasta.parent) else PASTA_PETICOES
        if not templates_dir.is_dir():
            templates_dir = PASTA_PETICOES
        if templates_dir == self._templates_dir_atual and self._templates_vars:
            self._scan_templates_em_background()
            self._on_template_toggle()
            return

        for w in self._templates_inner.winfo_children():
            w.destroy()
        self._templates_vars.clear()
        self._templates_checkbtns.clear()
        self._templates_dir_atual = templates_dir
        if templates_dir.is_dir():
            subdirs = sorted(
                d for d in templates_dir.iterdir()
                if d.is_dir() and not d.name.startswith(".")
            )
            # Calcula o máximo de linhas por coluna para equilibrar as alturas:
            # usa a mediana dos tamanhos das categorias (mín. 6, máx. 12).
            tamanhos = []
            grupos: list[tuple[str, list]] = []
            for subdir in subdirs:
                arqs = sorted(f for f in subdir.iterdir() if f.is_file() and f.suffix.lower() == ".docx")
                if arqs:
                    tamanhos.append(len(arqs))
                    grupos.append((subdir.name.title(), arqs))
            raiz = sorted(f for f in templates_dir.iterdir() if f.is_file() and f.suffix.lower() == ".docx")
            if raiz:
                tamanhos.append(len(raiz))
                grupos.append(("Outros", raiz))

            if tamanhos:
                mediana = sorted(tamanhos)[len(tamanhos) // 2]
                _MAX_COL = max(6, min(12, mediana))
            else:
                _MAX_COL = 8

            col_idx = 0
            for titulo, arquivos in grupos:
                # Divide em fatias de _MAX_COL itens; cada fatia = 1 coluna.
                fatias = [arquivos[i:i + _MAX_COL] for i in range(0, len(arquivos), _MAX_COL)]
                for fatia_i, fatia in enumerate(fatias):
                    col = ttk.Frame(self._templates_inner)
                    col.grid(row=0, column=col_idx, sticky="nsew", padx=(0, 16))
                    self._templates_inner.columnconfigure(col_idx, weight=1)
                    # Título só na primeira fatia; colunas de continuação ficam
                    # com espaço vazio para manter o alinhamento visual.
                    lbl_txt = titulo if fatia_i == 0 else ""
                    ttk.Label(
                        col, text=lbl_txt,
                        font=_FONT_BOLD, bootstyle="info",
                    ).pack(anchor="w", pady=(0, 2))
                    ttk.Separator(col, orient="horizontal").pack(fill="x", pady=(0, 6))
                    for f in fatia:
                        var = tk.BooleanVar(value=f in marcados)
                        self._templates_vars[f] = var
                        chk = ttk.Checkbutton(
                            col, text=f.stem, variable=var,
                            command=self._on_template_toggle, bootstyle="info",
                        )
                        chk.pack(anchor="w", pady=2)
                        self._templates_checkbtns.append((chk, f.stem))
                    col_idx += 1
            self._templates_ncols = max(1, col_idx)
        self._requebrar_templates()
        self._scan_templates_em_background()
        self._on_template_toggle()

    def _requebrar_templates(self, event=None) -> None:
        """Insere quebras de linha nos textos dos checkbuttons de petição para
        que caibam na largura da coluna (ttk.Checkbutton não suporta wraplength).
        Reavaliado a cada <Configure> da área e ao recarregar os modelos."""
        if not self._templates_checkbtns:
            return
        largura = event.width if event is not None else self._templates_inner.winfo_width()
        if largura <= 1:
            return
        # Largura útil por coluna: desconta o espaçamento entre colunas (padx 16),
        # o indicador do checkbox e uma folga (~34px no total).
        col_px = max(60, int(largura / self._templates_ncols) - 34)
        fonte = tkfont.nametofont("TkDefaultFont")
        for chk, texto in self._templates_checkbtns:
            novo = self._quebrar_para_largura(texto, col_px, fonte)
            if chk.cget("text") != novo:
                chk.configure(text=novo)

    @staticmethod
    def _quebrar_para_largura(texto: str, largura_px: int, fonte: tkfont.Font) -> str:
        """Quebra `texto` em linhas cuja largura renderizada não exceda `largura_px`."""
        if fonte.measure(texto) <= largura_px:
            return texto
        linhas: list[str] = []
        atual = ""
        for palavra in texto.split(" "):
            candidato = f"{atual} {palavra}".strip()
            if atual and fonte.measure(candidato) > largura_px:
                linhas.append(atual)
                atual = palavra
            else:
                atual = candidato
        if atual:
            linhas.append(atual)
        return "\n".join(linhas)

    def _marcar_todos_templates(self) -> None:
        for var in self._templates_vars.values():
            var.set(True)
        self._on_template_toggle()

    def _desmarcar_todos_templates(self) -> None:
        for var in self._templates_vars.values():
            var.set(False)
        self._on_template_toggle()

    def _propagar_grupos_templates(self) -> None:
        """Sincroniza templates combinados com seus individuais.
        Apenas propaga indivíduos → combinado (não o inverso)."""
        for comb_stem, membros_stems in _GRUPOS_TEMPLATE.items():
            comb_path = next(
                (p for p in self._templates_vars if p.stem == comb_stem), None
            )
            if comb_path is None:
                continue
            membros_paths = [
                p for p in self._templates_vars if p.stem in membros_stems
            ]
            if len(membros_paths) != len(membros_stems):
                continue
            comb_var = self._templates_vars[comb_path]
            todos_marcados = all(
                self._templates_vars[mp].get() for mp in membros_paths
            )
            if todos_marcados:
                comb_var.set(True)

    def _scan_templates_em_background(self) -> None:
        if self._scan_tpls_thread and self._scan_tpls_thread.is_alive():
            return
        pendentes = [
            p for p in self._templates_vars
            if p not in self._templates_placeholders
        ]
        if not pendentes:
            return

        # Cache em disco: abrir os 39 modelos (13,5 MB de .docx, um deles com
        # 3,2 MB) custava ~3 s de CPU em TODA abertura, e os modelos mudam uma vez
        # por semestre. Com o cache, a abertura normal não abre nenhum .docx —
        # e por consequência nem carrega o python-docx.
        cache = _carregar_cache_placeholders()
        do_cache, faltando = [], []
        for p in pendentes:
            phs = cache.get(_chave_cache_template(p))
            if phs is None:
                faltando.append(p)
            else:
                self._templates_placeholders[p] = phs
                do_cache.append(p)
        if do_cache:
            self._pesquisa_log_queue.put(
                f"{len(do_cache)} template(s) lidos do cache de placeholders.\n"
            )
        if not faltando:
            self._on_template_toggle()
            _perf(f"templates:cache({len(do_cache)})")
            return

        self._pesquisa_log_queue.put(
            f"Escaneando placeholders de {len(faltando)} template(s)...\n"
        )

        def run() -> None:
            # Import tardio (python-docx + lxml): acontece nesta thread, então a
            # janela já está desenhada e responsiva quando o custo é pago.
            from .peticoes import _extrair_placeholders_template

            try:
                import pythoncom
                pythoncom.CoInitialize()
            except Exception:
                pass
            novos: dict[str, set[str]] = {}
            for p in faltando:
                try:
                    phs = _extrair_placeholders_template(p)
                    novos[_chave_cache_template(p)] = phs
                except Exception as exc:
                    self._pesquisa_log_queue.put(f"! Erro escaneando {p.name}: {exc}\n")
                    phs = set()  # falha não vai para o cache: tenta de novo depois
                self._templates_placeholders[p] = phs
            # UMA reconstrução do formulário no fim, não uma por template: cada
            # _on_template_toggle refaz grid/grid_remove de todos os campos, e
            # eram 39 reconstruções seguidas na abertura.
            self.after(0, self._on_template_toggle)
            if novos:
                _gravar_cache_placeholders({**cache, **novos})
            self._pesquisa_log_queue.put("Escaneamento de templates concluído.\n")
            _perf(f"templates:escaneados({len(faltando)})")

        self._scan_tpls_thread = threading.Thread(target=run, daemon=True)
        self._scan_tpls_thread.start()

    def _on_template_toggle(self) -> None:
        # Propagação de grupo de templates combinados (ex.: Sisbajud+Renajud+Infojud).
        if not self._atualizando_grupo:
            self._atualizando_grupo = True
            try:
                self._propagar_grupos_templates()
            finally:
                self._atualizando_grupo = False

        obrigatorias: set[str] = set()
        desconhecidos: set[str] = set()
        algum_imobiliario = False
        for tmpl, var in self._templates_vars.items():
            if not var.get():
                continue
            if _template_eh_imobiliario(tmpl.name):
                algum_imobiliario = True
            for ph in self._templates_placeholders.get(tmpl, set()):
                if ph in PLACEHOLDERS_PETICAO:
                    obrigatorias.add(PLACEHOLDERS_PETICAO[ph])
                elif ph in PLACEHOLDERS_AUTO:
                    # Resolvido automaticamente — não pede ao usuário.
                    continue
                elif _eh_disjuncao_flexao(ph):
                    # Disjunção de concordância (ex.: "o ou a ou os ou as",
                    # "pela ou pelo ou pelos ou pelas") — o sistema resolve por
                    # gênero+número do polo na geração; não pede ao usuário.
                    continue
                else:
                    desconhecidos.add(ph)
        # Secretaria e Vara são espelhadas: se uma é exigida, mostramos as duas.
        if "secretaria" in obrigatorias or "vara" in obrigatorias:
            obrigatorias.add("secretaria")
            obrigatorias.add("vara")
        self._campos_obrigatorios = obrigatorias
        self._campos_desconhecidos = desconhecidos
        self._templates_tem_imovel = algum_imobiliario
        self._atualizar_destaques_campos()
        self._atualizar_campos_dinamicos()
        self._atualizar_campo_imoveis()

    @staticmethod
    def _pos_edit(vis_idx: int) -> tuple[int, int, int]:
        """Posição (linha, col_rótulo, col_entry) do campo editável de índice
        visível ``vis_idx`` no grid de 2 pares por linha."""
        return vis_idx // 2, (vis_idx % 2) * 2, (vis_idx % 2) * 2 + 1

    def _atualizar_destaques_campos(self) -> None:
        # Recompacta os campos obrigatórios em slots contíguos no grid de 2
        # colunas (sem buracos quando campos não-obrigatórios são ocultados).
        # _ordem_edit garante ordem estável; _n_linhas_visiveis informa
        # _atualizar_campo_imoveis onde posicionar o checkbox.
        vis_idx = 0
        for atr in self._ordem_edit:
            lbl = self._labels_edit.get(atr)
            if lbl is None:
                continue
            rotulo = self._rotulos_edit.get(atr, "")
            entry = self._entries_edit.get(atr)
            if atr in self._campos_obrigatorios:
                row, col_rot, col_ent = self._pos_edit(vis_idx)
                lbl.configure(text=f"* {rotulo}", foreground="#e03131")
                lbl.grid(row=row, column=col_rot, sticky="e", padx=(4, 6), pady=2)
                if entry is not None:
                    entry.grid(row=row, column=col_ent, sticky="ew", padx=(0, 12), pady=2)
                vis_idx += 1
            else:
                lbl.grid_remove()
                if entry is not None:
                    entry.grid_remove()
        self._n_linhas_visiveis = vis_idx

    def _aplicar_estilo_entry(self, atr: str, entry: ttk.Entry, erro: bool) -> None:
        """Borda vermelha (erro) ou estilo normal num campo editável."""
        entry.configure(bootstyle="danger" if erro else "default")
        # bootstyle redefine o estilo: reaplica o cinza dos campos auto-preenchidos.
        if not erro and str(getattr(self, f"var_{atr}")) in self._entries_auto:
            entry.configure(foreground="#868e96")

    def _limpar_destaque_campo(self, atr: str) -> None:
        """Ao digitar, remove o destaque vermelho de um campo obrigatório vazio."""
        entry = self._entries_edit.get(atr)
        var = getattr(self, f"var_{atr}", None)
        if entry is None or var is None:
            return
        if (var.get() or "").strip():
            self._aplicar_estilo_entry(atr, entry, erro=False)

    def _destacar_campos_faltantes(self) -> list[str]:
        """Marca em vermelho os campos editáveis obrigatórios ainda vazios e
        devolve a lista dos que faltam (vazia se tudo preenchido)."""
        faltantes: list[str] = []
        for atr in self._ordem_edit:
            if atr not in self._campos_obrigatorios:
                continue
            entry = self._entries_edit.get(atr)
            var = getattr(self, f"var_{atr}", None)
            if entry is None or var is None:
                continue
            vazio = not (var.get() or "").strip()
            self._aplicar_estilo_entry(atr, entry, erro=vazio)
            if vazio:
                faltantes.append(atr)
        return faltantes

    def _rotulos_campos_faltantes(self) -> list[str]:
        """Rótulos legíveis dos campos obrigatórios vazios (editáveis conhecidos +
        campos adicionais/placeholders novos). Usado para avisar antes de gerar."""
        rotulos: list[str] = []
        for atr in self._destacar_campos_faltantes():
            rotulos.append(self._rotulos_edit.get(atr, atr).rstrip(":"))
        for ph, (_, _, var) in self._dinamicos.items():
            if ph in self._campos_desconhecidos and not (var.get() or "").strip():
                rotulos.append(ph.capitalize())
        return rotulos

    def _atualizar_campo_imoveis(self) -> None:
        if self._frame_imoveis is None:
            return
        if getattr(self, "_templates_tem_imovel", False):
            # Logo abaixo da última linha de campos visíveis, abrangendo as
            # 4 colunas do grid de 2 pares.
            n_vis = getattr(self, "_n_linhas_visiveis", len(self._ordem_edit))
            row = (n_vis + 1) // 2
            self._frame_imoveis.grid(
                row=row, column=0, columnspan=4,
                sticky="w", padx=(4, 4), pady=(4, 1),
            )
        else:
            self._frame_imoveis.grid_remove()

    def _atualizar_campos_dinamicos(self) -> None:
        if self._frame_dinamicos is None:
            return
        for ph in self._campos_desconhecidos:
            if ph in self._dinamicos:
                continue
            var = tk.StringVar()
            rotulo = ph.capitalize() + ":"
            lbl = ttk.Label(
                self._frame_dinamicos, text=f"* {rotulo}",
                anchor="e", width=18, foreground="#e03131",
                font=_FONT_BASE,
            )
            entry = ttk.Entry(self._frame_dinamicos, textvariable=var, width=28, font=_FONT_BASE)
            self._dinamicos[ph] = (lbl, entry, var)

        row = 0
        qualquer_visivel = False
        for ph, (lbl, entry, _) in self._dinamicos.items():
            if ph in self._campos_desconhecidos:
                lbl.grid(row=row, column=0, sticky="e", padx=(4, 6), pady=2)
                entry.grid(row=row, column=1, sticky="w", padx=(0, 4), pady=2)
                qualquer_visivel = True
                row += 1
            else:
                lbl.grid_remove()
                entry.grid_remove()

        empacotado = self._frame_dinamicos.winfo_manager() == "pack"
        if qualquer_visivel and not empacotado:
            # Acima do "Gerar petições" para o botão de gerar ficar sempre por último.
            if self._section_gen is not None:
                self._frame_dinamicos.pack(fill="x", pady=(0, 6), before=self._section_gen)
            else:
                self._frame_dinamicos.pack(fill="x", pady=(0, 6))
        elif not qualquer_visivel and empacotado:
            self._frame_dinamicos.pack_forget()

    def _escolher_pesquisa_destino(self) -> None:
        path = filedialog.askdirectory(initialdir=self.var_pesquisa_destino.get() or ".")
        if path:
            self.var_pesquisa_destino.set(path)
            _salvar_ui_state(pesquisa_destino=path)

    def _construir_proc_manual(self) -> Processo:
        """Monta um Processo a partir dos campos preenchidos à mão, para gerar
        petições sem buscar/selecionar um processo na lista. Os painéis "Dados do
        processo" e "Campos editáveis" já são sempre editáveis; aqui apenas
        consolidamos seus valores num Processo para o restante de _gerar_peticoes
        (que lê proc.classe/assunto/uf, contexto de gênero das partes, etc.)."""
        det = {atr: var.get().strip() for atr, var in self._det_vars.items()}
        proc = Processo(
            numero=det.get("numero", ""),
            tribunal=det.get("tribunal", "") or self.var_tribunal.get().strip(),
            classe=det.get("classe", ""),
            orgao_julgador=det.get("orgao_julgador", ""),
            juiz=det.get("juiz", "") or self.var_juiz.get().strip(),
            valor_causa=det.get("valor_causa", "") or self.var_valor_causa.get().strip(),
            assunto=self.var_assunto.get().strip(),
            comarca=self.var_comarca.get().strip(),
            vara=self.var_vara.get().strip(),
            uf=self.var_estado.get().strip(),
            foro_endereco=self.var_endereco_foro.get().strip(),
        )
        # Partes a partir dos nomes digitados → flexão de gênero/número correta.
        if det.get("autor"):
            proc.partes.append(Parte(papel="AUTOR", nome=det["autor"]))
        if det.get("reu"):
            proc.partes.append(Parte(papel="RÉU", nome=det["reu"]))
        return proc

    def _gerar_peticoes(self) -> None:
        if self._gen_worker and self._gen_worker.is_alive():
            return
        # Sem processo selecionado: monta um Processo a partir dos campos
        # preenchidos à mão (modo manual). Destino e ≥1 modelo continuam exigidos.
        proc = self._proc_selecionado or self._construir_proc_manual()
        destino = Path(self.var_pesquisa_destino.get() or "").expanduser()
        if not destino:
            messagebox.showwarning("Aviso", "Escolha uma pasta de destino para as petições.")
            return
        _salvar_ui_state(pesquisa_destino=str(destino))
        templates_sel = [p for p, v in self._templates_vars.items() if v.get()]
        if not templates_sel:
            messagebox.showwarning("Aviso", "Selecione ao menos um modelo de petição.")
            return

        # Campos obrigatórios vazios (dos modelos selecionados): destaca em
        # vermelho e, se houver algum, pede confirmação antes de gerar — senão a
        # petição sairia com o campo cru no lugar (ex.: o endereço no "Mandado de
        # penhora de bens", que fica em branco quando o campo não é preenchido).
        rotulos_faltantes = self._rotulos_campos_faltantes()
        if rotulos_faltantes:
            lista = "\n".join(f"  • {r}" for r in rotulos_faltantes)
            prosseguir = messagebox.askyesno(
                "Campos obrigatórios vazios",
                "Os campos abaixo são exigidos pelos modelos selecionados e estão "
                f"vazios:\n\n{lista}\n\n"
                "Se prosseguir, o texto correspondente sairá em branco ou com o "
                "marcador cru no lugar.\n\nGerar mesmo assim?",
                icon="warning",
                default="no",
            )
            if not prosseguir:
                self._pesquisa_log_queue.put("Geração cancelada: campos obrigatórios vazios.\n")
                return

        self._pesquisa_log_queue.put(
            f"Gerando {len(templates_sel)} petição(ões) para "
            f"{proc.numero or '(sem número)'} ...\n"
        )

        # Secretaria e Vara são espelhadas — o mesmo valor alimenta ambos placeholders.
        vara_val = _limpar_secretaria(self.var_vara.get() or proc.vara)
        # UF de fallback pelo número CNJ quando o processo não trouxe a UF.
        uf_cnj = proc.uf or _uf_de_numero_cnj(proc.numero)
        # Campos editáveis dos "Dados do processo": número/autor/réu vêm das vars
        # do painel (refletem correções do usuário); demais atributos já foram
        # gravados no proc por _on_detalhe_edit.
        numero_val = self._det_vars["numero"].get().strip() or proc.numero
        dados = {
            "numero": numero_val,
            "classe": proc.classe,
            "assunto": proc.assunto,
            "tribunal": self.var_tribunal.get() or proc.tribunal,
            "uf": proc.uf,
            "comarca": self.var_comarca.get() or proc.comarca,
            "vara": vara_val,
            "juiz": self.var_juiz.get() or proc.juiz,
            "autor": self._det_vars["autor"].get().strip() or self.var_secretaria.get(),
            "reu": self._det_vars["reu"].get().strip(),
            "valor_causa": self.var_valor_causa.get() or proc.valor_causa,
            "secretaria": vara_val,
            "municipio": self.var_municipio.get() or _municipio_de_processo(proc),
            "estado": self.var_estado.get() or uf_cnj,
            "oab_uf": self.var_oab_uf.get() or uf_cnj,
            "oab_numero": self.var_oab_numero.get() or OAB_ESCRITORIO_POR_UF.get(uf_cnj.upper(), ""),
            "oab_completa": _oab_completa(
                self.var_oab_uf.get() or uf_cnj,
                self.var_oab_numero.get() or OAB_ESCRITORIO_POR_UF.get(uf_cnj.upper(), ""),
            ),
            "endereco_reu": self.var_endereco_reu.get(),
            "vara_edit": vara_val,
            "tribunal_edit": self.var_tribunal.get() or proc.tribunal,
            "endereco_foro": self.var_endereco_foro.get() or proc.foro_endereco,
            "juiz_edit": self.var_juiz.get() or proc.juiz,
            "assunto_edit": self.var_assunto.get() or proc.assunto,
        }
        for ph, (_, _, var) in self._dinamicos.items():
            val = var.get().strip()
            if val:
                dados[ph] = val

        # Memoriza o município (digitado ou derivado) para o código de origem do
        # CNJ — processos futuros do mesmo foro auto-preenchem o campo.
        aprender_municipio(proc.numero, dados.get("municipio", ""))

        documento_unico = self.var_documento_unico.get()
        ctx = _construir_contexto(proc)
        multiplos_imoveis = bool(self.var_multiplos_imoveis.get())
        gerar_pdf = self.var_gerar_pdf.get()

        n_templates = len(templates_sel)

        def run() -> None:
            # Import tardio (python-docx + lxml), nesta thread worker.
            from .peticoes import gerar_peticoes

            try:
                gerados = gerar_peticoes(
                    proc,
                    templates_sel,
                    destino,
                    dados,
                    log=lambda msg: self._pesquisa_log_queue.put(msg + "\n"),
                    documento_unico=documento_unico,
                    ctx=ctx,
                    multiplos_imoveis=multiplos_imoveis,
                )
                todos: list[Path] = list(gerados)
                if gerar_pdf:
                    pdfs_gerados, falhas_pdf = _converter_lote_docx_para_pdf(
                        gerados,
                        log=lambda msg: self._pesquisa_log_queue.put(msg + "\n"),
                    )
                    todos.extend(pdfs_gerados)
                    # Antes a falha ficava só nesta linha do log (oculto por
                    # padrão): o .docx aparecia, o PDF não, e ninguém sabia por
                    # quê. Agora avisa de forma visível — foi o sintoma relatado
                    # na máquina fraca ("o PDF não está sendo gerado").
                    if falhas_pdf:
                        self._notificar(
                            "PDF não gerado",
                            f"As petições .docx foram criadas, mas "
                            f"{len(falhas_pdf)} PDF(s) não. Motivo: {falhas_pdf[0]}",
                            nivel="warning",
                        )
                self._pesquisa_log_queue.put("\nConcluído.\n")
                self._gen_result_queue.put(todos)
                self._status_queue.put((
                    "status",
                    f"Geração concluída — {proc.numero or '(sem número)'} "
                    f"({n_templates} modelo(s)).",
                ))
                with contextlib.suppress(Exception):
                    self.bell()
            except Exception as exc:
                self._pesquisa_log_queue.put(f"\nERRO: {exc}\n")
                self._notificar("Erro ao gerar petições", str(exc), nivel="error")

        self._gen_worker = threading.Thread(target=run, daemon=True)
        self._gen_worker.start()

    def _drain_gen_result_queue(self) -> None:
        try:
            while True:
                arquivos = self._gen_result_queue.get_nowait()
                self._mostrar_arquivos_gerados(arquivos)
        except queue.Empty:
            pass

    def _mostrar_arquivos_gerados(self, arquivos: list) -> None:
        frame = self._frame_gerados
        if frame is None:
            return
        for w in frame.winfo_children():
            w.destroy()
        if not arquivos:
            return
        # Guarda a lista completa (docx + pdf) para permitir re-renderizar o
        # painel após reconverter um Word editado para PDF.
        self._ultimos_arquivos_gerados = [Path(a) for a in arquivos]
        word_ok = _word_disponivel()
        # Paths de PDF já presentes — define o rótulo do botão por arquivo
        # (Atualizar vs Gerar) e evita duplicar ao reconverter.
        pdfs_presentes = {
            Path(a).resolve()
            for a in arquivos
            if Path(a).suffix.lower() == ".pdf"
        }
        docs_docx = [Path(a) for a in arquivos if Path(a).suffix.lower() == ".docx"]
        ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=(6, 4))
        ttk.Label(
            frame, text="Arquivos gerados:", font=_FONT_BOLD,
        ).pack(anchor="w", pady=(0, 2))
        for arq in arquivos:
            f_row = ttk.Frame(frame)
            f_row.pack(fill="x", pady=1)
            arq_path = Path(arq)
            is_docx = arq_path.suffix.lower() == ".docx"
            icone = "▢" if arq_path.suffix.lower() == ".pdf" else "◧"
            ttk.Label(
                f_row, text=f"{icone}  {arq_path.name}", font=_FONT_BASE,
                anchor="w",
            ).pack(side="left", fill="x", expand=True)
            ttk.Button(
                f_row, text="Abrir", width=8, bootstyle="outline-info",
                command=lambda a=arq: os.startfile(a) if Path(a).exists() else None,
            ).pack(side="right", padx=(4, 0))
            # Botão por arquivo: (re)converte este .docx para PDF, refletindo
            # edições manuais feitas no Word. Só se o Word estiver disponível.
            if is_docx and word_ok:
                tem_pdf = arq_path.with_suffix(".pdf").resolve() in pdfs_presentes
                ttk.Button(
                    f_row,
                    text="↻ Atualizar PDF" if tem_pdf else "▢ Gerar PDF",
                    width=16,
                    bootstyle="outline-info" if tem_pdf else "outline-secondary",
                    command=lambda d=arq_path: self._reconverter_pdf([d]),
                ).pack(side="right", padx=(4, 0))
        pasta = Path(arquivos[0]).parent if arquivos else None
        if pasta:
            ttk.Button(
                frame, text="▰  Abrir pasta de destino",
                bootstyle="outline-secondary",
                command=lambda p=pasta: os.startfile(p) if p.exists() else None,
            ).pack(fill="x", pady=(4, 0))

        # Botão geral: reconverte todos os .docx da lista de uma vez (após
        # editar um ou mais Words). Só aparece se houver .docx e Word disponível.
        if docs_docx and word_ok:
            ttk.Button(
                frame,
                text="↻  Reconverter Word(s) editado(s) para PDF",
                bootstyle="outline-info",
                command=lambda ds=list(docs_docx): self._reconverter_pdf(ds),
            ).pack(fill="x", pady=(4, 0))

        # Integração Autojur: guarda os PDFs gerados e oferece "Enviar ao Autojur"
        # (1 tarefa de Protocolo com TODOS os PDFs anexados). Só aparece se houver
        # PDF (o protocolo anexa PDF) — se gerou só .docx, orienta a ligar o PDF.
        self._ultimos_pdfs_gerados = [
            Path(a) for a in arquivos if Path(a).suffix.lower() == ".pdf"
        ]
        if self._ultimos_pdfs_gerados:
            ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=(8, 4))
            n = len(self._ultimos_pdfs_gerados)
            ttk.Button(
                frame,
                text=f"▶  Enviar ao Autojur (1 tarefa · {n} anexo{'s' if n > 1 else ''})",
                bootstyle="primary",
                command=self._enviar_peticoes_autojur,
            ).pack(fill="x", pady=(2, 0))

        if frame.winfo_manager() != "pack":
            frame.pack(fill="x", pady=(4, 0))

    def _reconverter_pdf(self, docx_paths: list) -> None:
        """(Re)converte um ou mais .docx para PDF em background, refletindo
        edições manuais feitas no Word, e re-renderiza o painel de resultados.

        A conversão (Word/COM) trava a UI por alguns segundos, então roda numa
        daemon thread; o painel é atualizado via _gen_result_queue (main thread).
        """
        if self._pdf_reconv_worker and self._pdf_reconv_worker.is_alive():
            return
        docs = [Path(d) for d in docx_paths]
        if not docs:
            return
        n = len(docs)
        self._pesquisa_log_queue.put(
            f"Convertendo {n} Word{'s' if n > 1 else ''} para PDF ...\n"
        )
        # Snapshot da lista atual para reconstruir a lista de arquivos ao final.
        base = list(self._ultimos_arquivos_gerados)

        def run() -> None:
            try:
                novos_pdfs, falhas_pdf = _converter_lote_docx_para_pdf(
                    docs,
                    log=lambda msg: self._pesquisa_log_queue.put(msg + "\n"),
                )
                if not novos_pdfs and falhas_pdf:
                    # Nenhum PDF saiu: avisa, em vez de deixar só no log oculto.
                    self._notificar("PDF não gerado", falhas_pdf[0], nivel="warning")
                # Lista atualizada = base ∪ pdfs (re)gerados, sem duplicar.
                # Um docx editado sobrescreve o pdf de mesmo path (já em base);
                # um pdf novo é acrescentado.
                atuais = {Path(a).resolve() for a in base}
                todos = list(base)
                for pdf in novos_pdfs:
                    if pdf.resolve() not in atuais:
                        todos.append(pdf)
                        atuais.add(pdf.resolve())
                self._pesquisa_log_queue.put("\nConcluído.\n")
                self._gen_result_queue.put(todos)
                with contextlib.suppress(Exception):
                    self.bell()
            except Exception as exc:
                self._pesquisa_log_queue.put(f"\nERRO: {exc}\n")
                self._notificar("Erro ao converter para PDF", str(exc), nivel="error")

        self._pdf_reconv_worker = threading.Thread(target=run, daemon=True)
        self._pdf_reconv_worker.start()

    # -- Integração Petições -> Autojur ------------------------------------
    def _numero_processo_atual(self) -> str:
        """Número do processo selecionado (mesma fonte da geração de petições:
        o campo editável 'numero', com fallback ao proc selecionado)."""
        try:
            n = self._det_vars["numero"].get().strip()
        except Exception:
            n = ""
        if not n and self._proc_selecionado is not None:
            n = self._proc_selecionado.numero or ""
        return n

    def _enviar_peticoes_autojur(self) -> None:
        """Abre o modal de confirmação para enviar os PDFs gerados ao Autojur
        (uma tarefa de Protocolo por PDF). O login é feito sob demanda."""
        if self._envio_autojur_worker is not None and self._envio_autojur_worker.is_alive():
            messagebox.showinfo("Autojur", "Já há um envio em andamento.")
            return
        pdfs = list(self._ultimos_pdfs_gerados)
        if not pdfs:
            messagebox.showwarning("Autojur", "Gere ao menos uma petição em PDF antes de enviar.")
            return
        numero = self._numero_processo_atual()
        if not numero:
            messagebox.showwarning(
                "Autojur", "Selecione um processo (ou preencha o número) antes de enviar."
            )
            return
        self._modal_enviar_autojur(pdfs, numero)

    @staticmethod
    def _renomear_pdf_para_envio(caminho_original: str, nome_desejado: str) -> str:
        """Devolve o caminho a enviar ao Autojur com o nome escolhido pelo
        usuário. Se o nome não mudou, devolve o caminho original sem tocar
        nele; se mudou, cria uma CÓPIA com o novo nome numa pasta temporária
        — o arquivo original (ex.: já indexado em outras abas) nunca é
        renomeado nem movido."""
        import re as _re
        import shutil
        import tempfile

        original = Path(caminho_original)
        nome = nome_desejado.strip()
        if _re.search(r'[<>:"/\\|?*\x00-\x1f]', nome):
            raise OSError(f"Nome de arquivo inválido: {nome_desejado!r}")
        if not nome.lower().endswith(".pdf"):
            nome += ".pdf"
        if nome == original.name:
            return str(original)
        pasta_tmp = Path(tempfile.mkdtemp(prefix="autojur_envio_"))
        destino = pasta_tmp / nome
        shutil.copy2(original, destino)
        return str(destino)

    def _modal_enviar_autojur(self, pdfs: list, numero: str) -> None:
        """Modal: confirma dados e dispara o envio ao Autojur (1 tarefa por PDF)."""
        top = tk.Toplevel(self)
        top.title("Enviar ao Autojur")
        top.transient(self)
        top.grab_set()
        top.geometry("640x600")

        frm = ttk.Frame(top, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Nº processo:", font=_FONT_BASE).grid(
            row=0, column=0, sticky="e", padx=(0, 6), pady=4)
        var_num = tk.StringVar(value=numero)
        ttk.Entry(frm, textvariable=var_num, font=_FONT_BASE).grid(
            row=0, column=1, sticky="ew", pady=4)

        ttk.Label(frm, text="Local:", font=_FONT_BASE).grid(
            row=1, column=0, sticky="e", padx=(0, 6), pady=4)
        # Pré-preenche o Local com o tribunal do processo (ex.: "TJMG"); editável.
        tribunal = self.var_tribunal.get().strip()
        if not tribunal and self._proc_selecionado is not None:
            tribunal = self._proc_selecionado.tribunal or ""
        var_local = tk.StringVar(value=tribunal)
        ttk.Entry(frm, textvariable=var_local, font=_FONT_BASE).grid(
            row=1, column=1, sticky="ew", pady=4)

        ttk.Label(frm, text="Lote:", font=_FONT_BASE).grid(
            row=2, column=0, sticky="e", padx=(0, 6), pady=4)
        var_lote = tk.StringVar(value="dia seguinte")
        ttk.Combobox(
            frm, textvariable=var_lote,
            values=["iniciais", "urgente", "dia seguinte"],
            state="readonly", font=_FONT_BASE,
        ).grid(row=2, column=1, sticky="w", pady=4)

        ttk.Label(frm, text="Conteúdo:", font=_FONT_BASE).grid(
            row=3, column=0, sticky="ne", padx=(0, 6), pady=4)
        txt_cont = tk.Text(frm, height=3, wrap="word", font=_FONT_BASE,
                           relief="flat", borderwidth=1, highlightthickness=1,
                           highlightcolor="#d0d5dd", highlightbackground="#d0d5dd")
        txt_cont.insert("1.0", "Protocolo de peça processual")
        txt_cont.grid(row=3, column=1, sticky="ew", pady=4)

        # Cabeçalho da lista (contador dinâmico + botão "Adicionar") numa
        # sub-frame própria com pack: um Label sem columnspan e com texto
        # longo força a coluna 0 do grid de `frm` a ficar larga demais e
        # empurra as outras linhas (Nº processo, Local...) para fora da
        # janela — por isso o cabeçalho fica isolado do grid principal.
        frm_header = ttk.Frame(frm)
        frm_header.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 2))
        lbl_lista = ttk.Label(frm_header, font=_FONT_BOLD, wraplength=460)
        lbl_lista.pack(side="left", fill="x", expand=True)
        ttk.Button(
            frm_header, text="⊕  Adicionar arquivo", command=lambda: _adicionar_arquivo(),
            bootstyle="outline-primary", width=20,
        ).pack(side="right")

        frm_pdfs = ttk.Frame(frm)
        frm_pdfs.grid(row=5, column=0, columnspan=2, sticky="ew")

        # Fonte da verdade da lista (mutável): [ (BooleanVar marcado?, caminho
        # original, StringVar com o nome — editável, PRÉ-NOMEADO na convenção do
        # protocolo: "1. Petição - <modelo>", "2. <arquivo>", ...). O nome
        # editado só afeta a CÓPIA enviada ao Autojur; o arquivo original em
        # disco nunca é tocado.
        vars_pdf: list = [
            (
                tk.BooleanVar(value=True),
                pdf,
                tk.StringVar(value=_prenomear_anexo_autojur(i, pdf, peticao=True)),
            )
            for i, pdf in enumerate(pdfs, 1)
        ]

        def _render_pdfs() -> None:
            for w in frm_pdfs.winfo_children():
                w.destroy()
            frm_pdfs.columnconfigure(1, weight=1)
            for i, (v, pdf, nome_var) in enumerate(vars_pdf):
                ttk.Checkbutton(
                    frm_pdfs, text="▢", variable=v, bootstyle="info",
                ).grid(row=i, column=0, sticky="w", pady=1)
                ttk.Entry(
                    frm_pdfs, textvariable=nome_var, font=_FONT_BASE,
                ).grid(row=i, column=1, sticky="ew", padx=(4, 0), pady=1)
            lbl_lista.configure(
                text=f"Arquivos a anexar ({len(vars_pdf)} — todos em UMA tarefa). "
                     "Nome editável — a numeração é reaplicada na ordem de envio:"
            )

        def _adicionar_arquivo() -> None:
            caminhos = filedialog.askopenfilenames(
                parent=top, title="Adicionar arquivo(s) ao envio",
                filetypes=[("PDF", "*.pdf"), ("Todos", "*.*")],
            )
            existentes = {str(p) for _, p, _ in vars_pdf}
            novos = 0
            for c in caminhos:
                if c and str(c) not in existentes:
                    # Continua a sequência (não é petição gerada: fica só
                    # "<n>. <nome do arquivo>", sem o prefixo "Petição - ").
                    vars_pdf.append((
                        tk.BooleanVar(value=True),
                        c,
                        tk.StringVar(
                            value=_prenomear_anexo_autojur(len(vars_pdf) + 1, c)
                        ),
                    ))
                    existentes.add(str(c))
                    novos += 1
            if novos:
                _render_pdfs()

        _render_pdfs()

        base = 6
        # Default = preferência global (Configurações). No teste fica desligado;
        # em produção, ligado. A pessoa ainda pode ajustar pontualmente aqui.
        var_salvar = tk.BooleanVar(value=bool(self.var_autojur_salvar_prod.get()))
        ttk.Checkbutton(
            frm,
            text="Salvar automaticamente no Autojur (senão só preenche p/ conferência)",
            variable=var_salvar, bootstyle="warning-round-toggle",
        ).grid(row=base, column=0, columnspan=2, sticky="w", pady=(10, 0))
        # Visualizar o navegador (default = preferência global). Com "salvar"
        # desligado o navegador já é sempre visível, independentemente daqui.
        var_visualizar = tk.BooleanVar(value=bool(self.var_autojur_visualizar.get()))
        ttk.Checkbutton(
            frm,
            text="Visualizar navegador (mostra o Chrome mesmo salvando)",
            variable=var_visualizar, bootstyle="info-round-toggle",
        ).grid(row=base + 1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        f_b = ttk.Frame(top)
        f_b.pack(fill="x", padx=12, pady=12)

        def enviar() -> None:
            marcados = [(pdf, nome_var.get().strip()) for v, pdf, nome_var in vars_pdf if v.get()]
            if not marcados:
                messagebox.showwarning("Autojur", "Marque ao menos um PDF.", parent=top)
                return
            for _, nome in marcados:
                if not _RE_NUM_PREFIXO.sub("", nome).strip():
                    messagebox.showwarning(
                        "Autojur", "O nome do arquivo não pode ficar vazio.", parent=top,
                    )
                    return
            # Renumera 1..N só sobre os MARCADOS: desmarcar o primeiro deixaria
            # a sequência começando em "2." no Autojur.
            nomes = _renumerar_anexos_autojur([nome for _, nome in marcados])
            try:
                selecionados = [
                    self._renomear_pdf_para_envio(pdf, nome)
                    for (pdf, _), nome in zip(marcados, nomes)
                ]
            except OSError as exc:
                messagebox.showerror("Autojur", f"Falha ao preparar arquivo renomeado: {exc}", parent=top)
                return
            dados = {
                "numero": var_num.get().strip(),
                "local": var_local.get().strip(),
                "lote": var_lote.get().strip().lower().replace(" ", "_"),
                "conteudo": txt_cont.get("1.0", "end").strip(),
                "salvar": bool(var_salvar.get()),
                "visualizar": bool(var_visualizar.get()),
                "pdfs": selecionados,
            }
            if not dados["numero"]:
                messagebox.showwarning("Autojur", "Informe o número do processo.", parent=top)
                return
            if not dados["conteudo"]:
                messagebox.showwarning("Autojur", "O campo Conteúdo é obrigatório.", parent=top)
                return
            top.destroy()
            self._disparar_envio_autojur(dados)

        ttk.Button(f_b, text="▶  Enviar ao Autojur", command=enviar,
                   bootstyle="success", width=20).pack(side="right")
        ttk.Button(f_b, text="Cancelar", command=top.destroy,
                   bootstyle="secondary-outline", width=12).pack(side="right", padx=(0, 8))

    @staticmethod
    def _sessao_autojur_viva(scraper) -> bool:
        """A sessão retida ainda está viva? (navegador aberto e conectado).
        Uma chamada barata ao driver levanta se a sessão morreu — 'invalid
        session id' / 'not connected to DevTools' quando o Chrome foi fechado
        por fora, crashou ou teve o processo morto."""
        try:
            _ = scraper.driver.window_handles
            return True
        except Exception:
            return False

    def _marcar_uso_autojur(self) -> None:
        """Zera o relógio de ociosidade da sessão do Autojur.

        Chamado de workers também: é só a escrita de um float (atômica sob o
        GIL), não precisa de lock."""
        self._autojur_ultimo_uso = time.monotonic()

    def _fechar_autojur_ocioso(self) -> None:
        """Encerra a sessão OCULTA do Autojur parada há muito tempo.

        POR QUE SÓ A OCULTA
        -------------------
        A sessão é retida de propósito, para reaproveitar o login. O problema é
        quando ela é headless: não tem janela nem ícone na barra, então o usuário
        termina o envio, acha que fechou tudo, e ficam 300-500 MB ocupados até o
        app fechar. Reabrir custa segundos (o perfil persistente dispensa novo
        2FA), então segurar a sessão indefinidamente é o pior dos dois lados.

        Uma sessão VISÍVEL nunca é fechada aqui: ela está na tela do usuário — é
        o modo de conferência do envio sem salvar, e a tarefa em revisão ainda
        não foi gravada. Fechá-la por baixo dele apagaria esse trabalho.
        """
        if self._autojur_scraper is None or self._autojur_scraper_headless is not True:
            return
        # Qualquer worker do Autojur vivo = sessão em uso (o envio pode passar
        # minutos numa tela sem tocar no scraper).
        for w in (self._autojur_worker, self._autojur_tarefa_worker,
                  self._autojur_prelogin_worker, self._envio_autojur_worker):
            if w is not None and w.is_alive():
                self._marcar_uso_autojur()
                return
        if (time.monotonic() - self._autojur_ultimo_uso) < self._autojur_ocioso_s:
            return
        # Não pode correr com _garantir_autojur_logado (que roda no worker e pode
        # estar recriando o navegador agora). Se o lock está tomado, a sessão está
        # em uso: fica para a próxima checagem.
        if not self._autojur_login_lock.acquire(blocking=False):
            self._marcar_uso_autojur()
            return
        try:
            scraper = self._autojur_scraper
            self._autojur_scraper = None
            self._autojur_scraper_headless = None
        finally:
            self._autojur_login_lock.release()
        self._pesquisa_log_queue.put(
            f"Autojur: navegador oculto encerrado após {int(self._autojur_ocioso_s // 60)} "
            "min sem uso (a sessão volta sozinha no próximo envio — o perfil "
            "persistente dispensa novo 2FA).\n"
        )
        # Os botões só existem se a aba Autojur (dev) foi construída.
        with contextlib.suppress(Exception):
            self._btn_autojur_fechar.configure(state="disabled")
            self._btn_autojur_tarefa.configure(state="disabled")
            self._btn_autojur_abrir.configure(state="normal")
        self._fechar_em_thread(scraper)

    def _garantir_autojur_logado(self, headless: bool = True):
        """Retorna o scraper logado; se não houver sessão (ou ela morreu), faz
        login sob demanda. Roda no WORKER (login é bloqueante — 2FA por e-mail
        só na 1ª vez, pois o perfil persistente dispensa o 2FA nas seguintes).
        `headless`: visibilidade desejada. Se já há uma sessão com visibilidade
        diferente (ou morta), o navegador é recriado (barato com perfil
        persistente)."""
        from .config import AUTOJUR_PROFILE_DIR
        # Serializa criação/relogin (pré-login x envio não podem correr juntos).
        with self._autojur_login_lock:
            if self._autojur_scraper is not None:
                if (self._autojur_scraper_headless == headless
                        and self._sessao_autojur_viva(self._autojur_scraper)):
                    self._marcar_uso_autojur()
                    return self._autojur_scraper
                # Visibilidade mudou OU a sessão morreu (navegador fechado por
                # fora / crash): fecha (se der) e recria — barato com o perfil
                # persistente (sem novo 2FA).
                self._pesquisa_log_queue.put(
                    "Autojur: sessão indisponível — reabrindo o navegador...\n"
                )
                with contextlib.suppress(Exception):
                    self._autojur_scraper.fechar()
                self._autojur_scraper = None
                self._autojur_scraper_headless = None
            self._pesquisa_log_queue.put(
                "Autojur: sem sessão — logando (2FA por e-mail só na 1ª vez)...\n"
            )
            from dotenv import load_dotenv
            from .config import CONFIG_ENV_PATH
            from .scraper import _preparar_env_local
            from .integracoes.tjsp_scraper.scrapers.autojur import AutojurScraper
            _preparar_env_local()
            load_dotenv(CONFIG_ENV_PATH)
            AUTOJUR_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            scraper = AutojurScraper(
                headless=headless, user_data_dir=str(AUTOJUR_PROFILE_DIR),
            )
            try:
                scraper.login()
            except Exception:
                scraper.fechar()
                raise
            self._autojur_scraper = scraper
            self._autojur_scraper_headless = headless
            self._marcar_uso_autojur()
            # habilita os controles da aba Autojur (na main thread)
            self.after(0, lambda: (
                self._btn_autojur_fechar.configure(state="normal"),
                self._btn_autojur_tarefa.configure(state="normal"),
                self._btn_autojur_abrir.configure(state="disabled"),
            ))
            return scraper

    def _iniciar_prelogin_autojur(self) -> None:
        """Aquece a sessão do Autojur em 2º plano (worker daemon), sem travar a
        GUI. Reutiliza _garantir_autojur_logado (idempotente + lock).

        Sobe SEMPRE OCULTO (headless): a sessão fica pronta em background. Só
        quando o envio precisar de conferência (salvar=False / Visualizar
        navegador) é que o navegador é recriado visível — em segundos, pois o
        perfil persistente dispensa novo 2FA."""
        w = self._autojur_prelogin_worker
        if w is not None and w.is_alive():
            return
        if self._autojur_scraper is not None:
            return
        headless = True  # pré-login sempre oculto; vira visível sob demanda

        def run() -> None:
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except Exception:
                pass
            writer = _LogWriter(self._pesquisa_log_queue.put)
            try:
                with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                    self._garantir_autojur_logado(headless=headless)
                self._pesquisa_log_queue.put("✅ Autojur: sessão pré-logada e pronta.\n")
            except Exception as exc:
                self._pesquisa_log_queue.put(f"Autojur: pré-login falhou: {exc}\n")

        self._autojur_prelogin_worker = threading.Thread(target=run, daemon=True)
        self._autojur_prelogin_worker.start()

    def _disparar_envio_autojur(self, dados: dict) -> None:
        """Worker: garante login e cria uma tarefa de Protocolo por PDF."""
        self.progress_pesquisa.start(_PROGRESS_MS)
        self.var_status.set("Autojur: enviando petições...")
        # Visibilidade pela regra única: visível quando NÃO salvar (conferência),
        # quando "Visualizar navegador" está ligado ou no modo dev; senão oculto.
        # Lido aqui (main thread) — Tkinter não é thread-safe.
        headless = self._autojur_headless(
            salvar=bool(dados.get("salvar")), visualizar=dados.get("visualizar"),
        )

        def run() -> None:
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except Exception:
                pass
            writer = _LogWriter(self._pesquisa_log_queue.put)
            try:
                with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                    scraper = self._garantir_autojur_logado(headless=headless)
                    pdfs = dados["pdfs"]
                    total = len(pdfs)
                    # UMA tarefa com TODOS os anexos (não uma tarefa por PDF).
                    self._pesquisa_log_queue.put(
                        f"Autojur: criando 1 tarefa com {total} anexo(s)...\n"
                    )
                    scraper.criar_tarefa_protocolo(
                        numero_processo=dados["numero"],
                        conteudo=dados["conteudo"],
                        local=dados["local"],
                        lote=dados["lote"],
                        pdf_paths=[str(p) for p in pdfs],
                        requer_protocolo=True,
                        salvar=dados["salvar"],
                        escolher_resultado=self._autojur_escolher_resultado,
                        log=self._pesquisa_log_queue.put,
                    )
                self._envio_autojur_result_queue.put(("ok", (total, dados["salvar"])))
            except Exception as exc:
                self._envio_autojur_result_queue.put(("erro", _descrever_erro(exc)))

        self._envio_autojur_worker = threading.Thread(target=run, daemon=True)
        self._envio_autojur_worker.start()

    def _drain_envio_autojur_result(self) -> None:
        """Drena o resultado do envio de petições ao Autojur na main thread."""
        try:
            while True:
                tipo, payload = self._envio_autojur_result_queue.get_nowait()
                self.progress_pesquisa.stop()
                if tipo == "ok":
                    total, salvou = payload
                    estado_tarefa = "salva" if salvou else "preenchida (não salva)"
                    msg = (f"1 tarefa {estado_tarefa} no Autojur "
                           f"com {total} anexo(s).")
                    self.var_status.set(f"Autojur: {msg}")
                    # O navegador fica ABERTO para conferência — só fecha pelo
                    # botão 'Fechar navegador' da aba Autojur.
                    self._pesquisa_log_queue.put(
                        f"✅ {msg} Navegador deixado ABERTO para validação "
                        "(feche pela aba Autojur quando terminar).\n"
                    )
                    self._notificar("Autojur", msg + " Navegador aberto para conferência.",
                                    nivel="info")
                else:
                    self.var_status.set("Autojur: falha no envio.")
                    self._pesquisa_log_queue.put(f"! Erro no envio ao Autojur: {payload}\n")
                    self._notificar("Autojur", str(payload), nivel="error")
        except queue.Empty:
            pass

    def _drain_pesquisa_log_queue(self) -> None:
        mensagens: list[str] = []
        try:
            while True:
                mensagens.append(self._pesquisa_log_queue.get_nowait())
        except queue.Empty:
            pass
        self._append_log(self.txt_pesquisa, mensagens)

    def _toggle_log_pesq(self) -> None:
        """Mostra/oculta o log da aba Pesquisa de Processo."""
        if self._log_pesq_visivel:
            self._section_log_pesq.pack_forget()
            self._log_pesq_visivel = False
            self.btn_detalhes_pesq.configure(text="Ver detalhes ▾")
        else:
            self._section_log_pesq.pack(fill="x")
            self._log_pesq_visivel = True
            self.btn_detalhes_pesq.configure(text="Ocultar detalhes ▴")

    def _atualizar_progresso_pesquisa(self) -> None:
        """Anima a barra de progresso enquanto houver trabalho em segundo plano.

        A checagem roda na main thread (Tkinter não é thread-safe), inspecionando
        as threads worker da aba Pesquisa: busca online, geração de petições e
        escaneamento de templates. A barra é indeterminada — só sinaliza que o
        sistema está rodando, sem percentual."""
        threads = (
            self._busca_worker,
            self._gen_worker,
            self._scan_tpls_thread,
            self._pesquisa_load_worker,
        )
        # Caminho ocioso (o normal): nenhuma thread criada ainda, nada a fazer —
        # sem is_alive(), sem tocar no widget.
        if not self._pesquisa_em_progresso and not any(t is not None for t in threads):
            return
        ativo = any(t is not None and t.is_alive() for t in threads)
        if ativo and not self._pesquisa_em_progresso:
            self._pesquisa_em_progresso = True
            self.progress_pesquisa.start(_PROGRESS_MS)
        elif not ativo and self._pesquisa_em_progresso:
            self._pesquisa_em_progresso = False
            self.progress_pesquisa.stop()

    def _verificar_atualizacao_silenciosa(self) -> None:
        """Checa atualização em silêncio na abertura do app.

        Só mostra a janela se houver versão nova; se já estiver atualizado ou
        houver qualquer erro/sem internet, sai calado (ver atualizacao.py).
        Best-effort: qualquer falha é ignorada.
        """
        with contextlib.suppress(Exception):
            _atualizacao().verificar(self, auto=True)

    def _verificar_atualizacao_manual(self) -> None:
        """Clique no botão "Verificar atualização": sempre mostra o resultado,
        inclusive quando já está na versão mais recente."""
        try:
            _atualizacao().verificar(self, auto=False)
        except Exception as exc:
            # Sem este try, uma falha ao abrir a janela sobe para o
            # report_callback_exception do Tk, que imprime em stderr —
            # inexistente no exe --windowed. O clique não fazia NADA: nem janela,
            # nem erro, nem segunda linha no updater.log.
            messagebox.showerror(
                "Verificar atualização",
                f"Não consegui abrir a verificação de atualização:\n\n{exc}\n\n"
                "O detalhe foi gravado em updater.log.",
            )

    # -- atualizar senhas dos tribunais ------------------------------------
    def _atualizar_senhas_dos_tribunais(self) -> None:
        """Clique em "Atualizar senhas dos tribunais" (aba Configurações).

        Vai à PLANILHA de senhas do escritório (ver `organizador.sheets`), então
        roda numa thread: uma planilha leva segundos, e o import a frio do
        google-auth num .exe onefile leva mais ainda — na main thread isso
        congelaria a janela inteira. O worker só LÊ e PLANEJA; quem grava é o
        dreno, depois do usuário confirmar vendo quais tribunais mudam.
        """
        if self._senhas_worker and self._senhas_worker.is_alive():
            return  # clique repetido não abre uma segunda ida à rede
        with contextlib.suppress(Exception):
            self._btn_atualizar_senhas.configure(state="disabled")
        self.var_status.set("Conferindo a planilha de senhas do escritório...")

        def run() -> None:
            # Sem pythoncom.CoInitialize() de propósito: aqui não há COM nem
            # selenium, só HTTP (diferente do worker da busca online).
            linhas: list[str] = []
            try:
                from .config import planejar_credenciais_da_planilha

                diff, final = planejar_credenciais_da_planilha(log=linhas.append)
                self._senhas_result_queue.put(("ok", diff, final))
            except Exception as exc:
                from . import sheets

                self._senhas_result_queue.put(
                    ("erro", sheets.explicar_falha_de_acesso(str(exc)), str(exc)[:300])
                )

        self._senhas_worker = threading.Thread(target=run, daemon=True)
        self._senhas_worker.start()

    def _drain_senhas_result(self) -> None:
        """Resultado da conferência da planilha (main thread).

        Tira o item da fila ANTES de abrir qualquer diálogo: `messagebox` roda um
        laço de eventos aninhado e o `_pump` continua batendo — com o item ainda
        na fila, o dreno reentrante abriria uma segunda janela igual.
        """
        try:
            item = self._senhas_result_queue.get_nowait()
        except queue.Empty:
            return
        try:
            self._mostrar_resultado_senhas(item)
        finally:
            with contextlib.suppress(Exception):
                self._btn_atualizar_senhas.configure(state="normal")

    def _mostrar_resultado_senhas(self, item: tuple) -> None:
        estado = item[0]
        if estado == "erro":
            _e, amigavel, detalhe = item
            self.var_status.set("Não consegui conferir a planilha de senhas.")
            messagebox.showerror(
                "Atualizar senhas",
                f"{amigavel}\n\nNada foi alterado nesta máquina.\n\n"
                f"Detalhe técnico: {detalhe}",
            )
            return

        _e, diff, final = item
        if not any(diff.values()):
            self.var_status.set("Senhas dos tribunais já em dia.")
            messagebox.showinfo(
                "Atualizar senhas",
                "As senhas desta máquina já são iguais às da planilha do "
                "escritório.\n\nNada foi alterado.",
            )
            return

        def _bloco(titulo: str, rotulos: list[str]) -> str:
            if not rotulos:
                return ""
            mostra = rotulos[:12]
            resto = len(rotulos) - len(mostra)
            corpo = "\n".join(f"  • {r}" for r in mostra)
            if resto:
                corpo += f"\n  • (+{resto} outro(s))"
            return f"{titulo}\n{corpo}\n\n"

        detalhe = (
            _bloco("Senha/login MUDA em:", diff["alteradas"])
            + _bloco("Passa a existir:", diff["novas"])
            + _bloco("Fica só nesta máquina (não será apagado):", diff["so_local"])
        )
        if not (diff["alteradas"] or diff["novas"]):
            # Só "so_local": não há o que gravar, mas o usuário merece saber que
            # aqueles acessos não estão na planilha do escritório.
            self.var_status.set("Senhas dos tribunais já em dia.")
            messagebox.showinfo(
                "Atualizar senhas",
                f"{detalhe}Nenhuma senha da planilha é diferente das desta "
                "máquina. Nada foi alterado.",
            )
            return

        if not messagebox.askyesno(
            "Atualizar senhas dos tribunais",
            f"{detalhe}"
            "Se você editou alguma senha NESTE computador, ela será substituída "
            "pela da planilha. O arquivo atual fica guardado como "
            "credenciais.json.bak, ao lado dele.\n\nContinuar?",
            icon="warning", default="no",
        ):
            self.var_status.set("Atualização de senhas cancelada.")
            return

        try:
            from .config import aplicar_credenciais

            backup = aplicar_credenciais(final)
        except OSError as exc:
            messagebox.showerror(
                "Atualizar senhas",
                f"Não consegui gravar as senhas novas:\n\n{exc}\n\n"
                "Nada foi alterado — feche outros programas que possam estar com "
                "o arquivo aberto e tente de novo.",
            )
            return

        self.var_status.set("Senhas dos tribunais atualizadas.")
        messagebox.showinfo(
            "Atualizar senhas",
            "Senhas atualizadas nesta máquina.\n\n"
            + (f"Cópia do anterior: {backup}\n\n" if backup else "")
            + "Se alguma busca online já estava aberta, feche e refaça para ela "
              "usar a senha nova.",
        )

    # -- restaurar dados do programa ---------------------------------------
    def _restaurar_dados_locais(self) -> None:
        """Clique em "Restaurar dados do programa" (aba Configurações).

        Confirma DIZENDO quantos processos somem e o que NÃO é tocado, apaga
        numa thread (são milhares de pastinhas — numa máquina fraca isso trava
        a janela por segundos) e recarrega a lista da Pesquisa no fim, para o
        resultado aparecer sem precisar reabrir o app."""
        n = _contar_processos_locais()
        perdidos = (
            f"{n} processo(s) trazidos por busca online NESTE computador serão "
            "apagados (dá para buscá-los de novo).\n\n" if n else ""
        )
        itens = "\n".join(f"  • {rotulo}" for _c, rotulo in _itens_reset())
        if not messagebox.askyesno(
            "Restaurar dados do programa",
            f"{perdidos}"
            f"Será apagado desta máquina:\n{itens}\n\n"
            "NÃO serão tocados: suas senhas e configurações, os processos da "
            "pasta base e os processos que já vêm dentro do programa — estes "
            "voltam a aparecer na lista.\n\n"
            "Continuar?",
            icon="warning", default="no",
        ):
            return

        self._btn_reset_dados.configure(state="disabled")
        self.var_status.set("Restaurando dados do programa...")

        def run() -> None:
            apagados, falhas = _resetar_dados_locais()
            orfaos = _limpar_temp_orfaos()
            # Tkinter só é seguro na main thread (mesmo padrão de
            # _reter_debug_scraper).
            self.after(0, lambda: self._fim_restaurar_dados(apagados, falhas, orfaos))

        threading.Thread(target=run, daemon=True).start()

    def _fim_restaurar_dados(
        self, apagados: list[str], falhas: list[str], orfaos: int,
    ) -> None:
        """Fecha o ciclo do reset, na main thread."""
        with contextlib.suppress(Exception):
            self._btn_reset_dados.configure(state="normal")
        _atualizacao().registrar(
            f"reset de dados locais: apagados={apagados} falhas={falhas} "
            f"_MEI_removidos={orfaos}"
        )
        # Recarrega a lista: sem isto a tela continua mostrando em memória
        # exatamente os processos que acabaram de ser apagados do disco. O
        # próprio recarregamento limpa o cache de Processos completos.
        self._iniciar_carregamento_pesquisa(silencioso=False)
        if falhas:
            self._notificar(
                "Restauração incompleta",
                "Não consegui apagar:\n"
                + "\n".join(f"  • {r}" for r in falhas)
                + "\n\nFeche TODAS as janelas do Organizador de Processos e "
                  "tente de novo.",
                nivel="warning",
            )
        elif apagados:
            self._notificar(
                "Dados restaurados",
                "Pronto. A lista voltou a mostrar só os processos que vêm "
                "dentro do programa.",
            )
        else:
            self._notificar(
                "Nada a restaurar",
                "Esta máquina não tinha dados guardados — a lista já é a que "
                "vem dentro do programa.",
            )


# Extensões cuja IMAGEM o Windows trava enquanto um processo as tem carregadas.
_EXT_BINARIAS = (".dll", ".pyd", ".exe")

# Idade mínima para uma pasta _MEI ser considerada órfã.
#
# A sonda de travamento (_mei_em_uso) NÃO cobre uma instância que está abrindo
# AGORA: o bootloader do onefile extrai os ~98 MB inteiros ANTES de carregar o
# python3xx.dll e iniciar o Python, e durante a extração nenhum binário está
# travado. Uma instância que abre nessa janela vê a pasta da outra como órfã e
# faz rmtree no meio da extração dela. O resultado é uma extração mutilada, com
# os binários que ainda não haviam sido escritos ou já apagados — e o app morre
# depois, longe daqui, num ImportError de .pyd (visto ao vivo em 14/08/2026:
# `cannot import name '_imagingtk' from 'PIL'`, com 96 GB de disco livre).
#
# Extrair 98 MB leva SEGUNDOS numa máquina fraca, então a janela é larga — e é
# por isso que o problema só aparecia nas máquinas lentas. Uma pasta que ainda
# está recebendo bytes tem mtime de agora; órfã de verdade é sempre velha.
_EXTRACAO_S = 300  # 5 min


def _mei_em_uso(caminho: str) -> bool:
    """True se algum binário de `caminho` ainda está carregado por um processo.

    POR QUE ESTA SONDA EXISTE
    -------------------------
    A versão anterior confiava em ``rmtree(..., ignore_errors=True)`` "falhar"
    numa pasta em uso. Ele NÃO falha: apaga tudo que não está travado e só
    preserva as DLLs carregadas. Medido aqui — de um ``_MEI`` de instância viva
    sobrou só a DLL; ``Petições/``, ``data/processos_base.sqlite`` e ``config/``
    foram apagados. Como esses arquivos ficam em ``_MEIPASS``, a instância aberta
    perdia os modelos (nada para preencher) e a base embutida (os processos
    pré-carregados sumiam da lista) sem uma única mensagem de erro.

    O Windows trava a imagem de um .dll/.pyd CARREGADO: abrir para escrita dá
    PermissionError. É o sinal confiável. Renomear não serve de sonda — o
    Windows permite renomear tanto a pasta quanto a própria DLL carregada (é
    justamente disso que a auto-atualização depende, ver atualizacao.py).
    """
    for raiz_dir, _dirs, arquivos in os.walk(caminho):
        for nome in arquivos:
            if not nome.lower().endswith(_EXT_BINARIAS):
                continue
            try:
                with open(os.path.join(raiz_dir, nome), "r+b"):
                    pass
            except PermissionError:
                # Travado. Na dúvida (ex.: arquivo só-leitura por outro motivo)
                # o erro é para o lado seguro: sobra lixo, não some dado vivo.
                return True
            except OSError:
                continue      # sumiu no meio do caminho: não conclui nada
    return False


def _mei_recem_escrito(caminho: str, segundos: int = _EXTRACAO_S) -> bool:
    """True se `caminho` recebeu bytes nos últimos `segundos` (extração em curso).

    Olha a própria pasta e os filhos diretos — é ali que o onefile escreve as
    DLLs e os .pyd, então o mtime mais novo desse nível acompanha a extração sem
    custo de percorrer a árvore toda. Na dúvida devolve True: sobra lixo, que é
    barato, em vez de mutilar a extração de uma instância que está abrindo.
    """
    try:
        mais_novo = os.stat(caminho).st_mtime
        with os.scandir(caminho) as entradas:
            for entrada in entradas:
                try:
                    mais_novo = max(mais_novo, entrada.stat().st_mtime)
                except OSError:
                    continue
    except OSError:
        return True
    return (time.time() - mais_novo) < segundos


def _iniciar_varredura_navegadores() -> None:
    """Varre navegadores órfãos em 2º plano (ver organizador/navegadores.py).

    Em thread porque sobe um PowerShell (0,9 s aqui, 1-3 s num i3) e a janela já
    está na tela. Nunca levanta: limpeza não pode atrapalhar quem só quer usar o
    app. O que ela achou vai para o log de atualizações, que é onde se olha
    quando alguém reclama de lentidão."""
    def run() -> None:
        try:
            mortos, perfis = navegadores.varrer_orfaos()
        except Exception:
            return
        if not (mortos or perfis):
            return
        with contextlib.suppress(Exception):
            _atualizacao().registrar(
                f"varredura de navegadores órfãos: processos={mortos} "
                f"perfis_temporarios={perfis}"
            )

    threading.Thread(target=run, daemon=True).start()


def _raizes_mei() -> list[str]:
    """Pastas onde pode haver ``_MEI*``, na ordem em que interessam.

    Duas, e as duas precisam ser varridas:

    1. A que o bundle usa HOJE — o pai de ``sys._MEIPASS``. O onefile se extrai
       em ``%LOCALAPPDATA%\\OrganizadorProcessos\\_bundle``
       (``runtime_tmpdir`` no OrganizadorProcessos.spec), fora do alcance dos
       limpadores de temporários que apagavam .pyd ainda não importado e
       estouravam a busca em ``ModuleNotFoundError: No module named 'select'``.
    2. O ``%TEMP%`` LEGADO, onde o app se extraía até a 1.15.0. As órfãs
       de ~99 MB que ficaram lá continuam sendo lixo para recolher, e ninguém
       mais passaria por elas se esta função só olhasse a raiz nova.

    Do fonte só existe a segunda (não há ``_MEIPASS``)."""
    raizes: list[str] = []
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        with contextlib.suppress(Exception):
            raizes.append(os.path.dirname(os.path.abspath(mei)))
    with contextlib.suppress(Exception):
        import tempfile
        raizes.append(tempfile.gettempdir())
    vistas: set[str] = set()
    unicas: list[str] = []
    for raiz in raizes:
        chave = os.path.normcase(raiz)
        if chave not in vistas:
            vistas.add(chave)
            unicas.append(raiz)
    return unicas


def _limpar_temp_orfaos() -> int:
    """Apaga pastas ``_MEI*`` órfãs nas raízes de ``_raizes_mei()``.
    Devolve quantas foram removidas.

    O onefile do PyInstaller extrai o bundle inteiro num ``_MEIxxxxx`` novo a
    cada abertura e o apaga ao sair — mas um processo morto sem passar por essa
    saída normal (crash, "Finalizar tarefa", queda de energia) nunca faz essa
    limpeza. O lixo acumula (16 pastas foram encontradas nesta máquina), o que
    dói justamente nas máquinas fracas, que costumam ter disco pequeno.

    Só apaga o que está comprovadamente órfão, em três filtros que cobrem coisas
    diferentes: a pasta do processo ATUAL é pulada por caminho; as das instâncias
    já RODANDO são puladas pela sonda ``_mei_em_uso`` (binário carregado = imagem
    travada); e as que estão sendo EXTRAÍDAS agora são puladas por idade
    (``_mei_recem_escrito``), porque durante a extração nada está travado ainda e
    a sonda daria "órfã" para uma instância que está abrindo — ver ``_EXTRACAO_S``.

    O bootloader sempre cria um ``_MEI`` NOVO, nunca reaproveita um existente."""
    import shutil

    atual = getattr(sys, "_MEIPASS", None)
    atual_norm = os.path.normcase(os.path.abspath(atual)) if atual else None
    removidas = 0
    for raiz in _raizes_mei():
        try:
            nomes = os.listdir(raiz)
        except OSError:
            continue          # raiz nova ainda não criada, ou sem permissão
        for nome in nomes:
            if not nome.startswith("_MEI"):
                continue
            caminho = os.path.join(raiz, nome)
            try:
                if not os.path.isdir(caminho):
                    continue
                if atual_norm and os.path.normcase(os.path.abspath(caminho)) == atual_norm:
                    continue
                # Antes da sonda: durante a extração nenhum binário está travado,
                # e a sonda diria "órfã" para uma instância que está abrindo agora.
                if _mei_recem_escrito(caminho):
                    continue
                if _mei_em_uso(caminho):
                    continue
                antes = os.path.isdir(caminho)
                shutil.rmtree(caminho, ignore_errors=True)
                if antes and not os.path.isdir(caminho):
                    removidas += 1
            except OSError:
                continue
    return removidas


# --------------------------------------------------------------------------
# Restaurar os dados do programa (botão da aba Configurações)
# --------------------------------------------------------------------------
# POR QUE ATUALIZAR NÃO BASTA
# ---------------------------
# Os processos pré-carregados vêm DENTRO do .exe (data/processos_base.sqlite),
# mas a lista da aba Pesquisa mescla três fontes por prioridade — pasta base >
# processos_online > embutido (ver indice_processos.carregar_resumos_pesquisa).
# Ou seja: o que está em APP_DATA_DIR ESCONDE o que vem no programa. E a
# atualização não toca em APP_DATA_DIR de propósito (semear_config nunca
# sobrescreve, senão o update apagaria a senha que o usuário acabou de corrigir).
#
# O efeito numa máquina que recebeu o app com o cache já povoado de dados
# errados: atualizar não mudava nada, e a única saída era apagar arquivos no
# %USERPROFILE% na mão — o que ninguém vai pedir para um usuário fazer por
# telefone. Daí o botão.

def _itens_reset() -> tuple[tuple[Path, str], ...]:
    """(caminho, rótulo) de tudo que "Restaurar dados do programa" apaga.

    Só dados DERIVADOS: cada um destes se reconstrói sozinho na abertura
    seguinte, a partir do que vem no .exe. Ficam de fora DE PROPÓSITO:
    ``config/`` (senhas editadas pelo usuário — apagar seria perder o que ele
    digitou), ``autojur_profile/`` (apagar força o 2FA de novo),
    ``ui_state.json`` (pastas escolhidas) e ``comarcas_por_origem.json``, que
    guarda correções MANUAIS de comarca — a parte auto-aprendida dele é
    sobrescrita sozinha quando a lista recarrega."""
    return (
        (PROCESSOS_ONLINE_DIR, "processos guardados de buscas online"),
        (PROCESSOS_INDICE_JSON, "índice da lista de processos"),
        (TEMPLATES_CACHE_PATH, "cache dos modelos de petição"),
    )


def _contar_processos_locais() -> int:
    """Quantos processos o cache desta máquina guarda.

    Usado só para dizer no aviso QUANTOS somem: quem clica no botão está com o
    app estranho, não querendo descobrir depois que perdeu trabalho."""
    try:
        return sum(1 for _ in PROCESSOS_ONLINE_DIR.glob("*/metadados.json"))
    except OSError:
        return 0


def _resetar_dados_locais() -> tuple[list[str], list[str]]:
    """Apaga os itens de `_itens_reset`. Devolve ``(apagados, falhas)``.

    Item a item, e o que já não existe não conta nem como apagado nem como
    falha. Best-effort dentro de cada item (``ignore_errors``) porque a máquina
    que precisa deste botão é justamente a que está num estado esquisito: um
    metadados.json travado não pode impedir a limpeza dos outros 3.000. Se
    sobrou pasta, aí sim é falha — e o rótulo dela entra no relatório em vez de
    o app afirmar que limpou o que não limpou."""
    import shutil

    apagados: list[str] = []
    falhas: list[str] = []
    for caminho, rotulo in _itens_reset():
        try:
            if caminho.is_dir():
                shutil.rmtree(caminho, ignore_errors=True)
            elif caminho.exists():
                caminho.unlink()
            else:
                continue
            if caminho.exists():
                falhas.append(rotulo)
                continue
        except OSError:
            falhas.append(rotulo)
            continue
        apagados.append(rotulo)
    return apagados, falhas


# --------------------------------------------------------------------------
# Extensões C que o app só importaria TARDE — importadas na abertura DE PROPÓSITO
# --------------------------------------------------------------------------
# O `select` entra pelo urllib3 (`urllib3/util/wait.py`, linha 3: `import
# select`), e o urllib3 só é carregado quando o selenium é — ou seja, no PRIMEIRO
# clique em "Buscar online", pelo import tardio de `organizador.scraper`. Até ali
# `select.pyd` nunca foi carregado. Duas consequências, as duas medidas:
#
# 1. O ARQUIVO FICA DESPROTEGIDO. O Windows trava a imagem de DLL/.pyd
#    CARREGADO; o que ninguém importou é apagável por qualquer faxina de
#    temporários. Foi assim que `select.pyd` sumiu da pasta extraída com o app
#    aberto (27/08/2026 e 31/08/2026) e `PIL/_imagingtk` antes dele (14/08/2026),
#    numa máquina com 96 GB livres. Importar aqui faz o Windows travar a imagem
#    destes arquivos — e SÓ destes. Medido no bundle em 31/08/2026: o clique em
#    "Buscar online" carrega 8 outras extensões (lxml/etree e _elementpath,
#    charset_normalizer/cd, md e __mypyc, _brotli, _uuid, _wmi) que ficam
#    destravadas até ele acontecer. Cobri-las exigiria importar selenium+bs4 na
#    abertura, que são os 2,5 s que scraper.py existe para evitar. A defesa que
#    vale para o bundle INTEIRO é a outra: `runtime_tmpdir` fora do %TEMP% (ver
#    OrganizadorProcessos.spec). Esta aqui é a segunda linha, para a stdlib que
#    sai barato — e para o diagnóstico do item 2.
# 2. O ERRO SAÍA LONGE DA CAUSA. Uma extração incompleta passava limpa pela
#    checagem de abertura e estourava no meio de uma busca, numa janela de erro
#    que não diz nada ao usuário. Aqui a falha vira "Instalação incompleta",
#    antes de a pessoa começar a trabalhar.
#
# Custo: são as mesmas extensões que o app carregaria de todo jeito — `_socket` e
# `_ssl` pelo auto-updater (`urllib.request`), `_sqlite3` pela base embutida,
# `zlib` que no Python 3.14 é builtin (não há arquivo para travar; fica na lista
# porque a checagem vale para qualquer versão). O que muda é a ORDEM, e por isso
# quem chama é `_tarefas_de_abertura`, numa thread: medido em processo frio nesta
# máquina, `select` 2,4 ms + `_socket` 4,6 ms + `_ssl` 11,0 ms = ~18 ms que NÃO
# podem entrar no tempo até a janela aparecer. Medir com scripts/bench_abertura.py.
#
# IMPORTAR, não `find_spec`: find_spec responde a mesma coisa mas não carrega, e
# era justamente o carregamento que faltava para o arquivo ficar protegido.
_MODULOS_TARDIOS = ("select", "_socket", "_ssl", "_sqlite3", "zlib")

_tardios_faltando: list[str] | None = None


def _modulos_binarios_faltando() -> list[str]:
    """Importa as extensões de ``_MODULOS_TARDIOS``; devolve as que faltaram.

    Vazio = tudo certo (e tudo travado). O resultado é memorizado: a resposta é
    do momento da abertura, e repetir os imports não mudaria nada."""
    global _tardios_faltando
    if _tardios_faltando is None:
        from importlib import import_module

        faltando = []
        for nome in _MODULOS_TARDIOS:
            try:
                import_module(nome)
            except Exception:
                faltando.append(nome)
        _tardios_faltando = faltando
    return _tardios_faltando


def _temp_apertado() -> str:
    """Descrição da pasta de extração quando ela explica um bundle incompleto.

    Espaço livre e pastas `_MEI*` de sobra: as duas coisas que o usuário
    consegue resolver sozinho, e que ainda podem truncar a extração. Cada órfã
    pesa o bundle inteiro (~99 MB), e elas só acumulam onde a remoção na saída
    falha (antivírus segurando arquivo, processo morto à força).

    Olha as raízes de `_raizes_mei()` — a do bundle de hoje e o %TEMP% legado —
    e mede o espaço livre na primeira, que é onde a extração acontece."""
    import shutil

    raizes = _raizes_mei()
    if not raizes:
        return ""
    atual = os.path.normcase(str(getattr(sys, "_MEIPASS", "")))
    orfaos = 0
    for raiz in raizes:
        try:
            for n in os.listdir(raiz):
                caminho = os.path.join(raiz, n)
                if (n.startswith("_MEI") and os.path.isdir(caminho)
                        and os.path.normcase(caminho) != atual):
                    orfaos += 1
        except OSError:
            continue
    try:
        livre_gb = shutil.disk_usage(raizes[0]).free / (1024 ** 3)
    except OSError:
        livre_gb = float("inf")
    partes = []
    if livre_gb < 3:
        partes.append(f"só {livre_gb:.1f} GB livres no disco")
    if orfaos:
        partes.append(f"{orfaos} pasta(s) _MEI* de sobra em {raizes[0]}")
    return "; ".join(partes)


def _dados_do_bundle_faltando() -> list[str]:
    """Itens embutidos na release que sumiram do ``_MEIPASS``. Vazio = tudo certo.

    Só faz sentido no .exe. Existe porque a perda desses arquivos é MUDA: sem
    ``Petições/`` a lista de modelos aparece vazia e nenhum campo é preenchido;
    sem a base SQLite os processos pré-carregados somem da aba Pesquisa. Nos dois
    casos o app abre com cara de são, e o usuário conclui que "o programa bugou".
    Foi exatamente o que a limpeza de ``_MEI*`` provocava ao rodar com duas
    instâncias abertas (ver ``_mei_em_uso``)."""
    if not getattr(sys, "frozen", False):
        return []
    faltando: list[str] = []
    if not PASTA_PETICOES.is_dir() or not any(PASTA_PETICOES.rglob("*.docx")):
        faltando.append("os modelos de petição (Petições/)")
    if not caminho_sqlite(Path(PROCESSOS_BASE_JSON)).is_file():
        faltando.append("a base de processos pré-carregados")
    if not CONFIG_SEMENTE_DIR.is_dir():
        faltando.append("a configuração do escritório (config/)")
    binarios = _modulos_binarios_faltando()
    if binarios:
        faltando.append("partes do próprio Python (" + ", ".join(binarios) + ")")
    return faltando


def _avisar_bundle_incompleto(app: "App", faltando: list[str] | None = None) -> None:
    """Registra e avisa o usuário quando o bundle chegou/ficou incompleto.

    `faltando` pronto vem de `_tarefas_de_abertura`, que já pagou esse I/O na
    thread; sem ele a lista é levantada aqui (é o que os testes fazem)."""
    if faltando is None:
        faltando = _dados_do_bundle_faltando()
    if not faltando:
        return
    lista = "\n".join(f"  • {item}" for item in faltando)
    aperto = _temp_apertado()
    _atualizacao().registrar(
        f"BUNDLE INCOMPLETO: {'; '.join(faltando)}"
        + (f" | temp: {aperto}" if aperto else "")
    )
    app._notificar(
        "Instalação incompleta",
        "Não encontrei estes itens que vêm dentro do programa:\n\n"
        f"{lista}\n\n"
        + (f"Causa provável: {aperto}. Feche o programa, libere espaço e apague "
           f"as pastas _MEI* que sobraram.\n\n" if aperto else
           "Feche TODAS as janelas do Organizador de Processos e abra apenas "
           "uma.\n\n")
        + "Se continuar, baixe o programa novamente.",
        nivel="error",
    )


def _tarefas_de_abertura(app: "App") -> None:
    """I/O de abertura que não pode atrasar o primeiro desenho da janela.

    Roda numa thread, e a ORDEM é o desenho aqui:

    1. Travar as extensões tardias (ver ``_MODULOS_TARDIOS``) — é a DEFESA, vem
       primeiro. Medido nesta máquina, em processo frio: ``select`` 2,4 ms,
       ``_socket`` 4,6 ms, ``_ssl`` 11,0 ms, ``_sqlite3`` e ``zlib`` já
       carregados pelo import da GUI. Fora do caminho crítico de propósito: sem a
       thread, esses ~18 ms (e o múltiplo disso num i3) sairiam do tempo até a
       janela aparecer, que é o que o app mais defende — e a janela de exposição
       que interessa é a SESSÃO inteira, não o primeiro segundo.
    2. Recolher as ``_MEI*`` órfãs — faxina, pode esperar.
    3. Avisar se o bundle está incompleto, já com o diagnóstico dos dois
       primeiros na mão. Também tira do caminho crítico o ``rglob("*.docx")``
       sobre ``Petições/`` que ``_dados_do_bundle_faltando`` faz.

    Nunca levanta: nada aqui pode impedir o app de abrir."""
    with contextlib.suppress(Exception):
        travadas = not _modulos_binarios_faltando()
        _perf("bundle:tardios_travados" if travadas else "bundle:tardios_faltando")
    with contextlib.suppress(Exception):
        _limpar_temp_orfaos()
    with contextlib.suppress(Exception):
        faltando = _dados_do_bundle_faltando()
        if faltando:
            # Tkinter só é seguro na main thread.
            app.after(0, lambda: _avisar_bundle_incompleto(app, faltando))


def main() -> None:
    # Semeia a config editável em APP_DATA_DIR na 1ª execução. Tem de vir ANTES
    # de qualquer leitura de credenciais/2FA: no .exe a config do bundle está numa
    # pasta temporária, e gravar lá perdia toda edição ao fechar o app.
    with contextlib.suppress(Exception):
        copiados = semear_config()
        if copiados:
            _perf(f"config:semeada({len(copiados)})")

    app = App()
    _perf("app:construido")
    # Numa thread: é I/O de disco e não pode atrasar o primeiro desenho.
    threading.Thread(
        target=_tarefas_de_abertura, args=(app,), daemon=True,
    ).start()
    # Órfãos de execuções ANTERIORES (navegador que ficou vivo depois de o app
    # morrer, e os perfis temporários que ele deixou no %TEMP%). Alguns segundos
    # depois de abrir, porque a varredura percorre a tabela de processos.
    app.after(_ATRASO_VARREDURA_MS, _iniciar_varredura_navegadores)
    # Pré-login do Autojur em 2º plano, se ligado (aquece a sessão sem travar a
    # GUI; na 1ª vez pede o 2FA por e-mail, depois o perfil persistente pula).
    if bool(app.var_autojur_prelogin.get()):
        app._iniciar_prelogin_autojur()
    app.mainloop()
