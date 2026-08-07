# -*- coding: utf-8 -*-
"""Ponto de entrada do executável empacotado.

Existe separado de ``organizar_processos.py`` de propósito: aquele módulo é a
camada de compatibilidade da API antiga e faz ``globals().update`` sobre 11
módulos do pacote, o que FORÇA o carregamento de todos eles (inclusive
``peticoes``, que puxa python-docx/lxml) antes de a janela aparecer. Aqui só o
``gui`` é importado, e o resto é carregado quando for usado.

``organizar_processos.py`` continua funcionando para quem roda do fonte e para a
suíte de testes.
"""

from __future__ import annotations

from organizador.gui import main

if __name__ == "__main__":
    main()
