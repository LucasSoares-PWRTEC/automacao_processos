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

from .models import Processo
from .organizacao import (
    _dict_para_processo,
    _juntar_nomes,
    carregar_processos_base,
)
from .padroes import PAPEIS_ATIVO, PAPEIS_PASSIVO

INDICE_VERSAO = 1
ORIGEM_EMBUTIDO = "embutido"

_ATIVO_UP = {p.upper() for p in PAPEIS_ATIVO}
_PASSIVO_UP = {p.upper() for p in PAPEIS_PASSIVO}


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


def carregar_resumos_pesquisa(
    pasta_base: Path,
    processos_online_dir: Path,
    processos_base_json: Path,
    indice_path: Path,
) -> list[ResumoProcesso]:
    """Resumos das três fontes (base > online > embutido), com dedup por número.

    Usa o índice em `indice_path`: inalterados vêm do índice; novos/alterados são
    reparseados; o índice é reescrito ao final. Nunca levanta — na pior hipótese
    reconstrói do zero (custo da carga antiga)."""
    pasta_base = Path(pasta_base).expanduser()
    indice = _ler_indice(indice_path)
    disco_prev: dict = indice.get("disco") or {}
    disco_novo: dict = {}

    base = _atualizar_fonte_disco(pasta_base, "base", disco_prev, disco_novo)
    online = _atualizar_fonte_disco(processos_online_dir, "online", disco_prev, disco_novo)

    # Embutido: reusa por mtime do .gz (arquivo único).
    gz_mtime = None
    try:
        gz_mtime = os.stat(processos_base_json).st_mtime
    except OSError:
        pass
    if gz_mtime is not None and gz_mtime == indice.get("gz_mtime") and indice.get("embutidos"):
        embutidos = [ResumoProcesso(**r) for r in indice["embutidos"]]
    else:
        embutidos = [
            _resumo_de_processo(p, ORIGEM_EMBUTIDO, ORIGEM_EMBUTIDO)
            for p in carregar_processos_base(Path(processos_base_json))
        ]

    _salvar_indice(indice_path, {
        "versao": INDICE_VERSAO,
        "gz_mtime": gz_mtime,
        "disco": disco_novo,
        "embutidos": [asdict(r) for r in embutidos],
    })

    # Mescla por prioridade, dedup por número (mesma regra de
    # mesclar_processos_por_prioridade, mas sobre resumos).
    resultado: list[ResumoProcesso] = []
    vistos: set[str] = set()
    for fonte in (base, online, embutidos):
        for r in fonte:
            numero = (r.numero or "").strip()
            if numero:
                if numero in vistos:
                    continue
                vistos.add(numero)
            resultado.append(r)
    return resultado


# --------------------------------------------------------------------------
# Carga preguiçosa do Processo completo
# --------------------------------------------------------------------------

# Cache do snapshot embutido (numero -> dict), reconstruído se o .gz mudar.
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


def carregar_processo_completo(
    resumo: ResumoProcesso,
    processos_base_json: Path | None = None,
) -> Processo | None:
    """Carrega o Processo completo referente a `resumo`, sob demanda.

    Fontes em disco: lê o metadados.json apontado por `resumo.origem`.
    Embutido: resolve pelo snapshot .gz (parseado uma vez e cacheado)."""
    if resumo.origem and resumo.origem != ORIGEM_EMBUTIDO:
        try:
            d = json.loads(Path(resumo.origem).read_text(encoding="utf-8"))
            return _dict_para_processo(d)
        except Exception:
            return None
    if processos_base_json is not None:
        return _mapa_embutido(processos_base_json).get((resumo.numero or "").strip())
    return None
