# Project_Agent

[English](README.md) · **한국어**

**시간순으로 기록하고 주제·파일별 맥락으로 복구하는 AI 에이전트 & 개발자 겸용 Markdown 작업 이력 컴파일러**

AI 코딩·연구 에이전트(Codex, Claude Code, OpenCode)가 여러 세션에 걸쳐 프로젝트를 진행할 때, 단일 메모리 파일(`MEMORY.md`)에 모든 내용을 계속 덧붙이면 컨텍스트 윈도우가 금방 넘치고, 벡터 DB를 쓰면 시간순 인과관계(*"지난주에 왜 A 설계를 버리고 B로 바꿨는가?"*)와 원문 출처가 사라집니다.

**Project_Agent**는 **LLM 토큰 소모 없이 결정론적 Python 분류 엔진**으로 이 문제를 해결합니다:
1. **에이전트(또는 사람)가 날짜별 원본 일지(`Workspace/Daily_Logs/`)에 개별 작업 이벤트(`Entry`)를 추가**합니다 (`--files`, `--kind`, `--git`/`--commit` 지원).
2. **Python 규칙 기반 분류기**가 상위 토픽(`#`~`#####`) 아래의 각 작업 엔트리(`###### <제목>`)마다 고유 `Source ID`를 부여하고, `organizer_metadata.json`에 검색 인덱스(`kind`, `files`, `commit`, `summary`)를 사전 구축하며, 주제별 상세 문서(`Subject/`)와 연대기 리뷰(`Topic_Reviews/`)를 컴파일합니다.
3. **에이전트는 다음 작업 시** 출력 길이가 엄격히 통제된 토픽 맥락(`pagent context --mode index|review|entries --max-chars 12000`)을 조회하거나, 사전 인덱싱된 메타데이터에서 파일 경로·키워드·종류·커밋(`pagent search`)으로 과거 이력을 즉시 찾습니다.
4. **사람은 언제든** Obsidian, VS Code 또는 **순수 읽기 전용 로컬 웹 뷰어(`pagent view`)**로 폴더 트리, 문서 내 목차(TOC), 렌더링된 마크다운을 열람할 수 있습니다.

---

## 핵심 기능 (v0.3.0)

- **토픽 Heading 공유 + 개별 작업 `Entry` 독립 식별**: 같은 날짜·같은 토픽에 연속 `append`할 때 상위 `#`~`#####` 토픽 Heading은 중복 작성하지 않으면서, 각 `###### <제목>` 작업 이벤트는 독립된 `Source ID`, `Kind`, `Files`, `Commit`을 가진 별도 엔트리로 분리·보존됩니다.
- **출력 길이가 보장되는 Bounded Context (`pagent context`)**:
  - `--mode index`: 제목·날짜·Kind·Files·Commit·요약 목록만 저비용 조회
  - `--mode review` (기본값): 길이 제한된 토픽 리뷰 + 최근 엔트리 요약 반환
  - `--mode entries`: 선택된 최근 엔트리 본문 반환 (전체 응답은 `--max-chars` [기본 12,000자] 이내로 엄격히 제한되며, 원문 전체가 필요할 때만 `pagent source --id <ID>` 호출)
- **컴파일 시점 검색 인덱스 내장 (`pagent search`)**: `organizer_metadata.json`에 `kind`, `files`, `commit`, `summary`, `search_text`를 미리 인덱싱하여 검색 시 원본 Markdown 파일들을 다시 열지 않고 즉시 응답합니다.
- **Windows `.venv` 런처 스텁 프로세스 잠금 호환**: Windows `CreateToolhelp32Snapshot`으로 조상 PID 체인을 확인하여 `.venv` 가상환경에서도 다중 프로세스 잠금 위임이 정확히 동작합니다.
- **순수 읽기 전용 로컬 웹 뷰어 (`pagent view`)**: 기본적으로 원본 일지를 수정하는 분류기를 실행하지 않고 열람만 수행합니다 (`--refresh` 옵션을 줄 때만 분류기 갱신).

---

## CLI 명령어 요약

| 명령어 | 설명 |
| :--- | :--- |
| `pagent init [--here]` | `Workspace/Daily_Logs/`, `attachments/`, `Classification_Rules.md` 초기 생성 |
| `pagent update [--dry-run] [--rebuild-ids]` | Python 분류기를 실행하여 `Subject/`, `Topic_Reviews/`, 검색 인덱스 갱신 |
| `pagent rules [--json]` | `Classification_Rules.md`의 5단계 카테고리 경로 및 키워드 목록 조회 |
| `pagent rule-add --path "..." [--keywords "..."]` | 규칙표에 새 카테고리 경로 행 자동 삽입 |
| `pagent append --path "..." [--title "..."] [--files "..."] [--kind "..."] [--git \| --commit "..."] [--create-rule]` | 오늘 날짜 일지에 독립 `######` 작업 엔트리 추가 후 즉시 컴파일 |
| `pagent search [--query "..."] [--file "..."] [--kind "..."] [--commit "..."] [--topic "..."] [--json]` | 사전 구축된 인덱스에서 파일 경로, 키워드, 종류, Git 커밋, 토픽으로 검색 |
| `pagent context --topic "..." [--mode review\|index\|entries] [--max-chars 12000] [--limit N] [--json]` | 특정 토픽의 리뷰와 엔트리를 지정된 문자 예산 내에서 조회 |
| `pagent status [--json]` | 분류 통계 및 미분류(`needs_review`) 항목 확인 |
| `pagent classify --source-id <ID> --path "..."` | 특정 `Source ID`에 수동 `Category:` 지정 또는 해제(`--clear-manual`) |
| `pagent source --id <ID> [--max-chars N] [--json]` | 특정 `Source ID`의 원본 일지 위치, 메타데이터(`kind`, `files`, `commit`), 본문 전문 역추적 |
| `pagent view [--port 8000] [--refresh]` | 사람 열람용 순수 읽기 전용 로컬 웹 뷰어 실행 |
| `pagent version [--check] [--json]` | 현재 설치된 버전 확인 및 원격 GitHub 최신 릴리스 존재 여부 수동 조회 |

---

## 설치 및 업데이트 방법

사용자 데이터(`Workspace/`)와 프로그램 코드가 완전히 분리되어 있으므로, 도구를 업데이트해도 작성한 일지나 분류 규칙은 절대 덮어써지지 않습니다.

### 방법 A: `uv` 또는 `pipx` 사용 (권장)
```powershell
# 최초 설치
uv tool install git+https://github.com/Rubidius37/Project_Agent.git
# (또는: pipx install git+https://github.com/Rubidius37/Project_Agent.git)

# 최신 버전으로 업데이트
uv tool upgrade pagent
```

### 방법 B: `git clone` + `pip install -e .`
```powershell
git clone https://github.com/Rubidius37/Project_Agent.git
cd Project_Agent
pip install -e .

# 최신 버전으로 업데이트
git pull --ff-only
```

### 수동 버전 확인 (`pagent version --check`)
`pagent`는 백그라운드에서 몰래 네트워크 통신을 하거나 자동 설치를 수행하지 않습니다. 새 버전이 나왔는지 확인하고 싶을 때만 아래 명령을 실행하세요:
```powershell
pagent version --check
```

### 릴리스 및 추후 PyPI 자동 배포 안내 (유지보수자용)
`.github/workflows/release.yml`에 배포 파이프라인이 미리 준비되어 있습니다:
1. **GitHub Release 자동 생성**: `git tag v0.3.0 && git push origin v0.3.0`으로 태그를 푸시하면 테스트 검증 후 `.whl`, `.tar.gz`, `SHA256SUMS.txt`가 첨부된 GitHub Release가 자동 생성됩니다.
2. **추후 PyPI 공개 배포 활성화**: 나중에 정식 이름으로 PyPI에 배포하고 싶을 때는 [PyPI Trusted Publisher](https://docs.pypi.org/trusted-publishers/)에 저장소를 등록(환경 이름: `pypi`)한 뒤, GitHub 저장소 변수(`Settings > Secrets and variables > Actions > Variables`)에 `PYPI_ENABLED=true`만 추가하면 태그 푸시 시 PyPI까지 자동 업로드됩니다.

---

## 테스트 실행

```powershell
python -B -m unittest discover -s tests
```

## 라이선스

이 프로젝트는 [GNU General Public License v3.0 (GPL-3.0)](LICENSE)로 배포됩니다.
