# -*- coding: utf-8 -*-
"""Cobertura do fluxo de organização de pasta e utilitários de IO."""

from __future__ import annotations

import threading
from pathlib import Path

import organizar_processos as op
from organizador.models import AtoDocumento, Evento, Parte, Processo


def test_mover_pdf_mesmo_arquivo_nao_move(tmp_path):
    pasta = tmp_path / "proc"
    pasta.mkdir()
    pdf = pasta / "origem.pdf"
    pdf.write_text("x", encoding="utf-8")
    alvo = op._mover_pdf(pdf, pasta)
    assert alvo == pdf
    assert pdf.exists()


def test_fmt_advogados_ignora_campos_vazios():
    advs = [op.Advogado(nome="A", oab="SP1"), op.Advogado(), op.Advogado(nome="B")]
    assert op.organizacao._fmt_advogados(advs) == "A (SP1), B ()"


def test_mover_pdf_resolve_colisao_de_nome(tmp_path):
    origem = tmp_path / "entrada.pdf"
    origem.write_text("origem", encoding="utf-8")
    destino = tmp_path / "destino"
    destino.mkdir()
    (destino / "ATO - entrada.pdf").write_text("existente", encoding="utf-8")

    alvo = op._mover_pdf(origem, destino, "ATO - ")
    assert alvo.name == "ATO - entrada (1).pdf"
    assert alvo.exists()
    assert not origem.exists()


def test_merge_processos_complementa_tipo_e_atos_sem_duplicar():
    base = Processo(
        partes=[Parte(papel="AUTOR", nome="EMPRESA", tipo="", documento="")],
        tags=["urgente"],
        arquivos_origem=["a.pdf"],
        atos=[AtoDocumento(codigo_verificador="V1", arquivo="a.pdf")],
    )
    novo = Processo(
        partes=[Parte(papel="AUTOR", nome="empresa", tipo="Pessoa Jurídica", documento="1")],
        tags=["urgente", "prioridade"],
        arquivos_origem=["a.pdf", "b.pdf"],
        atos=[
            AtoDocumento(codigo_verificador="V1", arquivo="a.pdf"),
            AtoDocumento(codigo_verificador="V2", arquivo="b.pdf"),
        ],
    )
    op._merge_processos(base, novo, "detalhes")
    assert base.partes[0].tipo == "Pessoa Jurídica"
    assert base.partes[0].documento == "1"
    assert sorted(base.tags) == ["prioridade", "urgente"]
    assert sorted(base.arquivos_origem) == ["a.pdf", "b.pdf"]
    assert {(a.codigo_verificador, a.arquivo) for a in base.atos} == {
        ("V1", "a.pdf"),
        ("V2", "b.pdf"),
    }


def test_merge_processos_preenche_numero_vazio_e_complementa_advogado_novo():
    base = Processo(numero="", partes=[Parte(papel="AUTOR", nome="EMPRESA", advogados=[op.Advogado(oab="SP1")])])
    novo = Processo(
        numero="123",
        partes=[Parte(papel="AUTOR", nome="EMPRESA", tipo="Pessoa Jurídica", advogados=[op.Advogado(oab="SP2")])],
    )
    op._merge_processos(base, novo, "detalhes")
    assert base.numero == "123"
    assert base.partes[0].tipo == "Pessoa Jurídica"
    assert [a.oab for a in base.partes[0].advogados] == ["SP1", "SP2"]


def test_organizar_pasta_vazia(tmp_path):
    logs = []
    resumo = op.organizar_pasta(tmp_path, log=logs.append)
    assert resumo == {"total": 0, "organizados": 0, "nao_identificados": 0, "processos": []}
    assert any("Encontrados 0 PDF(s)" in linha for linha in logs)


def test_resumo_txt_inclui_foro_referencia_e_truncamento():
    eventos = [Evento(numero=str(i), data="20/05/2026", hora="10:00:00", descricao="Desc", refer_evento="9") for i in range(55)]
    proc = Processo(foro_endereco="Rua A", eventos=eventos)
    texto = op.organizacao._resumo_txt(proc)
    assert "Endereço foro  : Rua A" in texto
    assert "Refer. ao evento: 9" in texto
    assert "... e mais 5 evento(s) (total: 55)" in texto


def test_organizar_pasta_cancelada_antes_de_iniciar(tmp_path):
    cancel = threading.Event()
    cancel.set()
    resumo = op.organizar_pasta(tmp_path, cancel_event=cancel, log=lambda _msg: None)
    assert resumo == {"total": 0, "organizados": 0, "nao_identificados": 0, "processos": []}


def test_organizar_pasta_cancelada_antes_da_primeira_passada(tmp_path):
    class ToggleCancel:
        def __init__(self):
            self.calls = 0

        def is_set(self):
            self.calls += 1
            return self.calls >= 2

    resumo = op.organizar_pasta(tmp_path, cancel_event=ToggleCancel(), log=lambda _msg: None)
    assert resumo == {"total": 0, "organizados": 0, "nao_identificados": 0, "processos": []}


def test_organizar_pasta_cancelada_apos_primeira_passada(monkeypatch, tmp_path):
    pdf = tmp_path / "proc.pdf"
    pdf.write_text("x", encoding="utf-8")
    cancel = threading.Event()

    def fake_processar(
        pdf, grupos, pdfs_por_numero, nao_identificados, lock, log,
        cancel_event=None, cache_dir=None, progress_cb=None, total=0, processed_counter=None,
    ):
        with lock:
            grupos["123"] = Processo(numero="123")
            pdfs_por_numero["123"] = [(pdf, "detalhes")]
        cancel.set()

    monkeypatch.setattr(op.organizacao, "_processar_pdf", fake_processar)
    resumo = op.organizar_pasta(tmp_path, cancel_event=cancel, log=lambda _msg: None)
    assert resumo == {"total": 1, "organizados": 0, "nao_identificados": 0, "processos": []}


def test_processar_pdf_retorna_se_cancelado_antes(monkeypatch, tmp_path):
    pdf = tmp_path / "proc.pdf"
    pdf.write_text("x", encoding="utf-8")
    grupos, pdfs_por_numero, nao_identificados = {}, {}, []
    cancel = threading.Event()
    cancel.set()
    op.organizacao._processar_pdf(
        pdf, grupos, pdfs_por_numero, nao_identificados, threading.Lock(), lambda _msg: None, cancel
    )
    assert grupos == {}


def test_processar_pdf_tolera_erro_de_leitura(monkeypatch, tmp_path):
    pdf = tmp_path / "proc.pdf"
    pdf.write_text("x", encoding="utf-8")
    logs = []
    progresso = []
    monkeypatch.setattr(op.organizacao, "extrair_texto", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("falhou")))
    op.organizacao._processar_pdf(
        pdf, {}, {}, [], threading.Lock(), logs.append, progress_cb=lambda a, b: progresso.append((a, b)),
        total=1, processed_counter=iter([1]),
    )
    assert any("Erro ao ler PDF" in linha for linha in logs)
    assert progresso == [(1, 1)]


def test_processar_pdf_tolera_processed_counter_esgotado(monkeypatch, tmp_path):
    pdf = tmp_path / "proc.pdf"
    pdf.write_text("x", encoding="utf-8")
    monkeypatch.setattr(op.organizacao, "extrair_texto", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("falhou")))
    op.organizacao._processar_pdf(
        pdf, {}, {}, [], threading.Lock(), lambda _msg: None, progress_cb=lambda *_args: None,
        total=1, processed_counter=iter(()),
    )


def test_processar_pdf_usa_numero_do_nome_do_arquivo_quando_parser_nao_acha(monkeypatch, tmp_path):
    pdf = tmp_path / "4004459-04.2026.8.26.0248.pdf"
    pdf.write_text("x", encoding="utf-8")
    grupos, pdfs_por_numero = {}, {}
    monkeypatch.setattr(op.organizacao, "extrair_texto", lambda *_args, **_kwargs: "texto")
    monkeypatch.setattr(op.organizacao, "parse_pdf", lambda *_args, **_kwargs: ("detalhes", Processo()))
    op.organizacao._processar_pdf(
        pdf, grupos, pdfs_por_numero, [], threading.Lock(), lambda _msg: None
    )
    assert "4004459-04.2026.8.26.0248" in grupos


def test_processar_pdf_mergeia_quando_numero_ja_existe(monkeypatch, tmp_path):
    pdf = tmp_path / "proc.pdf"
    pdf.write_text("x", encoding="utf-8")
    base = Processo(numero="123")
    grupos = {"123": base}
    pdfs_por_numero = {}
    chamado = {"merge": 0}

    monkeypatch.setattr(op.organizacao, "extrair_texto", lambda *_args, **_kwargs: "texto")
    monkeypatch.setattr(op.organizacao, "parse_pdf", lambda *_args, **_kwargs: ("detalhes", Processo(numero="123")))
    monkeypatch.setattr(op.organizacao, "_merge_processos", lambda *_args, **_kwargs: chamado.__setitem__("merge", 1))
    op.organizacao._processar_pdf(
        pdf, grupos, pdfs_por_numero, [], threading.Lock(), lambda _msg: None
    )
    assert chamado["merge"] == 1


def test_organizar_pasta_move_nao_identificado(monkeypatch, tmp_path):
    pdf = tmp_path / "solto.pdf"
    pdf.write_text("x", encoding="utf-8")
    monkeypatch.setattr(op.organizacao, "extrair_texto", lambda *_args, **_kwargs: "sem cnj")
    monkeypatch.setattr(op.organizacao, "parse_pdf", lambda *_args, **_kwargs: ("desconhecido", Processo()))

    resumo = op.organizar_pasta(tmp_path, log=lambda _msg: None)
    destino_ni = tmp_path / op.PASTA_NAO_IDENTIFICADOS / "solto.pdf"
    assert resumo["nao_identificados"] == 1
    assert destino_ni.exists()


def test_organizar_pasta_cancela_durante_segunda_passada(monkeypatch, tmp_path):
    pdf = tmp_path / "detalhes.pdf"
    pdf.write_text("x", encoding="utf-8")

    class ToggleCancel:
        def __init__(self):
            self.calls = 0

        def is_set(self):
            self.calls += 1
            return self.calls >= 4

    def fake_processar(pdf, grupos, pdfs_por_numero, nao_identificados, lock, log, *args):
        with lock:
            grupos["123"] = Processo(numero="123")
            pdfs_por_numero["123"] = [(pdf, "detalhes")]

    monkeypatch.setattr(op.organizacao, "_processar_pdf", fake_processar)
    resumo = op.organizar_pasta(tmp_path, cancel_event=ToggleCancel(), log=lambda _msg: None)
    assert resumo["organizados"] == 0


def test_organizar_pasta_cancela_durante_nao_identificados(monkeypatch, tmp_path):
    pdf = tmp_path / "solto.pdf"
    pdf.write_text("x", encoding="utf-8")

    class ToggleCancel:
        def __init__(self):
            self.calls = 0

        def is_set(self):
            self.calls += 1
            return self.calls >= 4

    monkeypatch.setattr(op.organizacao, "extrair_texto", lambda *_args, **_kwargs: "sem cnj")
    monkeypatch.setattr(op.organizacao, "parse_pdf", lambda *_args, **_kwargs: ("desconhecido", Processo()))
    resumo = op.organizar_pasta(tmp_path, cancel_event=ToggleCancel(), log=lambda _msg: None)
    assert resumo["nao_identificados"] == 0


def test_organizar_pasta_reporta_falha_em_worker(monkeypatch, tmp_path):
    pdf = tmp_path / "detalhes.pdf"
    pdf.write_text("x", encoding="utf-8")
    logs = []
    monkeypatch.setattr(op.organizacao, "_processar_pdf", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("worker falhou")))
    op.organizar_pasta(tmp_path, log=logs.append)
    assert any("Erro em detalhes.pdf: worker falhou" in linha for linha in logs)


def test_organizar_pasta_tolera_erro_ao_processar_grupo(monkeypatch, tmp_path):
    pdf = tmp_path / "detalhes.pdf"
    pdf.write_text("x", encoding="utf-8")
    proc = Processo(numero="123")
    monkeypatch.setattr(op.organizacao, "extrair_texto", lambda *_args, **_kwargs: "texto")
    monkeypatch.setattr(op.organizacao, "parse_pdf", lambda *_args, **_kwargs: ("detalhes", proc))
    monkeypatch.setattr(op.organizacao, "_mover_pdf", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("move falhou")))
    resumo = op.organizar_pasta(tmp_path, log=lambda _msg: None)
    assert resumo["organizados"] == 0


def test_organizar_pasta_tolera_erro_ao_gerar_indice(monkeypatch, tmp_path):
    pdf = tmp_path / "detalhes.pdf"
    pdf.write_text("x", encoding="utf-8")
    monkeypatch.setattr(op.organizacao, "extrair_texto", lambda *_args, **_kwargs: "texto")
    monkeypatch.setattr(op.organizacao, "parse_pdf", lambda *_args, **_kwargs: ("detalhes", Processo(numero="123")))
    monkeypatch.setattr(op.organizacao, "gerar_indice_csv", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("csv")))
    resumo = op.organizar_pasta(tmp_path, log=lambda _msg: None)
    assert resumo["organizados"] == 1


def test_organizar_pasta_gera_metadados_resumo_e_indice(monkeypatch, tmp_path):
    pdf = tmp_path / "detalhes.pdf"
    pdf.write_text("x", encoding="utf-8")

    proc = Processo(
        numero="4004459-04.2026.8.26.0248",
        tribunal="TJSP",
        uf="SP",
        partes=[Parte(papel="AUTOR", nome="EMPRESA"), Parte(papel="RÉU", nome="FULANO")],
        eventos=[Evento(numero="1", data="20/05/2026", descricao="Juntada")],
        atos=[AtoDocumento(codigo_verificador="V1", arquivo="detalhes.pdf")],
    )

    monkeypatch.setattr(op.organizacao, "extrair_texto", lambda *_args, **_kwargs: "texto lido")
    monkeypatch.setattr(op.organizacao, "parse_pdf", lambda *_args, **_kwargs: ("ato", proc))

    progressos = []
    resumo = op.organizar_pasta(tmp_path, log=lambda _msg: None, progress_cb=lambda a, b: progressos.append((a, b)))
    pasta_proc = tmp_path / proc.numero

    assert resumo["organizados"] == 1
    assert (pasta_proc / "metadados.json").exists()
    assert (pasta_proc / "resumo.txt").exists()
    assert (tmp_path / "índice.csv").exists()
    assert (pasta_proc / "ATO - detalhes.pdf").exists()
    assert progressos[-1] == (1, 1)


def test_organizar_pasta_move_nao_identificado_reporta_progresso(monkeypatch, tmp_path):
    pdf = tmp_path / "solto.pdf"
    pdf.write_text("x", encoding="utf-8")
    progressos = []
    monkeypatch.setattr(op.organizacao, "extrair_texto", lambda *_args, **_kwargs: "sem cnj")
    monkeypatch.setattr(op.organizacao, "parse_pdf", lambda *_args, **_kwargs: ("desconhecido", Processo()))
    op.organizar_pasta(tmp_path, log=lambda _msg: None, progress_cb=lambda a, b: progressos.append((a, b)))
    assert progressos[-1] == (1, 1)


def test_uf_do_cnj_sem_match_e_partes_vazias():
    assert op.uf_do_cnj("invalido") == ""
    assert op.organizacao._autor(Processo()) == ""
    assert op.organizacao._reus_joined(Processo()) == ""


def test_carregar_processos_ignora_json_invalido(tmp_path):
    pasta = tmp_path / "123"
    pasta.mkdir()
    (pasta / "metadados.json").write_text("{invalido", encoding="utf-8")
    assert op._carregar_processos(tmp_path) == []


def test_carregar_processos_ignora_arquivos_que_nao_sao_pastas(tmp_path):
    (tmp_path / "arquivo.txt").write_text("x", encoding="utf-8")
    assert op._carregar_processos(tmp_path) == []
