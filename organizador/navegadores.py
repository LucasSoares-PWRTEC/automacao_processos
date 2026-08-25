# -*- coding: utf-8 -*-
"""Ciclo de vida dos PROCESSOS de navegador — o que o Selenium não garante.

POR QUE ESTE MÓDULO EXISTE
--------------------------
``driver.quit()`` funciona, mas não é instantâneo: medido nesta máquina, um
Chrome headless leva **2,1 s** para morrer. O app fechava a janela em
milissegundos e chamava o ``quit()`` em threads *daemon* — que o interpretador
mata na saída do processo, no meio do caminho. O que sobrava (reproduzido ao
vivo em 25/08/2026): ``chromedriver.exe`` com o pai já morto e 8 ``chrome.exe``
pendurados nele, **491 MB** invisíveis (headless não tem janela nem ícone na
barra), vivos até o reboot. Era a "lentidão no PC depois de usar o programa".

O rastro em disco confirmava o histórico: 6 pastas ``scoped_dir*`` de 100-130 MB
no ``%TEMP%`` (o perfil temporário que o chromedriver apaga no ``quit()`` limpo —
e só nele).

Três peças, então:

1. **Registro** dos navegadores vivos deste processo (``registrar``/``esquecer``).
   O ``quit()`` educado é sempre a primeira tentativa; o registro é a rede de
   segurança para o que não morreu no prazo — e para os navegadores presos em
   threads worker, que o fechamento da janela não tem como alcançar.
2. **Varredura de órfãos** de execuções anteriores (``varrer_orfaos``), porque o
   estrago já feito não se desfaz sozinho.
3. **Limpeza dos perfis temporários** que sobram de um navegador morto à força.

SÓ STDLIB, DE PROPÓSITO
-----------------------
Nada de selenium/psutil aqui: este módulo é importado pela GUI na abertura, e
``tests/test_abertura_leve.py`` proíbe (corretamente) que a janela pague o
import do Selenium. A inspeção de processos vai por ``powershell`` +
``taskkill``, que existem em qualquer Windows.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from typing import NamedTuple, Optional

# Janela oculta: sem isto cada chamada pisca um console preto na cara do usuário.
_SEM_JANELA = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_EXES_DRIVER = ("chromedriver.exe", "msedgedriver.exe", "geckodriver.exe")
_EXES_NAVEGADOR = ("chrome.exe", "msedge.exe", "firefox.exe")

# Marcas, na linha de comando do navegador, de uma sessão dirigida por driver:
#   scoped_dir<PID>_<rnd>  -> perfil temporário criado pelo chromedriver/msedgedriver
#   rust_mozprofile        -> idem, geckodriver
#   automacao_processos    -> nosso perfil PERSISTENTE (o do Autojur)
# A marca é o que separa "sessão de automação abandonada" de um chromedriver que
# alguém subiu à mão para outra finalidade: sem ela, a varredura poderia matar a
# ferramenta de outro projeto desta máquina (e há vários aqui).
_MARCAS_SESSAO = ("scoped_dir", "rust_mozprofile", "automacao_processos")

# Prefixo das pastas de perfil temporário no %TEMP%.
_PREFIXO_PERFIL_TEMP = "scoped_dir"

# Idade mínima para um perfil temporário ser candidato a lixo. Os dois filtros
# (idade E não citado por navegador vivo) precisam passar: apagar por dentro o
# perfil de um navegador VIVO corromperia a sessão dele — é a mesma lição do
# `rmtree` das pastas _MEI (ver gui._mei_em_uso).
_IDADE_PERFIL_S = 900  # 15 min


# --------------------------------------------------------------------------
# Registro dos navegadores vivos deste processo
# --------------------------------------------------------------------------
# Chave: PID do processo do DRIVER (chromedriver.exe), não do navegador. É ele
# que o Selenium sobe como filho direto, e matar a árvore dele (`taskkill /T`)
# leva o navegador inteiro junto — verificado ao vivo.
_lock = threading.Lock()
_vivos: "dict[int, str]" = {}


def registrar(pid: int, rotulo: str = "") -> None:
    """Anota um driver vivo. Idempotente."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return
    if pid <= 0:
        return
    with _lock:
        _vivos[pid] = rotulo


def esquecer(pid: Optional[int]) -> None:
    """Tira o driver do registro (ele morreu pelo caminho educado)."""
    if pid is None:
        return
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return
    with _lock:
        _vivos.pop(pid, None)


def registrados() -> list[int]:
    with _lock:
        return sorted(_vivos)


def matar_arvore(pid: int) -> bool:
    """``taskkill /F /T`` no PID. True se o Windows disse que matou.

    ``/T`` é o que importa: o chromedriver é o pai da árvore do navegador, e sem
    ele os ``chrome.exe`` filhos continuariam vivos."""
    if os.name != "nt":
        return False
    try:
        proc = subprocess.run(
            ["taskkill", "/PID", str(int(pid)), "/F", "/T"],
            capture_output=True, timeout=20, creationflags=_SEM_JANELA,
        )
        return proc.returncode == 0
    except Exception:
        return False


def matar_registrados() -> int:
    """Mata à força todo navegador ainda registrado. Devolve quantos morreram.

    Último recurso do fechamento do app: cobre o navegador cujo ``quit()`` não
    terminou no prazo E o que está preso numa thread worker (busca online,
    passada de monitoramento), fora do alcance de quem fecha a janela."""
    with _lock:
        pids = sorted(_vivos)
        _vivos.clear()
    return sum(1 for pid in pids if matar_arvore(pid))


# --------------------------------------------------------------------------
# Inspeção de processos (uma ida ao PowerShell)
# --------------------------------------------------------------------------
class Proc(NamedTuple):
    pid: int
    ppid: int
    nome: str        # minúsculo, com extensão
    cmd: str         # minúsculo (só é usado para procurar marcas)


# Uma chamada só: `Get-CimInstance Win32_Process` percorre a tabela inteira de
# processos (1-3 s e ~60 MB de PowerShell num i3), então ela paga por tudo —
# a lista de PIDs vivos (para saber quem ficou sem pai) e as linhas de comando
# dos navegadores (para saber de quem é a sessão).
_PS_PROCESSOS = (
    "$ErrorActionPreference='SilentlyContinue';"
    "$p = Get-CimInstance Win32_Process;"
    "'V:' + (($p | ForEach-Object { $_.ProcessId }) -join ',');"
    "$alvo = @(@ALVOS@);"
    "$p | Where-Object { $alvo -contains $_.Name.ToLower() } | ForEach-Object {"
    " 'P:' + $_.ProcessId + '|' + $_.ParentProcessId + '|' + $_.Name + '|'"
    " + ($_.CommandLine -replace '\\s+',' ') }"
)


def _comando_listagem() -> str:
    # Marcador @ALVOS@ em vez de `str.format`/f-string: o script está cheio de
    # chaves do PowerShell, que as duas rotas tentariam interpretar.
    alvos = ",".join(f"'{n}'" for n in _EXES_DRIVER + _EXES_NAVEGADOR)
    return _PS_PROCESSOS.replace("@ALVOS@", alvos)


def _analisar_listagem(saida: str) -> tuple[set[int], list[Proc]]:
    """Converte a saída do PowerShell em (PIDs vivos, processos de navegador)."""
    vivos: set[int] = set()
    procs: list[Proc] = []
    for linha in saida.splitlines():
        linha = linha.strip()
        if linha.startswith("V:"):
            for parte in linha[2:].split(","):
                parte = parte.strip()
                if parte.isdigit():
                    vivos.add(int(parte))
        elif linha.startswith("P:"):
            campos = linha[2:].split("|", 3)
            if len(campos) < 3:
                continue
            pid, ppid, nome = campos[0].strip(), campos[1].strip(), campos[2].strip()
            cmd = campos[3] if len(campos) > 3 else ""
            if not pid.isdigit():
                continue
            procs.append(Proc(
                pid=int(pid),
                ppid=int(ppid) if ppid.isdigit() else 0,
                nome=nome.lower(),
                cmd=cmd.lower(),
            ))
    return vivos, procs


def listar_processos() -> Optional[tuple[set[int], list[Proc]]]:
    """(PIDs vivos, processos de navegador/driver). None se não deu para listar.

    None é diferente de "nada encontrado": sem a listagem NADA é apagado nem
    morto — na dúvida sobra lixo, que é barato, em vez de matar sessão viva."""
    if os.name != "nt":
        return None
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _comando_listagem()],
            capture_output=True, text=True, timeout=60, creationflags=_SEM_JANELA,
        )
    except Exception:
        return None
    if proc.returncode != 0 and not proc.stdout:
        return None
    vivos, procs = _analisar_listagem(proc.stdout or "")
    if not vivos:
        return None  # listagem vazia = falhou (o próprio PowerShell estaria nela)
    return vivos, procs


# --------------------------------------------------------------------------
# Órfãos: driver sem pai, com sessão de navegador pendurada
# --------------------------------------------------------------------------
def _tem_marca_de_sessao(cmd: str) -> bool:
    return any(marca in cmd for marca in _MARCAS_SESSAO)


def pids_orfaos(
    vivos: set[int], procs: "list[Proc]", protegidos: "set[int]" = frozenset(),
) -> list[int]:
    """PIDs de driver comprovadamente abandonados.

    Dois requisitos, os dois necessários:

    1. **o pai morreu** — quem dirigia o navegador não existe mais, então
       ninguém vai fechá-lo. É este filtro que protege uma SEGUNDA instância do
       app rodando agora: o driver dela tem pai vivo (o app) e é ignorado.
    2. **há navegador pendurado com marca de sessão de automação** — separa o
       órfão de verdade de um ``chromedriver.exe`` que alguém subiu à mão como
       servidor (o pai dele pode ter saído sem que ele seja lixo).

    ``protegidos``: PIDs registrados por ESTE processo, que nunca são tocados
    aqui (o caminho deles é o ``quit()``)."""
    filhos: "dict[int, list[Proc]]" = {}
    for p in procs:
        filhos.setdefault(p.ppid, []).append(p)

    orfaos: list[int] = []
    for p in procs:
        if p.nome not in _EXES_DRIVER or p.pid in protegidos:
            continue
        if p.ppid in vivos:
            continue  # tem dono vivo
        navegadores = [
            f for f in filhos.get(p.pid, ())
            if f.nome in _EXES_NAVEGADOR and _tem_marca_de_sessao(f.cmd)
        ]
        if navegadores:
            orfaos.append(p.pid)
    return orfaos


def perfis_temp_orfaos(
    procs: "list[Proc]", raiz: str, ignorar_pids: "set[int]" = frozenset(),
    idade_minima_s: int = _IDADE_PERFIL_S,
) -> list[str]:
    """Pastas ``scoped_dir*`` em `raiz` que nenhum navegador vivo está usando.

    O chromedriver cria uma pasta NOVA por sessão e nunca reaproveita uma
    existente, então uma pasta que ninguém cita é lixo por definição. O filtro
    de idade é redundância deliberada, para a janela entre "pasta criada" e
    "navegador com a flag na linha de comando".

    `ignorar_pids`: donos que acabamos de matar — o perfil deles JÁ é lixo,
    ainda que a listagem (tirada antes) os cite."""
    em_uso: list[str] = [
        p.cmd for p in procs
        if p.nome in _EXES_NAVEGADOR and p.pid not in ignorar_pids
        and p.ppid not in ignorar_pids
    ]
    agora = time.time()
    achados: list[str] = []
    try:
        nomes = os.listdir(raiz)
    except OSError:
        return achados
    for nome in nomes:
        if not nome.startswith(_PREFIXO_PERFIL_TEMP):
            continue
        caminho = os.path.join(raiz, nome)
        if not os.path.isdir(caminho):
            continue
        alvo = nome.lower()
        if any(alvo in cmd for cmd in em_uso):
            continue
        try:
            if (agora - os.stat(caminho).st_mtime) < idade_minima_s:
                continue
        except OSError:
            continue
        achados.append(caminho)
    return achados


def varrer_orfaos() -> tuple[int, int]:
    """Mata navegadores órfãos de execuções anteriores e apaga os perfis
    temporários que ninguém usa. Devolve ``(processos mortos, perfis apagados)``.

    Roda na abertura, em thread, e nunca levanta: uma limpeza não pode impedir o
    app de abrir."""
    import tempfile

    try:
        listagem = listar_processos()
    except Exception:
        return 0, 0
    if listagem is None:
        return 0, 0
    vivos, procs = listagem

    mortos: list[int] = []
    try:
        for pid in pids_orfaos(vivos, procs, protegidos=set(registrados())):
            if matar_arvore(pid):
                mortos.append(pid)
    except Exception:
        pass

    apagados = 0
    try:
        for caminho in perfis_temp_orfaos(
            procs, tempfile.gettempdir(), ignorar_pids=set(mortos),
        ):
            shutil.rmtree(caminho, ignore_errors=True)
            if not os.path.isdir(caminho):
                apagados += 1
    except Exception:
        pass
    return len(mortos), apagados
