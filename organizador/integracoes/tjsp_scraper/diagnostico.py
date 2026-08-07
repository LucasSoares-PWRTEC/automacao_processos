# -*- coding: utf-8 -*-
"""Artefatos de diagnóstico dos scrapers (screenshot + dump de DOM).

DESLIGADO POR PADRÃO — ligue com ``SCRAPER_DEBUG=1``.

Por que precisou de gate: as ~53 chamadas espalhadas pelos quatro scrapers eram
incondicionais, e boa parte delas fica em desfechos NORMAIS, não em erro. O app
tenta vários sistemas por processo (eproc × e-SAJ × PJe), então "não encontrado
neste tribunal" é o caso comum — e cada ocorrência gravava um PNG (0,3-1,5 s) e,
no PJe, ainda um dump de DOM que consulta atributo por atributo de todo input e
todo botão da página (~1.750 idas ao navegador, 5-15 s numa página JSF).

Numa corrida noturna isso chegou a 122 screenshots de um único tipo de falha
(ver comentário em scrapers/pje.py) — minutos de relógio e dezenas de MB gastos
em arquivos que, no .exe, nem sobreviviam: o ``_DEBUG_DIR`` antigo era derivado
de ``__file__``, que no onefile aponta para dentro do ``_MEIPASS`` e é apagado na
saída. Agora tudo vai para ``APP_DATA_DIR/_debug``, que persiste, com poda dos
mais antigos.

Uso:
    set SCRAPER_DEBUG=1
    python scripts/monitor.py --limit 3 --headless
    explorer %USERPROFILE%\\.automacao_processos\\_debug
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from ...config import APP_DATA_DIR

DEBUG_DIR = APP_DATA_DIR / "_debug"

# Quantos arquivos manter na pasta (os mais antigos são apagados). Evita que uma
# sessão de depuração longa encha o disco de uma máquina pequena.
MAX_ARQUIVOS = 50


def debug_ativo() -> bool:
    """Lido a cada chamada (e não uma vez no import) para dar para ligar/desligar
    em teste sem recarregar o módulo."""
    return os.getenv("SCRAPER_DEBUG", "") not in ("", "0", "false", "False")


def _preparar() -> bool:
    if not debug_ativo():
        return False
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        return True
    except OSError:
        return False


def podar(max_arquivos: int | None = None) -> int:
    """Mantém só os `max_arquivos` mais recentes. Devolve quantos apagou.

    O default é resolvido AQUI, e não na assinatura: `max_arquivos=MAX_ARQUIVOS`
    congelaria o valor no momento do import, e ajustar `MAX_ARQUIVOS` depois não
    teria efeito nenhum."""
    if max_arquivos is None:
        max_arquivos = MAX_ARQUIVOS
    try:
        arquivos = sorted(
            (p for p in DEBUG_DIR.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
    except OSError:
        return 0
    apagados = 0
    for p in arquivos[: max(0, len(arquivos) - max_arquivos)]:
        try:
            p.unlink()
            apagados += 1
        except OSError:
            pass
    return apagados


def salvar_screenshot(driver, prefixo: str, nome: str) -> Path | None:
    """PNG da tela atual. No-op silencioso quando o debug está desligado.

    `prefixo` identifica o scraper ("pje", "eproc", "esaj_sp", "autojur")."""
    if not _preparar():
        return None
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        caminho = DEBUG_DIR / f"{prefixo}_{nome}_{ts}.png"
        driver.save_screenshot(str(caminho))
        print(f"🖼️  Screenshot de debug salvo: {caminho}")
        podar()
        return caminho
    except Exception:
        return None


def dump_dom(driver, contexto: str) -> None:
    """Lista inputs e botões visíveis — acelera achar os ids reais de uma tela.

    CARO: uma ida ao navegador por atributo consultado. Só roda com
    SCRAPER_DEBUG=1. Os atributos são lidos num único ``execute_script`` em vez
    de ``get_attribute`` por elemento (era o que fazia isto custar 5-15 s)."""
    if not debug_ativo():
        return
    print(f"\n--- DOM DUMP ({contexto}) | url={driver.current_url} ---")
    try:
        dados = driver.execute_script(
            """
            const vis = el => !!(el.offsetWidth || el.offsetHeight ||
                                 el.getClientRects().length);
            const inputs = [...document.querySelectorAll('input')]
                .filter(vis)
                .map(e => ({id: e.id, name: e.name, type: e.type,
                            placeholder: e.placeholder}));
            const btns = [...document.querySelectorAll(
                    'button, a.btn, input[type=submit]')]
                .filter(vis)
                .map(e => ({id: e.id,
                            texto: (e.innerText || e.value || '').trim().slice(0, 40)}));
            return {inputs: inputs, botoes: btns};
            """
        ) or {}
        for item in dados.get("inputs", []):
            print("INPUT", item)
        for item in dados.get("botoes", []):
            print("BTN", item)
    except Exception as exc:
        print(f"(falha no dump: {exc})")
    print("--- fim do dump ---\n")
