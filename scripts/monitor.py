# -*- coding: utf-8 -*-
"""Monitoramento periódico: planilhas Google → mapeamento de sistema.

Uma passada: lê os processos das planilhas de credores (somente leitura, via
conta de serviço), descarta os que JÁ estão mapeados (no cache do app ou no
estado) e raspa os pendentes p/ descobrir qual sistema os hospeda e enriquecer.

O motor mora em organizador.monitoramento (o mesmo usado pelo botão da GUI);
este script é só um wrapper de linha de comando p/ teste/execução sem abrir o app.

Uso:
    python scripts/monitor.py --dry-run                 # só relata pendentes × mapeados
    python scripts/monitor.py --headless                # roda a passada (invisível)
    python scripts/monitor.py --limit 3 --headless      # só os 3 primeiros (teste)
    python scripts/monitor.py --retry-falhas            # reprocessa nao_encontrado/falha
    python scripts/monitor.py --so-rapidos --headless   # só sistemas TOTP (PJe/eproc)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))
load_dotenv(_RAIZ / "config" / ".env")

# Console do Windows costuma ser cp1252; os logs usam emojis/acentos.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from organizador.monitoramento import rodar_passada  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Monitoramento: planilhas Google → mapeamento de sistema"
    )
    ap.add_argument("--headless", action="store_true", help="navegador invisível")
    ap.add_argument("--dry-run", action="store_true",
                    help="só relata pendentes × já mapeados, sem raspar")
    ap.add_argument("--limit", type=int, default=0, help="processa só os N primeiros")
    ap.add_argument("--retry-falhas", action="store_true",
                    help="reprocessa nao_encontrado/falha (senão são pulados)")
    ap.add_argument("--so-rapidos", action="store_true",
                    help="só sistemas TOTP (PJe/eproc); deixa o e-SAJ p/ depois")
    args = ap.parse_args()

    try:
        resumo = rodar_passada(
            headless=args.headless,
            dry_run=args.dry_run,
            limit=args.limit,
            retry_falhas=args.retry_falhas,
            so_rapidos=args.so_rapidos,
        )
    except Exception as exc:
        print(f"\n❌ {exc}")
        sys.exit(1)
    print(f"\nResumo: {resumo}")


if __name__ == "__main__":
    main()
