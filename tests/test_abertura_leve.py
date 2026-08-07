# -*- coding: utf-8 -*-
"""Trava as garantias de ABERTURA do app (custo fixo por execução).

Estes testes existem porque as otimizações de startup são fáceis de desfazer sem
ninguém notar: basta alguém acrescentar um `import selenium` no topo de um módulo
que a GUI importa, e a abertura volta a custar segundos numa máquina fraca.

Rodam num subprocesso limpo: `sys.modules` do processo do pytest já está poluído
pelos outros testes, então perguntar "o selenium foi carregado?" só faz sentido
num interpretador novo.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent

# Bibliotecas que NÃO podem ser carregadas só para desenhar a janela.
# (nome do módulo raiz, por que é caro)
PESADAS = [
    ("selenium", "dirige o Chrome; só a busca online precisa"),
    ("webdriver_manager", "resolve o chromedriver pela rede"),
    ("bs4", "parser de HTML; puxa charset_normalizer (~1,5 s)"),
    ("charset_normalizer", "detecção de encoding, caríssima de importar"),
    ("pypdf", "leitura de PDF; só o Organizador precisa"),
    ("docx", "python-docx; puxa lxml. Só a geração de petições precisa"),
    ("pandas", "saiu do app na migração para openpyxl — não pode voltar"),
    ("numpy", "vinha junto com o pandas"),
    ("pyarrow", "vinha junto com o pandas (81 MB no bundle)"),
    ("IPython", "vinha junto com o pandas"),
]


def _rodar(codigo: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(codigo)],
        cwd=str(RAIZ), capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, f"subprocesso falhou:\n{proc.stderr}"
    return proc.stdout.strip()


@pytest.fixture(scope="module")
def modulos_apos_import_da_gui() -> set[str]:
    """Raízes de módulo carregadas por `import organizador.gui`, num processo novo."""
    saida = _rodar(
        """
        import sys
        import organizador.gui  # noqa: F401
        print("\\n".join(sorted({m.split(".")[0] for m in sys.modules})))
        """
    )
    return set(saida.splitlines())


@pytest.mark.parametrize("modulo, motivo", PESADAS, ids=[m for m, _ in PESADAS])
def test_biblioteca_pesada_nao_entra_na_abertura(
    modulo: str, motivo: str, modulos_apos_import_da_gui: set[str]
):
    assert modulo not in modulos_apos_import_da_gui, (
        f"'{modulo}' passou a ser carregado por `import organizador.gui` — {motivo}. "
        f"Mova o import para dentro da função que o usa "
        f"(ver o cabeçalho de organizador/scraper.py) e rode "
        f"`python -X importtime -c \"import organizador.gui\"` para conferir."
    )


def test_gui_de_fato_importa():
    """Guarda-costas do teste acima: se o import da GUI quebrasse, a lista de
    módulos viria vazia e todos os asserts passariam por vacuidade."""
    assert "ttkbootstrap" in _rodar(
        """
        import sys
        import organizador.gui  # noqa: F401
        print("\\n".join(sorted({m.split(".")[0] for m in sys.modules})))
        """
    ).splitlines()


def test_placeholders_nao_puxa_docx():
    """O gui.py depende deste módulo no topo; ele tem de continuar sendo só dados."""
    assert _rodar(
        """
        import sys
        from organizador.placeholders import (
            PLACEHOLDERS_AUTO, PLACEHOLDERS_PETICAO, _template_eh_imobiliario,
        )
        assert PLACEHOLDERS_PETICAO and PLACEHOLDERS_AUTO
        assert _template_eh_imobiliario("Penhora imobiliária.docx")
        print("docx" in sys.modules or "lxml" in sys.modules)
        """
    ) == "False"


def test_scraper_expoe_os_simbolos_tardios_sem_importar_selenium():
    """`__getattr__` de módulo mantém o ponto de injeção dos testes (e a API)
    sem trazer o selenium para o import."""
    assert _rodar(
        """
        import sys
        import organizador.scraper as s
        assert "selenium" not in sys.modules, "import de scraper trouxe selenium"
        # Presentes em dir() e resolvíveis sob demanda.
        assert "criar_scraper" in dir(s)
        assert callable(s.criar_scraper)
        print("selenium" in sys.modules)
        """
    ) == "True"  # depois de TOCAR em criar_scraper, aí sim o selenium entra


def test_atributo_inexistente_ainda_levanta_attribute_error():
    assert _rodar(
        """
        import organizador.scraper as s
        try:
            s.nao_existe
        except AttributeError as exc:
            print("ok" if "nao_existe" in str(exc) else f"msg ruim: {exc}")
        else:
            print("nao levantou")
        """
    ) == "ok"
