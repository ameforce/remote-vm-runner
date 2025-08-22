# QA VMware API Server

VMware 스냅샷 자동 복구 및 RDP 연결을 위한 REST API 서버입니다.

## 아키텍처

- **API 서버**: `REMOTE_VM_API_LISTEN_HOST:REMOTE_VM_API_PORT` (FastAPI/uvicorn)
- **클라이언트**: 로컬 PC (`REMOTE_VM_API_HOST:REMOTE_VM_API_PORT`로 API 접근)

## 사전 요구사항

### API 서버 호스트
- Windows OS
- VMware Workstation Pro 설치
- Python 3.12+
- VM 파일: `C:\VMware\Windows Server 2025 TEST\Windows Server 2025 TEST.vmx`

### 클라이언트 PC
- Windows OS  
- Python 3.12+
- 네트워크: `REMOTE_VM_API_HOST:REMOTE_VM_API_PORT` 접근 가능

## 설치 및 실행

### 1. 서버 설정

```bash
# 1. 프로젝트 파일 복사
# 프로젝트 파일을 API 서버 호스트에 복사

# 2. 패키지 설치
pip install -r requirements.txt

# 3. VM 경로 확인 및 수정 (필요시)
# qa_vm_api.py의 VM_MAP 설정 확인

# 4. 환경 변수 설정 (예시)
# PowerShell
$env:REMOTE_VM_API_LISTEN_HOST = "0.0.0.0"
$env:REMOTE_VM_API_PORT = "495"

# CMD
set REMOTE_VM_API_LISTEN_HOST=0.0.0.0
set REMOTE_VM_API_PORT=495

# 5. API 서버 실행
python main.py server
```

### 2. 클라이언트 PC 설정

```bash
# 1. 패키지 설치
pip install requests

# 2. 연결 테스트
python test_connection.py

# 3. VM 클라이언트 실행
python vm_cli.py
```

## 방화벽 설정

### Windows 방화벽
```cmd
# 관리자 권한으로 실행 (예: 495 포트 허용)
netsh advfirewall firewall add rule name="QA VMware API" dir=in action=allow protocol=TCP localport=%REMOTE_VM_API_PORT%
```

## VM 설정

### Ping 허용 (VM 내부에서)
```cmd
# 관리자 권한으로 실행
netsh advfirewall firewall add rule name="Allow ICMP" protocol=icmpv4:8,any dir=in action=allow
```

### RDP 활성화 (VM 내부에서)
1. 시스템 속성 → 원격 탭
2. "이 컴퓨터에 대한 원격 연결 허용" 체크
3. 사용자 계정: `administrator` / `epapyrus`

## 사용법

### 기본 사용법
```bash
python vm_cli.py
```

1. 스냅샷 목록에서 원하는 번호 선택
2. 자동 복구 및 IP 획득 대기
3. RDP 클라이언트 자동 시작
4. 비밀번호 `epapyrus` 입력하여 접속

### API 직접 사용
```bash
# PowerShell (Windows)
$env:REMOTE_VM_API_HOST = "127.0.0.1"; $env:REMOTE_VM_API_PORT = "495"

# Bash (Linux/macOS/Git Bash)
export REMOTE_VM_API_HOST="${REMOTE_VM_API_HOST:-127.0.0.1}"
export REMOTE_VM_API_PORT="${REMOTE_VM_API_PORT:-495}"

# 스냅샷 목록 조회 (CMD 예시)
curl http://%REMOTE_VM_API_HOST%:%REMOTE_VM_API_PORT%/snapshots?vm=init

# 스냅샷 복구 (CMD 예시)
curl -X POST -H "Content-Type: application/json" \
  -d '{"vm":"init","snapshot":"Init"}' \
  http://%REMOTE_VM_API_HOST%:%REMOTE_VM_API_PORT%/revert
```

## 트러블슈팅

### 연결 실패
```bash
# 연결 테스트
python test_connection.py

# 네트워크 확인
ping %REMOTE_VM_API_HOST%
telnet %REMOTE_VM_API_HOST% %REMOTE_VM_API_PORT%
```

### 로그 확인
- API 서버: 콘솔 출력으로 실시간 확인

## 파일 구조

```
remote-vm-runner/
├── main.py               # Entrypoint (server/client)
├── src/                  # 모듈
├── tests/                # 테스트
├── templates/            # 템플릿(.rdp 템플릿 등)
└── requirements.txt      # 패키지 목록
```

## 주요 기능

- 스냅샷 목록 자동 조회
- 스냅샷 복구 자동화
- IP 주소 자동 획득
- Ping 기반 VM 준비 상태 확인
- RDP 자동 연결 (사용자명 사전 입력)
- VMware 메시지박스 방지 

## 구성 옵션

- 환경변수
  - `GUEST_USER` / `GUEST_PASS`: 게스트 OS 로그인 자격 증명
  - `RDP_TEMPLATE_PATH`: `.rdp` 생성 시 사용할 템플릿 경로(기본: `templates/rdp_template.rdp`)
  - `REQUIRE_GUEST_CREDENTIALS`: true로 설정하면 서버 시작 시 `GUEST_USER`/`GUEST_PASS` 미설정일 경우 시작을 거부하고 에러 로그를 남깁니다. 기본값 false.
  - `RDP_DETECTION_MODE`: `thorough|hybrid|fast|tcp|off` (기본: `hybrid`)
    - thorough: 게스트 내 PowerShell/쿼리 기반, 정확도 높음(부하 큼)
    - hybrid: fast 1차 + 불확실 시 thorough 재검증(기본)
    - fast: `query.exe`/`qwinsta` 우선, 실패 시 `listProcessesInGuest`(경량)
    - tcp: 게스트 명령 미실행, 호스트에서 TCP로 RDP 포트 확인(최소 부하)
    - off: RDP 활동 감지 비활성화
  - `RDP_CHECK_CONCURRENCY`: 한 tick당 병렬 감지 수(기본: 2)
  - `RDP_CHECK_BATCH_SIZE`: 한 tick당 감지할 VM 수 제한(0=무제한)
  - `CPU_SAMPLE_DURATION_SEC`: psutil CPU 샘플링 윈도우(기본: 1.0초). Windows에서는 우선적으로 `typeperf`/`Get-Counter` 기반 1초 샘플을 사용하여 작업관리자와 일치하도록 측정합니다.

운영 팁:
- CPU 스파이크가 보이면 `RDP_DETECTION_MODE=tcp`, `RDP_CHECK_CONCURRENCY=1`로 시작해 부하를 확인하세요.
- 정확도가 더 필요하면 `fast` 또는 `hybrid`로 올리되 동시성은 낮게 유지하세요.

### 리소스 압력 및 CPU 샘플링

Windows 호스트에서 CPU 사용률은 작업관리자와의 일치도를 높이기 위해 다음 순서로 측정됩니다:

- 우선: `typeperf "\\Processor(_Total)\\% Processor Time" -sc 1` (약 1초 샘플)
- 폴백: PowerShell `Get-Counter '\\Processor(_Total)\\% Processor Time' -SampleInterval 1 -MaxSamples 1`
- 최종 폴백: `psutil.cpu_percent(interval=CPU_SAMPLE_DURATION_SEC)`

기본 `CPU_SAMPLE_DURATION_SEC`는 1.0초로 상향되어 순간적인 지터를 줄이고 작업관리자 그래프와 더 유사한 값을 제공합니다. 로그의 `cpu_avail`은 `100 - cpu_used_percent`로 표시됩니다.

### RDP 템플릿 사용법

`templates/rdp_template.rdp` 파일을 생성하고, 아래와 같이 플레이스홀더를 사용할 수 있습니다.

```
screen mode id:i:2
desktopwidth:i:1920
desktopheight:i:1080
session bpp:i:32
full address:s:{ip}
username:s:{username}
prompt for credentials:i:0
promptcredentialonce:i:1
authentication level:i:0
negotiate security layer:i:1
enablecredsspsupport:i:1
```

템플릿에 `full address`나 `username` 키가 비어있다면, 실행 시 자동으로 보강됩니다.