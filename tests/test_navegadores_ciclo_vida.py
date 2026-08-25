# -*- coding: utf-8 -*-
"""Ciclo de vida dos processos de navegador (organizador/navegadores.py + GUI).

BUG QUE ORIGINOU ISTO (reproduzido ao vivo em 25/08/2026)
---------------------------------------------------------
O app fechava a janela disparando `driver.quit()` em threads DAEMON e chamava
`destroy()` na linha seguinte. Um `quit()` limpo leva ~2,1 s; o interpretador
mata thread daemon na saída do processo. Resultado medido: um `chromedriver.exe`
com o pai já morto e 8 `chrome.exe` pendurados nele, 491 MB INVISÍVEIS (headless
não tem janela nem ícone na barra), vivos até o reboot — a "lentidão no PC depois
de usar o programa". Em disco sobravam 13 perfis `scoped_dir*` (~715 MB), que o
chromedriver só apaga num `quit()` limpo.

Nada aqui sobe navegador de verdade: os testes cobrem a DECISÃO (quem é órfão,
qual perfil é lixo, em que ordem o fechamento espera e mata) e a ORDEM dos
efeitos, que é justamente o que estava errado.

Requisitos de SEGURANÇA (mais importantes que a limpeza em si):
  - nunca matar navegador de dono VIVO (uma 2ª instância do app, ou o Chrome
    pessoal do usuário);
  - nunca matar o que este processo registrou (o caminho dele é o `quit()`);
  - nunca apagar por dentro o perfil de um navegador vivo (corromperia a sessão);
  - sem listagem de processos confiável, NÃO fazer nada.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from organizador import navegadores as N
from organizador.gui import App


# ---------------------------------------------------------------------
# Registro de navegadores vivos
# ---------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _registro_limpo():
    N._vivos.clear()
    yield
    N._vivos.clear()


def test_registrar_e_esquecer():
    N.registrar(1234, "chrome")
    N.registrar(1234, "chrome")          # idempotente
    assert N.registrados() == [1234]
    N.esquecer(1234)
    assert N.registrados() == []


@pytest.mark.parametrize("pid", [None, 0, -5, "abc"])
def test_pid_invalido_nao_entra_no_registro(pid):
    """`_pid_do_driver` devolve None em driver remoto (sem `service`), e um PID
    lixo no registro faria o taskkill do fechamento mirar em nada."""
    N.registrar(pid)  # type: ignore[arg-type]
    N.esquecer(pid)   # type: ignore[arg-type]
    assert N.registrados() == []


def test_matar_registrados_esvazia_e_conta(monkeypatch):
    mortos: list[int] = []

    def matar(pid):
        mortos.append(pid)
        return pid != 999        # 999 finge um PID que já não existia

    monkeypatch.setattr(N, "matar_arvore", matar)
    N.registrar(11)
    N.registrar(999)
    assert N.matar_registrados() == 1
    assert sorted(mortos) == [11, 999]
    assert N.registrados() == [], "registro tem de ficar vazio (não matar 2x)"


# ---------------------------------------------------------------------
# Leitura da tabela de processos
# ---------------------------------------------------------------------
_SAIDA = (
    "V:4,8228,25120\n"
    "P:8228|4|chrome.exe|\"c:\\program files\\google\\chrome\\application\\chrome.exe\" "
    "--profile-directory=\"Profile 1\"\n"
    "P:26328|25120|chrome.exe|chrome.exe --user-data-dir=c:\\temp\\scoped_dir25120_83 --headless\n"
    "P:25120|22268|chromedriver.exe|chromedriver.exe --port=57344\n"
)


def test_analisar_listagem():
    vivos, procs = N._analisar_listagem(_SAIDA)
    assert vivos == {4, 8228, 25120}
    assert [p.pid for p in procs] == [8228, 26328, 25120]
    driver = procs[-1]
    assert (driver.ppid, driver.nome) == (22268, "chromedriver.exe")
    assert procs[0].cmd.startswith('"c:\\program files')


def test_analisar_listagem_ignora_lixo():
    vivos, procs = N._analisar_listagem("ruído\nV:\nP:\nP:x|y|z|w\n")
    assert vivos == set() and procs == []


def test_comando_listagem_cita_todos_os_executaveis():
    cmd = N._comando_listagem()
    for exe in N._EXES_DRIVER + N._EXES_NAVEGADOR:
        assert f"'{exe}'" in cmd
    assert "@ALVOS@" not in cmd


def test_listar_processos_devolve_none_quando_falha(monkeypatch):
    """None (e não lista vazia) é o que impede a varredura de agir no escuro."""
    monkeypatch.setattr(os, "name", "nt")

    def explodir(*a, **k):
        raise OSError("powershell não encontrado")

    monkeypatch.setattr(N.subprocess, "run", explodir)
    assert N.listar_processos() is None


def test_listar_processos_devolve_none_com_saida_vazia(monkeypatch):
    """Saída sem NENHUM PID vivo é falha disfarçada: o próprio PowerShell
    estaria na lista."""
    monkeypatch.setattr(os, "name", "nt")

    class _Fim:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(N.subprocess, "run", lambda *a, **k: _Fim())
    assert N.listar_processos() is None


# ---------------------------------------------------------------------
# Quem é órfão
# ---------------------------------------------------------------------
def _proc(pid, ppid, nome, cmd=""):
    return N.Proc(pid=pid, ppid=ppid, nome=nome, cmd=cmd.lower())


CMD_TEMP = "chrome.exe --user-data-dir=c:\\temp\\scoped_dir500_1 --headless"
CMD_PERSISTENTE = (
    "chrome.exe --user-data-dir=c:\\users\\u\\.automacao_processos\\autojur_profile"
)


def test_orfao_e_driver_sem_pai_com_navegador_pendurado():
    procs = [
        _proc(500, 4242, "chromedriver.exe"),      # 4242 não está entre os vivos
        _proc(501, 500, "chrome.exe", CMD_TEMP),
    ]
    assert N.pids_orfaos({1, 2, 3}, procs) == [500]


def test_perfil_persistente_do_autojur_tambem_conta():
    procs = [
        _proc(600, 4242, "chromedriver.exe"),
        _proc(601, 600, "chrome.exe", CMD_PERSISTENTE),
    ]
    assert N.pids_orfaos({1}, procs) == [600]


def test_driver_com_pai_vivo_nunca_e_tocado():
    """O caso perigoso: uma SEGUNDA instância do app rodando agora. O driver
    dela tem pai vivo (o app) e não pode ser morto."""
    procs = [
        _proc(500, 300, "chromedriver.exe"),
        _proc(501, 500, "chrome.exe", CMD_TEMP),
    ]
    assert N.pids_orfaos({300}, procs) == []


def test_driver_sem_navegador_pendurado_nao_e_morto():
    """Um `chromedriver.exe` que alguém subiu à mão como servidor pode ter o pai
    (o terminal) encerrado sem ser lixo. Sem sessão de automação pendurada,
    ficamos de fora."""
    procs = [_proc(500, 4242, "chromedriver.exe")]
    assert N.pids_orfaos({1}, procs) == []


def test_navegador_pessoal_do_usuario_nao_conta_como_sessao():
    """Chrome pessoal (perfil do usuário, sem marca de automação) pendurado num
    driver órfão não faz dele um alvo."""
    procs = [
        _proc(500, 4242, "chromedriver.exe"),
        _proc(501, 500, "chrome.exe", '"c:\\program files\\...\\chrome.exe" --profile-directory="Profile 1"'),
    ]
    assert N.pids_orfaos({1}, procs) == []


def test_pid_registrado_por_este_processo_e_protegido():
    procs = [
        _proc(500, 4242, "chromedriver.exe"),
        _proc(501, 500, "chrome.exe", CMD_TEMP),
    ]
    assert N.pids_orfaos({1}, procs, protegidos={500}) == []


# ---------------------------------------------------------------------
# Perfis temporários (scoped_dir*) que ninguém usa
# ---------------------------------------------------------------------
def _perfil(raiz, nome: str, idade_s: float = N._IDADE_PERFIL_S + 60) -> str:
    d = raiz / nome
    d.mkdir()
    (d / "Default").mkdir()
    quando = time.time() - idade_s
    os.utime(d, (quando, quando))
    return str(d)


def test_perfil_velho_e_sem_dono_e_lixo(tmp_path):
    alvo = _perfil(tmp_path, "scoped_dir9_1")
    assert N.perfis_temp_orfaos([], str(tmp_path)) == [alvo]


def test_perfil_citado_por_navegador_vivo_e_preservado(tmp_path):
    """O caso perigoso: apagar por dentro o perfil de um navegador VIVO
    corromperia a sessão dele (mesma lição do rmtree das pastas _MEI)."""
    _perfil(tmp_path, "scoped_dir9_1")
    procs = [_proc(1, 2, "chrome.exe", "chrome.exe --user-data-dir=x\\scoped_dir9_1")]
    assert N.perfis_temp_orfaos(procs, str(tmp_path)) == []


def test_perfil_recem_criado_e_preservado(tmp_path):
    """Janela entre 'pasta criada' e 'navegador já com a flag na linha de
    comando': a idade cobre o que a citação ainda não cobre."""
    _perfil(tmp_path, "scoped_dir9_1", idade_s=5)
    assert N.perfis_temp_orfaos([], str(tmp_path)) == []


def test_perfil_do_dono_que_acabamos_de_matar_e_lixo(tmp_path):
    """A listagem foi tirada ANTES do kill: sem `ignorar_pids`, o perfil do
    órfão que acabamos de matar ficaria mais um dia no disco."""
    alvo = _perfil(tmp_path, "scoped_dir9_1")
    procs = [_proc(11, 500, "chrome.exe", "chrome.exe --user-data-dir=x\\scoped_dir9_1")]
    assert N.perfis_temp_orfaos(procs, str(tmp_path)) == []
    assert N.perfis_temp_orfaos(procs, str(tmp_path), ignorar_pids={500}) == [alvo]


def test_ignora_o_que_nao_e_perfil_de_navegador(tmp_path):
    _perfil(tmp_path, "_MEI12345")
    _perfil(tmp_path, "autojur_envio_abc")
    (tmp_path / "scoped_dir_arquivo.txt").write_text("x", encoding="utf-8")
    assert N.perfis_temp_orfaos([], str(tmp_path)) == []


def test_raiz_inacessivel_nao_levanta(tmp_path):
    assert N.perfis_temp_orfaos([], str(tmp_path / "nao_existe")) == []


# ---------------------------------------------------------------------
# Varredura completa
# ---------------------------------------------------------------------
def test_varredura_sem_listagem_nao_faz_nada(monkeypatch, tmp_path):
    monkeypatch.setattr(N, "listar_processos", lambda: None)
    alvo = _perfil(tmp_path, "scoped_dir9_1")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    assert N.varrer_orfaos() == (0, 0)
    assert os.path.isdir(alvo), "sem listagem confiável, nada pode ser apagado"


def test_varredura_mata_orfao_e_apaga_o_perfil_dele(monkeypatch, tmp_path):
    alvo = _perfil(tmp_path, "scoped_dir500_1")
    procs = [
        _proc(500, 4242, "chromedriver.exe"),
        _proc(501, 500, "chrome.exe", "chrome.exe --user-data-dir=x\\scoped_dir500_1"),
    ]
    monkeypatch.setattr(N, "listar_processos", lambda: ({1}, procs))
    monkeypatch.setattr(N, "matar_arvore", lambda pid: True)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))

    assert N.varrer_orfaos() == (1, 1)
    assert not os.path.isdir(alvo)


# ---------------------------------------------------------------------
# Fechamento do app (organizador/gui.py)
# ---------------------------------------------------------------------
# A classe App não é instanciável na suíte (precisaria de display), então os
# métodos são chamados desligados da classe, com um `self` de mentira. O que se
# testa aqui é ORDEM e ESPERA — exatamente o que estava errado.
class _AppFalso:
    _ao_fechar = App._ao_fechar
    _fechar_em_thread = staticmethod(App._fechar_em_thread)
    _fechar_autojur_ocioso = App._fechar_autojur_ocioso
    _marcar_uso_autojur = App._marcar_uso_autojur


class _ScraperFalso:
    def __init__(self, eventos: list, demora: float = 0.0):
        self._eventos = eventos
        self._demora = demora
        self.fechado = False

    def fechar(self):
        time.sleep(self._demora)
        self.fechado = True
        self._eventos.append("fechou")


def _app_fechando(eventos: list, monkeypatch, scraper=None):
    app = _AppFalso()
    app._monitor_worker = None
    app._monitor_cancel = threading.Event()
    app._worker = None
    app._cancel_event = threading.Event()
    app._autojur_scraper = scraper
    app._debug_scraper = None
    app.withdraw = lambda: eventos.append("withdraw")
    app.update_idletasks = lambda: None
    app.destroy = lambda: eventos.append("destroy")
    monkeypatch.setattr(
        "organizador.gui.navegadores.matar_registrados",
        lambda: (eventos.append("matar"), 0)[1],
    )
    return app


def test_fechamento_espera_o_quit_terminar(monkeypatch):
    """A regressão que importa: antes o `destroy()` vinha ANTES do `quit()`, e o
    navegador ficava órfão."""
    eventos: list[str] = []
    scraper = _ScraperFalso(eventos, demora=0.3)
    app = _app_fechando(eventos, monkeypatch, scraper)

    t0 = time.monotonic()
    app._ao_fechar()
    decorrido = time.monotonic() - t0

    assert scraper.fechado
    assert eventos == ["withdraw", "fechou", "matar", "destroy"]
    assert decorrido >= 0.3, "não esperou o quit()"


def test_fechamento_nao_fica_preso_num_quit_travado(monkeypatch):
    """O prazo é teto, não promessa: um `quit()` travado não pode prender o app.
    Passado o prazo, o taskkill resolve — e ele vem ANTES do destroy()."""
    monkeypatch.setattr("organizador.gui._PRAZO_QUIT_S", 0.2)
    eventos: list[str] = []
    scraper = _ScraperFalso(eventos, demora=5.0)
    app = _app_fechando(eventos, monkeypatch, scraper)

    t0 = time.monotonic()
    app._ao_fechar()
    decorrido = time.monotonic() - t0

    assert decorrido < 2.0, "esperou além do prazo"
    assert eventos[-2:] == ["matar", "destroy"]


def test_fechamento_cancela_os_workers_e_mata_o_que_sobrou(monkeypatch):
    """Sem scraper retido a matança ainda tem de acontecer: o navegador da busca
    ou da passada de monitoramento está preso numa thread worker, fora do
    alcance deste método — o registro é quem o alcança."""
    eventos: list[str] = []
    app = _app_fechando(eventos, monkeypatch, None)
    vivo = threading.Event()
    parar = threading.Event()

    def trabalhar():
        vivo.set()
        parar.wait(5)

    app._worker = threading.Thread(target=trabalhar, daemon=True)
    app._monitor_worker = app._worker
    app._worker.start()
    vivo.wait(2)
    try:
        app._ao_fechar()
    finally:
        parar.set()

    assert app._cancel_event.is_set() and app._monitor_cancel.is_set()
    assert eventos == ["withdraw", "matar", "destroy"]


# ---------------------------------------------------------------------
# Sessão oculta do Autojur parada
# ---------------------------------------------------------------------
class _FilaFalsa:
    def __init__(self):
        self.itens: list[str] = []

    def put(self, msg):
        self.itens.append(msg)


def _app_autojur(scraper, headless, ocioso_ha: float, worker=None):
    app = _AppFalso()
    app._autojur_scraper = scraper
    app._autojur_scraper_headless = headless
    app._autojur_worker = None
    app._autojur_tarefa_worker = worker
    app._autojur_prelogin_worker = None
    app._envio_autojur_worker = None
    app._autojur_login_lock = threading.Lock()
    app._autojur_ocioso_s = 300.0
    app._autojur_ultimo_uso = time.monotonic() - ocioso_ha
    app._pesquisa_log_queue = _FilaFalsa()
    return app


def _esperar_fechar(scraper, prazo: float = 2.0) -> bool:
    fim = time.monotonic() + prazo
    while time.monotonic() < fim:
        if scraper.fechado:
            return True
        time.sleep(0.01)
    return False


def test_sessao_oculta_parada_e_encerrada():
    scraper = _ScraperFalso([])
    app = _app_autojur(scraper, headless=True, ocioso_ha=400)

    app._fechar_autojur_ocioso()

    assert _esperar_fechar(scraper), "navegador oculto ficou aberto"
    assert app._autojur_scraper is None
    assert app._autojur_scraper_headless is None
    assert "sem uso" in "".join(app._pesquisa_log_queue.itens)


def test_sessao_visivel_nunca_e_encerrada_por_ociosidade():
    """Ela está na TELA do usuário — é o modo de conferência do envio sem
    salvar. Fechá-la por baixo dele apagaria a tarefa em revisão."""
    scraper = _ScraperFalso([])
    app = _app_autojur(scraper, headless=False, ocioso_ha=99999)

    app._fechar_autojur_ocioso()

    assert not scraper.fechado
    assert app._autojur_scraper is scraper


def test_sessao_em_uso_recente_nao_e_encerrada():
    scraper = _ScraperFalso([])
    app = _app_autojur(scraper, headless=True, ocioso_ha=10)

    app._fechar_autojur_ocioso()

    assert not scraper.fechado
    assert app._pesquisa_log_queue.itens == []


def test_worker_vivo_zera_o_relogio():
    """O envio pode passar minutos numa tela sem tocar no scraper; a sessão está
    em uso e o relógio recomeça."""
    scraper = _ScraperFalso([])
    parar = threading.Event()
    w = threading.Thread(target=lambda: parar.wait(5), daemon=True)
    w.start()
    app = _app_autojur(scraper, headless=True, ocioso_ha=400, worker=w)
    try:
        app._fechar_autojur_ocioso()
    finally:
        parar.set()

    assert not scraper.fechado
    assert (time.monotonic() - app._autojur_ultimo_uso) < 1.0


def test_login_em_curso_adia_o_fechamento():
    """`_garantir_autojur_logado` roda no worker com o lock tomado: fechar agora
    seria fechar um navegador que está sendo criado."""
    scraper = _ScraperFalso([])
    app = _app_autojur(scraper, headless=True, ocioso_ha=400)
    app._autojur_login_lock.acquire()
    try:
        app._fechar_autojur_ocioso()
    finally:
        app._autojur_login_lock.release()

    assert not scraper.fechado
    assert app._autojur_scraper is scraper


def test_sem_sessao_nao_faz_nada():
    app = _app_autojur(None, headless=True, ocioso_ha=99999)
    app._fechar_autojur_ocioso()
    assert app._pesquisa_log_queue.itens == []
