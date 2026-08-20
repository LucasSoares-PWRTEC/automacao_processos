# -*- coding: utf-8 -*-
"""Cooldown do monitoramento: UMA tentativa por processo por dia.

POR QUE ISTO EXISTE (pedido do usuário, 2026-08-20): "para evitar flood, tenta
enriquecer um processo, tenta só no próximo dia".

Antes, a fila era escolhida só por STATUS. Duas consequências:

  • `--retry-falhas` (ou o botão da GUI) rodado duas vezes no mesmo dia
    reconsultava os MESMOS processos nos MESMOS tribunais, minutos depois. Uma
    tentativa repetida no mesmo dia não muda o resultado — só soma requisições,
    e é volume que faz o tribunal bloquear a conta compartilhada.
  • pior: um processo que derrubasse o app no meio da passada não deixava
    registro nenhum, então reabrir o app o colocava na fila outra vez. O laço
    "abre → tenta → cai → reabre → tenta" é flood sem ninguém perceber.

Daí o registro da tentativa ser gravado ANTES da consulta (log append-only, com
fsync), e não junto do resultado.

Nada aqui sobe navegador nem lê planilha: a passada é exercitada com dublês.
"""

from __future__ import annotations

import pytest

from organizador import monitoramento as M


# ---------------------------------------------------------------------
# Diário de tentativas
# ---------------------------------------------------------------------
def test_marca_e_le_a_tentativa_do_dia(tmp_path):
    log = tmp_path / "tentativas.log"
    assert M.tentativas_de_hoje(log) == set()

    M.marcar_tentativa("1102758-57.2025.8.13.0024", log)
    assert M.tentativas_de_hoje(log) == {"1102758-57.2025.8.13.0024"}


def test_linhas_de_outros_dias_sao_ignoradas_e_podadas(tmp_path):
    """O arquivo guarda só o dia corrente — senão cresceria para sempre."""
    log = tmp_path / "tentativas.log"
    log.write_text(
        "2026-08-18\t1111111-11.2025.8.13.0024\n"
        "2026-08-19\t2222222-22.2025.8.13.0024\n",
        encoding="utf-8",
    )
    M.marcar_tentativa("3333333-33.2025.8.13.0024", log)

    assert M.tentativas_de_hoje(log) == {"3333333-33.2025.8.13.0024"}
    # e o de ontem não ficou ocupando o arquivo
    assert "2026-08-18" not in log.read_text(encoding="utf-8")


def test_arquivo_corrompido_nao_derruba_nada(tmp_path):
    log = tmp_path / "tentativas.log"
    log.write_bytes(b"\xff\xfe lixo binario \x00")
    assert M.tentativas_de_hoje(log) == set()


def test_marcar_em_caminho_impossivel_nao_levanta(tmp_path):
    """Disco cheio/pasta sem permissão não pode derrubar a passada."""
    arquivo = tmp_path / "arquivo"
    arquivo.write_text("sou um arquivo", encoding="utf-8")
    M.marcar_tentativa("1102758-57.2025.8.13.0024", arquivo / "sub" / "log")  # não levanta


def test_marcar_duas_vezes_nao_duplica_na_leitura(tmp_path):
    log = tmp_path / "tentativas.log"
    M.marcar_tentativa("1102758-57.2025.8.13.0024", log)
    M.marcar_tentativa("1102758-57.2025.8.13.0024", log)
    assert M.tentativas_de_hoje(log) == {"1102758-57.2025.8.13.0024"}


# ---------------------------------------------------------------------
# filtrar_tentados_hoje
# ---------------------------------------------------------------------
_N1 = "1102758-57.2025.8.13.0024"
_N2 = "1102775-93.2025.8.13.0024"


def _pendentes(*numeros):
    return {n: {"uf": "MG", "candidatos": [], "existing": None, "campanha": ""} for n in numeros}


def test_quem_nunca_foi_tentado_passa():
    fila, adiados = M.filtrar_tentados_hoje(_pendentes(_N1, _N2), {}, set())
    assert set(fila) == {_N1, _N2}
    assert adiados == []


def test_quem_esta_no_log_de_hoje_fica_para_amanha():
    fila, adiados = M.filtrar_tentados_hoje(_pendentes(_N1, _N2), {}, {_N1})
    assert set(fila) == {_N2}
    assert adiados == [_N1]


def test_resultado_gravado_hoje_tambem_conta_como_tentativa():
    """2º sinal do gate: sobrevive a apagar o log de tentativas."""
    estado = {_N1: {"status": "nao_encontrado", "quando": f"{M._hoje()}T14:03:11"}}
    fila, adiados = M.filtrar_tentados_hoje(_pendentes(_N1), estado, set())
    assert fila == {}
    assert adiados == [_N1]


def test_resultado_de_ontem_volta_a_fila():
    estado = {_N1: {"status": "nao_encontrado", "quando": "2026-08-19T23:59:00"}}
    fila, _ = M.filtrar_tentados_hoje(_pendentes(_N1), estado, set())
    assert set(fila) == {_N1}, "o cooldown é de um dia, não permanente"


def test_estado_sem_quando_nao_bloqueia():
    """Semeado como 'pendente' e nunca alcançado (limit/cancelamento): não foi
    tentado, então continua elegível hoje mesmo."""
    fila, _ = M.filtrar_tentados_hoje(_pendentes(_N1), {_N1: {"status": "pendente"}}, set())
    assert set(fila) == {_N1}


def test_quando_com_lixo_nao_bloqueia_para_sempre():
    estado = {_N1: {"status": "falha", "quando": "sei lá quando"}}
    fila, _ = M.filtrar_tentados_hoje(_pendentes(_N1), estado, set())
    assert set(fila) == {_N1}


# ---------------------------------------------------------------------
# A passada inteira (dublês) — o comportamento que o usuário pediu
# ---------------------------------------------------------------------
class _ProcFalso:
    classe = "EXECUÇÃO DE TÍTULO EXTRAJUDICIAL"


class _PoolFalso:
    def __init__(self, *a, **k):
        pass

    def obter(self, tj_label):
        return object()

    def fechar_todos(self):
        pass


class _GravadorFalso:
    def __init__(self, *a, **k):
        pass

    def talvez(self, estado):
        return False

    def finalizar(self, estado):
        pass


@pytest.fixture
def passada(tmp_path, monkeypatch):
    """Prepara `rodar_passada` sem rede, sem disco do usuário e sem navegador.

    Devolve um objeto com `consultas` (números consultados em cada corrida),
    `estado` (persistido entre corridas, como o JSON real) e `rodar()`.
    """
    from organizador import sheets

    numeros = [_N1, _N2]
    monkeypatch.setattr(
        sheets, "coletar_processos_das_planilhas",
        lambda log=None: [{"numero": n, "campanha": "DIRECIONAL"} for n in numeros],
    )
    monkeypatch.setattr(M, "DIARIO_TENTATIVAS", tmp_path / "tentativas.log")
    monkeypatch.setattr(M, "PAUSA_ENTRE_CONSULTAS", 0)
    monkeypatch.setattr(M, "_preparar_env_local", lambda: None)
    monkeypatch.setattr(M, "_numeros_no_cache", lambda: set())
    monkeypatch.setattr(M, "_teto_navegadores", lambda: 1)
    monkeypatch.setattr(M, "PoolScrapers", _PoolFalso)
    monkeypatch.setattr(M, "_GravadorThrottled", _GravadorFalso)
    monkeypatch.setattr(M, "_gerar_relatorios", lambda *a, **k: None)
    monkeypatch.setattr(M, "_merge_e_salvar", lambda *a, **k: _ProcFalso())
    monkeypatch.setattr(M, "_autor", lambda p: "AUTOR X")
    monkeypatch.setattr(M, "_reus_joined", lambda p: "RÉU Y")

    class Ctx:
        def __init__(self):
            self.consultas: list[str] = []
            self.estado: dict = {}
            self.logs: list[str] = []
            self.erro: Exception | None = None

        def rodar(self, **kw):
            self.consultas.clear()
            self.logs.clear()
            return M.rodar_passada(log=self.logs.append, **kw)

    ctx = Ctx()
    monkeypatch.setattr(M, "_carregar_estado", lambda *a, **k: ctx.estado)
    monkeypatch.setattr(M, "_gravar_estado", lambda estado, *a, **k: None)

    def _consultar(scraper, site, numero, tribunal, uf, **kw):
        ctx.consultas.append(numero)
        if ctx.erro is not None:
            raise ctx.erro
        return _ProcFalso()

    monkeypatch.setattr(M, "_consultar_e_extrair", _consultar)
    return ctx


def test_segunda_passada_no_mesmo_dia_nao_consulta_nada(passada):
    """A REGRESSÃO: `--retry-falhas` (ou o botão) rodado de novo no mesmo dia
    remartelava os mesmos processos nos mesmos tribunais."""
    passada.erro = RuntimeError("não encontrado")
    r1 = passada.rodar()
    assert set(passada.consultas) == {_N1, _N2}
    assert r1["nao_encontrados"] == 2

    r2 = passada.rodar(retry_falhas=True)
    assert passada.consultas == [], "reconsultou no mesmo dia"
    assert r2["adiados"] == 2
    assert any("já foram tentados hoje" in l for l in passada.logs), passada.logs


def test_passada_bem_sucedida_mapeia_tudo_e_nao_repete(passada):
    r1 = passada.rodar()
    assert set(passada.consultas) == {_N1, _N2}
    assert r1["mapeados_agora"] == 2
    assert r1["adiados"] == 0

    passada.rodar(retry_falhas=True)
    assert passada.consultas == []


def test_no_dia_seguinte_os_pendentes_voltam(passada, monkeypatch):
    passada.erro = RuntimeError("não encontrado")
    passada.rodar()
    assert set(passada.consultas) == {_N1, _N2}
    assert passada.estado[_N1]["status"] == "nao_encontrado"

    # Mesmo dia: nada. Dia seguinte: voltam (com retry_falhas, como antes).
    passada.rodar(retry_falhas=True)
    assert passada.consultas == []

    monkeypatch.setattr(M, "_hoje", lambda: "2999-12-31")  # amanhã
    passada.rodar(retry_falhas=True)
    assert set(passada.consultas) == {_N1, _N2}, "o cooldown virou bloqueio permanente"


def test_tentativa_fica_registrada_mesmo_com_a_consulta_estourando(passada):
    """O caso que geraria o laço de flood: erro técnico no meio da passada.
    O registro é anterior à consulta, então o processo não volta hoje."""
    passada.erro = ValueError("navegador morreu")
    passada.rodar()
    assert set(passada.consultas) == {_N1, _N2}
    assert M.tentativas_de_hoje() == {_N1, _N2}

    passada.rodar(retry_falhas=True)
    assert passada.consultas == []


def test_um_processo_por_dia_mesmo_com_varios_sistemas_candidatos(passada):
    """O fallback entre sistemas da UF continua sendo UMA tentativa: o processo
    é tentado em cada candidato na mesma passada, e só volta amanhã."""
    passada.erro = RuntimeError("não encontrado")
    passada.rodar()
    assert len(passada.consultas) > 2, "o fallback entre sistemas foi perdido"
    assert M.tentativas_de_hoje() == {_N1, _N2}


def test_processo_cancelado_antes_da_consulta_nao_gasta_o_dia(passada):
    """Cancelar antes de chegar no processo não pode queimar a tentativa dele:
    ele nunca foi ao tribunal."""
    import threading

    cancelar = threading.Event()
    cancelar.set()
    passada.rodar(cancel=cancelar)
    assert passada.consultas == []
    assert M.tentativas_de_hoje() == set()

    passada.rodar()
    assert passada.consultas == [_N1, _N2]


def test_dry_run_nao_gasta_a_tentativa_do_dia(passada):
    passada.rodar(dry_run=True)
    assert passada.consultas == []
    assert M.tentativas_de_hoje() == set()


def test_limit_nao_queima_a_tentativa_de_quem_ficou_de_fora(passada):
    """`--limit 1` processa um; o outro não foi tentado e continua elegível."""
    passada.rodar(limit=1)
    assert len(passada.consultas) == 1
    tentados = M.tentativas_de_hoje()
    assert len(tentados) == 1

    passada.rodar(limit=1)
    assert len(passada.consultas) == 1
    assert M.tentativas_de_hoje() == {_N1, _N2}
