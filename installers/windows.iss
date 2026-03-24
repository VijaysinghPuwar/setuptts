; =============================================================================
;  VoiceCraft — Inno Setup Installer Script
;  Inno Setup 6 required: https://jrsoftware.org/isdl.php
; =============================================================================

#define AppName      "VoiceCraft"
#define AppVersion   "1.0.0"
#define AppPublisher "VoiceCraft"
#define AppURL       "https://github.com/your-username/voicecraft"
#define AppExe       "VoiceCraft.exe"
#define SourceDir    "..\dist"

[Setup]
AppId={{A3F7E2B1-4D8C-4E5F-9A1B-2C3D4E5F6A7B}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
AllowNoIcons=yes
OutputDir=out
OutputBaseFilename=VoiceCraftSetup
SetupIconFile=..\app\assets\icons\app.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64

; Visual: modern UI style
WizardImageFile=compiler:WizModernImage.bmp
WizardSmallImageFile=compiler:WizModernSmallImage.bmp

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon";   Description: "{cm:CreateDesktopIcon}";   GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "quicklaunch";   Description: "Add to Quick Launch bar";   GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked; OnlyBelowVersion: 6.1

[Files]
; The PyInstaller single-file EXE
Source: "{#SourceDir}\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion

; Optional: if you switch to one-folder build, use this instead:
; Source: "{#SourceDir}\VoiceCraft\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}";                       Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}";             Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";                 Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: quicklaunch

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Clean up user data only if the user wants — we leave it alone by default.
; Type: filesandordirs; Name: "{localappdata}\VoiceCraft"

[Code]
// Nothing custom needed — Inno Setup handles everything.
