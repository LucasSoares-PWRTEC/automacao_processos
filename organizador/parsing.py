# -*- coding: utf-8 -*-
"""Parsing de texto de PDF para a estrutura Processo (capa, extrato, ato)."""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

from .models import Advogado, AtoDocumento, Evento, Parte, Prazo, Processo
from .padroes import (
    CAMPOS_CABECALHO, MAPA_TRIBUNAL_CNJ, MAPA_UF_CNJ, PAPEIS_ATIVO, PAPEIS_PASSIVO,
    RE_ADVOGADO, RE_ASSUNTO_ATO, RE_BLOCO_PARTES, RE_CABECALHO_ATO, RE_CABECALHO_PARTES,
    RE_CABECALHO_TABELA_EVENTOS, RE_CNJ_TR, RE_CODIGO_CRC, RE_CODIGO_VERIFICADOR,
    RE_DATA_ASSINATURA, RE_ENDERECO_VARA, RE_EXTRATO_CHAVE, RE_EXTRATO_MAGISTRADO,
    RE_EXTRATO_NOME, RE_EXTRATO_OAB, RE_EXTRATO_ORGAO, RE_EXTRATO_PARTE,
    RE_EXTRATO_VALOR_CAUSA, RE_FIM_TABELA_EVENTOS, RE_LINHA_DOC, RE_LINHA_OAB,
    RE_LINHA_USUARIO, RE_LOCAL_ATO, RE_MULTI_ESPACO, RE_NAO_GEROU_DOC, RE_NUMERO_CNJ,
    RE_PARTE_ATO, RE_PRAZO_DATA_FINAL, RE_PRAZO_DATA_INICIAL, RE_PRAZO_HEAD, RE_PUA,
    RE_REFER_EVENTO, RE_RUIDO_NOME_PARTE, RE_SIGNATARIO, RE_TIPO_ATO, RE_USUARIO_TAIL,
    TAGS_CONHECIDAS,
)
from .texto import detectar_tipo_pdf


def _valor_apos_rotulo(texto: str, rotulo_regex: str) -> str:
    """Pega a primeira linha não-vazia logo após o rótulo."""
    m = re.search(rotulo_regex + r"\s*\n+\s*(?P<val>[^\n]+)", texto, re.IGNORECASE)
    if not m:
        return ""
    return m.group("val").strip()


def _inferir_tribunal(numero_cnj: str) -> tuple[str, str]:
    """Retorna (tribunal, uf) a partir do CNJ. Vazio se desconhecido."""
    m = RE_CNJ_TR.search(numero_cnj or "")
    if not m:
        return "", ""
    chave = f"{m.group(1)}.{m.group(2)}"
    return MAPA_TRIBUNAL_CNJ.get(chave, ""), MAPA_UF_CNJ.get(chave, "")


def _uf_de_numero_cnj(numero: str) -> str:
    """UF inferida do número CNJ (segmento .J.TR.). Fallback para quando a
    comarca/UF não veio do processo — vazio se não for justiça estadual."""
    _, uf = _inferir_tribunal(numero)
    return uf


def _municipio_de_comarca(comarca: str) -> str:
    """Extrai o nome do município de uma string tipo 'Comarca de Carapicuíba'."""
    if not comarca:
        return ""
    s = comarca.strip()
    for prefixo in ("Comarca de ", "Foro Regional de ", "Foro de "):
        if s.lower().startswith(prefixo.lower()):
            return s[len(prefixo):].strip()
    return s


def _oab_completa(uf: str, numero: str) -> str:
    """Formata a OAB completa no padrão 'OAB/UF NÚMERO' (ex.: 'OAB/SP 346.433')."""
    uf = (uf or "").strip().upper()
    numero = (numero or "").strip()
    if uf and numero:
        return f"OAB/{uf} {numero}"
    return numero or (f"OAB/{uf}" if uf else "")


def _parse_orgao_julgador(orgao: str) -> tuple[str, str]:
    if not orgao:
        return "", ""
    m = re.search(
        r"(?P<vara>(?:\d+[ªº]\s+)?(?:UPJ[^,]*?|Ju[íi]zo[^,]*?|Vara[^,]*?))"
        r"\s+(?:da|de|do)\s+(?P<comarca>(?:Comarca|Regional|Foro|Subse[çc][ãa]o|Se[çc][ãa]o)\s+(?:Judici[áa]ria\s+)?(?:de|da|do)?\s*[^,\n]+)",
        orgao,
        re.IGNORECASE,
    )
    if m:
        vara = re.sub(r"\s+", " ", m.group("vara")).strip()
        comarca = re.sub(r"\s+", " ", m.group("comarca")).strip()
        return vara, comarca
    m2 = re.search(r"(?P<vara>\d+[ªº]\s+Vara[^,\n]*?)\s+(?:da\s+)?Comarca\s+de\s+(?P<comarca>[^,\n]+)", orgao, re.IGNORECASE)
    if m2:
        return m2.group("vara").strip(), f"Comarca de {m2.group('comarca').strip()}"
    return orgao.strip(), ""


def _extrair_advogados(tail: str) -> list[Advogado]:
    """Extrai advogados (nome + OAB) de um trecho de texto após uma parte."""
    advs: list[Advogado] = []
    vistos: set[str] = set()
    for m in RE_ADVOGADO.finditer(tail):
        nome = re.sub(r"\s+", " ", m.group(1)).strip()
        uf = m.group(2).strip().upper()
        num = m.group(3).strip()
        oab = f"{uf}{num}"
        if oab in vistos:
            continue
        vistos.add(oab)
        advs.append(Advogado(nome=nome, oab=oab, uf=uf))
    return advs


def _parse_cabecalho(texto: str, proc: Processo) -> None:
    for atributo, padrao in CAMPOS_CABECALHO:
        valor = _valor_apos_rotulo(texto, padrao)
        if valor:
            setattr(proc, atributo, valor)
    m = RE_NUMERO_CNJ.search(proc.numero or texto)
    if m:
        proc.numero = m.group(0)
    topo = texto[:1500]
    for tag in TAGS_CONHECIDAS:
        if re.search(r"\b" + re.escape(tag) + r"\b", topo, re.IGNORECASE):
            if tag not in proc.tags:
                proc.tags.append(tag)
    m_assunto = re.search(r"Assunto[s]?:\s*\n*\s*([^\n]+)", texto, re.IGNORECASE)
    if m_assunto:
        proc.assunto = m_assunto.group(1).strip()
    tribunal, uf = _inferir_tribunal(proc.numero)
    if tribunal:
        proc.tribunal = tribunal
    if uf:
        proc.uf = uf
    if proc.orgao_julgador:
        vara, comarca = _parse_orgao_julgador(proc.orgao_julgador)
        if vara:
            proc.vara = vara
        if comarca:
            proc.comarca = comarca


def _detectar_papeis(texto: str) -> tuple[str, str]:
    m_bloco = re.search(r"Partes\s+e\s+Representantes(.{0,300})", texto, re.DOTALL | re.IGNORECASE)
    janela = m_bloco.group(1) if m_bloco else texto[:500]
    m = RE_CABECALHO_PARTES.search(janela)
    if m:
        return m.group(1).upper(), m.group(2).upper()
    return "AUTOR", "RÉU"


def _parse_partes(texto: str) -> list[Parte]:
    m = RE_BLOCO_PARTES.search(texto)
    if not m:
        return []
    bloco = m.group(1)
    papel_ativo, papel_passivo = _detectar_papeis(texto)

    parte_pattern = re.compile(
        r"(?P<nome>[A-ZÁÉÍÓÚÂÊÔÃÕÇ][^\n()]{2,}?)"
        r"\s*\(\s*(?P<doc>[\d./-]{11,})\s*\)\s*-\s*"
        r"(?P<tipo>Pessoa\s+(?:Jur[íi]dica|F[íi]sica))",
        re.IGNORECASE,
    )
    matches = list(parte_pattern.finditer(bloco))
    if not matches:
        return []

    partes: list[Parte] = []
    for i, mp in enumerate(matches):
        nome = re.sub(r"\s+", " ", mp.group("nome")).strip()
        # Remove rótulos que o PDF cola ao nome (ex.: "Histórico de RepresentantesITAU...").
        nome_limpo = RE_RUIDO_NOME_PARTE.sub("", nome).strip()
        if nome_limpo:
            nome = nome_limpo
        doc = mp.group("doc").strip()
        tipo = re.sub(r"\s+", " ", mp.group("tipo")).strip()

        ini = mp.end()
        fim = matches[i + 1].start() if i + 1 < len(matches) else len(bloco)
        tail = bloco[ini:fim]

        advs = _extrair_advogados(tail)
        papel = papel_ativo if i % 2 == 0 else papel_passivo
        partes.append(Parte(papel=papel, nome=nome, documento=doc, tipo=tipo, advogados=advs))
    return partes


def _extrair_bloco_eventos(texto: str) -> str:
    cabecalhos = list(RE_CABECALHO_TABELA_EVENTOS.finditer(texto))
    if not cabecalhos:
        return ""
    pedacos: list[str] = []
    for i, cab in enumerate(cabecalhos):
        inicio = cab.end()
        fim_cab = cabecalhos[i + 1].start() if i + 1 < len(cabecalhos) else len(texto)
        m_fim = RE_FIM_TABELA_EVENTOS.search(texto, pos=inicio, endpos=fim_cab)
        fim = m_fim.start() if m_fim else fim_cab
        pedacos.append(texto[inicio:fim])
    return "\n".join(p.strip() for p in pedacos if p.strip())


def _limpa_linha(linha: str) -> str:
    """Remove icones PUA, normaliza non-breaking space e colapsa whitespace.

    Linhas do PDF terminam com chars PUA (icones de fonte Material Symbols).
    Sem limpar, regexes que procuram tokens entram em catastrophic backtracking
    em eventos com muitos documentos."""
    s = RE_PUA.sub("", linha).replace(" ", " ")
    return RE_MULTI_ESPACO.sub(" ", s).strip()


_RE_EVENTO_HEAD_INLINE = re.compile(
    r"^(\d+)\s+(\d{2}/\d{2}/\d{4})(?:\s+(\d{2}:\d{2}:\d{2}))?\s*(.*)$"
)
_RE_EVENTO_NUM_SO = re.compile(r"^(\d+)$")
_RE_EVENTO_DATA = re.compile(
    r"^(\d{2}/\d{2}/\d{4})(?:\s+(\d{2}:\d{2}:\d{2}))?\s*(.*)$"
)
_RE_EVENTO_HORA = re.compile(r"^(\d{2}:\d{2}:\d{2})\s*(.*)$")


def _parse_evento_linhas(linhas: list[str]) -> Evento | None:
    """Parseia um unico evento a partir de suas linhas.

    Classifica linha a linha (documento, prazo, usuario, descricao) - nunca
    aplica regex sobre o texto concatenado. Elimina o backtracking que
    travava o programa em eventos como "Juntada de integra" com 200+ docs."""
    if not linhas:
        return None

    ev = Evento()
    idx = 0
    descricao_inline = ""

    primeira = _limpa_linha(linhas[idx])
    m = _RE_EVENTO_HEAD_INLINE.match(primeira)
    if m:
        ev.numero = m.group(1)
        ev.data = m.group(2)
        if m.group(3):
            ev.hora = m.group(3)
        descricao_inline = (m.group(4) or "").strip()
        idx += 1
    else:
        m_num = _RE_EVENTO_NUM_SO.match(primeira)
        if not m_num:
            return None
        ev.numero = m_num.group(1)
        idx += 1
        if idx < len(linhas):
            l2 = _limpa_linha(linhas[idx])
            m_d = _RE_EVENTO_DATA.match(l2)
            if m_d:
                ev.data = m_d.group(1)
                if m_d.group(2):
                    ev.hora = m_d.group(2)
                descricao_inline = (m_d.group(3) or "").strip()
                idx += 1

    if not ev.hora and idx < len(linhas):
        l_h = _limpa_linha(linhas[idx])
        m_h = _RE_EVENTO_HORA.match(l_h)
        if m_h:
            ev.hora = m_h.group(1)
            resto = m_h.group(2).strip()
            if resto:
                descricao_inline = f"{descricao_inline} {resto}".strip()
            idx += 1

    desc_parts: list[str] = []
    if descricao_inline:
        desc_parts.append(descricao_inline)
    docs: list[str] = []
    nao_gerou_doc = False
    usuario = ""

    for raw in linhas[idx:]:
        l = _limpa_linha(raw)
        if not l:
            continue

        if RE_LINHA_DOC.match(l) and not RE_LINHA_OAB.match(l):
            if l not in docs:
                docs.append(l)
            continue

        if RE_NAO_GEROU_DOC.search(l):
            nao_gerou_doc = True
            cleaned = RE_NAO_GEROU_DOC.sub("", l).strip()
            if cleaned:
                desc_parts.append(cleaned)
            continue

        m_pz = RE_PRAZO_HEAD.search(l)
        if m_pz:
            try:
                ev.prazo_dias = int(m_pz.group("dias"))
            except (TypeError, ValueError):
                pass
            ev.prazo_status = (m_pz.group("status") or "").upper()
            m_ini = RE_PRAZO_DATA_INICIAL.search(l)
            if m_ini:
                ev.prazo_inicio = m_ini.group(1)
            m_fim = RE_PRAZO_DATA_FINAL.search(l)
            if m_fim:
                ev.prazo_fim = m_fim.group(1)
            continue

        if not ev.prazo_inicio:
            m_ini = RE_PRAZO_DATA_INICIAL.search(l)
            if m_ini:
                ev.prazo_inicio = m_ini.group(1)
                continue
        if not ev.prazo_fim:
            m_fim = RE_PRAZO_DATA_FINAL.search(l)
            if m_fim:
                ev.prazo_fim = m_fim.group(1)
                continue

        m_ref = RE_REFER_EVENTO.search(l)
        if m_ref:
            ev.refer_evento = re.sub(r"\s+", "", m_ref.group(1)).rstrip(",")
            desc_parts.append(l)
            continue

        if RE_LINHA_USUARIO.match(l):
            usuario = l
            continue

        desc_parts.append(l)

    descricao = RE_MULTI_ESPACO.sub(" ", " ".join(desc_parts)).strip()

    if not usuario:
        m_u = RE_USUARIO_TAIL.search(descricao)
        if m_u:
            usuario = m_u.group("user").strip()
            descricao = descricao[:m_u.start()].rstrip()

    ev.descricao = descricao
    ev.usuario = usuario
    if docs:
        ev.documentos = docs
    elif nao_gerou_doc:
        ev.documentos = ["Evento nao gerou documento"]
    return ev


def _parse_eventos(texto: str) -> list[Evento]:
    bloco = _extrair_bloco_eventos(texto)
    if not bloco:
        return []
    linhas = bloco.splitlines()

    inicios: list[int] = []
    for i, l in enumerate(linhas):
        ls = _limpa_linha(l)
        if re.match(r"^\d+\s+\d{2}/\d{2}/\d{4}", ls):
            inicios.append(i)
        elif re.match(r"^\d+$", ls) and i + 1 < len(linhas):
            if re.match(r"^\d{2}/\d{2}/\d{4}", _limpa_linha(linhas[i + 1])):
                inicios.append(i)

    if not inicios:
        return []
    inicios.append(len(linhas))

    eventos: list[Evento] = []
    for k in range(len(inicios) - 1):
        i, j = inicios[k], inicios[k + 1]
        ev = _parse_evento_linhas(linhas[i:j])
        if ev is not None:
            eventos.append(ev)
    return eventos


# ---------------------------------------------------------------------------
# Parsers de outros tipos de PDF
# ---------------------------------------------------------------------------

def _parse_extrato_distribuicao(texto: str, proc: Processo) -> None:
    m_num = RE_NUMERO_CNJ.search(texto)
    if m_num and not proc.numero:
        proc.numero = m_num.group(0)
    m = RE_EXTRATO_CHAVE.search(texto)
    if m and not proc.chave_consulta:
        proc.chave_consulta = m.group(1).strip()
    m = RE_EXTRATO_VALOR_CAUSA.search(texto)
    if m and not proc.valor_causa:
        proc.valor_causa = f"R$ {m.group(1).strip()}"
    m = RE_EXTRATO_ORGAO.search(texto)
    if m and not proc.orgao_julgador:
        proc.orgao_julgador = m.group(1).strip()
    m = RE_EXTRATO_MAGISTRADO.search(texto)
    if m and not proc.juiz:
        proc.juiz = m.group(1).strip()
    if not proc.partes:
        partes_ext: list[Parte] = []
        for mp in RE_EXTRATO_PARTE.finditer(texto):
            nome = mp.group(1).strip()
            papel = mp.group(2).strip().upper()
            if len(nome) < 3 or nome.lower().startswith(("nome", "evento", "orgao", "magistrado")):
                continue
            partes_ext.append(Parte(papel=papel, nome=nome))
        if partes_ext:
            proc.partes = partes_ext
    m_nome = RE_EXTRATO_NOME.search(texto)
    m_oab = RE_EXTRATO_OAB.search(texto)
    if m_nome and m_oab and proc.partes:
        nome_adv = m_nome.group(1).strip()
        uf = m_oab.group(1).strip().upper()
        num = m_oab.group(2).strip()
        adv = Advogado(nome=nome_adv, oab=f"{uf}{num}", uf=uf)
        for p in proc.partes:
            if p.papel.upper() in [pp.upper() for pp in PAPEIS_ATIVO]:
                if not any(a.oab == adv.oab for a in p.advogados):
                    p.advogados.append(adv)
                break
    tribunal, uf = _inferir_tribunal(proc.numero)
    if tribunal and not proc.tribunal:
        proc.tribunal = tribunal
    if uf and not proc.uf:
        proc.uf = uf
    if proc.orgao_julgador and not proc.vara:
        vara, comarca = _parse_orgao_julgador(proc.orgao_julgador)
        if vara:
            proc.vara = vara
        if comarca and not proc.comarca:
            proc.comarca = comarca


def _parse_ato(texto: str, arquivo: Path | None) -> AtoDocumento:
    ato = AtoDocumento()
    if arquivo:
        ato.arquivo = arquivo.name

    m_cab = RE_CABECALHO_ATO.search(texto)
    if m_cab:
        ato.classe_acao = m_cab.group(1).strip()

    for m in RE_TIPO_ATO.finditer(texto):
        tipo = re.sub(r"\s+", " ", m.group(1)).strip().upper()
        if tipo:
            ato.tipo = tipo
            break

    m_sig = RE_SIGNATARIO.search(texto)
    if m_sig:
        sig = re.sub(r"\s+", " ", m_sig.group(1)).strip()
        sig = re.split(r"\s*,\s*na forma", sig, maxsplit=1)[0]
        ato.signatario = sig.strip()

    m_data = RE_DATA_ASSINATURA.search(texto)
    if m_data:
        ato.data_assinatura = re.sub(r"\s+", " ", m_data.group(1)).strip()

    m_ver = RE_CODIGO_VERIFICADOR.search(texto)
    if m_ver:
        ato.codigo_verificador = m_ver.group(1).strip()
    m_crc = RE_CODIGO_CRC.search(texto)
    if m_crc:
        ato.codigo_crc = m_crc.group(1).strip()

    m_local = RE_LOCAL_ATO.search(texto)
    if m_local:
        ato.local = m_local.group(1).strip()

    if ato.tipo:
        idx_tipo = texto.lower().find(ato.tipo.lower())
        idx_assina = texto.lower().find("documento eletrônico")
        if idx_assina == -1:
            idx_assina = texto.lower().find("documento eletronico")
        if idx_tipo >= 0:
            inicio = idx_tipo + len(ato.tipo)
            fim = idx_assina if idx_assina > inicio else len(texto)
            corpo = texto[inicio:fim].strip()
            ato.teor = re.sub(r"\n{3,}", "\n\n", corpo)[:4000]

    return ato


def _ato_para_processo(ato: AtoDocumento, texto: str, arquivo: Path | None) -> Processo:
    proc = Processo()
    if arquivo:
        proc.arquivos_origem.append(arquivo.name)
    m = RE_NUMERO_CNJ.search(texto)
    if m:
        proc.numero = m.group(0)
    if ato.classe_acao:
        proc.classe = ato.classe_acao
    m_assunto = RE_ASSUNTO_ATO.search(texto)
    if m_assunto:
        proc.assunto = m_assunto.group(1).strip()
    partes_ato: list[Parte] = []
    for mp in RE_PARTE_ATO.finditer(texto):
        papel = mp.group("papel").strip().upper()
        nome = mp.group("nome").strip()
        if len(nome) < 3:
            continue
        partes_ato.append(Parte(papel=papel, nome=nome))
    if partes_ato:
        proc.partes = partes_ato
    m_orgao = re.search(
        r"((?:UPJ|Ju[íi]zo|Vara)[^,\n]+?(?:Comarca|Foro|Regional|Subse[çc][ãa]o)[^,\n]+)",
        texto,
        re.IGNORECASE,
    )
    if m_orgao:
        proc.orgao_julgador = re.sub(r"\s+", " ", m_orgao.group(1)).strip()
    enderecos = []
    for me in RE_ENDERECO_VARA.finditer(texto[:2000]):
        linha = re.sub(r"\s+", " ", me.group(1)).strip()
        if linha and linha not in enderecos:
            enderecos.append(linha)
        if len(enderecos) >= 2:
            break
    if enderecos:
        proc.foro_endereco = " | ".join(enderecos)
    tribunal, uf = _inferir_tribunal(proc.numero)
    if tribunal:
        proc.tribunal = tribunal
    if uf:
        proc.uf = uf
    if proc.orgao_julgador:
        vara, comarca = _parse_orgao_julgador(proc.orgao_julgador)
        if vara:
            proc.vara = vara
        if comarca:
            proc.comarca = comarca
    proc.atos.append(ato)
    return proc


# ---------------------------------------------------------------------------
# Agregados derivados (prazos abertos, primeira/última movimentação, etc.)
# ---------------------------------------------------------------------------

def _parse_data_br(d: str) -> date | None:
    try:
        return datetime.strptime(d.strip(), "%d/%m/%Y").date()
    except (ValueError, AttributeError):
        return None


def _evento_parte_papel(ev: Evento, partes: list[Parte]) -> tuple[str, str]:
    m = re.search(
        r"\((?P<papel>" + "|".join(PAPEIS_ATIVO + PAPEIS_PASSIVO) + r")\s*-\s*(?P<nome>[^)]+)\)",
        ev.descricao,
        re.IGNORECASE,
    )
    if m:
        return m.group("nome").strip(), m.group("papel").strip().upper()
    m2 = re.search(r"Decorrido\s+prazo\s+de\s+([A-ZÁÉÍÓÚÂÊÔÃÕÇ][^.]+?)\s+em\s+\d{2}/\d{2}", ev.descricao)
    if m2:
        nome = m2.group(1).strip()
        for p in partes:
            if p.nome.upper() == nome.upper():
                return p.nome, p.papel
        return nome, ""
    return "", ""


def _derivar_agregados(proc: Processo, hoje: date | None = None) -> None:
    hoje = hoje or date.today()
    proc.total_eventos = len(proc.eventos)
    if proc.eventos:
        primeiro = proc.eventos[-1]
        ultimo = proc.eventos[0]
        proc.primeira_movimentacao = primeiro.data
        desc_curta = (ultimo.descricao[:80] + "...") if len(ultimo.descricao) > 80 else ultimo.descricao
        proc.ultima_movimentacao = f"{ultimo.data} — {desc_curta}".strip(" —")
    prazos: list[Prazo] = []
    for ev in proc.eventos:
        if ev.prazo_status != "ABERTO":
            continue
        fim = _parse_data_br(ev.prazo_fim)
        if fim is not None and fim < hoje:
            continue
        nome, papel = _evento_parte_papel(ev, proc.partes)
        prazos.append(Prazo(
            parte_nome=nome,
            parte_papel=papel,
            dias=ev.prazo_dias or 0,
            inicio=ev.prazo_inicio,
            fim=ev.prazo_fim,
            status=ev.prazo_status,
            evento_origem=ev.numero,
        ))
    prazos.sort(key=lambda p: _parse_data_br(p.fim) or date.max)
    proc.prazos_abertos = prazos


# ---------------------------------------------------------------------------
# Dispatcher principal
# ---------------------------------------------------------------------------

def parse_processo(texto: str, arquivo_origem: Path | None = None) -> Processo:
    proc = Processo()
    if arquivo_origem:
        proc.arquivos_origem.append(arquivo_origem.name)
    _parse_cabecalho(texto, proc)
    proc.partes = _parse_partes(texto)
    proc.eventos = _parse_eventos(texto)
    _derivar_agregados(proc)
    return proc


def parse_pdf(texto: str, arquivo: Path | None = None) -> tuple[str, Processo]:
    tipo = detectar_tipo_pdf(texto)
    if tipo == "detalhes":
        proc = parse_processo(texto, arquivo)
        return "detalhes", proc
    if tipo == "extrato":
        proc = Processo()
        if arquivo:
            proc.arquivos_origem.append(arquivo.name)
        _parse_extrato_distribuicao(texto, proc)
        return "extrato", proc
    if tipo == "ato":
        ato = _parse_ato(texto, arquivo)
        proc = _ato_para_processo(ato, texto, arquivo)
        return "ato", proc
    proc = Processo()
    if arquivo:
        proc.arquivos_origem.append(arquivo.name)
    m = RE_NUMERO_CNJ.search(texto)
    if m:
        proc.numero = m.group(0)
    return "desconhecido", proc
