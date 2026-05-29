# -*- coding: utf-8 -*-
"""Testes estruturais do pacote: cada símbolo deve morar no módulo correto.

Garante que o split em módulos se manteve (e que o shim re-exporta o pacote),
complementando os testes de caracterização que usam `organizar_processos`.
"""

import organizar_processos as op


def test_modulos_importam_diretamente():
    from organizador import (  # noqa: F401
        config, flexao, gui, models, organizacao, padroes, parsing, peticoes, scraper, texto,
    )


def test_simbolos_no_modulo_esperado():
    from organizador.models import Processo
    from organizador.parsing import parse_pdf
    from organizador.organizacao import organizar_pasta, gerar_indice_csv
    from organizador.flexao import _inflexionar_texto, _construir_contexto
    from organizador.peticoes import gerar_peticoes, _aplicar_substituicoes
    from organizador.scraper import buscar_processo_online
    from organizador.texto import detectar_tipo_pdf
    from organizador.padroes import RE_NUMERO_CNJ
    from organizador.config import PASTA_PADRAO, TJ_SISTEMAS
    assert callable(parse_pdf)
    assert callable(gerar_peticoes)


def test_shim_reexporta_pacote():
    # O mesmo objeto deve ser visível pelo shim e pelo módulo do pacote.
    from organizador import parsing, models
    assert op.parse_pdf is parsing.parse_pdf
    assert op.Processo is models.Processo
    assert op.main is __import__("organizador.gui", fromlist=["main"]).main
