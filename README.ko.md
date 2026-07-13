<div align="center">

# ☕ RoleNavi

**구직 조사와 지원 준비를 위한 로컬 우선 AI 도구.**

[English](README.md) | 한국어 | [日本語](README.ja.md) | [繁體中文](README.zh-Hant.md)

</div>

---

## 개요

RoleNavi는 커리어 변화를 위해 새로운 기회를 찾는 분들의 구직 조사와 지원 준비 시간을 줄이기 위한 도구입니다.

RoleNavi는 호스팅 백엔드를 운영하거나 사용자의 커리어 데이터를 RoleNavi 서버로 수집하지 않습니다. 원본 파일, 저장소, 생성 자료는 로컬 디바이스에 남습니다. 다만 라이브 합성 실행 시에는 사용자가 인증한 CLI를 통해 최소화된 워크플로 패킷을 선택한 모델 제공자에게 전송하며, 기본값은 Codex입니다. Codex 합성은 일회용 staging 디렉터리에서 읽기 전용 sandbox로 시작되고 shell/unified-exec/apps/web search, 대화 기록이 비활성화되며 허용 목록 기반 프로세스 환경만 사용합니다. RoleNavi는 첫 라이브 실행 전에 제공자 안내를 표시하고 각 워크플로가 사용하는 데이터 분류를 알려줍니다. 연락처, 지원 상태, 보상 이력, 취업 허가, LinkedIn URL, 무관한 비공개 메모는 기본적으로 모델 프롬프트에서 제외되며, 목표 보상은 모델 사용이 허용된 검색 선호사항입니다.

강제 가능한 경계와 잔여 위험은 [`references/privacy-threat-model.md`](references/privacy-threat-model.md)에 설명되어 있습니다.

관심 지역, 관심 회사 예시, 희망 직급을 입력하면 사용자에게 맞는 포지션을 조사하고, 정리와 요약, 적합도 평가, 우선순위 판단을 도와줍니다.

## 설치

RoleNavi는 활성 ChatGPT/Codex subscription 사용을 전제로 설계되었습니다. 기본 라이브 워크플로는 로컬에서 인증한 Codex CLI를 사용하므로, 설치 후 `npm install -g @openai/codex`와 `codex login`으로 subscription을 연결하세요.

### macOS

```bash
curl -fsSL https://raw.githubusercontent.com/chriscord/rolenavi/main/tools/install-macos.sh | bash
```

### Linux

```bash
curl -fsSL https://raw.githubusercontent.com/chriscord/rolenavi/main/tools/install-linux.sh | bash
```

### Windows PowerShell

```powershell
irm https://raw.githubusercontent.com/chriscord/rolenavi/main/tools/install-windows.ps1 | iex
```

각 installer는 RoleNavi를 macOS/Windows에서는 `~/RoleNavi`, Linux에서는 `~/rolenavi`에 clone하고, `.venv` 생성·스프레드시트 지원 기본 설치·`rolenavi` 명령 검증까지 수행합니다. 다른 위치를 쓰려면 installer 실행 전에 `ROLENAVI_INSTALL_DIR`를 설정하세요.

### LinkedIn 프로필 자동 분석을 위한 선택 브라우저 도구

LinkedIn 프로필을 자동으로 분석하려면 [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp) 또는 [Playwright](https://playwright.dev/docs/intro#installing-playwright) 설치를 권장합니다. 필수는 아니지만, 사용할 수 있는 환경에서는 브라우저 기반으로 최신 프로필 내용을 캡처하는 데 도움이 됩니다.

이력서 DOCX 시각 렌더 검사는 선택 사항입니다. RoleNavi는 아래 도구 없이도 DOCX를 생성하고 구조 검사를 수행하지만, 1페이지 레이아웃 검증은 차단됨으로 기록합니다.

```bash
# 렌더 검사에 사용하는 Python 패키지
python -m pip install -e ".[render]"
# 스프레드시트 옵션도 함께 설치
python -m pip install -e ".[xlsx,render]"

# macOS
brew install libreoffice poppler

# Ubuntu/Debian
sudo apt update
sudo apt install libreoffice poppler-utils
```

```powershell
# Windows
python -m pip install -e ".[xlsx,render]"
winget install TheDocumentFoundation.LibreOffice
winget install oschwartz10612.Poppler
```

선택 렌더 스택은 `pdf2image`(Python), LibreOffice/`soffice`, Poppler/`pdftoppm`입니다.

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

외부 CLI 연결(개발자 전용, RoleNavi는 임의 CLI의 sandbox를 검증할 수 없음):

```bash
rolenavi run search \
  --provider cli \
  --llm-name glm \
  --llm-cmd 'your-agent run --model {model} --effort {effort}'
```

프롬프트는 표준 입력으로 전달됩니다. `{root}`와 `{project}`는 일회용 staging 디렉터리로 해석되고, `{model}`과 `{effort}`는 모델 프로필 파일에서 가져옵니다. 프로세스 목록에 패킷 내용이 노출될 수 있으므로 `{prompt}` argv placeholder는 기본적으로 거부됩니다.

외부 제공자의 파일시스템·도구 격리를 검토한 후에만 `ROLENAVI_ENABLE_UNSANDBOXED_CLI=1`을 설정하세요. 프로세스는 허용 목록 기반 환경을 사용하는 일회용 staging 디렉터리에서 시작합니다.

> [!WARNING]
> RoleNavi는 Codex subscription 연결로 검증되었습니다. 다른 AI agent 연동은 실험적인 external-CLI adapter를 사용하며, 아직 테스트되거나 지원되지 않았습니다.

## 설치 완료 후 확인

```bash
rolenavi --version
```

정상 출력:

```text
rolenavi 0.1.0
```

추가 점검:

```bash
rolenavi doctor
```

## 사용 방법

```bash
rolenavi web
```

브라우저에서 `http://127.0.0.1:8787` 자동 연결. UI는 loopback 전용이며 호스팅되지 않음.

기본 라이브 AI 워크플로를 사용하기 전 `codex login`으로 ChatGPT/Codex subscription을 연결하세요. RoleNavi는 로컬 Codex CLI를 호출하며 API key가 필요하지 않습니다.

이 워크플로는 사람이 통제권을 유지하도록 설계되었습니다: **결정적 구직 검색 → 에이전트 기반 평가 → focused 포지션 선택 → 준비 → 수동 지원**.

1. **프로필 — 가장 먼저 생성.** 이름, LinkedIn URL, 이력서를 추가합니다. 지원 형식: **PDF, DOCX, Markdown(`.md`), `.txt`, HTML**. 필요하면 참고 자료를 추가합니다. 프로필 저장 또는 이력서 업로드 시 `profile-intake`가 백그라운드에서 시작됩니다. 결정적 추출로 범위가 제한된 source packet을 만든 뒤 typed model output을 `candidate-profile.md`와 `evidence-map.md`로 구체화합니다. LinkedIn URL은 로컬 포인터일 뿐이며, 현재 LinkedIn 근거는 지원되는 import/capture 경로에서 가져와야 합니다.

   > [!NOTE]
   > **상시 지시사항은 기본적으로 로컬에만 남습니다.** 모델과 공유 가능한 검색 선호는 프로젝트의 구조화된 target field에 입력하세요. 자유 형식 프로필 지시사항에는 비공개 사실이 포함될 수 있으므로 기본적으로 라이브 프롬프트에 주입하지 않습니다.

2. **프로젝트.** 프로젝트 생성 또는 선택. 프로젝트 1개는 하나의 job search / prep 세션으로 이해하면 됨. 원하는 회사 예시, 직급, 직무, 관심 지역, 연봉 수준, 제외 조건 등 세션 선호사항 자유 설정.

3. **계획 → 수집 → 평가 → 확정.** `opportunity-plan`은 검증된 company universe를 작성하는 선택적·제한형 모델 단계입니다. `search`는 해당 universe(또는 seed-only 모드의 명시적 seed)에서 공고를 결정적으로 수집하고 URL/JD 정규화 및 저장을 수행합니다. `score`는 압축된 공고 batch를 의미 평가에 사용한 뒤 결정적 finalize 단계에서 가중치를 적용하고 점수를 기록합니다. 이로써 결정적 구직 검색과 에이전트 기반 평가를 분리합니다. 에이전트는 수집된 근거를 평가하지만 공고를 수집하거나 실제 지원 여부를 결정하지 않습니다. 별표를 눌러 관심 포지션을 **focused**로 등록합니다.

   > [!IMPORTANT]
   > Prep 계열 명령은 focused 포지션이 1개 이상 있어야 동작합니다. 선택해서 추진할 포지션을 기준으로 Strategy, Resume, LinkedIn, Interview 자료를 만드는 의도된 설계입니다.

4. **준비.** focused 포지션을 1개 이상 선택한 뒤 `prep`을 선택하고 **Run**을 누르면 네 준비 워크플로를 함께 실행합니다. 각각 따로 실행할 수도 있으며 결과는 이름이 같은 Prep 탭에서 확인합니다.

   - **Strategy** (`prep-strategy`) — 관련 포지션을 그룹화하고 전체 지원 전략과 우선순위를 세우며, focused set의 강점·약점·준비 경로를 설명합니다.
   - **Resume** (`prep-resume`) — job group별 targeted resume draft를 생성합니다.
   - **LinkedIn** (`prep-linkedin`) — 현재 LinkedIn을 검토하고 current → to-be 형식의 수정안을 제시합니다.
   - **Interview** (`prep-interview`) — 이력서와 target-position JD를 분석해 예상 질문, 답변 계획, 이력서 기반 story bank, 최근 회사/포지션 뉴스, 업계·회사 glossary를 준비합니다.

5. **지원.** `apply` 선택 후 **Run** 실행 시 Focused로 등록한 포지션에 대해 Applications 탭에 tracker 생성 및 position별 application instruction 생성. 안전상 실제 자동 apply는 하지 않음. 지원 후 사용자가 tracker status를 직접 업데이트할 수 있고, 이 상태는 Job list에도 자동 반영됨.

> [!NOTE]
> web UI가 실행되는 동안 terminal을 열어 두세요. web UI는 terminal 프로세스의 로컬 companion입니다.

### CLI

모든 CLI 명령은 별도 지정이 없으면 활성 프로필과 활성 프로젝트 기준으로 실행됨.

```bash
rolenavi init --person you --focus ai-product --locations "San Francisco"
rolenavi run profile-intake --person you
rolenavi run opportunity-plan
rolenavi run search
rolenavi run score
rolenavi run prep
rolenavi run prep-strategy
rolenavi run prep-resume
rolenavi run prep-linkedin
rolenavi run prep-interview
rolenavi run story-bank
rolenavi run apply
rolenavi export --public
rolenavi privacy audit
```

| 명령 | 예상 결과 |
|---|---|
| `rolenavi init --person you --focus ai-product --locations "San Francisco"` | 프로필/프로젝트 쌍 생성 또는 활성화. `--companies`, `--role`, `--level`, `--comp-range`, `--negatives`로 프로젝트 선호사항 지정 가능. |
| `rolenavi run profile-intake --person you` | resume/material 및 허용된 LinkedIn current-source content로 `profiles/<person>/candidate-profile.md`, `profiles/<person>/evidence-map.md` 생성 또는 갱신. |
| `rolenavi run opportunity-plan` | 모델 사용이 허용된 target preference로 범위가 제한된 typed company universe를 선택적으로 생성. |
| `rolenavi run search` | 결정적 provider-first discovery를 실행하고 direct posting URL/JD snapshot을 수집해 raw Jobs store와 UI-visible Jobs view를 작성. 기본적으로 scoring은 실행하지 않음. |
| `rolenavi run score` | UI에 보이는 모든 Jobs row를 runner가 만든 압축 batch로 평가한 뒤 가중 점수와 `fit_score`/`priority`를 결정적으로 다시 계산해 기록. |
| `rolenavi run prep` | focused 포지션 기준 strategy, resume, LinkedIn, interview 준비 동시 실행. |
| `rolenavi run prep-strategy` | 그룹화된 application strategy와 priority plan만 생성. |
| `rolenavi run prep-resume` | focused job group별 targeted resume draft 생성. |
| `rolenavi run prep-linkedin` | LinkedIn current → to-be 개선안 생성. |
| `rolenavi run prep-interview` | focused 포지션별 interview pack과 story bank 생성. |
| `rolenavi run story-bank` | 이력서 근거 기반의 공용 story bank를 독립적으로 다시 생성. |
| `rolenavi run apply` | focused 포지션별 application instruction과 tracker row 생성. 자동 제출 없음. |
| `rolenavi export --public` / `--private` | 민감도에 따라 분리된 export와 revision manifest를 명시적으로 생성. |
| `rolenavi privacy audit` | 비공개 내용을 출력하지 않고 로컬 runtime/telemetry 흔적을 보고. |
| `rolenavi clean --runtime` | 보존 데이터 dry-run manifest 출력. 삭제하려면 `--apply` 추가. |
| `rolenavi delete-person --person <slug>` | 프로필/프로젝트 삭제 dry-run manifest 출력. 삭제하려면 `--apply` 추가. |

프로젝트 전환은 `rolenavi init --activate <code>`, 1회성 프로젝트 지정은 `--project <code>` 사용.

## 모델 설정

RoleNavi는 사용자의 Codex CLI 기본 모델 또는 추론 강도 설정을 그대로 사용하지 않음. Codex 실행 시 작업별 명시 설정 전달.

기본 설정:

| 작업 | 모델 | 추론 강도 |
|---|---|---|
| `opportunity-plan` | `gpt-5.5` | `medium` |
| `score` | `gpt-5.5` | `medium` |
| `profile-intake` | `gpt-5.5` | `high` |
| `prep-strategy` | `gpt-5.5` | `xhigh` |
| `prep`, `prep-resume`, `prep-linkedin`, `prep-interview`, `story-bank` | `gpt-5.5` | `high` |
| `apply` | `gpt-5.5` | `medium` |

`search`는 기본적으로 결정적 워크플로이므로 선택적 auto-scoring 또는 legacy search path를 명시적으로 활성화하지 않는 한 모델을 호출하지 않습니다.

수정 가능한 파일은 `rolenavi doctor` 또는 라이브 Codex 실행 시 `~/.rolenavi/model-profiles.json`에 생성됨. 해당 파일을 직접 수정하거나 다른 JSON 파일 지정 가능:

```bash
ROLENAVI_MODEL_PROFILES=/path/to/model-profiles.json rolenavi run search
```

`--provider cli` 사용 시 같은 파일의 `external_cli` 섹션으로 `{model}`, `{effort}` 값 전달.

1회 실행 변경:

```bash
ROLENAVI_CODEX_MODEL=gpt-5.5 ROLENAVI_CODEX_EFFORT=high rolenavi run prep-resume
```

## 주요 기능

| 기능 | 설명 |
|---|---|
| 결정적 구직 검색 | 여러 출처 기반 조사, 표준 URL 정리, 중복 제거, 공고 원문 스냅샷, 조사 로그 생성. |
| 에이전트 기반 평가와 결정적 확정 | 압축된 수집 공고 batch를 의미 평가하고, 명시적 가중치·게이트·분리된 사용자 수정 기록으로 최종 0–100점 우선순위를 확정. |
| 지원 준비 자료 | 대상 그룹별 전략, 1쪽 DOCX 이력서 변형본, LinkedIn 검토, 포지셔닝 노트 생성. |
| 면접 준비 | 포지션별 예상 질문, 답변 방향, 이력서 기반 story bank, 회사/포지션 뉴스, glossary, 면접 준비 노트 생성. |
| 지원 절차 안내 | 포지션별 로컬 안내 생성: 링크 확인, 필요 자료, 보이는 질문, 민감 항목 처리, 추적표 행 추가. |
| 근거 기반 준비 자료 | 이력서, LinkedIn, 면접 자료의 주장은 로컬 evidence map 근거에 연결. |
| 사람이 통제하는 지원 흐름 | 검색 → focused 포지션 선택 → 준비 → 수동 지원. RoleNavi는 사용자를 대신해 지원서를 제출하지 않음. |
| 로컬 추적표 | 사용자가 직접 관리하는 상태, 다음 행동, 기한, 메모 기록. |
| 민감도 분리 SQLite 저장소 | 공개 구인정보는 `data/public-opportunities.db`, 비공개 지원·pipeline 상태는 `private/pipeline.db`에 저장. export도 명시적으로 실행하며 서로 분리됨. |
| 로컬 데이터 구조 | 프로필, 프로젝트, 생성 파일, SQLite 저장소, telemetry를 로컬에 보관. privacy audit와 dry-run 정리/삭제 명령으로 보존 상태 확인 가능. |
| 방어적 로컬 처리 | 원자적 쓰기, 경로 검증, 크기 제한, deny-by-default privacy registry로 로컬 산출물과 모델 패킷을 보호. |
| CLI 확장성 | 기본 Codex, 그리고 개발자 전용 generic adapter를 통해 사용자가 인증한 다른 로컬 agent CLI 연결 가능. |

## 언어

GitHub는 README 언어 전환 기능을 기본 제공하지 않음. 이 저장소는 명시적 링크 사용:

- [English](README.md)
- [日本語](README.ja.md)
- [繁體中文](README.zh-Hant.md)

---

이 프로젝트는 비영리 프로젝트입니다. 구인/구직 업체, 대행사, 고용주, 채용 게시판과 제휴되어 있지 않습니다.

License: MIT.
