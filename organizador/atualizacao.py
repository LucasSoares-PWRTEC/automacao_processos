# -*- coding: utf-8 -*-
"""Auto-atualização do Peticoes.exe embutida no próprio app.

Antes disto existia um segundo executável (Atualizador.exe) que baixava a nova
versão e trocava o Peticoes.exe rodando ao lado. Isso exigia distribuir 3
arquivos por máquina (Peticoes.exe, Atualizador.exe, version.txt) e o
Atualizador matava o app com TerminateProcess antes de trocar o .exe.

Aqui o próprio Peticoes.exe se substitui: confirmado ao vivo (rename de um exe
em execução, no mesmo volume) que o Windows permite RENOMEAR a imagem de um
processo em execução — só não permite SOBRESCREVER os bytes dela. Por isso o
fluxo é sempre "renomear para .old, depois mover o novo para o nome original",
nunca escrever direto no arquivo aberto. Assim não precisa mais de um segundo
processo: baixa, renomeia a si mesmo, abre a versão nova e se fecha.

Teste em modo fonte:
    $env:PETICOES_UPDATER_TOKEN = (Get-Content secrets\\token_github.txt)
    python -c "from organizador import gui; gui.main()"
"""
from __future__ import annotations

import json
import os
import queue
import ssl
import sys
import threading
import time
import tkinter as tk
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from tkinter import messagebox
from urllib.parse import urlsplit

import ttkbootstrap as ttk

from . import __version__
from .config import APP_DATA_DIR, CAMINHO_ICONE

try:
    from ._token import GITHUB_TOKEN  # gerado pelo release.ps1; NUNCA commitado
except ImportError:
    GITHUB_TOKEN = os.environ.get("PETICOES_UPDATER_TOKEN", "")


REPO = "LucasTalen/automacao_processos"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET_NAME = "Peticoes.exe"
TIMEOUT = 30  # segundos (conexão/socket)
# Tamanho da leitura do socket. Era 1 MiB, e isso NÃO é detalhe de desempenho: o
# progresso só é reportado ao fechar um chunk, então até o 1º MiB chegar a janela
# fica em "Baixando atualização..." sem número nenhum e com a barra em zero —
# indistinguível de travada. Foi assim que uma VM de 2 GB apareceu "infinita"
# baixando: o rótulo sem os MB provou que nem um chunk havia completado. 64 KiB dá
# ~850 avisos nos 53 MB (barra andando desde o 1º segundo) e é o que torna o
# vigia de PARADO honesto: 60 s sem 64 KiB é travamento de verdade, 60 s sem
# 1 MiB pode ser só rede ruim.
CHUNK = 64 * 1024  # 64 KiB
# Teto para a checagem INTEIRA. TIMEOUT cobre o socket, mas NÃO o getaddrinfo:
# numa máquina com DNS em blackhole a resolução do nome pendura sem estourar
# timeout nenhum, a thread da checagem nunca volta e — em modo auto, com a janela
# withdraw() — nada aparece na tela nem no log. Foi o que uma máquina apresentou
# em 14/08/2026: só a linha "iniciado", tentativa após tentativa.
WATCHDOG = 45  # segundos
# O download tem seu próprio vigia, e não podia ser o mesmo WATCHDOG: baixar 53 MB
# leva ~12 s numa rede boa, mas legitimamente minutos numa ruim, então desistir por
# tempo TOTAL cancelaria download que ia terminar. O que denuncia travamento é
# ficar sem UM byte novo — e isso o log não registrava: barra parada em 0 MB era
# indistinguível de app morto, e nenhuma linha saía do processo.
PARADO = 60  # segundos sem avanço nenhum no download


# ---------------------------------------------------------------------------
# Erros com mensagem própria para o usuário
# ---------------------------------------------------------------------------
class UpdaterError(Exception):
    """Erro com mensagem em PT-BR pronta para exibir."""


class NetworkError(UpdaterError):
    pass


class TokenError(UpdaterError):
    pass


class NoReleaseError(UpdaterError):
    pass


class AssetMissingError(UpdaterError):
    pass


class DownloadCorruptError(UpdaterError):
    pass


class AppRunningError(UpdaterError):
    pass


class CancelledError(UpdaterError):
    pass


# ---------------------------------------------------------------------------
# Caminhos e versão
# ---------------------------------------------------------------------------
def parse_version(s: str) -> tuple[int, ...]:
    """'v1.2.3' / '1.2.3' -> (1, 2, 3). Inválido -> (0,) (força oferta de update)."""
    try:
        return tuple(int(p) for p in s.strip().lstrip("vV").split("."))
    except (ValueError, AttributeError):
        return (0,)


def _exe_atual() -> Path | None:
    """Caminho do próprio Peticoes.exe, ou None fora do build (modo fonte)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return None


# ---------------------------------------------------------------------------
# Registro da checagem (mesmo arquivo/nome de antes, para não quebrar o hábito
# de diagnóstico já documentado: ler updater.log em ~/.automacao_processos).
# ---------------------------------------------------------------------------
def _caminho_log() -> Path:
    return APP_DATA_DIR / "updater.log"


def registrar(msg: str) -> None:
    """Anexa uma linha ao log da checagem. Nunca levanta."""
    try:
        caminho = _caminho_log()
        caminho.parent.mkdir(parents=True, exist_ok=True)
        linhas: list[str] = []
        if caminho.exists():
            try:
                linhas = caminho.read_text(encoding="utf-8", errors="replace").splitlines()[-199:]
            except OSError:
                linhas = []
        quando = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        linhas.append(f"{quando}  {msg}")
        caminho.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    except Exception:
        pass


def limpar_sobras() -> None:
    """Remove restos de uma atualização anterior interrompida (best-effort)."""
    alvo = _exe_atual()
    if alvo is None:
        return
    for sufixo in (".download", ".old"):
        try:
            alvo.with_name(alvo.name + sufixo).unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# GitHub API
# ---------------------------------------------------------------------------
def _request(url: str, accept: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Peticoes-Updater",
    })


class _AuthStrippingRedirect(urllib.request.HTTPRedirectHandler):
    """Remove o header Authorization quando o redirect troca de host.

    O download de asset responde 302 para objects.githubusercontent.com (URL
    assinada), que rejeita requisições com Authorization + assinatura na query.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlsplit(newurl).hostname != urlsplit(req.full_url).hostname:
            new.remove_header("Authorization")
        return new


_opener = urllib.request.build_opener(
    _AuthStrippingRedirect,
    urllib.request.HTTPSHandler(context=ssl.create_default_context()),
)


def _mapear_http_error(e: urllib.error.HTTPError) -> UpdaterError:
    if e.code in (401, 403):
        return TokenError(
            "O token de acesso do atualizador expirou ou é inválido.\n"
            "Avise o responsável para gerar uma nova versão do programa."
        )
    if e.code == 404:
        return NoReleaseError("Nenhuma versão foi publicada ainda.")
    return NetworkError(f"O GitHub respondeu com erro {e.code}. Tente novamente mais tarde.")


def fetch_latest_release() -> dict:
    """Retorna {"tag": "v1.2.0", "asset_url": str, "size": int}."""
    try:
        with _opener.open(_request(API_LATEST, "application/vnd.github+json"),
                          timeout=TIMEOUT) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        raise _mapear_http_error(e) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise NetworkError(
            "Não foi possível conectar à internet.\nVerifique a conexão e tente novamente."
        ) from e

    tag = data.get("tag_name", "")
    asset = next((a for a in data.get("assets", []) if a.get("name") == ASSET_NAME), None)
    if asset is None:
        raise AssetMissingError(
            f"A versão {tag} foi publicada sem o arquivo {ASSET_NAME}.\nAvise o responsável."
        )
    return {"tag": tag, "asset_url": asset["url"], "size": int(asset.get("size") or 0)}


def download_asset(asset_url: str, dest: Path, expected_size: int,
                   progress_cb, cancel: threading.Event) -> None:
    """Baixa o asset para dest, chamando progress_cb(baixado, total) por chunk."""
    try:
        req = _request(asset_url, "application/octet-stream")
        with _opener.open(req, timeout=TIMEOUT) as resp, open(dest, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or expected_size or 0)
            done = 0
            while True:
                if cancel.is_set():
                    raise CancelledError("Download cancelado.")
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                progress_cb(done, total)
        if expected_size and dest.stat().st_size != expected_size:
            raise DownloadCorruptError("O download veio incompleto. Tente novamente.")
    except urllib.error.HTTPError as e:
        dest.unlink(missing_ok=True)
        raise _mapear_http_error(e) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        dest.unlink(missing_ok=True)
        raise NetworkError(
            "A conexão caiu durante o download.\nVerifique a internet e tente novamente."
        ) from e
    except UpdaterError:
        dest.unlink(missing_ok=True)
        raise


def substituir_a_si_mesmo(novo: Path, alvo: Path) -> None:
    """Troca `alvo` (o PRÓPRIO exe em execução) por `novo`.

    Nunca escreve por cima de `alvo` — só renomeia. Confirmado ao vivo que o
    Windows permite renomear a imagem de um processo rodando (é o desvio pelo
    .old que já era usado quando um segundo processo fazia a troca; aqui quem
    renomeia é o próprio processo, mas a permissão do SO é a mesma).
    """
    old = alvo.with_name(alvo.name + ".old")
    old.unlink(missing_ok=True)
    try:
        os.rename(alvo, old)
        os.replace(novo, alvo)
    except OSError as e:
        raise AppRunningError(
            "Não consegui substituir o programa.\n"
            "Feche outras janelas do Organizador de Processos abertas e "
            "clique em \"Tentar novamente\"."
        ) from e
    try:
        old.unlink(missing_ok=True)
    except OSError:
        # Confirmado ao vivo: enquanto ESTE processo (a imagem que acabou de
        # ser renomeada) ainda está rodando, o Windows nega até um DELETE, não
        # só uma escrita — "Access is denied", não PermissionError silenciosa.
        # limpar_sobras() apaga na próxima abertura, quando o processo antigo
        # já saiu e soltou o arquivo.
        pass


# ---------------------------------------------------------------------------
# Interface (PT-BR) — Toplevel da janela principal, não um Tk() próprio: o
# app já tem seu mainloop rodando, então isto só participa dele.
# ---------------------------------------------------------------------------
def _fmt_mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f}".replace(".", ",")


class _JanelaAtualizacao(tk.Toplevel):  # pragma: no cover - janela Tk, como gui.py
    def __init__(self, master: tk.Misc, *, auto: bool) -> None:
        super().__init__(master)
        self._app = master
        self.auto = auto
        self.title("Atualização — Organizador de Processos")
        try:
            self.iconbitmap(str(CAMINHO_ICONE))
        except Exception:
            pass
        self.geometry("440x170")
        self.resizable(False, False)
        self.transient(master)
        self._centralizar()
        if auto:
            self.withdraw()

        self.eventos: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.release: dict | None = None
        self._baixando = False
        self._baixados = 0
        self._ultimo_avanco = 0.0
        self._avisou_parado = False

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        self.lbl = ttk.Label(frame, text="Verificando atualizações...",
                             justify="center", anchor="center", wraplength=400)
        self.lbl.pack(fill="x", pady=(4, 10))
        self.barra = ttk.Progressbar(frame, mode="indeterminate", length=400)
        self.barra.pack(fill="x", pady=(0, 12))
        self.botoes = ttk.Frame(frame)
        self.botoes.pack()

        self.protocol("WM_DELETE_WINDOW", self._fechar)

        limpar_sobras()
        if not GITHUB_TOKEN:
            registrar("ERRO: build sem token de acesso (gere pelo release.ps1)")
            if auto:
                self.destroy()
                return
            self.withdraw()
            messagebox.showerror(
                "Atualizador",
                "Este programa foi compilado sem o token de acesso ao\n"
                "repositório de atualizações. Gere uma nova versão pelo release.ps1.",
            )
            self.destroy()
            return

        if not auto:
            self.barra.start(12)
        self._respondeu = False
        threading.Thread(target=self._checar, daemon=True).start()
        self.after(100, self._drenar_eventos)
        # A thread é daemon: se ela pendurar de vez, isto desiste por ela e o
        # log passa a dizer POR QUE nada aconteceu (ver WATCHDOG).
        self.after(WATCHDOG * 1000, self._watchdog)

    # -- infra -------------------------------------------------------------
    def _centralizar(self) -> None:
        self.update_idletasks()
        w, h = 440, 170
        x = (self.winfo_screenwidth() - w) // 2
        y = (self.winfo_screenheight() - h) // 3
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _limpar_botoes(self) -> None:
        for filho in self.botoes.winfo_children():
            filho.destroy()

    def _botao(self, texto: str, comando, *, default: bool = False) -> None:
        b = ttk.Button(self.botoes, text=texto, command=comando, width=16)
        b.pack(side="left", padx=6)
        if default:
            b.focus_set()
            self.bind("<Return>", lambda _e: comando())

    def _fechar(self) -> None:
        self.cancel.set()
        self.destroy()

    def _drenar_eventos(self) -> None:
        try:
            while True:
                evento = self.eventos.get_nowait()
                evento()
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._drenar_eventos)

    def _post(self, fn) -> None:
        self.eventos.put(fn)

    def _watchdog(self) -> None:
        """Encerra a checagem que nunca volta, deixando rastro no log."""
        if self._respondeu or not self.winfo_exists():
            return
        self._mostrar_erro(NetworkError(
            f"Sem resposta em {WATCHDOG} segundos ao verificar a atualização.\n"
            "A rede desta máquina parece estar bloqueando o acesso ao GitHub "
            "(DNS ou proxy)."
        ))

    # -- estados -----------------------------------------------------------
    def _checar(self) -> None:
        # Marco no log ANTES da rede: separa "a thread nem nasceu / a janela
        # estourou no construtor" de "a chamada de rede pendurou", que produziam
        # exatamente o mesmo silêncio.
        registrar(f"consultando {urlsplit(API_LATEST).hostname}")
        try:
            release = fetch_latest_release()
        except UpdaterError as e:
            self._post(lambda: self._mostrar_erro(e))
            return
        except Exception as e:
            # Sem isto, qualquer exceção fora de UpdaterError (proxy corporativo
            # devolvendo HTML em vez de JSON, erro de SSL fora de OSError, resposta
            # da API sem os campos esperados etc.) mata esta thread em silêncio.
            self._post(lambda: self._mostrar_erro(
                NetworkError(f"Erro inesperado ao verificar atualização: {e}")
            ))
            return
        self._post(lambda: self._resultado_checagem(release))

    def _resultado_checagem(self, release: dict) -> None:
        self._respondeu = True
        self.release = release
        tem_update = parse_version(release["tag"]) > parse_version(__version__)
        registrar(
            f"checagem ok: instalada={__version__} publicada={release['tag']} "
            f"tem_update={tem_update} modo={'auto' if self.auto else 'manual'}"
        )
        if self.auto and not tem_update:
            self.destroy()  # silencioso: já está atualizado, não mostra nada
            return
        self.barra.stop()
        self.barra.configure(mode="determinate", value=0)
        if tem_update:
            if self.auto:
                self.deiconify()
                self.lift()
                self.focus_force()
            self.lbl.configure(
                text=f"Nova versão disponível: {release['tag']} "
                     f"(instalada: v{__version__}).\nDeseja atualizar agora?"
            )
            self._limpar_botoes()
            self._botao("Atualizar", self._comecar_download, default=True)
            self._botao("Agora não", self._fechar)
        else:
            self.lbl.configure(text=f"Você já está na versão mais recente (v{__version__}).")
            self._limpar_botoes()
            self._botao("Fechar", self._fechar, default=True)

    def _comecar_download(self) -> None:
        if _exe_atual() is None:
            messagebox.showinfo(
                "Atualização",
                "Rodando a partir do código-fonte: instalar não se aplica aqui.\n"
                "Isto só funciona no Peticoes.exe compilado.",
            )
            return
        self.unbind("<Return>")
        self.cancel.clear()
        self.lbl.configure(text="Baixando atualização...")
        self._limpar_botoes()
        self._botao("Cancelar", self._cancelar_download)
        self._baixando = True
        self._baixados = 0
        self._ultimo_avanco = time.monotonic()
        self._avisou_parado = False
        threading.Thread(target=self._baixar_e_instalar, daemon=True).start()
        self.after(PARADO * 1000, self._vigiar_download)

    def _cancelar_download(self) -> None:
        self.cancel.set()

    def _vigiar_download(self) -> None:
        """Denuncia no log o download que parou de avançar (sem cancelá-lo)."""
        if not self._baixando or not self.winfo_exists():
            return
        parado = time.monotonic() - self._ultimo_avanco
        if parado >= PARADO and not self._avisou_parado:
            self._avisou_parado = True
            registrar(
                f"download PARADO: {self._baixados} de {self.release['size']} bytes, "
                f"{parado:.0f}s sem avanço"
            )
        self.after(PARADO * 1000, self._vigiar_download)

    def _progresso(self, done: int, total: int) -> None:
        def aplicar() -> None:
            # No thread principal: é daqui que o vigia lê o avanço.
            self._baixados = done
            self._ultimo_avanco = time.monotonic()
            if total:
                self.barra.configure(maximum=total, value=done)
            self.lbl.configure(
                text=f"Baixando atualização... {_fmt_mb(done)} MB de {_fmt_mb(total)} MB"
            )
        self._post(aplicar)

    def _baixar_e_instalar(self) -> None:
        alvo = _exe_atual()
        destino = alvo.with_name(alvo.name + ".download")
        # Marco de entrada: sem ele, "o usuário nunca clicou em Atualizar" e "o
        # download começou e sumiu" davam o mesmo log — nada depois de
        # "checagem ok". O destino vai junto porque é ele que precisa de
        # permissão de escrita, e é a pasta que varia de máquina para máquina.
        registrar(f"baixando {self.release['tag']} ({self.release['size']} bytes) -> {destino}")
        try:
            download_asset(self.release["asset_url"], destino,
                           self.release["size"], self._progresso, self.cancel)
        except CancelledError:
            self._post(self._cancelado)
            return
        except UpdaterError as e:
            self._post(lambda: self._mostrar_erro(e))
            return
        except Exception as e:
            self._post(lambda: self._mostrar_erro(
                NetworkError(f"Erro inesperado ao baixar atualização: {e}")
            ))
            return
        self._post(lambda: self._instalar(destino, alvo))

    def _instalar(self, destino: Path, alvo: Path) -> None:
        self._baixando = False
        try:
            substituir_a_si_mesmo(destino, alvo)
        except AppRunningError as e:
            registrar(f"instalação bloqueada: {e}")
            self.lbl.configure(text=str(e))
            self.barra.configure(value=0)
            self._limpar_botoes()
            self._botao("Tentar novamente", lambda: self._instalar(destino, alvo), default=True)
            self._botao("Cancelar", lambda: self._abortar_instalacao(destino))
            return
        registrar(f"instalado {self.release['tag']}")
        self._concluido(alvo)

    def _abortar_instalacao(self, destino: Path) -> None:
        destino.unlink(missing_ok=True)
        self._fechar()

    def _cancelado(self) -> None:
        self._baixando = False
        registrar("download cancelado pelo usuário")
        self.lbl.configure(text="Atualização cancelada.")
        self.barra.configure(value=0)
        self._limpar_botoes()
        self._botao("Fechar", self._fechar, default=True)

    def _concluido(self, alvo: Path) -> None:
        versao = self.release["tag"]
        self.lbl.configure(text=f"Atualização para {versao} concluída! Reabrindo...")
        self.barra.configure(value=self.barra.cget("maximum") or 1)
        self._limpar_botoes()
        self.update_idletasks()
        try:
            os.startfile(str(alvo))  # noqa: S606 — abre a versão recém-instalada
        except OSError as exc:
            registrar(f"instalado, mas não consegui reabrir automaticamente: {exc}")
            messagebox.showinfo(
                "Atualização",
                f"Atualizado para {versao}.\nAbra o programa novamente para usar a versão nova.",
            )
        # Fecha o app inteiro (não só esta janela): a versão nova já foi aberta,
        # e a versão antiga em memória não pode continuar rodando ao lado dela.
        self._app._ao_fechar()

    def _mostrar_erro(self, erro: UpdaterError) -> None:
        # Sempre registra: em auto o erro não vai para a tela, e sem isto "token
        # expirado", "sem internet" e "já atualizado" ficam indistinguíveis.
        self._respondeu = True
        self._baixando = False
        registrar(f"ERRO na checagem ({type(erro).__name__}): {erro}")
        if self.auto:
            self.destroy()  # silencioso: não incomoda o usuário a cada abertura
            return
        self.barra.stop()
        self.barra.configure(mode="determinate", value=0)
        self.lbl.configure(text=str(erro))
        self._limpar_botoes()
        self._botao("Fechar", self._fechar, default=True)


def verificar(master: tk.Misc, *, auto: bool) -> None:
    """Abre a checagem de atualização como Toplevel de `master`.

    `auto=True`: silencioso (chamado na abertura do app) — só aparece se
    houver versão nova. `auto=False`: sempre mostra o resultado (botão
    "Verificar atualização" em Configurações).
    """
    registrar(f"iniciado modo={'auto' if auto else 'manual'} versao={__version__}")
    try:
        _JanelaAtualizacao(master, auto=auto)
    except Exception:
        # Sem isto, a janela morrendo no construtor é 100% silenciosa nas DUAS
        # vias: em auto o `contextlib.suppress(Exception)` de gui.py engole, e em
        # manual a exceção sobe para o report_callback_exception do Tk, que
        # imprime em stderr — inexistente no exe --windowed. O log parava no
        # "iniciado", indistinguível de uma thread pendurada na rede.
        registrar("ERRO ao abrir a janela de atualização:\n" + traceback.format_exc())
        raise
