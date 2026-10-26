param([Parameter(Mandatory=$true)][string]$InputJson, [Parameter(Mandatory=$true)][string]$OutputJsonl)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime]
$awaiter = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetGenericArguments().Count -eq 1 -and $_.GetParameters().Count -eq 1
} | Select-Object -First 1
function Await-Result($Operation, [Type]$ResultType) {
    $task = $awaiter.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    $task.GetAwaiter().GetResult()
}
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) { throw 'Windows OCR recognizer unavailable' }
$items = Get-Content -LiteralPath $InputJson -Raw -Encoding UTF8 | ConvertFrom-Json
$writer = [System.IO.StreamWriter]::new($OutputJsonl, $false, [System.Text.UTF8Encoding]::new($false))
try {
    $count = 0
    foreach ($item in $items) {
        try {
            $file = Await-Result ([Windows.Storage.StorageFile]::GetFileFromPathAsync($item.path)) ([Windows.Storage.StorageFile])
            $stream = Await-Result ($file.OpenReadAsync()) ([Windows.Storage.Streams.IRandomAccessStreamWithContentType])
            try {
                $decoder = Await-Result ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
                $bitmap = Await-Result ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
                try {
                    $result = Await-Result ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
                    $words = @($result.Lines | ForEach-Object { $_.Words | ForEach-Object {
                        @{ text=$_.Text; x=$_.BoundingRect.X; y=$_.BoundingRect.Y; width=$_.BoundingRect.Width; height=$_.BoundingRect.Height }
                    } })
                    $value = @{ id=$item.id; text=$result.Text; words=$words; width=$decoder.PixelWidth; height=$decoder.PixelHeight }
                } finally { $bitmap.Dispose() }
            } finally { $stream.Dispose() }
        } catch {
            $value = @{ id=$item.id; error=$_.Exception.Message }
        }
        $writer.WriteLine(($value | ConvertTo-Json -Depth 8 -Compress))
        $writer.Flush()
        $count++
        if ($count % 50 -eq 0) { Write-Output "OCR $count / $($items.Count)" }
    }
} finally { $writer.Dispose() }
