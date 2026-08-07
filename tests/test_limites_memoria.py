# -*- coding: utf-8 -*-
"""Estruturas que cresciam sem limite durante o uso prolongado.

Numa máquina de 4 GB o problema não é só a abertura: uma passada de
monitoramento (1.600 processos, até 11 linhas de log cada) deixava 6-17 mil
linhas presas nos widgets de log, e navegar pela lista retinha um `Processo`
completo — com todos os eventos — para cada linha já clicada.

A GUI em si não é testável aqui (precisa de display e interação), então os
testes atacam as peças isoladas: a fila de log com teto e a política LRU. O
`_append_log` é exercitado contra um `tk.Text` de verdade quando há display.
"""

from __future__ import annotations

import queue
import threading
from collections import OrderedDict

import pytest

from organizador.scraper import fila_de_log


# ---------------------------------------------------------------------
# Fila de log com teto
# ---------------------------------------------------------------------
def test_fila_respeita_o_teto():
    q = fila_de_log(maxsize=3)
    for i in range(10):
        q.put(f"linha {i}")
    assert q.qsize() == 3


def test_fila_descarta_as_mais_antigas():
    """Descartar o começo é o certo: quem olha o log quer as últimas linhas."""
    q = fila_de_log(maxsize=3)
    for i in range(6):
        q.put(f"linha {i}")
    restante = [q.get_nowait() for _ in range(3)]
    assert restante == ["linha 3", "linha 4", "linha 5"]


def test_fila_nao_bloqueia_o_worker():
    """`put` bloqueante numa fila cheia congelaria a thread de scraping."""
    q = fila_de_log(maxsize=1)
    terminou = threading.Event()

    def produzir():
        for _ in range(20_000):
            q.put("x")
        terminou.set()

    t = threading.Thread(target=produzir, daemon=True)
    t.start()
    assert terminou.wait(timeout=30), "put() bloqueou com a fila cheia"


def test_fila_nao_levanta_full():
    q = fila_de_log(maxsize=2)
    for _ in range(50):
        q.put("x")  # não pode levantar queue.Full
    assert q.qsize() == 2


def test_put_nowait_continua_funcionando():
    """`queue.Queue.put_nowait` é implementado como `put(block=False)`; a
    sobrescrita de `put` não pode virar recursão infinita."""
    q = fila_de_log(maxsize=2)
    q.put_nowait("a")
    q.put_nowait("b")
    q.put_nowait("c")  # estoura o teto: descarta o mais antigo
    assert [q.get_nowait(), q.get_nowait()] == ["b", "c"]


def test_fila_vazia_se_comporta_normalmente():
    q = fila_de_log(maxsize=5)
    with pytest.raises(queue.Empty):
        q.get_nowait()
    q.put("a")
    assert q.get_nowait() == "a"


# ---------------------------------------------------------------------
# LRU do cache de Processos completos
# ---------------------------------------------------------------------
# Réplica da política de App._memorizar_processo (a GUI não é instanciável aqui).
def _memorizar(cache: OrderedDict, teto: int, chave: str, valor) -> None:
    if not chave:
        return
    cache[chave] = valor
    cache.move_to_end(chave)
    while len(cache) > teto:
        cache.popitem(last=False)


def test_cache_respeita_o_teto():
    cache: OrderedDict = OrderedDict()
    for i in range(100):
        _memorizar(cache, 10, f"proc{i}", object())
    assert len(cache) == 10


def test_cache_descarta_o_menos_usado_recentemente():
    cache: OrderedDict = OrderedDict()
    for nome in ("a", "b", "c"):
        _memorizar(cache, 3, nome, nome)
    cache.move_to_end("a")          # "a" foi consultado de novo
    _memorizar(cache, 3, "d", "d")  # estoura o teto
    assert "b" not in cache, "descartou o errado"
    assert set(cache) == {"a", "c", "d"}


def test_cache_ignora_numero_vazio():
    cache: OrderedDict = OrderedDict()
    _memorizar(cache, 5, "", object())
    assert len(cache) == 0


def test_cache_reinsercao_nao_duplica():
    cache: OrderedDict = OrderedDict()
    _memorizar(cache, 5, "a", 1)
    _memorizar(cache, 5, "a", 2)
    assert len(cache) == 1 and cache["a"] == 2


# ---------------------------------------------------------------------
# Poda do widget de log (precisa de display)
# ---------------------------------------------------------------------
@pytest.fixture
def texto_tk():
    tk = pytest.importorskip("tkinter")
    try:
        raiz = tk.Tk()
        raiz.withdraw()
    except tk.TclError:
        pytest.skip("sem display para criar um widget Tk")
    widget = tk.Text(raiz)
    yield widget
    raiz.destroy()


def _append(widget, mensagens, teto):
    """Mesma lógica de App._append_log, sem depender da classe App."""
    if not mensagens:
        return
    widget.configure(state="normal")
    widget.insert("end", "".join(mensagens))
    total = int(widget.index("end-1c").split(".")[0])
    if total > teto:
        widget.delete("1.0", f"{total - teto + 1}.0")
    widget.see("end")
    widget.configure(state="disabled")


def test_log_poda_no_teto_e_preserva_o_fim(texto_tk):
    _append(texto_tk, [f"linha {i}\n" for i in range(5000)], teto=100)
    conteudo = texto_tk.get("1.0", "end-1c")
    linhas = conteudo.splitlines()
    assert len(linhas) <= 100
    assert linhas[-1] == "linha 4999", "a poda tem de cortar o começo, não o fim"


def test_log_abaixo_do_teto_nao_perde_nada(texto_tk):
    _append(texto_tk, [f"l{i}\n" for i in range(10)], teto=100)
    assert texto_tk.get("1.0", "end-1c").splitlines() == [f"l{i}" for i in range(10)]


def test_log_lote_vazio_nao_faz_nada(texto_tk):
    _append(texto_tk, [], teto=10)
    assert texto_tk.get("1.0", "end-1c") == ""
