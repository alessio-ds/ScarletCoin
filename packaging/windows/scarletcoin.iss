; ScarletCoin desktop installer for Windows.
;
; Compile it from the repository root, after running tools/build_release.py:
;
;   iscc /DMyAppVersion=2.0.0 packaging\windows\scarletcoin.iss
;
; The bundle built by PyInstaller (release\bundle) is packaged as-is, so the
; wallet and the miner can still find the node they run in the background.

#ifndef MyAppVersion
  #error "Define MyAppVersion, e.g. /DMyAppVersion=2.0.0"
#endif

#define MyAppName "ScarletCoin"
#define MyAppPublisher "Alessio Della Santa"
#define MyAppURL "https://github.com/alessio-ds/ScarletCoin"

[Setup]
; The script lives in packaging/windows; make paths like release\bundle
; resolve from the repository root.
SourceDir=..\..
AppId={{9D6447AD-B4F8-4D36-B5AD-31F27A991CCF}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\ScarletCoin
DefaultGroupName=ScarletCoin
DisableProgramGroupPage=yes
OutputDir=release
OutputBaseFilename=ScarletCoin-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\scarlet-wallet-gui.exe

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"; GroupDescription: "Additional icons:"

[Files]
Source: "release\bundle\*"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName} Wallet"; Filename: "{app}\scarlet-wallet-gui.exe"
Name: "{autoprograms}\{#MyAppName} Miner"; Filename: "{app}\scarlet-miner-gui.exe"
Name: "{autodesktop}\{#MyAppName} Wallet"; Filename: "{app}\scarlet-wallet-gui.exe"; Tasks: desktopicon
Name: "{autodesktop}\{#MyAppName} Miner"; Filename: "{app}\scarlet-miner-gui.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\scarlet-wallet-gui.exe"; Description: "Launch the {#MyAppName} wallet"; Flags: nowait postinstall skipifsilent
