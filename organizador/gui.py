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
from tkinter import filedialog, messagebox

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
from .flexao import _construir_contexto
from .models import Parte, Processo
from .organizacao import (
    _autor, _carregar_processos, _reus_joined, carregar_processos_base,
    organizar_pasta, salvar_processo_metadados, uf_do_cnj,
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
        self.var_empregador = tk.StringVar(value="")
        self.var_percentual = tk.StringVar(value="")
        self._entries_auto: dict[str, tk.Entry] = {}
        self._processos_carregados: list[Processo] = []
        self._proc_selecionado: Processo | None = None
        self._templates_vars: dict[Path, tk.BooleanVar] = {}
        self.var_documento_unico = tk.BooleanVar(value=True)
        self.var_multiplos_imoveis = tk.BooleanVar(value=False)
        self.var_gerar_pdf = tk.BooleanVar(value=_word_disponivel())
        self._pesquisa_log_queue: queue.Queue[str] = queue.Queue()
        self._gen_worker: threading.Thread | None = None
        self._busca_worker: threading.Thread | None = None
        self._busca_result_queue: queue.Queue = queue.Queue()
        self._gen_result_queue: queue.Queue = queue.Queue()
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
        self.after(150, self._drain_busca_result)
        self.after(150, self._atualizar_progresso_pesquisa)
        self.after(200, self._drain_notify_queue)
        self.after(200, self._drain_gen_result_queue)
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

    def _build_widgets(self) -> None:
        nb = ttk.Notebook(self, bootstyle="primary")
        nb.pack(fill="both", expand=True, padx=_PAD_X, pady=(_PAD_X, 0))

        frm_org = ttk.Frame(nb, padding=8)
        frm_pesq = ttk.Frame(nb, padding=8)
        frm_config = ttk.Frame(nb, padding=8)
        nb.add(frm_org, text="  Organizador  ")
        nb.add(frm_pesq, text="  Pesquisa de Processo  ")
        nb.add(frm_config, text="  Configurações  ")
        # Referências p/ mostrar/ocultar conforme o modo dev.
        self._nb = nb
        self._tab_org = frm_org
        self._tab_pesq = frm_pesq

        self._build_aba_organizador(frm_org)
        self._build_aba_pesquisa(frm_pesq)
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
        self.var_filtro.trace_add("write", lambda *_: self._filtrar_pesquisa())
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

        # ── Modelos de petição (full-width, acima do painel de processos) ─
        f_templates = ttk.Labelframe(
            parent, text="  Modelos de petição  ",
            bootstyle="default", padding=(12, 8),
        )
        f_templates.pack(fill="x", expand=False, pady=(0, 4))
        self._templates_inner = ttk.Frame(f_templates, padding=4)
        self._templates_inner.pack(fill="both", expand=True)

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
            ("empregador", "O empregador:"),
            ("percentual", "Percentual (penhora):"),
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
        pasta = Path(self.var_pesquisa_base.get()).expanduser()
        procs = _carregar_processos(pasta) if pasta.is_dir() else []
        vistos = {p.numero for p in procs}
        for fonte in (_carregar_processos(PROCESSOS_ONLINE_DIR),
                      carregar_processos_base(PROCESSOS_BASE_JSON)):
            for p in fonte:
                if p.numero and p.numero not in vistos:
                    procs.append(p)
                    vistos.add(p.numero)
        return procs

    def _carregar_pesquisa_inicial(self) -> None:
        """Carga silenciosa no startup: popula a lista (base + buscados online),
        sem popup de erro quando a pasta base ainda não foi escolhida."""
        procs = self._listar_processos()
        if procs:
            self._processos_carregados = procs
            aprender_comarcas(procs)
            self._preencher_tree(procs)

    def _recarregar_pesquisa(self) -> None:
        pasta = Path(self.var_pesquisa_base.get()).expanduser()
        if not pasta.is_dir():
            messagebox.showerror("Erro", f"Pasta não existe:\n{pasta}")
            return
        _salvar_ui_state(pesquisa_base=str(pasta))
        # Pasta base + processos buscados online (lembrados entre execuções).
        self._processos_carregados = self._listar_processos()
        # Aprende código-de-origem CNJ → comarca destes processos (fallback de
        # município para processos futuros sem comarca explícita).
        aprender_comarcas(self._processos_carregados)
        self._preencher_tree(self._processos_carregados)

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

    def _preencher_tree(self, processos: list[Processo]) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for idx, proc in enumerate(processos):
            autor = _autor(proc)
            reu = _reus_joined(proc)
            proxima = ""
            if proc.prazos_abertos:
                proxima = proc.prazos_abertos[0].fim or "?"
            tag = "oddrow" if idx % 2 else "evenrow"
            self.tree.insert("", "end", iid=proc.numero, values=(
                proc.numero, proc.classe, autor, reu,
                proc.valor_causa, proc.ultima_movimentacao[:30], proxima,
            ), tags=(tag,))

    def _filtrar_pesquisa(self) -> None:
        filtro = self.var_filtro.get().lower().strip()
        if not filtro or not self._processos_carregados:
            self._preencher_tree(self._processos_carregados)
            return
        filtrados = [
            p for p in self._processos_carregados
            if filtro in p.numero.lower()
            or filtro in p.classe.lower()
            or filtro in _autor(p).lower()
            or filtro in _reus_joined(p).lower()
        ]
        self._preencher_tree(filtrados)

    def _toggle_modo_dev(self) -> None:
        """Persiste o modo dev e aplica a visibilidade na hora."""
        _salvar_ui_state(modo_dev=self.var_modo_dev.get())
        self._aplicar_modo_dev()

    def _aplicar_modo_dev(self) -> None:
        """Mostra/oculta os elementos avançados conforme o modo dev:
        aba Organizador, opções avançadas da busca e a seção 2FA das config."""
        dev = bool(self.var_modo_dev.get())

        # Aba Organizador (preserva a posição usando o estado da aba).
        if getattr(self, "_nb", None) is not None:
            with contextlib.suppress(Exception):
                self._nb.tab(self._tab_org, state="normal" if dev else "hidden")
                if not dev and self._nb.select() == str(self._tab_org):
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
        for w in self._templates_inner.winfo_children():
            w.destroy()
        self._templates_vars.clear()
        pasta = Path(self.var_pesquisa_base.get()).expanduser()
        templates_dir = pasta.parent / "Petições" if "Petições" in str(pasta.parent) else PASTA_PETICOES
        if not templates_dir.is_dir():
            templates_dir = PASTA_PETICOES
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
                        ttk.Checkbutton(
                            col, text=f.stem, variable=var,
                            command=self._on_template_toggle, bootstyle="info",
                        ).pack(anchor="w", pady=2)
                    col_idx += 1
        self._scan_templates_em_background()
        self._on_template_toggle()

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

        # Campos editáveis obrigatórios vazios: apenas destaca em vermelho
        # (sem bloquear a geração).
        self._destacar_campos_faltantes()

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
            "empregador": self.var_empregador.get(),
            "percentual": self.var_percentual.get(),
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
        ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=(6, 4))
        ttk.Label(
            frame, text="Arquivos gerados:", font=_FONT_BOLD,
        ).pack(anchor="w", pady=(0, 2))
        for arq in arquivos:
            f_row = ttk.Frame(frame)
            f_row.pack(fill="x", pady=1)
            icone = "📄" if Path(arq).suffix.lower() == ".pdf" else "📝"
            ttk.Label(
                f_row, text=f"{icone}  {Path(arq).name}", font=_FONT_BASE,
                anchor="w",
            ).pack(side="left", fill="x", expand=True)
            ttk.Button(
                f_row, text="Abrir", width=8, bootstyle="outline-info",
                command=lambda a=arq: os.startfile(a) if Path(a).exists() else None,
            ).pack(side="right", padx=(4, 0))
        pasta = Path(arquivos[0]).parent if arquivos else None
        if pasta:
            ttk.Button(
                frame, text="📁  Abrir pasta de destino",
                bootstyle="outline-secondary",
                command=lambda p=pasta: os.startfile(p) if p.exists() else None,
            ).pack(fill="x", pady=(4, 0))
        if frame.winfo_manager() != "pack":
            frame.pack(fill="x", pady=(4, 0))

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
            for t in (self._busca_worker, self._gen_worker, self._scan_tpls_thread)
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
    app.mainloop()
