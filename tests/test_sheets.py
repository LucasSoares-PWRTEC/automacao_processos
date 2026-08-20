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


# ---------------------------------------------------------------------
# Planilha de senhas dos tribunais
# ---------------------------------------------------------------------
def _ler_senhas(abas: dict[str, list[list]]):
    return sheets.ler_senhas_da_planilha(_SessaoFalsa(_xlsx(abas)), "fid", log=lambda s: None)


def _tabela(linhas: list[list], cab=("tj", "Login", "Senha")) -> dict[str, list[list]]:
    return {"Senhas": [list(cab)] + linhas}


LINHAS = [
    ["TJSP - Esaj", "111", "s1"],
    ["TJMG - PJE", "222", "s2"],
    ["TJGO - Projudi", "333", "s3"],
]


def test_achar_col_ignora_cabecalho_vazio():
    """Cabeçalho em branco casava QUALQUER alvo por "contém" e elegia uma coluna
    vazia. Só não explodia porque a busca de processo regatava com conta_cnj."""
    assert sheets._achar_col(["", "Tribunal"], "tj") is None
    assert sheets._achar_col(["", "tj"], "tj") == 1


def test_achar_col_respeita_a_exclusao():
    assert sheets._achar_col(["Senha", "Senha nova"], "senha") == 0
    assert sheets._achar_col(["Senha", "Senha nova"], "senha", {0}) == 1
    assert sheets._achar_col(["Senha"], "senha", {0}) is None


def test_senhas_layout_legado_tj_login_senha():
    itens, como = _ler_senhas(_tabela(LINHAS))
    assert itens == [
        {"tj": "TJSP - Esaj", "login": "111", "senha": "s1"},
        {"tj": "TJMG - PJE", "login": "222", "senha": "s2"},
        {"tj": "TJGO - Projudi", "login": "333", "senha": "s3"},
    ]
    assert "aba 'Senhas'" in como and "linha 1" in como


def test_senhas_aceita_sinonimos_do_cabecalho():
    itens, _ = _ler_senhas(_tabela(LINHAS, cab=("Tribunal", "Usuário", "Password")))
    assert [i["tj"] for i in itens] == ["TJSP - Esaj", "TJMG - PJE", "TJGO - Projudi"]
    assert [i["senha"] for i in itens] == ["s1", "s2", "s3"]


def test_senhas_prefere_a_coluna_de_senha_atual():
    """"Senha" casaria "Senha antiga" por 'contém'; o sinônimo mais específico
    vem primeiro justamente para isso."""
    abas = {"Senhas": [["tj", "Login", "Senha antiga", "Senha nova"]] + [
        ["TJSP - Esaj", "111", "VELHA", "nova1"],
        ["TJMG - PJE", "222", "VELHA", "nova2"],
        ["TJGO - Projudi", "333", "VELHA", "nova3"],
    ]}
    itens, _ = _ler_senhas(abas)
    assert [i["senha"] for i in itens] == ["nova1", "nova2", "nova3"]


def test_senhas_com_cabecalho_deslocado_por_capa():
    abas = {"Senhas": [
        ["ACESSOS DO ESCRITÓRIO", "", ""],
        ["atualizado em janeiro", "", ""],
        ["tj", "Login", "Senha"],
    ] + LINHAS}
    itens, como = _ler_senhas(abas)
    assert len(itens) == 3 and "linha 3" in como


def test_senhas_escolhe_a_aba_com_mais_linhas():
    abas = {
        "Resumo": [["tj", "Login", "Senha"]] + LINHAS,
        "Senhas": [["tj", "Login", "Senha"]] + LINHAS + [["TJDF - 1º Grau", "444", "s4"]],
    }
    itens, como = _ler_senhas(abas)
    assert len(itens) == 4 and "aba 'Senhas'" in como


def test_senhas_desempata_pela_coluna_de_senha_preenchida():
    """Aba de resumo com a coluna de senha vazia não pode ganhar da tabela real:
    aplicá-la seria propor apagar tudo."""
    abas = {
        "Resumo": [["tj", "Login", "Senha"]] + [[l[0], l[1], ""] for l in LINHAS],
        "Senhas": [["tj", "Login", "Senha"]] + LINHAS,
    }
    itens, como = _ler_senhas(abas)
    assert "aba 'Senhas'" in como and all(i["senha"] for i in itens)


def test_senhas_rejeita_aba_sem_as_tres_colunas():
    with pytest.raises(RuntimeError, match="nenhuma aba com colunas"):
        _ler_senhas({"Aba1": [["tj", "Login"], ["TJSP - Esaj", "111"]]})


def test_senhas_rejeita_tabela_curta_demais():
    """Duas linhas é capa/dashboard, não a tabela de senhas do escritório."""
    with pytest.raises(RuntimeError, match="nenhuma aba com colunas"):
        _ler_senhas(_tabela(LINHAS[:2]))


def test_senhas_rejeita_coluna_de_senha_inteiramente_vazia():
    with pytest.raises(RuntimeError, match="nenhuma aba com colunas"):
        _ler_senhas(_tabela([[l[0], l[1], ""] for l in LINHAS]))


def test_senhas_ignora_linha_sem_tribunal_e_aguenta_linha_curta():
    ws = _xlsx({"Senhas": [["tj", "Login", "Senha"]] + LINHAS + [
        ["", "999", "orfa"],          # sem tribunal: fora
        ["TÍTULO DE SEÇÃO"],          # linha curta, só rótulo: fora
        ["TJDF - 1º Grau", "444"],    # linha curta com login: entra, senha vazia
    ]})
    itens, _ = sheets.ler_senhas_da_planilha(_SessaoFalsa(ws), "fid", log=lambda s: None)
    assert [i["tj"] for i in itens] == [
        "TJSP - Esaj", "TJMG - PJE", "TJGO - Projudi", "TJDF - 1º Grau",
    ]
    assert itens[-1] == {"tj": "TJDF - 1º Grau", "login": "444", "senha": ""}


def test_senhas_ignora_aba_ilegivel_e_segue_para_a_boa():
    """Uma aba que estoura na leitura não pode derrubar a planilha inteira."""
    xlsx = _xlsx({"Quebrada": [["tj", "Login", "Senha"]] + LINHAS,
                  "Senhas": [["tj", "Login", "Senha"]] + LINHAS})

    class _SessaoComAbaRuim(_SessaoFalsa):
        pass

    original = sheets._grade
    def _grade_falha(ws, log):
        if ws.title == "Quebrada":
            raise RuntimeError("aba corrompida")
        return original(ws, log)

    sheets._grade = _grade_falha
    try:
        itens, como = sheets.ler_senhas_da_planilha(
            _SessaoComAbaRuim(xlsx), "fid", log=lambda s: None
        )
    finally:
        sheets._grade = original
    assert len(itens) == 3 and "aba 'Senhas'" in como


def test_senhas_rejeita_aba_sem_coluna_de_login():
    """Sem as TRÊS colunas o candidato cai — inclusive quando só falta o login."""
    with pytest.raises(RuntimeError, match="nenhuma aba com colunas"):
        _ler_senhas({"Aba1": [["tj", "Senha"]] + [[l[0], l[2]] for l in LINHAS]})


def test_senhas_mantem_as_linhas_de_recado_do_escritorio():
    """O arquivo real carrega "senha robo", "SENHA CERTIFICADO A1: ..." — é
    informação do escritório, não lixo a filtrar."""
    itens, _ = _ler_senhas(_tabela(LINHAS + [["senha robo", "", "xyz"]]))
    assert itens[-1] == {"tj": "senha robo", "login": "", "senha": "xyz"}


def test_senhas_login_numerico_perde_o_ponto_zero():
    itens, _ = _ler_senhas(_tabela([
        ["TJSP - Esaj", 12345678900.0, "s1"],
        ["TJMG - PJE", "222", "s2"],
        ["TJGO - Projudi", "333", "s3"],
    ]))
    assert itens[0]["login"] == "12345678900"


def test_senhas_cpf_numerico_recupera_o_zero_a_esquerda():
    """Célula numérica come o zero do CPF; sem recuperá-lo o login gravado
    quebraria o e-SAJ. 9 dígitos ou menos não é CPF (pode ser OAB) e fica como
    está."""
    itens, _ = _ler_senhas(_tabela([
        ["TJSP - Esaj", 3015257650, "s1"],
        ["TJMG - PJE", 12345678, "s2"],
        ["TJGO - Projudi", "333", "s3"],
    ]))
    assert itens[0]["login"] == "03015257650"
    assert itens[1]["login"] == "12345678"


def test_senhas_nunca_expoem_senha_no_log_nem_na_descricao():
    linhas: list[str] = []
    itens, como = sheets.ler_senhas_da_planilha(
        _SessaoFalsa(_xlsx(_tabela(LINHAS))), "fid", log=linhas.append
    )
    assert itens  # leu mesmo
    for senha in ("s1", "s2", "s3"):
        assert senha not in como
        assert not any(senha in l for l in linhas)


def test_coletar_credenciais_estoura_quando_nao_consegue_ler(monkeypatch):
    """Sem acesso não pode virar "as senhas já estão em dia"."""
    class _SessaoQueFalha:
        def get(self, *a, **k):
            raise RuntimeError("404 File not found")

    monkeypatch.setattr(sheets, "_sessao", lambda: (_SessaoQueFalha(), "robo@exemplo"))
    with pytest.raises(RuntimeError, match="404"):
        sheets.coletar_credenciais_da_planilha(log=lambda s: None)


def test_coletar_credenciais_anuncia_a_conta_de_servico(monkeypatch):
    linhas: list[str] = []
    monkeypatch.setattr(
        sheets, "_sessao",
        lambda: (_SessaoFalsa(_xlsx(_tabela(LINHAS))), "robo@exemplo"),
    )
    itens = sheets.coletar_credenciais_da_planilha(log=linhas.append)
    assert len(itens) == 3
    assert any("robo@exemplo" in l for l in linhas)
    assert not any("s1" in l for l in linhas)  # nem aqui vaza senha


def test_id_da_planilha_aceita_url_completa_ou_id_cru():
    url = ("https://docs.google.com/spreadsheets/d/"
           "1BkNU0gkcl9A3O88i10e1Qbih4Abf36nthfpIj478bZg/edit?gid=0#gid=0")
    assert sheets._id_de_url(url) == "1BkNU0gkcl9A3O88i10e1Qbih4Abf36nthfpIj478bZg"
    assert sheets._id_de_url("  fid-cru  ") == "fid-cru"
    assert sheets._id_de_url("") == ""


def test_id_padrao_aponta_para_a_planilha_do_escritorio():
    assert sheets.SHEET_SENHAS == "1BkNU0gkcl9A3O88i10e1Qbih4Abf36nthfpIj478bZg"


@pytest.mark.parametrize("erro, esperado", [
    ("404 File not found: 1BkNU…", "compartilhad"),
    ("403 Forbidden", "compartilhad"),
    ("Google Drive API has not been used in project…", "habilitad"),
    ("HTTPSConnectionPool: getaddrinfo failed", "internet"),
    ("credencial da conta de serviço não encontrada em …", "token_google_api"),
    ("nenhuma aba com colunas de tribunal/login/senha (abas: ['Plan1'])", "cabeçalho"),
])
def test_explicar_falha_traduz_os_casos_reais(erro, esperado):
    assert esperado in sheets.explicar_falha_de_acesso(erro)


def test_explicar_falha_nomeia_a_conta_e_devolve_o_cru_quando_nao_conhece():
    msg = sheets.explicar_falha_de_acesso("404 not found", robot="robo@exemplo")
    assert "robo@exemplo" in msg and "Leitor" in msg
    assert sheets.explicar_falha_de_acesso("pane desconhecida") == "pane desconhecida"
    # sem robô explícito, cai na conta de serviço que vai no programa
    assert sheets.ROBOT_PADRAO in sheets.explicar_falha_de_acesso("403")
