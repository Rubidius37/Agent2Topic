# 분류 규칙

아래 표가 실제 자동 분류 규칙입니다. **표의 여섯 열과 열 순서는 바꾸지 마세요.** 새 Level 1은 첫 번째 열에 이름을 적은 행을 추가하면 됩니다. 자세한 작성 방법은 표 아래에서 확인할 수 있습니다.

## 실제 분류 규칙

| 대분류 (Level 1) | 중분류 (Level 2) | 소분류 (Level 3) | 상세 분류 (Level 4) | 세부 태스크 (Level 5) | 매칭 키워드 (쉼표: OR, 띄어쓰기: AND) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Projects | Sample Project | Planning | Requirements | | requirement, plan |
| | | Testing | Functional Test | | functional test |
| | | | Regression Test | | regression test |
| Knowledge | Methods | Data Analysis | | | analysis, dataset |
| | References | | | | reference, document |

## 사용 방법

이 문서는 일지의 Heading을 어떤 주제 경로로 분류할지 정의합니다. 처음에는 아래 세 가지 규칙만 기억하면 됩니다.

1. 일지의 `#`부터 `#####`까지를 표의 Level 1부터 Level 5까지의 이름과 맞춥니다.
2. Heading이 분류명과 정확히 일치하면 키워드는 없어도 됩니다.
3. 새 경로를 추가한 뒤 Log2Topic 트레이에서 `로컬 문서 갱신`을 실행해 결과를 확인합니다.

### Heading으로 분류하기

| 규칙 단계 | 일지에서 작성할 Heading |
| :--- | :--- |
| Level 1 대분류 | `#` |
| Level 2 중분류 | `##` |
| Level 3 소분류 | `###` |
| Level 4 상세 분류 | `####` |
| Level 5 세부 태스크 | `#####` |

다음 Heading은 `Projects > Sample Project > Testing > Functional Test` 경로로 분류됩니다. 아래 이름은 사용법을 설명하기 위한 가상 예시입니다.

```markdown
# Projects
## Sample Project
### Testing
#### Functional Test
```

규칙표에서 빈 Level 셀은 바로 위 행의 값을 이어받습니다. 같은 부모 아래에 하위 분류를 추가할 때 상위 이름을 반복하지 않아도 됩니다. 새 Level 1을 만들 때만 첫 번째 열에 이름을 적습니다.

일지에서도 같은 부모 아래의 형제 분류는 상위 Heading을 반복하지 않고 이어서 작성할 수 있습니다. 같은 레벨의 Heading은 앞서 나온 분류의 하위가 아니라 현재 부모 아래의 새 가지가 됩니다.

```markdown
# Level1
## Level2
### Level3
abc
### Level3-1
def
```

위 예시의 `abc`는 `Level1 > Level2 > Level3`, `def`는 `Level1 > Level2 > Level3-1`로 각각 분류됩니다. 반대로 `#### Functional Test`처럼 Heading 레벨을 하나 더 깊게 쓰면 `Level3` 아래의 하위 분류가 됩니다.

### 키워드는 필요할 때만 추가하기

`매칭 키워드`는 필수값이 아닙니다. Heading을 끝까지 작성하지 않은 내용을 하위 경로로 보조 분류할 때만 사용합니다.

- 쉼표로 나눈 키워드는 하나만 포함되어도 매칭됩니다. 예: `failure, error, 실패`
- 하나의 키워드 안에서 띄어 쓴 단어는 모두 포함되어야 합니다. 예: `test result`는 `test`와 `result`가 모두 있어야 합니다.
- 짧고 일반적인 단어보다 프로젝트명, 모듈명, 작업명처럼 구체적인 표현을 우선합니다.
- 서로 다른 Level 2 가지에 같은 이름이 있으면 상위 분류명이나 식별자를 키워드에 함께 적습니다.

### 오분류를 막는 경계 규칙

- 규칙표와 일치하는 H1은 Level 1 범위를 고정합니다. 해당 구간의 키워드는 다른 Level 1로 넘어가지 않습니다.
- H2부터 H5는 현재 부모 아래에 같은 이름의 분류가 있을 때만 경로를 확장합니다. 그 밖의 Heading은 본문 구조로 유지됩니다.
- 인식할 수 있는 H1이 없는 새 구간은 추측하지 않고 `Unclassified/Missing Level 1`에 보존합니다.
- 다른 Level 1에 동시에 넣어야 하는 내용은 원본의 `Category:`에 여러 전체 경로를 명시합니다.
- 과거 문서의 Level 1 경계는 `organizer_metadata.json`에서 하나로 확인될 때만 승계합니다.
