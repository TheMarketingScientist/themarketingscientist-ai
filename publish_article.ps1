#requires -Version 5.1

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateNotNullOrEmpty()]
    [string]$ArticlePath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$script:ValidationWarnings = [System.Collections.Generic.List[string]]::new()

function Stop-Publication {
    param([Parameter(Mandatory = $true)][string]$Message)

    Write-Error $Message
    exit 1
}

function Invoke-GitLines {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $result = @(& git -c core.quotepath=false @Arguments)
    if ($LASTEXITCODE -ne 0) {
        Stop-Publication "Git command failed: git $($Arguments -join ' ')"
    }
    return @($result | ForEach-Object { [string]$_ })
}

function Normalize-GitPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $normalized = $Path.Replace("\", "/")
    if ($normalized.StartsWith("./", [System.StringComparison]::Ordinal)) {
        return $normalized.Substring(2)
    }
    return $normalized
}

function Convert-ToRepoPath {
    param([Parameter(Mandatory = $true)][string]$FullPath)

    $normalized = [System.IO.Path]::GetFullPath($FullPath)
    $prefix = $script:RepoRoot.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
    if (-not $normalized.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        Stop-Publication "Path is outside the repository: $normalized"
    }
    return Normalize-GitPath -Path ($normalized.Substring($prefix.Length))
}

function Get-WorkingChanges {
    $unstaged = @(Invoke-GitLines -Arguments @("diff", "--name-only", "--"))
    $untracked = @(Invoke-GitLines -Arguments @("ls-files", "--others", "--exclude-standard"))
    return @(
        ($unstaged + $untracked) |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
            ForEach-Object { Normalize-GitPath $_ } |
            Sort-Object -Unique
    )
}

function ConvertTo-NativeArgument {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Argument
    )

    if ($Argument.Length -gt 0 -and $Argument -notmatch '[\s"]') {
        return $Argument
    }

    $builder = [System.Text.StringBuilder]::new()
    [void]$builder.Append([char]34)
    $backslashes = 0
    foreach ($character in $Argument.ToCharArray()) {
        if ($character -eq [char]92) {
            $backslashes += 1
            continue
        }
        if ($character -eq [char]34) {
            [void]$builder.Append(('\' * (($backslashes * 2) + 1)))
            [void]$builder.Append([char]34)
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append(('\' * $backslashes))
            $backslashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($backslashes -gt 0) {
        [void]$builder.Append(('\' * ($backslashes * 2)))
    }
    [void]$builder.Append([char]34)
    return $builder.ToString()
}

function Invoke-NativeProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [switch]$Utf8Python
    )

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = (@($Arguments | ForEach-Object { ConvertTo-NativeArgument $_ }) -join " ")
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    $startInfo.StandardOutputEncoding = $utf8
    $startInfo.StandardErrorEncoding = $utf8
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    $environmentTemporarilyChanged = $false
    $previousPythonUtf8 = $null
    $previousPythonIoEncoding = $null
    try {
        if ($Utf8Python) {
            $previousPythonUtf8 = [Environment]::GetEnvironmentVariable(
                "PYTHONUTF8", [EnvironmentVariableTarget]::Process
            )
            $previousPythonIoEncoding = [Environment]::GetEnvironmentVariable(
                "PYTHONIOENCODING", [EnvironmentVariableTarget]::Process
            )
            $environmentTemporarilyChanged = $true
            [Environment]::SetEnvironmentVariable(
                "PYTHONUTF8", "1", [EnvironmentVariableTarget]::Process
            )
            [Environment]::SetEnvironmentVariable(
                "PYTHONIOENCODING", "utf-8", [EnvironmentVariableTarget]::Process
            )
        }
        try {
            if (-not $process.Start()) {
                throw "Failed to start native process: $FilePath"
            }
        }
        finally {
            if ($environmentTemporarilyChanged) {
                [Environment]::SetEnvironmentVariable(
                    "PYTHONUTF8", $previousPythonUtf8, [EnvironmentVariableTarget]::Process
                )
                [Environment]::SetEnvironmentVariable(
                    "PYTHONIOENCODING", $previousPythonIoEncoding,
                    [EnvironmentVariableTarget]::Process
                )
                $environmentTemporarilyChanged = $false
            }
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        return [PSCustomObject]@{
            ExitCode = $process.ExitCode
            StdOut = $stdout
            StdErr = $stderr
        }
    }
    finally {
        if ($environmentTemporarilyChanged) {
            [Environment]::SetEnvironmentVariable(
                "PYTHONUTF8", $previousPythonUtf8, [EnvironmentVariableTarget]::Process
            )
            [Environment]::SetEnvironmentVariable(
                "PYTHONIOENCODING", $previousPythonIoEncoding,
                [EnvironmentVariableTarget]::Process
            )
        }
        $process.Dispose()
    }
}

function Show-NativeProcessOutput {
    param(
        [Parameter(Mandatory = $true)]$Result,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not [string]::IsNullOrWhiteSpace([string]$Result.StdOut)) {
        Write-Host ([string]$Result.StdOut).TrimEnd("`r", "`n")
    }
    if (-not [string]::IsNullOrWhiteSpace([string]$Result.StdErr)) {
        Write-Host "=== $Label stderr ===" -ForegroundColor DarkYellow
        Write-Host ([string]$Result.StdErr).TrimEnd("`r", "`n") -ForegroundColor DarkYellow
    }
}

function Invoke-ArticleValidator {
    param(
        [Parameter(Mandatory = $true)][string]$Mode,
        [string[]]$AdditionalArguments = @()
    )

    $arguments = @(
        "-B", $script:ValidatorPath,
        $Mode,
        "--repo-root", $script:RepoRoot,
        "--article", $script:ArticleFullPath
    ) + $AdditionalArguments
    $processResult = Invoke-NativeProcess `
        -FilePath $script:PythonPath `
        -Arguments $arguments `
        -WorkingDirectory $script:RepoRoot `
        -Utf8Python
    if (-not [string]::IsNullOrWhiteSpace([string]$processResult.StdErr)) {
        Show-NativeProcessOutput -Result ([PSCustomObject]@{
            StdOut = ""
            StdErr = $processResult.StdErr
        }) -Label "validator"
    }

    try {
        $report = ([string]$processResult.StdOut) | ConvertFrom-Json
    }
    catch {
        Stop-Publication (
            "Article validator returned invalid JSON in '$Mode' mode: " +
            "$($_.Exception.Message)$([Environment]::NewLine)$($processResult.StdOut)"
        )
    }
    if ($processResult.ExitCode -eq 0 -and $report.status -cne "passed") {
        Stop-Publication "Validator contract error: exit code 0 returned status '$($report.status)'."
    }
    if ($processResult.ExitCode -ne 0 -and $report.status -cne "failed") {
        Stop-Publication (
            "Validator contract error: exit code $($processResult.ExitCode) returned status " +
            "'$($report.status)'."
        )
    }
    return [PSCustomObject]@{ ExitCode = $processResult.ExitCode; Report = $report }
}

function Show-ValidatorReport {
    param(
        [Parameter(Mandatory = $true)]$Report,
        [Parameter(Mandatory = $true)][string]$Label
    )

    Write-Host "$Label status: $($Report.status)" -ForegroundColor Cyan
    if ($Report.classification -and $Report.classification -cne "Unknown") {
        Write-Host "Article classification: $($Report.classification)" -ForegroundColor Cyan
    }
    foreach ($indicator in @($Report.detected_executable_indicators)) {
        Write-Host ("Execution indicator: " + $indicator.detail) -ForegroundColor Yellow
    }
    foreach ($warning in @($Report.warnings)) {
        $script:ValidationWarnings.Add([string]$warning)
        Write-Warning $warning
    }
    foreach ($validationError in @($Report.errors)) {
        Write-Host ("Validation error: " + $validationError) -ForegroundColor Red
    }
}

function Invoke-Publication {
    if ($null -eq (Get-Command git -ErrorAction SilentlyContinue)) {
        Stop-Publication "Git is not available on PATH."
    }
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        Stop-Publication "Python is not available on PATH."
    }
    $script:PythonPath = $pythonCommand.Source

    $gitRootLines = @(Invoke-GitLines -Arguments @("rev-parse", "--show-toplevel"))
    if ($gitRootLines.Count -ne 1) {
        Stop-Publication "Could not determine the repository root."
    }
    $script:RepoRoot = [System.IO.Path]::GetFullPath($gitRootLines[0])

    $currentDirectory = [System.IO.Path]::GetFullPath((Get-Location).Path)
    if (-not $currentDirectory.Equals($script:RepoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        Stop-Publication "Run this script from the repository root: $script:RepoRoot"
    }
    $scriptDirectory = [System.IO.Path]::GetFullPath($PSScriptRoot)
    if (-not $scriptDirectory.Equals($script:RepoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        Stop-Publication "publish_article.ps1 must remain at the repository root."
    }

    $branchLines = @(Invoke-GitLines -Arguments @("branch", "--show-current"))
    $branch = if ($branchLines.Count -eq 1) { $branchLines[0].Trim() } else { "" }
    if ($branch -cne "GH_Pages") {
        Stop-Publication "Current branch is '$branch'. Publication is allowed only from 'GH_Pages'."
    }
    $headLines = @(Invoke-GitLines -Arguments @("rev-parse", "HEAD"))
    if ($headLines.Count -ne 1) {
        Stop-Publication "Could not determine the current commit."
    }
    $headBefore = $headLines[0].Trim()

    $script:ValidatorPath = Join-Path $script:RepoRoot "scripts\article_validator.py"
    if (-not (Test-Path -LiteralPath $script:ValidatorPath -PathType Leaf)) {
        Stop-Publication "Article validator is missing: $script:ValidatorPath"
    }
    $script:ArticleFullPath = [System.IO.Path]::GetFullPath($ArticlePath)

    $stagedBefore = @(Invoke-GitLines -Arguments @("diff", "--cached", "--name-only", "--"))
    if ($stagedBefore.Count -gt 0) {
        Stop-Publication "The Git index already contains staged files: $($stagedBefore -join ', ')"
    }

    $sourceValidation = Invoke-ArticleValidator -Mode "source"
    Show-ValidatorReport -Report $sourceValidation.Report -Label "Source validation"
    if ($sourceValidation.ExitCode -ne 0) {
        Stop-Publication "Source validation failed. The generator was not run."
    }

    if ($null -eq (Get-Command quarto -ErrorAction SilentlyContinue)) {
        Stop-Publication "Quarto is not available on PATH."
    }

    $allowedAuthoringPaths = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    $articleRepoPath = Convert-ToRepoPath $script:ArticleFullPath
    [void]$allowedAuthoringPaths.Add($articleRepoPath)
    foreach ($image in @($sourceValidation.Report.source_images)) {
        [void]$allowedAuthoringPaths.Add((Normalize-GitPath ([string]$image.source_path)))
    }

    $workflowPaths = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    foreach ($path in @(
        "AGENTS.md",
        "_quarto.yml",
        "publish_article.ps1",
        "articles/generate_articles.py",
        "scripts/article_validator.py",
        "tests/test_article_validator.py"
    )) {
        [void]$workflowPaths.Add($path)
    }
    $permittedPreExistingPaths = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )

    $changesBefore = @(Get-WorkingChanges)
    $workflowChanges = @($changesBefore | Where-Object { $workflowPaths.Contains($_) })
    $authoringChanges = @($changesBefore | Where-Object { $allowedAuthoringPaths.Contains($_) })
    $generatedChanges = @(
        $changesBefore | Where-Object {
            $_.StartsWith("_site/", [System.StringComparison]::OrdinalIgnoreCase)
        }
    )
    $unrelatedBefore = @(
        $changesBefore | Where-Object {
            -not $workflowPaths.Contains($_) -and
            -not $allowedAuthoringPaths.Contains($_) -and
            -not $_.StartsWith("_site/", [System.StringComparison]::OrdinalIgnoreCase)
        }
    )
    if ($unrelatedBefore.Count -gt 0) {
        Stop-Publication "Unrelated existing changes were found: $($unrelatedBefore -join ', ')"
    }
    if ($workflowChanges.Count -gt 0) {
        $trackedArticle = @(Invoke-GitLines -Arguments @("ls-files", "--", $articleRepoPath))
        if ($trackedArticle -cnotcontains $articleRepoPath -or $authoringChanges.Count -gt 0) {
            Stop-Publication (
                "Workflow implementation changes may be exercised only against an unchanged, " +
                "already-tracked article."
            )
        }
        foreach ($path in $workflowChanges) {
            [void]$permittedPreExistingPaths.Add($path)
        }
        $message = "Controlled workflow self-test includes: $($workflowChanges -join ', ')"
        $script:ValidationWarnings.Add($message)
        Write-Warning $message
    }
    if ($generatedChanges.Count -gt 0) {
        $message = (
            "Pre-existing generated output will be regenerated and revalidated: " +
            "$($generatedChanges.Count) path(s) under _site/."
        )
        $script:ValidationWarnings.Add($message)
        Write-Warning $message
    }

    $articlesTemplate = Join-Path $script:RepoRoot "articles.qmd"
    $articlesBackup = $articlesTemplate + ".bak"
    if (-not (Test-Path -LiteralPath $articlesTemplate -PathType Leaf)) {
        Stop-Publication "Article-index source is missing: $articlesTemplate"
    }
    if (Test-Path -LiteralPath $articlesBackup) {
        Stop-Publication "A source-side generator backup already exists: $articlesBackup"
    }
    $templateHash = (Get-FileHash -LiteralPath $articlesTemplate -Algorithm SHA256).Hash.ToLowerInvariant()

    Write-Host "Preflight passed for $($sourceValidation.Report.expected_article_slug)." -ForegroundColor Green
    Write-Host "Running: python articles/generate_articles.py"
    $generatorResult = Invoke-NativeProcess `
        -FilePath $script:PythonPath `
        -Arguments @("articles/generate_articles.py") `
        -WorkingDirectory $script:RepoRoot `
        -Utf8Python
    Show-NativeProcessOutput -Result $generatorResult -Label "generator"
    Write-Host "Generator exit code: $($generatorResult.ExitCode)" -ForegroundColor Cyan

    $templateStateErrors = @()
    if (Test-Path -LiteralPath $articlesBackup) {
        $templateStateErrors += "Temporary backup remains: $articlesBackup"
    }
    if (-not (Test-Path -LiteralPath $articlesTemplate -PathType Leaf)) {
        $templateStateErrors += "articles.qmd is missing after generation"
    }
    elseif ((Get-FileHash -LiteralPath $articlesTemplate -Algorithm SHA256).Hash.ToLowerInvariant() -cne $templateHash) {
        $templateStateErrors += "articles.qmd was not restored byte-for-byte"
    }
    if ($templateStateErrors.Count -gt 0) {
        Stop-Publication "Generator source-state validation failed: $($templateStateErrors -join '; ')"
    }
    if ($generatorResult.ExitCode -ne 0) {
        Stop-Publication "Article generation failed with exit code $($generatorResult.ExitCode)."
    }

    $imageSync = Invoke-ArticleValidator -Mode "sync-images"
    Show-ValidatorReport -Report $imageSync.Report -Label "Image synchronization"
    if ($imageSync.ExitCode -ne 0) {
        Stop-Publication "Deployable image synchronization failed."
    }

    $renderedValidation = Invoke-ArticleValidator `
        -Mode "rendered" `
        -AdditionalArguments @("--template-sha256", $templateHash)
    Show-ValidatorReport -Report $renderedValidation.Report -Label "Rendered-output validation"
    if ($renderedValidation.ExitCode -ne 0) {
        Stop-Publication "Rendered-output validation failed."
    }

    $branchAfterLines = @(Invoke-GitLines -Arguments @("branch", "--show-current"))
    $branchAfter = if ($branchAfterLines.Count -eq 1) { $branchAfterLines[0].Trim() } else { "" }
    if ($branchAfter -cne "GH_Pages") {
        Stop-Publication "The current branch changed unexpectedly during generation: '$branchAfter'."
    }
    $headAfterLines = @(Invoke-GitLines -Arguments @("rev-parse", "HEAD"))
    $headAfter = if ($headAfterLines.Count -eq 1) { $headAfterLines[0].Trim() } else { "" }
    if ($headAfter -cne $headBefore) {
        Stop-Publication "HEAD changed unexpectedly during generation: $headBefore -> $headAfter"
    }

    $stagedAfter = @(Invoke-GitLines -Arguments @("diff", "--cached", "--name-only", "--"))
    if ($stagedAfter.Count -gt 0) {
        Stop-Publication "Files became staged unexpectedly: $($stagedAfter -join ', ')"
    }

    $changesAfter = @(Get-WorkingChanges)
    $unexpectedAfter = @(
        $changesAfter | Where-Object {
            -not $allowedAuthoringPaths.Contains($_) -and
            -not $permittedPreExistingPaths.Contains($_) -and
            -not $_.StartsWith("_site/", [System.StringComparison]::OrdinalIgnoreCase)
        }
    )
    if ($unexpectedAfter.Count -gt 0) {
        Stop-Publication "Generation changed unexpected files: $($unexpectedAfter -join ', ')"
    }

    Write-Host ""
    Write-Host "=== git status --short --untracked-files=all ===" -ForegroundColor Cyan
    & git status --short --untracked-files=all
    if ($LASTEXITCODE -ne 0) {
        Stop-Publication "git status failed."
    }

    Write-Host ""
    Write-Host "=== git diff --stat ===" -ForegroundColor Cyan
    & git diff --stat --
    if ($LASTEXITCODE -ne 0) {
        Stop-Publication "git diff --stat failed."
    }

    Write-Host ""
    Write-Host "=== Files that would be staged (no git add was run) ===" -ForegroundColor Yellow
    if ($changesAfter.Count -eq 0) {
        Write-Host "(none)"
    }
    else {
        $changesAfter | ForEach-Object { Write-Host $_ }
    }

    Write-Host ""
    if ($script:ValidationWarnings.Count -gt 0) {
        Write-Host "=== Warnings requiring review ===" -ForegroundColor Yellow
        $script:ValidationWarnings | Sort-Object -Unique | ForEach-Object { Write-Host ("- " + $_) }
        Write-Host ""
    }
    Write-Host "Validation completed. No files were staged, committed, or pushed." -ForegroundColor Green
}

if ($MyInvocation.InvocationName -ne ".") {
    Invoke-Publication
}
