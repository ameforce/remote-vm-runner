# QA VMware API Server

VMware 스냅샷 자동 복구 및 RDP 연결을 위한 REST API 서버입니다.

## 🏗️ 아키텍처

- **API 서버**: `192.168.0.6:495` (qa_vm_api.py)
- **클라이언트**: 로컬 PC (vm_cli.py)

## 📋 사전 요구사항

### 192.168.0.6 서버
- Windows OS
- VMware Workstation Pro 설치
- Python 3.12+
- VM 파일: `C:\VMware\Windows Server 2025 TEST\Windows Server 2025 TEST.vmx`

### 클라이언트 PC
- Windows OS  
- Python 3.12+
- 네트워크: 192.168.0.6 접근 가능

## 🚀 설치 및 실행

### 1. 192.168.0.6 서버 설정

```bash
# 1. 프로젝트 파일 복사
# qa_vm_api.py, requirements.txt를 192.168.0.6으로 복사

# 2. 패키지 설치
pip install -r requirements.txt

# 3. VM 경로 확인 및 수정 (필요시)
# qa_vm_api.py의 VM_MAP 설정 확인

# 4. API 서버 실행
python qa_vm_api.py
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

## 🔧 방화벽 설정

### Windows 방화벽 (192.168.0.6)
```cmd
# 관리자 권한으로 실행
netsh advfirewall firewall add rule name="QA VMware API" dir=in action=allow protocol=TCP localport=495
```

## 🖥️ VM 설정

### Ping 허용 (VM 내부에서)
```cmd
# 관리자 권한으로 실행
netsh advfirewall firewall add rule name="Allow ICMP" protocol=icmpv4:8,any dir=in action=allow
```

### RDP 활성화 (VM 내부에서)
1. 시스템 속성 → 원격 탭
2. "이 컴퓨터에 대한 원격 연결 허용" 체크
3. 사용자 계정: `administrator` / `epapyrus12#$`

## 📱 사용법

### 기본 사용법
```bash
python vm_cli.py
```

1. 스냅샷 목록에서 원하는 번호 선택
2. 자동 복구 및 IP 획득 대기
3. RDP 클라이언트 자동 시작
4. 비밀번호 `epapyrus12#$` 입력하여 접속

### API 직접 사용
```bash
# 스냅샷 목록 조회
curl http://192.168.0.6:495/snapshots?vm=init

# 스냅샷 복구
curl -X POST -H "Content-Type: application/json" \
  -d '{"vm":"init","snapshot":"Init"}' \
  http://192.168.0.6:495/revert
```

## 🔍 트러블슈팅

### 연결 실패
```bash
# 연결 테스트
python test_connection.py

# 네트워크 확인
ping 192.168.0.6
telnet 192.168.0.6 495
```

### VM 메시지박스 문제
- VMX 파일에서 CD-ROM 자동감지 비활성화 완료
- 더 이상 "Cannot connect virtual device" 메시지 없음

### 로그 확인
- API 서버: 콘솔 출력으로 실시간 확인
- 상세한 진행 상황 로그 포함

## 📁 파일 구조

```
core-qa-runner/
├── qa_vm_api.py          # API 서버 (192.168.0.6에서 실행)
├── vm_cli.py             # 클라이언트 (로컬 PC에서 실행)
├── test_connection.py    # 연결 테스트
├── requirements.txt      # 패키지 목록
└── README.md            # 이 파일
```

## 🎯 주요 기능

- ✅ 스냅샷 목록 자동 조회
- ✅ 스냅샷 복구 자동화
- ✅ IP 주소 자동 획득
- ✅ Ping 기반 VM 준비 상태 확인
- ✅ RDP 자동 연결 (사용자명 사전 입력)
- ✅ VMware 메시지박스 방지 