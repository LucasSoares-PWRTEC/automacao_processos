# -*- coding: utf-8 -*-
"""Cronometra o fluxo do Autojur passo a passo, SEM SALVAR NADA.

NÃO SALVA — e isso está travado em três camadas, porque aqui um engano escreve
num sistema de produção:
  1. `criar_tarefa_protocolo(salvar=False)`, que só preenche e deixa o
     formulário aberto (é o padrão do método, ver o docstring dele);
  2. `--salvar` NÃO EXISTE como opção neste script: não há como pedir para
     salvar sem editar o arquivo;
  3. o navegador é fechado no `finally`, então o formulário preenchido é
     descartado com a sessão mesmo se algo estourar no meio.

Para que serve: o fluxo do Autojur é o mais longo do app (login com 2FA por
e-mail + 11 passos de formulário em iframe) e é dominado por espera de rede e
de servidor, não por CPU. Cronometrar passo a passo é o único jeito de saber
onde ele realmente agarra — e o log do próprio scraper já marca cada passo,
então aqui só carimbamos o tempo de cada linha que ele emite.

Cuidado deliberado: uma tentativa por execução, sem laço de retentativa. O 2FA
chega por e-mail e a conta é compartilhada; repetir em laço é o que bloqueia a
conta (mesma regra do monitoramento.py).

Uso:
    python scripts/perfil_autojur.py --processo 0000223-52.2026.8.26.0127
    python scripts/perfil_autojur.py --processo N --headless
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import time
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

CONTEUDO_TESTE = (
    "TESTE DE DESEMPENHO — não salvar. Formulário preenchido apenas para "
    "cronometrar o fluxo; a sessão é encerrada sem gravar."
)


class Cronometro:
    """Carimba o tempo de cada linha de log do scraper.

    O scraper loga uma linha por passo do fluxo; o intervalo entre duas linhas
    é o custo do passo. É medição sem tocar no código do scraper."""

    def __init__(self) -> None:
        self.t0 = time.perf_counter()
        self.ultimo = self.t0
        self.passos: list[tuple[float, str]] = []
        self.saida = sys.stdout   # stdout REAL, imune ao redirect_stdout

    def _escrever(self, linha: str) -> None:
        self.saida.write(linha + "\n")
        self.saida.flush()

    def __call__(self, msg: str) -> None:
        agora = time.perf_counter()
        texto = str(msg).strip()
        if texto:
            self.passos.append(((agora - self.ultimo) * 1000, texto))
            self._escrever(
                f"  [{(agora - self.t0):6.1f}s  +{(agora - self.ultimo) * 1000:7.0f}ms] "
                f"{texto[:96]}")
        self.ultimo = agora

    # `login()` não aceita `log=`: ele usa `print`. Como a GUI faz, capturamos
    # stdout — por isso o cronômetro também serve de arquivo de escrita.
    def write(self, texto: str) -> int:
        for linha in str(texto).splitlines():
            if linha.strip():
                self(linha)
        return len(texto)

    def flush(self) -> None:
        pass

    def marco(self, rotulo: str) -> None:
        agora = time.perf_counter()
        self.passos.append(((agora - self.ultimo) * 1000, f"== {rotulo}"))
        self._escrever(
            f"  [{(agora - self.t0):6.1f}s  +{(agora - self.ultimo) * 1000:7.0f}ms] "
            f"== {rotulo}")
        self.ultimo = agora

    def ranking(self, quantos: int = 12) -> None:
        self._escrever("")
        self._escrever("=" * 74)
        self._escrever("PASSOS MAIS LENTOS DO FLUXO")
        self._escrever("=" * 74)
        for ms, texto in sorted(self.passos, reverse=True)[:quantos]:
            self._escrever(f"  {ms:8.0f} ms   {texto[:82]}")
        self._escrever(f"\n  TOTAL do fluxo: {time.perf_counter() - self.t0:.1f} s")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--processo", required=True, help="número CNJ do processo")
    ap.add_argument("--headless", action="store_true",
                    help="Chrome oculto (mais parecido com o pré-login)")
    ap.add_argument("--pdf", default="", help="PDF opcional para anexar")
    args = ap.parse_args()

    from organizador.integracoes.tjsp_scraper.scrapers.autojur import AutojurScraper

    cron = Cronometro()
    print(f"Autojur — processo {args.processo}")
    print("MODO CONFERÊNCIA: preenche e NÃO salva; o navegador fecha no fim.")
    print()

    scraper = None
    try:
        scraper = AutojurScraper(headless=args.headless)
        cron.marco("navegador aberto")

        # O print do scraper vai para o cronômetro; o print do cronômetro vai
        # para o stdout REAL (guardado antes do redirect), senão ele se
        # alimentaria em laço.
        real = sys.stdout
        cron.saida = real
        with contextlib.redirect_stdout(cron):
            scraper.login()
        cron.marco("login concluído (inclui 2FA por e-mail)")

        scraper.criar_tarefa_protocolo(
            numero_processo=args.processo,
            conteudo=CONTEUDO_TESTE,
            local="",
            lote="dia_seguinte",
            pdf_path=(args.pdf or None),
            requer_protocolo=True,
            salvar=False,          # NUNCA mude isto: ver o docstring do módulo
            escolher_resultado=lambda resultados: 0 if resultados else None,
            log=cron,
        )
        cron.marco("formulário PREENCHIDO (não salvo)")
    except Exception as exc:
        print(f"\n  !! {type(exc).__name__}: {exc}")
    finally:
        if scraper is not None:
            with contextlib.suppress(Exception):
                scraper.fechar()
            cron.marco("navegador fechado (nada salvo)")
        cron.ranking()


if __name__ == "__main__":
    main()
