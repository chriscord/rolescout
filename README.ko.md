<div align="center">

# ☕ RoleScout

**구직 조사와 지원 준비를 위한 로컬 우선 AI 도구.**

[English](README.md) | 한국어 | [日本語](README.ja.md) | [繁體中文](README.zh-Hant.md)

</div>

---

## 개요

RoleScout는 커리어 변화를 위해 새로운 기회를 찾는 분들의 구직 조사와 지원 준비 시간을 줄이기 위한 도구입니다.

RoleScout는 별도의 호스팅 서버를 운영하지 않으며 사용자의 커리어 데이터를 RoleScout 서버로 수집하지 않습니다. 이력서, 관심 회사, 프로젝트 저장소, 생성된 자료, 추적표 데이터는 로컬 디바이스에 보관됩니다. 라이브 모델 실행은 사용자가 직접 로그인한 로컬 CLI를 통해 수행되며, 기본값은 Codex입니다.

관심 지역, 관심 회사 예시, 희망 직급을 입력하면 사용자에게 맞는 포지션을 조사하고, 정리와 요약, 적합도 평가, 우선순위 판단을 도와줍니다.

## 설치

필요 항목:

- Git
- Python 3.10 이상
- Codex CLI 설치를 위한 Node.js/npm
- 라이브 모델 실행을 위한 ChatGPT/Codex 계정

기본 모델 CLI 연결:

```bash
npm install -g @openai/codex
codex login
```

macOS 또는 Linux:

```bash
git clone https://github.com/chriscord/rolescout
cd rolescout
./tools/setup.sh
source .venv/bin/activate
```

Windows PowerShell:

```powershell
git clone https://github.com/chriscord/rolescout
cd rolescout
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip setuptools
python -m pip install -e ".[xlsx]"
```

Python 버전이 낮은 경우:

```bash
# macOS, Homebrew 사용
brew install python@3.12

# Ubuntu/Debian
sudo apt update
sudo apt install python3.12 python3.12-venv
```

```powershell
# Windows
winget install Python.Python.3.12
```

다른 로컬 CLI 연결:

```bash
rolescout run search \
  --provider cli \
  --llm-name glm \
  --llm-cmd 'your-agent run --model {model} --effort {effort}'
```

`{prompt}`는 실행 프롬프트로 대체됨. 생략 시 표준 입력으로 전달됨. `{root}`, `{project}`, `{model}`, `{effort}`도 사용 가능. `{model}`과 `{effort}`는 모델 설정 파일 기준이며, 사용하는 CLI에 맞게 템플릿 조정 필요.

## 설치 완료 후 확인

```bash
rolescout --version
```

정상 출력:

```text
rolescout 0.1.0
```

추가 점검:

```bash
rolescout doctor
```

## 사용 방법

```bash
rolescout web
```

브라우저에서 `http://127.0.0.1:8787` 자동 연결. UI는 loopback 전용이며 호스팅되지 않음.

1. **프로필 — 가장 먼저 생성.** 이름, LinkedIn URL, 이력서 추가. 지원 형식: **PDF, DOCX, Markdown(`.md`), `.txt`**(`.doc` / `.html`도 지원). 필요한 경우 참고 자료 추가. 프로필 저장 또는 이력서 업로드 후 `profile-intake`가 백그라운드에서 시작되어 `candidate-profile.md`와 `evidence-map.md`를 생성/갱신함. LinkedIn URL은 source pointer일 뿐이며, 실제 LinkedIn 근거는 지원되는 import/capture 경로로 확보된 current content에서만 사용됨.

> **상시 지시사항(선택, 권장).** 프로필에 자유롭게 작성하는 지침으로, RoleScout가 모든 실행에 반영함: 우선순위, 제약 사항, 강조할 점. 승인 경계가 항상 우선하며 RoleScout는 사용자를 대신해 제출하거나 전송하지 않음. 이력서만으로 드러나지 않는 선호, 직접 다뤄야 할 공백기, 강조할 경험, 이주 제한 등을 적어두면 유용함.

2. **프로젝트.** 프로젝트 생성 또는 선택. 프로젝트 1개는 하나의 job search / prep 세션으로 이해하면 됨. 원하는 회사 예시, 직급, 직무, 관심 지역, 연봉 수준, 제외 조건 등 세션 선호사항 자유 설정.

3. **검색.** 화면 오른쪽 Chat session 패널에서 `search` 선택 후 **Run** 실행. `profile-intake`가 아직 실행 중이어도 바로 시작 가능하며, profile/evidence map이 준비되기 전의 fit/grouping은 provisional로 표시됨. 여러 ATS와 회사 채용 페이지를 훑기 때문에 시간이 걸릴 수 있음. 완료 후 Jobs 탭 하위에 조사된 job list 생성. 최초 search run은 가능한 경우 현재 이력서/프로필 기준 fit score도 함께 산정함. Jobs 리스트 왼쪽의 별표를 눌러 관심 포지션을 **focused**로 등록.

> **중요.** Prep 계열 명령은 focused 포지션이 1개 이상 있어야 동작함. 이는 관심 있는 포지션을 기준으로 strategy, resume, LinkedIn, interview 준비물을 만드는 의도된 설계임.

4. **준비.** focused 포지션을 1개 이상 선택한 뒤 `prep` 선택 후 **Run** 실행. `prep-strategy`, `prep-resume`, `prep-linkedin`, `prep-interview`를 한 번에 실행. 각 명령을 하나씩 선택해 실행하는 것도 가능. 생성 결과는 Prep 탭의 **Strategy**, **Resume**, **LinkedIn**, **Interview**에서 확인.

**Strategy.** 관련 포지션을 그룹으로 묶고 전체 application 전략과 priority 제시. focused set 기준 강점, 약점, 준비 방향 정리.

**Resume.** job group별 targeted resume draft 생성.

**LinkedIn.** 현재 LinkedIn 항목에서 개선할 부분을 current → to-be 형태로 제시.

**Interview.** 이력서와 target position JD를 분석해 예상 질문과 답변 방향, 이력서 기반 story bank, 회사/포지션 관련 최근 뉴스, 업계·회사 glossary 요약.

5. **지원.** `apply` 선택 후 **Run** 실행 시 Focused로 등록한 포지션에 대해 Applications 탭에 tracker 생성 및 position별 application instruction 생성. 안전상 실제 자동 apply는 하지 않음. 지원 후 사용자가 tracker status를 직접 업데이트할 수 있고, 이 상태는 Job list에도 자동 반영됨.

### 주의사항

web UI가 떠 있을 때도 terminal 유지 필요. web UI는 terminal 실행을 보조하는 로컬 인터페이스에 가까움.

### CLI

모든 CLI 명령은 별도 지정이 없으면 활성 프로필과 활성 프로젝트 기준으로 실행됨.

```bash
rolescout init --person you --focus ai-product --locations "San Francisco"
rolescout run profile-intake --person you
rolescout run search
rolescout run score
rolescout run prep
rolescout run prep-strategy
rolescout run prep-resume
rolescout run prep-linkedin
rolescout run prep-interview
rolescout run apply
```

| 명령 | 예상 결과 |
|---|---|
| `rolescout init --person you --focus ai-product --locations "San Francisco"` | 프로필/프로젝트 쌍 생성 또는 활성화. `--companies`, `--role`, `--level`, `--comp-range`, `--negatives`로 프로젝트 선호사항 지정 가능. |
| `rolescout run profile-intake --person you` | resume/material 및 허용된 LinkedIn current-source content로 `profiles/<person>/candidate-profile.md`, `profiles/<person>/evidence-map.md` 생성 또는 갱신. |
| `rolescout run search` | opportunity thesis 생성, 관련 출처 조사, Jobs list 작성, 최초 search 후 scoring 1회 실행. |
| `rolescout run score` | 현재 Jobs list를 활성 프로젝트 선호사항과 scoring model 기준으로 재평가. |
| `rolescout run prep` | focused 포지션 기준 strategy, resume, LinkedIn, interview 준비 동시 실행. |
| `rolescout run prep-strategy` | 그룹화된 application strategy와 priority plan만 생성. |
| `rolescout run prep-resume` | focused job group별 targeted resume draft 생성. |
| `rolescout run prep-linkedin` | LinkedIn current → to-be 개선안 생성. |
| `rolescout run prep-interview` | focused 포지션별 interview pack과 story bank 생성. |
| `rolescout run apply` | focused 포지션별 application instruction과 tracker row 생성. 자동 제출 없음. |

프로젝트 전환은 `rolescout init --activate <code>`, 1회성 프로젝트 지정은 `--project <code>` 사용.

## 모델 설정

RoleScout는 사용자의 Codex CLI 기본 모델 또는 추론 강도 설정을 그대로 사용하지 않음. Codex 실행 시 작업별 명시 설정 전달.

기본 설정:

| 작업 | 모델 | 추론 강도 |
|---|---|---|
| `search` | `gpt-5.5` | `medium` |
| `score` | `gpt-5.5` | `medium` |
| `profile-intake` | `gpt-5.5` | `high` |
| `prep-strategy` | `gpt-5.5` | `xhigh` |
| `prep`, `prep-resume`, `prep-linkedin`, `prep-interview` | `gpt-5.5` | `high` |
| `apply` | `gpt-5.5` | `medium` |

수정 가능한 파일은 `rolescout doctor` 또는 라이브 Codex 실행 시 `~/.rolescout/model-profiles.json`에 생성됨. 해당 파일을 직접 수정하거나 다른 JSON 파일 지정 가능:

```bash
ROLESCOUT_MODEL_PROFILES=/path/to/model-profiles.json rolescout run search
```

`--provider cli` 사용 시 같은 파일의 `external_cli` 섹션으로 `{model}`, `{effort}` 값 전달.

1회 실행 변경:

```bash
ROLESCOUT_CODEX_MODEL=gpt-5.5 ROLESCOUT_CODEX_EFFORT=high rolescout run prep-resume
```

## 주요 기능

| 기능 | 설명 |
|---|---|
| 공고 조사 | 여러 출처 기반 조사, 표준 URL 정리, 중복 제거, 공고 원문 스냅샷, 조사 로그 생성. |
| 적합도 평가 | 명시적 기준에 따른 0-100점 우선순위 평가와 사용자 수정 기록 분리. |
| 지원 준비 자료 | 대상 그룹별 전략, 1쪽 DOCX 이력서 변형본, LinkedIn 검토, 포지셔닝 노트 생성. |
| 면접 준비 | 포지션별 예상 질문, 답변 방향, 이력서 기반 story bank, 회사/포지션 뉴스, glossary, 면접 준비 노트 생성. |
| 지원 절차 안내 | 포지션별 로컬 안내 생성: 링크 확인, 필요 자료, 보이는 질문, 민감 항목 처리, 추적표 행 추가. |
| 근거 관리 | 이력서와 프로필 제안의 주요 내용은 로컬 evidence map 근거에 연결. |
| 로컬 추적표 | 사용자가 직접 관리하는 상태, 다음 행동, 기한, 메모 기록. |
| 로컬 데이터 구조 | 프로필, 프로젝트, 생성 파일, SQLite 저장소, telemetry의 로컬 보관. |
| CLI 확장성 | 기본 Codex, 그 외 사용자가 직접 인증한 로컬 에이전트 CLI 연결 가능. |

## 언어

GitHub는 README 언어 전환 기능을 기본 제공하지 않음. 이 저장소는 명시적 링크 사용:

- [English](README.md)
- [日本語](README.ja.md)
- [繁體中文](README.zh-Hant.md)

---

이 프로젝트는 비영리 프로젝트입니다. 구인/구직 업체, 대행사, 고용주, 채용 게시판과 제휴되어 있지 않습니다.

License: MIT.
