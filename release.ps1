# Publica uma nova versão do Organizador de Processos no GitHub Releases.
#
# Fluxo: lê __version__ → valida segredos/git → gera updater\_token.py →
#        compila Peticoes.exe e Atualizador.exe → tag vX.Y.Z → release no GitHub.
#
# Uso:
#   .\release.ps1                 # release completa
#   .\release.ps1 -SkipApp        # recompila só o Atualizador (troca de token)
#   .\release.ps1 -AllowDirty     # permite working tree com mudanças não commitadas
#   .\release.ps1 -Notes "..."    # notas da release
param(
    [switch]$SkipApp,
    [switch]$AllowDirty,
    [string]$Notes = ""
)

$ErrorActionPreference = "Stop"
$RepoSlug = "LucasTalen/automacao_processos"
$Root = $PSScriptRoot
Set-Location $Root

function Abort($msg) { Write-Host "ERRO: $msg" -ForegroundColor Red; exit 1 }

# --- 1. Versão -------------------------------------------------------------
$initPy = Get-Content "$Root\organizador\__init__.py" -Raw
if ($initPy -notmatch '__version__\s*=\s*"([^"]+)"') {
    Abort "__version__ não encontrado em organizador\__init__.py"
}
$Version = $Matches[1]
$Tag = "v$Version"
Write-Host "Versão: $Version (tag $Tag)" -ForegroundColor Cyan

# --- 2. Pré-checagens --------------------------------------------------------
gh auth status *> $null
if ($LASTEXITCODE -ne 0) { Abort "gh não autenticado. Rode: gh auth login" }

$tokenFile = "$Root\secrets\token_github.txt"
if (-not (Test-Path $tokenFile)) {
    Abort "secrets\token_github.txt não existe. Crie o PAT fine-grained (Contents: Read-only) e salve o token nesse arquivo."
}
$Token = (Get-Content $tokenFile -Raw).Trim()
if ($Token -notmatch '^github_pat_') {
    Abort "secrets\token_github.txt não parece conter um PAT fine-grained (github_pat_...)."
}

if (-not $AllowDirty) {
    $dirty = git status --porcelain
    if ($dirty) { Abort "Working tree com mudanças não commitadas. Commite antes (ou use -AllowDirty)." }
}

if ((git tag -l $Tag)) { Abort "A tag $Tag já existe. Aumente __version__ em organizador\__init__.py." }

# Trava de segurança: o token jamais pode estar rastreado pelo git.
git check-ignore -q updater/_token.py
if ($LASTEXITCODE -ne 0) { Abort "updater/_token.py NÃO está no .gitignore — risco de vazar o token." }
git check-ignore -q secrets/token_github.txt
if ($LASTEXITCODE -ne 0) { Abort "secrets/ NÃO está no .gitignore — risco de vazar o token." }
$tracked = git ls-files updater/_token.py secrets/
if ($tracked) { Abort "Arquivos de segredo rastreados pelo git: $tracked — remova com git rm --cached antes de continuar." }

# --- 3. Gerar updater\_token.py ---------------------------------------------
$tokenPy = "# -*- coding: utf-8 -*-`n# GERADO pelo release.ps1 — NUNCA commitar.`nGITHUB_TOKEN = `"$Token`"`n"
[System.IO.File]::WriteAllText("$Root\updater\_token.py", $tokenPy)  # UTF-8 sem BOM
Write-Host "updater\_token.py gerado." -ForegroundColor Cyan

# --- 4. Builds ----------------------------------------------------------------
if (-not $SkipApp) {
    # A base embutida vai no bundle como SQLite (indexada por número), para a
    # abertura não parsear os 70 MB do snapshot. Regerada a cada release a partir
    # de data/processos_base.json.gz, que é a fonte versionada.
    Write-Host "Gerando data\processos_base.sqlite..." -ForegroundColor Cyan
    python scripts\gerar_base_sqlite.py
    if ($LASTEXITCODE -ne 0) { Abort "Geração da base SQLite falhou." }

    Write-Host "Compilando Peticoes.exe..." -ForegroundColor Cyan
    python -m PyInstaller --noconfirm OrganizadorProcessos.spec
    if ($LASTEXITCODE -ne 0) { Abort "Build do Peticoes.exe falhou." }
}
Write-Host "Compilando Atualizador.exe..." -ForegroundColor Cyan
python -m PyInstaller --noconfirm Atualizador.spec
if ($LASTEXITCODE -ne 0) { Abort "Build do Atualizador.exe falhou." }

# --- 5. Sanidade dos artefatos -----------------------------------------------
$app = Get-Item "$Root\dist\Peticoes.exe" -ErrorAction SilentlyContinue
if (-not $app) { Abort "dist\Peticoes.exe não existe." }
# Piso baixado de 50 MB para 20 MB: o bundle emagreceu de 141 MB para ~45 MB ao
# sair o pandas (e com ele pyarrow/numpy/IPython) — ver OrganizadorProcessos.spec.
# O piso serve só para pegar build truncado, não para validar o conteúdo.
if ($app.Length -lt 20MB) { Abort "dist\Peticoes.exe suspeito de incompleto ($([math]::Round($app.Length/1MB)) MB < 20 MB)." }
if ($app.Length -gt 90MB) {
    Write-Host "AVISO: dist\Peticoes.exe com $([math]::Round($app.Length/1MB)) MB (esperado ~45 MB). Alguma dependência pesada voltou ao bundle? Confira o relatório '[spec] payload:' acima." -ForegroundColor Yellow
}

$upd = Get-Item "$Root\dist\Atualizador.exe" -ErrorAction SilentlyContinue
if (-not $upd) { Abort "dist\Atualizador.exe não existe." }
if ($upd.Length -lt 5MB -or $upd.Length -gt 30MB) {
    Abort "dist\Atualizador.exe com tamanho suspeito ($([math]::Round($upd.Length/1MB)) MB; esperado 5-30 MB)."
}

[System.IO.File]::WriteAllText("$Root\dist\version.txt", $Version)

# --- 6. Tag + push --------------------------------------------------------------
if ($SkipApp) {
    Write-Host "-SkipApp: pulando tag/release. Novo Atualizador.exe em dist\ — copie para as máquinas." -ForegroundColor Yellow
    exit 0
}
git tag $Tag
if ($LASTEXITCODE -ne 0) { Abort "git tag falhou." }
git push origin main
if ($LASTEXITCODE -ne 0) { Abort "git push origin main falhou." }
git push origin $Tag
if ($LASTEXITCODE -ne 0) { Abort "git push da tag falhou." }

# --- 7. Release ------------------------------------------------------------------
if (-not $Notes) { $Notes = "Build $Tag" }
gh release create $Tag `
    "$Root\dist\Peticoes.exe" `
    "$Root\dist\Atualizador.exe" `
    "$Root\dist\version.txt" `
    --repo $RepoSlug --title "Peticoes $Tag" --notes $Notes
if ($LASTEXITCODE -ne 0) { Abort "gh release create falhou." }

Write-Host ""
Write-Host "Release $Tag publicada: https://github.com/$RepoSlug/releases/tag/$Tag" -ForegroundColor Green
Write-Host "Nas máquinas dos usuários, basta rodar o Atualizador.exe." -ForegroundColor Green
