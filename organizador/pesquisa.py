# -*- coding: utf-8 -*-
"""Lógica pura da aba Pesquisa de Processo.

Mantém fora do Tkinter as operações que podem ficar caras com bases maiores:
carregamento/mescla de fontes, filtro e montagem das linhas exibidas na lista.
"""

from __future__ import annotations

from dataclasses import fields as _campos_de
from pathlib import Path

from .models import Processo
from .organizacao import (
    _autor,
    _carregar_processos,
    _reus_joined,
    carregar_processos_base,
)

# --------------------------------------------------------------------------
# Preenchimento de lacuna entre fontes
# --------------------------------------------------------------------------
# A prioridade é `pasta base > processos_online > embutido`, e por anos o
# primeiro vencia o REGISTRO INTEIRO. O efeito era um processo "parar de
# preencher": o dado existe dentro do programa, mas o registro que ganhou vinha
# sem ele e o de baixo era descartado junto.
#
# Medido em 31/08/2026 nesta base: 324 processos da pasta base e 19 do cache
# online sombreavam o embutido perdendo campo — quase todos `valor_causa`,
# alguns `classe` e `comarca`. Nenhum era MELHOR que o embutido em outro campo,
# ou seja o descarte era perda pura.
#
# A prioridade NÃO muda: dado preenchido do vencedor sempre vence. Só a lacuna é
# preenchida, de cima para baixo.

# Campos que só fazem sentido JUNTOS. Os três últimos são derivados da lista de
# eventos (conferido num metadados.json real: `total_eventos == len(eventos)`),
# então preencher `ultima_movimentacao` de outra fonte e deixar `eventos` vazio
# mostraria data de movimentação num processo de timeline vazia. Ou vem o grupo
# todo, ou nenhum. O grupo só vale quando o LÍDER existe no destino: no
# `ResumoProcesso` não há `eventos`, e lá `ultima_movimentacao` é só uma coluna.
_GRUPOS_ACOPLADOS: tuple[tuple[str, ...], ...] = (
    ("eventos", "total_eventos", "primeira_movimentacao", "ultima_movimentacao"),
)

# Campos que dizem QUEM É / DE ONDE VEIO o registro, não o que ele contém. Nunca
# são preenchidos de uma reserva, por segurança e não por economia:
#   - `origem` é o arquivo que `carregar_processo_completo` vai abrir. Herdá-lo
#     do perdedor faria o perdedor virar o vencedor, silenciosamente.
#   - `fonte` ("base"/"online"/"embutido") passaria a mentir de onde o registro é.
#   - `numero` é a chave da disputa: se estivesse vazio não haveria disputa.
#   - `origens_reserva` é a lista de perdedores, montada pela mescla.
#   - `arquivos_origem` é procedência: são os PDFs de onde ESTE registro foi
#     lido, e apontar os do outro atribuiria arquivo que não é dele.
_NAO_PREENCHER = frozenset({
    "numero", "fonte", "origem", "origens_reserva", "arquivos_origem",
    # `atualizado_em` diz que ESTE registro foi atualizado pelo usuário; herdá-lo
    # de uma reserva faria um registro que ninguém mandou atualizar passar a
    # ganhar da pasta base para sempre.
    "atualizado_em",
})


def prefere_atualizado(novo: object, atual: object) -> bool:
    """True se `novo` deve tomar o lugar de `atual` APESAR da prioridade da fonte.

    A prioridade normal é `pasta base > processos_online > embutido`. Ela é boa
    como padrão — a pasta base é o que o escritório curou — mas atrapalha no caso
    em que a pessoa olha a tela, vê um dado errado e manda consultar o tribunal:
    o resultado cai em processos_online e perde para o registro antigo. Medido
    num cenário mínimo: com "CLASSE ERRADA" na pasta base, ao reabrir o app a
    lista e a petição voltavam a mostrar a errada.

    A marca ``atualizado_em`` (ver models.Processo) inverte a prioridade só para
    o número que a pessoa mandou atualizar. Comparação de string porque o formato
    é ISO, que ordena por ordem alfabética; vazio perde de qualquer data, então
    registro sem marca nunca rouba o lugar de ninguém. Empate mantém o vencedor
    atual, ou seja a prioridade de fonte continua desempatando."""
    return (str(getattr(novo, "atualizado_em", "") or "")
            > str(getattr(atual, "atualizado_em", "") or ""))


def _vazio(valor: object) -> bool:
    """"Não temos esse dado" — o que autoriza puxar de fonte de menor prioridade.

    `0` NÃO conta como vazio por si: `total_eventos` zero é consistente com
    `eventos` vazio, e quem decide esse par é o grupo acoplado. Tratar 0 como
    lacuna faria um processo sem movimentação nenhuma herdar o total de outro."""
    if valor is None:
        return True
    if isinstance(valor, str):
        return not valor.strip()
    if isinstance(valor, (list, tuple, dict, set)):
        return not valor
    return False


def _da_reserva(reserva: object, campo: str) -> object:
    """Valor de `campo` na reserva, que pode ser dataclass ou dict.

    Dict porque o embutido chega como dict cru do SQLite
    (``base_embutida.ler_processo``), sem custo de virar ``Processo``."""
    if isinstance(reserva, dict):
        return reserva.get(campo)
    return getattr(reserva, campo, None)


def _plano(tipo: type) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """(campos livres, grupos acoplados) para um tipo, calculado uma vez.

    Memoizado porque isto roda no caminho de ABERTURA: a mescla preenche lacuna
    de ~3.800 duplicatas nesta base. Medido sobre as três fontes reais: a mescla
    saiu de 3,0 ms (o loop antigo, que descartava a duplicata) para 33 ms; com o
    plano em cache e sem os campos de ``_NAO_PREENCHER``, 19,4 ms. É custo real e
    conhecido, pago numa THREAD (`_iniciar_carregamento_pesquisa`) e não no
    desenho da janela, contra 1,54 s de abertura total.
    Um tipo por processo, então o dict não cresce."""
    plano = _PLANOS.get(tipo)
    if plano is None:
        nomes = tuple(f.name for f in _campos_de(tipo))
        grupos = tuple(g for g in _GRUPOS_ACOPLADOS if g[0] in nomes)
        fora = {c for g in grupos for c in g} | _NAO_PREENCHER
        plano = (tuple(n for n in nomes if n not in fora), grupos)
        _PLANOS[tipo] = plano
    return plano


_PLANOS: dict[type, tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]] = {}


def preencher_lacunas(vencedor, *reservas):
    """Copia para `vencedor` só os campos que ele NÃO tem, na ordem das reservas.

    Muta e devolve `vencedor` — em todos os chamadores ele já é um objeto
    recém-montado a partir do disco, não algo compartilhado.

    Serve para ``Processo`` e para ``ResumoProcesso``: percorre os campos do
    próprio destino, então cada um preenche só o que tem."""
    reservas = tuple(r for r in reservas if r is not None)
    if not reservas:
        return vencedor
    livres, grupos = _plano(type(vencedor))
    for reserva in reservas:
        for grupo in grupos:
            lider = grupo[0]
            if not _vazio(getattr(vencedor, lider, None)):
                continue
            if _vazio(_da_reserva(reserva, lider)):
                continue
            for campo in grupo:
                valor = _da_reserva(reserva, campo)
                if not _vazio(valor):
                    setattr(vencedor, campo, valor)
        for campo in livres:
            if not _vazio(getattr(vencedor, campo, None)):
                continue
            valor = _da_reserva(reserva, campo)
            if not _vazio(valor):
                setattr(vencedor, campo, valor)
    return vencedor


def mesclar_processos_por_prioridade(*fontes: list[Processo]) -> list[Processo]:
    """Mescla fontes preservando a prioridade da esquerda para a direita.

    Duplicata não é mais descartada inteira: o que ela tem e o vencedor não tem
    é aproveitado (ver ``preencher_lacunas``). E um registro que o usuário mandou
    atualizar toma o lugar do vencedor mesmo vindo de fonte de menor prioridade
    (ver ``prefere_atualizado``).

    Processos com número vazio não são deduplicados, pois não há chave confiável.
    """
    resultados: list[Processo] = []
    vencedor_de: dict[str, Processo] = {}
    posicao_de: dict[str, int] = {}
    for fonte in fontes:
        for proc in fonte:
            numero = (proc.numero or "").strip()
            if numero:
                anterior = vencedor_de.get(numero)
                if anterior is not None:
                    if prefere_atualizado(proc, anterior):
                        # Troca de vencedor no lugar: o antigo vira reserva.
                        preencher_lacunas(proc, anterior)
                        resultados[posicao_de[numero]] = proc
                        vencedor_de[numero] = proc
                    else:
                        preencher_lacunas(anterior, proc)
                    continue
                vencedor_de[numero] = proc
                posicao_de[numero] = len(resultados)
            resultados.append(proc)
    return resultados


def carregar_processos_pesquisa(
    pasta_base: Path,
    processos_online_dir: Path,
    processos_base_json: Path,
) -> list[Processo]:
    """Carrega base curada, cache online e snapshot embutido, nesta prioridade."""
    pasta_base = Path(pasta_base).expanduser()
    base = _carregar_processos(pasta_base) if pasta_base.is_dir() else []
    online = _carregar_processos(processos_online_dir)
    embutidos = carregar_processos_base(processos_base_json)
    return mesclar_processos_por_prioridade(base, online, embutidos)


# As funções abaixo aceitam tanto Processo (que tem `.partes`, exige derivar
# autor/réu/prazo) quanto ResumoProcesso (que já traz `.autor`/`.reu`/`.prazo_fim`
# prontos). O duck-typing por `hasattr(x, "partes")` evita importar
# ResumoProcesso aqui (e o acoplamento/ciclo que isso traria).

def _autor_de(x) -> str:
    return _autor(x) if hasattr(x, "partes") else (x.autor or "")


def _reu_de(x) -> str:
    return _reus_joined(x) if hasattr(x, "partes") else (x.reu or "")


def _prazo_de(x) -> str:
    if hasattr(x, "partes"):
        return (x.prazos_abertos[0].fim or "?") if x.prazos_abertos else ""
    return x.prazo_fim or ""


def texto_filtro_processo(proc) -> str:
    return " ".join(
        (
            proc.numero or "",
            proc.classe or "",
            _autor_de(proc),
            _reu_de(proc),
        )
    ).lower()


def filtrar_processos(processos: list, filtro: str) -> list:
    termo = (filtro or "").lower().strip()
    if not termo:
        return list(processos)
    return [proc for proc in processos if termo in texto_filtro_processo(proc)]


def indexar_para_filtro(processos: list) -> list[str]:
    """Pré-computa o texto de busca de cada processo, UMA vez.

    ``filtrar_processos`` remonta ``texto_filtro_processo`` (4 concatenações +
    ``.lower()``) para a lista inteira a cada tecla digitada. Com ~3.200
    processos isso é ~13 mil operações de string por caractere — imperceptível
    numa máquina rápida, mas visível num i3. O índice é calculado quando a lista
    é carregada e reusado por ``filtrar_processos_indexado``."""
    return [texto_filtro_processo(p) for p in processos]


def filtrar_processos_indexado(processos: list, textos: list[str], filtro: str) -> list:
    """Como ``filtrar_processos``, usando o índice de ``indexar_para_filtro``.

    Se o índice estiver dessincronizado da lista (tamanhos diferentes), recalcula
    na hora em vez de devolver resultado errado."""
    termo = (filtro or "").lower().strip()
    if not termo:
        return list(processos)
    if len(textos) != len(processos):
        return filtrar_processos(processos, filtro)
    return [proc for proc, texto in zip(processos, textos) if termo in texto]


def linhas_tree_processos(processos: list) -> list[tuple[str, tuple[str, ...], str]]:
    """Monta os dados consumidos pela Treeview sem tocar em widgets.

    Aceita Processo ou ResumoProcesso (mesmos campos de exibição)."""
    linhas: list[tuple[str, tuple[str, ...], str]] = []
    for idx, proc in enumerate(processos):
        tag = "oddrow" if idx % 2 else "evenrow"
        linhas.append((
            proc.numero,
            (
                proc.numero,
                proc.classe,
                _autor_de(proc),
                _reu_de(proc),
                proc.valor_causa,
                proc.ultima_movimentacao[:30],
                _prazo_de(proc),
            ),
            tag,
        ))
    return linhas
