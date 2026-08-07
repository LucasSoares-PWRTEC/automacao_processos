# -*- coding: utf-8 -*-
"""Converte data/processos_base.json.gz em data/processos_base.sqlite.

O .json.gz continua sendo a FONTE versionada (é o que o reenriquecimento produz);
o .sqlite é um artefato de build, regenerado pelo release.ps1 e ignorado pelo git.

Motivo: no .exe a abertura parseava os 70 MB do snapshot inteiro só para montar a
lista de resumos — ver organizador/base_embutida.py.

Uso:
    python scripts/gerar_base_sqlite.py
    python scripts/gerar_base_sqlite.py --origem outra.json.gz --destino saida.sqlite
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from organizador import base_embutida  # noqa: E402
from organizador.indice_processos import ORIGEM_EMBUTIDO, _resumo_de_dict  # noqa: E402


def _carregar(origem: Path) -> list[dict]:
    if origem.suffix == ".gz":
        with gzip.open(origem, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    return json.loads(origem.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--origem", default=str(_RAIZ / "data" / "processos_base.json.gz"))
    ap.add_argument("--destino", default=str(_RAIZ / "data" / "processos_base.sqlite"))
    args = ap.parse_args()

    origem, destino = Path(args.origem), Path(args.destino)
    if not origem.is_file():
        print(f"❌ origem não encontrada: {origem}")
        return 2

    t0 = time.perf_counter()
    print(f"lendo {origem.name} ({origem.stat().st_size / 1e6:.1f} MB comprimido)...")
    registros = _carregar(origem)
    print(f"  {len(registros)} registro(s) em {time.perf_counter() - t0:.1f}s")

    t1 = time.perf_counter()
    n = base_embutida.gravar(
        destino,
        registros,
        resumo_de_dict=lambda d: _resumo_de_dict(d, ORIGEM_EMBUTIDO, ORIGEM_EMBUTIDO),
        gerado_em=time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    print(f"gravados {n} processo(s) em {time.perf_counter() - t1:.1f}s")
    print(f"✅ {destino}  ({destino.stat().st_size / 1e6:.1f} MB)")

    if not base_embutida.disponivel(destino):
        print("❌ base gerada não passou na checagem de esquema.")
        return 1

    # Prova rápida de que a leitura por chave funciona no artefato gerado.
    resumos = base_embutida.ler_resumos(destino)
    print(f"   resumos legíveis: {len(resumos)}")
    if resumos:
        amostra = resumos[len(resumos) // 2]["numero"]
        completo = base_embutida.ler_processo(destino, amostra)
        print(
            f"   amostra {amostra}: "
            f"{len((completo or {}).get('eventos') or [])} evento(s), "
            f"{len((completo or {}).get('partes') or [])} parte(s)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
