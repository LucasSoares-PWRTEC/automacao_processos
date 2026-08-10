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
from datetime import datetime
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


# Versão informada pelo app que nos chamou (`--versao-atual`). É mais confiável
# que o version.txt: o app SABE sua versão (`__version__` compilado no exe),
# enquanto o version.txt é um arquivo à parte que pode ficar velho ou ser copiado
# sem o executável correspondente — e aí o Atualizador conclui "já está
# atualizado" e não mostra nada.
_versao_informada: str | None = None


def versao_instalada() -> str:
    if _versao_informada:
        return _versao_informada
    arq = base_dir() / "version.txt"
    try:
        v = arq.read_text(encoding="ascii").strip()
        return v or "0.0.0"
    except OSError:
        return "0.0.0"


# --------------------------------------------------------------------------
# Registro da checagem
# --------------------------------------------------------------------------
# No modo --auto TODO erro é silencioso de propósito (não incomodar a cada
# abertura). O efeito colateral é que "não aparece nada para atualizar" fica
# indistinguível de "já está na última versão", e não há como diagnosticar à
# distância: token expirado, sem rede, proxy bloqueando e Atualizador ausente
# produzem exatamente o mesmo silêncio. Este log resolve isso sem incomodar.
def _caminho_log() -> Path:
    base = os.environ.get("ORGANIZADOR_APP_DATA")
    raiz = Path(base) if base else Path.home() / ".automacao_processos"
    return raiz / "updater.log"


def registrar(msg: str) -> None:
    """Anexa uma linha ao log da checagem. Nunca levanta."""
    try:
        caminho = _caminho_log()
        caminho.parent.mkdir(parents=True, exist_ok=True)
        # Mantém o arquivo pequeno: só as últimas ~200 linhas interessam.
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
            registrar(
                "ERRO: Atualizador compilado SEM token de acesso "
                "(gere o executável pelo release.ps1)"
            )
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
        except Exception as e:
            # Sem isto, qualquer exceção fora das UpdaterError (proxy corporativo
            # devolvendo HTML em vez de JSON, erro de SSL não-OSError, resposta da
            # API sem os campos esperados etc.) mata esta thread em silêncio: o
            # log fica só no "iniciado", a janela trava em "Verificando..." (modo
            # manual) ou nunca aparece (--auto), e não há erro nenhum pra investigar.
            self._post(lambda: self._mostrar_erro(
                NetworkError(f"Erro inesperado ao verificar atualização: {e}")
            ))
            return
        self._post(lambda: self._resultado_checagem(release))

    def _resultado_checagem(self, release: dict) -> None:
        self.release = release
        local = versao_instalada()
        tem_update = parse_version(release["tag"]) > parse_version(local)
        registrar(
            f"checagem ok: instalada={local} publicada={release['tag']} "
            f"tem_update={tem_update} modo={'auto' if self.auto else 'manual'}"
        )
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
        except Exception as e:
            # Mesma lacuna de _checar: sem isto o download trava a thread em
            # silêncio numa exceção que não é UpdaterError.
            self._post(lambda: self._mostrar_erro(
                NetworkError(f"Erro inesperado ao baixar atualização: {e}")
            ))
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
            registrar(f"instalação bloqueada: {e}")
            self.lbl.configure(text=str(e))
            self.barra.configure(value=0)
            self._limpar_botoes()
            self._botao("Tentar novamente", lambda: self._instalar(destino, alvo), default=True)
            self._botao("Cancelar", lambda: self._abortar_instalacao(destino))
            return

        # Gravar o version.txt NÃO pode derrubar a instalação — o executável já
        # foi trocado com sucesso neste ponto. Mas a falha precisa aparecer: sem
        # este arquivo atualizado, um Atualizador rodado à mão (sem
        # `--versao-atual`) volta a oferecer a MESMA versão em toda execução, o
        # que a pessoa vê como "fica verificando/atualizando infinitamente".
        nova = self.release["tag"].lstrip("vV")
        try:
            (base_dir() / "version.txt").write_text(nova, encoding="ascii")
            registrar(f"instalado {nova}; version.txt atualizado em {base_dir()}")
        except OSError as exc:
            registrar(
                f"ATENÇÃO: instalado {nova}, mas NÃO consegui gravar o "
                f"version.txt em {base_dir()} ({exc}). Rodado à mão, o "
                "Atualizador vai oferecer esta versão de novo."
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
        # Sempre registra: em --auto o erro não vai para a tela, e sem isto
        # "token expirado", "sem internet" e "já atualizado" ficam indistinguíveis.
        registrar(f"ERRO na checagem ({type(erro).__name__}): {erro}")
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


def _arg_str(argv: list[str], flag: str) -> str | None:
    """Lê o valor que segue `flag` em argv (ex.: --versao-atual 1.9.3)."""
    if flag in argv:
        try:
            valor = argv[argv.index(flag) + 1].strip()
            return valor or None
        except IndexError:
            return None
    return None


def main() -> None:
    global _versao_informada

    argv = sys.argv[1:]
    auto = "--auto" in argv
    parent_pid = _arg_int(argv, "--parent-pid")
    # O app passa a própria versão; é mais confiável que o version.txt (ver
    # `versao_instalada`). Sem o argumento, cai no arquivo, como antes.
    _versao_informada = _arg_str(argv, "--versao-atual")
    registrar(
        f"iniciado modo={'auto' if auto else 'manual'} "
        f"versao_informada={_versao_informada or '(nenhuma, usa version.txt)'}"
    )
    AtualizadorApp(auto=auto, parent_pid=parent_pid).iniciar()


if __name__ == "__main__":
    main()
