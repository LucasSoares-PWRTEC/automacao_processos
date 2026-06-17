# -*- coding: utf-8 -*-
"""Helpers de CNJ, OAB e órgão julgador."""

import organizar_processos as op


def test_re_numero_cnj_encontra_padrao():
    texto = "Processo nº 4004459-04.2026.8.26.0248 distribuído"
    m = op.RE_NUMERO_CNJ.search(texto)
    assert m is not None
    assert m.group(0) == "4004459-04.2026.8.26.0248"


def test_inferir_tribunal_sp():
    assert op._inferir_tribunal("4004459-04.2026.8.26.0248") == ("TJSP", "SP")


def test_inferir_tribunal_mg():
    assert op._inferir_tribunal("4004459-04.2026.8.13.0024") == ("TJMG", "MG")


def test_inferir_tribunal_desconhecido():
    # Justiça federal (J=4) não está no mapa estadual -> vazio.
    assert op._inferir_tribunal("0001234-56.2026.4.03.6100") == ("", "")


def test_inferir_tribunal_sem_match():
    assert op._inferir_tribunal("texto sem cnj") == ("", "")


def test_uf_do_cnj():
    assert op.uf_do_cnj("4004459-04.2026.8.26.0248") == "SP"
    assert op.uf_do_cnj("4004459-04.2026.8.13.0024") == "MG"
    assert op.uf_do_cnj("") == ""
    assert op.uf_do_cnj("sem cnj") == ""


def test_oab_completa():
    assert op._oab_completa("sp", "346.433") == "OAB/SP 346.433"
    assert op._oab_completa("SP", "346.433") == "OAB/SP 346.433"


def test_oab_completa_parcial():
    assert op._oab_completa("", "346.433") == "346.433"
    assert op._oab_completa("SP", "") == "OAB/SP"
    assert op._oab_completa("", "") == ""


def test_municipio_de_comarca():
    assert op._municipio_de_comarca("Comarca de Carapicuíba") == "Carapicuíba"
    assert op._municipio_de_comarca("Foro Regional de Pinheiros") == "Pinheiros"
    assert op._municipio_de_comarca("São Paulo") == "São Paulo"
    assert op._municipio_de_comarca("") == ""
    # Formato do e-SAJ: "Foro Regional <id> - <Nome>" → nome da regional.
    assert op._municipio_de_comarca("Foro Regional VI - Penha de França") == "Penha de França"
    assert op._municipio_de_comarca("Foro de Indaiatuba") == "Indaiatuba"


def test_municipio_de_processo_fallback_orgao_julgador():
    from organizador.models import Processo
    # Comarca explícita.
    assert op._municipio_de_processo(Processo(comarca="Comarca de Contagem")) == "Contagem"
    # Sem comarca: deriva do órgão julgador.
    p = Processo(comarca="", orgao_julgador="2ª Vara Cível da Comarca de Carapicuíba")
    assert op._municipio_de_processo(p) == "Carapicuíba"
    # Sem nenhuma fonte (e sem cache): vazio (campo fica manual na GUI).
    assert op._municipio_de_processo(Processo(comarca="", orgao_julgador="")) == ""


def test_limpar_secretaria_remove_juizo_titular():
    f = op._limpar_secretaria
    assert f("Juízo Titular II - 2ª Vara Cível") == "2ª Vara Cível"
    assert f("JUÍZO TITULAR II - 2ª Vara Cível") == "2ª Vara Cível"
    # Sem o prefixo, mantém intacto.
    assert f("2ª Vara Cível") == "2ª Vara Cível"
    assert f("") == ""


def test_limpar_secretaria_remove_comarca_grudada():
    """O modelo já acrescenta 'DA COMARCA DE <comarca>/<UF>', então o sufixo de
    localidade que o PJe gruda na vara precisa sair para não duplicar."""
    f = op._limpar_secretaria
    assert f("4ª Vara Cível da Comarca de Contagem") == "4ª Vara Cível"
    assert f("Juízo Titular III - 1ª Vara Cível da Comarca de X") == "1ª Vara Cível"
    assert f("Vara Única da Comarca de Nova Lima") == "Vara Única"
    assert f("1ª Vara da Subseção Judiciária de São Paulo") == "1ª Vara"
    assert f("Vara do Foro Regional de Santo Amaro") == "Vara"
    # Não confunde "de Família/Fazenda" com localidade.
    assert f("2ª Vara de Família") == "2ª Vara de Família"
    assert f("3ª Vara da Fazenda Pública") == "3ª Vara da Fazenda Pública"


def test_origem_cnj_extrai_justica_tribunal_foro():
    from organizador import parsing
    assert parsing._origem_cnj("4004459-04.2026.8.26.0248") == "8.26.0248"
    assert parsing._origem_cnj("sem número") == ""


def _isola_cache(monkeypatch, tmp_path):
    """Aponta o cache de comarcas para um arquivo temporário e zera o estado."""
    from organizador import parsing
    monkeypatch.setattr(parsing, "COMARCAS_CACHE_PATH", tmp_path / "comarcas.json")
    monkeypatch.setattr(parsing, "_cache_comarcas", None)


def test_cache_comarcas_aprende_persiste_e_deriva(monkeypatch, tmp_path):
    from organizador import parsing
    from organizador.models import Processo
    _isola_cache(monkeypatch, tmp_path)

    hist = [
        Processo(numero="1102493-63.2022.8.26.0100", comarca="Foro Regional VI - Penha de França"),
        Processo(numero="4004459-04.2026.8.26.0248", comarca="Comarca de Carapicuíba"),
    ]
    assert parsing.aprender_comarcas(hist) == 2
    assert (tmp_path / "comarcas.json").exists()  # persistiu

    # Processo NOVO, mesmo foro (0248), SEM comarca → resolve pelo número.
    novo = Processo(numero="5005000-00.2027.8.26.0248", comarca="", orgao_julgador="")
    assert parsing._municipio_de_processo(novo) == "Carapicuíba"
    # Foro desconhecido → vazio.
    outro = Processo(numero="9009000-00.2027.8.26.9999", comarca="", orgao_julgador="")
    assert parsing._municipio_de_processo(outro) == ""
    # Reaprender o mesmo histórico não grava de novo (idempotente).
    assert parsing.aprender_comarcas(hist) == 0


def test_aprender_municipio_digitado_memoriza_por_foro(monkeypatch, tmp_path):
    from organizador import parsing
    from organizador.models import Processo
    _isola_cache(monkeypatch, tmp_path)

    # Usuário digita o município de um processo sem comarca (e gera).
    assert parsing.aprender_municipio("4004459-04.2026.8.26.0248", "Indaiatuba") is True
    # Outro processo do MESMO foro (0248), sem comarca → auto-preenche.
    novo = Processo(numero="5005000-00.2027.8.26.0248", comarca="", orgao_julgador="")
    assert parsing._municipio_de_processo(novo) == "Indaiatuba"
    # Manual VENCE até a comarca parseada de um processo futuro do mesmo foro.
    com_comarca = Processo(numero="6006000-00.2027.8.26.0248", comarca="Comarca de Outra")
    assert parsing._municipio_de_processo(com_comarca) == "Indaiatuba"
    # Correção do usuário sobrescreve; vazio e repetido não gravam.
    assert parsing.aprender_municipio("4004459-04.2026.8.26.0248", "Itu") is True
    assert parsing._municipio_de_processo(novo) == "Itu"
    assert parsing.aprender_municipio("4004459-04.2026.8.26.0248", "") is False
    assert parsing.aprender_municipio("4004459-04.2026.8.26.0248", "Itu") is False


def test_cache_comarcas_nao_aprende_sem_comarca(monkeypatch, tmp_path):
    from organizador import parsing
    from organizador.models import Processo
    _isola_cache(monkeypatch, tmp_path)
    # Processo sem comarca não polui o cache.
    assert parsing.aprender_comarcas([Processo(numero="5005000-00.2027.8.26.0248", comarca="")]) == 0
    assert parsing._carregar_cache_comarcas()["comarcas"] == {}


def test_parse_orgao_julgador():
    vara, comarca = op._parse_orgao_julgador("2ª Vara Cível da Comarca de Carapicuíba")
    assert vara == "2ª Vara Cível"
    assert comarca == "Comarca de Carapicuíba"


def test_parse_orgao_julgador_vazio():
    assert op._parse_orgao_julgador("") == ("", "")
