# -*- coding: utf-8 -*-
"""Índice de RESUMO dos processos já no sistema + carga preguiçosa do completo.

A lista da aba Pesquisa só mostra 7 colunas de resumo e o filtro só usa
nº/classe/autor/réu. Ler e parsear TODOS os ``metadados.json`` (base + cache
online) e o snapshot embutido a cada abertura custa vários segundos. Aqui
mantemos um índice pequeno (só os campos de exibição/filtro) persistido em disco
e atualizado incrementalmente por ``mtime`` — varrer diretórios + ``stat`` é
barato; só arquivos novos/alterados são reparseados. O ``Processo`` completo
(com eventos/atos/partes) é carregado sob demanda ao selecionar a linha.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import base_embutida
from .models import Processo
from .organizacao import (
    _dict_para_processo,
    _juntar_nomes,
    carregar_processos_base,
)
from .padroes import PAPEIS_ATIVO_UP as _ATIVO_UP, PAPEIS_PASSIVO_UP as _PASSIVO_UP
from .perf import marca as _perf
# `pesquisa` só importa models/organizacao, que já estão aqui — não há ciclo e
# nada novo entra no caminho de abertura.
from .pesquisa import preencher_lacunas

INDICE_VERSAO = 1
ORIGEM_EMBUTIDO = "embutido"


def caminho_sqlite(processos_base: Path) -> Path:
    """A base SQLite irmã do snapshot .json.gz (mesma pasta, sufixo .sqlite).

    Deriva do caminho do .gz em vez de ser uma constante própria para que os
    testes (e o `--destino` do gerador) consigam apontar para um tmp_path."""
    p = Path(processos_base)
    nome = p.name
    for sufixo in (".json.gz", ".json"):
        if nome.endswith(sufixo):
            nome = nome[: -len(sufixo)]
            break
    else:
        nome = p.stem
    return p.with_name(nome + ".sqlite")

@dataclass
class ResumoProcesso:
    """Só os campos exibidos na lista e usados no filtro — sem eventos/atos."""
    numero: str = ""
    classe: str = ""
    autor: str = ""
    reu: str = ""
    valor_causa: str = ""
    ultima_movimentacao: str = ""
    prazo_fim: str = ""
    comarca: str = ""        # usado por aprender_comarcas (CNJ-origem → comarca)
    tags: list[str] = field(default_factory=list)
    fonte: str = ""          # "base" | "online" | "embutido"
    origem: str = ""         # caminho do metadados.json, ou ORIGEM_EMBUTIDO
    # Origens das fontes que PERDERAM a disputa por este número, em ordem de
    # prioridade. Existem para `carregar_processo_completo` saber de onde puxar o
    # campo que o vencedor não tem (ver pesquisa.preencher_lacunas): sem isto o
    # dado estaria no programa e continuaria não aparecendo na petição.
    #
    # Preenchida na MESCLA, não na leitura do arquivo — é propriedade da disputa,
    # não do registro. Por isso o que vai para o índice em disco é sempre `()`:
    # o `asdict` acontece antes da mescla. Um índice de versão anterior, sem esta
    # chave, continua carregando (o default cobre), então INDICE_VERSAO não muda.
    origens_reserva: tuple[str, ...] = ()


def _nomes_por_papel(partes: list[dict], papeis: set[str]) -> str:
    nomes = [p.get("nome", "") for p in partes if (p.get("papel") or "").upper() in papeis]
    return _juntar_nomes(nomes)


def _ultima_mov_str(valor) -> str:
    """`ultima_movimentacao` pode vir como str (metadados.json) ou dict
    {data, descricao} (saída do scraper). Normaliza para str."""
    if isinstance(valor, dict):
        data, desc = valor.get("data", ""), valor.get("descricao", "")
        return f"{data} — {desc}".strip(" —")
    return valor or ""


def _resumo_de_dict(d: dict, fonte: str, origem: str) -> ResumoProcesso:
    """Extrai o resumo direto do dict — sem construir eventos/atos (mais rápido)."""
    partes = d.get("partes") or []
    prazos = d.get("prazos_abertos") or []
    prazo_fim = ""
    if prazos:
        prazo_fim = (prazos[0].get("fim") or "") or "?"
    return ResumoProcesso(
        numero=d.get("numero", "") or "",
        classe=d.get("classe", "") or "",
        autor=_nomes_por_papel(partes, _ATIVO_UP),
        reu=_nomes_por_papel(partes, _PASSIVO_UP),
        valor_causa=d.get("valor_causa", "") or "",
        ultima_movimentacao=_ultima_mov_str(d.get("ultima_movimentacao")),
        prazo_fim=prazo_fim,
        comarca=d.get("comarca", "") or "",
        tags=list(d.get("tags") or []),
        fonte=fonte,
        origem=origem,
    )


def _resumo_de_processo(proc: Processo, fonte: str, origem: str) -> ResumoProcesso:
    """Resumo a partir de um Processo já em memória (ex.: busca online)."""
    return _resumo_de_dict(asdict(proc), fonte, origem)


# --------------------------------------------------------------------------
# Persistência do índice
# --------------------------------------------------------------------------

def _ler_indice(indice_path: Path) -> dict:
    try:
        dados = json.loads(indice_path.read_text(encoding="utf-8"))
        if isinstance(dados, dict) and dados.get("versao") == INDICE_VERSAO:
            return dados
    except Exception:
        pass
    return {"versao": INDICE_VERSAO, "gz_mtime": None, "disco": {}, "embutidos": []}


def _salvar_indice(indice_path: Path, indice: dict) -> None:
    """Gravação atômica (tmp + os.replace); falha nunca derruba a carga."""
    try:
        indice_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = indice_path.with_suffix(indice_path.suffix + ".tmp")
        tmp.write_text(json.dumps(indice, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, indice_path)
    except Exception:
        pass


def _atualizar_fonte_disco(
    pasta: Path, fonte: str, disco_prev: dict, disco_novo: dict,
) -> list[ResumoProcesso]:
    """Varre `pasta` (subpasta/metadados.json), reaproveitando do índice os
    inalterados (por mtime) e reparseando só os novos/alterados. Preenche
    `disco_novo[path] = {mtime, resumo}` e devolve os resumos desta fonte."""
    resumos: list[ResumoProcesso] = []
    if not pasta.is_dir():
        return resumos
    try:
        entradas = sorted(os.scandir(pasta), key=lambda e: e.name)
    except OSError:
        return resumos
    for entry in entradas:
        if not entry.is_dir():
            continue
        meta = os.path.join(entry.path, "metadados.json")
        try:
            mtime = os.stat(meta).st_mtime
        except OSError:
            continue  # sem metadados.json
        prev = disco_prev.get(meta)
        if prev and prev.get("mtime") == mtime and prev.get("resumo"):
            resumo = ResumoProcesso(**prev["resumo"])
        else:
            try:
                d = json.loads(Path(meta).read_text(encoding="utf-8"))
            except Exception:
                continue
            resumo = _resumo_de_dict(d, fonte, meta)
        disco_novo[meta] = {"mtime": mtime, "resumo": asdict(resumo)}
        resumos.append(resumo)
    return resumos


def _resumos_embutidos(
    processos_base: Path, indice: dict
) -> tuple[list[ResumoProcesso], dict]:
    """Resumos do snapshot embutido na release.

    Devolve ``(resumos, meta)``, onde `meta` é o que o índice em disco precisa
    guardar para reaproveitar na próxima abertura — vazio no caminho SQLite,
    porque lá não há nada a cachear.

    Caminho normal: lê a tabela `resumos` do SQLite — milhares de linhas curtas,
    sem tocar no JSON completo de nenhum processo.

    Fallback (rodando do código-fonte, com o .sqlite ainda não gerado): parseia o
    .json.gz como antes e reaproveita o resultado pelo `mtime`, que do fonte é
    estável. No .exe esse cache nunca acertava, porque o onefile extrai o arquivo
    em _MEIPASS com timestamp novo a cada abertura — era o custo de ~25 s por
    abertura num i3 que motivou a migração para SQLite.
    """
    caminho = Path(processos_base)
    sqlite_path = caminho_sqlite(caminho)
    if base_embutida.disponivel(sqlite_path):
        # `fonte`/`origem` são constantes nesta fonte — não vale gravar 3.100
        # cópias da mesma string; são preenchidas na leitura.
        return [
            ResumoProcesso(**d, fonte=ORIGEM_EMBUTIDO, origem=ORIGEM_EMBUTIDO)
            for d in base_embutida.ler_resumos(sqlite_path)
        ], {}

    try:
        gz_mtime = os.stat(caminho).st_mtime
    except OSError:
        gz_mtime = None
    if gz_mtime is not None and gz_mtime == indice.get("gz_mtime") and indice.get("embutidos"):
        resumos = [ResumoProcesso(**r) for r in indice["embutidos"]]
    else:
        resumos = [
            _resumo_de_processo(p, ORIGEM_EMBUTIDO, ORIGEM_EMBUTIDO)
            for p in carregar_processos_base(caminho)
        ]
    return resumos, {
        "gz_mtime": gz_mtime,
        "embutidos": [asdict(r) for r in resumos],
    }


def carregar_resumos_pesquisa(
    pasta_base: Path,
    processos_online_dir: Path,
    processos_base_json: Path,
    indice_path: Path,
) -> list[ResumoProcesso]:
    """Resumos das três fontes (base > online > embutido), com dedup por número.

    Usa o índice em `indice_path`: inalterados vêm do índice; novos/alterados são
    reparseados; o índice é reescrito ao final SÓ SE algo mudou. Nunca levanta —
    na pior hipótese reconstrói do zero (custo da carga antiga)."""
    pasta_base = Path(pasta_base).expanduser()
    indice = _ler_indice(indice_path)
    disco_prev: dict = indice.get("disco") or {}
    disco_novo: dict = {}

    base = _atualizar_fonte_disco(pasta_base, "base", disco_prev, disco_novo)
    online = _atualizar_fonte_disco(processos_online_dir, "online", disco_prev, disco_novo)
    _perf(f"resumos:disco({len(base)}+{len(online)})")

    embutidos, meta_embutidos = _resumos_embutidos(Path(processos_base_json), indice)
    _perf(f"resumos:embutidos({len(embutidos)})")

    # Só reescreve o índice se algo mudou. Antes ele era regravado em TODA
    # abertura (4,9 MB de JSON) mesmo sem nenhum processo novo.
    novo = {"versao": INDICE_VERSAO, "disco": disco_novo, **meta_embutidos}
    if novo != {k: indice.get(k) for k in novo}:
        _salvar_indice(indice_path, novo)
        _perf("resumos:indice_salvo")

    # Mescla por prioridade, dedup por número (mesma regra de
    # mesclar_processos_por_prioridade, mas sobre resumos).
    #
    # A duplicata não é mais descartada inteira: o que ela tem e o vencedor não
    # tem preenche a lacuna do vencedor (colunas da lista), e a origem dela fica
    # guardada em `origens_reserva` para o Processo COMPLETO poder fazer o mesmo
    # no clique — que é onde a petição é preenchida.
    resultado: list[ResumoProcesso] = []
    vencedor_de: dict[str, ResumoProcesso] = {}
    for fonte in (base, online, embutidos):
        for r in fonte:
            numero = (r.numero or "").strip()
            if numero:
                anterior = vencedor_de.get(numero)
                if anterior is not None:
                    preencher_lacunas(anterior, r)
                    if r.origem:
                        anterior.origens_reserva = (
                            *anterior.origens_reserva, r.origem,
                        )
                    continue
                vencedor_de[numero] = r
            resultado.append(r)
    return resultado


# --------------------------------------------------------------------------
# Carga preguiçosa do Processo completo
# --------------------------------------------------------------------------

# Fallback do .json.gz: mapa {numero -> Processo} do snapshot inteiro. Só é
# construído quando NÃO existe a base SQLite (ou seja, rodando do código-fonte
# sem ter gerado o artefato) — custa ~8 s e retém ~104 MB. Com o SQLite, cada
# clique lê um registro só e nada fica retido.
_embutido_cache: dict[str, object] = {"path": None, "mtime": None, "mapa": {}}


def _mapa_embutido(processos_base_json: Path) -> dict:
    caminho = Path(processos_base_json)
    try:
        mtime = os.stat(caminho).st_mtime
    except OSError:
        return {}
    if _embutido_cache["path"] == str(caminho) and _embutido_cache["mtime"] == mtime:
        return _embutido_cache["mapa"]  # type: ignore[return-value]
    mapa = {
        (p.numero or "").strip(): p
        for p in carregar_processos_base(caminho)
        if (p.numero or "").strip()
    }
    _embutido_cache.update(path=str(caminho), mtime=mtime, mapa=mapa)
    return mapa


def _dict_de_origem(
    origem: str, numero: str, processos_base_json: Path | None,
) -> dict | None:
    """O registro CRU (dict) de uma origem: metadados.json ou base embutida.

    Dict, não ``Processo``: o preenchimento de lacuna lê a reserva campo a campo
    e não precisa dela montada — e o embutido já sai como dict do SQLite."""
    if origem and origem != ORIGEM_EMBUTIDO:
        try:
            return json.loads(Path(origem).read_text(encoding="utf-8"))
        except Exception:
            return None
    if processos_base_json is None:
        return None
    sqlite_path = caminho_sqlite(Path(processos_base_json))
    if base_embutida.disponivel(sqlite_path):
        return base_embutida.ler_processo(sqlite_path, numero)
    proc = _mapa_embutido(processos_base_json).get(numero)
    return asdict(proc) if proc is not None else None


def carregar_processo_completo(
    resumo: ResumoProcesso,
    processos_base_json: Path | None = None,
) -> Processo | None:
    """Carrega o Processo completo referente a `resumo`, sob demanda.

    Fontes em disco: lê o metadados.json apontado por `resumo.origem`.
    Embutido: busca o registro POR NÚMERO na base SQLite (descomprime um só);
    sem ela, cai no snapshot .json.gz inteiro.

    E, se o vencedor vier com lacuna, puxa o campo que falta das fontes que
    perderam a disputa (``resumo.origens_reserva``). É AQUI que isso mais
    importa: este é o Processo que preenche a petição — era o "esse processo
    parou de preencher" quando o registro vencedor vinha sem `valor_causa`.

    CUSTO, medido nesta base: 1,31 ms por clique na média de 200 processos com
    reserva (uma leitura extra de metadados.json, ou um registro do SQLite).
    Não há guarda de "só leia se houver lacuna" porque ela não guardaria nada:
    medido, 3.108 de 3.108 processos têm ALGUM campo vazio — `chave_consulta`,
    `atos`, `prazos_abertos` e `juiz` são vazios em quase todo registro, em todas
    as fontes. A guarda dispararia 100% das vezes e só somaria uma varredura de
    campos a cada clique."""
    numero = (resumo.numero or "").strip()
    bruto = _dict_de_origem(resumo.origem, numero, processos_base_json)
    if bruto is None:
        return None
    try:
        proc = _dict_para_processo(bruto)
    except Exception:
        return None

    for origem in tuple(resumo.origens_reserva or ()):
        d = _dict_de_origem(origem, numero, processos_base_json)
        if d is not None:
            preencher_lacunas(proc, d)
    return proc
