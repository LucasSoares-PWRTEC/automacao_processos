# -*- coding: utf-8 -*-
"""Entrada/saída em arquivos: índice CSV e carregamento de metadados."""

import csv
import json
from pathlib import Path

import organizar_processos as op
from organizar_processos import Advogado, Parte, Processo

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def ler_fixture(nome: str) -> str:
    return (FIXTURES_DIR / nome).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# _dict_para_processo
# --------------------------------------------------------------------------

def test_dict_para_processo_reconstroi_aninhados():
    dados = json.loads(ler_fixture("metadados.json"))
    proc = op._dict_para_processo(dados)
    assert isinstance(proc, Processo)
    assert proc.numero == "4004459-04.2026.8.26.0248"
    assert proc.tribunal == "TJSP"
    assert len(proc.partes) == 2
    autor = proc.partes[0]
    assert isinstance(autor, Parte)
    assert isinstance(autor.advogados[0], Advogado)
    assert autor.advogados[0].oab == "SP346433"
    assert proc.eventos[0].numero == "102"


def test_dict_para_processo_ultima_mov_como_dict():
    # Formato do scraper: ultima_movimentacao é dict -> vira string.
    dados = {
        "numero": "X",
        "ultima_movimentacao": {"data": "20/05/2026", "descricao": "Juntada"},
    }
    proc = op._dict_para_processo(dados)
    assert proc.ultima_movimentacao == "20/05/2026 — Juntada"


def test_dict_para_processo_ignora_chaves_estranhas():
    dados = {"numero": "X", "genero_autor": "f", "campo_inexistente": 1}
    proc = op._dict_para_processo(dados)
    assert proc.numero == "X"
    assert not hasattr(proc, "genero_autor")


# --------------------------------------------------------------------------
# _carregar_processos
# --------------------------------------------------------------------------

def test_carregar_processos(tmp_path):
    conteudo = ler_fixture("metadados.json")
    p1 = tmp_path / "4004459-04.2026.8.26.0248"
    p1.mkdir()
    (p1 / "metadados.json").write_text(conteudo, encoding="utf-8")
    # Subpasta sem metadados.json é ignorada.
    (tmp_path / "_nao_identificados").mkdir()

    processos = op._carregar_processos(tmp_path)
    assert len(processos) == 1
    assert processos[0].numero == "4004459-04.2026.8.26.0248"


def test_carregar_processos_pasta_inexistente(tmp_path):
    assert op._carregar_processos(tmp_path / "nao_existe") == []


# --------------------------------------------------------------------------
# gerar_indice_csv
# --------------------------------------------------------------------------

def test_gerar_indice_csv(tmp_path):
    proc = Processo(
        numero="4004459-04.2026.8.26.0248",
        tribunal="TJSP", uf="SP", classe="EXECUÇÃO",
        valor_causa="R$ 1.000,00",
        partes=[
            Parte(papel="EXEQUENTE", nome="EMPRESA EXEMPLO LTDA",
                  advogados=[Advogado(oab="SP346433")]),
            Parte(papel="EXECUTADO", nome="FULANO DE TAL"),
        ],
        arquivos_origem=["detalhes.pdf"],
    )
    caminho = op.gerar_indice_csv([proc], tmp_path)
    assert caminho.name == "índice.csv"
    assert caminho.exists()

    with caminho.open(encoding="utf-8-sig", newline="") as fh:
        linhas = list(csv.reader(fh, delimiter=";"))
    cabecalho, linha = linhas[0], linhas[1]
    registro = dict(zip(cabecalho, linha))
    assert registro["numero"] == "4004459-04.2026.8.26.0248"
    assert registro["tribunal"] == "TJSP"
    assert registro["autor"] == "EMPRESA EXEMPLO LTDA"
    assert registro["reu"] == "FULANO DE TAL"
    assert registro["oabs_autor"] == "SP346433"
    assert registro["qtd_arquivos"] == "1"


def test_gerar_indice_csv_ordena_por_numero(tmp_path):
    p1 = Processo(numero="2000000-00.2026.8.26.0001")
    p2 = Processo(numero="1000000-00.2026.8.26.0001")
    caminho = op.gerar_indice_csv([p1, p2], tmp_path)
    with caminho.open(encoding="utf-8-sig", newline="") as fh:
        linhas = list(csv.reader(fh, delimiter=";"))
    assert linhas[1][0] == "1000000-00.2026.8.26.0001"
    assert linhas[2][0] == "2000000-00.2026.8.26.0001"
