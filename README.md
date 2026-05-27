# FrameExtractor

영상에서 PNG 프레임을 추출하는 웹 어드민 도구. CLI 없이 브라우저에서 영상 업로드 → 추출 설정 → 결과 갤러리/다운로드까지 처리한다.

- **기본 입력**: mp4 (그 외 mov, avi, mkv, webm, m4v도 OpenCV가 받는 한 동작)
- **추출 옵션**: 초당 N프레임, X초마다 1장, 랜덤 N장, head/tail 자르기, resize
- **출력**: PNG (`/data/frames/<영상이름>/<영상이름>_NNNNN.png`)
- **메타 저장**: 자체 PostgreSQL 컨테이너 (`fx-postgres`)
- **실행 모델**: 백그라운드 asyncio 워커 풀 (기본 2개), 진행률 폴링 + 취소 지원
- **네트워크**: 완전 독립 (`fx-net`). 다른 레포(SocketDaim 등)와 무관.

---

## 빠른 시작

```powershell
cd C:\project\FrameExtractor
docker compose up -d --build

# 두 컨테이너 healthy 확인
docker compose ps

# 어드민 UI
start http://localhost:9110/
```

종료/초기화:

```powershell
docker compose down            # 데이터 보존
docker compose down -v         # 메타 DB + 업로드/추출 파일 모두 삭제
```

> ⚠️ `docker compose down -v`는 `fx-pgdata` 볼륨만 지운다. 호스트 bind mount인 `./media/`, `./frames/` 파일은 그대로 남는다 (필요하면 수동 삭제).

---

## 사용 흐름

1. **업로드** — `http://localhost:9110/`의 업로드 폼에서 영상 선택. OpenCV가 자동으로 duration / fps / 해상도 추출.
2. **추출 작업 생성** — 영상 상세 페이지(`/videos/<id>`)에서 옵션 입력 후 [추출 시작]. 잡이 큐에 들어가고 워커가 즉시 처리.
3. **진행 모니터링** — 잡 상세 페이지(`/jobs/<id>`)에서 2초 간격 폴링으로 진행률 표시. 취소 버튼으로 중단 가능 (이미 추출된 프레임은 유지).
4. **결과 확인** — 잡 상세에 갤러리 형태로 PNG 썸네일 표시. 개별 프레임 삭제 또는 원본 PNG 다운로드 가능.

---

## 추출 옵션

| 파라미터 | 기본 | 의미 |
|---|---|---|
| `target_fps` | 5 | 초당 추출할 프레임 수 (다운샘플링만 — 원본 fps보다 클 수 없음) |
| `interval_sec` | null | 지정 시 매 X초마다 1장 (target_fps 무시) |
| `resize_w` / `resize_h` | null | 둘 다 지정 시 cv2.INTER_AREA로 리사이즈 |
| `head_skip_sec` | 0 | 영상 앞쪽 N초 제외 |
| `tail_skip_sec` | 0 | 영상 뒤쪽 N초 제외 |
| `sampling_mode` | `uniform` | `uniform` / `random_n` |
| `random_n` | null | `random_n` 모드일 때 추출할 총 장수 |
| `seed` | null | `random_n` 모드의 난수 시드 (재현용) |
| `format` | `png` | 현재 PNG 고정. JPEG/WebP 확장 여지 있음 |

---

## API

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET    | `/` | 영상 목록 + 업로드 폼 (HTML) |
| GET    | `/videos/{id}` | 영상 상세 + 추출 폼 + 잡 이력 (HTML) |
| GET    | `/jobs/{id}` | 잡 상세 + 프레임 갤러리 (HTML) |
| POST   | `/api/videos` | multipart 업로드 (`file=<binary>`) |
| GET    | `/api/videos` | 전체 영상 메타 목록 |
| GET    | `/api/videos/{id}` | 단건 |
| DELETE | `/api/videos/{id}` | 영상 + 잡 + 프레임 + 파일 cascade 삭제 |
| POST   | `/api/videos/{id}/jobs` | 추출 잡 생성 (JSON body) |
| GET    | `/api/jobs/{id}` | 잡 상태 폴링용 |
| POST   | `/api/jobs/{id}/cancel` | 큐/실행 중 잡 취소 |
| GET    | `/api/jobs/{id}/frames` | 프레임 메타 페이지네이션 |
| DELETE | `/api/frames/{id}` | 단일 프레임 + 파일 삭제 |
| GET    | `/api/health` | DB ping |
| GET    | `/media/*` | 업로드 원본 정적 서빙 |
| GET    | `/frames/*` | 추출 PNG 정적 서빙 |

---

## API 사용 예 (PowerShell)

```powershell
# 1) 업로드
$resp = Invoke-RestMethod -Method Post `
    -Uri http://localhost:9110/api/videos `
    -Form @{ file = Get-Item C:\path\to\sample.mp4 }
$videoId = $resp.id
$resp

# 2) 기본 추출 (5fps, 640x360)
$job = Invoke-RestMethod -Method Post `
    -Uri "http://localhost:9110/api/videos/$videoId/jobs" `
    -ContentType application/json `
    -Body (@{ target_fps = 5; resize_w = 640; resize_h = 360 } | ConvertTo-Json)
$jobId = $job.id

# 3) 진행 폴링
do {
    Start-Sleep -Seconds 2
    $s = Invoke-RestMethod "http://localhost:9110/api/jobs/$jobId"
    Write-Host "$($s.status) $($s.progress_pct)% ($($s.frames_done)/$($s.frames_total))"
} while ($s.status -in @('queued','running'))

# 4) 결과 파일
Get-ChildItem "C:\project\FrameExtractor\frames\$jobId"

# 5) 랜덤 20장 + head/tail 자르기
Invoke-RestMethod -Method Post `
    -Uri "http://localhost:9110/api/videos/$videoId/jobs" `
    -ContentType application/json `
    -Body (@{
        sampling_mode = 'random_n'
        random_n = 20
        head_skip_sec = 3
        tail_skip_sec = 2
        resize_w = 1024
        resize_h = 576
        seed = 42
    } | ConvertTo-Json)
```

---

## 환경변수 ([docker-compose.yml](docker-compose.yml) 참조)

| 변수 | 기본 | 의미 |
|---|---|---|
| `FX_HTTP_HOST` / `FX_HTTP_PORT` | `0.0.0.0` / 9110 | HTTP 바인딩 |
| `FX_DB_HOST` / `FX_DB_PORT` | `fx-postgres` / 5432 | 메타 DB 위치 |
| `FX_DB_NAME` / `FX_DB_USER` / `FX_DB_PASSWORD` | `frames_db` / `extractor_role` / `dev_extractor_pw` | DB 자격증명 (dev only) |
| `FX_MEDIA_DIR` / `FX_FRAMES_DIR` | `/data/media` / `/data/frames` | 컨테이너 내부 마운트 경로 |
| `FX_WORKERS` | 2 | 동시 처리 워커 수 |
| `FX_DEFAULT_TARGET_FPS` | 5 | UI 폼 기본값 |
| `FX_MAX_UPLOAD_MB` | 2048 | 업로드 크기 상한 |
| `FX_LOG_LEVEL` / `FX_LOG_FORMAT` | `INFO` / `json` | structlog 설정 |

---

## 디렉터리 구조

```
FrameExtractor/
├── docker-compose.yml      # fx-postgres + frame-extractor
├── Dockerfile              # python:3.11-slim + ffmpeg + opencv-python-headless
├── init_db.sql             # video / job / frame + extractor_role
├── requirements.txt
├── pyproject.toml
├── README.md
├── media/                  # 호스트 bind mount: 업로드 영상
├── frames/                 # 호스트 bind mount: 추출 PNG
└── src/frame_extractor/
    ├── __main__.py
    ├── main.py             # uvicorn + pool + 워커 부팅
    ├── config.py           # pydantic-settings (FX_*)
    ├── logging_config.py
    ├── app.py              # FastAPI 라우트 (HTML + JSON)
    ├── job_queue.py        # asyncio.Queue + 워커 + 취소 이벤트
    ├── extractor/
    │   ├── opencv_backend.py   # probe + open_capture
    │   ├── sampling.py         # uniform / random_n / head-tail 정책
    │   └── worker.py           # 한 job 실행 (seek → decode → resize → PNG)
    ├── repository/             # asyncpg + repo 클래스
    ├── static/{css,js}/        # admin.css + admin.js
    └── templates/              # index / video_detail / job_detail
```

---

## 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| 컨테이너 시작 직후 `db_connect_retry` 로그 | postgres healthcheck 직후 잠시 connect refused — 자동 재시도 (1~5초 내 OK) |
| 업로드는 되는데 메타가 비어있음 | OpenCV가 해당 코덱을 디코딩 못함. 컨테이너에 ffmpeg가 깔려 있어도 일부 코덱은 빠질 수 있음 — 영상은 그대로 저장되지만 추출 시 실패할 수 있다 |
| 잡이 `running`에서 멈춤 | 워커 풀이 다른 잡으로 막혀있을 수 있음. `FX_WORKERS`를 늘리거나 잡 취소 |
| 컨테이너 재기동 시 진행 중이던 잡 | 부팅 시 `running` → `queued`로 리셋되어 자동 재투입 (DB row 유지) |
| 호스트에서 `frames/`/`media/` 권한 오류 | `user: "1000:1000"` 설정과 호스트 UID 불일치. compose에서 `user:` 라인 조정 |
| 갤러리 이미지 404 | bind mount 경로와 DB `file_path` 불일치. `FX_FRAMES_DIR`이 컨테이너 안에서 `/data/frames`인지 확인 |

---

## 향후 확장 여지

- JPEG/WebP 출력 (`format` 컬럼은 이미 존재)
- 페이지네이션·필터링이 있는 프레임 목록
- 다른 모노레포(예: SocketDaim) 네트워크와 join — `external network`로 [docker-compose.yml](docker-compose.yml) 수정
- SSE 기반 실시간 진행률 (현재는 폴링)
- 폐쇄망 배포: `docker compose build` → `docker save -o frameextractor.tar frameextractor-frame-extractor postgres:16` → 서버 `docker load` (자세한 절차는 [../QUICKSTART.md](../QUICKSTART.md) §8)
