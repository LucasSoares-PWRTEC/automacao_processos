# -*- coding: utf-8 -*-
"""Botão "Restaurar dados do programa" (aba Configurações).

Ele existe porque atualizar o .exe NÃO conserta uma máquina cujo cache local
está com dados errados: a lista da Pesquisa dá prioridade ao que está em
APP_DATA_DIR sobre a base embutida na release, e a atualização não toca em
APP_DATA_DIR de propósito (as senhas do usuário moram lá).

O risco do botão é o oposto do bug: apagar de mais. Estes testes fixam a
fronteira — o que é derivado vai, o que o usuário digitou fica.
"""

from __future__ import annotations

import shutil

from organizador import gui
from organizador.config import (
    AUTOJUR_PROFILE_DIR, COMARCAS_CACHE_PATH, CONFIG_DIR, CONFIG_ENV_PATH,
    CREDENCIAIS_PATH, UI_STATE_PATH,
)


def _app_data_falso(tmp_path, n_processos: int = 3):
    """APP_DATA_DIR de mentira, com derivados E dados do usuário."""
    online = tmp_path / "processos_online"
    for i in range(n_processos):
        pasta = online / f"000{i}-11.2020.8.26.0100"
        pasta.mkdir(parents=True)
        (pasta / "metadados.json").write_text("{}", encoding="utf-8")
    indice = tmp_path / "processos_indice.json"
    indice.write_text("{}", encoding="utf-8")
    templates = tmp_path / "templates_placeholders.json"
    templates.write_text("{}", encoding="utf-8")
    return online, indice, templates


def _apontar(monkeypatch, online, indice, templates):
    monkeypatch.setattr(gui, "PROCESSOS_ONLINE_DIR", online)
    monkeypatch.setattr(gui, "PROCESSOS_INDICE_JSON", indice)
    monkeypatch.setattr(gui, "TEMPLATES_CACHE_PATH", templates)


def test_apaga_os_tres_derivados(monkeypatch, tmp_path):
    online, indice, templates = _app_data_falso(tmp_path)
    _apontar(monkeypatch, online, indice, templates)

    apagados, falhas = gui._resetar_dados_locais()

    assert falhas == []
    assert len(apagados) == 3
    assert not online.exists()
    assert not indice.exists()
    assert not templates.exists()


def test_nao_apaga_nada_que_o_usuario_digitou():
    """A fronteira do botão, em forma de teste.

    `config/` guarda as senhas dos tribunais e os segredos de 2FA; o perfil do
    Autojur guarda o dispositivo confiável (apagar = 2FA de novo); o ui_state
    guarda as pastas escolhidas; e comarcas_por_origem.json tem correções
    MANUAIS de comarca. Nada disso se reconstrói sozinho — se um dia alguém
    incluir um deles na limpeza, este teste quebra antes de virar release."""
    alvos = {c.resolve() for c, _rotulo in gui._itens_reset()}
    intocaveis = (
        CONFIG_DIR, CONFIG_ENV_PATH, CREDENCIAIS_PATH,
        AUTOJUR_PROFILE_DIR, UI_STATE_PATH, COMARCAS_CACHE_PATH,
    )
    for caminho in intocaveis:
        assert caminho.resolve() not in alvos, f"{caminho.name} não pode ser apagado"


def test_nao_toca_no_resto_de_app_data(monkeypatch, tmp_path):
    online, indice, templates = _app_data_falso(tmp_path)
    _apontar(monkeypatch, online, indice, templates)
    vizinhos = []
    for nome in ("ui_state.json", "comarcas_por_origem.json", "updater.log"):
        p = tmp_path / nome
        p.write_text("x", encoding="utf-8")
        vizinhos.append(p)
    config = tmp_path / "config"
    config.mkdir()
    (config / ".env").write_text("SENHA=123", encoding="utf-8")

    gui._resetar_dados_locais()

    assert all(p.exists() for p in vizinhos)
    assert (config / ".env").read_text(encoding="utf-8") == "SENHA=123"


def test_sem_nada_para_apagar_nao_e_falha(monkeypatch, tmp_path):
    """Segunda vez seguida no botão: "nada a restaurar", não "deu erro"."""
    _apontar(
        monkeypatch,
        tmp_path / "nao_existe", tmp_path / "sumiu.json", tmp_path / "sumiu2.json",
    )
    assert gui._resetar_dados_locais() == ([], [])


def test_pasta_que_sobrevive_vira_falha(monkeypatch, tmp_path):
    """`rmtree(ignore_errors=True)` não levanta quando um arquivo está travado —
    ele apaga o resto e volta calado. Sem a conferência depois, o app diria
    "pronto, limpei" para uma pasta que continua lá."""
    online, indice, templates = _app_data_falso(tmp_path)
    _apontar(monkeypatch, online, indice, templates)
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)

    apagados, falhas = gui._resetar_dados_locais()

    assert falhas == ["processos guardados de buscas online"]
    assert "processos guardados de buscas online" not in apagados
    assert not indice.exists()  # os outros dois seguem sendo limpos


def test_contagem_de_processos_locais(monkeypatch, tmp_path):
    online, indice, templates = _app_data_falso(tmp_path, n_processos=7)
    _apontar(monkeypatch, online, indice, templates)
    assert gui._contar_processos_locais() == 7


def test_contagem_sem_cache_e_zero(monkeypatch, tmp_path):
    _apontar(monkeypatch, tmp_path / "nao_existe", tmp_path / "a", tmp_path / "b")
    assert gui._contar_processos_locais() == 0


# --------------------------------------------------------------------------
# Atualizar senhas dos tribunais a partir da planilha do escritório
# --------------------------------------------------------------------------
def _escrever_creds(caminho, itens):
    import json as _json

    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(_json.dumps({"credenciais": itens}, ensure_ascii=False), encoding="utf-8")


def _montar(monkeypatch, tmp_path, locais):
    """Aponta CONFIG_DIR/CREDENCIAIS_PATH (a máquina do usuário) para tmp."""
    from organizador import config as C

    local_dir = tmp_path / "appdata"
    _escrever_creds(local_dir / "credenciais.json", locais)
    monkeypatch.setattr(C, "CONFIG_DIR", local_dir)
    monkeypatch.setattr(C, "CONFIG_SEMENTE_DIR", tmp_path / "bundle")
    monkeypatch.setattr(C, "CREDENCIAIS_PATH", local_dir / "credenciais.json")
    return C, local_dir


def test_diff_da_planilha_aponta_rotulo_e_nunca_senha(monkeypatch, tmp_path):
    C, _ = _montar(
        monkeypatch, tmp_path,
        locais=[
            {"tj": "TJDF - 1º Grau", "login": "1", "senha": "velha"},
            {"tj": "TJMG - PJE", "login": "1", "senha": "igual"},
        ],
    )
    diff, final = C.planejar_credenciais([
        {"tj": "TJDF - 1º Grau", "login": "1", "senha": "NOVA"},
        {"tj": "TJMG - PJE", "login": "1", "senha": "igual"},
        {"tj": "TJZZ - nova", "login": "1", "senha": "y"},
    ])
    assert diff["alteradas"] == ["TJDF - 1º Grau"]   # MG igual → fora
    assert diff["novas"] == ["TJZZ - nova"]
    assert diff["so_local"] == []
    # nenhuma senha pode escapar no relatório mostrado ao usuário
    assert "NOVA" not in str(diff) and "velha" not in str(diff)
    assert [i["senha"] for i in final] == ["NOVA", "igual", "y"]


def test_planilha_nao_apaga_credencial_que_so_existe_aqui(monkeypatch, tmp_path):
    """ANTI-WIPE. A planilha ACRESCENTA E CORRIGE — nunca apaga.

    Planilha meio preenchida (filtro ligado, coluna renomeada, download
    truncado) não pode zerar acesso que funcionava nesta máquina. Se alguém
    "simplificar" o merge para um replace, é este teste que grita.
    """
    C, _ = _montar(
        monkeypatch, tmp_path,
        locais=[
            {"tj": "TJDF - 1º Grau", "login": "1", "senha": "s"},
            {"tj": "TJXX - só aqui", "login": "2", "senha": "guardada"},
        ],
    )
    diff, final = C.planejar_credenciais([{"tj": "TJDF - 1º Grau", "login": "1", "senha": "s"}])
    assert diff["so_local"] == ["TJXX - só aqui"]
    assert diff["alteradas"] == [] and diff["novas"] == []
    guardada = [i for i in final if i["tj"] == "TJXX - só aqui"]
    assert guardada == [{"tj": "TJXX - só aqui", "login": "2", "senha": "guardada"}]


def test_celula_vazia_na_planilha_nao_apaga_senha_local(monkeypatch, tmp_path):
    """ANTI-WIPE. Célula em branco é ausência de informação, não ordem de apagar."""
    C, _ = _montar(
        monkeypatch, tmp_path,
        locais=[{"tj": "TJDF - 1º Grau", "login": "1", "senha": "boa"}],
    )
    diff, final = C.planejar_credenciais([{"tj": "TJDF - 1º Grau", "login": "", "senha": ""}])
    assert final == [{"tj": "TJDF - 1º Grau", "login": "1", "senha": "boa"}]
    assert diff["alteradas"] == []


def test_rotulo_repetido_no_arquivo_local_nao_e_perdido(monkeypatch, tmp_path):
    """O credenciais.json real tem "TRF6 1º Grau" DUAS vezes, com logins
    diferentes. Merge com dict por rótulo colapsaria as duas em uma e destruiria
    uma credencial de verdade."""
    C, _ = _montar(
        monkeypatch, tmp_path,
        locais=[
            {"tj": "TRF6 1º Grau", "login": "a", "senha": "1"},
            {"tj": "TRF6 1º Grau", "login": "b", "senha": "2"},
        ],
    )
    _diff, final = C.planejar_credenciais([{"tj": "TRF6 1º Grau", "login": "", "senha": "NOVA"}])
    assert len(final) == 2
    assert [i["login"] for i in final] == ["a", "b"]        # logins preservados
    assert [i["senha"] for i in final] == ["NOVA", "NOVA"]  # senha aplicada nas duas


def test_travessao_da_planilha_casa_com_o_hifen_local(monkeypatch, tmp_path):
    """Os rótulos reais misturam '-' e '–'. Sem normalizar, a mesma credencial
    apareceria como 'nova' e 'só local' ao mesmo tempo."""
    C, _ = _montar(
        monkeypatch, tmp_path,
        locais=[{"tj": "TJMA – PJE – 1º Grau", "login": "1", "senha": "velha"}],
    )
    diff, final = C.planejar_credenciais([
        {"tj": "TJMA - PJE - 1o Grau", "login": "1", "senha": "NOVA"},
    ])
    assert diff["alteradas"] == ["TJMA – PJE – 1º Grau"]  # mantém a grafia local
    assert diff["novas"] == [] and diff["so_local"] == []
    assert len(final) == 1 and final[0]["senha"] == "NOVA"


def test_chave_credencial_casa_com_a_normalizacao_do_scraper():
    """`config._chave_credencial` e `credenciais._norm` têm de concordar: se
    divergirem, o app grava numa chave e o scraper procura por outra."""
    from organizador import config as C
    from organizador.integracoes.tjsp_scraper import credenciais as cred

    for rotulo in ("TJSP - Esaj", "TJMA – PJE – 1º Grau", "TJMG  -  eproc 1 e 2 graus",
                   "TRF6 1º Grau", "OAB Nacional", "TJBA – PJE – 1º Grau"):
        assert C._chave_credencial(rotulo) == cred._norm(rotulo)


def test_cpf_sem_zero_a_esquerda_nao_conta_como_alterada(monkeypatch, tmp_path):
    """Célula numérica na planilha come o zero do CPF. Sem recuperá-lo, o
    tribunal apareceria como 'alterada' para sempre e o login gravado quebraria
    o login do e-SAJ."""
    from organizador import sheets

    C, _ = _montar(
        monkeypatch, tmp_path,
        locais=[{"tj": "TJSP - Esaj", "login": "03015257650", "senha": "s"}],
    )
    # como a planilha entrega: célula numérica → sem o zero
    remoto = {"tj": "TJSP - Esaj", "login": sheets._login_credencial("3015257650"), "senha": "s"}
    diff, final = C.planejar_credenciais([remoto])
    assert diff["alteradas"] == []
    assert final[0]["login"] == "03015257650"


def test_planilha_vale_rodando_do_codigo_fonte(monkeypatch, tmp_path):
    """A regra 'bundle == local ⇒ nada a comparar' era do bundle. Uma planilha
    remota existe e difere do local também quando se roda do código-fonte."""
    from organizador import config as C

    mesma = tmp_path / "config"
    _escrever_creds(mesma / "credenciais.json", [{"tj": "T", "login": "1", "senha": "velha"}])
    monkeypatch.setattr(C, "CONFIG_DIR", mesma)
    monkeypatch.setattr(C, "CONFIG_SEMENTE_DIR", mesma)
    monkeypatch.setattr(C, "CREDENCIAIS_PATH", mesma / "credenciais.json")

    diff, _final = C.planejar_credenciais([{"tj": "T", "login": "1", "senha": "NOVA"}])
    assert diff["alteradas"] == ["T"]


def test_aplicar_credenciais_guarda_backup_e_grava_canonico(monkeypatch, tmp_path):
    import json as _json

    C, local_dir = _montar(
        monkeypatch, tmp_path,
        locais=[{"tj": "TJDF - 1º Grau", "login": "1", "senha": "velha"}],
    )
    _diff, final = C.planejar_credenciais([
        {"tj": "TJDF - 1º Grau", "login": "1", "senha": "NOVA"},
    ])
    backup = C.aplicar_credenciais(final)

    agora = _json.loads((local_dir / "credenciais.json").read_text(encoding="utf-8"))
    assert agora == {"credenciais": [{"tj": "TJDF - 1º Grau", "login": "1", "senha": "NOVA"}]}
    # a edição local descartada tem de ter volta
    assert backup is not None and backup.exists()
    assert "velha" in backup.read_text(encoding="utf-8")


def test_aplicar_credenciais_sem_arquivo_anterior_nao_tem_backup(monkeypatch, tmp_path):
    from organizador import config as C

    local_dir = tmp_path / "appdata"
    monkeypatch.setattr(C, "CONFIG_DIR", local_dir)
    monkeypatch.setattr(C, "CREDENCIAIS_PATH", local_dir / "credenciais.json")
    assert C.aplicar_credenciais([{"tj": "T", "login": "1", "senha": "s"}]) is None
    assert (local_dir / "credenciais.json").exists()


def test_aplicar_credenciais_limpa_o_cache_do_scraper(monkeypatch, tmp_path):
    """Sem o cache_clear a busca em curso continuaria logando com a senha velha
    que já está em memória."""
    from organizador import config as C
    from organizador.integracoes.tjsp_scraper import credenciais as cred

    C, local_dir = _montar(
        monkeypatch, tmp_path,
        locais=[{"tj": "TJDF - 1º Grau", "login": "1", "senha": "velha"}],
    )
    monkeypatch.setattr(cred, "CREDENCIAIS_PATH", local_dir / "credenciais.json")
    cred._carregar_credenciais.cache_clear()
    assert cred.obter_credenciais("TJDF - 1º Grau") == ("1", "velha")

    C.aplicar_credenciais([{"tj": "TJDF - 1º Grau", "login": "1", "senha": "NOVA"}])
    assert cred.obter_credenciais("TJDF - 1º Grau") == ("1", "NOVA")
    cred._carregar_credenciais.cache_clear()


def test_arquivo_local_ilegivel_nao_derruba_o_plano(monkeypatch, tmp_path):
    """JSON corrompido vira lista vazia: tudo da planilha entra como 'nova',
    em vez de o botão estourar sem dizer o que fazer."""
    from organizador import config as C

    local_dir = tmp_path / "appdata"
    local_dir.mkdir(parents=True)
    (local_dir / "credenciais.json").write_text("{isso não é json", encoding="utf-8")
    monkeypatch.setattr(C, "CONFIG_DIR", local_dir)
    monkeypatch.setattr(C, "CREDENCIAIS_PATH", local_dir / "credenciais.json")

    diff, final = C.planejar_credenciais([{"tj": "T", "login": "1", "senha": "s"}])
    assert diff["novas"] == ["T"] and len(final) == 1


def test_normalizacao_de_valor_de_credencial():
    from organizador import config as C

    assert C._valor_credencial(None) == ""
    assert C._valor_credencial("  s  ") == "s"
    assert C._valor_credencial("12345678900.0") == "12345678900"  # ".0" do Excel
    assert C._valor_credencial("senha.0") == "senha.0"            # não é dígito: fica
    assert C._login_credencial("3015257650") == "03015257650"
    assert C._login_credencial("12345678") == "12345678"          # OAB: não infla


def test_json_de_formato_estranho_vira_lista_vazia(monkeypatch, tmp_path):
    """Arquivo com forma inesperada não pode derrubar o botão: vira "nada local",
    e tudo da planilha entra como novo."""
    from organizador import config as C

    local_dir = tmp_path / "appdata"
    local_dir.mkdir(parents=True)
    alvo = local_dir / "credenciais.json"
    monkeypatch.setattr(C, "CONFIG_DIR", local_dir)
    monkeypatch.setattr(C, "CREDENCIAIS_PATH", alvo)

    alvo.write_text('{"credenciais": "isto devia ser lista"}', encoding="utf-8")
    assert C._itens_credenciais(alvo) == []

    # itens que não são dicionário, e dicionário sem tj, são pulados
    alvo.write_text('{"credenciais": ["texto solto", {"login": "1"}, '
                    '{"tj": "T", "login": "1", "senha": "s"}]}', encoding="utf-8")
    assert C._itens_credenciais(alvo) == [{"tj": "T", "login": "1", "senha": "s"}]


def test_planejar_da_planilha_usa_o_leitor_do_sheets(monkeypatch, tmp_path):
    """Fecha o circuito planilha → plano sem ir à rede."""
    from organizador import config as C, sheets

    C, _ = _montar(monkeypatch, tmp_path, locais=[{"tj": "T", "login": "1", "senha": "velha"}])
    monkeypatch.setattr(
        sheets, "coletar_credenciais_da_planilha",
        lambda log=print: [{"tj": "T", "login": "1", "senha": "NOVA"}],
    )
    diff, final = C.planejar_credenciais_da_planilha(log=lambda _m: None)
    assert diff["alteradas"] == ["T"] and final[0]["senha"] == "NOVA"
