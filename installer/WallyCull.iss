; WallyCull Setup (Inno Setup 6.3 oder neuer). Gebaut von .github/workflows/setup.yml:
;   iscc /DAppVersion=0.6.0 installer\WallyCull.iss
; Erwartet das mitgelieferte Python (samt Paketen) unter build\python.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6B2E4D1A-8C3F-4A7B-9E51-2D7C0F3A9B64}
AppName=WallyCull
AppVersion={#AppVersion}
AppVerName=WallyCull {#AppVersion}
AppPublisher=WallyFrame
AppPublisherURL=https://wallyframe.com
DefaultDirName={localappdata}\Programs\WallyCull
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=WallyCull-Setup-{#AppVersion}
SetupIconFile=..\fotosort\symbol.ico
UninstallDisplayIcon={app}\python\WallyCull.exe
UninstallDisplayName=WallyCull
Compression=lzma2/ultra64
SolidCompression=yes
LZMANumBlockThreads=4
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
RestartApplications=no
ShowLanguageDialog=no
VersionInfoVersion={#AppVersion}
VersionInfoProductName=WallyCull

[Languages]
Name: "de"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktop"; Description: "Verknüpfung auf dem Desktop anlegen"; GroupDescription: "Zusätzlich:"

[InstallDelete]
; Bei Updates das Programm sauber ersetzen. Deine Daten (%LOCALAPPDATA%\WallyCull) bleiben unberührt.
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\fotosort"

[Files]
Source: "..\build\python\*"; DestDir: "{app}\python"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\fotosort\*"; DestDir: "{app}\fotosort"; Excludes: "__pycache__,*.pyc"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\config.yaml"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\requirements.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\requirements-ki.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\ANLEITUNG.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\vorbilder\LIESMICH.txt"; DestDir: "{localappdata}\WallyCull\vorbilder"; Flags: onlyifdoesntexist

[Icons]
Name: "{autoprograms}\WallyCull"; Filename: "{app}\python\WallyCull.exe"; Parameters: "-m fotosort"; WorkingDir: "{app}"; IconFilename: "{app}\fotosort\symbol.ico"; AppUserModelID: "WallyFrame.WallyCull"; Comment: "Fotos nach Qualität und Wert sortieren"
Name: "{autodesktop}\WallyCull"; Filename: "{app}\python\WallyCull.exe"; Parameters: "-m fotosort"; WorkingDir: "{app}"; IconFilename: "{app}\fotosort\symbol.ico"; AppUserModelID: "WallyFrame.WallyCull"; Comment: "Fotos nach Qualität und Wert sortieren"; Tasks: desktop

[Run]
Filename: "{app}\python\WallyCull.exe"; Parameters: "-m fotosort"; WorkingDir: "{app}"; Description: "WallyCull jetzt starten"; Flags: postinstall nowait

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
var
  KiSeite: TOutputProgressWizardPage;
  KiSchritt: String;

procedure KiAusgabe(const S: String; const Error, FirstLine: Boolean);
var
  Zeile: String;
begin
  Zeile := Trim(S);
  if Zeile = '' then
    Exit;
  if Pos('SCHRITT ', Zeile) = 1 then
    KiSchritt := Copy(Zeile, 9, Length(Zeile) - 8);
  KiSeite.SetText(KiSchritt, Zeile);
end;

procedure KiEinrichten;
var
  Ergebnis: Integer;
  Python: String;
  Ok: Boolean;
begin
  Python := ExpandConstant('{app}\python\python.exe');
  if WizardSilent then
  begin
    { Update im Hintergrund: prüft nur, ob die KI-Pakete noch passen, und ergänzt sie bei Bedarf }
    Exec(Python, '-u -m fotosort ki-installieren', ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Ergebnis);
    Exit;
  end;
  KiSchritt := 'KI wird vorbereitet ...';
  KiSeite := CreateOutputProgressPage('KI wird eingerichtet',
    'WallyCull lädt PyTorch für deine Grafikkarte, CLIP und die Bewertungsmodelle (einmalig ca. 5 GB). ' +
    'Je nach Internetverbindung dauert das einige Minuten. Alles läuft danach offline auf deinem PC.');
  KiSeite.Show;
  try
    KiSeite.ProgressBar.Style := npbstMarquee;
    Ok := ExecAndLogOutput(Python, '-u -m fotosort ki-installieren', ExpandConstant('{app}'), SW_HIDE,
      ewWaitUntilTerminated, Ergebnis, @KiAusgabe);
    if (not Ok) or (Ergebnis <> 0) then
      MsgBox('Die KI konnte nicht vollständig eingerichtet werden (Code ' + IntToStr(Ergebnis) + ').' + #13#10 +
        'WallyCull funktioniert trotzdem. In der App kannst du es rechts unter "KI einrichten" erneut versuchen.',
        mbInformation, MB_OK);
  finally
    KiSeite.Hide;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    KiEinrichten;
end;
