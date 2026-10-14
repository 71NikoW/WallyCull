; WallyCull setup (Inno Setup 6.3 or newer). Built by .github/workflows/setup.yml:
;   iscc /DAppVersion=0.7.0 installer\WallyCull.iss
; Expects the bundled Python (with its packages) in build\python.

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
SetupIconFile=..\wallycull\icon.ico
UninstallDisplayIcon={app}\python\WallyCull.exe
UninstallDisplayName=WallyCull
; lzma2/max instead of ultra64: ultra64 runs out of memory on the build machine
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
RestartApplications=no
ShowLanguageDialog=no
VersionInfoVersion={#AppVersion}
VersionInfoProductName=WallyCull

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktop"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[InstallDelete]
; Replace the program cleanly on updates. Your data (%LOCALAPPDATA%\WallyCull) is never touched.
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\wallycull"
; Left over from the German versions up to 0.6
Type: filesandordirs; Name: "{app}\fotosort"
Type: files; Name: "{app}\ANLEITUNG.md"
Type: files; Name: "{app}\requirements-ki.txt"

[Files]
Source: "..\build\python\*"; DestDir: "{app}\python"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\wallycull\*"; DestDir: "{app}\wallycull"; Excludes: "__pycache__,*.pyc"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\config.yaml"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\requirements.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\requirements-ai.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\docs\user-guide.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "references-README.txt"; DestDir: "{localappdata}\WallyCull\references"; DestName: "README.txt"; Flags: onlyifdoesntexist

[Icons]
Name: "{autoprograms}\WallyCull"; Filename: "{app}\python\WallyCull.exe"; Parameters: "-m wallycull"; WorkingDir: "{app}"; IconFilename: "{app}\wallycull\icon.ico"; AppUserModelID: "WallyFrame.WallyCull"; Comment: "Sort photos by quality and value"
Name: "{autodesktop}\WallyCull"; Filename: "{app}\python\WallyCull.exe"; Parameters: "-m wallycull"; WorkingDir: "{app}"; IconFilename: "{app}\wallycull\icon.ico"; AppUserModelID: "WallyFrame.WallyCull"; Comment: "Sort photos by quality and value"; Tasks: desktop

[Run]
Filename: "{app}\python\WallyCull.exe"; Parameters: "-m wallycull"; WorkingDir: "{app}"; Description: "Start WallyCull now"; Flags: postinstall nowait

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
var
  AiPage: TOutputProgressWizardPage;
  AiStep: String;

procedure AiOutput(const S: String; const Error, FirstLine: Boolean);
var
  Line: String;
begin
  Line := Trim(S);
  if Line = '' then
    Exit;
  if Pos('STEP ', Line) = 1 then
    AiStep := Copy(Line, 6, Length(Line) - 5);
  AiPage.SetText(AiStep, Line);
end;

procedure SetUpAi;
var
  ResultCode: Integer;
  Python: String;
  Ok: Boolean;
begin
  Python := ExpandConstant('{app}\python\python.exe');
  if WizardSilent then
  begin
    { Update in the background: only checks that the AI packages still match and adds them if needed }
    Exec(Python, '-u -m wallycull setup-ai', ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exit;
  end;
  AiStep := 'Preparing the AI ...';
  AiPage := CreateOutputProgressPage('Setting up the AI',
    'WallyCull downloads PyTorch for your graphics card, CLIP and the rating models (once, about 5 GB). ' +
    'Depending on your internet connection this takes a few minutes. Afterwards everything runs offline on your PC.');
  AiPage.Show;
  try
    AiPage.ProgressBar.Style := npbstMarquee;
    Ok := ExecAndLogOutput(Python, '-u -m wallycull setup-ai', ExpandConstant('{app}'), SW_HIDE,
      ewWaitUntilTerminated, ResultCode, @AiOutput);
    if (not Ok) or (ResultCode <> 0) then
      MsgBox('The AI could not be set up completely (code ' + IntToStr(ResultCode) + ').' + #13#10 +
        'WallyCull still works. You can try again in the app with "Set up AI" on the right.',
        mbInformation, MB_OK);
  finally
    AiPage.Hide;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    SetUpAi;
end;
