# -*- coding: utf-8 -*-
"""Cobertura do leitor de 2FA por e-mail do e-SAJ.

Guarda as três regressões reais que travavam o login no e-SAJ (jun/2026):
  1. remetente errado — o código chega de acesso.tribunais@mcsarc.com.br,
     não de csc01@candiottovalle.com;
  2. sem filtro de assunto — o MESMO remetente manda "Andamento Processual"
     com números de 6 dígitos, que não podem ser confundidos com o código;
  3. janela `desde` — só e-mails posteriores ao login valem, mas o leitor
     deve devolver o MAIS RECENTE que casa remetente+assunto.
"""

from __future__ import annotations

import email.utils
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from types import SimpleNamespace

import pytest

from organizador.integracoes.tjsp_scraper import two_factor
from organizador.integracoes.tjsp_scraper.scrapers import esaj

UTC = timezone.utc
BASE = datetime(2026, 6, 7, 23, 30, 0, tzinfo=UTC)
REMETENTE_OK = '"e-SAJ" via Acessos Tribunais <acesso.tribunais@mcsarc.com.br>'
ASSUNTO_2FA = "Portal e-SAJ - Validação de identificação"


def _raw(sender: str, subject: str, body: str, when: datetime) -> bytes:
    m = EmailMessage()
    m["From"] = sender
    m["Subject"] = subject
    m["Date"] = email.utils.format_datetime(when)
    m.set_content(body)
    return m.as_bytes()


class _FakeIMAP:
    """IMAP mínimo: ids 1..N na ordem em que `raws` é passado (mais antigo
    primeiro). `obter_codigo_2fa_email` itera em reversed() → mais novo
    primeiro."""

    def __init__(self, raws: list[bytes]):
        self._raws = raws

    def select(self, pasta):
        return ("OK", [b"1"])

    def search(self, charset, query):
        ids = b" ".join(str(i + 1).encode() for i in range(len(self._raws)))
        return ("OK", [ids])

    def fetch(self, msg_id, what):
        return ("OK", [(b"hdr", self._raws[int(msg_id) - 1])])

    def logout(self):
        return ("BYE", [b""])


class _FakeClock:
    """Relógio determinístico: avança `step` a cada time(); sleep() só soma."""

    def __init__(self, start=1000.0, step=1.0):
        self._t = start
        self._step = step

    def time(self):
        t = self._t
        self._t += self._step
        return t

    def sleep(self, s):
        self._t += s


def _patch(monkeypatch, raws, step=1.0):
    monkeypatch.setattr(two_factor, "_imap_conectar", lambda: _FakeIMAP(raws))
    monkeypatch.setattr(two_factor, "time", _FakeClock(step=step))


def _ler(remetente=None, assunto=None, desde=BASE, timeout=5):
    return two_factor.obter_codigo_2fa_email(
        desde=desde,
        remetente=remetente if remetente is not None else esaj.REMETENTE_2FA,
        assunto_contem=assunto if assunto is not None else esaj.ASSUNTO_2FA,
        timeout_seg=timeout,
        intervalo_seg=3,
    )


def test_pega_o_codigo_mais_recente_e_ignora_remetente_errado(monkeypatch):
    raws = [
        _raw(REMETENTE_OK, ASSUNTO_2FA, "Código: 111111", BASE + timedelta(minutes=1)),
        _raw(REMETENTE_OK, "Portal e-Saj - Andamento Processual", "Proc 999999",
             BASE + timedelta(minutes=2)),
        _raw(REMETENTE_OK, ASSUNTO_2FA, "Seu código é 222222", BASE + timedelta(minutes=3)),
        _raw("no-reply@tjsp.jus.br", ASSUNTO_2FA, "Código: 555555",
             BASE + timedelta(minutes=4)),
    ]
    _patch(monkeypatch, raws)
    # Ignora o remetente errado (mais novo) e o andamento; pega o 2FA mais recente.
    assert _ler() == "222222"


def test_ignora_mesmo_remetente_com_assunto_de_andamento(monkeypatch):
    raws = [
        _raw(REMETENTE_OK, ASSUNTO_2FA, "Código: 333333", BASE + timedelta(minutes=1)),
        _raw(REMETENTE_OK, "Portal e-Saj - Andamento Processual", "Mov 888888",
             BASE + timedelta(minutes=5)),
    ]
    _patch(monkeypatch, raws)
    assert _ler() == "333333"


def test_descarta_codigo_anterior_ao_login(monkeypatch):
    raws = [
        _raw(REMETENTE_OK, ASSUNTO_2FA, "Código: 444444", BASE - timedelta(minutes=5)),
    ]
    _patch(monkeypatch, raws, step=1.0)
    # Único e-mail é anterior ao `desde` → não serve → timeout → None.
    assert _ler(timeout=2) is None


def test_escolhe_maior_data_mesmo_fora_de_ordem_de_sequencia(monkeypatch):
    """E-mails encaminhados chegam fora de ordem: o código novo pode ter
    número de sequência IMAP MENOR que um código antigo. O leitor deve
    escolher pelo maior Date, não pela posição na caixa."""
    raws = [
        # seq 1 = código NOVO (Date mais recente)
        _raw(REMETENTE_OK, ASSUNTO_2FA, "Código: 866057", BASE + timedelta(minutes=5)),
        # seq 2 = código ANTIGO (Date anterior, mas sequência maior)
        _raw(REMETENTE_OK, ASSUNTO_2FA, "Código: 589134", BASE + timedelta(minutes=2)),
    ]
    _patch(monkeypatch, raws)
    assert _ler() == "866057"


def test_constantes_do_esaj_corrigidas():
    # Guarda os bugs #1 e #2 contra regressão.
    assert esaj.REMETENTE_2FA == "acesso.tribunais@mcsarc.com.br"
    assert esaj.ASSUNTO_2FA == "Validação de identificação"
