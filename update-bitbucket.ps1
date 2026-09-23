param(
    [string]$BaseUrl = "https://bitbucket.example.com",
    [string]$Destination = "D:\Repos\projects",
    [string]$Username = "user",
    [string]$Password = $env:BITBUCKET_PASSWORD,
    [int]$PageLimit = 100,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($Password)) {
    throw "Password is required. Pass -Password or set BITBUCKET_PASSWORD."
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git is not available in PATH."
}

$BaseUrl = $BaseUrl.TrimEnd("/")
$Destination = [System.IO.Path]::GetFullPath($Destination)
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logDir = Join-Path $PSScriptRoot "logs"
$logFile = Join-Path $logDir "update-$timestamp.log"
$reportFile = Join-Path $logDir "update-$timestamp.csv"

if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}

function Write-Log {
    param([string]$Message)
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
}

function Normalize-GitUrl {
    param([string]$Url)
    if ([string]::IsNullOrWhiteSpace($Url)) {
        return ""
    }

    $normalized = $Url.Trim()
    $normalized = $normalized -replace "^(https?://)[^/@]+@", '$1'
    $normalized = $normalized.TrimEnd("/")
    return $normalized.ToLowerInvariant()
}

function Invoke-BitbucketGet {
    param([string]$PathAndQuery)

    $pair = "{0}:{1}" -f $Username, $Password
    $token = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($pair))
    $headers = @{ Authorization = "Basic $token" }
    $uri = "{0}{1}" -f $BaseUrl, $PathAndQuery

    Invoke-RestMethod -Method Get -Uri $uri -Headers $headers
}

function Get-PagedValues {
    param([string]$Path)

    $values = New-Object System.Collections.Generic.List[object]
    $start = 0

    do {
        $separator = if ($Path.Contains("?")) { "&" } else { "?" }
        $page = Invoke-BitbucketGet ("{0}{1}limit={2}&start={3}" -f $Path, $separator, $PageLimit, $start)
        foreach ($value in $page.values) {
            $values.Add($value)
        }
        $start = $page.nextPageStart
    } while (-not $page.isLastPage)

    return $values
}

function Get-HttpCloneUrl {
    param($ProjectKey, $Repo)

    if ($Repo.links.clone) {
        $httpClone = $Repo.links.clone |
            Where-Object { $_.name -in @("http", "https") -or $_.href -like "http*" } |
            Select-Object -First 1
        if ($httpClone) {
            return $httpClone.href
        }
    }

    return "{0}/scm/{1}/{2}.git" -f $BaseUrl, $ProjectKey.ToLowerInvariant(), $Repo.slug
}

function Invoke-Git {
    param(
        [string[]]$Arguments,
        [string]$WorkingDirectory = $PSScriptRoot
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & git -C $WorkingDirectory @Arguments 2>&1 | ForEach-Object { $_.ToString() }
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    if ($output) {
        foreach ($line in $output) {
            Write-Log ("git: {0}" -f $line)
        }
    }

    if ($exitCode -ne 0) {
        throw "git $($Arguments -join ' ') failed with exit code $exitCode"
    }
}

function Get-LocalRepoIndex {
    $byRemote = @{}
    $byName = @{}

    if (-not (Test-Path -LiteralPath $Destination)) {
        New-Item -ItemType Directory -Path $Destination | Out-Null
    }

    Get-ChildItem -LiteralPath $Destination -Directory | ForEach-Object {
        $gitDir = Join-Path $_.FullName ".git"
        if (-not (Test-Path -LiteralPath $gitDir)) {
            return
        }

        $remote = (& git -C $_.FullName remote get-url origin 2>$null)
        if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($remote)) {
            $normalized = Normalize-GitUrl $remote
            if (-not $byRemote.ContainsKey($normalized)) {
                $byRemote[$normalized] = $_.FullName
            }
        }

        if (-not $byName.ContainsKey($_.Name.ToLowerInvariant())) {
            $byName[$_.Name.ToLowerInvariant()] = $_.FullName
        }
    }

    return @{
        ByRemote = $byRemote
        ByName = $byName
    }
}

function Get-CloneTargetPath {
    param(
        [hashtable]$NameIndex,
        [string]$ProjectKey,
        [string]$Slug
    )

    $preferredName = $Slug
    if (-not $NameIndex.ContainsKey($preferredName.ToLowerInvariant())) {
        return Join-Path $Destination $preferredName
    }

    $fallbackName = "{0}__{1}" -f $ProjectKey.ToLowerInvariant(), $Slug
    return Join-Path $Destination $fallbackName
}

Write-Log "Starting Bitbucket update. Destination: $Destination"

$projects = Get-PagedValues "/rest/api/1.0/projects"
Write-Log ("Found {0} projects." -f $projects.Count)

$localIndex = Get-LocalRepoIndex
$results = New-Object System.Collections.Generic.List[object]
$totalRepos = 0

foreach ($project in $projects) {
    $projectKey = $project.key
    Write-Log "Reading repositories for project $projectKey"
    $repos = Get-PagedValues ("/rest/api/1.0/projects/{0}/repos" -f [uri]::EscapeDataString($projectKey))

    foreach ($repo in $repos) {
        $totalRepos++
        $cloneUrl = Get-HttpCloneUrl $projectKey $repo
        $normalizedCloneUrl = Normalize-GitUrl $cloneUrl
        $localPath = $localIndex.ByRemote[$normalizedCloneUrl]
        $action = "updated"
        $status = "ok"
        $message = ""

        try {
            if ($localPath) {
                Write-Log ("Updating {0}/{1} in {2}" -f $projectKey, $repo.slug, $localPath)
                if (-not $DryRun) {
                    Invoke-Git @("remote", "set-url", "origin", $cloneUrl) $localPath
                    Invoke-Git @("fetch", "origin", "--prune", "--tags") $localPath
                }
            } else {
                $localPath = Get-CloneTargetPath $localIndex.ByName $projectKey $repo.slug
                $action = "cloned"
                Write-Log ("Cloning {0}/{1} into {2}" -f $projectKey, $repo.slug, $localPath)
                if (-not $DryRun) {
                    Invoke-Git @("clone", "--origin", "origin", $cloneUrl, $localPath) $Destination
                    $localIndex.ByRemote[$normalizedCloneUrl] = $localPath
                    $localIndex.ByName[[System.IO.Path]::GetFileName($localPath).ToLowerInvariant()] = $localPath
                }
            }
        } catch {
            $status = "failed"
            $message = $_.Exception.Message
            Write-Log ("FAILED {0}/{1}: {2}" -f $projectKey, $repo.slug, $message)
        }

        $results.Add([pscustomobject]@{
            Project = $projectKey
            Repository = $repo.slug
            Action = $action
            Status = $status
            Path = $localPath
            Message = $message
        })
    }
}

$results | Export-Csv -LiteralPath $reportFile -NoTypeInformation -Encoding UTF8

$updated = ($results | Where-Object { $_.Action -eq "updated" -and $_.Status -eq "ok" }).Count
$cloned = ($results | Where-Object { $_.Action -eq "cloned" -and $_.Status -eq "ok" }).Count
$failed = ($results | Where-Object { $_.Status -eq "failed" }).Count

Write-Log ("Done. Repositories: {0}; updated: {1}; cloned: {2}; failed: {3}" -f $totalRepos, $updated, $cloned, $failed)
Write-Log "Log: $logFile"
Write-Log "Report: $reportFile"

if ($failed -gt 0) {
    exit 1
}
