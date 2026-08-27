# -*- coding: utf-8 -*-
"""Perfila TODOS os fluxos do app e ranqueia onde mais agarra.

Cada fluxo roda num processo NOVO (pico de memória limpo, sem cache de um
contaminando o outro) e é medido em tempo de parede + pico de RSS. Roda duas
vezes: livre e sob carga (ver scripts/maquina_fraca.py), para separar "é lento"
de "é lento porque a máquina é fraca".

Por que pico de RSS e não pressão de memória de verdade: a máquina alvo tem
4-8 GB com o Windows comendo ~2 GB (ver organizador/maquina.py). Induzir swap
nesta máquina de 32 GB exigiria consumir quase toda a RAM livre, com risco para
os outros programas abertos. Medir o pico e comparar com o orçamento de 2 GB
responde a mesma pergunta sem esse risco.

O que NÃO está aqui: os scrapers de tribunal ao vivo (rede + 2FA + risco de
bloquear a conta compartilhada — ver o docstring de monitoramento.py) e o
Autojur, que tem script próprio por exigir login real.

Uso:
    python scripts/perfil_completo.py                    # livre e com carga
    python scripts/perfil_completo.py --carga 16
    python scripts/perfil_completo.py --so-livre
    python scripts/perfil_completo.py --fluxo peticoes   # um fluxo só
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ / "scripts"))

# Prólogo de todo fluxo: mede tempo e pico de RSS e imprime um JSON numa linha.
# O pico vem do Windows (PeakWorkingSetSize) — o mesmo contador que
# organizador/perf.py usa, para os números serem comparáveis com o perf.log.
_PROLOGO = '''
import json, os, sys, time
sys.path.insert(0, r"{raiz}")

def _pico_mb():
    # argtypes/restype EXPLICITOS: sem eles o handle pseudo (-1) de
    # GetCurrentProcess vira int de 32 bits e a chamada falha calada em x64,
    # devolvendo 0. Mesma armadilha documentada em organizador/perf.py.
    try:
        import ctypes
        from ctypes import wintypes
        class M(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        p = ctypes.WinDLL("psapi", use_last_error=True)
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.GetCurrentProcess.argtypes = []
        p.GetProcessMemoryInfo.restype = wintypes.BOOL
        p.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE,
                                           ctypes.POINTER(M), wintypes.DWORD]
        c = M(); c.cb = ctypes.sizeof(M)
        if not p.GetProcessMemoryInfo(k.GetCurrentProcess(), ctypes.byref(c), c.cb):
            return 0.0
        return round(c.PeakWorkingSetSize / (1024 * 1024), 1)
    except Exception:
        return 0.0

_t0 = time.perf_counter()
_erro = ""
try:
{corpo}
except Exception as _e:
    _erro = f"{{type(_e).__name__}}: {{_e}}"
print("@@" + json.dumps({{"ms": (time.perf_counter() - _t0) * 1000,
                         "pico_mb": _pico_mb(), "erro": _erro}}))
'''


def _corpo(codigo: str) -> str:
    """Indenta o corpo do fluxo para dentro do try do prólogo."""
    return "\n".join("    " + l if l.strip() else l for l in codigo.strip().split("\n"))


# --------------------------------------------------------------------------
# Os fluxos. Cada um é o que o usuário de fato espera acontecer.
# --------------------------------------------------------------------------
FLUXOS: dict[str, tuple[str, str]] = {}


def fluxo(chave: str, rotulo: str, codigo: str) -> None:
    FLUXOS[chave] = (rotulo, codigo)


fluxo("import", "1. import do módulo da GUI", """
import organizador.gui
""")

# O cronômetro do prólogo já está correndo quando o corpo começa, então este
# fluxo mede import + janela. É de propósito: é o número que o usuário sente ao
# dar duplo clique. O fluxo 1 dá a parcela do import.
fluxo("janela", "2. abertura completa (import+janela)", """
import organizador.gui as G
for _m in ("_carregar_pesquisa_inicial", "_carregar_templates",
           "_verificar_atualizacao_silenciosa", "_detectar_word_pdf"):
    setattr(G.App, _m, lambda self: None)
app = G.App()
app.destroy()
""")

fluxo("lista", "3. carga da lista de processos", """
import time
from organizador.config import (PASTA_PADRAO, PROCESSOS_ONLINE_DIR,
                                PROCESSOS_BASE_JSON, PROCESSOS_INDICE_JSON)
from organizador.indice_processos import carregar_resumos_pesquisa
_t = time.perf_counter()
r = carregar_resumos_pesquisa(PASTA_PADRAO, PROCESSOS_ONLINE_DIR,
                              PROCESSOS_BASE_JSON, PROCESSOS_INDICE_JSON)
print("#so_carga_ms", round((time.perf_counter() - _t) * 1000, 1), "#processos", len(r))
""")

fluxo("indice_filtro", "4. índice do filtro (1x na carga)", """
from organizador.config import (PASTA_PADRAO, PROCESSOS_ONLINE_DIR,
                                PROCESSOS_BASE_JSON, PROCESSOS_INDICE_JSON)
from organizador.indice_processos import carregar_resumos_pesquisa
from organizador.pesquisa import indexar_para_filtro
import time
r = carregar_resumos_pesquisa(PASTA_PADRAO, PROCESSOS_ONLINE_DIR,
                              PROCESSOS_BASE_JSON, PROCESSOS_INDICE_JSON)
_t = time.perf_counter()
tx = indexar_para_filtro(r)
print("#so_indice_ms", round((time.perf_counter() - _t) * 1000, 1), "#n", len(tx))
""")

fluxo("filtro_tecla", "5. filtro (100 teclas digitadas)", """
from organizador.config import (PASTA_PADRAO, PROCESSOS_ONLINE_DIR,
                                PROCESSOS_BASE_JSON, PROCESSOS_INDICE_JSON)
from organizador.indice_processos import carregar_resumos_pesquisa
from organizador.pesquisa import indexar_para_filtro, filtrar_processos_indexado
import time
r = carregar_resumos_pesquisa(PASTA_PADRAO, PROCESSOS_ONLINE_DIR,
                              PROCESSOS_BASE_JSON, PROCESSOS_INDICE_JSON)
tx = indexar_para_filtro(r)
_t = time.perf_counter()
for i in range(100):
    filtrar_processos_indexado(r, tx, "silva"[: (i % 5) + 1])
print("#por_tecla_ms", round((time.perf_counter() - _t) * 10, 2))
""")

fluxo("abrir_processo", "6. abrir 20 processos da lista", """
from organizador.config import (PASTA_PADRAO, PROCESSOS_ONLINE_DIR,
                                PROCESSOS_BASE_JSON, PROCESSOS_INDICE_JSON)
from organizador.indice_processos import (carregar_resumos_pesquisa,
                                          carregar_processo_completo)
import time
r = carregar_resumos_pesquisa(PASTA_PADRAO, PROCESSOS_ONLINE_DIR,
                              PROCESSOS_BASE_JSON, PROCESSOS_INDICE_JSON)
_t = time.perf_counter()
n = 0
for resumo in r[:20]:
    if carregar_processo_completo(resumo, PROCESSOS_BASE_JSON) is not None:
        n += 1
print("#por_clique_ms", round((time.perf_counter() - _t) / 20 * 1000, 1), "#ok", n)
""")

_DADOS_PET = """{"numero": "1234567-89.2020.8.26.0100", "autor": "Banco Exemplo S/A",
 "reu": "Joao da Silva", "classe": "Execucao de Titulo Extrajudicial",
 "vara": "1a Vara Civel", "comarca": "Sao Paulo", "valor_causa": "R$ 10.000,00",
 "cpf_cnpj": "123.456.789-00", "endereco": "Rua Exemplo, 100"}"""

fluxo("peticoes_lote", "7. gerar 8 petições (uso real)", """
import json, tempfile
from pathlib import Path
from organizador.config import PASTA_PETICOES
from organizador.peticoes import gerar_peticoes
from organizador.models import Processo, Contexto
t = sorted(p for p in Path(PASTA_PETICOES).rglob("*.docx"))[:8]
d = json.loads(r'''%s''')
gerar_peticoes(Processo(numero=d["numero"]), t, Path(tempfile.mkdtemp()), d,
               log=lambda s: None, ctx=Contexto())
print("#modelos", len(t))
""" % _DADOS_PET)

fluxo("peticoes_todas", "8. gerar TODAS as petições (39)", """
import json, tempfile
from pathlib import Path
from organizador.config import PASTA_PETICOES
from organizador.peticoes import gerar_peticoes
from organizador.models import Processo, Contexto
t = sorted(p for p in Path(PASTA_PETICOES).rglob("*.docx"))
d = json.loads(r'''%s''')
gerar_peticoes(Processo(numero=d["numero"]), t, Path(tempfile.mkdtemp()), d,
               log=lambda s: None, ctx=Contexto())
print("#modelos", len(t))
""" % _DADOS_PET)

fluxo("pdf", "9. converter 3 docx em PDF (Word)", """
import json, tempfile
from pathlib import Path
from organizador import pdf as P
from organizador.config import PASTA_PETICOES
from organizador.peticoes import gerar_peticoes
from organizador.models import Processo, Contexto
conv = P.conversor_disponivel()
if not conv:
    raise RuntimeError("sem conversor de PDF nesta maquina: " + P.descricao_indisponivel())
t = sorted(p for p in Path(PASTA_PETICOES).rglob("*.docx"))[:3]
d = json.loads(r'''%s''')
dest = Path(tempfile.mkdtemp())
ger = gerar_peticoes(Processo(numero=d["numero"]), t, dest, d,
                     log=lambda s: None, ctx=Contexto())
import time
_t = time.perf_counter()
ok, falhas = P.converter_lote_para_pdf(ger, log=None)
print("#conversor", conv, "#pdfs", len(ok), "#falhas", len(falhas),
      "#so_conversao_ms", round((time.perf_counter() - _t) * 1000))
""" % _DADOS_PET)

fluxo("organizador", "10. organizar pasta de PDFs", """
import shutil, tempfile, time
from pathlib import Path
from organizador.organizacao import organizar_pasta
# Copia os PDFs para um tmp: organizar_pasta MOVE os arquivos de origem, e não
# vamos mexer na pasta de trabalho de verdade.
origem = Path(tempfile.mkdtemp())
achados = sorted(Path.home().joinpath("Desktop", "Processos").rglob("*.pdf"))[:10]
if not achados:
    raise RuntimeError("nenhum PDF de entrada para organizar")
for p in achados:
    shutil.copy2(p, origem / p.name)
_t = time.perf_counter()
r = organizar_pasta(origem, Path(tempfile.mkdtemp()), log=lambda s: None)
print("#so_organizacao_ms", round((time.perf_counter() - _t) * 1000),
      "#pdfs", r.get("total"), "#ok", r.get("organizados"),
      "#por_pdf_ms", round((time.perf_counter() - _t) * 1000 / max(1, r.get("total", 1))))
""")

fluxo("log_gui", "11. log da GUI (5000 linhas)", """
import time, tkinter as tk
import organizador.gui as G
for _m in ("_carregar_pesquisa_inicial", "_carregar_templates",
           "_verificar_atualizacao_silenciosa", "_detectar_word_pdf"):
    setattr(G.App, _m, lambda self: None)
app = G.App()
# Uma passada de monitoramento produz 6-17 mil linhas de log (ver o docstring
# de tests/test_limites_memoria.py). É o widget que segurava tudo isso.
widget = tk.Text(app)
_t = time.perf_counter()
for i in range(0, 5000, 50):
    app._append_log(widget, [f"[{i+j}] linha de log do monitoramento\\n" for j in range(50)])
ms = (time.perf_counter() - _t) * 1000
print("#5000_linhas_ms", round(ms), "#linhas_no_widget",
      int(widget.index("end-1c").split(".")[0]))
app.destroy()
""")

fluxo("estado_monitor", "12. estado do monitor (1600 processos)", """
import time, tempfile
from pathlib import Path
from organizador import monitoramento as M
# O estado é regravado por INTEIRO durante a passada; com ~1600 processos ele
# passa de 500 KB. O _GravadorThrottled existe por causa disso — aqui medimos
# o custo de uma gravação e de 1600 chamadas ao gravador.
estado = {f"{i:07d}-11.2020.8.26.0100": {
    "status": "enriquecido", "uf": "SP", "campanha": "teste",
    "tem_reu": True, "autor": "AUTOR " * 4, "reu": "REU " * 6,
    "quando": "2026-08-26T10:00:00"} for i in range(1600)}
alvo = Path(tempfile.mkdtemp()) / "_monitor_estado.json"
_t = time.perf_counter()
M._gravar_estado(estado, alvo)
uma = (time.perf_counter() - _t) * 1000
g = M._GravadorThrottled(alvo)
_t = time.perf_counter()
for _ in range(1600):
    g.talvez(estado)
g.finalizar(estado)
passada = (time.perf_counter() - _t) * 1000
print("#uma_gravacao_ms", round(uma, 1), "#passada_1600_ms", round(passada),
      "#kb", round(alvo.stat().st_size / 1024))
""")

fluxo("word_frio", "13. Word: 1ª conversão vs 2ª", """
import json, tempfile, time
from pathlib import Path
from organizador import pdf as P
from organizador.config import PASTA_PETICOES
from organizador.peticoes import gerar_peticoes
from organizador.models import Processo, Contexto
if not P.conversor_disponivel():
    raise RuntimeError("sem conversor de PDF: " + P.descricao_indisponivel())
t = sorted(p for p in Path(PASTA_PETICOES).rglob("*.docx"))[:2]
d = json.loads(r'''%s''')
ger = gerar_peticoes(Processo(numero=d["numero"]), t, Path(tempfile.mkdtemp()), d,
                     log=lambda s: None, ctx=Contexto())
# Uma por vez, para separar o start do Word (1ª) do custo por documento (2ª).
_t = time.perf_counter(); P.converter_para_pdf(ger[0]); primeira = (time.perf_counter()-_t)*1000
_t = time.perf_counter(); P.converter_para_pdf(ger[1]); segunda = (time.perf_counter()-_t)*1000
print("#1a_conversao_ms", round(primeira), "#2a_conversao_ms", round(segunda),
      "#start_do_word_ms", round(primeira - segunda))
""" % _DADOS_PET)

fluxo("parse_pje", "14. parsear HTML do PJe (50x)", """
import time
from pathlib import Path
fix = Path(r"{raiz}") / "tests" / "fixtures" / "pje_tjrj_detalhe.html"
html = fix.read_text(encoding="utf-8", errors="replace")
from organizador.integracoes.tjsp_scraper.parser_pje import extrair_processo_pje
_t = time.perf_counter()
for _ in range(50):
    extrair_processo_pje(html)
print("#por_parse_ms", round((time.perf_counter() - _t) / 50 * 1000, 2))
""".replace("{raiz}", str(_RAIZ)))


# --------------------------------------------------------------------------
def _rodar(chave: str, n: int) -> dict:
    rotulo, codigo = FLUXOS[chave]
    script = _PROLOGO.format(raiz=str(_RAIZ), corpo=_corpo(codigo))
    amostras, notas, erro = [], "", ""
    for _ in range(n):
        r = subprocess.run([sys.executable, "-c", script],
                           capture_output=True, text=True, cwd=str(_RAIZ))
        linha = next((l for l in r.stdout.splitlines() if l.startswith("@@")), None)
        if not linha:
            erro = (r.stderr or r.stdout or "sem saida").strip().split("\n")[-1][:120]
            continue
        d = json.loads(linha[2:])
        if d["erro"]:
            erro = d["erro"][:120]
            continue
        amostras.append(d)
        # As notas são os "#chave valor" que o próprio fluxo imprime (quantos
        # processos, ms por tecla, qual conversor). Tudo menos a linha @@.
        notas = " ".join(l for l in r.stdout.splitlines()
                         if not l.startswith("@@") and l.strip())[:100]
    if not amostras:
        return {"rotulo": rotulo, "erro": erro or "falhou"}
    return {
        "rotulo": rotulo,
        "ms": statistics.median(a["ms"] for a in amostras),
        "pico_mb": max(a["pico_mb"] for a in amostras),
        "notas": notas,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--carga", type=int, default=16, help="ladrões de CPU (0 = sem carga)")
    ap.add_argument("-n", type=int, default=3, help="repetições por fluxo")
    ap.add_argument("--so-livre", action="store_true", help="não mede sob carga")
    ap.add_argument("--fluxo", action="append", help="só este fluxo (repetível)")
    args = ap.parse_args()

    chaves = args.fluxo or list(FLUXOS)
    desconhecidas = [c for c in chaves if c not in FLUXOS]
    if desconhecidas:
        ap.error(f"fluxo desconhecido: {desconhecidas}. Use: {', '.join(FLUXOS)}")

    print("=" * 78)
    print("LIVRE")
    print("=" * 78)
    livres = {}
    for c in chaves:
        livres[c] = _rodar(c, args.n)
        r = livres[c]
        if "erro" in r:
            print(f"  {r['rotulo']:38} FALHOU: {r['erro']}")
        else:
            print(f"  {r['rotulo']:38} {r['ms']:8.0f} ms   pico {r['pico_mb']:6.1f} MB"
                  f"   {r.get('notas','')}")

    if args.so_livre or args.carga <= 0:
        return

    from maquina_fraca import Ladroes, calibrar
    print()
    print("=" * 78)
    print(f"SOB CARGA ({args.carga} ladrões)")
    print("=" * 78)
    fator = calibrar(args.carga, 3)
    print()
    carregados = {}
    with Ladroes(args.carga):
        for c in chaves:
            carregados[c] = _rodar(c, args.n)
            r = carregados[c]
            if "erro" in r:
                print(f"  {r['rotulo']:38} FALHOU: {r['erro']}")
            else:
                print(f"  {r['rotulo']:38} {r['ms']:8.0f} ms   pico {r['pico_mb']:6.1f} MB")

    # ---- ranking: onde mais agarra ----
    print()
    print("=" * 78)
    print("ONDE MAIS AGARRA (ordenado pelo tempo sob carga)")
    print("=" * 78)
    print(f"  {'fluxo':38} {'livre':>9} {'carga':>9} {'fator':>7} {'pico':>9}")
    linhas = []
    for c in chaves:
        a, b = livres.get(c, {}), carregados.get(c, {})
        if "ms" not in a or "ms" not in b:
            continue
        linhas.append((b["ms"], a["ms"], b["ms"] / a["ms"] if a["ms"] else 0,
                       max(a["pico_mb"], b["pico_mb"]), a["rotulo"]))
    for ms_c, ms_l, f, pico, rot in sorted(linhas, reverse=True):
        alerta = "  <-- " + ("ESTOURA 2 GB" if pico > 2048 else "GARGALO") if (
            ms_c > 3000 or pico > 2048) else ""
        print(f"  {rot:38} {ms_l:6.0f} ms {ms_c:6.0f} ms {f:6.2f}x {pico:6.1f} MB{alerta}")
    print()
    print(f"régua da carga: {fator:.2f}x — um fluxo com fator MUITO acima disso é")
    print("mais sensível à máquina fraca do que o resto (e é onde mexer primeiro).")


if __name__ == "__main__":
    main()
