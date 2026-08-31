; Instalador do launcher do Tibia 2 (Inno Setup 6).
;
; Instalador FINO: empacota so o Tibia 2.exe (~20 MB). O jogador escolhe a pasta,
; ganha atalhos (Desktop + Menu Iniciar) e um desinstalador; na primeira execucao
; o launcher baixa o cliente inteiro NA PASTA DA INSTALACAO.
;
; Pasta padrao: uma pasta "Tibia 2" na AREA DE TRABALHO do usuario (o jogador ve
; e acha facil). PrivilegesRequired=lowest = sem admin, e o Desktop e gravavel,
; entao o launcher grava os ~940 MB do cliente na propria pasta. O usuario pode
; trocar a pasta no assistente. O UninstallDelete remove tambem o cliente baixado.

#define MyAppName "Tibia 2"
#define MyAppVersion "1.0"
#define MyAppPublisher "Tibia 2"
#define MyAppURL "https://tibia2ot.com/"
#define MyAppExeName "Tibia 2.exe"

[Setup]
AppId={{D04FCDFF-24FF-4AC1-8CE5-00C8C970F943}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={autodesktop}\{#MyAppName}
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
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Remove tambem o cliente baixado na primeira execucao (nao rastreado pelo Files).
Type: filesandordirs; Name: "{app}"
