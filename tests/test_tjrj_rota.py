# -*- coding: utf-8 -*-
"""Cobertura das DUAS rotas do RJ: consulta pública (legado) e eproc (08*).

A consulta pública do TJRJ só tem rota de detalhe para o sistema legado
(``tipoProcesso: 1``). Processos de numeração 08* são do PJe (13) ou do eproc
(16) e nela devolvem só um cabeçalho sem partes — daí a rota "TJRJ — eproc".

Nada aqui sobe navegador nem toca a rede: testa-se que as rotas estão ligadas de
ponta a ponta e que o desfecho "está em outro sistema" vira mensagem acionável.
"""

from __future__ import annotations

import json

import pytest

from organizador.config import (
    TJ_SISTEMAS,
    _ESTADO_TJ_UF,
    candidatos_tj_para_uf,
    rota_incompativel_com_numero,
    rotulos_tj_selecionaveis,
    sugerir_tj_para_uf,
    uf_efetiva_tj,
)
from organizador.integracoes.tjsp_scraper.scrapers.tjrj import TJRJScraper


def test_rota_eproc_do_tjrj_esta_configurada():
    site, estado, url, perfil = TJ_SISTEMAS["TJRJ — eproc"]
    assert site == "eproc"
    assert perfil is None
    assert url.startswith("https://eproc1g.tjrj.jus.br")
    assert _ESTADO_TJ_UF[estado] == ("TJRJ", "RJ")
    assert uf_efetiva_tj("TJRJ — eproc") == "RJ"
    assert "TJRJ — eproc" in rotulos_tj_selecionaveis()


def test_fallback_do_rj_tenta_a_consulta_publica_antes_das_rotas_com_login():
    """No FALLBACK automático a consulta pública vem primeiro: custa 1 requisição
    HTTP, enquanto eproc e PJe custam navegador + senha + TOTP. Errar barato
    primeiro é o que evita abrir o Chrome para um processo do sistema legado."""
    assert candidatos_tj_para_uf("RJ") == [
        "TJRJ — Consulta", "TJRJ — eproc", "TJRJ — PJe",
    ]


# Sequencial 0024491 = legado (`tipoProcesso: 1`, número antigo 2021.203.024535-2),
# validado ao vivo na consulta pública em 2026-08-26.
NUM_LEGADO_RJ = "0024491-10.2021.8.19.0203"
NUM_08_RJ = "0800123-45.2025.8.19.0001"
# `09*` também é PJe/eproc: quatro destes apareceram na varredura e derrubam a
# suposição de que só "08" sai do legado. Ver test_corte_do_sequencial_do_rj.
NUM_09_RJ = "0944609-35.2023.8.19.0001"


def test_sugestao_da_tela_no_rj_e_o_pje_quando_nao_ha_numero():
    """Pergunta diferente do fallback: aqui é o "trocar para X?" que a pessoa
    responde. Sem número legível para desempatar, dos três sistemas do RJ o PJe
    é o que mais aparece na carteira."""
    assert sugerir_tj_para_uf("RJ") == "TJRJ — PJe"


def test_sugestao_do_rj_segue_o_sequencial_do_numero():
    """O override "RJ → PJe" sozinho mandava TODO processo legado para uma rota
    que gasta navegador + senha + TOTP e volta "não encontrado". O sequencial do
    CNJ decide: `08*` é PJe/eproc, o resto é legado (só a consulta pública)."""
    assert sugerir_tj_para_uf("RJ", NUM_LEGADO_RJ) == "TJRJ — Consulta"
    assert sugerir_tj_para_uf("RJ", NUM_08_RJ) == "TJRJ — PJe"
    # Número de outra UF não muda a sugestão daquela UF (a regra é só do RJ).
    assert sugerir_tj_para_uf("SP", "4004459-04.2026.8.26.0248") == "TJSP — eproc"


def test_rota_incompativel_avisa_quando_a_uf_bate_mas_o_sistema_nao():
    """No RJ os três sistemas são todos "RJ", então o aviso por UF nunca dispara
    — era assim que um número do legado com "TJRJ — PJe" selecionado falhava
    calado. Só a linha legado × 08* é FATO; PJe × eproc dentro do 08* é palpite
    e não deve interromper ninguém."""
    assert rota_incompativel_com_numero("TJRJ — PJe", NUM_LEGADO_RJ) == "TJRJ — Consulta"
    assert rota_incompativel_com_numero("TJRJ — eproc", NUM_LEGADO_RJ) == "TJRJ — Consulta"
    assert rota_incompativel_com_numero("TJRJ — Consulta", NUM_08_RJ) == "TJRJ — PJe"
    # Rota plausível → nenhuma pergunta.
    assert rota_incompativel_com_numero("TJRJ — Consulta", NUM_LEGADO_RJ) is None
    assert rota_incompativel_com_numero("TJRJ — PJe", NUM_08_RJ) is None
    assert rota_incompativel_com_numero("TJRJ — eproc", NUM_08_RJ) is None
    # Fora do RJ e número ilegível: a regra não opina.
    assert rota_incompativel_com_numero("TJSP — eproc", NUM_LEGADO_RJ) is None
    assert rota_incompativel_com_numero("TJRJ — PJe", "sem numero") is None
    assert rota_incompativel_com_numero("rótulo inexistente", NUM_LEGADO_RJ) is None


def test_sugestao_dos_outros_estados_nao_muda_com_o_override_do_rj():
    """O override é por UF; MG segue preferindo eproc na sugestão mesmo com o
    fallback dele começando no PJe (ver tests/test_projudi_rota.py)."""
    assert TJ_SISTEMAS[sugerir_tj_para_uf("MG")][0] == "eproc"
    assert sugerir_tj_para_uf("SP") == "TJSP — eproc"
    assert sugerir_tj_para_uf("GO") == "TJGO — Projudi"


def test_override_de_sugestao_aponta_para_rota_existente():
    """Um rótulo com erro de digitação (ou apontando p/ rota removida) faria a
    sugestão sumir em silêncio — o `in candidatos` protege, e este teste garante
    que o override em uso está mesmo válido."""
    from organizador.config import _SUGESTAO_POR_UF

    for uf, rotulo in _SUGESTAO_POR_UF.items():
        assert rotulo in TJ_SISTEMAS, f"override de {uf} aponta p/ rótulo inexistente"
        assert rotulo in candidatos_tj_para_uf(uf)
        assert sugerir_tj_para_uf(uf) == rotulo


def test_eproc_do_rj_usa_os_campos_do_keycloak_e_nao_os_do_eproc_interno():
    """A tela de login do TJRJ tem #txtUsuario/#pwdSenha TAMBÉM — mas como
    <input type="hidden"> de um form auxiliar. Apontar para eles faria o
    `visibility_of_element_located` esperar 60 s por campo que nunca aparece."""
    from organizador.integracoes.tjsp_scraper.scrapers.eproc import _CONFIG_POR_ESTADO

    cfg = _CONFIG_POR_ESTADO["RIO DE JANEIRO"]
    assert cfg["login_user_id"] == "username"
    assert cfg["login_pass_id"] == "password"
    assert cfg["login_submit_id"] == "kc-login"
    assert cfg["totp_input_id"] == "otp"
    assert cfg["sigla"] == "RJ"
    assert cfg["tj_chave"] == "TJRJ - Eproc"
    assert cfg["totp_env"] == "TOTP_EPROC_TJRJ"


def test_fabrica_devolve_eproc_para_o_rj(monkeypatch):
    from organizador.integracoes.tjsp_scraper.scrapers import EprocScraper, criar_scraper

    monkeypatch.setattr(
        "organizador.integracoes.tjsp_scraper.scrapers.eproc.criar_chrome_driver",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        "organizador.integracoes.tjsp_scraper.scrapers.eproc.WebDriverWait",
        lambda *a, **k: None,
    )
    s = criar_scraper(site="eproc", estado="RIO DE JANEIRO",
                      url="https://eproc1g.tjrj.jus.br/eproc/", perfil=None)
    assert isinstance(s, EprocScraper)
    assert s._sigla == "RJ"


# ---------------------------------------------------------------------
# Corte por `tipoProcesso` na consulta pública
# ---------------------------------------------------------------------
class _Resposta:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


def _scraper_com_respostas(monkeypatch, resumo, detalhe=None):
    s = TJRJScraper("RIO DE JANEIRO")

    def falso_post(path, payload):
        if "por-numeracao-unica" in path:
            return _Resposta([resumo])
        return _Resposta(detalhe or {})

    monkeypatch.setattr(s, "_post", falso_post)
    return s


@pytest.mark.parametrize(
    "tipo, trecho",
    [
        (16, "TJRJ — eproc"),      # aponta a rota que resolve
        (13, "certificado digital"),  # PJe do TJRJ: sem rota automatizada
        (99, "tipoProcesso=99"),   # tipo novo/desconhecido não vira erro mudo
    ],
)
def test_consulta_publica_recusa_o_que_nao_e_legado_com_mensagem_util(
    monkeypatch, tipo, trecho
):
    """Antes, qualquer um destes caía no genérico "Scraper não retornou dados
    para esse número" — indistinguível de processo inexistente."""
    s = _scraper_com_respostas(
        monkeypatch,
        {"numProcesso": "08144328120228190206", "tipoProcesso": tipo},
    )
    with pytest.raises(RuntimeError) as exc:
        s.consultar_processo("0814432-81.2022.8.19.0206")
    assert trecho in str(exc.value)


def test_consulta_publica_segue_normal_no_legado(monkeypatch):
    s = _scraper_com_respostas(
        monkeypatch,
        {"numProcesso": "2018.208.003445-2", "tipoProcesso": 1},
        {"personagensProcesso": [{"nome": "FULANO"}]},
    )
    blob = json.loads(s.consultar_processo("0003454-14.2018.8.19.0208"))
    assert blob["resumo"]["numProcesso"] == "2018.208.003445-2"
    assert blob["detalhe"]["personagensProcesso"]


def test_tipo_ausente_nao_inventa_erro(monkeypatch):
    """Campo some numa versão da API → segue o fluxo antigo, não bloqueia."""
    s = _scraper_com_respostas(
        monkeypatch,
        {"numProcesso": "2018.208.003445-2"},
        {"personagensProcesso": [{"nome": "FULANO"}]},
    )
    assert s.consultar_processo("0003454-14.2018.8.19.0208")


# ---------------------------------------------------------------------
# Recusa de 2FA
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "frase",
    [
        "Código autenticador inválido.",   # TJRJ (visto ao vivo, v9.21.0-3.0.2)
        "Erro verificando chave",          # MG/SC
        "Erro na autenticação",
    ],
)
def test_recusa_de_2fa_e_reconhecida_e_nomeia_a_variavel_do_env(frase):
    """A recusa do TJRJ não casava nenhum marcador: o login esperava 25 s pelo
    Painel e morria em "não cheguei ao Painel" — que não distingue segredo TOTP
    errado de sessão perdida."""
    from organizador.integracoes.tjsp_scraper.scrapers.eproc import _RECUSAS_2FA

    pagina = f"<html><body><span>{frase}</span></body></html>".lower()
    recusa = next((m for m in _RECUSAS_2FA if m in pagina), None)
    assert recusa is not None, f"nenhum marcador casa {frase!r}"


# ---------------------------------------------------------------------
# PJe do TJRJ: ids com naming container do JSF
# ---------------------------------------------------------------------
def _fixture_pje_rj() -> str:
    import pathlib
    caminho = pathlib.Path(__file__).parent / "fixtures" / "pje_tjrj_detalhe.html"
    return caminho.read_text(encoding="utf-8")


def test_rota_pje_do_tjrj_esta_configurada():
    """O host do PJe-RJ não segue o padrão: `pje.tjrj.jus.br` nem resolve."""
    site, estado, url, _perfil = TJ_SISTEMAS["TJRJ — PJe"]
    assert site == "pje"
    assert url == "https://tjrj.pje.jus.br/1g/"
    assert _ESTADO_TJ_UF[estado] == ("TJRJ", "RJ")


def test_pje_do_rj_tem_credencial_propria_e_nao_cai_no_fallback_do_tjmg():
    """`_CRED_POR_ESTADO` sem entrada cai SILENCIOSAMENTE no TJMG — foi o que
    deixou o DF logando com a senha errada (ver comentário em pje.py)."""
    from organizador.integracoes.tjsp_scraper.scrapers.pje import _CRED_POR_ESTADO

    assert _CRED_POR_ESTADO.get("RIO DE JANEIRO") == "TJRJ - 1ª instância"
    for _rotulo, (site, estado, _url, _p) in TJ_SISTEMAS.items():
        if site == "pje":
            assert estado in _CRED_POR_ESTADO, f"rota PJe sem credencial: {estado}"


def test_partes_saem_mesmo_com_id_prefixado_pelo_jsf():
    """No TJRJ os divs vêm como `navbar:poloAtivo`/`navbar:poloPassivo`. Com o
    `find(id="poloPassivo")` exato o processo era gravado SEM NENHUMA PARTE,
    embora nome, CPF/CNPJ e papel estivessem no DOM."""
    from organizador.integracoes.tjsp_scraper.parser_pje import _formatar_detalhe_pje

    bloco = _formatar_detalhe_pje(_fixture_pje_rj())
    assert "EXEQUENTE: AGUA NOVA EMPREENDIMENTOS IMOBILIARIOS LTDA" in bloco
    assert "28.473.630/0002-83" in bloco
    assert "EXECUTADO: ANA BARBARA DOS SANTOS TEIXEIRA" in bloco
    assert "112.317.547-02" in bloco


def test_casa_id_jsf_nao_pega_sufixo_solto():
    """`endswith("poloPassivo")` casaria `outroPoloPassivo`; o prefixo do JSF
    termina sempre em ':'."""
    from organizador.integracoes.tjsp_scraper.parser_pje import _casa_id_jsf

    casa = _casa_id_jsf("poloPassivo")
    assert casa("poloPassivo")
    assert casa("navbar:poloPassivo")
    assert casa("form:navbar:poloPassivo")
    assert not casa("outroPoloPassivo")
    assert not casa("poloPassivoExtra")
    assert not casa("")
    assert not casa(None)


def test_processo_do_pje_rj_vira_processo_com_autor_e_reu():
    """Ponta a ponta do parser: o que o scraper devolve tem de virar partes com
    papel utilizável, senão o monitor marca `reu_faltando` e reprocessa sempre."""
    from organizador.integracoes.tjsp_scraper.parser_pje import (
        _formatar_detalhe_pje, extrair_processo_pje,
    )
    from organizador.organizacao import _autor, _dict_para_processo, _reus_joined

    # `extrair_processo_pje` recebe o BLOB que o scraper monta (timeline +
    # painel formatado), não o HTML cru — é o que `_consultar_e_extrair` passa.
    blob = _formatar_detalhe_pje(_fixture_pje_rj())
    proc = _dict_para_processo(
        extrair_processo_pje(blob, numero_cnj="0814432-81.2022.8.19.0206")
    )
    assert _autor(proc) == "AGUA NOVA EMPREENDIMENTOS IMOBILIARIOS LTDA"
    assert _reus_joined(proc) == "ANA BARBARA DOS SANTOS TEIXEIRA"


# ---------------------------------------------------------------------------
# Rede: o www3 do TJRJ recusa parte das conexões (medido em 2026-08-26: 7 de 20
# connects TCP nunca completam). Sem retry isso derrubava ~1/3 das buscas; e o
# `return None` de então virava "Scraper não retornou dados para esse número",
# mandando a pessoa conferir um número que estava certo.
# ---------------------------------------------------------------------------

class _SessaoFalsa:
    """Session de mentira: `falhas_ssl` SSLError, depois `falhas` timeouts de
    conexão, depois responde. A ordem imita a real: o certificado do www3 falha
    logo no 1º POST de cada sessão, e só então entram (ou não) os timeouts."""

    def __init__(self, falhas: int = 0, resposta=None, falhas_ssl: int = 0):
        self.falhas = falhas
        self.falhas_ssl = falhas_ssl
        self.resposta = resposta
        self.chamadas = 0

    def post(self, *_a, **_kw):
        import requests
        self.chamadas += 1
        if self.chamadas <= self.falhas_ssl:
            raise requests.exceptions.SSLError("cadeia de certificado de mentira")
        if self.chamadas <= self.falhas_ssl + self.falhas:
            raise requests.exceptions.ConnectTimeout("connect timeout de mentira")
        return self.resposta

    def close(self):
        pass


class _RespostaFalsa:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture()
def _sem_pausa(monkeypatch):
    from organizador.integracoes.tjsp_scraper.scrapers import tjrj as mod

    monkeypatch.setattr(mod, "_PAUSA_ENTRE_TENTATIVAS", 0)
    return mod


def test_post_repete_quando_a_conexao_nao_abre(_sem_pausa):
    sc = TJRJScraper("RIO DE JANEIRO")
    ok = _RespostaFalsa(200, [])
    sessao = _SessaoFalsa(falhas=_sem_pausa._TENTATIVAS - 1, resposta=ok)
    sc._sess = sessao

    assert sc._post("qualquer", {}) is ok
    assert sessao.chamadas == _sem_pausa._TENTATIVAS


def test_falha_de_rede_nao_vira_processo_nao_encontrado(_sem_pausa):
    """A distinção que faltava: rede fora do ar tem de gritar "rede", não somir
    num None que o scraper.py traduz como número inexistente."""
    sc = TJRJScraper("RIO DE JANEIRO")
    sessao = _SessaoFalsa(falhas=99)
    sc._sess = sessao

    with pytest.raises(RuntimeError) as exc:
        sc.consultar_processo(NUM_LEGADO_RJ)

    assert "REDE" in str(exc.value)
    assert sessao.chamadas == _sem_pausa._TENTATIVAS


def test_processo_inexistente_continua_devolvendo_none(_sem_pausa):
    """O outro lado da moeda: lista vazia é "não achei", e isso NÃO é erro."""
    sc = TJRJScraper("RIO DE JANEIRO")
    sc._sess = _SessaoFalsa(falhas=0, resposta=_RespostaFalsa(200, []))

    assert sc.consultar_processo(NUM_LEGADO_RJ) is None


@pytest.mark.parametrize("status", [403, 429, 500, 502, 503])
def test_recusa_do_servidor_e_tratada_como_rede(_sem_pausa, status):
    """403 apareceu ao vivo depois de uma rajada de consultas (WAF do TJRJ), e
    429 é o mesmo recado com outro número. Nenhum dos dois diz nada sobre o
    número do processo — mandar a pessoa conferi-lo seria pista falsa."""
    sc = TJRJScraper("RIO DE JANEIRO")
    sc._sess = _SessaoFalsa(resposta=_RespostaFalsa(status, None))

    with pytest.raises(RuntimeError) as exc:
        sc.consultar_processo(NUM_LEGADO_RJ)

    assert "REDE" in str(exc.value)
    assert str(status) in str(exc.value)


@pytest.mark.parametrize("status", [404, 412])
def test_http_sobre_o_numero_continua_sendo_nao_encontrado(_sem_pausa, status):
    """O outro lado: 404/412 falam do número, não do servidor — seguem no
    caminho silencioso de "não achei", que o fallback do monitor usa para pular
    para o próximo sistema sem alarde."""
    sc = TJRJScraper("RIO DE JANEIRO")
    sc._sess = _SessaoFalsa(resposta=_RespostaFalsa(status, None))

    assert sc.consultar_processo(NUM_LEGADO_RJ) is None


def test_degradacao_de_ssl_nao_consome_tentativa_de_rede(_sem_pausa):
    """A cadeia de certificado do www3 não valida nesta máquina: a 1ª requisição
    de toda sessão nova cai em SSLError e o scraper degrada para verify=False.
    Isso é troca de modo, não conexão perdida — se contasse como tentativa,
    sobrariam 2 de 3 justo no caso que acontece SEMPRE. E o POST refeito tem de
    voltar pelo laço: fora dele, um timeout logo após a degradação escapava sem
    repetir nenhuma vez (visto ao vivo antes desta correção)."""
    sc = TJRJScraper("RIO DE JANEIRO")
    ok = _RespostaFalsa(200, [])
    sessao = _SessaoFalsa(falhas=_sem_pausa._TENTATIVAS - 1, resposta=ok, falhas_ssl=1)
    sc._sess = sessao

    assert sc._post("qualquer", {}) is ok
    assert sc._verify is False, "a degradação para verify=False não aconteceu"
    # 1 SSL (de graça) + as _TENTATIVAS de rede.
    assert sessao.chamadas == _sem_pausa._TENTATIVAS + 1


def test_ssl_que_persiste_sem_verificacao_vira_erro_de_rede(_sem_pausa):
    """Se o SSL falha MESMO com verify=False, o certificado não é a causa — aí é
    falha real e tem de gastar tentativa, em vez de degradar em laço eterno."""
    sc = TJRJScraper("RIO DE JANEIRO")
    sessao = _SessaoFalsa(falhas_ssl=99)
    sc._sess = sessao

    with pytest.raises(RuntimeError) as exc:
        sc.consultar_processo(NUM_LEGADO_RJ)

    assert "REDE" in str(exc.value)
    # 1 SSL de graça (a degradação) + _TENTATIVAS cobradas.
    assert sessao.chamadas == _sem_pausa._TENTATIVAS + 1


def test_corte_do_sequencial_do_rj_e_medido_nao_e_o_prefixo_08():
    """Varredura ao vivo dos 389 processos do RJ da carteira (2026-08-26, campo
    `tipoProcesso` da consulta pública): legado de 0000000 a 0331403, PJe/eproc
    a partir de 0800054 — separação limpa, sem nada no vão. Testar "começa com
    08" deixaria de fora os quatro `09*` reais que a varredura achou."""
    from organizador.config import _numero_e_do_legado_rj

    # Extremos medidos do legado.
    assert _numero_e_do_legado_rj("0000001-00.2020.8.19.0001") is True
    assert _numero_e_do_legado_rj("0331403-00.2020.8.19.0001") is True
    # Extremo medido do outro lado, e os `09*` que o prefixo "08" perdia.
    assert _numero_e_do_legado_rj("0800054-00.2025.8.19.0001") is False
    assert _numero_e_do_legado_rj(NUM_09_RJ) is False
    assert sugerir_tj_para_uf("RJ", NUM_09_RJ) == "TJRJ — PJe"
    assert rota_incompativel_com_numero("TJRJ — Consulta", NUM_09_RJ) == "TJRJ — PJe"
    # Ilegível continua sem opinião.
    assert _numero_e_do_legado_rj("") is None
