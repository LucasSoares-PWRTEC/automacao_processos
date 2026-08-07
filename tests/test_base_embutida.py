# -*- coding: utf-8 -*-
"""Snapshot embutido em SQLite (organizador.base_embutida).

O que precisa ser verdade:
  - a abertura lê só os resumos, sem materializar nenhum Processo completo;
  - o Processo completo é obtido POR NÚMERO (um registro, não a base toda);
  - base ausente/corrompida degrada para o fallback do .json.gz, sem exceção;
  - o resumo gravado é o MESMO que a leitura de disco produziria.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict

from organizador import base_embutida
from organizador.indice_processos import (
    ORIGEM_EMBUTIDO,
    ResumoProcesso,
    _resumo_de_dict,
    caminho_sqlite,
    carregar_processo_completo,
    carregar_resumos_pesquisa,
)
from organizador.models import Evento, Parte, Prazo, Processo


def _resumo(d: dict) -> ResumoProcesso:
    return _resumo_de_dict(d, ORIGEM_EMBUTIDO, ORIGEM_EMBUTIDO)


def _gravar(destino, procs: list[Processo]) -> int:
    return base_embutida.gravar(
        destino, [asdict(p) for p in procs], resumo_de_dict=_resumo, gerado_em="teste"
    )


def _proc_exemplo(numero: str = "111") -> Processo:
    return Processo(
        numero=numero,
        classe="Execução de Título Extrajudicial",
        comarca="São Paulo",
        valor_causa="R$ 10.000,00",
        ultima_movimentacao="10/07/2026 — despacho",
        tags=["penhora", "bloqueio"],
        partes=[
            Parte(papel="EXEQUENTE", nome="Empresa Alfa"),
            Parte(papel="EXECUTADO", nome="Fulano Beta"),
        ],
        prazos_abertos=[Prazo(fim="15/07/2026")],
        eventos=[Evento(data="01/07/2026", descricao="distribuição")],
    )


# ---------------------------------------------------------------------
# Escrita e leitura
# ---------------------------------------------------------------------
def test_gravar_e_ler_resumos(tmp_path):
    db = tmp_path / "base.sqlite"
    assert _gravar(db, [_proc_exemplo("111"), _proc_exemplo("222")]) == 2
    assert base_embutida.disponivel(db)

    resumos = base_embutida.ler_resumos(db)
    assert len(resumos) == 2
    r = ResumoProcesso(**next(d for d in resumos if d["numero"] == "111"))
    assert r.classe == "Execução de Título Extrajudicial"
    assert r.autor == "Empresa Alfa"
    assert r.reu == "Fulano Beta"
    assert r.valor_causa == "R$ 10.000,00"
    assert r.prazo_fim == "15/07/2026"
    assert r.comarca == "São Paulo"
    assert r.tags == ["penhora", "bloqueio"]
    # `fonte`/`origem` não são gravadas (constantes desta fonte); quem preenche é
    # indice_processos._resumos_embutidos — ver test_pesquisa_usa_sqlite_e_nao_o_gz.
    assert "fonte" not in resumos[0] and "origem" not in resumos[0]


def test_ler_processo_por_numero(tmp_path):
    db = tmp_path / "base.sqlite"
    _gravar(db, [_proc_exemplo("111"), _proc_exemplo("222")])
    d = base_embutida.ler_processo(db, "222")
    assert d is not None
    assert d["numero"] == "222"
    assert [p["nome"] for p in d["partes"]] == ["Empresa Alfa", "Fulano Beta"]
    assert len(d["eventos"]) == 1


def test_ler_processo_inexistente_ou_vazio(tmp_path):
    db = tmp_path / "base.sqlite"
    _gravar(db, [_proc_exemplo("111")])
    assert base_embutida.ler_processo(db, "999") is None
    assert base_embutida.ler_processo(db, "") is None
    assert base_embutida.ler_processo(db, "   ") is None


def test_numero_vazio_e_duplicado_sao_descartados(tmp_path):
    """Sem número não há chave de busca; duplicado quebraria a PRIMARY KEY."""
    db = tmp_path / "base.sqlite"
    n = _gravar(db, [
        _proc_exemplo("111"),
        _proc_exemplo("111"),   # duplicado
        _proc_exemplo(""),      # sem número
        _proc_exemplo("222"),
    ])
    assert n == 2
    assert {d["numero"] for d in base_embutida.ler_resumos(db)} == {"111", "222"}


def test_gravar_e_atomico_e_nao_deixa_tmp(tmp_path):
    db = tmp_path / "base.sqlite"
    _gravar(db, [_proc_exemplo("111")])
    assert db.exists()
    assert not db.with_suffix(db.suffix + ".tmp").exists()


def test_regravar_substitui_a_base_anterior(tmp_path):
    db = tmp_path / "base.sqlite"
    _gravar(db, [_proc_exemplo("111")])
    _gravar(db, [_proc_exemplo("222"), _proc_exemplo("333")])
    assert {d["numero"] for d in base_embutida.ler_resumos(db)} == {"222", "333"}


def test_meta_registra_versao_e_contagem(tmp_path):
    db = tmp_path / "base.sqlite"
    _gravar(db, [_proc_exemplo("111"), _proc_exemplo("222")])
    with closing(sqlite3.connect(db)) as con:
        meta = dict(con.execute("SELECT chave, valor FROM meta").fetchall())
    assert meta["versao"] == str(base_embutida.VERSAO_ESQUEMA)
    assert meta["n"] == "2"
    assert meta["gerado_em"] == "teste"


# ---------------------------------------------------------------------
# Degradação: nada disso pode levantar no caminho de abertura
# ---------------------------------------------------------------------
def test_disponivel_falso_para_ausente_lixo_e_versao_errada(tmp_path):
    assert not base_embutida.disponivel(tmp_path / "nao_existe.sqlite")

    lixo = tmp_path / "lixo.sqlite"
    lixo.write_bytes(b"nao sou um banco de dados")
    assert not base_embutida.disponivel(lixo)

    # Banco válido, mas de um esquema futuro que este código não entende.
    outro = tmp_path / "futuro.sqlite"
    _gravar(outro, [_proc_exemplo("111")])
    with closing(sqlite3.connect(outro)) as con:
        con.execute("UPDATE meta SET valor = '999' WHERE chave = 'versao'")
        con.commit()
    assert not base_embutida.disponivel(outro)


def test_leitura_de_base_invalida_devolve_vazio_sem_levantar(tmp_path):
    lixo = tmp_path / "lixo.sqlite"
    lixo.write_bytes(b"nao sou um banco")
    assert base_embutida.ler_resumos(lixo) == []
    assert base_embutida.ler_processo(lixo, "111") is None
    ausente = tmp_path / "nao_existe.sqlite"
    assert base_embutida.ler_resumos(ausente) == []
    assert base_embutida.ler_processo(ausente, "111") is None


def test_tags_corrompidas_viram_lista_vazia(tmp_path):
    db = tmp_path / "base.sqlite"
    _gravar(db, [_proc_exemplo("111")])
    with closing(sqlite3.connect(db)) as con:
        con.execute("UPDATE resumos SET tags = 'nao é json' WHERE numero = '111'")
        con.commit()
    assert base_embutida.ler_resumos(db)[0]["tags"] == []


# ---------------------------------------------------------------------
# Integração com o índice da aba Pesquisa
# ---------------------------------------------------------------------
def test_caminho_sqlite_derivado_do_snapshot(tmp_path):
    assert caminho_sqlite(tmp_path / "processos_base.json.gz") == tmp_path / "processos_base.sqlite"
    assert caminho_sqlite(tmp_path / "processos_base.json") == tmp_path / "processos_base.sqlite"
    assert caminho_sqlite(tmp_path / "outro.bin") == tmp_path / "outro.sqlite"


def _cenario(tmp_path):
    base = tmp_path / "base"
    online = tmp_path / "online"
    base.mkdir()
    online.mkdir()
    return base, online, tmp_path / "processos_base.json.gz", tmp_path / "indice.json"


def test_pesquisa_usa_sqlite_e_nao_o_gz(tmp_path, monkeypatch):
    """O ponto central da Fase 2: com a base SQLite presente, a abertura NÃO
    parseia o snapshot .json.gz (era o custo de ~25 s no i3)."""
    base, online, gz, idx = _cenario(tmp_path)
    # .gz com conteúdo DIFERENTE, para provar de onde os dados vieram.
    with gzip.open(gz, "wt", encoding="utf-8") as fh:
        json.dump([asdict(Processo(numero="999", classe="veio-do-gz"))], fh)
    _gravar(caminho_sqlite(gz), [_proc_exemplo("111")])

    import organizador.indice_processos as I
    monkeypatch.setattr(
        I, "carregar_processos_base",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("não devia ler o .gz")),
    )

    resumos = carregar_resumos_pesquisa(base, online, gz, idx)
    assert [(r.numero, r.fonte) for r in resumos] == [("111", ORIGEM_EMBUTIDO)]


def test_pesquisa_cai_no_gz_quando_nao_ha_sqlite(tmp_path):
    base, online, gz, idx = _cenario(tmp_path)
    with gzip.open(gz, "wt", encoding="utf-8") as fh:
        json.dump([asdict(Processo(numero="999", classe="veio-do-gz"))], fh)
    resumos = carregar_resumos_pesquisa(base, online, gz, idx)
    assert [(r.numero, r.classe) for r in resumos] == [("999", "veio-do-gz")]


def test_processo_completo_do_sqlite(tmp_path):
    base, online, gz, idx = _cenario(tmp_path)
    with gzip.open(gz, "wt", encoding="utf-8") as fh:
        json.dump([], fh)
    _gravar(caminho_sqlite(gz), [_proc_exemplo("111")])

    resumos = carregar_resumos_pesquisa(base, online, gz, idx)
    full = carregar_processo_completo(resumos[0], gz)
    assert isinstance(full, Processo)
    assert full.numero == "111"
    assert [p.nome for p in full.partes] == ["Empresa Alfa", "Fulano Beta"]
    assert [e.descricao for e in full.eventos] == ["distribuição"]


def test_processo_completo_ausente_no_sqlite_devolve_none(tmp_path):
    base, online, gz, idx = _cenario(tmp_path)
    with gzip.open(gz, "wt", encoding="utf-8") as fh:
        json.dump([], fh)
    _gravar(caminho_sqlite(gz), [_proc_exemplo("111")])
    r = ResumoProcesso(numero="999", origem=ORIGEM_EMBUTIDO)
    assert carregar_processo_completo(r, gz) is None


def test_indice_nao_e_reescrito_quando_nada_mudou(tmp_path):
    """Antes o índice (4,9 MB em produção) era regravado em toda abertura."""
    base, online, gz, idx = _cenario(tmp_path)
    with gzip.open(gz, "wt", encoding="utf-8") as fh:
        json.dump([], fh)
    _gravar(caminho_sqlite(gz), [_proc_exemplo("111")])

    (base / "222").mkdir()
    (base / "222" / "metadados.json").write_text(
        json.dumps(asdict(Processo(numero="222"))), encoding="utf-8"
    )

    carregar_resumos_pesquisa(base, online, gz, idx)
    assert idx.exists()
    mtime1 = idx.stat().st_mtime_ns

    carregar_resumos_pesquisa(base, online, gz, idx)
    assert idx.stat().st_mtime_ns == mtime1, "índice foi regravado sem mudança"


def test_indice_e_reescrito_quando_ha_processo_novo(tmp_path):
    base, online, gz, idx = _cenario(tmp_path)
    with gzip.open(gz, "wt", encoding="utf-8") as fh:
        json.dump([], fh)
    _gravar(caminho_sqlite(gz), [])
    carregar_resumos_pesquisa(base, online, gz, idx)

    (online / "333").mkdir()
    (online / "333" / "metadados.json").write_text(
        json.dumps(asdict(Processo(numero="333"))), encoding="utf-8"
    )
    resumos = carregar_resumos_pesquisa(base, online, gz, idx)
    assert [r.numero for r in resumos] == ["333"]
    dados = json.loads(idx.read_text(encoding="utf-8"))
    assert len(dados["disco"]) == 1


def test_indice_do_caminho_sqlite_nao_guarda_embutidos(tmp_path):
    """Com SQLite não há nada a cachear: o índice não deve mais carregar uma
    segunda cópia dos resumos embutidos (eram ~2,4 MB dos 4,9 MB)."""
    base, online, gz, idx = _cenario(tmp_path)
    with gzip.open(gz, "wt", encoding="utf-8") as fh:
        json.dump([], fh)
    _gravar(caminho_sqlite(gz), [_proc_exemplo("111")])
    (base / "222").mkdir()
    (base / "222" / "metadados.json").write_text(
        json.dumps(asdict(Processo(numero="222"))), encoding="utf-8"
    )
    carregar_resumos_pesquisa(base, online, gz, idx)
    dados = json.loads(idx.read_text(encoding="utf-8"))
    assert "embutidos" not in dados
    assert "gz_mtime" not in dados
