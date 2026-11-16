; Agent Nexus installer hooks (docs/design.md section 9.7).
;
; Before files are replaced or removed, the running manager, its plugins and the
; desktop shell are stopped with the same clean-stop flow the updater uses
; (`agent-nexus.exe --shutdown-manager`, section 9.3). The data root
; (%LOCALAPPDATA%\AgentNexus or an explicit --root) is kept unless the user ticks
; the uninstaller's "delete application data" checkbox.

!macro ANX_SHUTDOWN_MANAGER
  IfFileExists "$INSTDIR\agent-nexus.exe" 0 +3
    DetailPrint "Stopping Agent Nexus..."
    ExecWait '"$INSTDIR\agent-nexus.exe" --shutdown-manager --timeout 20'
!macroend

!macro NSIS_HOOK_PREINSTALL
  !insertmacro ANX_SHUTDOWN_MANAGER
!macroend

; Unpack the runtime and build its Python environments now, from the bundled
; interpreter and package cache, so the first start after an install or update
; needs no preparation (section 9.7). A failure is not fatal: startup finishes
; whatever is missing.
!macro NSIS_HOOK_POSTINSTALL
  DetailPrint "正在准备运行环境（释放文件并安装 Python 依赖），请稍候..."
  nsExec::ExecToLog '"$INSTDIR\agent-nexus.exe" --prepare-runtime'
  Pop $0
  ${If} $0 != 0
    DetailPrint "运行环境未能全部准备好（$0），首次打开 Agent Nexus 时会自动补全。"
  ${EndIf}
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  !insertmacro ANX_SHUTDOWN_MANAGER
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  ; An update keeps the user's autostart choice; a real uninstall removes it.
  ${If} $UpdateMode <> 1
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "Agent Nexus"
  ${EndIf}
  ${If} $DeleteAppDataCheckboxState = 1
  ${AndIf} $UpdateMode <> 1
    SetShellVarContext current
    RmDir /r "$LOCALAPPDATA\AgentNexus"
  ${EndIf}
!macroend
