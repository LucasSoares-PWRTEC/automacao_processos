# -*- coding: utf-8 -*-
"""Estruturas de dados (dataclasses) do domínio processual."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Advogado:
    nome: str = ""
    oab: str = ""   # ex.: "MG104784", "SP423256B"
    uf: str = ""    # ex.: "MG"


@dataclass
class Parte:
    papel: str = ""             # AUTOR / RÉU / EXEQUENTE / EXECUTADO / REQUERENTE / REQUERIDO / ...
    nome: str = ""
    documento: str = ""         # CNPJ/CPF (com máscara preservada)
    tipo: str = ""              # "Pessoa Jurídica" / "Pessoa Física"
    advogados: list[Advogado] = field(default_factory=list)


@dataclass
class Contexto:
    """Gênero/número derivados das partes processuais, usado para flexão."""
    genero_autor: str = "m"   # "m" | "f"
    numero_autor: str = "s"   # "s" | "p"
    genero_reu: str = "m"
    numero_reu: str = "s"
    genero_juiz: str = "m"    # "m" | "f" - gênero do juiz/magistrado


@dataclass
class Evento:
    numero: str = ""
    data: str = ""
    hora: str = ""
    descricao: str = ""
    usuario: str = ""
    documentos: list[str] = field(default_factory=list)
    refer_evento: str = ""              # "Refer. ao Evento N"  (apenas N — pode ter vírgulas)
    prazo_dias: int | None = None
    prazo_status: str = ""              # ABERTO / FECHADO
    prazo_inicio: str = ""              # DD/MM/AAAA
    prazo_fim: str = ""                 # DD/MM/AAAA


@dataclass
class Prazo:
    parte_nome: str = ""
    parte_papel: str = ""
    dias: int = 0
    inicio: str = ""
    fim: str = ""
    status: str = ""
    evento_origem: str = ""


@dataclass
class AtoDocumento:
    tipo: str = ""                  # "ATO ORDINATÓRIO" / "DESPACHO" / "DECISÃO" / "SENTENÇA"
    classe_acao: str = ""           # "PROCEDIMENTO COMUM CÍVEL"
    teor: str = ""                  # texto do corpo do ato (truncado a ~4000 chars)
    signatario: str = ""
    data_assinatura: str = ""
    codigo_verificador: str = ""    # ex.: "610009680897v1"
    codigo_crc: str = ""            # ex.: "9c4507c5"
    local: str = ""                 # cidade
    arquivo: str = ""               # nome do PDF de origem


@dataclass
class Processo:
    # --- cabeçalho ---
    numero: str = ""
    classe: str = ""
    assunto: str = ""
    competencia: str = ""
    data_autuacao: str = ""
    situacao: str = ""
    orgao_julgador: str = ""
    juiz: str = ""
    # --- identificação extra ---
    tribunal: str = ""              # "TJSP" / "TJMG" / "TJRJ"
    uf: str = ""                    # "SP" / "MG" / "RJ"
    comarca: str = ""
    vara: str = ""
    foro_endereco: str = ""         # endereço/CEP/fone/email da vara (dos atos)
    valor_causa: str = ""           # R$ 44.924,15
    chave_consulta: str = ""        # do extrato de distribuição
    tags: list[str] = field(default_factory=list)
    # --- conteúdo ---
    partes: list[Parte] = field(default_factory=list)
    eventos: list[Evento] = field(default_factory=list)
    prazos_abertos: list[Prazo] = field(default_factory=list)
    atos: list[AtoDocumento] = field(default_factory=list)
    # --- agregados (derivados) ---
    total_eventos: int = 0
    primeira_movimentacao: str = ""     # "DD/MM/AAAA"
    ultima_movimentacao: str = ""       # "DD/MM/AAAA — descrição curta"
    # --- arquivos ---
    arquivos_origem: list[str] = field(default_factory=list)

