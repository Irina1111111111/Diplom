if ([string]::IsNullOrEmpty($Env:INTELLIJ_TERMINAL_COMMAND_BLOCKS)) {
  return
}

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# Import PSReadLine module forcefully if it was skipped because of active Screen Reader.
# PowerShell is skipping it when Screen Reader is active because they consider it as not well accessibility friendly.
# PSReadLine module is required for our shell integration. Namely for command_started event and command history filtering.
# PSReadLine module is shipped together PowerShell with from version 5.1.
# Since PowerShell 5.1 is bundled in both Windows 10 and 11, it suits us well and this module must be present.
if ((Get-Module -Name PSReadLine) -eq $null) {
  Import-Module PSReadLine
}

function Global:__JetBrainsIntellijEncode([object]$value) {
  # Value that we need to encode is not always a string.
  # Generator result can be an array of objects, for example, type of the `git config --get-regexp "^alias"` command output is Object[].
  # So, we need to use Out-String cmdlet to transform the Object[] to the string like in the terminal output.
  # Otherwise GetBytes call will transform it in some other way and we will lose the line brakes.
  $ValueAsString = if ($value -is [string]) { $value } else { ($value | Out-String).trim() }
  $Bytes = [System.Text.Encoding]::UTF8.GetBytes($ValueAsString)
  return [System.BitConverter]::ToString($Bytes).Replace("-", "")
}

function Global:__JetBrainsIntellijOSC([string]$body) {
  return "$([char]0x1B)]1341;$body`a"
  # ConPTY processes custom OSC asynchronously with regular output.
  # Let's use C1 control codes for OSC to fool ConPTY and output
  # the escape sequence in proper position of the regular output.
  # return "$([char]0x9D)1341;$body$([char]0x9C)"
}

function Global:__JetBrainsIntellijGetCommandEndMarker() {
  $CommandEndMarker = $Env:JETBRAINS_INTELLIJ_COMMAND_END_MARKER
  if ($CommandEndMarker -eq $null) {
    $CommandEndMarker = ""
  }
  return $CommandEndMarker
}

$Global:__JetBrainsIntellijTerminalInitialized=$false
$Global:__JetBrainsIntellijGeneratorRunning=$false

if (Test-Path Function:\Prompt) {
  Rename-Item Function:\Prompt Global:__JetBrainsIntellijOriginalPrompt
}
else {
  function Global:__JetBrainsIntellijOriginalPrompt() { return "" }
}

function Global:Prompt() {
  $Success = $?
  $ExitCode = $Global:LastExitCode
  $Global:LastExitCode = 0
  if ($Global:__JetBrainsIntellijGeneratorRunning) {
    $Global:__JetBrainsIntellijGeneratorRunning = $false
    # Hide internal command in the built-in session history.
    # See "Set-PSReadLineOption -AddToHistoryHandler" for hiding same commands in the PSReadLine history.
    Clear-History -CommandLine "__jetbrains_intellij_run_generator*"
    return ""
  }

  $Result = ""
  $CommandEndMarker = Global:__JetBrainsIntellijGetCommandEndMarker
  $PromptStateOSC = Global:__JetBrainsIntellijCreatePromptStateOSC
  if ($__JetBrainsIntellijTerminalInitialized) {
    if (($ExitCode -eq $null) -or ($ExitCode -eq 0 -and -not $Success)) {
      $ExitCode = if ($Success) { 0 } else { 1 }
    }
    if ($Env:JETBRAINS_INTELLIJ_TERMINAL_DEBUG_LOG_LEVEL) {
      [Console]::WriteLine("command_finished exit_code=$ExitCode")
    }
    $CommandFinishedEvent = Global:__JetBrainsIntellijOSC "command_finished;exit_code=$ExitCode"
    $Result = $PromptStateOSC + $CommandFinishedEvent + $CommandEndMarker
  }
  else {
    # For some reason there is no error if I delete the history file, just an empty string returned.
    # There can be a check for file existence using Test-Path cmdlet, but if I add it, the prompt is failed to initialize.
    $History = Get-Content -Raw (Get-PSReadlineOption).HistorySavePath
    $HistoryOSC = Global:__JetBrainsIntellijOSC "command_history;history_string=$(__JetBrainsIntellijEncode $History)"

    $ShellInfo = Global:__JetBrainsIntellijCollectShellInfo
    $Global:__JetBrainsIntellijTerminalInitialized = $true
    if ($Env:JETBRAINS_INTELLIJ_TERMINAL_DEBUG_LOG_LEVEL) {
      [Console]::WriteLine("initialized")
    }
    $InitializedEvent = Global:__JetBrainsIntellijOSC "initialized;shell_info=$(__JetBrainsIntellijEncode $ShellInfo)"
    $Result = $PromptStateOSC + $HistoryOSC + $InitializedEvent + $CommandEndMarker
  }
  return $Result
}

function Global:__JetBrainsIntellijCreatePromptStateOSC() {
  # Remember the exit code, because it can be changed in a result of git operations
  $RealExitCode = $Global:LastExitCode

  $CurrentDirectory = (Get-Location).Path
  $UserName = if ($Env:UserName -ne $null) { $Env:UserName } else { "" }
  $UserHome = if ($Env:HOME -ne $null) { $Env:HOME } else { "" }
  $GitBranch = ""
  if (Get-Command "git.exe" -ErrorAction SilentlyContinue) {
    $GitBranch = git.exe symbolic-ref --short HEAD 2>$null
    if ($GitBranch -eq $null) {
      # get the current revision hash, if not on the branch
      $GitBranch = git.exe rev-parse --short HEAD 2>$null
      if ($GitBranch -eq $null) {
        $GitBranch = ""
      }
    }
  }
  $VirtualEnv = if ($Env:VIRTUAL_ENV -ne $null) { $Env:VIRTUAL_ENV } else { "" }
  $CondaEnv = if ($Env:CONDA_DEFAULT_ENV -ne $null) { $Env:CONDA_DEFAULT_ENV } else { "" }
  $OriginalPrompt = __JetBrainsIntellijOriginalPrompt 6>&1
  $StateOSC = Global:__JetBrainsIntellijOSC ("prompt_state_updated;" +
    "current_directory=$(__JetBrainsIntellijEncode $CurrentDirectory);" +
    "user_name=$(__JetBrainsIntellijEncode $UserName);" +
    "user_home=$(__JetBrainsIntellijEncode $UserHome);" +
    "git_branch=$(__JetBrainsIntellijEncode $GitBranch);" +
    "virtual_env=$(__JetBrainsIntellijEncode $VirtualEnv);" +
    "conda_env=$(__JetBrainsIntellijEncode $CondaEnv);" +
    "original_prompt=$(__JetBrainsIntellijEncode $OriginalPrompt)")

  $Global:LastExitCode = $RealExitCode
  return $StateOSC
}

function Global:__JetBrainsIntellijCollectShellInfo() {
  $ShellVersion = if ($PSVersionTable -ne $null) { $PSVersionTable.PSVersion.toString() } else { "" }
  $IsStarship = ($Env:STARSHIP_START_TIME -ne $null) -or ($Env:STARSHIP_SHELL -ne $null) -or ($Env:STARSHIP_SESSION_KEY -ne $null)
  $OhMyPoshTheme = ""
  if (($Env:POSH_THEME -ne $null) -or ($Env:POSH_PID -ne $null) -or ($Env:POSH_SHELL_VERSION -ne $null)) {
    $OhMyPoshTheme = if ($Env:POSH_THEME -ne $null) { $Env:POSH_THEME } else { "default" }
  }
  $ShellInfo = [PSCustomObject]@{
    shellVersion = $ShellVersion
    isStarship = $IsStarship
    ohMyPoshTheme = $OhMyPoshTheme
  }
  return $ShellInfo | ConvertTo-Json -Compress
}

function Global:__JetBrainsIntellij_ClearAllAndMoveCursorToTopLeft() {
  [Console]::Clear()
}

function Global:__jetbrains_intellij_run_generator([int]$RequestId, [string]$Command) {
  $Global:__JetBrainsIntellijGeneratorRunning = $true
  # Remember the exit code, because it can be changed in a result of generator command execution
  $RealExitCode = $Global:LastExitCode
  $Global:LastExitCode = 0

  $Success = $false
  $Result = ""
  # Redirect the stderr of generator command to stdout. Invoke-Expression can't take all the output for external applications.
  $AdjustedCommand = $Command + " 2>&1"
  # Catch the exceptions in a different ways, because exceptions
  # inside Invoke-Expression and inside $AdjustedCommand can be propagated differently.
  try {
    $Result = Invoke-Expression $AdjustedCommand -ErrorVariable Exception
    if($Exception -ne $null){
      Throw $Exception
    }
    $Success = $true
  }
  catch {
    $Result = $_
  }
  $ExitCode = $Global:LastExitCode
  if (($ExitCode -eq $null) -or ($ExitCode -eq 0 -and -not $Success)) {
    $ExitCode = if ($Success) { 0 } else { 1 }
  }

  $ResultOSC = Global:__JetBrainsIntellijOSC "generator_finished;request_id=$RequestId;result=$(__JetBrainsIntellijEncode $Result);exit_code=$ExitCode"
  $CommandEndMarker = Global:__JetBrainsIntellijGetCommandEndMarker
  [Console]::Write($CommandEndMarker + $ResultOSC)
  $Global:LastExitCode = $RealExitCode
}

function Global:__JetBrainsIntellijGetCompletions([string]$Command, [int]$CursorIndex) {
  $Completions = TabExpansion2 -inputScript $Command -cursorColumn $CursorIndex
  if ($null -ne $Completions) {
    $CompletionsJson = $Completions | ConvertTo-Json -Compress
  }
  else {
    $CompletionsJson = ""
  }
  return $CompletionsJson
}

function Global:__jetbrains_intellij_get_directory_files([string]$Path) {
  # This setting is effective only in the scope of this function.
  $ErrorActionPreference="Stop"
  $Files = Get-ChildItem -Force -Path $Path | Where { $_ -is [System.IO.FileSystemInfo] }
  $Separator = [System.IO.Path]::DirectorySeparatorChar
  $FileNames = $Files | ForEach-Object { if ($_ -is [System.IO.DirectoryInfo]) { $_.Name + $Separator } else { $_.Name } }
  $FilesString = $FileNames -join "`n"
  return $FilesString
}

function Global:__jetbrains_intellij_get_aliases() {
  $Global:__JetBrainsIntellijGeneratorRunning = $true
  $Aliases = Get-Alias | ForEach-Object { [PSCustomObject]@{ name = $_.Name; definition = $_.Definition } }
  return $Aliases | ConvertTo-Json -Compress
}

function Global:__jetbrains_intellij_get_environment() {
  $Global:__JetBrainsIntellijGeneratorRunning = $true
  $FunctionTypes = @("Function", "Filter", "ExternalScript", "Script")
  $Functions = Get-Command -ListImported -CommandType $FunctionTypes
  $Cmdlets = Get-Command -ListImported -CommandType Cmdlet
  $Commands = Get-Command -ListImported -CommandType Application
  $Aliases = Global:__jetbrains_intellij_get_aliases

  $EnvObject = [PSCustomObject]@{
    envs = ""
    keywords = ""
    builtins = ($Cmdlets | ForEach-Object { $_.Name }) -join "`n"
    functions = ($Functions | ForEach-Object { $_.Name }) -join "`n"
    commands = ($Commands | ForEach-Object { $_.Name }) -join "`n"
    aliases = $Aliases
  }
  $EnvJson = $EnvObject | ConvertTo-Json -Compress
  return $EnvJson
}

function Global:__JetBrainsIntellijIsGeneratorCommand([string]$Command) {
  return $Command -like "__jetbrains_intellij_run_generator*"
}

# Override the clear cmdlet to handle it on IDE side and remove the blocks
function Global:Clear-Host() {
  $OSC = Global:__JetBrainsIntellijOSC "clear_invoked"
  [Console]::Write($OSC)
}
function Global:clear() {
  Global:Clear-Host
}

$Global:__JetBrainsIntellijOriginalPSConsoleHostReadLine = $function:PSConsoleHostReadLine

function Global:PSConsoleHostReadLine {
  $OriginalReadLine = $Global:__JetBrainsIntellijOriginalPSConsoleHostReadLine.Invoke()
  if (__JetBrainsIntellijIsGeneratorCommand $OriginalReadLine) {
    return $OriginalReadLine
  }

  $CurrentDirectory = (Get-Location).Path
  if ($Env:JETBRAINS_INTELLIJ_TERMINAL_DEBUG_LOG_LEVEL) {
    [Console]::WriteLine("command_started $OriginalReadLine")
  }
  $CommandStartedOSC = Global:__JetBrainsIntellijOSC "command_started;command=$(__JetBrainsIntellijEncode $OriginalReadLine);current_directory=$(__JetBrainsIntellijEncode $CurrentDirectory)"
  [Console]::Write($CommandStartedOSC)
  Global:__JetBrainsIntellij_ClearAllAndMoveCursorToTopLeft
  return $OriginalReadLine
}

$Global:__JetBrainsIntellijOriginalAddToHistoryHandler = (Get-PSReadLineOption).AddToHistoryHandler

Set-PSReadLineOption -AddToHistoryHandler {
  param([string]$Command)
  if (__JetBrainsIntellijIsGeneratorCommand $Command) {
    return $false
  }
  if ($Global:__JetBrainsIntellijOriginalAddToHistoryHandler -ne $null) {
    return $Global:__JetBrainsIntellijOriginalAddToHistoryHandler.Invoke($Command)
  }
  return $true
}

# SIG # Begin signature block
# MIIobAYJKoZIhvcNAQcCoIIoXTCCKFkCAQExDzANBglghkgBZQMEAgEFADB5Bgor
# BgEEAYI3AgEEoGswaTA0BgorBgEEAYI3AgEeMCYCAwEAAAQQH8w7YFlLCE63JNLG
# KX7zUQIBAAIBAAIBAAIBAAIBADAxMA0GCWCGSAFlAwQCAQUABCD+YdWsWDO8QULf
# rod9MNIzZ94WBfXYKzItxQZ2z6k9XaCCIXgwggWNMIIEdaADAgECAhAOmxiO+dAt
# 5+/bUOIIQBhaMA0GCSqGSIb3DQEBDAUAMGUxCzAJBgNVBAYTAlVTMRUwEwYDVQQK
# EwxEaWdpQ2VydCBJbmMxGTAXBgNVBAsTEHd3dy5kaWdpY2VydC5jb20xJDAiBgNV
# BAMTG0RpZ2lDZXJ0IEFzc3VyZWQgSUQgUm9vdCBDQTAeFw0yMjA4MDEwMDAwMDBa
# Fw0zMTExMDkyMzU5NTlaMGIxCzAJBgNVBAYTAlVTMRUwEwYDVQQKEwxEaWdpQ2Vy
# dCBJbmMxGTAXBgNVBAsTEHd3dy5kaWdpY2VydC5jb20xITAfBgNVBAMTGERpZ2lD
# ZXJ0IFRydXN0ZWQgUm9vdCBHNDCCAiIwDQYJKoZIhvcNAQEBBQADggIPADCCAgoC
# ggIBAL/mkHNo3rvkXUo8MCIwaTPswqclLskhPfKK2FnC4SmnPVirdprNrnsbhA3E
# MB/zG6Q4FutWxpdtHauyefLKEdLkX9YFPFIPUh/GnhWlfr6fqVcWWVVyr2iTcMKy
# unWZanMylNEQRBAu34LzB4TmdDttceItDBvuINXJIB1jKS3O7F5OyJP4IWGbNOsF
# xl7sWxq868nPzaw0QF+xembud8hIqGZXV59UWI4MK7dPpzDZVu7Ke13jrclPXuU1
# 5zHL2pNe3I6PgNq2kZhAkHnDeMe2scS1ahg4AxCN2NQ3pC4FfYj1gj4QkXCrVYJB
# MtfbBHMqbpEBfCFM1LyuGwN1XXhm2ToxRJozQL8I11pJpMLmqaBn3aQnvKFPObUR
# WBf3JFxGj2T3wWmIdph2PVldQnaHiZdpekjw4KISG2aadMreSx7nDmOu5tTvkpI6
# nj3cAORFJYm2mkQZK37AlLTSYW3rM9nF30sEAMx9HJXDj/chsrIRt7t/8tWMcCxB
# YKqxYxhElRp2Yn72gLD76GSmM9GJB+G9t+ZDpBi4pncB4Q+UDCEdslQpJYls5Q5S
# UUd0viastkF13nqsX40/ybzTQRESW+UQUOsxxcpyFiIJ33xMdT9j7CFfxCBRa2+x
# q4aLT8LWRV+dIPyhHsXAj6KxfgommfXkaS+YHS312amyHeUbAgMBAAGjggE6MIIB
# NjAPBgNVHRMBAf8EBTADAQH/MB0GA1UdDgQWBBTs1+OC0nFdZEzfLmc/57qYrhwP
# TzAfBgNVHSMEGDAWgBRF66Kv9JLLgjEtUYunpyGd823IDzAOBgNVHQ8BAf8EBAMC
# AYYweQYIKwYBBQUHAQEEbTBrMCQGCCsGAQUFBzABhhhodHRwOi8vb2NzcC5kaWdp
# Y2VydC5jb20wQwYIKwYBBQUHMAKGN2h0dHA6Ly9jYWNlcnRzLmRpZ2ljZXJ0LmNv
# bS9EaWdpQ2VydEFzc3VyZWRJRFJvb3RDQS5jcnQwRQYDVR0fBD4wPDA6oDigNoY0
# aHR0cDovL2NybDMuZGlnaWNlcnQuY29tL0RpZ2lDZXJ0QXNzdXJlZElEUm9vdENB
# LmNybDARBgNVHSAECjAIMAYGBFUdIAAwDQYJKoZIhvcNAQEMBQADggEBAHCgv0Nc
# Vec4X6CjdBs9thbX979XB72arKGHLOyFXqkauyL4hxppVCLtpIh3bb0aFPQTSnov
# Lbc47/T/gLn4offyct4kvFIDyE7QKt76LVbP+fT3rDB6mouyXtTP0UNEm0Mh65Zy
# oUi0mcudT6cGAxN3J0TU53/oWajwvy8LpunyNDzs9wPHh6jSTEAZNUZqaVSwuKFW
# juyk1T3osdz9HNj0d1pcVIxv76FQPfx2CWiEn2/K2yCNNWAcAgPLILCsWKAOQGPF
# mCLBsln1VWvPJ6tsds5vIy30fnFqI2si/xK4VC0nftg62fC2h5b9W9FcrBjDTZ9z
# twGpn1eqXijiuZQwggauMIIElqADAgECAhAHNje3JFR82Ees/ShmKl5bMA0GCSqG
# SIb3DQEBCwUAMGIxCzAJBgNVBAYTAlVTMRUwEwYDVQQKEwxEaWdpQ2VydCBJbmMx
# GTAXBgNVBAsTEHd3dy5kaWdpY2VydC5jb20xITAfBgNVBAMTGERpZ2lDZXJ0IFRy
# dXN0ZWQgUm9vdCBHNDAeFw0yMjAzMjMwMDAwMDBaFw0zNzAzMjIyMzU5NTlaMGMx
# CzAJBgNVBAYTAlVTMRcwFQYDVQQKEw5EaWdpQ2VydCwgSW5jLjE7MDkGA1UEAxMy
# RGlnaUNlcnQgVHJ1c3RlZCBHNCBSU0E0MDk2IFNIQTI1NiBUaW1lU3RhbXBpbmcg
# Q0EwggIiMA0GCSqGSIb3DQEBAQUAA4ICDwAwggIKAoICAQDGhjUGSbPBPXJJUVXH
# JQPE8pE3qZdRodbSg9GeTKJtoLDMg/la9hGhRBVCX6SI82j6ffOciQt/nR+eDzMf
# UBMLJnOWbfhXqAJ9/UO0hNoR8XOxs+4rgISKIhjf69o9xBd/qxkrPkLcZ47qUT3w
# 1lbU5ygt69OxtXXnHwZljZQp09nsad/ZkIdGAHvbREGJ3HxqV3rwN3mfXazL6IRk
# tFLydkf3YYMZ3V+0VAshaG43IbtArF+y3kp9zvU5EmfvDqVjbOSmxR3NNg1c1eYb
# qMFkdECnwHLFuk4fsbVYTXn+149zk6wsOeKlSNbwsDETqVcplicu9Yemj052FVUm
# cJgmf6AaRyBD40NjgHt1biclkJg6OBGz9vae5jtb7IHeIhTZgirHkr+g3uM+onP6
# 5x9abJTyUpURK1h0QCirc0PO30qhHGs4xSnzyqqWc0Jon7ZGs506o9UD4L/wojzK
# QtwYSH8UNM/STKvvmz3+DrhkKvp1KCRB7UK/BZxmSVJQ9FHzNklNiyDSLFc1eSuo
# 80VgvCONWPfcYd6T/jnA+bIwpUzX6ZhKWD7TA4j+s4/TXkt2ElGTyYwMO1uKIqjB
# Jgj5FBASA31fI7tk42PgpuE+9sJ0sj8eCXbsq11GdeJgo1gJASgADoRU7s7pXche
# MBK9Rp6103a50g5rmQzSM7TNsQIDAQABo4IBXTCCAVkwEgYDVR0TAQH/BAgwBgEB
# /wIBADAdBgNVHQ4EFgQUuhbZbU2FL3MpdpovdYxqII+eyG8wHwYDVR0jBBgwFoAU
# 7NfjgtJxXWRM3y5nP+e6mK4cD08wDgYDVR0PAQH/BAQDAgGGMBMGA1UdJQQMMAoG
# CCsGAQUFBwMIMHcGCCsGAQUFBwEBBGswaTAkBggrBgEFBQcwAYYYaHR0cDovL29j
# c3AuZGlnaWNlcnQuY29tMEEGCCsGAQUFBzAChjVodHRwOi8vY2FjZXJ0cy5kaWdp
# Y2VydC5jb20vRGlnaUNlcnRUcnVzdGVkUm9vdEc0LmNydDBDBgNVHR8EPDA6MDig
# NqA0hjJodHRwOi8vY3JsMy5kaWdpY2VydC5jb20vRGlnaUNlcnRUcnVzdGVkUm9v
# dEc0LmNybDAgBgNVHSAEGTAXMAgGBmeBDAEEAjALBglghkgBhv1sBwEwDQYJKoZI
# hvcNAQELBQADggIBAH1ZjsCTtm+YqUQiAX5m1tghQuGwGC4QTRPPMFPOvxj7x1Bd
# 4ksp+3CKDaopafxpwc8dB+k+YMjYC+VcW9dth/qEICU0MWfNthKWb8RQTGIdDAiC
# qBa9qVbPFXONASIlzpVpP0d3+3J0FNf/q0+KLHqrhc1DX+1gtqpPkWaeLJ7giqzl
# /Yy8ZCaHbJK9nXzQcAp876i8dU+6WvepELJd6f8oVInw1YpxdmXazPByoyP6wCeC
# RK6ZJxurJB4mwbfeKuv2nrF5mYGjVoarCkXJ38SNoOeY+/umnXKvxMfBwWpx2cYT
# gAnEtp/Nh4cku0+jSbl3ZpHxcpzpSwJSpzd+k1OsOx0ISQ+UzTl63f8lY5knLD0/
# a6fxZsNBzU+2QJshIUDQtxMkzdwdeDrknq3lNHGS1yZr5Dhzq6YBT70/O3itTK37
# xJV77QpfMzmHQXh6OOmc4d0j/R0o08f56PGYX/sr2H7yRp11LB4nLCbbbxV7HhmL
# NriT1ObyF5lZynDwN7+YAN8gFk8n+2BnFqFmut1VwDophrCYoCvtlUG3OtUVmDG0
# YgkPCr2B2RP+v6TR81fZvAT6gt4y3wSJ8ADNXcL50CN/AAvkdgIm2fBldkKmKYcJ
# RyvmfxqkhQ/8mJb2VVQrH4D6wPIOK+XW+6kvRBVK5xMOHds3OBqhK/bt1nz8MIIG
# sDCCBJigAwIBAgIQCK1AsmDSnEyfXs2pvZOu2TANBgkqhkiG9w0BAQwFADBiMQsw
# CQYDVQQGEwJVUzEVMBMGA1UEChMMRGlnaUNlcnQgSW5jMRkwFwYDVQQLExB3d3cu
# ZGlnaWNlcnQuY29tMSEwHwYDVQQDExhEaWdpQ2VydCBUcnVzdGVkIFJvb3QgRzQw
# HhcNMjEwNDI5MDAwMDAwWhcNMzYwNDI4MjM1OTU5WjBpMQswCQYDVQQGEwJVUzEX
# MBUGA1UEChMORGlnaUNlcnQsIEluYy4xQTA/BgNVBAMTOERpZ2lDZXJ0IFRydXN0
# ZWQgRzQgQ29kZSBTaWduaW5nIFJTQTQwOTYgU0hBMzg0IDIwMjEgQ0ExMIICIjAN
# BgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEA1bQvQtAorXi3XdU5WRuxiEL1M4zr
# PYGXcMW7xIUmMJ+kjmjYXPXrNCQH4UtP03hD9BfXHtr50tVnGlJPDqFX/IiZwZHM
# gQM+TXAkZLON4gh9NH1MgFcSa0OamfLFOx/y78tHWhOmTLMBICXzENOLsvsI8Irg
# nQnAZaf6mIBJNYc9URnokCF4RS6hnyzhGMIazMXuk0lwQjKP+8bqHPNlaJGiTUyC
# EUhSaN4QvRRXXegYE2XFf7JPhSxIpFaENdb5LpyqABXRN/4aBpTCfMjqGzLmysL0
# p6MDDnSlrzm2q2AS4+jWufcx4dyt5Big2MEjR0ezoQ9uo6ttmAaDG7dqZy3SvUQa
# khCBj7A7CdfHmzJawv9qYFSLScGT7eG0XOBv6yb5jNWy+TgQ5urOkfW+0/tvk2E0
# XLyTRSiDNipmKF+wc86LJiUGsoPUXPYVGUztYuBeM/Lo6OwKp7ADK5GyNnm+960I
# HnWmZcy740hQ83eRGv7bUKJGyGFYmPV8AhY8gyitOYbs1LcNU9D4R+Z1MI3sMJN2
# FKZbS110YU0/EpF23r9Yy3IQKUHw1cVtJnZoEUETWJrcJisB9IlNWdt4z4FKPkBH
# X8mBUHOFECMhWWCKZFTBzCEa6DgZfGYczXg4RTCZT/9jT0y7qg0IU0F8WD1Hs/q2
# 7IwyCQLMbDwMVhECAwEAAaOCAVkwggFVMBIGA1UdEwEB/wQIMAYBAf8CAQAwHQYD
# VR0OBBYEFGg34Ou2O/hfEYb7/mF7CIhl9E5CMB8GA1UdIwQYMBaAFOzX44LScV1k
# TN8uZz/nupiuHA9PMA4GA1UdDwEB/wQEAwIBhjATBgNVHSUEDDAKBggrBgEFBQcD
# AzB3BggrBgEFBQcBAQRrMGkwJAYIKwYBBQUHMAGGGGh0dHA6Ly9vY3NwLmRpZ2lj
# ZXJ0LmNvbTBBBggrBgEFBQcwAoY1aHR0cDovL2NhY2VydHMuZGlnaWNlcnQuY29t
# L0RpZ2lDZXJ0VHJ1c3RlZFJvb3RHNC5jcnQwQwYDVR0fBDwwOjA4oDagNIYyaHR0
# cDovL2NybDMuZGlnaWNlcnQuY29tL0RpZ2lDZXJ0VHJ1c3RlZFJvb3RHNC5jcmww
# HAYDVR0gBBUwEzAHBgVngQwBAzAIBgZngQwBBAEwDQYJKoZIhvcNAQEMBQADggIB
# ADojRD2NCHbuj7w6mdNW4AIapfhINPMstuZ0ZveUcrEAyq9sMCcTEp6QRJ9L/Z6j
# fCbVN7w6XUhtldU/SfQnuxaBRVD9nL22heB2fjdxyyL3WqqQz/WTauPrINHVUHmI
# moqKwba9oUgYftzYgBoRGRjNYZmBVvbJ43bnxOQbX0P4PpT/djk9ntSZz0rdKOtf
# JqGVWEjVGv7XJz/9kNF2ht0csGBc8w2o7uCJob054ThO2m67Np375SFTWsPK6Wrx
# oj7bQ7gzyE84FJKZ9d3OVG3ZXQIUH0AzfAPilbLCIXVzUstG2MQ0HKKlS43Nb3Y3
# LIU/Gs4m6Ri+kAewQ3+ViCCCcPDMyu/9KTVcH4k4Vfc3iosJocsL6TEa/y4ZXDlx
# 4b6cpwoG1iZnt5LmTl/eeqxJzy6kdJKt2zyknIYf48FWGysj/4+16oh7cGvmoLr9
# Oj9FpsToFpFSi0HASIRLlk2rREDjjfAVKM7t8RhWByovEMQMCGQ8M4+uKIw8y4+I
# Cw2/O/TOHnuO77Xry7fwdxPm5yg/rBKupS8ibEH5glwVZsxsDsrFhsP2JjMMB0ug
# 0wcCampAMEhLNKhRILutG4UI4lkNbcoFUCvqShyepf2gpx8GdOfy1lKQ/a+FSCH5
# Vzu0nAPthkX0tGFuv2jiJmCG6sivqf6UHedjGzqGVnhOMIIGvDCCBKSgAwIBAgIQ
# C65mvFq6f5WHxvnpBOMzBDANBgkqhkiG9w0BAQsFADBjMQswCQYDVQQGEwJVUzEX
# MBUGA1UEChMORGlnaUNlcnQsIEluYy4xOzA5BgNVBAMTMkRpZ2lDZXJ0IFRydXN0
# ZWQgRzQgUlNBNDA5NiBTSEEyNTYgVGltZVN0YW1waW5nIENBMB4XDTI0MDkyNjAw
# MDAwMFoXDTM1MTEyNTIzNTk1OVowQjELMAkGA1UEBhMCVVMxETAPBgNVBAoTCERp
# Z2lDZXJ0MSAwHgYDVQQDExdEaWdpQ2VydCBUaW1lc3RhbXAgMjAyNDCCAiIwDQYJ
# KoZIhvcNAQEBBQADggIPADCCAgoCggIBAL5qc5/2lSGrljC6W23mWaO16P2RHxjE
# iDtqmeOlwf0KMCBDEr4IxHRGd7+L660x5XltSVhhK64zi9CeC9B6lUdXM0s71EOc
# Re8+CEJp+3R2O8oo76EO7o5tLuslxdr9Qq82aKcpA9O//X6QE+AcaU/byaCagLD/
# GLoUb35SfWHh43rOH3bpLEx7pZ7avVnpUVmPvkxT8c2a2yC0WMp8hMu60tZR0Cha
# V76Nhnj37DEYTX9ReNZ8hIOYe4jl7/r419CvEYVIrH6sN00yx49boUuumF9i2T8U
# uKGn9966fR5X6kgXj3o5WHhHVO+NBikDO0mlUh902wS/Eeh8F/UFaRp1z5SnROHw
# SJ+QQRZ1fisD8UTVDSupWJNstVkiqLq+ISTdEjJKGjVfIcsgA4l9cbk8Smlzddh4
# EfvFrpVNnes4c16Jidj5XiPVdsn5n10jxmGpxoMc6iPkoaDhi6JjHd5ibfdp5uzI
# Xp4P0wXkgNs+CO/CacBqU0R4k+8h6gYldp4FCMgrXdKWfM4N0u25OEAuEa3Jyidx
# W48jwBqIJqImd93NRxvd1aepSeNeREXAu2xUDEW8aqzFQDYmr9ZONuc2MhTMizch
# NULpUEoA6Vva7b1XCB+1rxvbKmLqfY/M/SdV6mwWTyeVy5Z/JkvMFpnQy5wR14GJ
# cv6dQ4aEKOX5AgMBAAGjggGLMIIBhzAOBgNVHQ8BAf8EBAMCB4AwDAYDVR0TAQH/
# BAIwADAWBgNVHSUBAf8EDDAKBggrBgEFBQcDCDAgBgNVHSAEGTAXMAgGBmeBDAEE
# AjALBglghkgBhv1sBwEwHwYDVR0jBBgwFoAUuhbZbU2FL3MpdpovdYxqII+eyG8w
# HQYDVR0OBBYEFJ9XLAN3DigVkGalY17uT5IfdqBbMFoGA1UdHwRTMFEwT6BNoEuG
# SWh0dHA6Ly9jcmwzLmRpZ2ljZXJ0LmNvbS9EaWdpQ2VydFRydXN0ZWRHNFJTQTQw
# OTZTSEEyNTZUaW1lU3RhbXBpbmdDQS5jcmwwgZAGCCsGAQUFBwEBBIGDMIGAMCQG
# CCsGAQUFBzABhhhodHRwOi8vb2NzcC5kaWdpY2VydC5jb20wWAYIKwYBBQUHMAKG
# TGh0dHA6Ly9jYWNlcnRzLmRpZ2ljZXJ0LmNvbS9EaWdpQ2VydFRydXN0ZWRHNFJT
# QTQwOTZTSEEyNTZUaW1lU3RhbXBpbmdDQS5jcnQwDQYJKoZIhvcNAQELBQADggIB
# AD2tHh92mVvjOIQSR9lDkfYR25tOCB3RKE/P09x7gUsmXqt40ouRl3lj+8QioVYq
# 3igpwrPvBmZdrlWBb0HvqT00nFSXgmUrDKNSQqGTdpjHsPy+LaalTW0qVjvUBhcH
# zBMutB6HzeledbDCzFzUy34VarPnvIWrqVogK0qM8gJhh/+qDEAIdO/KkYesLyTV
# OoJ4eTq7gj9UFAL1UruJKlTnCVaM2UeUUW/8z3fvjxhN6hdT98Vr2FYlCS7Mbb4H
# v5swO+aAXxWUm3WpByXtgVQxiBlTVYzqfLDbe9PpBKDBfk+rabTFDZXoUke7zPgt
# d7/fvWTlCs30VAGEsshJmLbJ6ZbQ/xll/HjO9JbNVekBv2Tgem+mLptR7yIrpaid
# RJXrI+UzB6vAlk/8a1u7cIqV0yef4uaZFORNekUgQHTqddmsPCEIYQP7xGxZBIhd
# mm4bhYsVA6G2WgNFYagLDBzpmk9104WQzYuVNsxyoVLObhx3RugaEGru+SojW4dH
# PoWrUhftNpFC5H7QEY7MhKRyrBe7ucykW7eaCuWBsBb4HOKRFVDcrZgdwaSIqMDi
# CLg4D+TPVgKx2EgEdeoHNHT9l3ZDBD+XgbF+23/zBjeCtxz+dL/9NWR6P2eZRi7z
# cEO1xwcdcqJsyz/JceENc2Sg8h3KeFUCS7tpFk7CrDqkMIIHvTCCBaWgAwIBAgIQ
# C1DPJGsmPv2FpykxUVjz/zANBgkqhkiG9w0BAQsFADBpMQswCQYDVQQGEwJVUzEX
# MBUGA1UEChMORGlnaUNlcnQsIEluYy4xQTA/BgNVBAMTOERpZ2lDZXJ0IFRydXN0
# ZWQgRzQgQ29kZSBTaWduaW5nIFJTQTQwOTYgU0hBMzg0IDIwMjEgQ0ExMB4XDTI0
# MDQwODAwMDAwMFoXDTI3MDQxMDIzNTk1OVowgcUxEzARBgsrBgEEAYI3PAIBAxMC
# VVMxGTAXBgsrBgEEAYI3PAIBAhMIRGVsYXdhcmUxHTAbBgNVBA8MFFByaXZhdGUg
# T3JnYW5pemF0aW9uMRAwDgYDVQQFEwczNTgyNjkxMQswCQYDVQQGEwJVUzETMBEG
# A1UECBMKQ2FsaWZvcm5pYTEWMBQGA1UEBxMNTW91bnRhaW4gVmlldzETMBEGA1UE
# ChMKR29vZ2xlIExMQzETMBEGA1UEAxMKR29vZ2xlIExMQzCCAiIwDQYJKoZIhvcN
# AQEBBQADggIPADCCAgoCggIBALEbiH4H31sVTQxVJTBpKgRjwYubR+0ZwqBSXude
# NE/vHfytN3fyutyz2lycUKCW6X3qPjK+zb3+uwbC2WkjRksqNNXqTQYgrBJuksJx
# RD+cSShaZG/7sJaey0R3WNa5wlAbBrZBAMwgZXaLX0YDr1NzcknsjCou4o7y6jh/
# 0TjC0bo7wYsVKb0Pq1oN2zYwO48NaFeU4bNn7AgEwwYy6GVLoPtrziEq8TVn4i9k
# U6wRWyUNBmBRyyAoFsbcyQPnr7wp13PXs5sIy6FI85XC3/NTC881SdXClMJEpoQz
# gjj6BpJgDaiwhM6muReB1zReN4J2rPsuEwFxp/cSeCaE3bOj5+rSMe4H1gt5U+k9
# U1/pRe8jyJ9DSG7c3q18HIa3znV5I26DtG5D+An8iK1gBQpI1kPJyLttRePBjEwa
# v9L7I6iSa2ygp2Aw8bhjmIFzdK68eBpAwxCNfhY4JUY6e6ors5F5zWqebwcCL2kF
# JxAYDLML1Gw625Jos/9Fop+VNglkuN4PKo/qYJaDRRqaNLl+5VkwCSakbIo0M03h
# MBEDUe0urQFzDqXxHAD1tvjiCwgyLL8eDa2Co7+QhZlblAFL7IKWri9GFBZ0RCgG
# Qj+nA6r//FbYbU00PDgOKHjrJduad4gH6aRwG62MZAGxJcK+yfKxMs+zesiVzeTI
# Ns91AgMBAAGjggICMIIB/jAfBgNVHSMEGDAWgBRoN+Drtjv4XxGG+/5hewiIZfRO
# QjAdBgNVHQ4EFgQUT17SRem6UfzmqjVYayx7xFZ0MjQwPQYDVR0gBDYwNDAyBgVn
# gQwBAzApMCcGCCsGAQUFBwIBFhtodHRwOi8vd3d3LmRpZ2ljZXJ0LmNvbS9DUFMw
# DgYDVR0PAQH/BAQDAgeAMBMGA1UdJQQMMAoGCCsGAQUFBwMDMIG1BgNVHR8Ega0w
# gaowU6BRoE+GTWh0dHA6Ly9jcmwzLmRpZ2ljZXJ0LmNvbS9EaWdpQ2VydFRydXN0
# ZWRHNENvZGVTaWduaW5nUlNBNDA5NlNIQTM4NDIwMjFDQTEuY3JsMFOgUaBPhk1o
# dHRwOi8vY3JsNC5kaWdpY2VydC5jb20vRGlnaUNlcnRUcnVzdGVkRzRDb2RlU2ln
# bmluZ1JTQTQwOTZTSEEzODQyMDIxQ0ExLmNybDCBlAYIKwYBBQUHAQEEgYcwgYQw
# JAYIKwYBBQUHMAGGGGh0dHA6Ly9vY3NwLmRpZ2ljZXJ0LmNvbTBcBggrBgEFBQcw
# AoZQaHR0cDovL2NhY2VydHMuZGlnaWNlcnQuY29tL0RpZ2lDZXJ0VHJ1c3RlZEc0
# Q29kZVNpZ25pbmdSU0E0MDk2U0hBMzg0MjAyMUNBMS5jcnQwCQYDVR0TBAIwADAN
# BgkqhkiG9w0BAQsFAAOCAgEAtWb9U46/Mxfbnpi8mtN8CdXLVl4tSWvsVqzs9Hop
# C1jf/crM0cOsWNH09wcwd0RMXm9emge5jENyq7GOk0vqLLioFAktTqICdKqrkwp2
# csGIYyVMEDwBe5R3RQ0xr281+F0CFB2C38fmrHZXDPjLtb2AGIbvG5fY7oo9VIkm
# phrNTKsY9Pzv5g/pAjDjmoeyh266xmGNt8WaOyCjK2PQivipS1ewonKzCGuNTKo3
# g5XvyFe1A51diis1KuV9EGth6jKAujRPmCV2u9pZayhDTv/6eF+uKFEEzc0GLaLj
# iUw0CQ9JYYgb8Y74kalPqlfXlHTEmmwMHWGmnB82/I64FHXqU2QOjPUKRSdphnds
# Oct8fjpkzhkXzMwLUBsgANOuXsb9IkDOR5b25jrFUfo0C0eH58J66eiQlsc9bnhc
# tHaE5xZKGYv1n+OtO3zA0ownE+LvnEX1ejUaOWJp6lEy9vvQxrBOKZ07vCb+WxI3
# XK9moP5N/yaci73hUKRtdykpqbYNdpzonDuCFLRKPBFRPrguQK9SvHijXn0g3lX9
# WQSqzpzTv1dUBOjF2Y/N4W2EYnBADG7+hG8+wC/gjnMLdGLWvcaaTiU+ITNDLaH4
# rlMkayJjkI7RETBcRNxZiq+wJ7yMCjxzjo+33njjgLNJaatyXA55aijNcTH7f/PI
# imwxggZKMIIGRgIBATB9MGkxCzAJBgNVBAYTAlVTMRcwFQYDVQQKEw5EaWdpQ2Vy
# dCwgSW5jLjFBMD8GA1UEAxM4RGlnaUNlcnQgVHJ1c3RlZCBHNCBDb2RlIFNpZ25p
# bmcgUlNBNDA5NiBTSEEzODQgMjAyMSBDQTECEAtQzyRrJj79hacpMVFY8/8wDQYJ
# YIZIAWUDBAIBBQCgfDAQBgorBgEEAYI3AgEMMQIwADAZBgkqhkiG9w0BCQMxDAYK
# KwYBBAGCNwIBBDAcBgorBgEEAYI3AgELMQ4wDAYKKwYBBAGCNwIBFTAvBgkqhkiG
# 9w0BCQQxIgQgPOG9GtkT4VLaeaCxd7mtKXnapj5OXd3S395KxnVESCMwDQYJKoZI
# hvcNAQEBBQAEggIAr5Tb6h2tX3mJ+4Xyxkjt/Q/dpdz0LFTu/DuB7lyyN4uG72LA
# 7sy88BXo1oLORY24cLCum2CA17f6ML1dU6FbW5aWvib8n8CbJkkIYMd8RkfM048V
# qKiknFUQwnLVJt5zGr/UBwgpJLvB2UR4A5IKWcLwCvLBKkrYftwvnXwUi8sYJqlN
# e4dthgt9HHTwn9nDQQlNbZTz/oZcv/VGjJjUtefoh8aOF8lC7Kbpbr85aGPt4OCk
# PS+hdAcdqHPbKtAVrHdgV7HMHORmxCHaly6MuUk7978N8MU8IGxdbHF/d9yu+xOf
# oisSosVCa6RJBEKsLh3AnO/ebg92iCRicyc1uX3/LFpAWfX6BvXeb5qy9AFLEIWT
# jkTDast1mWJ77nXj3wd1OaVSj3KQWmgp0ZnxQuKcdZD03d9WxPPSKCOtfsnueLiU
# wqb3p+BqiLhwmxy2rzjCSxDoEiuI/OPJcOGJbtokYLMF+NdwQ6R87HM/pNHsuKmI
# odwM3euK2uWiWrbRCqPA/mwTgR3FpQ/UJDcXb5mKJWYcXc1C9NSoiL9urVCbVkAo
# r6YpG71enLmVh33WRN9p+ScyHKWjjHfnXzllB5XY4JYqQ+xUMUXayY5Wjw3w7Zol
# 26cYh5sDWuzXwMUvx7KbhAVfkVSN5AH7kcS1SrMiP0Oqs3o9DBIXYH5N2uuhggMg
# MIIDHAYJKoZIhvcNAQkGMYIDDTCCAwkCAQEwdzBjMQswCQYDVQQGEwJVUzEXMBUG
# A1UEChMORGlnaUNlcnQsIEluYy4xOzA5BgNVBAMTMkRpZ2lDZXJ0IFRydXN0ZWQg
# RzQgUlNBNDA5NiBTSEEyNTYgVGltZVN0YW1waW5nIENBAhALrma8Wrp/lYfG+ekE
# 4zMEMA0GCWCGSAFlAwQCAQUAoGkwGAYJKoZIhvcNAQkDMQsGCSqGSIb3DQEHATAc
# BgkqhkiG9w0BCQUxDxcNMjUwMzEzMTY0MzUzWjAvBgkqhkiG9w0BCQQxIgQgSMM+
# iXZs/6LGHZ4ZFgn2KKrreuva3VcE2ld0kWp6yQYwDQYJKoZIhvcNAQEBBQAEggIA
# iZO7ndDHvZepzTBHooHZreuYiEaGw35A+nE2yXwIDX7/9/hVf43TN//jmQ62wlj7
# WwYbbZZ2RTZfX2eg5QwbGkE8035M5QCgnJZqPnPajZPQ+uPbzA5XdTIARKYfNxEO
# tQy4zli1a4cuj+0jkSrVH8GTrvsausQx0ESUnb1mE0d7r5D0z4XSTFM0dZAkb/Xi
# zuoVDsljOWOPRBusOvvIi+od+3F2CiyQLw2MpFBe223udLa1k1oCk4eVgEFKzYSU
# csMY6P63fObcTauUl2DnHS+OcbA88p0F387NPLopBCgVJywJ1nhwBZQrkOc+0xEq
# hYskmXkfQc0Tj3lmrrRxpjNP3RBn08HHO0AG144e3OCyzrR/SGXAbiQuj8zIG8Cs
# fJKng4YKTTke8cnVxXQ7HIIjmEIGVpID7wbTHl3/a/1izcQaGeigPdPco1WQqiAA
# t6WXnhSpBxbk9jq9GJXcklrTq8WZse64CghaILPAsHYjTS11I0KwGTdVq66nzkZK
# lDXAd/NRuEB9M0jUi7jNc9lnys5FcpIYXetzKzsXfRalZI/Ar01grnj1uRZKGXfU
# M0XURamyT/Af3maUdXyd5gaUCBrq4BLO394zKxnDZGxWv9yaQgGklX2akuxO7bem
# ik1ffR3qz8Ipgj/9pI5DfN8ItaV3kbDXuAoefGXYYBQ=
# SIG # End signature block
