# -*- coding: utf-8 -*-
"""A regra por NÚMERO, para as UFs onde escolher pela UF erra a maioria.

Os cortes daqui são MEDIDOS, não deduzidos do formato do número — a primeira
tentativa no RJ usou "começa com 08" e errava 4 processos `09*`. Cada valor
abaixo é o extremo que a varredura ao vivo encontrou, e é isso que estes testes
travam: se alguém mexer no corte, um extremo real muda de lado.

RJ  — 389 processos, 2026-08-26, campo `tipoProcesso` da consulta pública.
SP  — 159 de 1051, 2026-08-27, consulta pública do e-SAJ.
"""

from __future__ import annotations

import pytest

from organizador.config import (
    TJ_SISTEMAS,
    _metade_do_numero,
    rota_exclusiva_para_numero,
    rota_incompativel_com_numero,
    sugerir_tj_para_uf,
)

# Extremos medidos: o último de cada metade e o primeiro da seguinte.
RJ_LEGADO_TOPO = "0331403-00.2021.8.19.0001"
RJ_NOVO_BASE = "0800054-27.2025.8.19.0203"
SP_ESAJ_TOPO = "1194081-00.2024.8.26.0100"
SP_EPROC_BASE = "4000224-40.2026.8.26.0362"


@pytest.mark.parametrize("numero, uf, metade", [
    (RJ_LEGADO_TOPO, "RJ", 0), (RJ_NOVO_BASE, "RJ", 1),
    (SP_ESAJ_TOPO, "SP", 0), (SP_EPROC_BASE, "SP", 1),
])
def test_extremos_medidos_ficam_do_lado_certo(numero, uf, metade):
    assert _metade_do_numero(uf, numero) == metade


def test_uf_sem_corte_medido_nao_opina():
    """MG tem PJe e eproc, mas ninguém mediu o corte — melhor calar que chutar."""
    assert _metade_do_numero("MG", "5000123-00.2024.8.13.0024") is None
    assert _metade_do_numero("SP", "sem numero") is None


@pytest.mark.parametrize("numero, esperado", [
    ("1000034-65.2025.8.26.0362", "TJSP — e-SAJ"),   # confirmado ao vivo
    ("0000451-36.2024.8.26.0664", "TJSP — e-SAJ"),   # confirmado ao vivo
    ("4000224-40.2026.8.26.0362", "TJSP — eproc"),   # ausente do e-SAJ
    ("0024491-10.2021.8.19.0203", "TJRJ — Consulta"),
    ("0944609-35.2023.8.19.0001", "TJRJ — PJe"),
])
def test_sugestao_segue_o_numero(numero, esperado):
    uf = "SP" if ".8.26." in numero else "RJ"
    assert sugerir_tj_para_uf(uf, numero) == esperado


def test_sem_numero_a_sugestao_volta_a_ser_por_uf():
    """Nada a medir sem número: vale o palpite antigo de cada UF."""
    assert sugerir_tj_para_uf("SP") == "TJSP — eproc"
    assert sugerir_tj_para_uf("RJ") == "TJRJ — PJe"


@pytest.mark.parametrize("rota, numero, esperado", [
    # SP `4*` NÃO existe no e-SAJ (47 de 47 na amostra deram "não existem
    # informações") -> incompatibilidade de fato.
    ("TJSP — e-SAJ", "4000224-40.2026.8.26.0362", "TJSP — eproc"),
    # SP `1*` com eproc selecionado: NÃO é incompatível. Os sistemas de SP se
    # sobrepõem — 8 de 8 processos achados no eproc estavam também no e-SAJ.
    # Dizer "não fica nele" seria mentira; a sugestão prefere o e-SAJ, mas isso
    # é preferência, e preferência não interrompe ninguém.
    ("TJSP — eproc", "1000034-65.2025.8.26.0362", None),
    # Rota plausível: nenhuma pergunta.
    ("TJSP — eproc", "4000224-40.2026.8.26.0362", None),
    ("TJSP — e-SAJ", "1000034-65.2025.8.26.0362", None),
    # RJ: dentro da metade nova, PJe x eproc é empate — não interrompe.
    ("TJRJ — PJe", "0800123-45.2025.8.19.0001", None),
    ("TJRJ — eproc", "0800123-45.2025.8.19.0001", None),
])
def test_incompatibilidade_so_quando_e_fato(rota, numero, esperado):
    assert rota_incompativel_com_numero(rota, numero) == esperado


@pytest.mark.parametrize("numero, esperado", [
    ("0024491-10.2021.8.19.0203", "TJRJ — Consulta"),  # legado: só a pública
    ("4000224-40.2026.8.26.0362", "TJSP — eproc"),     # ausente do e-SAJ
    # Metade com DOIS sistemas: não há rota exclusiva, o fallback vale.
    ("0800123-45.2025.8.19.0001", None),   # RJ novo: PJe OU eproc
    ("1000034-65.2025.8.26.0362", None),   # SP `1*`: e-SAJ sempre, eproc às vezes
    # UF sem regra medida.
    ("5000123-00.2024.8.13.0024", None),
])
def test_rota_exclusiva_so_onde_a_metade_tem_um_sistema(numero, esperado):
    """`rota_exclusiva_para_numero` corta o fallback numa falha técnica. Errar
    aqui para MAIS custa um login com TOTP da conta compartilhada à toa; errar
    para MENOS só devolve um erro em vez de tentar de novo."""
    assert rota_exclusiva_para_numero(numero) == esperado


def test_rotulos_da_regra_existem_de_verdade():
    """Um rótulo com erro de digitação faria a regra sumir em silêncio."""
    from organizador.config import _ROTULO_DA_METADE, _SITES_DA_METADE

    for chave, rotulo in _ROTULO_DA_METADE.items():
        assert rotulo in TJ_SISTEMAS, f"{chave} aponta p/ rótulo inexistente"
        assert TJ_SISTEMAS[rotulo][0] in _SITES_DA_METADE[chave], (
            f"{chave}: o rótulo sugerido não está entre os sites da metade"
        )


@pytest.mark.parametrize("rota, numero", [
    # Número do RJ com rota de SP selecionada: o corte de SP não vale aqui.
    # Sem a guarda de UF, isto sugeria "TJSP — e-SAJ" — rota que também não o tem.
    ("TJSP — eproc", "0024491-10.2021.8.19.0203"),
    ("TJSP — e-SAJ", "0800123-45.2025.8.19.0001"),
    ("TJRJ — PJe", "1000034-65.2025.8.26.0362"),
])
def test_corte_de_uma_uf_nao_se_aplica_a_numero_de_outra(rota, numero):
    """Discrepância de UF é outro assunto, e a gui.py já avisa sobre ela antes."""
    assert rota_incompativel_com_numero(rota, numero) is None


def test_sistemas_de_sp_se_sobrepoem(_=None):
    """O contraexemplo que derrubou a primeira versão da regra de SP.

    `data/_naomapeados_estado.json` guarda em que sistema cada processo foi
    ACHADO. O lote de SP tentava eproc antes do e-SAJ, e 29 dos 314 processos
    `0*`/`1*` foram achados no eproc — o que parecia contradizer o corte. A
    conferência ao vivo mostrou outra coisa: 8 de 8 desses estavam TAMBÉM no
    e-SAJ. Os sistemas de SP se sobrepõem, então `1*` não é exclusivo de
    ninguém, mas o e-SAJ (público, 100% de cobertura) é a sugestão certa.
    """
    from organizador.config import _SITES_DA_METADE

    assert _SITES_DA_METADE[("SP", 0)] == frozenset({"esaj", "eproc"})
    assert sugerir_tj_para_uf("SP", "1002290-32.2025.8.26.0248") == "TJSP — e-SAJ"
    assert rota_exclusiva_para_numero("1002290-32.2025.8.26.0248") is None
    assert rota_incompativel_com_numero(
        "TJSP — eproc", "1002290-32.2025.8.26.0248") is None


def test_rota_do_rs_esta_ligada_de_ponta_a_ponta():
    """TJRS usa Keycloak PRÓPRIO (keycloak-eks.tjrs.jus.br/realms/eproc), não o
    PDPJ — mesmo formato do eproc do RJ, inclusive na armadilha: a tela também
    traz #txtUsuario/#pwdSenha do eproc interno, e apontar para eles faria a
    espera por visibilidade estourar num campo que nunca aparece."""
    from organizador.config import _ESTADO_TJ_UF, uf_efetiva_tj
    from organizador.integracoes.tjsp_scraper.scrapers.eproc import _CONFIG_POR_ESTADO

    site, estado, url, perfil = TJ_SISTEMAS["TJRS — eproc"]
    assert (site, perfil) == ("eproc", None)
    assert url.startswith("https://eproc1g.tjrs.jus.br")
    assert _ESTADO_TJ_UF[estado] == ("TJRS", "RS")
    assert uf_efetiva_tj("TJRS — eproc") == "RS"

    cfg = _CONFIG_POR_ESTADO[estado]
    assert (cfg["login_user_id"], cfg["login_pass_id"]) == ("username", "password")
    assert cfg["login_submit_id"] == "kc-login"
    assert cfg["totp_env"] == "TOTP_EPROC_TJRS"


def test_toda_rota_eproc_tem_config_de_estado():
    """Sem entrada em `_CONFIG_POR_ESTADO` o scraper do eproc não sabe nem os ids
    da tela de login — falharia só na hora de logar, depois de abrir o Chrome."""
    from organizador.integracoes.tjsp_scraper.scrapers.eproc import _CONFIG_POR_ESTADO

    for rotulo, (site, estado, _url, _perfil) in TJ_SISTEMAS.items():
        if site == "eproc":
            assert estado in _CONFIG_POR_ESTADO, f"{rotulo}: estado {estado} sem config"
