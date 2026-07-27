# -*- coding: utf-8 -*-
"""Interface gráfica Tkinter (Organizador + Pesquisa de Processo)."""

from __future__ import annotations

import contextlib
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox

import ttkbootstrap as ttk
from ttkbootstrap.constants import *

from organizador import __version__

from .config import (
    OAB_ESCRITORIO_POR_UF, PASTA_PADRAO, PASTA_PETICOES, PROCESSOS_BASE_JSON,
    PROCESSOS_ONLINE_DIR,
    TJ_SISTEMAS, UI_STATE_PATH, sugerir_tj_para_uf, uf_efetiva_tj,
)


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
from .flexao import _construir_contexto, _eh_disjuncao_flexao
from .models import Parte, Processo
from .organizacao import (
    _autor, _reus_joined, organizar_pasta, salvar_processo_metadados, uf_do_cnj,
)
from .padroes import RE_NUMERO_CNJ
from .parsing import (
    _limpar_secretaria, _municipio_de_processo, _oab_completa, _uf_de_numero_cnj,
    aprender_comarcas, aprender_municipio,
)
from .peticoes import (
    PLACEHOLDERS_AUTO, PLACEHOLDERS_PETICAO,
    _extrair_placeholders_template, _template_eh_imobiliario, gerar_peticoes,
)
from .pesquisa import (
    carregar_processos_pesquisa, filtrar_processos, linhas_tree_processos,
)
from .integracoes.tjsp_scraper.credenciais import listar_credenciais, salvar_credenciais
from .scraper import (
    CHAVES_2FA, _LogWriter, buscar_processo_online, ler_config_2fa, salvar_config_2fa,
)


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
    try:
        import win32com.client  # noqa: F401
        return True
    except ImportError:
        return False


def _converter_docx_para_pdf(docx_path: Path) -> Path:
    """Converte .docx para .pdf usando Microsoft Word via COM."""
    try:
        import win32com.client
        import pythoncom
    except ImportError:
        raise RuntimeError("pywin32 não instalado — conversão para PDF indisponível.")
    pythoncom.CoInitialize()
    word = win32com.client.Dispatch("Word.Application")
    word.Visible = False
    word.DisplayAlerts = False
    try:
        doc = word.Documents.Open(str(docx_path.resolve()))
        pdf_path = docx_path.with_suffix(".pdf")
        doc.SaveAs(str(pdf_path.resolve()), FileFormat=17)  # 17 = wdFormatPDF
        doc.Close(SaveChanges=False)
    finally:
        word.Quit(SaveChanges=False)
    return pdf_path


# Fontes e constantes visuais
_FONT_BASE = ("Segoe UI", 11)
_FONT_BOLD = ("Segoe UI", 11, "bold")
_FONT_MONO = ("Consolas", 10)
_FONT_TITLE = ("Segoe UI", 12, "bold")
_PAD_X = 12
_PAD_Y = 6


class App(ttk.Window):
    def __init__(self) -> None:
        super().__init__(title=f"Organizador de Processos — eproc — v{__version__}", themename="litera")
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
        self._log_queue: queue.Queue[str] = queue.Queue()
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
        # Pré-login: loga no Autojur OCULTO em 2º plano ao abrir o app, para a
        # sessão já estar pronta na hora de gerar a tarefa (vira visível sob
        # demanda p/ conferência). Ligado por padrão; lembrado entre sessões.
        self.var_autojur_prelogin = tk.BooleanVar(
            value=bool(_ui_state.get("autojur_prelogin", True))
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
        self._autojur_prelogin_worker: threading.Thread | None = None
        self._autojur_worker: threading.Thread | None = None
        self._autojur_log_queue: queue.Queue[str] = queue.Queue()
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
        self._processos_carregados: list[Processo] = []
        self._proc_selecionado: Processo | None = None
        self._templates_vars: dict[Path, tk.BooleanVar] = {}
        self.var_documento_unico = tk.BooleanVar(value=True)
        self.var_multiplos_imoveis = tk.BooleanVar(value=False)
        self.var_gerar_pdf = tk.BooleanVar(value=_word_disponivel())
        self._pesquisa_log_queue: queue.Queue[str] = queue.Queue()
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
        # _cred_rows: uma linha por credencial de tribunal; _2fa_rows: um campo por
        # segredo de 2FA. Os Entry de senha guardam a referência para o toggle de
        # visibilidade ("Mostrar senhas").
        self._cred_rows: list[dict] = []
        self._2fa_rows: dict[str, dict] = {}
        self._entries_senha: list[ttk.Entry] = []
        self.var_mostrar_senhas = tk.BooleanVar(value=False)
        self._frame_cred: ttk.Frame | None = None

        self._build_widgets()
        self.after(100, self._drain_ui_queues)
        self.after(100, self._drain_pesquisa_log_queue)
        self.after(150, self._drain_pesquisa_load_result)
        self.after(150, self._drain_busca_result)
        self.after(200, self._drain_monitor_result)
        self.after(150, self._atualizar_progresso_pesquisa)
        self.after(200, self._drain_notify_queue)
        self.after(200, self._drain_gen_result_queue)
        self.after(100, self._drain_autojur_log_queue)
        self.after(150, self._drain_autojur_result)
        self.after(150, self._drain_autojur_tarefa_result)
        self.after(150, self._drain_autojur_escolha)
        self.after(150, self._drain_envio_autojur_result)
        # Pré-carrega os processos já trabalhados (pasta base lembrada), para
        # adicionar novas petições a um processo existente sem recarregar à mão.
        self.after(300, self._carregar_pesquisa_inicial)
        # Modelos de petição disponíveis já na abertura: permite escolher o
        # modelo e preencher tudo à mão (gerar sem buscar/selecionar processo).
        self.after(300, self._carregar_templates)
        # Verifica atualizações em silêncio na abertura: dispara o Atualizador em
        # modo --auto, que só mostra a janela se houver versão nova (caso contrário
        # sai sem exibir nada). Assim o usuário não precisa abrir o Atualizador.
        self.after(1500, self._verificar_atualizacao_silenciosa)
        # Monitoramento semanal: na abertura, se já se passaram ≥ 7 dias desde a
        # última passada bem-sucedida, dispara uma sincronização das planilhas em
        # silêncio (cadência semanal). Pode ser acionado a qualquer momento pelo
        # botão "Sincronizar planilhas & mapear" na aba Pesquisa.
        self.after(2500, self._verificar_monitoramento_semanal)

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

        self._build_aba_organizador(frm_org)
        self._build_aba_pesquisa(frm_pesq)
        self._build_aba_autojur(frm_autojur)
        self._build_aba_config(frm_config)
        self._aplicar_modo_dev()

        # Barra de status moderna
        bar = ttk.Frame(self, bootstyle="secondary")
        bar.pack(fill="x", side="bottom")
        ttk.Label(
            bar, textvariable=self.var_status, anchor="w",
            font=_FONT_BASE, bootstyle="inverse-secondary",
        ).pack(fill="x", padx=_PAD_X, pady=5)

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
        ttk.Combobox(
            f_busca, textvariable=self.var_busca_tj, values=list(TJ_SISTEMAS),
            state="readonly", width=18, height=20, font=_FONT_BASE,
        ).grid(row=0, column=3, sticky="w", pady=2)
        # ── Linha 1: "Forçar busca online" (sempre visível) + botão Buscar ──
        chk_forcar = ttk.Checkbutton(
            f_busca, text="Forçar busca online", variable=self.var_busca_forcar,
            bootstyle="warning-round-toggle",
        )
        chk_forcar.grid(row=1, column=0, columnspan=2, sticky="w", padx=(0, 6), pady=(4, 0))
        self._btn_buscar = ttk.Button(
            f_busca, text="🔍  Buscar online", command=self._buscar_online,
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
                 "busca os novos nos tribunais.",
            font=_FONT_BASE, wraplength=360, justify="left",
        ).pack(side="left", fill="x", expand=True)
        self._btn_monitor_parar = ttk.Button(
            f_mon, text="Parar", command=self._parar_monitoramento,
            bootstyle="danger-outline", width=8, state="disabled",
        )
        self._btn_monitor_parar.pack(side="right")
        self._btn_monitor = ttk.Button(
            f_mon, text="🔄  Sincronizar planilhas & mapear",
            command=self._iniciar_monitoramento, bootstyle="success", width=32,
        )
        self._btn_monitor.pack(side="right", padx=(0, 8))

        # ── Modelos de petição (full-width, acima do painel de processos) ─
        f_templates = ttk.Labelframe(
            parent, text="  Modelos de petição  ",
            bootstyle="default", padding=(12, 8),
        )
        f_templates.pack(fill="x", expand=False, pady=(0, 4))
        self._templates_inner = ttk.Frame(f_templates, padding=4)
        self._templates_inner.pack(fill="both", expand=True)
        # Requebra o texto dos checkbuttons de petição quando a área muda de largura.
        self._templates_inner.bind("<Configure>", self._requebrar_templates)

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
        det_canvas.bind("<Leave>", lambda e: det_canvas.unbind_all("<MouseWheel>"))

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
        # Botão para copiar todos os dados do processo de uma vez.
        ttk.Button(
            self._detalhes_frame, text="📋  Copiar dados", command=self._copiar_todos_dados,
            bootstyle="outline-info", width=16,
        ).grid(row=len(campos), column=1, sticky="e", padx=(0, 4), pady=(6, 2))

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
        ttk.Label(f_fmt, text="📝", font=("Segoe UI", 14)).pack(side="left", padx=(0, 4))
        ttk.Label(f_fmt, text="Word (.docx)", font=_FONT_BASE, bootstyle="secondary").pack(side="left", padx=(0, 20))
        ttk.Label(f_fmt, text="📄", font=("Segoe UI", 14)).pack(side="left", padx=(0, 4))
        _cb_pdf = ttk.Checkbutton(
            f_fmt, text="PDF", variable=self.var_gerar_pdf, bootstyle="info",
        )
        _cb_pdf.pack(side="left")
        if not _word_disponivel():
            _cb_pdf.configure(state="disabled")
            ttk.Label(
                f_fmt,
                text="(Microsoft Word não instalado — contate o TI)",
                font=_FONT_BASE,
                bootstyle="danger",
            ).pack(side="left", padx=(6, 0))
        ttk.Button(
            f_gerar, text="📄  Gerar petições selecionadas",
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
            f_btn, text="🔐  Abrir Autojur (login)", command=self._autojur_abrir,
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
            f_acao, text="📝  Criar tarefa de Protocolo",
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
        self.progress_autojur.start(15)
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
                self._autojur_result_queue.put(("erro", str(exc)))

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

        def run() -> None:
            with contextlib.suppress(Exception):
                scraper.fechar()

        threading.Thread(target=run, daemon=True).start()

    def _drain_autojur_log_queue(self) -> None:
        try:
            while True:
                msg = self._autojur_log_queue.get_nowait()
                self.txt_autojur.configure(state="normal")
                self.txt_autojur.insert("end", msg)
                self.txt_autojur.see("end")
                self.txt_autojur.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._drain_autojur_log_queue)

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
        finally:
            self.after(150, self._drain_autojur_result)

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
        self.progress_autojur.start(15)
        self.var_status.set("Autojur: criando tarefa de Protocolo...")
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
                self._autojur_tarefa_result_queue.put(("erro", str(exc)))

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
        finally:
            self.after(150, self._drain_autojur_tarefa_result)

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
        finally:
            self.after(150, self._drain_autojur_escolha)

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

        # ── Seção: Credenciais dos tribunais ───────────────────────────
        sec_cred = ttk.Labelframe(
            inner, text="  Credenciais dos tribunais  ", bootstyle="default", padding=(12, 8),
        )
        sec_cred.pack(fill="x", pady=(0, 8))
        self._frame_cred = ttk.Frame(sec_cred)
        self._frame_cred.pack(fill="x")

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
        for i, (rotulo, chave) in enumerate(CHAVES_2FA.items()):
            ttk.Label(f2, text=rotulo + ":", font=_FONT_BASE, anchor="e", width=32).grid(
                row=i, column=0, sticky="e", padx=(0, 8), pady=3,
            )
            var = tk.StringVar(value="")
            # Tudo que não for o e-mail é segredo → mascarado por padrão.
            secreto = chave != "IMAP_USER"
            entry = ttk.Entry(f2, textvariable=var, font=_FONT_BASE)
            if secreto:
                entry.configure(show="•")
                self._entries_senha.append(entry)
            entry.grid(row=i, column=1, sticky="ew", pady=3)
            self._2fa_rows[chave] = {"var": var, "entry": entry, "secreto": secreto}

        # ── Ações ──────────────────────────────────────────────────────
        f_acoes = ttk.Frame(inner)
        f_acoes.pack(fill="x", pady=(4, 0))
        self._f_acoes_config = f_acoes  # âncora p/ re-empacotar a seção 2FA na ordem
        ttk.Button(
            f_acoes, text="💾  Salvar configurações", command=self._salvar_config,
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
        ttk.Checkbutton(
            f_acoes, text="Modo desenvolvedor", variable=self.var_modo_dev,
            command=self._toggle_modo_dev, bootstyle="warning-round-toggle",
        ).pack(side="right")
        ttk.Checkbutton(
            f_acoes, text="Salvar tarefas no Autojur", variable=self.var_autojur_salvar_prod,
            command=self._toggle_autojur_salvar, bootstyle="danger-round-toggle",
        ).pack(side="right", padx=(0, 16))
        ttk.Checkbutton(
            f_acoes, text="Visualizar navegador do Autojur", variable=self.var_autojur_visualizar,
            command=self._toggle_autojur_visualizar, bootstyle="info-round-toggle",
        ).pack(side="right", padx=(0, 16))
        ttk.Checkbutton(
            f_acoes, text="Pré-logar no Autojur ao abrir (oculto)", variable=self.var_autojur_prelogin,
            command=self._toggle_autojur_prelogin, bootstyle="success-round-toggle",
        ).pack(side="right", padx=(0, 16))

        self._carregar_config_na_ui()

    def _carregar_config_na_ui(self) -> None:
        """(Re)carrega credenciais e segredos de 2FA dos arquivos para a tela."""
        # Credenciais: reconstrói as linhas a partir do arquivo atual.
        for w in self._frame_cred.winfo_children():
            w.destroy()
        self._cred_rows.clear()
        # Mantém só os Entry de 2FA na lista de senhas; os de credenciais serão
        # readicionados abaixo (evita referências a widgets destruídos).
        self._entries_senha = [
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

    def _drain_ui_queues(self) -> None:
        """Drena filas de log/progresso/status (main thread). Tkinter só é
        seguro nesta thread, então toda atualização vinda de workers passa por
        aqui."""
        # Logs: insere todas as mensagens acumuladas.
        try:
            mensagens: list[str] = []
            while True:
                mensagens.append(self._log_queue.get_nowait())
        except queue.Empty:
            pass
        if mensagens:
            self.txt.configure(state="normal")
            for msg in mensagens:
                self.txt.insert("end", msg + "\n")
            self.txt.see("end")
            self.txt.configure(state="disabled")

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

        self.after(100, self._drain_ui_queues)

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
        self.after(200, self._drain_notify_queue)

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

    def _listar_processos(self) -> list[Processo]:
        """Processos da pasta base + buscados online (persistidos) + os
        embutidos na release, sem duplicar por número. Prioridade: pasta base
        (curada) > cache de buscas online do usuário > base embutida (release),
        para que a busca do próprio usuário sempre prevaleça sobre o snapshot."""
        return carregar_processos_pesquisa(
            Path(self.var_pesquisa_base.get()),
            PROCESSOS_ONLINE_DIR,
            PROCESSOS_BASE_JSON,
        )

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
                procs = carregar_processos_pesquisa(pasta, online_dir, base_json)
                aprender_comarcas(procs)
                self._pesquisa_load_queue.put(("ok", procs, silencioso))
            except Exception as exc:
                self._pesquisa_load_queue.put(("erro", str(exc), silencioso))

        self._pesquisa_load_worker = threading.Thread(target=run, daemon=True)
        self._pesquisa_load_worker.start()

    def _carregar_pesquisa_inicial(self) -> None:
        """Carga silenciosa no startup: popula a lista (base + buscados online),
        sem popup de erro quando a pasta base ainda não foi escolhida."""
        self._iniciar_carregamento_pesquisa(silencioso=True)

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
        finally:
            self.after(150, self._drain_pesquisa_load_result)

    def _buscar_online(self) -> None:
        """Dispara a busca do processo no TJ/sistema escolhido numa thread.

        Cache-first: se o número digitado já estiver carregado (pasta base ou
        cache de buscas anteriores), apenas seleciona a linha — não vai à web."""
        texto = self.var_filtro.get().strip()
        if not texto:
            messagebox.showwarning("Buscar processo", "Informe o número do processo (CNJ).")
            return
        # Extrai o CNJ canônico do que foi digitado (a barra também serve de filtro).
        m = RE_NUMERO_CNJ.search(texto)
        numero = m.group(0) if m else texto

        # Se o processo já foi carregado, não precisa buscar na web: seleciona.
        # Exceção: "Forçar busca online" marcado → atualiza o dado pela web.
        forcar = self.var_busca_forcar.get()
        existente = next(
            (p for p in self._processos_carregados if p.numero == numero), None
        )
        if existente and not forcar:
            self._preencher_tree(self._processos_carregados)
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

        # Sugere trocar de sistema se o número for de outra UF que não a do TJ
        # selecionado. (Antes de desabilitar o botão: "Cancelar" só faz return.)
        uf_num = uf_do_cnj(numero)
        uf_sel = uf_efetiva_tj(tj_label)
        if uf_num and uf_sel and uf_num != uf_sel:
            sug = sugerir_tj_para_uf(uf_num)
            if sug:
                resp = messagebox.askyesnocancel(
                    "Estado do processo",
                    f"O número parece ser de {uf_num}, mas o sistema selecionado é\n"
                    f"'{tj_label}' ({uf_sel}).\n\n"
                    f"Deseja trocar para '{sug}'?\n\n"
                    f"Sim = trocar e buscar    •    Não = buscar assim mesmo    •    "
                    f"Cancelar = não buscar",
                )
                if resp is None:  # Cancelar
                    return
                if resp:  # Sim → troca o sistema
                    self.var_busca_tj.set(sug)
                    tj_label = sug
                    self._pesquisa_log_queue.put(
                        f"Sistema trocado para {sug} (número de {uf_num}).\n"
                    )
            # Sem sistema configurado p/ essa UF → segue (aviso no log do scraper).

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
                self._busca_result_queue.put(("erro", str(exc)))

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

        def run() -> None:
            try:
                scraper.fechar()
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

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
                    # de origem "Mapeamento: ..." e o valor da causa quando o
                    # online não o trouxe — não perde o que veio da planilha.
                    anterior = next(
                        (p for p in self._processos_carregados if p.numero == proc.numero), None
                    )
                    if anterior is not None:
                        for t in anterior.tags:
                            if str(t).startswith("Mapeamento:") and t not in proc.tags:
                                proc.tags.insert(0, t)
                        if not proc.valor_causa and anterior.valor_causa:
                            proc.valor_causa = anterior.valor_causa
                    self._processos_carregados = [
                        p for p in self._processos_carregados if p.numero != proc.numero
                    ]
                    self._processos_carregados.append(proc)
                    aprender_comarcas([proc])
                    # Persiste o processo buscado online para reaparecer ao reabrir.
                    salvar_processo_metadados(proc, PROCESSOS_ONLINE_DIR)
                    self._preencher_tree(self._processos_carregados)
                    if proc.numero and self.tree.exists(proc.numero):
                        self.tree.selection_set(proc.numero)
                        self.tree.see(proc.numero)
                    self._pesquisa_log_queue.put(f"Processo {proc.numero} carregado.\n")
                    self.var_status.set(f"Processo {proc.numero} carregado.")
                    with contextlib.suppress(Exception):
                        self.bell()  # sinal sonoro de conclusão da busca
                else:
                    self._pesquisa_log_queue.put(f"! Erro na busca online: {payload}\n")
                    self._notificar("Busca online", str(payload), nivel="error")
                self._btn_buscar.configure(state="normal")
        except queue.Empty:
            pass
        finally:
            self.after(150, self._drain_busca_result)

    # -- monitoramento: planilhas → mapeamento de sistema ------------------
    def _iniciar_monitoramento(self, auto: bool = False) -> None:
        """Dispara uma passada de monitoramento (planilhas → mapeamento) numa
        thread. Lê as planilhas, pula os já mapeados e raspa os pendentes.

        ``auto=True`` = disparo silencioso semanal: não abre pop-up de erro nem
        avisa quando as planilhas já estão em dia."""
        if self._monitor_worker and self._monitor_worker.is_alive():
            if not auto:
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
                self._monitor_result_queue.put(("ok", resumo, auto))
            except Exception as exc:
                self._monitor_result_queue.put(("erro", str(exc), auto))

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
                tipo, payload, auto = self._monitor_result_queue.get_nowait()
                self._btn_monitor.configure(state="normal")
                self._btn_monitor_parar.configure(state="disabled")
                if tipo == "ok":
                    r = payload
                    msg = (f"{r.get('mapeados_agora', 0)} mapeados agora • "
                           f"{r.get('nao_encontrados', 0)} não encontrados • "
                           f"{r.get('ja_mapeados', 0)} já estavam mapeados "
                           f"(de {r.get('total', 0)} nas planilhas).")
                    self.var_status.set("Monitoramento concluído: " + msg)
                    self._pesquisa_log_queue.put("✔️ " + msg + "\n")
                    # Novos processos no cache → recarrega a lista p/ aparecerem.
                    if r.get("mapeados_agora"):
                        with contextlib.suppress(Exception):
                            self._recarregar_pesquisa()
                    # Só notifica se foi manual OU se houve trabalho (evita pop-up
                    # no disparo semanal silencioso quando já está tudo em dia).
                    if not auto or r.get("mapeados_agora"):
                        self._notificar("Monitoramento", msg, nivel="info")
                    from datetime import datetime as _dt
                    _salvar_ui_state(
                        ultimo_monitoramento=_dt.now().isoformat(timespec="seconds")
                    )
                else:
                    self._pesquisa_log_queue.put(f"! Erro no monitoramento: {payload}\n")
                    self.var_status.set("Monitoramento: erro (ver detalhes).")
                    if not auto:
                        self._notificar("Monitoramento", str(payload), nivel="error")
        except queue.Empty:
            pass
        finally:
            self.after(200, self._drain_monitor_result)

    def _verificar_monitoramento_semanal(self) -> None:
        """Na abertura do app, dispara o monitoramento em silêncio se já se
        passaram ≥ 7 dias desde a última passada bem-sucedida (cadência semanal).
        Best-effort: qualquer falha é ignorada (nunca trava a abertura)."""
        try:
            from datetime import datetime, timedelta
            ultimo = _carregar_ui_state().get("ultimo_monitoramento")
            if ultimo:
                try:
                    quando = datetime.fromisoformat(ultimo)
                except ValueError:
                    quando = None
                if quando is not None and (datetime.now() - quando) < timedelta(days=7):
                    return  # ainda dentro da janela semanal
            self._pesquisa_log_queue.put(
                "🗓️ monitoramento semanal: iniciando passada automática...\n"
            )
            self._iniciar_monitoramento(auto=True)
        except Exception:
            pass

    def _preencher_tree(self, processos: list[Processo]) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for iid, values, tag in linhas_tree_processos(processos):
            self.tree.insert("", "end", iid=iid, values=values, tags=(tag,))

    def _agendar_filtrar_pesquisa(self) -> None:
        if self._filtro_after_id is not None:
            with contextlib.suppress(Exception):
                self.after_cancel(self._filtro_after_id)
        self._filtro_after_id = self.after(120, self._filtrar_pesquisa)

    def _filtrar_pesquisa(self) -> None:
        self._filtro_after_id = None
        self._preencher_tree(
            filtrar_processos(self._processos_carregados, self.var_filtro.get())
        )

    def _toggle_modo_dev(self) -> None:
        """Persiste o modo dev e aplica a visibilidade na hora."""
        _salvar_ui_state(modo_dev=self.var_modo_dev.get())
        self._aplicar_modo_dev()

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
        proc = next((p for p in self._processos_carregados if p.numero == numero), None)
        self._proc_selecionado = proc
        if not proc:
            return
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

        self._pesquisa_log_queue.put(
            f"Escaneando placeholders de {len(pendentes)} template(s)...\n"
        )

        def run() -> None:
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except Exception:
                pass
            for p in pendentes:
                try:
                    phs = _extrair_placeholders_template(p)
                except Exception as exc:
                    self._pesquisa_log_queue.put(f"! Erro escaneando {p.name}: {exc}\n")
                    phs = set()
                self._templates_placeholders[p] = phs
                # Atualiza destaques na main thread sempre que um template é processado.
                self.after(0, self._on_template_toggle)
            self._pesquisa_log_queue.put("Escaneamento de templates concluído.\n")

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
                    for docx_path in gerados:
                        try:
                            pdf = _converter_docx_para_pdf(docx_path)
                            todos.append(pdf)
                            self._pesquisa_log_queue.put(f"  PDF: {pdf.name}\n")
                        except Exception as exc:
                            self._pesquisa_log_queue.put(f"  ERRO ao gerar PDF: {exc}\n")
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
        self.after(200, self._drain_gen_result_queue)

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
            icone = "📄" if arq_path.suffix.lower() == ".pdf" else "📝"
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
                    text="🔄 Atualizar PDF" if tem_pdf else "📄 Gerar PDF",
                    width=16,
                    bootstyle="outline-info" if tem_pdf else "outline-secondary",
                    command=lambda d=arq_path: self._reconverter_pdf([d]),
                ).pack(side="right", padx=(4, 0))
        pasta = Path(arquivos[0]).parent if arquivos else None
        if pasta:
            ttk.Button(
                frame, text="📁  Abrir pasta de destino",
                bootstyle="outline-secondary",
                command=lambda p=pasta: os.startfile(p) if p.exists() else None,
            ).pack(fill="x", pady=(4, 0))

        # Botão geral: reconverte todos os .docx da lista de uma vez (após
        # editar um ou mais Words). Só aparece se houver .docx e Word disponível.
        if docs_docx and word_ok:
            ttk.Button(
                frame,
                text="🔄  Reconverter Word(s) editado(s) para PDF",
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
                text=f"🚀  Enviar ao Autojur (1 tarefa · {n} anexo{'s' if n > 1 else ''})",
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
                novos_pdfs: list[Path] = []
                for docx_path in docs:
                    try:
                        pdf = _converter_docx_para_pdf(docx_path)
                        novos_pdfs.append(pdf)
                        self._pesquisa_log_queue.put(f"  PDF atualizado: {pdf.name}\n")
                    except Exception as exc:
                        self._pesquisa_log_queue.put(
                            f"  ERRO ao gerar PDF de {docx_path.name}: {exc}\n"
                        )
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

        # Cabeçalho da lista (contador dinâmico) + lista de PDFs em container
        # próprio, para poder anexar arquivos extras sem quebrar o layout.
        lbl_lista = ttk.Label(frm, font=_FONT_BOLD)
        lbl_lista.grid(row=4, column=0, sticky="w", pady=(8, 2))

        frm_pdfs = ttk.Frame(frm)
        frm_pdfs.grid(row=5, column=0, columnspan=2, sticky="ew")

        # Fonte da verdade da lista (mutável): [ (BooleanVar marcado?, caminho) ].
        vars_pdf: list = [(tk.BooleanVar(value=True), pdf) for pdf in pdfs]

        def _render_pdfs() -> None:
            for w in frm_pdfs.winfo_children():
                w.destroy()
            for i, (v, pdf) in enumerate(vars_pdf):
                ttk.Checkbutton(
                    frm_pdfs, text=f"📄  {Path(pdf).name}", variable=v, bootstyle="info",
                ).grid(row=i, column=0, sticky="w", pady=1)
            lbl_lista.configure(
                text=f"Arquivos a anexar ({len(vars_pdf)} — todos em UMA tarefa):"
            )

        def _adicionar_arquivo() -> None:
            caminhos = filedialog.askopenfilenames(
                parent=top, title="Adicionar arquivo(s) ao envio",
                filetypes=[("PDF", "*.pdf"), ("Todos", "*.*")],
            )
            existentes = {str(p) for _, p in vars_pdf}
            novos = 0
            for c in caminhos:
                if c and str(c) not in existentes:
                    vars_pdf.append((tk.BooleanVar(value=True), c))
                    existentes.add(str(c))
                    novos += 1
            if novos:
                _render_pdfs()

        ttk.Button(
            frm, text="➕  Adicionar arquivo", command=_adicionar_arquivo,
            bootstyle="outline-primary", width=20,
        ).grid(row=4, column=1, sticky="e", pady=(8, 2))

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
            selecionados = [pdf for v, pdf in vars_pdf if v.get()]
            if not selecionados:
                messagebox.showwarning("Autojur", "Marque ao menos um PDF.", parent=top)
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

        ttk.Button(f_b, text="🚀  Enviar ao Autojur", command=enviar,
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
        self.progress_pesquisa.start(15)
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
                self._envio_autojur_result_queue.put(("erro", str(exc)))

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
        finally:
            self.after(150, self._drain_envio_autojur_result)

    def _drain_pesquisa_log_queue(self) -> None:
        try:
            while True:
                msg = self._pesquisa_log_queue.get_nowait()
                self.txt_pesquisa.configure(state="normal")
                self.txt_pesquisa.insert("end", msg)
                self.txt_pesquisa.see("end")
                self.txt_pesquisa.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._drain_pesquisa_log_queue)

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
        ativo = any(
            t is not None and t.is_alive()
            for t in (
                self._busca_worker,
                self._gen_worker,
                self._scan_tpls_thread,
                self._pesquisa_load_worker,
            )
        )
        if ativo and not self._pesquisa_em_progresso:
            self._pesquisa_em_progresso = True
            self.progress_pesquisa.start(15)
        elif not ativo and self._pesquisa_em_progresso:
            self._pesquisa_em_progresso = False
            self.progress_pesquisa.stop()
        self.after(150, self._atualizar_progresso_pesquisa)

    def _verificar_atualizacao_silenciosa(self) -> None:
        """Dispara o Atualizador em modo silencioso na abertura do app.

        O Atualizador.exe (que carrega o token de leitura do GitHub) checa a
        release mais recente e só mostra a janela se houver versão nova; se já
        estiver atualizado ou houver qualquer erro/sem internet, sai calado.
        Best-effort: qualquer falha em iniciá-lo é ignorada.
        """
        try:
            # Passa o PID desta instância para o Atualizador encerrá-la antes de
            # trocar o exe (senão a versão antiga ficaria aberta após o update).
            pid = str(os.getpid())
            if getattr(sys, "frozen", False):
                # App empacotado: o Atualizador.exe fica ao lado do Peticoes.exe.
                exe = Path(sys.executable).resolve().parent / "Atualizador.exe"
                if not exe.exists():
                    return
                cmd = [str(exe), "--auto", "--parent-pid", pid]
            else:
                # Modo fonte (dev): roda o updater.py com o Python atual.
                updater = Path(__file__).resolve().parent.parent / "updater" / "updater.py"
                if not updater.exists():
                    return
                cmd = [sys.executable, str(updater), "--auto", "--parent-pid", pid]
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # sem console no Windows
            subprocess.Popen(cmd, close_fds=True, creationflags=flags)
        except OSError:
            pass


def main() -> None:
    app = App()
    # Pré-login do Autojur em 2º plano, se ligado (aquece a sessão sem travar a
    # GUI; na 1ª vez pede o 2FA por e-mail, depois o perfil persistente pula).
    if bool(app.var_autojur_prelogin.get()):
        app._iniciar_prelogin_autojur()
    app.mainloop()
