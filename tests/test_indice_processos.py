# -*- coding: utf-8 -*-
"""Índice de resumo + carga preguiçosa do Processo completo."""

from __future__ import annotations

import gzip
import json
from dataclasses import asdict

from organizador.models import Parte, Prazo, Processo
from organizador.indice_processos import (
    ORIGEM_EMBUTIDO,
    ResumoProcesso,
    carregar_processo_completo,
    carregar_resumos_pesquisa,
)


def _gravar_meta(base, proc: Processo) -> None:
    pasta = base / proc.numero
    pasta.mkdir(parents=True)
    (pasta / "metadados.json").write_text(
        json.dumps(asdict(proc), ensure_ascii=False), encoding="utf-8"
    )


def _gravar_gz(caminho, procs: list[Processo]) -> None:
    with gzip.open(caminho, "wt", encoding="utf-8") as fh:
        json.dump([asdict(p) for p in procs], fh)


def _cenario(tmp_path):
    base = tmp_path / "base"
    online = tmp_path / "online"
    base.mkdir()
    online.mkdir()
    gz = tmp_path / "processos_base.json.gz"
    idx = tmp_path / "indice.json"
    return base, online, gz, idx


def test_resumo_extrai_campos_de_exibicao(tmp_path):
    base, online, gz, idx = _cenario(tmp_path)
    _gravar_meta(base, Processo(
        numero="111", classe="Execução", comarca="Comarca X",
        valor_causa="R$ 1,00", ultima_movimentacao="10/07/2026 — despacho",
        partes=[Parte(papel="EXEQUENTE", nome="Empresa Alfa"),
                Parte(papel="EXECUTADO", nome="Fulano Beta")],
        prazos_abertos=[Prazo(fim="15/07/2026")],
    ))
    _gravar_gz(gz, [])
    resumos = carregar_resumos_pesquisa(base, online, gz, idx)
    assert len(resumos) == 1
    r = resumos[0]
    assert (r.numero, r.classe, r.autor, r.reu) == ("111", "Execução", "Empresa Alfa", "Fulano Beta")
    assert (r.valor_causa, r.prazo_fim, r.comarca) == ("R$ 1,00", "15/07/2026", "Comarca X")
    assert r.fonte == "base"


def test_prioridade_base_online_embutido_e_dedup(tmp_path):
    base, online, gz, idx = _cenario(tmp_path)
    _gravar_meta(base, Processo(numero="111", classe="curada"))
    _gravar_meta(online, Processo(numero="111", classe="online-dup"))
    _gravar_meta(online, Processo(numero="222", classe="online"))
    _gravar_gz(gz, [Processo(numero="222", classe="snap-dup"),
                    Processo(numero="333", classe="snapshot")])
    resumos = carregar_resumos_pesquisa(base, online, gz, idx)
    assert [(r.numero, r.classe, r.fonte) for r in resumos] == [
        ("111", "curada", "base"),
        ("222", "online", "online"),
        ("333", "snapshot", "embutido"),
    ]


def test_indice_persistido_e_reuso_por_mtime(tmp_path, monkeypatch):
    base, online, gz, idx = _cenario(tmp_path)
    _gravar_meta(online, Processo(numero="222", classe="online"))
    _gravar_gz(gz, [])

    carregar_resumos_pesquisa(base, online, gz, idx)
    assert idx.exists()

    # Na 2ª carga, arquivos inalterados NÃO devem ser reparseados (só reusados).
    import organizador.indice_processos as I
    chamou = {"n": 0}
    orig = I._resumo_de_dict
    monkeypatch.setattr(I, "_resumo_de_dict", lambda *a, **k: (chamou.__setitem__("n", chamou["n"] + 1), orig(*a, **k))[1])
    resumos = carregar_resumos_pesquisa(base, online, gz, idx)
    assert [r.numero for r in resumos] == ["222"]
    assert chamou["n"] == 0  # veio do índice, sem reparse


def test_arquivo_alterado_e_reparseado(tmp_path):
    base, online, gz, idx = _cenario(tmp_path)
    _gravar_meta(online, Processo(numero="222", classe="antiga"))
    _gravar_gz(gz, [])
    carregar_resumos_pesquisa(base, online, gz, idx)

    # Reescreve o metadados.json com mtime novo (conteúdo diferente).
    meta = online / "222" / "metadados.json"
    meta.write_text(json.dumps(asdict(Processo(numero="222", classe="nova"))), encoding="utf-8")
    import os, time
    novo = os.stat(meta).st_mtime + 10
    os.utime(meta, (novo, novo))

    resumos = carregar_resumos_pesquisa(base, online, gz, idx)
    assert [(r.numero, r.classe) for r in resumos] == [("222", "nova")]


def test_diretorio_removido_some_do_indice(tmp_path):
    import shutil
    base, online, gz, idx = _cenario(tmp_path)
    _gravar_meta(online, Processo(numero="222", classe="online"))
    _gravar_gz(gz, [])
    carregar_resumos_pesquisa(base, online, gz, idx)

    shutil.rmtree(online / "222")
    resumos = carregar_resumos_pesquisa(base, online, gz, idx)
    assert resumos == []
    # O índice também não deve mais referenciar o caminho removido.
    dados = json.loads(idx.read_text(encoding="utf-8"))
    assert dados["disco"] == {}


def test_carregar_processo_completo_disco(tmp_path):
    base, online, gz, idx = _cenario(tmp_path)
    _gravar_meta(online, Processo(
        numero="222", classe="online",
        partes=[Parte(papel="EXECUTADO", nome="Fulano")],
        eventos=[],
    ))
    _gravar_gz(gz, [])
    resumos = carregar_resumos_pesquisa(base, online, gz, idx)
    full = carregar_processo_completo(resumos[0])
    assert isinstance(full, Processo)
    assert full.numero == "222"
    assert [(p.papel, p.nome) for p in full.partes] == [("EXECUTADO", "Fulano")]


def test_carregar_processo_completo_embutido(tmp_path):
    base, online, gz, idx = _cenario(tmp_path)
    _gravar_gz(gz, [Processo(numero="333", classe="snapshot",
                             partes=[Parte(papel="EXEQUENTE", nome="Autor Z")])])
    resumos = carregar_resumos_pesquisa(base, online, gz, idx)
    r = next(r for r in resumos if r.numero == "333")
    assert r.origem == ORIGEM_EMBUTIDO
    full = carregar_processo_completo(r, gz)
    assert full is not None and full.classe == "snapshot"
    assert [p.nome for p in full.partes] == ["Autor Z"]


def test_carregar_processo_completo_arquivo_sumido_retorna_none(tmp_path):
    r = ResumoProcesso(numero="999", origem=str(tmp_path / "nao_existe" / "metadados.json"))
    assert carregar_processo_completo(r) is None


# ---------------------------------------------------------------------
# Desfechos degradados
#
# Esta é a carga da aba Pesquisa: roda na abertura, sobre 3.000+ pastas que o
# usuário mexe à mão. Uma pasta sem metadados.json, um JSON truncado ou um
# índice que não pôde ser gravado não podem derrubar a lista inteira.
# ---------------------------------------------------------------------
def test_pasta_sem_metadados_e_pulada(tmp_path):
    base, online, gz, idx = _cenario(tmp_path)
    _gravar_meta(base, Processo(numero="111", classe="Boa"))
    (base / "pasta_solta").mkdir()                    # sem metadados.json
    (base / "arquivo_solto.txt").write_text("x", encoding="utf-8")  # nem é pasta
    _gravar_gz(gz, [])

    resumos = carregar_resumos_pesquisa(base, online, gz, idx)
    assert [r.numero for r in resumos] == ["111"]


def test_metadados_corrompido_nao_derruba_a_lista(tmp_path):
    base, online, gz, idx = _cenario(tmp_path)
    _gravar_meta(base, Processo(numero="111", classe="Boa"))
    ruim = base / "222"
    ruim.mkdir()
    (ruim / "metadados.json").write_text("{ isto não é JSON", encoding="utf-8")
    _gravar_gz(gz, [])

    resumos = carregar_resumos_pesquisa(base, online, gz, idx)
    assert [r.numero for r in resumos] == ["111"]


def test_pasta_base_inexistente_nao_levanta(tmp_path):
    base, online, gz, idx = _cenario(tmp_path)
    _gravar_gz(gz, [])
    assert carregar_resumos_pesquisa(tmp_path / "nunca_existiu", online, gz, idx) == []


def test_indice_ilegivel_nao_impede_a_carga(tmp_path):
    """ui_state/índice corrompido: recomeça do zero em vez de travar a abertura."""
    base, online, gz, idx = _cenario(tmp_path)
    _gravar_meta(base, Processo(numero="111", classe="Boa"))
    _gravar_gz(gz, [])
    idx.write_text("lixo que não é json", encoding="utf-8")

    resumos = carregar_resumos_pesquisa(base, online, gz, idx)
    assert [r.numero for r in resumos] == ["111"]


def test_falha_ao_gravar_o_indice_e_silenciosa(tmp_path, monkeypatch):
    """O índice é só cache: não poder gravá-lo custa desempenho, não a lista."""
    base, online, gz, idx = _cenario(tmp_path)
    _gravar_meta(base, Processo(numero="111", classe="Boa"))
    _gravar_gz(gz, [])
    monkeypatch.setattr(
        "organizador.indice_processos.os.replace",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("simulado: disco cheio")),
    )
    resumos = carregar_resumos_pesquisa(base, online, gz, idx)
    assert [r.numero for r in resumos] == ["111"]


def test_snapshot_ausente_deixa_so_o_que_ha_em_disco(tmp_path):
    """Sem .sqlite e sem .json.gz (o estrago da limpeza do _MEI): os processos
    do disco continuam aparecendo."""
    base, online, gz, idx = _cenario(tmp_path)
    _gravar_meta(base, Processo(numero="111", classe="Boa"))
    resumos = carregar_resumos_pesquisa(base, online, tmp_path / "nao_existe.gz", idx)
    assert [r.numero for r in resumos] == ["111"]


def test_ultima_movimentacao_aceita_dict_do_scraper(tmp_path):
    """O scraper devolve {data, descricao}; o metadados.json guarda string."""
    from organizador.indice_processos import _resumo_de_dict

    r = _resumo_de_dict(
        {"numero": "111", "ultima_movimentacao": {"data": "01/02/2026", "descricao": "Juntada"}},
        "base", "origem",
    )
    assert r.ultima_movimentacao == "01/02/2026 — Juntada"


def test_ultima_movimentacao_dict_vazio_nao_deixa_travessao_solto():
    from organizador.indice_processos import _ultima_mov_str

    assert _ultima_mov_str({}) == ""
    assert _ultima_mov_str({"data": "01/02/2026"}) == "01/02/2026"
    assert _ultima_mov_str(None) == ""


def test_processo_completo_sem_snapshot_devolve_none():
    """Resumo embutido, mas o chamador não informou a base: None, não exceção."""
    r = ResumoProcesso(numero="333", origem=ORIGEM_EMBUTIDO)
    assert carregar_processo_completo(r) is None


def test_processo_embutido_inexistente_devolve_none(tmp_path):
    base, online, gz, idx = _cenario(tmp_path)
    _gravar_gz(gz, [Processo(numero="333", classe="snapshot")])
    carregar_resumos_pesquisa(base, online, gz, idx)
    r = ResumoProcesso(numero="000-nao-existe", origem=ORIGEM_EMBUTIDO)
    assert carregar_processo_completo(r, gz) is None


def test_mapa_embutido_e_memoizado_por_mtime(tmp_path):
    """Sem o memo, cada clique num processo embutido reparsearia o snapshot."""
    from organizador import indice_processos as ip

    gz = tmp_path / "snapshot.json.gz"
    _gravar_gz(gz, [Processo(numero="333", classe="snapshot")])
    ip._embutido_cache.update(path=None, mtime=None, mapa={})

    primeiro = ip._mapa_embutido(gz)
    segundo = ip._mapa_embutido(gz)
    assert primeiro is segundo, "reparseou o snapshot inteiro de novo"
    assert "333" in primeiro


def test_mapa_embutido_sem_arquivo_e_vazio(tmp_path):
    from organizador import indice_processos as ip

    assert ip._mapa_embutido(tmp_path / "nao_existe.json.gz") == {}
