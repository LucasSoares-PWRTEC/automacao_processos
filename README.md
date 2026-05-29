# Organizador de Processos — eproc/TJSP

Ferramenta de desktop (GUI Tkinter) para organizar PDFs de processos judiciais e
gerar petições. Lê os PDFs de uma pasta, identifica o número do processo (padrão
CNJ), cria uma subpasta por processo, move os documentos e extrai os metadados
(cabeçalho, partes, advogados, eventos, prazos, atos) para `metadados.json` e
`resumo.txt`, além de um `índice.csv` consolidado.

Também permite pesquisar um processo já organizado (ou buscá-lo online via
[TJSP-Scraper](#busca-online-opcional)) e gerar petições `.docx` a partir de
modelos da pasta `Petições/`, com flexão automática de gênero/número.

## Funcionalidades

- **Organização em lote** de PDFs do eproc, com leitura paralela e cache de texto.
- **Classificação automática** do tipo de PDF: capa (`detalhes`), extrato de
  distribuição (`extrato`) ou ato individual — despacho/decisão/sentença (`ato`).
- **Extração estruturada**: tribunal/UF (inferidos do CNJ), comarca, vara, partes,
  advogados (OAB), eventos com prazos abertos, atos com signatário e código verificador.
- **Geração de petições** a partir de modelos `.doc`/`.docx`, substituindo
  placeholders e flexionando o vocabulário processual (autor/ré/executados...)
  conforme o gênero e número das partes. Suporta documento consolidado (vários
  modelos em um) com deduplicação de parágrafos.
- **Busca online** (opcional) do processo nos sistemas eproc/e-SAJ/PJe.

## Requisitos

- Python **3.11+** (desenvolvido/testado em 3.14).
- Dependências de runtime (ver [`requirements.txt`](requirements.txt)):
  - [`pypdf`](https://pypi.org/project/pypdf/) — extração de texto dos PDFs.
  - [`python-docx`](https://pypi.org/project/python-docx/) — leitura/escrita de `.docx`.

Instalação:

```powershell
python -m pip install -r requirements.txt
```

### Dependências opcionais (somente Windows)

São carregadas de forma **tardia** — a ausência delas não impede o uso das demais
funções:

- **`pywin32`** — conversão `.doc → .docx` via Microsoft Word (COM) e busca online.
- **[TJSP-Scraper](https://github.com/)** (projeto irmão, `selenium`, `python-dotenv`
  etc.) — necessário apenas para a busca online de processos.

## Como executar

```powershell
python organizar_processos.py
```

No Windows, basta dar duplo clique em **`Organizador.bat`**.

## Estrutura do projeto

O código foi refatorado de um único arquivo para um pacote organizado por
responsabilidade. O `organizar_processos.py` na raiz é um *shim* fino: mantém o
ponto de entrada (`Organizador.bat`) e re-exporta o pacote por compatibilidade.

```
automacao_processos/
├── organizar_processos.py   # ponto de entrada + camada de compatibilidade
├── organizador/             # pacote principal
│   ├── config.py            # caminhos, mapa de TJ/sistema, OAB, layout
│   ├── padroes.py           # regexes e mapas de parsing
│   ├── models.py            # dataclasses (Processo, Parte, Evento, ...)
│   ├── texto.py             # extração de texto do PDF + detecção de tipo
│   ├── parsing.py           # parsers de texto -> Processo
│   ├── organizacao.py       # organização de pastas, índice CSV, loaders
│   ├── flexao.py            # gênero/número e flexão de vocabulário
│   ├── peticoes.py          # geração de petições .docx
│   ├── scraper.py           # busca online (TJSP-Scraper, imports tardios)
│   └── gui.py               # interface gráfica Tkinter + main()
├── Petições/                # modelos .doc/.docx das petições
├── tests/                   # suíte de testes (pytest)
└── pyproject.toml           # configuração do projeto e do pytest
```

## Testes

A suíte usa **pytest** e cobre toda a lógica pura: parsing de PDF (sobre fixtures
de texto sintéticas), inferência de CNJ, flexão de gênero/número, substituição de
placeholders, geração de `.docx` em memória, índice CSV e carregamento de
metadados.

Ficam **fora** da cobertura automatizada (dependem de ambiente externo): a GUI
Tkinter, a busca online (Selenium/credenciais) e a conversão `.doc → .docx` via
Microsoft Word (COM).

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
```

## Busca online (opcional)

A aba *Pesquisa de Processo* permite consultar um processo diretamente no tribunal
via TJSP-Scraper. Apenas o **eproc** tem extrator estruturado (auto-preenche os
campos da petição); para e-SAJ/PJe é devolvido um processo mínimo. O caminho do
projeto TJSP-Scraper é configurado em [`organizador/config.py`](organizador/config.py)
(`TJSP_SCRAPER_DIR`).

## Saída gerada

Para cada processo identificado, dentro da pasta de destino:

```
<numero-do-processo>/
├── <PDFs do processo, renomeados por tipo>
├── metadados.json   # todos os campos extraídos
└── resumo.txt       # resumo legível (partes, prazos, histórico de eventos)
índice.csv           # uma linha por processo, na raiz do destino
_nao_identificados/  # PDFs sem número de processo reconhecível
```
