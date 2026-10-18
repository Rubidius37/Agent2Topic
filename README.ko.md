# Project_Agent

[English](README.md) · **한국어**

**시간순으로 기록하고 주제·파일별 맥락으로 복구하는 AI 에이전트 & 개발자 겸용 Markdown 작업 이력 컴파일러**

AI 코딩·연구 에이전트(Codex, Claude Code, OpenCode)가 여러 세션에 걸쳐 프로젝트를 진행할 때, 단일 메모리 파일(`MEMORY.md`)에 모든 내용을 계속 덧붙이면 컨텍스트 윈도우가 금방 넘치고, 벡터 DB를 쓰면 시간순 인과관계(*"지난주에 왜 A 설계를 버리고 B로 바꿨는가?"*)와 원문 출처가 사라집니다.

**Project_Agent**는 **LLM 토큰 소모 없이 결정론적 Python 분류 엔진**으로 이 문제를 해결합니다:
1. **에이전트(또는 사람)가 날짜별 원본 일지(`Workspace/Daily_Logs/`)에 작업 기록을 추가**합니다 (`--files`, `--kind` 메타데이터 지원).
2. **Python 규칙 기반 분류기**가 영구 `Source ID`로 각 기록 구간을 추적하며 주제별 상세 문서(`Workspace/Subject/`)와 컨텍스트 길이가 통제된 연대기 리뷰(`Workspace/Topic_Reviews/`)를 자동 컴파일합니다.
3. **에이전트는 다음 작업 시** 토픽별 조회(`pagent context`)뿐 아니라 **수정한 파일 경로·키워드·기록 종류(`pagent search --file ... --kind ...`)로 과거 이력을 0.1초 만에 역추적**합니다.
4. **사람은 언제든** Obsidian, VS Code 또는 내장된 **읽기 전용 로컬 웹 뷰어(`pagent view`)**로 폴더 트리, 문서 내 목차(TOC), 렌더링된 마크다운을 편하게 열람할 수 있습니다.

---

## 핵심 기능

- **분류·컴파일 시 LLM 토큰 소모 0**: 문서 분류, 구간 병합, 상대 링크 재계산, 리뷰 생성은 외부 API나 LLM 호출 없이 100% Python 표준 라이브러리로 즉시 수행됩니다.
- **파일 경로·키워드·종류 기반 역추적 (`pagent search`)**: 에이전트가 토픽 이름을 몰라도 `--file "review_renderer.py"`, `--kind "decision"`, `--query "atomic_write"`로 관련 `Source ID` 기록과 토픽 리뷰 경로를 즉시 찾습니다 (본문 내 백틱 `` `경로/파일.py` `` 자동 인덱싱 포함).
- **동일 날짜·동일 토픽 스마트 병합 (`append`)**: 같은 날 같은 토픽(또는 형제 토픽)으로 연속 기록할 때 상위 `#`~`#####` Heading을 중복 생성하지 않고 깔끔하게 병합합니다.
- **전역 CLI 설치 및 CWD 자동 탐색 (`pyproject.toml`)**: `pip install -e .` (또는 `uv tool install .`)로 설치하면 현재 작업 디렉터리(CWD) 상위의 `Workspace/`를 자동으로 찾으며, 외부 프로젝트에서도 `pagent init --here`로 즉시 사용할 수 있습니다.
- **읽기 전용 로컬 웹 뷰어 (`pagent view`)**: 좌측 폴더 트리, `검토 필요(Needs Review)` 배지, 우측 **문서 내 목차(On-page TOC)**, 상대 링크 내부 이동을 지원하며 브라우저 탭을 모두 닫으면 3초 후 서버가 자동 종료됩니다.

---

## 빠른 시작

**요구 사항**: Python 3.10 이상 (외부 패키지 설치 불필요, 100% 표준 라이브러리).

```powershell
# 선택 사항: 전역 `pagent` 명령어로 등록
pip install -e .
```

### 1. 작업공간 초기화
```powershell
# 기본 Workspace 초기화 (외부 프로젝트 폴더에 만들 때는 --here 추가)
.\pagent.bat init
```

### 2. 작업 기록 추가 및 자동 컴파일 (`append`)
```powershell
.\pagent.bat append `
  --path "Projects/CoreEngine/Parser" `
  --title "코드 블록 경계 파싱 수정" `
  --kind "bugfix" `
  --files "scripts/hierarchical_classifier.py" `
  --content "물결표(~~~) 코드 블록 내부의 Heading이 분리되지 않도록 수정함." `
  --create-rule
```

### 3. 파일 경로·키워드·토픽으로 과거 맥락 조회 (`search` / `context`)
```powershell
# 특정 파일과 관련된 과거 버그 수정·설계 결정 검색
.\pagent.bat search --file "hierarchical_classifier.py" --json

# 특정 토픽의 시간순 요약 리뷰 및 최근 기록 조회
.\pagent.bat context --topic "Projects/CoreEngine" --limit 5 --json
```

### 4. 읽기 전용 로컬 웹 뷰어 열기 (`view`)
```powershell
.\pagent.bat view
```

---

## CLI 명령어 요약

| 명령어 | 설명 |
| :--- | :--- |
| `pagent init [--here]` | `Workspace/Daily_Logs/`, `attachments/`, `Classification_Rules.md` 초기 생성 |
| `pagent update [--dry-run] [--rebuild-ids]` | Python 분류기를 실행하여 `Subject/` 및 `Topic_Reviews/` 갱신 |
| `pagent rules [--json]` | `Classification_Rules.md`의 5단계 카테고리 경로 및 키워드 목록 조회 |
| `pagent rule-add --path "..." [--keywords "..."]` | 규칙표에 새 카테고리 경로 행 자동 삽입 |
| `pagent append --path "..." [--title "..."] [--files "..."] [--kind "..."] [--create-rule]` | 오늘 날짜 일지에 기록 추가(스마트 병합 및 본문 Heading 자동 보호) 후 즉시 컴파일 |
| `pagent search [--query "..."] [--file "..."] [--kind "..."] [--topic "..."] [--json]` | 파일 경로, 키워드, 종류(`decision`, `bugfix` 등), 토픽으로 과거 기록 및 리뷰 검색 |
| `pagent context --topic "..." [--limit N] [--json]` | 특정 토픽의 `Topic_Reviews/` 문서와 최근 N건 기록 조회 |
| `pagent status [--json]` | 분류 통계 및 미분류(`needs_review`) 항목 확인 |
| `pagent classify --source-id <ID> --path "..."` | 특정 `Source ID`에 수동 `Category:` 지정 또는 해제(`--clear-manual`) |
| `pagent source --id <ID> [--json]` | 특정 `Source ID`의 원본 일지 위치, 메타데이터(`kind`, `files`), 본문 전문 역추적 |
| `pagent view [--port 8000]` | 사람 열람용 읽기 전용 로컬 웹 뷰어 실행 |

---

## 테스트 실행

```powershell
python -B -m unittest discover -s tests
```

## 라이선스

이 프로젝트는 [GNU General Public License v3.0 (GPL-3.0)](LICENSE)로 배포됩니다.
