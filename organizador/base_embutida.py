# -*- coding: utf-8 -*-
"""Snapshot de processos embutido na release, em SQLite indexado por número.

POR QUE NÃO É MAIS UM .json.gz
------------------------------
O snapshot tem ~3.100 processos e, descomprimido, ~70 MB de JSON. Lê-lo era
"tudo ou nada": para montar a lista da aba Pesquisa (7 colunas de resumo) o app
parseava os 70 MB inteiros e construía 3.100 objetos ``Processo`` com ~156 mil
eventos — 8 s e 300 MB de pico numa máquina rápida, ~25 s num i3 de 4 GB. E
acontecia DE NOVO ao clicar num processo embutido, retendo ~104 MB para sempre.

Havia um cache por ``mtime`` para evitar isso, mas ele nunca acertava no .exe: o
onefile extrai o arquivo em ``sys._MEIPASS`` com timestamp novo a cada abertura,
então o ganho só existia rodando do código-fonte.

Aqui as duas necessidades são separadas em duas tabelas:

  - ``resumos``   — as 7 colunas de exibição/filtro, uma linha por processo.
                    A abertura lê só isto (~3.100 linhas curtas).
  - ``processos`` — o JSON completo de cada processo, comprimido individualmente
                    com zlib. Buscado POR NÚMERO: um clique descomprime 1
                    registro, não 3.100.

Sem dependência de ``mtime``: a identidade do arquivo não importa mais, porque
nada é reconstruído a partir dele.

Gerado por ``scripts/gerar_base_sqlite.py`` durante o release.
"""

from __future__ import annotations

import json
import sqlite3
import zlib
from pathlib import Path

VERSAO_ESQUEMA = 1

# Ordem fixa: usada tanto na escrita quanto na leitura dos resumos.
COLUNAS_RESUMO = (
    "numero", "classe", "autor", "reu", "valor_causa",
    "ultima_movimentacao", "prazo_fim", "comarca", "tags",
)

ESQUEMA = """
CREATE TABLE meta (chave TEXT PRIMARY KEY, valor TEXT);
CREATE TABLE resumos (
    numero              TEXT PRIMARY KEY,
    classe              TEXT,
    autor               TEXT,
    reu                 TEXT,
    valor_causa         TEXT,
    ultima_movimentacao TEXT,
    prazo_fim           TEXT,
    comarca             TEXT,
    tags                TEXT
);
CREATE TABLE processos (numero TEXT PRIMARY KEY, blob BLOB);
"""


_con_cache: dict[str, sqlite3.Connection] = {}


def _conectar(caminho: Path) -> sqlite3.Connection:
    """Conexão somente-leitura, reaproveitada entre chamadas (a base nunca é
    escrita em tempo de execução, só regravada pelo gerador no release, com o
    app fechado). Evita reabrir o arquivo a cada clique na lista de Pesquisa.
    O URI evita criar um arquivo vazio quando o caminho não existe (o que
    faria a checagem de esquema passar por engano)."""
    chave = str(Path(caminho).resolve())
    con = _con_cache.get(chave)
    if con is not None:
        return con
    uri = Path(caminho).resolve().as_uri()
    con = sqlite3.connect(f"{uri}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    _con_cache[chave] = con
    return con


def disponivel(caminho: Path) -> bool:
    """True se `caminho` é uma base SQLite legível na versão de esquema esperada.

    Usado para decidir entre o caminho novo e o fallback pelo .json.gz — rodando
    do código-fonte a base pode simplesmente não ter sido gerada ainda."""
    p = Path(caminho)
    if not p.is_file():
        return False
    try:
        con = _conectar(p)
        linha = con.execute(
            "SELECT valor FROM meta WHERE chave = 'versao'"
        ).fetchone()
        return bool(linha) and int(linha["valor"]) == VERSAO_ESQUEMA
    except Exception:
        return False


def ler_resumos(caminho: Path) -> list[dict]:
    """Todos os resumos como dicts prontos para ``ResumoProcesso(**d)``.

    `tags` volta de JSON para lista. Não levanta: base ausente/corrompida devolve
    lista vazia (a lista da Pesquisa fica sem os embutidos, o app abre)."""
    try:
        con = _conectar(caminho)
        linhas = con.execute(
            f"SELECT {', '.join(COLUNAS_RESUMO)} FROM resumos"
        ).fetchall()
    except Exception:
        return []
    saida: list[dict] = []
    for linha in linhas:
        d = {c: (linha[c] or "") for c in COLUNAS_RESUMO}
        try:
            d["tags"] = json.loads(linha["tags"]) if linha["tags"] else []
        except (ValueError, TypeError):
            d["tags"] = []
        saida.append(d)
    return saida


def ler_processo(caminho: Path, numero: str) -> dict | None:
    """O dict completo de UM processo (o equivalente ao metadados.json).

    Este é o ganho principal em RAM: descomprime só o registro pedido."""
    numero = (numero or "").strip()
    if not numero:
        return None
    try:
        con = _conectar(caminho)
        linha = con.execute(
            "SELECT blob FROM processos WHERE numero = ?", (numero,)
        ).fetchone()
        if linha is None:
            return None
        return json.loads(zlib.decompress(linha["blob"]).decode("utf-8"))
    except Exception:
        return None


# --------------------------------------------------------------------------
# Escrita (usada só pelo gerador, no release)
# --------------------------------------------------------------------------
def gravar(
    destino: Path,
    registros,
    resumo_de_dict,
    gerado_em: str = "",
) -> int:
    """Cria `destino` com os processos de `registros` (iterável de dicts).

    `resumo_de_dict(d) -> ResumoProcesso` é injetado para não importar
    ``indice_processos`` aqui (evita ciclo) e para que o resumo gravado seja
    calculado pela MESMA função que a leitura de disco usa.

    Devolve quantos processos foram gravados. Grava em arquivo temporário e
    renomeia no fim, para nunca deixar uma base meio-escrita no lugar da boa.
    """
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_suffix(destino.suffix + ".tmp")
    tmp.unlink(missing_ok=True)

    con = sqlite3.connect(tmp)
    try:
        con.executescript(ESQUEMA)
        n = 0
        vistos: set[str] = set()
        for d in registros:
            numero = (d.get("numero") or "").strip()
            if not numero or numero in vistos:
                continue  # sem chave não há como buscar depois; dedup por número
            vistos.add(numero)
            r = resumo_de_dict(d)
            con.execute(
                f"INSERT INTO resumos ({', '.join(COLUNAS_RESUMO)}) "
                f"VALUES ({', '.join('?' * len(COLUNAS_RESUMO))})",
                (
                    r.numero, r.classe, r.autor, r.reu, r.valor_causa,
                    r.ultima_movimentacao, r.prazo_fim, r.comarca,
                    json.dumps(r.tags, ensure_ascii=False),
                ),
            )
            con.execute(
                "INSERT INTO processos (numero, blob) VALUES (?, ?)",
                (
                    numero,
                    zlib.compress(
                        json.dumps(d, ensure_ascii=False).encode("utf-8"), 9
                    ),
                ),
            )
            n += 1
        con.executemany(
            "INSERT INTO meta (chave, valor) VALUES (?, ?)",
            [
                ("versao", str(VERSAO_ESQUEMA)),
                ("n", str(n)),
                ("gerado_em", gerado_em),
            ],
        )
        con.commit()
        con.execute("VACUUM")
        con.commit()
    finally:
        con.close()

    destino.unlink(missing_ok=True)
    tmp.replace(destino)
    return n
