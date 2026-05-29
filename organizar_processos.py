# -*- coding: utf-8 -*-
"""Organizador de Processos — ponto de entrada e camada de compatibilidade.

O código foi refatorado para o pacote `organizador/` (config, padroes, models,
texto, flexao, parsing, organizacao, scraper, peticoes, gui). Este módulo:

  1. Mantém `python organizar_processos.py` / Organizador.bat funcionando
     (chama gui.main()).
  2. Re-exporta os símbolos do pacote para que código/teste que faça
     `import organizar_processos as op; op.<qualquer_coisa>` continue válido.
"""

from __future__ import annotations

from organizador import (
    config,
    flexao,
    gui,
    models,
    organizacao,
    padroes,
    parsing,
    peticoes,
    scraper,
    texto,
)
from organizador.gui import main  # App e demais símbolos vêm via re-export abaixo

# Re-exporta TODOS os nomes de cada módulo (inclusive helpers _privados), para
# preservar a superfície da API antiga. Módulos posteriores prevalecem em caso
# de nome repetido (não há colisão real — apenas re-imports do mesmo objeto).
for _mod in (
    models, padroes, config, texto, flexao,
    parsing, organizacao, scraper, peticoes, gui,
):
    globals().update(
        {_nome: _val for _nome, _val in vars(_mod).items() if not _nome.startswith("__")}
    )
del _mod


if __name__ == "__main__":
    main()
