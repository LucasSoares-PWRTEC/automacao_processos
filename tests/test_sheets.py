# -*- coding: utf-8 -*-
"""Leitura das planilhas Google (organizador.sheets), sem rede.

`ler_planilha` recebe uma "sessão" só para baixar bytes; os testes injetam uma
sessão falsa que devolve um .xlsx montado em memória. Assim exercitamos a
heurística de detecção (a parte que quebra na vida real) sem tocar no Google.

Os casos cobrem as armadilhas reais das planilhas do escritório:
  - cabeçalho que NÃO contém a palavra "processo" (BVEP: "Numero Processual");
  - cabeçalho deslocado por capa/dashboard;
  - CNJ guardado como número (20 dígitos), não como texto;
  - várias abas, valendo a que tem mais CNJs.
"""

from __future__ import annotations

import io

import pytest
from openpyxl import Workbook

from organizador import sheets


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def _xlsx(abas: dict[str, list[list]]) -> bytes:
    """Monta um .xlsx em memória: {nome_da_aba: [[celula, ...], ...]}."""
    wb = Workbook()
    wb.remove(wb.active)
    for nome, linhas in abas.items():
        ws = wb.create_sheet(title=nome)
        for linha in linhas:
            ws.append(linha)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class _SessaoFalsa:
    """Substitui a AuthorizedSession: devolve sempre o mesmo .xlsx."""

    def __init__(self, conteudo: bytes) -> None:
        self._conteudo = conteudo

    def get(self, url: str, params=None, **_kw):
        conteudo = self._conteudo
        eh_meta = "export" not in url and (params or {}).get("alt") != "media"

        class _Resp:
            def json(self):
                # 1ª chamada: metadados do Drive. MIME de .xlsx subido (não nativo),
                # para o download cair no ramo `alt=media`.
                return {"name": "planilha.xlsx", "mimeType": sheets.MIME_XLSX}

            @property
            def content(self):
                return conteudo

            def raise_for_status(self):
                return None

        assert eh_meta or conteudo  # sanity: os dois ramos são exercitados
        return _Resp()


def _ler(abas: dict[str, list[list]], hint: str):
    return sheets.ler_planilha(_SessaoFalsa(_xlsx(abas)), "fid", hint, log=lambda s: None)


CNJ_A = "4008846-10.2025.8.26.0309"
CNJ_B = "1104173-75.2025.8.13.0024"
CNJ_C = "4016398-29.2025.8.26.0405"


# ---------------------------------------------------------------------
# Normalização de célula e de CNJ
# ---------------------------------------------------------------------
def test_valor_celula_inteiro_exato_sem_notacao_cientifica():
    assert sheets._valor_celula(None) == ""
    assert sheets._valor_celula(12) == "12"
    assert sheets._valor_celula(12.0) == "12"
    assert sheets._valor_celula(" texto ") == " texto "
    assert sheets._valor_celula(True) == "True"


def test_valor_celula_nao_inventa_digitos_acima_do_limite_do_float():
    """Um float64 não representa inteiro de 20 dígitos: ``int()`` devolveria um
    número plausível mas ERRADO, que passaria por CNJ válido. Acima de 2**53
    devolvemos a forma científica, que não casa como CNJ (mesmo efeito do pandas).
    """
    grande = float(40088461020258260309)          # já corrompido pelo float64
    saida = sheets._valor_celula(grande)
    assert "e+" in saida                          # forma científica, não 20 dígitos
    assert sheets._normalizar_cnj(saida) == ""    # e por isso não vira CNJ falso
    # Logo abaixo do limite ainda é exato e vale converter.
    assert sheets._valor_celula(float(2 ** 52)) == str(2 ** 52)


def test_normalizar_cnj_aceita_pontuado_e_20_digitos():
    assert sheets._normalizar_cnj(f"  {CNJ_A}  ") == CNJ_A
    assert sheets._normalizar_cnj("4008846102025826309") == ""  # 19 dígitos: não é CNJ
    assert sheets._normalizar_cnj("40088461020258260309") == CNJ_A
    assert sheets._normalizar_cnj(None) == ""
    assert sheets._normalizar_cnj("sem número aqui") == ""


# ---------------------------------------------------------------------
# Detecção da coluna
# ---------------------------------------------------------------------
def test_achar_col_exato_vence_o_contem():
    cabecalho = ["Processo Antigo", "PROCESSO", "Outro"]
    assert sheets._achar_col(cabecalho, "processo") == 1


def test_achar_col_cai_no_contem():
    assert sheets._achar_col(["Nº PROCESSO ", "Réu"], "Nº PROCESSO") == 0
    assert sheets._achar_col(["Réu", "Valor"], "Nº PROCESSO") is None


def test_dica_do_cabecalho_manda_quando_tem_cnj():
    valores, col = _ler(
        {"Dados": [["Nº PROCESSO", "Réu"], [CNJ_A, "Fulano"], [CNJ_B, "Beltrano"]]},
        "Nº PROCESSO",
    )
    assert col == "Nº PROCESSO"
    assert valores == [CNJ_A, CNJ_B]


def test_bvep_cabecalho_sem_a_palavra_processo():
    """Regressão do commit 95defcf: 'Numero Processual' não contém 'processo'
    (após normalizar), então a detecção tem de cair na contagem de CNJ."""
    valores, col = _ler(
        {"Aba1": [["Cliente", "Numero Processual"],
                  ["ACME", CNJ_A],
                  ["ACME", CNJ_B]]},
        "Numero Processual",
    )
    assert col == "Numero Processual"
    assert valores == [CNJ_A, CNJ_B]


def test_coluna_detectada_por_contagem_quando_a_dica_erra():
    valores, col = _ler(
        {"Aba1": [["Codigo", "Autos"],
                  ["X1", CNJ_A],
                  ["X2", CNJ_B],
                  ["X3", CNJ_C]]},
        "NAO EXISTE",
    )
    assert col == "Autos"
    assert valores == [CNJ_A, CNJ_B, CNJ_C]


def test_uma_unica_ocorrencia_de_cnj_nao_basta_para_coluna_desconhecida():
    """A regra `melhor_q >= 2` evita eleger uma coluna por um CNJ solto."""
    with pytest.raises(RuntimeError, match="nenhuma aba com coluna de processo"):
        _ler({"Aba1": [["Codigo", "Obs"], ["X1", CNJ_A]]}, "NAO EXISTE")


def test_cabecalho_deslocado_por_capa_ainda_entrega_os_cnjs():
    """Dashboard/capa nas primeiras linhas.

    Caracterização: com empate na contagem de CNJs, o 1º cabeçalho candidato
    vence (a comparação é `n > melhor_n`), então o rótulo devolvido pode ser o
    texto da capa em vez de "Nº PROCESSO". Comportamento idêntico ao da versão
    com pandas — e irrelevante na prática, porque o que importa é a lista de
    valores, e as linhas extras não normalizam como CNJ."""
    valores, _col = _ler(
        {"Aba1": [["RELATÓRIO GERENCIAL"],
                  [],
                  ["Nº PROCESSO", "Situação"],
                  [CNJ_A, "ativo"],
                  [CNJ_B, "ativo"]]},
        "Nº PROCESSO",
    )
    assert [v for v in valores if sheets._normalizar_cnj(v)] == [CNJ_A, CNJ_B]


def test_cabecalho_deslocado_acerta_a_coluna_mesmo_errando_o_rotulo():
    """Complemento do caso acima: o RÓTULO pode vir da capa, mas a COLUNA e os
    valores são sempre os certos — que é o que o chamador consome."""
    valores, _col = _ler(
        {"Aba1": [["RELATÓRIO GERENCIAL", "gerado em 01/01"],
                  ["subtítulo", ""],
                  ["Situação", "Nº PROCESSO"],
                  ["ativo", CNJ_A],
                  ["ativo", CNJ_B]]},
        "Nº PROCESSO",
    )
    assert [v for v in valores if sheets._normalizar_cnj(v)] == [CNJ_A, CNJ_B]


def test_escolhe_a_aba_com_mais_cnjs():
    valores, _col = _ler(
        {"Resumo": [["PROCESSO"], [CNJ_A], [CNJ_B]],
         "Completa": [["PROCESSO"], [CNJ_A], [CNJ_B], [CNJ_C]]},
        "PROCESSO",
    )
    assert valores == [CNJ_A, CNJ_B, CNJ_C]


def test_cnj_de_20_digitos_como_numero_nao_e_recuperavel():
    """Documenta um limite dos DADOS, não do leitor: o .xlsx guarda número como
    double, então um CNJ de 20 dígitos digitado como número já chega corrompido
    (valia o mesmo com pandas). Estas planilhas guardam CNJ como texto."""
    # O que importa: nenhum CNJ FALSO sai do valor corrompido — a aba é
    # simplesmente reportada como "sem coluna de processo".
    with pytest.raises(RuntimeError, match="nenhuma aba com coluna de processo"):
        _ler(
            {"Aba1": [["PROCESSO"], [40088461020258260309], [11041737520258130024]]},
            "PROCESSO",
        )


def test_cnj_pontuado_como_texto_e_reconhecido():
    """O formato real das planilhas do escritório."""
    valores, col = _ler({"Aba1": [["PROCESSO"], [CNJ_A], [CNJ_B]]}, "PROCESSO")
    assert col == "PROCESSO"
    assert [sheets._normalizar_cnj(v) for v in valores] == [CNJ_A, CNJ_B]


def test_cnj_de_20_digitos_como_texto_recupera_a_pontuacao():
    valores, _col = _ler(
        {"Aba1": [["PROCESSO"], ["40088461020258260309"], ["11041737520258130024"]]},
        "PROCESSO",
    )
    assert [sheets._normalizar_cnj(v) for v in valores] == [CNJ_A, CNJ_B]


def test_linhas_curtas_nao_estouram_indice():
    """Linhas com menos células que o cabeçalho viram '' em vez de IndexError."""
    valores, _col = _ler(
        {"Aba1": [["Cliente", "PROCESSO"],
                  ["ACME", CNJ_A],
                  ["SO_CLIENTE"],
                  ["ACME", CNJ_B]]},
        "PROCESSO",
    )
    assert valores == [CNJ_A, "", CNJ_B]


def test_aba_sem_nenhum_cnj_levanta():
    with pytest.raises(RuntimeError, match="nenhuma aba com coluna de processo"):
        _ler({"Vazia": [["a", "b"], ["1", "2"]]}, "PROCESSO")


# ---------------------------------------------------------------------
# Pré-cômputo da contagem de CNJ (o que substituiu os 9 reparses do pandas)
# ---------------------------------------------------------------------
def test_mapa_cnj_conta_por_coluna_e_desconta_o_cabecalho():
    grade = [
        ["cabecalho", "PROCESSO"],
        ["x", CNJ_A],
        ["y", CNJ_B],
    ]
    n_col, total, prefixo = sheets._mapa_cnj(grade)
    assert n_col == 2
    assert total == [0, 2]          # só a coluna 1 tem CNJs
    assert prefixo[1][1] == 0       # nenhum CNJ na 1ª linha (é o cabeçalho)
    # Contagem abaixo do cabeçalho na linha 0 = total - prefixo[.][1]
    assert total[1] - prefixo[1][1] == 2


def test_mapa_cnj_grade_vazia():
    assert sheets._mapa_cnj([]) == (0, [], [])


# ---------------------------------------------------------------------
# Coleta agregada
# ---------------------------------------------------------------------
def test_coletar_dedup_por_cnj_primeira_campanha_vence(monkeypatch):
    xlsx = _xlsx({"Aba1": [["PROCESSO"], [CNJ_A], [CNJ_B], [CNJ_A]]})
    monkeypatch.setattr(sheets, "_sessao", lambda: (_SessaoFalsa(xlsx), "robo@exemplo"))
    monkeypatch.setattr(sheets, "SHEETS", {
        "Campanha A": ("fid-a", "PROCESSO"),
        "Campanha B": ("fid-b", "PROCESSO"),
    })
    regs = sheets.coletar_processos_das_planilhas(log=lambda s: None)
    # CNJ_A e CNJ_B saem só uma vez, atribuídos à 1ª campanha que os trouxe.
    assert regs == [
        {"numero": CNJ_A, "campanha": "Campanha A"},
        {"numero": CNJ_B, "campanha": "Campanha A"},
    ]


def test_coletar_estoura_quando_nenhuma_planilha_pode_ser_lida(monkeypatch):
    """Falha de configuração (API desabilitada / sem compartilhamento) não pode
    ser confundida com "nada novo a fazer"."""
    class _SessaoQueFalha:
        def get(self, *a, **k):
            raise RuntimeError("403 Forbidden")

    monkeypatch.setattr(sheets, "_sessao", lambda: (_SessaoQueFalha(), "robo@exemplo"))
    monkeypatch.setattr(sheets, "SHEETS", {"Campanha A": ("fid-a", "PROCESSO")})
    with pytest.raises(RuntimeError, match="nenhuma planilha pôde ser lida"):
        sheets.coletar_processos_das_planilhas(log=lambda s: None)


def test_sessao_exige_credencial(monkeypatch, tmp_path):
    monkeypatch.setattr(sheets, "GOOGLE_SA_PATH", tmp_path / "nao_existe.json")
    with pytest.raises(RuntimeError, match="conta de serviço não encontrada"):
        sheets._sessao()


def test_truncamento_por_max_linhas_e_avisado(monkeypatch):
    monkeypatch.setattr(sheets, "MAX_LINHAS", 3)
    avisos: list[str] = []
    xlsx = _xlsx({"Aba1": [["PROCESSO"], [CNJ_A], [CNJ_B], [CNJ_C]]})
    valores, _col = sheets.ler_planilha(
        _SessaoFalsa(xlsx), "fid", "PROCESSO", log=avisos.append
    )
    assert valores == [CNJ_A, CNJ_B]  # 3 linhas lidas: cabeçalho + 2
    assert any("truncada" in a for a in avisos)
