# FrameExtractor — CLI Cheat Sheet

GUI에서 하는 모든 작업을 터미널에서 그대로 할 수 있습니다. 모든 명령은 동일한 REST 엔드포인트(`http://localhost:9110/api/...`)를 사용합니다.

> **Windows PowerShell 환경 기준**. `curl.exe`(Windows 10+ 기본 탑재) 또는 `Invoke-RestMethod`. Bash가 필요한 경우 별도 표시.
>
> **참고**: 브라우저 파일 업로드는 사내 DLP에 막힐 수 있지만 **CLI(`curl.exe`/PowerShell)는 보통 통과합니다** — DLP는 주로 브라우저 후킹이라 일반 HTTP 클라이언트는 별도 정책이 없으면 영향 없음.

---

## 0. 사전 준비 — 공통 변수

PowerShell 세션 시작 시 한 번:

```powershell
$FX     = "http://localhost:9110"
$MEDIA  = "C:\project\FrameExtractor\media"      # 호스트 영상 폴더
$FRAMES = "C:\project\FrameExtractor\frames"     # 호스트 결과 폴더
```

(이 변수들은 아래 모든 예제에서 그대로 사용)

---

## 1. 빠른 참조 — 한 줄 명령

| 작업 | 명령 |
|---|---|
| 헬스 체크 | `curl.exe -s $FX/api/health` |
| 영상 목록 | `curl.exe -s $FX/api/videos` |
| 영상 단건 조회 | `curl.exe -s $FX/api/videos/<vid>` |
| 영상 업로드 | `curl.exe -F "files=@C:\path\clip.mp4" $FX/api/videos` |
| 다중 업로드 | `curl.exe -F "files=@a.mp4" -F "files=@b.mp4" $FX/api/videos` |
| 폴더 재스캔 | `curl.exe -X POST $FX/api/videos/rescan` |
| 영상 삭제 | `curl.exe -X DELETE $FX/api/videos/<vid>` |
| 단건 추출 잡 생성 | `curl.exe -X POST -H "Content-Type: application/json" -d '{\"target_fps\":5}' $FX/api/videos/<vid>/jobs` |
| 배치 추출 잡 생성 | `Invoke-RestMethod -Method Post -Uri $FX/api/jobs/batch -ContentType application/json -Body $jsonBody` |
| 잡 상태 확인 | `curl.exe -s $FX/api/jobs/<jid>` |
| 잡 취소 | `curl.exe -X POST $FX/api/jobs/<jid>/cancel` |
| 프레임 메타 목록 | `curl.exe -s "$FX/api/jobs/<jid>/frames?size=500"` |
| 단일 프레임 다운로드 | `curl.exe -O $FX/frames/<영상이름>/<영상이름>_00001.png` |
| 잡 전체 PNG 복사 | `Copy-Item "$FRAMES\<영상이름>\*.png" -Destination D:\out\` |
| 프레임 삭제 | `curl.exe -X DELETE $FX/api/frames/<fid>` |

---

## 2. 상세 명령

### 2.1 헬스 체크

```powershell
curl.exe -s $FX/api/health
# {"ok":true,"now":"2026-05-20T..."}
```

DB 연결까지 확인. 컨테이너 부팅 직후나 트러블슈팅 시.

### 2.2 영상 업로드 (브라우저 우회)

브라우저 [업로드] 버튼과 동일한 multipart 업로드를 curl로:

```powershell
# 단일 파일
curl.exe -F "files=@C:\videos\clip_a.mp4" $FX/api/videos

# 다중 파일 (-F 여러 번)
curl.exe -F "files=@C:\videos\clip_a.mp4" `
         -F "files=@C:\videos\clip_b.mp4" `
         -F "files=@C:\videos\clip_c.mp4" $FX/api/videos

# 폴더 안 모든 mp4 (PowerShell loop으로 -F 인자 동적 구성)
$args = Get-ChildItem C:\videos\session1 -Filter *.mp4 |
        ForEach-Object { "-F"; "files=@$($_.FullName)" }
curl.exe @args $FX/api/videos
```

응답:
```json
{
  "uploaded": [{"id":"...","filename":"clip_a.mp4", ...}, ...],
  "failed":   [],
  "skipped":  []
}
```

> 호스트 디스크에서 `media/`로 직접 복사하는 게 더 빠르면 그쪽이 권장 (§2.3 재스캔). 업로드는 multipart 인코딩 오버헤드가 있음.

### 2.3 폴더 재스캔 (권장 흐름)

호스트의 `C:\project\FrameExtractor\media\`에 영상 파일을 그냥 복사한 뒤:

```powershell
# 1) 파일 복사
Copy-Item C:\videos\session1\*.mp4 -Destination $MEDIA -Force

# 2) DB에 등록 시키기 (재스캔)
curl.exe -s -X POST $FX/api/videos/rescan
```

응답:
```json
{
  "media_dir": "/data/media",
  "added": [{"id":"...","filename":"clip_a.mp4",...}, ...],
  "skipped_registered": ["already_known.mp4"],
  "skipped_non_video":  ["notes.txt"]
}
```

이미 등록된 파일은 자동 skip. 비영상 확장자(txt, jpg 등)도 silent skip.

### 2.4 영상 목록 / 단건 / 삭제

```powershell
# 전체 목록
curl.exe -s $FX/api/videos

# JSON 깔끔하게 보기 (PowerShell 풀어줌)
Invoke-RestMethod $FX/api/videos |
    Select-Object id, filename, container_ext, width, height, duration_sec, src_fps |
    Format-Table -AutoSize

# 단건
$vid = "9af93318-519b-45f1-8fe5-a0cc83c40d8c"
curl.exe -s $FX/api/videos/$vid

# 삭제 (DB 행 + 호스트 파일 + 모든 잡/프레임 cascade)
curl.exe -X DELETE $FX/api/videos/$vid
```

### 2.5 추출 잡 생성 — 단건

```powershell
$vid = "9af93318-519b-45f1-8fe5-a0cc83c40d8c"

# (a) curl + JSON 이스케이프 (Windows PowerShell의 큰따옴표 이스케이프 주의)
curl.exe -X POST `
    -H "Content-Type: application/json" `
    -d '{\"target_fps\":5,\"resize_w\":640,\"resize_h\":360}' `
    "$FX/api/videos/$vid/jobs"

# (b) Invoke-RestMethod (Windows에서 가장 깨끗)
$body = @{
    target_fps     = 5
    resize_w       = 640
    resize_h       = 360
    head_skip_sec  = 0
    tail_skip_sec  = 0
} | ConvertTo-Json
Invoke-RestMethod -Method Post `
    -Uri "$FX/api/videos/$vid/jobs" `
    -ContentType application/json `
    -Body $body
```

옵션 전체 (생략 시 기본값 사용):

| 키 | 기본 | 의미 |
|---|---|---|
| `target_fps` | 5 | 초당 추출할 프레임 수 (다운샘플링만) |
| `interval_sec` | null | 지정 시 매 X초마다 1장 (target_fps 무시) |
| `resize_w` / `resize_h` | null | 둘 다 지정 시 INTER_AREA로 리사이즈 |
| `head_skip_sec` | 0 | 앞쪽 N초 제외 |
| `tail_skip_sec` | 0 | 뒤쪽 N초 제외 |
| `sampling_mode` | `"uniform"` | `"uniform"` / `"random_n"` |
| `random_n` | null | random_n 모드에서 추출할 총 장수 |
| `seed` | null | random_n 모드의 난수 시드 (재현용) |
| `format` | `"png"` | (현재 PNG 고정) |

### 2.6 추출 잡 생성 — 배치 (여러 영상에 동일 옵션)

```powershell
# 모든 영상 ID 모으기
$ids = Invoke-RestMethod $FX/api/videos | ForEach-Object { $_.id }

$body = @{
    video_ids = $ids
    params = @{
        target_fps    = 3
        resize_w      = 1280
        resize_h      = 720
        head_skip_sec = 2
        tail_skip_sec = 2
    }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Post `
    -Uri "$FX/api/jobs/batch" `
    -ContentType application/json `
    -Body $body
```

응답: `{"created":[<job>, ...], "failed":[...]}`. 부분 실패가 있어도 성공분은 큐로 들어감.

특정 영상만 골라서:
```powershell
$selected = @(
    "9af93318-...",
    "5ab10e22-..."
)
$body = @{ video_ids = $selected; params = @{ target_fps = 5 } } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri "$FX/api/jobs/batch" -ContentType application/json -Body $body
```

### 2.7 잡 상태 확인 + 폴링

```powershell
$jid = "adcd91f6-f7e4-448c-8009-ac75e459f126"

# 단발
curl.exe -s $FX/api/jobs/$jid

# 폴링 루프 — 완료될 때까지
do {
    Start-Sleep -Seconds 2
    $s = Invoke-RestMethod "$FX/api/jobs/$jid"
    Write-Host "$($s.status) $($s.progress_pct)% $($s.frames_done)/$($s.frames_total)"
} while ($s.status -in 'queued','running')
```

### 2.8 잡 취소

```powershell
curl.exe -X POST $FX/api/jobs/$jid/cancel
# {"ok":true,"signalled":true}
```

이미 추출된 프레임은 그대로 유지. queued면 무시되고 running이면 다음 프레임 직전에 중단.

### 2.9 프레임 메타 목록

```powershell
# 한 번에 많이
curl.exe -s "$FX/api/jobs/$jid/frames?size=500"

# 페이지네이션
curl.exe -s "$FX/api/jobs/$jid/frames?page=2&size=100"
```

응답: `{"rows":[{id, frame_index, time_sec, file_path, width, height, ...}], "total":..., "page":..., "size":...}`.

### 2.10 프레임 다운로드

세 가지 방법 — 상황에 맞게:

> 폴더 레이아웃: 각 영상의 모든 잡 결과는 `$FRAMES\<영상이름>\` 한 폴더를 공유합니다 (옛 PNG는 새 잡 시작 시 자동 정리). PNG 명: `<영상이름>_NNNNN.png`.

**A. 호스트 디스크에서 직접 복사 (가장 빠름)**
```powershell
# 영상별 폴더 통째로
$stem = "clip_a"            # 확장자 뺀 영상 이름
Copy-Item "$FRAMES\$stem\*" -Destination D:\out\session1\ -Force

# 모든 영상의 모든 PNG를 한 폴더로
Get-ChildItem $FRAMES -Recurse -Filter '*.png' | Copy-Item -Destination D:\out\ -Force
```

**B. HTTP로 받기 (원격 서버일 때)**
```powershell
# 단일 파일
curl.exe -O "$FX/frames/$stem/${stem}_00001.png"

# 잡 전체 — frames API에서 path 받아 loop
$jid = "adcd91f6-..."
$frames = Invoke-RestMethod "$FX/api/jobs/$jid/frames?size=10000"
New-Item -ItemType Directory -Force "D:\out\$jid" | Out-Null
foreach ($f in $frames.rows) {
    $rel = $f.file_path -replace '^/data/frames/',''       # "<stem>/<stem>_NNNNN.png"
    $name = ($rel -split '/')[-1]
    curl.exe -s -o "D:\out\$jid\$name" "$FX/frames/$rel"
}
```

**C. tar/zip으로 묶어서 받기**
- 현재 API에는 zip endpoint가 없습니다. 필요하면 호스트에서:
```powershell
Compress-Archive -Path "$FRAMES\$stem\*" -DestinationPath "D:\out\$stem.zip"
```

### 2.11 프레임 개별 삭제

```powershell
$fid = "12345678-..."
curl.exe -X DELETE $FX/api/frames/$fid
```

---

## 3. 자주 쓰는 워크플로 (붙여넣기 가능)

### A. 폴더 통째로 떨궈서 일괄 추출 → PC로 받기

```powershell
$FX     = "http://localhost:9110"
$MEDIA  = "C:\project\FrameExtractor\media"
$FRAMES = "C:\project\FrameExtractor\frames"
$OUT    = "D:\out\session_2026-05-20"

# 1) 원본 영상을 media 폴더에 복사
Copy-Item C:\videos\session_2026-05-20\*.mp4 -Destination $MEDIA -Force

# 2) DB 등록
$rescan = Invoke-RestMethod -Method Post "$FX/api/videos/rescan"
Write-Host "added: $($rescan.added.Count), already: $($rescan.skipped_registered.Count)"

# 3) 방금 등록된 영상에만 배치 추출
$ids = $rescan.added | ForEach-Object { $_.id }
if ($ids.Count -eq 0) { Write-Host "새로 추가된 영상이 없습니다"; return }

$body = @{
    video_ids = $ids
    params = @{ target_fps = 5; resize_w = 1280; resize_h = 720 }
} | ConvertTo-Json -Depth 5
$batch = Invoke-RestMethod -Method Post -Uri "$FX/api/jobs/batch" `
    -ContentType application/json -Body $body
$jobIds = $batch.created | ForEach-Object { $_.id }
Write-Host "$($jobIds.Count) jobs queued"

# 4) 모든 잡 완료 대기
while ($true) {
    $statuses = $jobIds | ForEach-Object { (Invoke-RestMethod "$FX/api/jobs/$_").status }
    $pending = $statuses | Where-Object { $_ -in 'queued','running' }
    Write-Host ("[" + (Get-Date -Format HH:mm:ss) + "] " + ($statuses -join ', '))
    if (-not $pending) { break }
    Start-Sleep -Seconds 3
}

# 5) PNG들을 PC 출력 폴더로 복사
New-Item -ItemType Directory -Force $OUT | Out-Null
foreach ($jid in $jobIds) {
    Copy-Item "$FRAMES\$jid\*.png" -Destination $OUT -Force
}
Write-Host "Saved to $OUT"
```

### B. 진행 중인 잡 모두 취소

```powershell
$jobs = Invoke-RestMethod $FX/api/videos | ForEach-Object {
    Invoke-RestMethod "$FX/api/videos/$($_.id)"
    # /api/videos에는 잡 목록이 없음 — 잡 단건 API로 일일이 확인하는 게 효율적이지 않으므로
    # 직접 DB 조회가 더 빠름 (§4 DB 직접 조회 참조)
}
```

대안 — DB로 진행 중 잡 ID 뽑아서:
```powershell
$jobIds = docker exec fx-postgres psql -U postgres -d frames_db -At -c `
    "SELECT id FROM job WHERE status IN ('queued','running')"
$jobIds -split "`n" | Where-Object { $_ } | ForEach-Object {
    curl.exe -s -X POST "$FX/api/jobs/$_/cancel"
}
```

### C. 영상별로 추출 결과(PNG 개수) 집계

```powershell
docker exec fx-postgres psql -U postgres -d frames_db -c @"
SELECT v.filename, j.id::text AS job_id, j.status, j.frames_done, j.frames_total
FROM job j JOIN video v ON v.id = j.video_id
ORDER BY j.created_at DESC LIMIT 50;
"@
```

### D. failed 또는 cancelled 잡만 정리

```powershell
# 1) DB에서 대상 job_id 뽑기
$stale = docker exec fx-postgres psql -U postgres -d frames_db -At -c `
    "SELECT id FROM job WHERE status IN ('failed','cancelled')"

# 2) 호스트 frames 폴더에서 그 잡 디렉터리 삭제
$stale -split "`n" | Where-Object { $_ } | ForEach-Object {
    $dir = "$FRAMES\$_"
    if (Test-Path $dir) { Remove-Item -Recurse -Force $dir; "removed $dir" }
}

# 3) DB rows 삭제
docker exec fx-postgres psql -U postgres -d frames_db -c `
    "DELETE FROM job WHERE status IN ('failed','cancelled')"
```

---

## 4. DB 직접 조회 (API에 없는 쿼리)

```powershell
# 컨테이너 안 psql
docker exec -it fx-postgres psql -U postgres -d frames_db

# 단발 쿼리
docker exec fx-postgres psql -U postgres -d frames_db -c "SELECT count(*) FROM video"
docker exec fx-postgres psql -U postgres -d frames_db -c "SELECT count(*) FROM job WHERE status='running'"

# 호스트에서 직접 (포트 2348)
psql "postgresql://extractor_role:dev_extractor_pw@localhost:2348/frames_db"
```

자주 쓰는 쿼리:
```sql
-- 영상별 추출 잡 수 + 총 프레임 수
SELECT v.filename,
       count(j.id)            AS jobs,
       sum(j.frames_done)     AS total_frames
FROM video v LEFT JOIN job j ON j.video_id = v.id
GROUP BY v.filename
ORDER BY v.uploaded_at DESC;

-- 가장 큰 결과 폴더 (디스크 압박 진단)
SELECT j.id, v.filename, j.frames_done, j.status
FROM job j JOIN video v ON v.id = j.video_id
ORDER BY j.frames_done DESC LIMIT 10;

-- 1시간 내 생성된 잡
SELECT id, status, frames_done, frames_total, created_at
FROM job
WHERE created_at > NOW() - interval '1 hour'
ORDER BY created_at DESC;
```

---

## 5. 컨테이너 직접 명령

```powershell
# 컨테이너 로그 (실시간)
docker logs -f frame-extractor

# 워커 동작 확인
docker logs frame-extractor 2>&1 | Select-String "job_planned|job_done|worker_started"

# 컨테이너 안에서 ffprobe로 영상 정보 확인 (probe 실패한 영상 진단)
docker exec frame-extractor ffmpeg -hide_banner -i /data/media/myvideo.mp4 2>&1 |
    Select-String "Duration|Stream"

# 컨테이너 안 frames 폴더 트리
docker exec frame-extractor sh -c "ls /data/frames/ | head -20"

# 컨테이너 재시작
docker compose restart frame-extractor
```

---

## 6. PowerShell 팁

### JSON을 깔끔하게 보기
```powershell
Invoke-RestMethod $FX/api/videos | ConvertTo-Json -Depth 5
```

### curl 응답을 객체로 파싱
```powershell
$obj = (curl.exe -s $FX/api/videos) | ConvertFrom-Json
$obj[0].filename
```

### curl로 JSON POST 시 따옴표 이스케이프 회피 (here-string + 파일)
```powershell
@'
{ "target_fps": 5, "resize_w": 640, "resize_h": 360 }
'@ | Set-Content -Encoding ascii body.json
curl.exe -X POST -H "Content-Type: application/json" `
    --data "@body.json" "$FX/api/videos/$vid/jobs"
Remove-Item body.json
```

### Bash/Linux 환경에서는
```bash
FX=http://localhost:9110

# 업로드
curl -F "files=@clip.mp4" $FX/api/videos

# 배치
curl -X POST -H 'Content-Type: application/json' -d '{
  "video_ids": ["uuid1","uuid2"],
  "params": {"target_fps": 5, "resize_w": 1280, "resize_h": 720}
}' $FX/api/jobs/batch

# jq로 ID만 추출
curl -s $FX/api/videos | jq -r '.[].id'
```

---

## 7. 트러블슈팅

| 증상 | 명령으로 진단 |
|---|---|
| 잡이 영원히 `queued` | `docker logs frame-extractor 2>&1 \| Select-String worker` — 워커가 살아있는지 확인 |
| 잡 `failed` | `curl.exe -s $FX/api/jobs/$jid \| ConvertFrom-Json \| Select error_message` |
| 업로드 후 메타가 null | 컨테이너에서 `ffmpeg -i /data/media/<file>` 로 디코딩 가능 여부 확인 |
| 디스크 부족 | `Get-PSDrive C` (호스트) + `docker exec fx-postgres psql ... "SELECT pg_database_size('frames_db')"` |
| 잡 결과를 다시 받기 | §2.10 (호스트 `$FRAMES\<jid>\` 복사 또는 HTTP) |
