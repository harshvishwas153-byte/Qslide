param(
    [string]$OutputDir = "C:\Users\harsh\Qlide\cad_output",
    [int]$ToothCount = 20,
    [double]$OuterDiameterMm = 90.0,
    [double]$RootDiameterMm = 78.0,
    [double]$InnerDiameterMm = 50.0,
    [double]$ThicknessMm = 10.0,
    [double]$ToothFill = 0.50
)

$ErrorActionPreference = "Stop"

function MmToM([double]$value) {
    return $value / 1000.0
}

if ($ToothCount -lt 3) {
    throw "ToothCount must be at least 3."
}

if ($ToothFill -le 0.05 -or $ToothFill -ge 0.95) {
    throw "ToothFill must be between 0.05 and 0.95."
}

New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null

$partPath = Join-Path $OutputDir "edp_see_q1_toothed_washer.SLDPRT"
$stepPath = Join-Path $OutputDir "edp_see_q1_toothed_washer.STEP"

$swApp = New-Object -ComObject SldWorks.Application
$swApp.Visible = $false

$model = $swApp.NewPart()
if ($null -eq $model) {
    throw "SolidWorks could not create a new part. Check that a default part template is configured."
}

# SolidWorks stores model geometry in meters.
$outerR = MmToM($OuterDiameterMm / 2.0)
$rootR = MmToM($RootDiameterMm / 2.0)
$innerR = MmToM($InnerDiameterMm / 2.0)
$thickness = MmToM($ThicknessMm)

$sketchMgr = $model.SketchManager
$ext = $model.Extension

$selected = $ext.SelectByID2("Front Plane", "PLANE", 0, 0, 0, $false, 0, $null, 0)
if (-not $selected) {
    throw "Could not select Front Plane."
}

$sketchMgr.InsertSketch($true)

$pitch = 2.0 * [Math]::PI / $ToothCount
$halfTooth = $pitch * $ToothFill / 2.0

$points = New-Object System.Collections.Generic.List[double[]]
for ($i = 0; $i -lt $ToothCount; $i++) {
    $center = $i * $pitch
    $start = $center - $halfTooth
    $end = $center + $halfTooth

    $points.Add(@(
        $rootR * [Math]::Cos($start),
        $rootR * [Math]::Sin($start)
    ))
    $points.Add(@(
        $outerR * [Math]::Cos($start),
        $outerR * [Math]::Sin($start)
    ))
    $points.Add(@(
        $outerR * [Math]::Cos($end),
        $outerR * [Math]::Sin($end)
    ))
    $points.Add(@(
        $rootR * [Math]::Cos($end),
        $rootR * [Math]::Sin($end)
    ))
}

for ($i = 0; $i -lt $points.Count; $i++) {
    $a = $points[$i]
    $b = $points[($i + 1) % $points.Count]
    $sketchMgr.CreateLine($a[0], $a[1], 0, $b[0], $b[1], 0) | Out-Null
}

$sketchMgr.CreateCircleByRadius(0, 0, 0, $innerR) | Out-Null
$sketchMgr.InsertSketch($true)

$model.ClearSelection2($true)
$feature = $model.FeatureManager.FeatureExtrusion2(
    $true, $false, $false,
    0, 0,
    $thickness, 0,
    $false, $false, $false, $false,
    0, 0,
    $false, $false, $false, $false,
    $true, $true, $true,
    0, 0, $false
)

if ($null -eq $feature) {
    throw "Boss-Extrude failed. The generated sketch may not have been accepted by SolidWorks."
}

$feature.Name = "10mm thick toothed washer"
$model.ForceRebuild3($false) | Out-Null

$errors = 0
$warnings = 0
$saveOk = $ext.SaveAs($partPath, 0, 1, $null, [ref]$errors, [ref]$warnings)
if (-not $saveOk -or $errors -ne 0) {
    throw "Failed to save SLDPRT. SaveAs returned $saveOk, errors=$errors, warnings=$warnings."
}

$stepErrors = 0
$stepWarnings = 0
$ext.SaveAs($stepPath, 0, 1, $null, [ref]$stepErrors, [ref]$stepWarnings) | Out-Null

$swApp.CloseDoc($model.GetTitle())

[pscustomobject]@{
    PartPath = $partPath
    StepPath = $stepPath
    ToothCount = $ToothCount
    OuterDiameterMm = $OuterDiameterMm
    RootDiameterMm = $RootDiameterMm
    InnerDiameterMm = $InnerDiameterMm
    ThicknessMm = $ThicknessMm
    ToothFill = $ToothFill
    SaveWarnings = $warnings
    StepWarnings = $stepWarnings
} | Format-List
