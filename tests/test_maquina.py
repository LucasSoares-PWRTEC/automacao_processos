# -*- coding: utf-8 -*-
"""Detecção de máquina fraca e resolução do Modo leve (organizador.maquina)."""

from __future__ import annotations

from organizador import maquina
from organizador.maquina import (
    NUCLEOS_MINIMOS,
    RAM_MINIMA_MB,
    PerfilMaquina,
    modo_leve_automatico,
    perfil_maquina,
    resolver_modo_leve,
)


# ---------------------------------------------------------------------
# Classificação
# ---------------------------------------------------------------------
def test_maquina_folgada_nao_e_fraca():
    assert not PerfilMaquina(ram_total_mb=16384, nucleos=8).fraca


def test_i3_de_4gb_e_fraca():
    """O caso que motivou tudo: i3 de 3ª geração, 2 núcleos, 4 GB."""
    assert PerfilMaquina(ram_total_mb=4096, nucleos=2).fraca


def test_pouca_ram_sozinha_basta():
    assert PerfilMaquina(ram_total_mb=RAM_MINIMA_MB - 1, nucleos=8).fraca
    assert not PerfilMaquina(ram_total_mb=RAM_MINIMA_MB, nucleos=8).fraca


def test_poucos_nucleos_sozinho_basta():
    assert PerfilMaquina(ram_total_mb=32768, nucleos=NUCLEOS_MINIMOS - 1).fraca
    assert not PerfilMaquina(ram_total_mb=32768, nucleos=NUCLEOS_MINIMOS).fraca


def test_ram_desconhecida_nao_conta_como_fraca():
    """RAM 0 = falha de medição. Não vale degradar a experiência por isso — a
    decisão fica só com os núcleos."""
    assert not PerfilMaquina(ram_total_mb=0, nucleos=8).fraca
    assert PerfilMaquina(ram_total_mb=0, nucleos=2).fraca


def test_descricao_legivel():
    assert PerfilMaquina(ram_total_mb=4096, nucleos=2).descricao() == "4.0 GB, 2 núcleo(s)"
    assert "RAM ?" in PerfilMaquina(ram_total_mb=0, nucleos=4).descricao()


# ---------------------------------------------------------------------
# Medição real
# ---------------------------------------------------------------------
def test_perfil_da_maquina_atual_e_plausivel():
    p = perfil_maquina()
    assert p.nucleos >= 1
    assert p.ram_total_mb >= 512, "não conseguiu ler a RAM total desta máquina"


def test_modo_leve_automatico_segue_o_perfil(monkeypatch):
    monkeypatch.setattr(
        maquina, "perfil_maquina", lambda: PerfilMaquina(ram_total_mb=4096, nucleos=2)
    )
    assert modo_leve_automatico() is True
    monkeypatch.setattr(
        maquina, "perfil_maquina", lambda: PerfilMaquina(ram_total_mb=16384, nucleos=8)
    )
    assert modo_leve_automatico() is False


# ---------------------------------------------------------------------
# Escolha do usuário x detecção
# ---------------------------------------------------------------------
def test_escolha_explicita_do_usuario_vence_a_deteccao(monkeypatch):
    """Detecção nunca sobrescreve o que a pessoa marcou em Configurações."""
    monkeypatch.setattr(maquina, "modo_leve_automatico", lambda: True)
    assert resolver_modo_leve(False) is False   # máquina fraca, usuário desligou
    monkeypatch.setattr(maquina, "modo_leve_automatico", lambda: False)
    assert resolver_modo_leve(True) is True     # máquina boa, usuário ligou


def test_sem_valor_salvo_cai_na_deteccao(monkeypatch):
    monkeypatch.setattr(maquina, "modo_leve_automatico", lambda: True)
    assert resolver_modo_leve(None) is True
    assert resolver_modo_leve("") is True
    monkeypatch.setattr(maquina, "modo_leve_automatico", lambda: False)
    assert resolver_modo_leve(None) is False


def test_aceita_valores_de_json_editado_a_mao(monkeypatch):
    """ui_state.json é texto e às vezes é ajustado à mão."""
    monkeypatch.setattr(maquina, "modo_leve_automatico", lambda: False)
    for valor in ("true", "True", "1", "sim", "on", " YES "):
        assert resolver_modo_leve(valor) is True, valor
    for valor in ("false", "0", "nao", "qualquer coisa"):
        assert resolver_modo_leve(valor) is False, valor
    assert resolver_modo_leve(1) is True
    assert resolver_modo_leve(0) is False


def test_ajustes_documentados_para_a_ui():
    """A aba Configurações exibe esta lista; vazia seria um toggle sem explicação."""
    assert maquina.AJUSTES_MODO_LEVE
    assert all(isinstance(t, str) and t for t in maquina.AJUSTES_MODO_LEVE)
