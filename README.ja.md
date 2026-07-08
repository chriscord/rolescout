<div align="center">

# ☕ RoleScout

**求人調査と応募準備のための、ローカル優先のAIツール。**

[English](README.md) | [한국어](README.ko.md) | 日本語 | [繁體中文](README.zh-Hant.md)

</div>

---

## 概要

RoleScoutは、キャリアの転機に新しい機会を探す人のために、求人調査と応募準備にかかる時間を減らすツールです。

RoleScoutはホスト型サーバーを運営せず、利用者のキャリアデータをRoleScoutのサーバーに収集しません。履歴書、関心企業、プロジェクト保存領域、生成資料、追跡表データはローカル端末に保存されます。ライブのモデル実行は、利用者が自分でログインしたローカルCLIを通じて行われ、既定はCodexです。

対象地域、関心企業の例、希望職位を入力すると、自分に合うポジションの調査、整理、要約、適合度評価、優先順位付けを支援します。

## インストール

必要なもの:

- Git
- Python 3.10以上
- Codex CLIのインストールに使うNode.js/npm
- ライブのモデル実行に使うChatGPT/Codexアカウント

既定のモデルCLI接続:

```bash
npm install -g @openai/codex
codex login
```

macOSまたはLinux:

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

Pythonのバージョンが古い場合:

```bash
# macOS、Homebrew
brew install python@3.12

# Ubuntu/Debian
sudo apt update
sudo apt install python3.12 python3.12-venv
```

```powershell
# Windows
winget install Python.Python.3.12
```

別のローカルCLI接続:

```bash
rolescout run search \
  --provider cli \
  --llm-name glm \
  --llm-cmd 'your-agent run --model {model} --effort {effort}'
```

`{prompt}`は実行プロンプトに置換。省略時は標準入力で渡される形式。`{root}`、`{project}`、`{model}`、`{effort}`も使用可能。`{model}`と`{effort}`はモデル設定ファイル基準。利用するCLIに合わせたテンプレート調整が必要。

## インストール確認

```bash
rolescout --version
```

正常な出力:

```text
rolescout 0.1.0
```

追加確認:

```bash
rolescout doctor
```

## 使い方

```bash
rolescout web
```

ブラウザーで`http://127.0.0.1:8787`を自動表示。UIはloopback専用で、ホスト公開されません。

1. **プロフィール — 最初に作成。** 氏名、LinkedIn URL、履歴書を追加。対応形式: **PDF、DOCX、Markdown（`.md`）、`.txt`**（`.doc` / `.html`も対応）。必要に応じて補足資料を追加。調査、評価、準備はすべてこのプロフィールを基盤に実行。

> **常時指示（任意、推奨）。** プロフィールに自由記述で入れる指示。RoleScoutが毎回の実行に反映します: 優先事項、制約、強調したい点。承認境界が常に優先され、RoleScoutが利用者に代わって提出・送信することはありません。履歴書だけでは分かりにくい希望、正面から扱うべき空白期間、強調したい経験、転居制約などの記録に有用。

2. **プロジェクト。** プロジェクト作成または選択。1つのプロジェクトは、1つのjob search / prepセッションとして扱う。関心企業の例、職位、職種、対象地域、報酬水準、除外条件など、セッションの希望条件を自由に設定。

3. **検索。** 画面右側のChat sessionパネルで`search`を選択し、**Run**を実行。多数のATSと企業採用ページを確認するため、時間がかかる場合があります。完了後、Jobsタブに調査済みjob listが生成。初回search runでは、現在の履歴書/プロフィールに基づくfit scoreも自動算定。Jobsリスト左側の星で関心ポジションを**focused**として登録。

> **重要。** 準備系コマンドは、focusedポジションが1件以上ある場合のみ動作。これは意図した設計。関心ポジションに対してstrategy、resume、LinkedIn、interview準備を行うため。

4. **準備。** focusedポジションを1件以上選択したうえで`prep`を選択し、**Run**を実行。`prep-strategy`、`prep-resume`、`prep-linkedin`、`prep-interview`をまとめて実行。各コマンドを個別に実行することも可能。生成結果はPrepタブの**Strategy**、**Resume**、**LinkedIn**、**Interview**で確認。

**Strategy。** 関連ポジションをグループ化し、全体のapplication戦略とpriorityを提示。focused setに対する強み、弱み、準備方針を整理。

**Resume。** job group別のtargeted resume draftを生成。

**LinkedIn。** 現在のLinkedIn項目について、改善点をcurrent → to-be形式で提示。

**Interview。** 履歴書とtarget position JDを分析し、想定質問と回答方針、履歴書に基づくstory bank、会社/ポジション関連の最近のニュース、業界・会社glossaryを要約。

5. **応募。** `apply`を選択して**Run**を実行すると、Focused登録済みポジションについてApplicationsタブにtrackerを作成し、各positionのapplication instructionを生成。安全上、実際の自動applyは行いません。応募後は利用者がtracker statusを手動更新でき、その状態はJob listにも自動反映。

### 注意事項

web UIの表示中もterminalの維持が必要。web UIはterminal実行を補助するローカルインターフェースという位置づけ。

### CLI

すべてのCLIコマンドは、指定がない限り有効なプロフィールと有効なプロジェクトを基準に実行。

```bash
rolescout init --person you --focus ai-product --locations "San Francisco"
rolescout run search
rolescout run score
rolescout run prep
rolescout run prep-strategy
rolescout run prep-resume
rolescout run prep-linkedin
rolescout run prep-interview
rolescout run apply
```

| コマンド | 期待される結果 |
|---|---|
| `rolescout init --person you --focus ai-product --locations "San Francisco"` | プロフィール/プロジェクトの組み合わせを作成または有効化。`--companies`、`--role`、`--level`、`--comp-range`、`--negatives`でプロジェクト希望条件を指定可能。 |
| `rolescout run search` | opportunity thesis作成、関連情報源の調査、Jobs list作成、初回search後のscoring 1回実行。 |
| `rolescout run score` | 現在のJobs listを有効プロジェクトの希望条件とscoring modelで再評価。 |
| `rolescout run prep` | focusedポジション向けにstrategy、resume、LinkedIn、interview準備をまとめて実行。 |
| `rolescout run prep-strategy` | グループ化されたapplication strategyとpriority planのみ生成。 |
| `rolescout run prep-resume` | focused job group別のtargeted resume draftを生成。 |
| `rolescout run prep-linkedin` | LinkedIn current → to-be改善案を生成。 |
| `rolescout run prep-interview` | focusedポジション別のinterview packとstory bankを生成。 |
| `rolescout run apply` | focusedポジション別のapplication instructionとtracker rowを生成。自動提出なし。 |

プロジェクト切り替えは`rolescout init --activate <code>`、1回だけのプロジェクト指定は`--project <code>`を使用。

## モデル設定

RoleScoutは、利用者のCodex CLI既定モデルや推論強度をそのまま継承しません。Codex実行時は作業別の明示設定を渡します。

既定設定:

| 作業 | モデル | 推論強度 |
|---|---|---|
| `search` | `gpt-5.5` | `medium` |
| `score` | `gpt-5.5` | `medium` |
| `prep-strategy` | `gpt-5.5` | `xhigh` |
| `prep`, `prep-resume`, `prep-linkedin`, `prep-interview` | `gpt-5.5` | `high` |
| `apply` | `gpt-5.5` | `medium` |

編集可能なファイルは、`rolescout doctor`またはライブCodex実行時に`~/.rolescout/model-profiles.json`へ作成。直接編集、または別のJSONファイル指定が可能:

```bash
ROLESCOUT_MODEL_PROFILES=/path/to/model-profiles.json rolescout run search
```

`--provider cli`使用時は、同じファイルの`external_cli`セクションから`{model}`、`{effort}`の値を渡す形式。

1回だけの上書き:

```bash
ROLESCOUT_CODEX_MODEL=gpt-5.5 ROLESCOUT_CODEX_EFFORT=high rolescout run prep-resume
```

## 主な機能

| 機能 | 説明 |
|---|---|
| 求人調査 | 複数の情報源による調査、標準URL整理、重複除去、求人内容スナップショット、調査ログ作成。 |
| 適合度評価 | 明示した基準による0-100点の優先順位付けと、利用者による修正記録の分離。 |
| 応募準備資料 | 対象グループ別の戦略、1ページDOCX履歴書バリエーション、LinkedInレビュー、ポジショニングメモの生成。 |
| 面接準備 | ポジション別の想定質問、回答方針、履歴書ベースのstory bank、会社/ポジションニュース、glossary、面接準備メモを生成。 |
| 応募手順 | ポジション別のローカル手順作成: リンク確認、必要資料、表示される質問、慎重に扱う項目、追跡表行の追加。 |
| 根拠管理 | 履歴書とプロフィール提案の主要内容をローカルのevidence mapに接続。 |
| ローカル追跡表 | 利用者が管理する状態、次の行動、期限、メモの記録。 |
| ローカルデータ構造 | プロフィール、プロジェクト、生成ファイル、SQLite保存領域、telemetryのローカル保存。 |
| CLI拡張性 | 既定はCodex。利用者が自分で認証した他のローカルエージェントCLIも接続可能。 |

## 言語

GitHubにはREADMEの言語切り替え機能が標準ではありません。このリポジトリでは明示的なリンクを使用:

- [English](README.md)
- [한국어](README.ko.md)
- [繁體中文](README.zh-Hant.md)

---

このプロジェクトは非営利プロジェクトです。求人・転職関連事業者、代行会社、雇用主、求人掲示板とは提携していません。

License: MIT.
