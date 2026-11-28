; Agent Nexus installer hooks (docs/design.md section 9.7).
;
; Two ways to install (installMode "both"):
;
; * 仅为我安装 (CurrentUser): the window runs the core itself from %LOCALAPPDATA%\AgentNexus.
;   Before files are replaced or removed, the running manager, its plugins and the desktop shell
;   are stopped with the same clean-stop flow the updater uses (`--shutdown-manager`, 9.3).
; * 为所有用户安装 (AllUsers, administrator): the core is the Windows service AgentNexus
;   (LocalSystem) with its root in %ProgramData%\AgentNexus -- the same split as the Ubuntu
;   package. `--setup-system` moves a per-user root there once, prepares the runtime and
;   (re)starts the service; the window is a per-user viewer started at logon.
;
; The data root is kept unless the user ticks the uninstaller's "delete application data".

; This file is included before MultiUser.nsh: an existing per-user installation (which has no
; install-mode registry value) stays per-user when the in-app updater runs the installer passively.
; The all-users core updates itself with an explicit /AllUsers (agent_nexus/linux_update.py).
!define MULTIUSER_INSTALLMODE_DEFAULT_CURRENTUSER
!include WinMessages.nsh  ; WM_SETTINGCHANGE: open Explorer windows pick up the PATH that --install-cli changed

!macro ANX_SHUTDOWN_MANAGER
  IfFileExists "$INSTDIR\agent-nexus.exe" 0 +3
    DetailPrint "Stopping Agent Nexus..."
    ExecWait '"$INSTDIR\agent-nexus.exe" --shutdown-manager --timeout 20'
!macroend

; The service (it holds agent-nexus.exe) and every user's window.
!macro ANX_STOP_SYSTEM
  DetailPrint "Stopping the Agent Nexus service..."
  nsExec::Exec `powershell -NoProfile -ExecutionPolicy Bypass -Command "Stop-Service AgentNexus -Force -ErrorAction SilentlyContinue; $$s = Get-Service AgentNexus -ErrorAction SilentlyContinue; if ($$s) { $$s.WaitForStatus('Stopped', '00:00:40') }"`
  Pop $0
  nsExec::Exec 'taskkill /F /IM agent-nexus.exe'
  Pop $0
!macroend

!macro NSIS_HOOK_PREINSTALL
  ${If} $MultiUser.InstallMode == "AllUsers"
    !insertmacro ANX_STOP_SYSTEM
  ${Else}
    !insertmacro ANX_SHUTDOWN_MANAGER
  ${EndIf}
!macroend

; Unpack the runtime and build its Python environments now, from the bundled interpreter and
; package cache, so the first start after an install or update needs no preparation (9.7).
!macro NSIS_HOOK_POSTINSTALL
  ${If} $MultiUser.InstallMode == "AllUsers"
    DetailPrint "正在设置 Agent Nexus 系统服务（迁移数据、准备运行环境），请稍候..."
    nsExec::ExecToLog '"$INSTDIR\agent-nexus.exe" --setup-system'
    Pop $0
    ${If} $0 != 0
      DetailPrint "系统服务没有设置好（$0）：请以管理员身份运行 agent-nexus.exe --setup-system"
    ${EndIf}
  ${Else}
    DetailPrint "正在准备运行环境（释放文件并安装 Python 依赖），请稍候..."
    nsExec::ExecToLog '"$INSTDIR\agent-nexus.exe" --prepare-runtime'
    Pop $0
    ${If} $0 != 0
      DetailPrint "运行环境未能全部准备好（$0），首次打开 Agent Nexus 时会自动补全。"
    ${EndIf}
  ${EndIf}
  ; anx for the owner's own terminals: $INSTDIR\bin (anx.exe only) appended to this install scope's PATH
  ; (cli_path.rs; also refreshes the copy after an update). Terminals opened from now on find it.
  nsExec::ExecToLog '"$INSTDIR\agent-nexus.exe" --install-cli'
  Pop $0
  SendMessage ${HWND_BROADCAST} ${WM_SETTINGCHANGE} 0 "STR:Environment" /TIMEOUT=5000
  ${If} $0 == 0
    DetailPrint "新开的终端里可以直接运行 anx"
  ${EndIf}
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  ; A real uninstall: the running core first removes the agent-nexus entries it wrote into the CLIs' own MCP
  ; config (as each CLI's user), so no CLI is left pointing at a dead http://127.0.0.1:<port>/mcp.
  ${If} $UpdateMode <> 1
    ; (LogicLib, not a relative jump: a plugin call such as nsExec::Exec is several instructions)
    ${If} ${FileExists} "$INSTDIR\agent-nexus.exe"
      DetailPrint "Removing the Agent Nexus MCP entries from the CLIs' config..."
      ExecWait '"$INSTDIR\agent-nexus.exe" --unlink-mcp'
      ; and the PATH entry --install-cli added (only that one)
      nsExec::Exec '"$INSTDIR\agent-nexus.exe" --remove-cli'
      Pop $0
    ${EndIf}
    SendMessage ${HWND_BROADCAST} ${WM_SETTINGCHANGE} 0 "STR:Environment" /TIMEOUT=5000
  ${EndIf}
  ${If} $MultiUser.InstallMode == "AllUsers"
    ${If} $UpdateMode <> 1
      DetailPrint "Removing the Agent Nexus service..."
      nsExec::Exec '"$INSTDIR\agent-nexus.exe" --remove-system'
      Pop $0
    ${EndIf}
    !insertmacro ANX_STOP_SYSTEM
  ${Else}
    !insertmacro ANX_SHUTDOWN_MANAGER
  ${EndIf}
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  ; An update keeps the user's autostart choice; a real uninstall removes it.
  ${If} $UpdateMode <> 1
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "Agent Nexus"
    ; the anx folder (the uninstaller only removes the files it installed itself)
    Delete "$INSTDIR\bin\anx.exe"
    Delete "$INSTDIR\bin\anx.exe.old"
    Delete "$INSTDIR\bin\anx.exe.new"
    Delete "$INSTDIR\bin\anx"
    Delete "$INSTDIR\bin\anx.cmd"
    RMDir "$INSTDIR\bin"
  ${EndIf}
  ${If} $DeleteAppDataCheckboxState = 1
  ${AndIf} $UpdateMode <> 1
    ${If} $MultiUser.InstallMode == "AllUsers"
      SetShellVarContext all
      RmDir /r "$APPDATA\AgentNexus"
    ${Else}
      SetShellVarContext current
      RmDir /r "$LOCALAPPDATA\AgentNexus"
    ${EndIf}
  ${EndIf}
!macroend
