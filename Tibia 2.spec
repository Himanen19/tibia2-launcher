# -*- mode: python ; coding: utf-8 -*-

# BUILD ONEDIR (pasta), e NAO onefile (um .exe so).
#
# Por que: o bootloader onefile do PyInstaller 6 extrai tudo para %TEMP%\_MEIxxxx,
# relanca uma copia de si mesmo e o FILHO faz uma "security validation" que abre o
# processo-PAI e le o caminho do executavel dele (QueryFullProcessImageNameW). Se
# essa consulta falha - antivirus/EDR interceptando OpenProcess, corrida com o pai
# saindo, nivel de integridade diferente - o bootloader aborta com o dialogo
# "Security validation failure: failed to obtain executable path for parent
# process!". Era o erro intermitente ao abrir o launcher.
#
# O onedir nao extrai para temp nem relanca processo-filho, entao essa rotina de
# validacao NUNCA roda - o dialogo fica impossivel. De quebra e menos suscetivel a
# heuristica de AV (nao tem comportamento auto-extrator).
#
# O preco: o codigo agora vive em _internal\ (nao num .exe unico), entao o
# auto-update troca a PASTA - ver self_update() no launcher.py (baixa zip +
# swapper .bat). O instalador Inno empacota a pasta inteira (installer.iss).

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[('assets', 'assets')],
    # certifi vai junto: o launcher valida HTTPS com a NOSSA lista de raizes,
    # e nao com o repositorio de certificados do Windows (que numa maquina
    # desatualizada faz a cadeia da Let's Encrypt parecer expirada).
    hiddenimports=['certifi'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,   # onedir: os binarios/datas ficam na pasta (COLLECT), nao no exe
    name='Tibia 2',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/shield.ico'],
    # Metadados do Windows (CompanyName/ProductName/versao). Sem eles o
    # executavel sai com tudo vazio - ver o porque em version_info.txt.
    version='version_info.txt',
)

# COLLECT = modo onedir: junta exe + binarios + datas numa pasta "Tibia 2".
# Saida: dist-noupx\Tibia 2\Tibia 2.exe + dist-noupx\Tibia 2\_internal\
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Tibia 2',
)
