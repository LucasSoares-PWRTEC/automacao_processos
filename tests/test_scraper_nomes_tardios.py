# -*- coding: utf-8 -*-
"""Os símbolos tardios de organizador.scraper funcionam NO CAMINHO REAL.

BUG (v1.9.0 a v1.9.5, relatado com o app na tela):
    Buscar online -> "name 'criar_scraper' is not defined"

`__getattr__` de módulo (PEP 562) só é acionado no acesso como ATRIBUTO
(`scraper.criar_scraper`). Uma referência a nome global DENTRO de uma função
compila para LOAD_GLOBAL, que consulta o dict do módulo e os builtins e **nunca**
chama `__getattr__`. O código chamava `criar_scraper(...)` como global — então
estourava NameError na primeira busca online de cada execução.

POR QUE A SUÍTE NÃO PEGOU: todos os testes de busca faziam
`monkeypatch.setattr(scraper, "criar_scraper", fake)`, o que CRIA a entrada no
dict do módulo. Com ela lá, o LOAD_GLOBAL encontra e tudo passa — validando um
caminho que a produção nunca percorre.

A regra destes testes: **não injetar nada no `organizador.scraper`**. Quando é
preciso um dublê, ele vai no MÓDULO DE ORIGEM, para a resolução tardia ser
exercitada de verdade. E os casos principais rodam em subprocesso limpo, porque
uma vez resolvido o nome fica cacheado nos globais do módulo e o segundo teste
não veria mais o problema.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent


def _rodar(corpo: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(corpo)],
        cwd=str(RAIZ), capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, f"subprocesso falhou:\n{proc.stderr[-3000:]}"
    return proc.stdout.strip()


# ---------------------------------------------------------------------
# O caminho real: chamar a função sem tocar no módulo antes
# ---------------------------------------------------------------------
def test_busca_online_nao_estoura_name_error():
    """A REGRESSÃO. O dublê é instalado no módulo de ORIGEM; nada é injetado em
    `organizador.scraper`, então a resolução tardia acontece de verdade."""
    saida = _rodar(
        """
        import sys, types

        # Dublê no MÓDULO DE ORIGEM (não no organizador.scraper).
        falso = types.ModuleType("organizador.integracoes.tjsp_scraper.scrapers")
        class _S:
            def login(self): pass
            def consultar_processo(self, *a, **k): return "<html>irrelevante</html>"
            def fechar(self): pass
        falso.criar_scraper = lambda **kw: _S()
        sys.modules["organizador.integracoes.tjsp_scraper.scrapers"] = falso

        # dotenv também é tocado no caminho; dublê para não ler disco/ambiente.
        sys.modules["dotenv"] = types.SimpleNamespace(load_dotenv=lambda *a, **k: None)

        import organizador.scraper as s
        try:
            s.buscar_processo_online(
                "0000223-52.2025.8.26.0127", "TJSP — eproc", True, lambda _m: None,
            )
            print("SEM_ERRO")
        except NameError as e:
            print("NAME_ERROR:", e)
        except Exception as e:
            # Qualquer outro erro é aceitável: significa que passou do
            # `criar_scraper` e quebrou adiante, no HTML falso.
            print("PASSOU_DO_CRIAR_SCRAPER:", type(e).__name__)
        """
    )
    assert not saida.startswith("NAME_ERROR"), saida
    assert saida.startswith(("SEM_ERRO", "PASSOU_DO_CRIAR_SCRAPER")), saida


def test_extratores_do_parser_tambem_resolvem():
    """`extrair_penhora_bloqueio_tribunais` e `_extrair_blocos_com_origem` eram
    usados como globais no mesmo esquema — mesmo defeito."""
    saida = _rodar(
        """
        import organizador.scraper as s
        for nome in ("criar_scraper", "extrair_penhora_bloqueio_tribunais",
                     "_extrair_blocos_com_origem"):
            try:
                obj = s._tardio(nome)
                print(f"{nome}={callable(obj)}")
            except Exception as e:
                print(f"{nome}=ERRO {type(e).__name__}")
        """
    )
    for linha in saida.splitlines():
        assert linha.endswith("=True"), f"não resolveu: {linha}"


def test_import_do_scraper_continua_sem_selenium():
    """A resolução tardia existe para isto: o import não pode puxar selenium/bs4."""
    saida = _rodar(
        """
        import sys
        import organizador.scraper  # noqa: F401
        pesados = [m for m in ("selenium", "bs4") if m in sys.modules]
        print(",".join(pesados) or "NENHUM")
        """
    )
    assert saida == "NENHUM", f"import do scraper puxou: {saida}"


# ---------------------------------------------------------------------
# `_tardio` respeita o dublê dos testes (o seam continua valendo)
# ---------------------------------------------------------------------
def test_tardio_prefere_o_que_o_teste_injetou(monkeypatch):
    import organizador.scraper as s

    sentinela = object()
    monkeypatch.setattr(s, "criar_scraper", sentinela)
    assert s._tardio("criar_scraper") is sentinela


def test_tardio_com_nome_desconhecido_levanta():
    import organizador.scraper as s

    with pytest.raises(AttributeError, match="nao_existe"):
        s._tardio("nao_existe")


def test_dir_lista_os_nomes_tardios():
    import organizador.scraper as s

    nomes = dir(s)
    for nome in ("criar_scraper", "extrair_penhora_bloqueio_tribunais",
                 "_extrair_blocos_com_origem"):
        assert nome in nomes


# ---------------------------------------------------------------------
# A armadilha, documentada como teste
# ---------------------------------------------------------------------
def test_getattr_de_modulo_nao_cobre_nome_global():
    """Trava a razão de `_tardio` existir: se alguém "simplificar" voltando a
    chamar o nome global direto, este teste explica por que quebra."""
    saida = _rodar(
        """
        import organizador.scraper as s
        # Uma função cujo global 'criar_scraper' NÃO foi resolvido ainda.
        exec("def f():\\n    return criar_scraper", vars(s))
        try:
            vars(s)["f"]()
            print("ACHOU")     # só acontece se algo já cacheou o nome
        except NameError:
            print("NAME_ERROR")
        """
    )
    assert saida == "NAME_ERROR", (
        "LOAD_GLOBAL passou a acionar __getattr__ de módulo? Então `_tardio` "
        "pode ser simplificado — reveja."
    )
