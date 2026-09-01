# -*- coding: utf-8 -*-
"""Preenchimento de lacuna entre as fontes (base > online > embutido).

O BUG QUE ISTO CONSERTA
-----------------------
A mescla era "o primeiro vence o REGISTRO INTEIRO": achado o número numa fonte
de maior prioridade, a de baixo era descartada junto com tudo que ela tinha. Na
prática um processo "parava de preencher" — o dado existe dentro do programa,
mas o registro que ganhou vinha sem ele.

Medido na base real em 31/08/2026: 324 processos da pasta base e 19 do cache
online sombreavam o embutido PERDENDO campo (quase todos `valor_causa`, alguns
`classe` e `comarca`), e nenhum deles era melhor que o embutido em outro campo —
o descarte era perda pura.

A prioridade NÃO muda: dado preenchido do vencedor sempre vence. Estes testes
travam as duas metades disso — o que preenche e o que NÃO sobrescreve.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from organizador.indice_processos import (
    ORIGEM_EMBUTIDO,
    ResumoProcesso,
    carregar_processo_completo,
    carregar_resumos_pesquisa,
)
from organizador.models import Processo
from organizador.pesquisa import (
    _vazio,
    mesclar_processos_por_prioridade,
    preencher_lacunas,
)


# ---------------------------------------------------------------------------
# preencher_lacunas
# ---------------------------------------------------------------------------

def test_preenche_o_campo_que_falta():
    vencedor = Processo(numero="1", classe="Execução", valor_causa="")
    reserva = Processo(numero="1", classe="OUTRA", valor_causa="R$ 1.000,00")

    preencher_lacunas(vencedor, reserva)

    assert vencedor.valor_causa == "R$ 1.000,00", "não preencheu a lacuna"
    assert vencedor.classe == "Execução", "sobrescreveu dado que o vencedor tinha"


def test_prioridade_nao_muda_nunca():
    """O ponto mais importante: isto é preenchimento de LACUNA, não merge que
    reescreve. Dado preenchido do vencedor vence sempre."""
    vencedor = Processo(numero="1", classe="A", comarca="Santos", valor_causa="R$ 5")
    reserva = Processo(numero="1", classe="B", comarca="Bauru", valor_causa="R$ 9")

    preencher_lacunas(vencedor, reserva)

    assert (vencedor.classe, vencedor.comarca, vencedor.valor_causa) == (
        "A", "Santos", "R$ 5",
    )


def test_ordem_das_reservas_e_respeitada():
    vencedor = Processo(numero="1", comarca="")
    preencher_lacunas(
        vencedor,
        Processo(numero="1", comarca="PRIMEIRA"),
        Processo(numero="1", comarca="SEGUNDA"),
    )
    assert vencedor.comarca == "PRIMEIRA"


def test_reserva_pode_ser_dict():
    """O embutido chega como dict cru do SQLite (base_embutida.ler_processo),
    sem custo de virar Processo."""
    vencedor = Processo(numero="1", valor_causa="")
    preencher_lacunas(vencedor, {"numero": "1", "valor_causa": "R$ 42,00"})
    assert vencedor.valor_causa == "R$ 42,00"


def test_sem_reserva_nao_faz_nada():
    vencedor = Processo(numero="1", classe="X")
    assert preencher_lacunas(vencedor) is vencedor
    assert preencher_lacunas(vencedor, None).classe == "X"


def test_partes_vazias_sao_preenchidas():
    """`partes` vazio é o pior caso: o processo aparece "sem parte" e nenhuma
    petição preenche autor/réu."""
    vencedor = Processo(numero="1", partes=[])
    reserva = {"numero": "1", "partes": [{"nome": "BANCO ALFA", "papel": "EXEQUENTE"}]}

    preencher_lacunas(vencedor, reserva)

    assert [p.get("nome") if isinstance(p, dict) else p.nome
            for p in vencedor.partes] == ["BANCO ALFA"]


# ---------------------------------------------------------------------------
# Campos acoplados: eventos + total + 1ª/última movimentação
# ---------------------------------------------------------------------------
# Os três últimos são derivados da lista de eventos (num metadados.json real,
# `total_eventos == len(eventos)`). Preencher a data e deixar a timeline vazia
# mostraria movimentação num processo sem evento nenhum.

def test_grupo_de_movimentacao_vem_junto():
    vencedor = Processo(numero="1", eventos=[], total_eventos=0,
                        primeira_movimentacao="", ultima_movimentacao="")
    reserva = {
        "numero": "1",
        "eventos": [{"data": "01/01/2025", "descricao": "Distribuído"}],
        "total_eventos": 1,
        "primeira_movimentacao": "01/01/2025",
        "ultima_movimentacao": "01/01/2025 — Distribuído",
    }

    preencher_lacunas(vencedor, reserva)

    assert len(vencedor.eventos) == 1
    assert vencedor.total_eventos == 1
    assert vencedor.primeira_movimentacao == "01/01/2025"
    assert vencedor.ultima_movimentacao == "01/01/2025 — Distribuído"


def test_quem_tem_eventos_nao_herda_data_de_outra_fonte():
    """Vencedor COM eventos e sem a string de última mov.: não puxa a data de
    outra fonte, senão exibiria uma movimentação que não está na timeline dele."""
    vencedor = Processo(
        numero="1",
        eventos=[{"data": "05/05/2025", "descricao": "Sentença"}],
        total_eventos=1, ultima_movimentacao="",
    )
    reserva = {"numero": "1", "eventos": [{"data": "01/01/2020", "descricao": "X"}],
               "total_eventos": 1, "ultima_movimentacao": "01/01/2020 — X"}

    preencher_lacunas(vencedor, reserva)

    assert vencedor.ultima_movimentacao == "", "herdou data de timeline alheia"
    assert vencedor.total_eventos == 1


def test_zero_nao_e_lacuna():
    """`total_eventos` 0 é consistente com `eventos` vazio. Tratar 0 como lacuna
    faria um processo sem movimentação herdar o total de outro."""
    assert _vazio(0) is False
    assert _vazio("") is True
    assert _vazio("   ") is True
    assert _vazio(None) is True
    assert _vazio([]) is True
    assert _vazio(["x"]) is False


def test_resumo_preenche_ultima_movimentacao():
    """No ResumoProcesso NÃO existe `eventos` — lá `ultima_movimentacao` é só uma
    coluna da lista, e tem de ser preenchida normalmente. O grupo acoplado só
    vale quando o líder existe no destino."""
    vencedor = ResumoProcesso(numero="1", ultima_movimentacao="")
    preencher_lacunas(vencedor, ResumoProcesso(numero="1",
                                               ultima_movimentacao="09/09/2025 — Ato"))
    assert vencedor.ultima_movimentacao == "09/09/2025 — Ato"


# ---------------------------------------------------------------------------
# mesclar_processos_por_prioridade
# ---------------------------------------------------------------------------

def test_duplicata_nao_e_mais_descartada_inteira():
    base = [Processo(numero="1", classe="Execução", valor_causa="")]
    embutido = [Processo(numero="1", classe="OUTRA", valor_causa="R$ 7,00")]

    fora = mesclar_processos_por_prioridade(base, embutido)

    assert len(fora) == 1, "a dedup por número parou de funcionar"
    assert fora[0].classe == "Execução"
    assert fora[0].valor_causa == "R$ 7,00"


def test_numero_vazio_continua_sem_dedup():
    fontes = ([Processo(numero="", classe="A")], [Processo(numero="", classe="B")])
    assert len(mesclar_processos_por_prioridade(*fontes)) == 2


# ---------------------------------------------------------------------------
# Fim a fim: da pasta no disco até o Processo que preenche a petição
# ---------------------------------------------------------------------------

def _escrever(pasta, numero, **campos):
    d = {"numero": numero, **campos}
    alvo = pasta / numero
    alvo.mkdir(parents=True, exist_ok=True)
    (alvo / "metadados.json").write_text(json.dumps(d, ensure_ascii=False),
                                         encoding="utf-8")
    return alvo / "metadados.json"


def _cenario(tmp_path):
    """Pasta base com o registro INCOMPLETO (o que vencia e perdia o campo) e
    cache online com o completo — o sombreamento medido na base real."""
    base, online = tmp_path / "base", tmp_path / "online"
    base.mkdir(); online.mkdir()
    _escrever(base, "1", classe="Execução", valor_causa="", comarca="")
    _escrever(online, "1", classe="IGNORADA", valor_causa="R$ 1.234,00",
              comarca="Foro de Santos",
              partes=[{"nome": "BANCO ALFA", "papel": "EXEQUENTE"}])
    return base, online


def test_lista_mostra_o_campo_que_vinha_vazio(tmp_path):
    base, online = _cenario(tmp_path)
    resumos = carregar_resumos_pesquisa(
        base, online, tmp_path / "nao_existe.json.gz", tmp_path / "indice.json",
    )
    assert len(resumos) == 1
    r = resumos[0]
    assert r.classe == "Execução", "a prioridade da pasta base foi quebrada"
    assert r.valor_causa == "R$ 1.234,00", "a coluna continuou vazia"
    assert r.comarca == "Foro de Santos"
    assert r.origens_reserva, "não guardou a fonte perdedora para o clique"


def test_processo_completo_preenche_a_peticao(tmp_path):
    """O teste que representa o sintoma relatado: é este Processo que enche a
    petição, e era ele que vinha sem valor_causa."""
    base, online = _cenario(tmp_path)
    resumos = carregar_resumos_pesquisa(
        base, online, tmp_path / "nao_existe.json.gz", tmp_path / "indice.json",
    )

    proc = carregar_processo_completo(resumos[0], tmp_path / "nao_existe.json.gz")

    assert proc is not None
    assert proc.classe == "Execução"
    assert proc.valor_causa == "R$ 1.234,00", "a petição continuaria sem o valor"
    assert proc.comarca == "Foro de Santos"
    assert proc.partes, "continuou sem partes"


def test_sem_reserva_o_completo_e_o_de_sempre(tmp_path):
    """Processo que só existe numa fonte: nada de leitura extra, nada muda."""
    base = tmp_path / "base"; base.mkdir()
    _escrever(base, "9", classe="Monitória", valor_causa="R$ 3,00")
    resumos = carregar_resumos_pesquisa(
        base, tmp_path / "vazio", tmp_path / "x.json.gz", tmp_path / "i.json",
    )
    assert resumos[0].origens_reserva == ()
    proc = carregar_processo_completo(resumos[0], tmp_path / "x.json.gz")
    assert proc.classe == "Monitória" and proc.valor_causa == "R$ 3,00"


def test_indice_antigo_sem_a_chave_nova_continua_carregando(tmp_path):
    """`origens_reserva` é campo NOVO do ResumoProcesso. Um índice gravado por
    versão anterior não tem a chave — o default cobre, e é por isso que
    INDICE_VERSAO não precisou mudar."""
    antigo = asdict(ResumoProcesso(numero="1", classe="X"))
    del antigo["origens_reserva"]
    assert ResumoProcesso(**antigo).origens_reserva == ()


def test_reserva_nao_vaza_para_o_indice_em_disco(tmp_path):
    """A reserva é propriedade da DISPUTA, não do registro: persistir levaria a
    apontar para uma fonte que pode não existir na próxima abertura."""
    base, online = _cenario(tmp_path)
    indice = tmp_path / "indice.json"
    carregar_resumos_pesquisa(base, online, tmp_path / "x.json.gz", indice)

    gravado = json.loads(indice.read_text(encoding="utf-8"))
    for entrada in (gravado.get("disco") or {}).values():
        assert not entrada["resumo"].get("origens_reserva"), (
            "gravou a reserva no índice"
        )


def test_embutido_como_reserva_do_completo(tmp_path, monkeypatch):
    """Caminho real no .exe: a pasta base vence e o EMBUTIDO é a reserva."""
    import organizador.indice_processos as ip

    base = tmp_path / "base"; base.mkdir()
    _escrever(base, "1", classe="Execução", valor_causa="")
    gz = tmp_path / "processos_base.json.gz"

    monkeypatch.setattr(ip.base_embutida, "disponivel", lambda _p: True)
    monkeypatch.setattr(ip.base_embutida, "ler_resumos",
                        lambda _p: [{"numero": "1", "classe": "IGNORADA",
                                     "valor_causa": "R$ 99,00"}])
    monkeypatch.setattr(ip.base_embutida, "ler_processo",
                        lambda _p, _n: {"numero": "1", "classe": "IGNORADA",
                                        "valor_causa": "R$ 99,00"})

    resumos = carregar_resumos_pesquisa(base, tmp_path / "vazio", gz,
                                        tmp_path / "i.json")
    assert resumos[0].origens_reserva == (ORIGEM_EMBUTIDO,)

    proc = carregar_processo_completo(resumos[0], gz)
    assert proc.classe == "Execução", "prioridade quebrada"
    assert proc.valor_causa == "R$ 99,00", "não puxou do embutido"


# ---------------------------------------------------------------------------
# Campos de identidade/procedência: NUNCA vêm da reserva
# ---------------------------------------------------------------------------

def test_origem_nunca_vem_da_reserva():
    """O risco mais sério: `origem` é o arquivo que carregar_processo_completo
    abre. Herdá-lo do perdedor faria o PERDEDOR virar o vencedor, em silêncio."""
    vencedor = ResumoProcesso(numero="1", origem="", fonte="")
    preencher_lacunas(vencedor, ResumoProcesso(numero="1", origem=r"C:\perdedor\metadados.json",
                                               fonte="online"))
    assert vencedor.origem == "", "herdou a origem do perdedor"
    assert vencedor.fonte == "", "passou a mentir de onde o registro veio"


def test_numero_e_reservas_nao_sao_preenchidos():
    vencedor = ResumoProcesso(numero="", origens_reserva=())
    preencher_lacunas(vencedor, ResumoProcesso(numero="9",
                                               origens_reserva=("x", "y")))
    assert vencedor.numero == ""
    assert vencedor.origens_reserva == ()


def test_arquivos_origem_nao_atribui_pdf_alheio():
    vencedor = Processo(numero="1", arquivos_origem=[])
    preencher_lacunas(vencedor, {"numero": "1", "arquivos_origem": ["outro.pdf"]})
    assert vencedor.arquivos_origem == []
