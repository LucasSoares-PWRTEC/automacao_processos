# -*- coding: utf-8 -*-
r"""Atualizador do Organizador de Processos (Peticoes.exe).

Verifica a release mais recente no repositório privado do GitHub e, com a
confirmação do usuário, baixa o Peticoes.exe novo e substitui o instalado.

Funciona ao lado do Peticoes.exe (mesma pasta) e mantém um version.txt com a
versão instalada. Usa apenas a stdlib (urllib/tkinter) para o exe ficar pequeno.

Teste em modo fonte:
    $env:PETICOES_UPDATER_TOKEN = (Get-Content secrets\token_github.txt)
    python updater\updater.py
"""
from __future__ import annotations

import json
import os
import queue
import signal
import ssl
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from pathlib import Path
from tkinter import messagebox, ttk
from urllib.parse import urlsplit

def _caminho_icone() -> Path:
    """Ícone da janela — mesmo arquivo embutido no Peticoes.exe (ver *.spec)."""
    raiz = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return raiz / "icone.ico"


REPO = "LucasTalen/automacao_processos"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET_NAME = "Peticoes.exe"
TIMEOUT = 30  # segundos (conexão/socket)
CHUNK = 1024 * 1024  # 1 MiB

try:
    from _token import GITHUB_TOKEN  # gerado pelo release.ps1; NUNCA commitado
except ImportError:
    GITHUB_TOKEN = os.environ.get("PETICOES_UPDATER_TOKEN", "")


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
def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def parse_version(s: str) -> tuple[int, ...]:
    """'v1.2.3' / '1.2.3' -> (1, 2, 3). Inválido -> (0,) (força oferta de update)."""
    try:
        return tuple(int(p) for p in s.strip().lstrip("vV").split("."))
    except (ValueError, AttributeError):
        return (0,)


def versao_instalada() -> str:
    arq = base_dir() / "version.txt"
    try:
        v = arq.read_text(encoding="ascii").strip()
        return v or "0.0.0"
    except OSError:
        return "0.0.0"


def limpar_sobras() -> None:
    """Remove restos de updates anteriores (best-effort)."""
    for nome in (f"{ASSET_NAME}.download", f"{ASSET_NAME}.old"):
        try:
            (base_dir() / nome).unlink(missing_ok=True)
        except OSError:
            pass


def _processo_vivo(pid: int) -> bool:
    """True se o processo `pid` ainda existe (Windows; best-effort).

    Não uso os.kill(pid, 0) porque no Windows QUALQUER sinal != CTRL_* chama
    TerminateProcess — ou seja, mataria o processo em vez de só checar.
    """
    try:
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False  # não abriu → provavelmente já saiu
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return False


def encerrar_processo(pid: int, espera_s: float = 3.0) -> None:
    """Encerra o processo `pid` (a versão ANTIGA do app que disparou o updater)
    e aguarda até `espera_s` o SO liberar o lock do .exe. Best-effort: nunca
    levanta — se o PID já saiu/é inválido, apenas retorna."""
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGTERM)  # no Windows: TerminateProcess
    except (OSError, ProcessLookupError, ValueError):
        return
    deadline = time.monotonic() + espera_s
    while time.monotonic() < deadline:
        if not _processo_vivo(pid):
            return
        time.sleep(0.1)


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
            "Avise o responsável para gerar um novo Atualizador.exe."
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


def replace_exe(new: Path, target: Path) -> None:
    """Substitui target por new. Um exe rodando não pode ser sobrescrito,
    mas pode ser renomeado no mesmo volume — daí o desvio pelo .old."""
    old = target.with_suffix(".exe.old")
    try:
        os.replace(new, target)
        return
    except PermissionError:
        pass
    except FileNotFoundError:
        pass  # target não existe ainda (instalação nova): cai no caminho abaixo
    try:
        old.unlink(missing_ok=True)
        if target.exists():
            os.rename(target, old)
        os.replace(new, target)
        try:
            old.unlink(missing_ok=True)
        except OSError:
            pass  # app antigo ainda aberto segura o .old; some no próximo update
    except (PermissionError, OSError) as e:
        raise AppRunningError(
            "O programa Peticoes está aberto.\nFeche-o e clique em \"Tentar novamente\"."
        ) from e


# ---------------------------------------------------------------------------
# Interface (PT-BR)
# ---------------------------------------------------------------------------
def _fmt_mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f}".replace(".", ",")


class AtualizadorApp:
    def __init__(self, auto: bool = False, parent_pid: int | None = None) -> None:
        # auto=True: modo silencioso (disparado pelo app na abertura). A janela
        # fica oculta durante a checagem e só aparece se houver versão nova;
        # quando já está atualizado ou ocorre erro, sai sem incomodar o usuário.
        # parent_pid: PID da instância do app que disparou o updater — encerrada
        # antes de trocar o exe, para não sobrar a versão antiga aberta.
        self.auto = auto
        self.parent_pid = parent_pid
        self.root = tk.Tk()
        self.root.title("Atualizador — Organizador de Processos")
        try:
            self.root.iconbitmap(str(_caminho_icone()))
        except Exception:
            pass
        self.root.geometry("440x170")
        self.root.resizable(False, False)
        self._centralizar()
        if auto:
            self.root.withdraw()

        self.eventos: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.release: dict | None = None

        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill="both", expand=True)
        self.lbl = ttk.Label(frame, text="Verificando atualizações...",
                             justify="center", anchor="center", wraplength=400)
        self.lbl.pack(fill="x", pady=(4, 10))
        self.barra = ttk.Progressbar(frame, mode="indeterminate", length=400)
        self.barra.pack(fill="x", pady=(0, 12))
        self.botoes = ttk.Frame(frame)
        self.botoes.pack()

        self.root.protocol("WM_DELETE_WINDOW", self._fechar)

    # -- infra -------------------------------------------------------------
    def _centralizar(self) -> None:
        self.root.update_idletasks()
        w, h = 440, 170
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 3
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _limpar_botoes(self) -> None:
        for filho in self.botoes.winfo_children():
            filho.destroy()

    def _botao(self, texto: str, comando, *, default: bool = False) -> None:
        b = ttk.Button(self.botoes, text=texto, command=comando, width=16)
        b.pack(side="left", padx=6)
        if default:
            b.focus_set()
            self.root.bind("<Return>", lambda _e: comando())

    def _fechar(self) -> None:
        self.cancel.set()
        self.root.destroy()

    def _em_thread(self, alvo) -> None:
        threading.Thread(target=alvo, daemon=True).start()

    def _drenar_eventos(self) -> None:
        try:
            while True:
                evento = self.eventos.get_nowait()
                evento()
        except queue.Empty:
            pass
        self.root.after(100, self._drenar_eventos)

    def _post(self, fn) -> None:
        self.eventos.put(fn)

    # -- estados -----------------------------------------------------------
    def iniciar(self) -> None:
        limpar_sobras()
        if not GITHUB_TOKEN:
            if self.auto:
                self.root.destroy()  # silencioso: nada a fazer sem token
                return
            self.root.withdraw()
            messagebox.showerror(
                "Atualizador",
                "Este Atualizador foi compilado sem o token de acesso.\n"
                "Gere o executável pelo release.ps1.",
            )
            self.root.destroy()
            return
        if not self.auto:
            self.barra.start(12)
        self._em_thread(self._checar)
        self.root.after(100, self._drenar_eventos)
        self.root.mainloop()

    def _checar(self) -> None:
        try:
            release = fetch_latest_release()
        except UpdaterError as e:
            self._post(lambda: self._mostrar_erro(e))
            return
        self._post(lambda: self._resultado_checagem(release))

    def _resultado_checagem(self, release: dict) -> None:
        self.release = release
        local = versao_instalada()
        tem_update = parse_version(release["tag"]) > parse_version(local)
        if self.auto and not tem_update:
            self.root.destroy()  # silencioso: já está atualizado, não mostra nada
            return
        self.barra.stop()
        self.barra.configure(mode="determinate", value=0)
        if tem_update:
            if self.auto:
                self.root.deiconify()
                self.root.lift()
                self.root.focus_force()
            self.lbl.configure(
                text=f"Nova versão disponível: {release['tag']} (instalada: v{local}).\n"
                     "Deseja atualizar agora?"
            )
            self._limpar_botoes()
            self._botao("Atualizar", self._comecar_download, default=True)
            self._botao("Agora não", self._fechar)
        else:
            self.lbl.configure(text=f"Você já está na versão mais recente (v{local}).")
            self._limpar_botoes()
            self._botao("Fechar", self._fechar, default=True)

    def _comecar_download(self) -> None:
        self.root.unbind("<Return>")
        self.cancel.clear()
        self.lbl.configure(text="Baixando atualização...")
        self._limpar_botoes()
        self._botao("Cancelar", self._cancelar_download)
        self._em_thread(self._baixar_e_instalar)

    def _cancelar_download(self) -> None:
        self.cancel.set()

    def _progresso(self, done: int, total: int) -> None:
        def aplicar() -> None:
            if total:
                self.barra.configure(maximum=total, value=done)
            self.lbl.configure(
                text=f"Baixando atualização... {_fmt_mb(done)} MB de {_fmt_mb(total)} MB"
            )
        self._post(aplicar)

    def _baixar_e_instalar(self) -> None:
        destino = base_dir() / f"{ASSET_NAME}.download"
        alvo = base_dir() / ASSET_NAME
        try:
            download_asset(self.release["asset_url"], destino,
                           self.release["size"], self._progresso, self.cancel)
        except CancelledError:
            self._post(self._cancelado)
            return
        except UpdaterError as e:
            self._post(lambda: self._mostrar_erro(e))
            return
        self._post(lambda: self._instalar(destino, alvo))

    def _instalar(self, destino: Path, alvo: Path) -> None:
        # Encerra a versão ANTIGA do app ANTES de trocar o exe: assim o
        # os.replace funciona direto (sem deixar Peticoes.exe.old preso pelo
        # processo aberto) e não sobram duas instâncias após a atualização.
        if self.parent_pid:
            self.lbl.configure(text="Finalizando: fechando a versão anterior...")
            self.root.update_idletasks()
            encerrar_processo(self.parent_pid)
        try:
            replace_exe(destino, alvo)
        except AppRunningError as e:
            self.lbl.configure(text=str(e))
            self.barra.configure(value=0)
            self._limpar_botoes()
            self._botao("Tentar novamente", lambda: self._instalar(destino, alvo), default=True)
            self._botao("Cancelar", lambda: self._abortar_instalacao(destino))
            return
        (base_dir() / "version.txt").write_text(
            self.release["tag"].lstrip("vV"), encoding="ascii"
        )
        self._concluido(alvo)

    def _abortar_instalacao(self, destino: Path) -> None:
        destino.unlink(missing_ok=True)
        self._fechar()

    def _cancelado(self) -> None:
        self.lbl.configure(text="Atualização cancelada.")
        self.barra.configure(value=0)
        self._limpar_botoes()
        self._botao("Fechar", self._fechar, default=True)

    def _concluido(self, alvo: Path) -> None:
        versao = self.release["tag"]
        self.lbl.configure(text=f"Atualização para {versao} concluída!")
        self.barra.configure(value=self.barra.cget("maximum") or 1)
        self._limpar_botoes()
        if self.parent_pid:
            # Auto-update: a versão antiga já foi encerrada — reabre direto na
            # nova (sem perguntar; senão o usuário ficaria sem nenhuma janela).
            os.startfile(alvo)  # noqa: S606 — abre o app recém-instalado
        elif messagebox.askyesno("Atualizador", "Atualização concluída!\nAbrir o programa agora?"):
            os.startfile(alvo)  # noqa: S606 — abre o app recém-instalado
        self.root.destroy()

    def _mostrar_erro(self, erro: UpdaterError) -> None:
        if self.auto:
            self.root.destroy()  # silencioso: não incomoda o usuário a cada abertura
            return
        self.barra.stop()
        self.barra.configure(mode="determinate", value=0)
        self.lbl.configure(text=str(erro))
        self._limpar_botoes()
        self._botao("Fechar", self._fechar, default=True)


def _arg_int(argv: list[str], flag: str) -> int | None:
    """Lê o valor inteiro que segue `flag` em argv (ex.: --parent-pid 1234)."""
    if flag in argv:
        try:
            return int(argv[argv.index(flag) + 1])
        except (ValueError, IndexError):
            return None
    return None


def main() -> None:
    argv = sys.argv[1:]
    auto = "--auto" in argv
    parent_pid = _arg_int(argv, "--parent-pid")
    AtualizadorApp(auto=auto, parent_pid=parent_pid).iniciar()


if __name__ == "__main__":
    main()
