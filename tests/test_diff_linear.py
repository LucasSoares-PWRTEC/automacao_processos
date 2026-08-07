# -*- coding: utf-8 -*-
"""Alinhamento linear usado em parágrafos muito longos (peticoes).

`_reescrever_preservando_formato` alinha o texto do modelo ao texto final para
decidir a formatação de cada caractere. O alinhamento normal é
`difflib.SequenceMatcher(..., autojunk=False)`, que é quadrático no pior caso —
com `autojunk=False` justamente sem a heurística que o limita. Acima de
`_MAX_DIFF_CARACTERES` caímos em `_opcodes_prefixo_sufixo`.

O que precisa ser verdade:
  - para o caso REAL (um placeholder trocado no meio de um texto igual), o
    resultado de formatação é IDÊNTICO ao do algoritmo completo;
  - os opcodes são sempre estruturalmente válidos (cobrem a e b por inteiro, sem
    buracos nem sobreposição), para o mapeamento de formato nunca ler fora;
  - o parágrafo longo não dispara o caminho quadrático.
"""

from __future__ import annotations

import difflib
import random

import pytest

from organizador.peticoes import _MAX_DIFF_CARACTERES, _opcodes_prefixo_sufixo


def _formatos(a: str, b: str, opcodes) -> list[int]:
    """Replica o mapeamento de formato de _reescrever_preservando_formato,
    usando o índice do caractere original como se fosse o "formato"."""
    orig = list(range(len(a)))
    default = orig[0] if orig else -1
    saida = [default] * len(b)
    for tag, i1, _i2, j1, j2 in opcodes:
        if tag == "equal":
            for k in range(j2 - j1):
                if i1 + k < len(orig):
                    saida[j1 + k] = orig[i1 + k]
        elif tag in ("replace", "insert"):
            if i1 < len(orig):
                ref = orig[i1]
            elif orig:
                ref = orig[-1]
            else:
                ref = default
            for j in range(j1, j2):
                saida[j] = ref
    return saida


def _validar_estrutura(a: str, b: str, opcodes) -> None:
    ia = ja = 0
    for tag, i1, i2, j1, j2 in opcodes:
        assert tag in ("equal", "replace", "insert", "delete")
        assert (i1, j1) == (ia, ja), f"buraco/sobreposição em {opcodes}"
        assert i1 <= i2 and j1 <= j2
        ia, ja = i2, j2
    assert (ia, ja) == (len(a), len(b)), f"não cobriu tudo: {opcodes}"


# ---------------------------------------------------------------------
# Equivalência no caso real
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "original, final",
    [
        ("Fica o (reu) intimado do prazo.", "Fica o JOAO DA SILVA intimado do prazo."),
        ("(autor) requer a penhora.", "BANCO ALFA S.A. requer a penhora."),
        ("texto sem nenhuma alteração", "texto sem nenhuma alteração"),
        ("prefixo (x)", "prefixo valor"),
        ("(x) sufixo", "valor sufixo"),
        ("abc", ""),
        ("", "abc"),
        ("", ""),
        ("(x)", "valor"),
    ],
)
def test_mesma_formatacao_que_o_algoritmo_completo(original, final):
    """Uma substituição só (o caso dos modelos) tem de dar exatamente igual."""
    meus = _opcodes_prefixo_sufixo(original, final)
    _validar_estrutura(original, final, meus)
    completos = difflib.SequenceMatcher(None, original, final, autojunk=False).get_opcodes()
    assert _formatos(original, final, meus) == _formatos(original, final, completos)


def test_varias_substituicoes_degrada_de_forma_previsivel():
    """Com mudanças espalhadas o miolo herda o formato do início da região
    trocada — mesmo critério do ramo `replace` do algoritmo completo. Não é
    idêntico, e por isso o caminho só é usado acima do teto de tamanho."""
    a = "Prezado (autor), o (reu) deve pagar."
    b = "Prezado MARIA, o JOAO deve pagar."
    opcodes = _opcodes_prefixo_sufixo(a, b)
    _validar_estrutura(a, b, opcodes)
    # Prefixo e sufixo comuns continuam preservados.
    assert opcodes[0] == ("equal", 0, 8, 0, 8)
    assert opcodes[-1][0] == "equal"


# ---------------------------------------------------------------------
# Robustez estrutural
# ---------------------------------------------------------------------
def test_opcodes_validos_para_entradas_aleatorias():
    random.seed(20260806)
    alfabeto = "abcde ()"
    for _ in range(2000):
        a = "".join(random.choice(alfabeto) for _ in range(random.randint(0, 40)))
        b = "".join(random.choice(alfabeto) for _ in range(random.randint(0, 40)))
        opcodes = _opcodes_prefixo_sufixo(a, b)
        _validar_estrutura(a, b, opcodes)
        # O mapeamento de formato nunca deve sair do intervalo de b.
        assert len(_formatos(a, b, opcodes)) == len(b)


def test_prefixo_e_sufixo_nao_se_sobrepoem():
    """'aaaa' -> 'aa': prefixo e sufixo comuns poderiam contar o mesmo caractere
    duas vezes e gerar índices negativos."""
    for a, b in [("aaaa", "aa"), ("aa", "aaaa"), ("aaa", "aaa"), ("ababab", "abab")]:
        opcodes = _opcodes_prefixo_sufixo(a, b)
        _validar_estrutura(a, b, opcodes)


def test_paragrafo_longo_usa_o_caminho_linear():
    """Guarda o teto: um parágrafo grande não pode cair no diff quadrático."""
    assert _MAX_DIFF_CARACTERES == 2000
    a = "x" * (_MAX_DIFF_CARACTERES + 500) + "(reu)" + "y" * 100
    b = "x" * (_MAX_DIFF_CARACTERES + 500) + "JOAO" + "y" * 100
    opcodes = _opcodes_prefixo_sufixo(a, b)
    _validar_estrutura(a, b, opcodes)
    assert [t for t, *_ in opcodes] == ["equal", "replace", "equal"]
