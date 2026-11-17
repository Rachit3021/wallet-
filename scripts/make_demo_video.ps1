# Render the five-minute slide demo as docs/demo.mp4.
# Requires ffmpeg on PATH.

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$output = Join-Path $repoRoot 'docs/demo.mp4'

$slides = @(
    @{ Start = 0;   End = 30;  Size = 60; Text = 'Digital Wallet\nDouble-entry ledger backend' }
    @{ Start = 30;  End = 60;  Size = 46; Text = 'One command starts everything\ndocker compose up' }
    @{ Start = 60;  End = 90;  Size = 42; Text = 'Create users and wallets\nPOST /users\nPOST /users/{id}/wallets' }
    @{ Start = 90;  End = 120; Size = 42; Text = 'Move money\ntop-up then peer transfer\nevery leg is double-entry' }
    @{ Start = 120; End = 150; Size = 42; Text = 'Read the books\nbalance and paginated statement\npaise in BIGINT only' }
    @{ Start = 150; End = 180; Size = 42; Text = 'Idempotency\nUUID key on every POST\nsame key and body replay first response\nsame key new body 422' }
    @{ Start = 180; End = 210; Size = 42; Text = 'Stampede acceptance\n100 concurrent 10-paise spends\nfrom 500 paise\n50 posted 50 rejected' }
    @{ Start = 210; End = 240; Size = 42; Text = 'Fraud review\nDB-backed thresholds\nhold then release\nLLM explains the flag' }
    @{ Start = 240; End = 270; Size = 42; Text = 'Nightly reconciliation\nREPEATABLE READ snapshot\njob lock\nthree injected corruptions found' }
    @{ Start = 270; End = 300; Size = 42; Text = 'Design takeaways\nimmutable ledger\nrow locks plus idempotency\nledger derived truth' }
)

$filters = foreach ($slide in $slides) {
    "drawtext=fontfile=C\\:/Windows/Fonts/arial.ttf:text='$($slide.Text)':fontsize=$($slide.Size):line_spacing=14:fontcolor=white:box=1:boxcolor=black@0.35:boxborderw=24:x=(w-text_w)/2:y=(h-text_h)/2:enable='between(t,$($slide.Start),$($slide.End))'"
}

$filterChain = $filters -join ','

ffmpeg -hide_banner -loglevel error -y `
    -f lavfi -i 'color=c=0x0f172a:s=1280x720:r=25:d=300' `
    -vf $filterChain `
    -t 300 -c:v libx264 -preset veryfast -crf 28 -pix_fmt yuv420p `
    $output

Write-Host "wrote $output"
