# -*- coding: utf-8 -*-
"""Auto-atualização embutida no Peticoes.exe (organizador/atualizacao.py).

Antes disto existia um Atualizador.exe separado, que matava o Peticoes.exe com
TerminateProcess para trocar o .exe. Agora o próprio app se substitui: baixa a
nova versão, renomeia A SI MESMO para .old (confirmado ao vivo que o Windows
permite isso com o processo rodando) e move o download para o nome original.

Cobre a lógica inteira do módulo: comparação de versão, log, troca do arquivo e
a camada de rede (checagem da release e download), esta com um dublê no lugar do
``_opener`` — nada aqui toca a internet. Só a janela Tk fica de fora, como
gui.py. Ficar sem os testes de rede escondia o cenário mais caro de todos: um
download truncado virando o Peticoes.exe do usuário.
"""

from __future__ import annotations

import sys

import pytest

from organizador import atualizacao


@pytest.fixture(autouse=True)
def _isolar(monkeypatch, tmp_path):
    monkeypatch.setenv("ORGANIZADOR_APP_DATA", str(tmp_path))
    monkeypatch.setattr(atualizacao, "APP_DATA_DIR", tmp_path)
    yield


def _log(tmp_path) -> str:
    p = tmp_path / "updater.log"
    return p.read_text(encoding="utf-8") if p.exists() else ""


# ---------------------------------------------------------------------
# Comparação de versões (o que decide se a janela aparece)
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "instalada, publicada, oferece",
    [
        ("1.9.0", "v1.9.3", True),    # desatualizada -> aparece
        ("1.9.3", "v1.9.3", False),   # em dia -> silencioso, correto
        ("1.9.4", "v1.9.3", False),   # à frente (build local) -> não regride
        ("1.8.4", "v1.10.0", True),   # 10 > 9: comparação numérica, não textual
        ("1.10.0", "v1.9.3", False),
    ],
)
def test_decisao_de_oferecer_update(instalada, publicada, oferece):
    assert (
        atualizacao.parse_version(publicada) > atualizacao.parse_version(instalada)
    ) is oferece


def test_parse_version_invalido_forca_oferta():
    """(0,) perde de qualquer versão real, então um valor ilegível nunca deixa a
    máquina presa numa versão antiga."""
    assert atualizacao.parse_version("") == (0,)
    assert atualizacao.parse_version(None) == (0,)
    assert atualizacao.parse_version("v1.9.3") > atualizacao.parse_version("qualquer coisa")


# ---------------------------------------------------------------------
# Caminho do próprio exe: fonte única da versão instalada (sem version.txt)
# ---------------------------------------------------------------------
def test_exe_atual_none_fora_do_build(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert atualizacao._exe_atual() is None


def test_exe_atual_e_o_proprio_executavel_no_build(monkeypatch, tmp_path):
    exe = tmp_path / "Peticoes.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe), raising=False)
    assert atualizacao._exe_atual() == exe.resolve()


# ---------------------------------------------------------------------
# limpar_sobras: restos de uma atualização anterior interrompida
# ---------------------------------------------------------------------
def test_limpar_sobras_remove_old_e_download(monkeypatch, tmp_path):
    exe = tmp_path / "Peticoes.exe"
    exe.write_bytes(b"atual")
    (tmp_path / "Peticoes.exe.old").write_bytes(b"velho")
    (tmp_path / "Peticoes.exe.download").write_bytes(b"parcial")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe), raising=False)

    atualizacao.limpar_sobras()

    assert exe.exists()
    assert not (tmp_path / "Peticoes.exe.old").exists()
    assert not (tmp_path / "Peticoes.exe.download").exists()


def test_limpar_sobras_no_op_fora_do_build(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    atualizacao.limpar_sobras()  # não pode levantar


# ---------------------------------------------------------------------
# substituir_a_si_mesmo: nunca escreve por cima do exe em execução, só
# renomeia — é essa permissão do Windows que dispensa um segundo processo.
# ---------------------------------------------------------------------
def test_substituir_troca_e_preserva_conteudo_novo(tmp_path):
    alvo = tmp_path / "Peticoes.exe"
    alvo.write_bytes(b"versao velha")
    novo = tmp_path / "Peticoes.exe.download"
    novo.write_bytes(b"versao nova")

    atualizacao.substituir_a_si_mesmo(novo, alvo)

    assert alvo.read_bytes() == b"versao nova"
    assert not novo.exists()
    assert not (tmp_path / "Peticoes.exe.old").exists()  # limpo no final


def test_substituir_propaga_erro_de_arquivo_travado(monkeypatch, tmp_path):
    """Se o rename falhar (ex.: outra instância do app com o mesmo exe aberto),
    quem chama precisa de uma mensagem para o usuário, não um traceback cru."""
    alvo = tmp_path / "Peticoes.exe"
    alvo.write_bytes(b"versao velha")
    novo = tmp_path / "Peticoes.exe.download"
    novo.write_bytes(b"versao nova")

    def _rename_falha(*_a, **_k):
        raise OSError("simulado: arquivo em uso")

    monkeypatch.setattr(atualizacao.os, "rename", _rename_falha)

    with pytest.raises(atualizacao.AppRunningError):
        atualizacao.substituir_a_si_mesmo(novo, alvo)
    assert alvo.read_bytes() == b"versao velha"  # nada foi perdido na falha


# ---------------------------------------------------------------------
# Log — é o que torna o silêncio do modo --auto diagnosticável
# ---------------------------------------------------------------------
def test_registra_no_app_data(tmp_path):
    atualizacao.registrar("mensagem de teste")
    conteudo = _log(tmp_path)
    assert "mensagem de teste" in conteudo
    assert conteudo.count("\n") == 1
    assert conteudo[:2].isdigit()  # traz data/hora


def test_registra_varias_linhas_em_ordem(tmp_path):
    atualizacao.registrar("primeira")
    atualizacao.registrar("segunda")
    linhas = _log(tmp_path).strip().splitlines()
    assert len(linhas) == 2
    assert "primeira" in linhas[0] and "segunda" in linhas[1]


def test_log_nao_cresce_sem_limite(tmp_path):
    """Roda em toda abertura do app; não pode virar um arquivo gigante."""
    for i in range(320):
        atualizacao.registrar(f"linha {i}")
    linhas = _log(tmp_path).strip().splitlines()
    assert len(linhas) <= 200
    assert "linha 319" in linhas[-1], "manteve as antigas em vez das recentes"


def test_registrar_nunca_levanta(tmp_path):
    """Log é acessório: falha de disco não pode derrubar a checagem."""
    alvo = tmp_path / "updater.log"
    alvo.mkdir(parents=True)  # diretório onde deveria haver arquivo
    atualizacao.registrar("nao deve levantar")


def test_caminho_do_log_segue_o_app_data(tmp_path):
    assert atualizacao._caminho_log() == tmp_path / "updater.log"


def test_log_fica_junto_do_resto_do_estado_do_app(monkeypatch):
    """Mesma pasta de ui_state.json/perf.log — é onde alguém vai procurar."""
    monkeypatch.delenv("ORGANIZADOR_APP_DATA", raising=False)
    import importlib

    import organizador.config as C

    importlib.reload(C)
    monkeypatch.setattr(atualizacao, "APP_DATA_DIR", C.APP_DATA_DIR)
    assert atualizacao._caminho_log().parent == C.APP_DATA_DIR


# ---------------------------------------------------------------------
# Rede: checagem da release e download do asset.
#
# É a camada que decide se uma máquina consegue se atualizar e, pior, se um
# download truncado pode virar o Peticoes.exe da pessoa. Nenhuma dela toca a
# rede aqui: o _opener do módulo é substituído por um dublê.
# ---------------------------------------------------------------------
import io
import json as _json
import threading
import urllib.error


class _Resposta(io.BytesIO):
    """Mínimo que urlopen devolve: context manager + .headers."""

    def __init__(self, dados: bytes, headers: dict | None = None):
        super().__init__(dados)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.close()
        return False


class _OpenerFalso:
    def __init__(self, resultado):
        self._resultado = resultado
        self.requisicoes: list = []

    def open(self, req, timeout=None):
        self.requisicoes.append(req)
        if isinstance(self._resultado, Exception):
            raise self._resultado
        return self._resultado


_URL_ASSET = "https://api.github.com/repos/x/y/releases/assets/9"


def _release(assets) -> bytes:
    return _json.dumps({"tag_name": "v2.0.0", "assets": assets}).encode()


def _http_error(codigo: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("u", codigo, "erro", {}, None)


def _usar_opener(monkeypatch, resultado) -> _OpenerFalso:
    falso = _OpenerFalso(resultado)
    monkeypatch.setattr(atualizacao, "_opener", falso)
    return falso


def test_fetch_devolve_tag_url_e_tamanho(monkeypatch):
    _usar_opener(monkeypatch, _Resposta(_release(
        [{"name": "outro.zip", "url": "u1", "size": 1},
         {"name": "Peticoes.exe", "url": "https://api/asset/9", "size": 4242}]
    )))
    assert atualizacao.fetch_latest_release() == {
        "tag": "v2.0.0", "asset_url": "https://api/asset/9", "size": 4242,
    }


def test_fetch_release_sem_o_exe_avisa_quem_publicou(monkeypatch):
    """Release publicada sem o asset: a máquina não pode ficar tentando à toa."""
    _usar_opener(monkeypatch, _Resposta(_release([{"name": "leiame.txt", "url": "u"}])))
    with pytest.raises(atualizacao.AssetMissingError):
        atualizacao.fetch_latest_release()


@pytest.mark.parametrize(
    "codigo, esperado",
    [
        (401, atualizacao.TokenError),      # token expirado -> avisar o responsável
        (403, atualizacao.TokenError),
        (404, atualizacao.NoReleaseError),  # nada publicado ainda
        (500, atualizacao.NetworkError),
    ],
)
def test_erro_http_vira_mensagem_certa(monkeypatch, codigo, esperado):
    _usar_opener(monkeypatch, _http_error(codigo))
    with pytest.raises(esperado):
        atualizacao.fetch_latest_release()


def test_sem_internet_nao_vira_traceback(monkeypatch):
    _usar_opener(monkeypatch, urllib.error.URLError("sem rede"))
    with pytest.raises(atualizacao.NetworkError):
        atualizacao.fetch_latest_release()


def test_request_leva_token_e_versao_da_api():
    req = atualizacao._request("https://api/x", "application/json")
    assert req.get_header("Authorization") == f"Bearer {atualizacao.GITHUB_TOKEN}"
    assert req.get_header("X-github-api-version") == "2022-11-28"


def test_redirect_para_outro_host_derruba_o_authorization():
    """O GitHub redireciona o asset para uma URL ASSINADA em outro host, que
    rejeita quem manda Authorization junto — sem isto o download não baixa."""
    manipulador = atualizacao._AuthStrippingRedirect()
    req = atualizacao._request("https://api.github.com/asset/1", "application/octet-stream")
    novo = manipulador.redirect_request(
        req, None, 302, "Found", {}, "https://objects.githubusercontent.com/x?sig=1",
    )
    assert novo.get_header("Authorization") is None


def test_redirect_no_mesmo_host_mantem_o_authorization():
    manipulador = atualizacao._AuthStrippingRedirect()
    req = atualizacao._request("https://api.github.com/a", "application/octet-stream")
    novo = manipulador.redirect_request(
        req, None, 302, "Found", {}, "https://api.github.com/b",
    )
    assert novo.get_header("Authorization") is not None


def test_download_grava_o_arquivo_e_reporta_progresso(monkeypatch, tmp_path):
    conteudo = b"x" * (3 * atualizacao.CHUNK)
    _usar_opener(monkeypatch, _Resposta(conteudo, {"Content-Length": str(len(conteudo))}))
    destino = tmp_path / "Peticoes.exe.download"
    progresso: list = []

    atualizacao.download_asset(
        _URL_ASSET, destino, len(conteudo), lambda d, t: progresso.append((d, t)),
        threading.Event(),
    )

    assert destino.read_bytes() == conteudo
    assert progresso[-1] == (len(conteudo), len(conteudo))


def test_progresso_aparece_antes_do_primeiro_mib(monkeypatch, tmp_path):
    """A janela só mostra os MB quando `progress_cb` roda. Com chunk de 1 MiB, um
    download lento ficava em "Baixando atualização..." sem número e com a barra em
    zero — foi exatamente o que uma VM apresentou como "infinito"."""
    conteudo = b"z" * (300 * 1024)  # 300 KiB: menos de 1 MiB
    _usar_opener(monkeypatch, _Resposta(conteudo, {"Content-Length": str(len(conteudo))}))
    progresso: list = []

    atualizacao.download_asset(
        _URL_ASSET, tmp_path / "Peticoes.exe.download", len(conteudo),
        lambda d, t: progresso.append(d), threading.Event(),
    )

    assert len(progresso) >= 4, f"feedback grosso demais: {progresso}"
    assert progresso[0] <= 64 * 1024, "o primeiro aviso tem de vir logo"


def test_download_truncado_nao_vira_o_exe_do_usuario(monkeypatch, tmp_path):
    """O pior desfecho possível: instalar meio executável. O tamanho declarado
    pela API é conferido, e o arquivo parcial é apagado."""
    _usar_opener(monkeypatch, _Resposta(b"meio arquivo"))
    destino = tmp_path / "Peticoes.exe.download"

    with pytest.raises(atualizacao.DownloadCorruptError):
        atualizacao.download_asset(_URL_ASSET, destino, 999999, lambda *_: None, threading.Event())
    assert not destino.exists()


def test_download_cancelado_nao_deixa_sobra(monkeypatch, tmp_path):
    _usar_opener(monkeypatch, _Resposta(b"y" * (2 * atualizacao.CHUNK)))
    destino = tmp_path / "Peticoes.exe.download"
    cancelar = threading.Event()
    cancelar.set()

    with pytest.raises(atualizacao.CancelledError):
        atualizacao.download_asset(_URL_ASSET, destino, 0, lambda *_: None, cancelar)
    assert not destino.exists()


def test_queda_no_meio_do_download_apaga_o_parcial(monkeypatch, tmp_path):
    _usar_opener(monkeypatch, urllib.error.URLError("caiu"))
    destino = tmp_path / "Peticoes.exe.download"
    with pytest.raises(atualizacao.NetworkError):
        atualizacao.download_asset(_URL_ASSET, destino, 10, lambda *_: None, threading.Event())
    assert not destino.exists()


def test_erro_http_no_download_tambem_apaga_o_parcial(monkeypatch, tmp_path):
    _usar_opener(monkeypatch, _http_error(403))
    destino = tmp_path / "Peticoes.exe.download"
    with pytest.raises(atualizacao.TokenError):
        atualizacao.download_asset(_URL_ASSET, destino, 10, lambda *_: None, threading.Event())
    assert not destino.exists()


def test_limpar_sobras_tolera_arquivo_travado(monkeypatch, tmp_path):
    """Roda em toda abertura: um .old ainda preso pelo processo anterior não
    pode impedir o app de abrir."""
    exe = tmp_path / "Peticoes.exe"
    exe.write_bytes(b"app")
    monkeypatch.setattr(atualizacao, "_exe_atual", lambda: exe)

    def _unlink_falha(*_a, **_k):
        raise OSError("simulado: em uso")

    monkeypatch.setattr(atualizacao.Path, "unlink", _unlink_falha)
    atualizacao.limpar_sobras()  # não levanta


def test_tamanho_em_mb_no_formato_brasileiro():
    assert atualizacao._fmt_mb(55 * 1024 * 1024) == "55,0"


# ---------------------------------------------------------------------
# Diagnóstico: a checagem que não deixa rastro
#
# Uma máquina apresentou, tentativa após tentativa, SÓ a linha "iniciado" no
# updater.log — nos dois modos, e sem nada na tela. Três causas produziam
# exatamente esse mesmo silêncio (janela estourando no construtor, thread
# pendurada na rede, callback Tk morrendo), e nenhuma delas era distinguível à
# distância. Os testes abaixo travam as linhas de log que separam as três.
# ---------------------------------------------------------------------
class _JanelaFalsa:
    """Dublê do Toplevel: só o que `_checar`/`_watchdog`/`_vigiar_download`
    tocam, sem Tk. `after` guarda o que foi reagendado em vez de agendar."""

    def __init__(self, *, respondeu: bool = False, existe: bool = True) -> None:
        self._respondeu = respondeu
        self._existe = existe
        self.posts: list = []
        self.erros: list = []
        self.agendados: list = []
        self._baixando = False
        self._baixados = 0
        self._ultimo_avanco = 0.0
        self._avisou_parado = False
        self.release: dict = {"size": 55_000_000}

    def _post(self, fn) -> None:
        self.posts.append(fn)

    def winfo_exists(self) -> bool:
        return self._existe

    def _mostrar_erro(self, erro) -> None:
        self.erros.append(erro)

    def after(self, _ms, fn) -> None:
        self.agendados.append(fn)

    def _vigiar_download(self) -> None:
        atualizacao._JanelaAtualizacao._vigiar_download(self)


def _parado_ha(segundos: float) -> float:
    return atualizacao.time.monotonic() - segundos


def test_falha_ao_abrir_a_janela_para_de_ser_invisivel(monkeypatch, tmp_path):
    """O construtor estourando era engolido nas duas vias (suppress em auto,
    stderr inexistente do exe --windowed em manual): o log parava no "iniciado"."""
    def _explode(*_a, **_k):
        raise RuntimeError("simulado: Toplevel não subiu")

    monkeypatch.setattr(atualizacao, "_JanelaAtualizacao", _explode)

    with pytest.raises(RuntimeError):
        atualizacao.verificar(None, auto=True)

    log = _log(tmp_path)
    assert "ERRO ao abrir a janela de atualização" in log
    assert "simulado: Toplevel não subiu" in log
    assert "Traceback" in log  # o traceback inteiro, não só a mensagem


def test_marco_no_log_antes_da_rede(monkeypatch, tmp_path):
    """Sem esta linha, "a thread nem nasceu" e "a chamada de rede pendurou" são
    o mesmo log — e pedem correções diferentes."""
    def _falha():
        raise atualizacao.NetworkError("sem rede")

    monkeypatch.setattr(atualizacao, "fetch_latest_release", _falha)
    janela = _JanelaFalsa()

    atualizacao._JanelaAtualizacao._checar(janela)

    linhas = _log(tmp_path).splitlines()
    assert any("consultando api.github.com" in ln for ln in linhas)
    assert janela.posts, "o erro tem de continuar chegando à janela"


def test_watchdog_desiste_da_checagem_pendurada():
    """TIMEOUT cobre o socket, NÃO o getaddrinfo: com DNS em blackhole a thread
    não volta nunca e, em auto (janela withdraw()), nada aparece em lugar nenhum."""
    janela = _JanelaFalsa()

    atualizacao._JanelaAtualizacao._watchdog(janela)

    assert len(janela.erros) == 1
    erro = janela.erros[0]
    assert isinstance(erro, atualizacao.NetworkError)
    assert str(atualizacao.WATCHDOG) in str(erro)


def test_watchdog_nao_atropela_checagem_que_respondeu():
    """Ele dispara por tempo, não por resultado: sem esta guarda ele sobrescreveria
    a resposta boa (ou o erro real) que chegou dentro do prazo."""
    janela = _JanelaFalsa(respondeu=True)

    atualizacao._JanelaAtualizacao._watchdog(janela)

    assert janela.erros == []


def test_download_parado_aparece_no_log(tmp_path):
    """Barra travada em 0 MB era indistinguível de app morto: nenhuma linha saía.
    O vigia denuncia sem cancelar — 53 MB numa rede ruim levam minutos e um
    cancelamento por tempo total mataria download que ia terminar."""
    janela = _JanelaFalsa()
    janela._baixando = True
    janela._baixados = 3 * 1024 * 1024
    janela._ultimo_avanco = _parado_ha(atualizacao.PARADO + 5)

    janela._vigiar_download()

    log = _log(tmp_path)
    assert "download PARADO" in log
    assert "3145728 de 55000000 bytes" in log
    assert janela.agendados, "o vigia tem de continuar armado"


def test_vigia_do_download_cala_enquanto_avanca(tmp_path):
    janela = _JanelaFalsa()
    janela._baixando = True
    janela._baixados = 10 * 1024 * 1024
    janela._ultimo_avanco = atualizacao.time.monotonic()  # avançou agora

    janela._vigiar_download()

    assert "download PARADO" not in _log(tmp_path)
    assert janela.agendados, "segue vigiando enquanto o download anda"


def test_vigia_avisa_uma_vez_so(tmp_path):
    """Ele se re-arma a cada PARADO segundos; sem a trava, um download morto
    encheria o log e empurraria as linhas úteis para fora das 200 mantidas."""
    janela = _JanelaFalsa()
    janela._baixando = True
    janela._baixados = 0
    janela._ultimo_avanco = _parado_ha(atualizacao.PARADO + 5)

    janela._vigiar_download()
    janela._vigiar_download()

    assert _log(tmp_path).count("download PARADO") == 1


def test_vigia_para_quando_o_download_termina(tmp_path):
    """`_instalar`/`_cancelado`/`_mostrar_erro` baixam a flag; o `after` já
    agendado ainda dispara depois disso."""
    janela = _JanelaFalsa()
    janela._baixando = False
    janela._ultimo_avanco = _parado_ha(9999)

    janela._vigiar_download()

    assert "download PARADO" not in _log(tmp_path)
    assert janela.agendados == [], "não deve se re-armar depois de terminar"


def test_watchdog_nao_toca_em_janela_ja_destruida():
    """Em auto a janela se destrói sozinha ao terminar; o `after` agendado ainda
    dispara depois disso e mexer em widget morto levanta TclError."""
    janela = _JanelaFalsa(existe=False)

    atualizacao._JanelaAtualizacao._watchdog(janela)

    assert janela.erros == []
