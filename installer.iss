; Instalador do launcher do Tibia 2 (Inno Setup 6).
;
; Instalador FINO: empacota so o Tibia 2.exe (~20 MB). O jogador escolhe a pasta,
; ganha atalhos (Desktop + Menu Iniciar) e um desinstalador; na primeira execucao
; o launcher baixa o cliente inteiro NA PASTA DA INSTALACAO.
;
; Pasta padrao: %LOCALAPPDATA%\Programs\Tibia 2 - onde o Windows espera programa
; instalado por usuario, sem admin (PrivilegesRequired=lowest), e gravavel, entao o
; launcher grava os ~940 MB do cliente na propria pasta. O jogador acha o jogo pelo
; ATALHO da Area de Trabalho (marcado de fabrica) e pelo Menu Iniciar.
;
; Ate 11/10/2026 o padrao era uma pasta "Tibia 2" na propria Area de Trabalho. Um
; executavel que baixa outros executaveis e troca a si mesmo, rodando do Desktop, e o
; retrato que antivirus e SmartScreen fazem de um "dropper"; da pasta de programas e
; o comportamento de um instalador comum. Quem ja instalou continua onde esta (o Inno
; reaproveita a pasta da instalacao anterior). O usuario pode trocar a pasta no
; assistente. O UninstallDelete remove tambem o cliente baixado.

#define MyAppName "Tibia 2"
#define MyAppVersion "1.0"
#define MyAppPublisher "Tibia 2"
#define MyAppURL "https://tibia2ot.com/"
#define MyAppExeName "Tibia 2.exe"
; Pasta que o PyInstaller gera (COLLECT name= no .spec). Fica "Tibia 2": e o
; nome do build.
#define BuildDir "Tibia 2"

[Setup]
AppId={{D04FCDFF-24FF-4AC1-8CE5-00C8C970F943}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=Tibia 2 Setup
SetupIconFile=assets\shield.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
; ATENCAO: a fonte e dist-noupx\Tibia 2\ (PASTA), e NAO dist\.
;
; O launcher agora e ONEDIR: uma pasta com "Tibia 2.exe" + "_internal\", e nao um
; .exe unico. Motivo em Tibia 2.spec (o onefile disparava "Security validation
; failure: failed to obtain executable path for parent process"). Entao o
; instalador copia a pasta INTEIRA para {app}, recursivamente.
;
; O PyInstaller com UPX ligado produz binario que o Defender marca como
; Trojan:Win32/Wacatac.B!ml. O Tibia 2.spec tem upx=False; dist\ ainda pode conter
; build antigo COM UPX, por isso a fonte e dist-noupx explicitamente.
;
; Sobre nao disparar auto-update na primeira aberta: o launcher compara a sua
; LAUNCHER_VERSION (baked no codigo) com o "version" do launcher.json. Como a
; pasta instalada carrega a mesma versao que foi publicada, quem instala do zero
; nao ve update imediato. Ver docs/deploy-online.md.
Source: "dist-noupx\{#BuildDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Rebrand OTibion (29/09) REVERTIDO em 30/09. O AppId e o MESMO, entao quem
; reinstala por cima atualiza a instalacao existente. Quem instalou naquela fase
; ganhou atalhos "OTibion" (-> OTibion.exe): eles saem para nao sobrar um de cada;
; os novos voltam a "Tibia 2". As copias "OTibion.exe" (launcher) e "OTibion
; Client.exe" (jogo) deixaram de ser publicadas em 01/10 e saem tambem.
Type: files; Name: "{autodesktop}\OTibion.lnk"
Type: files; Name: "{group}\OTibion.lnk"
Type: files; Name: "{app}\OTibion.exe"
Type: files; Name: "{app}\OTibion Client.exe"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Remove tambem o cliente baixado na primeira execucao (nao rastreado pelo Files).
Type: filesandordirs; Name: "{app}"
