"""Configuração compartilhada da suíte de testes.

Estes são testes de CARACTERIZAÇÃO: importam o código atual e travam o
comportamento corrente, para servir de rede de segurança durante o refactor
(split do monólito em pacote). Após o split, o import continua o mesmo graças
ao shim `organizar_processos.py`, que re-exporta os símbolos do pacote.

Fora de escopo de teste automatizado (dependem de ambiente externo):
  - GUI Tkinter (classe App): requer display e interação manual.
  - Busca online (scraper.buscar_processo_online): requer Selenium, credenciais,
    2FA e acesso real aos tribunais.

Tudo o que é lógica pura, parsing de texto e geração de .docx em memória é
exercitado pelos testes desta pasta.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Garante que a raiz do projeto (onde está organizar_processos.py) esteja no path,
# independentemente do diretório a partir do qual o pytest é invocado.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def ler_fixture(nome: str) -> str:
    """Lê o conteúdo de texto de uma fixture em tests/fixtures/."""
    return (FIXTURES_DIR / nome).read_text(encoding="utf-8")


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def texto_detalhes() -> str:
    return ler_fixture("detalhes_processo.txt")


@pytest.fixture
def texto_extrato() -> str:
    return ler_fixture("extrato_distribuicao.txt")


@pytest.fixture
def texto_ato() -> str:
    return ler_fixture("ato_despacho.txt")
