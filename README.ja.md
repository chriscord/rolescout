<div align="center">

# ☕ RoleNavi

**求人調査と応募準備のための、ローカル優先のAIツール。**

<img src="assets/demo.gif" alt="RoleNavi デモ" width="920">

[English](README.md) | [한국어](README.ko.md) | 日本語 | [繁體中文](README.zh-Hant.md)

</div>

---

## 概要

RoleNaviは、キャリアの転機に新しい機会を探す人のために、求人調査と応募準備にかかる時間を減らすツールです。

RoleNaviはホスト型バックエンドを運営せず、利用者のキャリアデータをRoleNaviのサーバーに収集しません。ソースファイル、保存領域、生成資料は端末内に残ります。ただし、ライブ合成の実行時には、認証済みCLIを通じて最小化されたワークフローパケットが選択したモデルプロバイダーへ送信されます。既定はCodexです。Codex合成は使い捨てのstagingディレクトリで読み取り専用sandboxとして開始され、shell/unified-exec/apps/web search、会話履歴は無効、プロセス環境はallowlistに限定されます。RoleNaviは最初のライブ実行前にプロバイダー通知を表示し、各ワークフローが使用するデータ分類を報告します。連絡先、応募状況、報酬履歴、就労許可、LinkedIn URL、無関係な非公開メモは既定でモデルプロンプトから除外され、目標報酬はモデル利用可能な検索希望条件です。

強制可能な境界と残存リスクは[`references/privacy-threat-model.md`](references/privacy-threat-model.md)に記載しています。

対象地域、関心企業の例、希望職位を入力すると、自分に合うポジションの調査、整理、要約、適合度評価、優先順位付けを支援します。

## インストール

RoleNaviは有効なChatGPT/Codex subscriptionの利用を前提に設計されています。既定のライブワークフローはローカルで認証したCodex CLIを使用するため、インストール後に`npm install -g @openai/codex`と`codex login`でsubscriptionを接続してください。

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

各installerはRoleNaviをmacOS/Windowsでは`~/RoleNavi`、Linuxでは`~/rolenavi`へcloneし、`.venv`作成・スプレッドシート対応の基本インストール・`rolenavi`コマンド検証まで実行します。別の場所を使う場合はinstaller実行前に`ROLENAVI_INSTALL_DIR`を設定してください。

### LinkedInプロフィール自動分析用の任意ブラウザーツール

LinkedInプロフィールを自動分析する場合は、[Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp) または [Playwright](https://playwright.dev/docs/intro#installing-playwright) の導入を推奨します。必須ではありませんが、利用可能な環境ではブラウザーベースで現在のプロフィール内容を取得できます。

履歴書DOCXの視覚レンダーQAは任意です。以下のツールがなくてもDOCX生成と構造チェックは実行できますが、1ページのレイアウト検証はblockedとして記録されます。

```bash
# レンダーチェッカー用Pythonパッケージ
python -m pip install -e ".[render]"
# スプレッドシートextraも含める場合
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

任意レンダースタックは`pdf2image`（Python）、LibreOffice/`soffice`、Poppler/`pdftoppm`です。

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

外部CLI接続（開発者向け。RoleNaviは任意のCLIのsandboxを検証できません）:

```bash
rolenavi run search \
  --provider cli \
  --llm-name glm \
  --llm-cmd 'your-agent run --model {model} --effort {effort}'
```

プロンプトは標準入力で渡されます。`{root}`と`{project}`は使い捨てのstagingディレクトリに解決され、`{model}`と`{effort}`はモデルプロファイルから取得されます。プロセス一覧にパケット内容が露出し得るため、`{prompt}`のargv placeholderは既定で拒否されます。

外部プロバイダーのファイルシステム・ツール分離を確認した後に限り、`ROLENAVI_ENABLE_UNSANDBOXED_CLI=1`を設定してください。プロセスはallowlist環境の使い捨てstagingディレクトリから開始されます。

> [!WARNING]
> RoleNaviはCodex subscription接続で検証されています。その他のAI agent連携は実験的なexternal-CLI adapterを使用し、まだテストもサポートもされていません。

## インストール確認

```bash
rolenavi --version
```

正常な出力:

```text
rolenavi 0.1.0
```

追加確認:

```bash
rolenavi doctor
```

## 使い方

```bash
rolenavi web
```

ブラウザーで`http://127.0.0.1:8787`を自動表示。UIはloopback専用で、ホスト公開されません。

既定のライブAIワークフローを使う前に、`codex login`でChatGPT/Codex subscriptionを接続してください。RoleNaviはローカルCodex CLIを呼び出し、API keyは必要ありません。

このワークフローは人が主導権を保つよう設計されています: **決定的な求人検索 → エージェント評価 → focusedポジションの選択 → 準備 → 手動応募**。

1. **プロフィール — 最初に作成。** 氏名、LinkedIn URL、履歴書を追加します。対応形式: **PDF、DOCX、Markdown（`.md`）、`.txt`、HTML**。必要に応じて補足資料を追加します。プロフィール保存または履歴書アップロードにより`profile-intake`がバックグラウンドで開始されます。決定的抽出で範囲を限定したsource packetを作成し、typed model outputを`candidate-profile.md`と`evidence-map.md`として具体化します。LinkedIn URLはローカルポインターに過ぎず、現在のLinkedIn根拠は対応するimport/capture経路から取得する必要があります。

   > [!NOTE]
   > **常時指示は既定でローカルにのみ残ります。** モデル共有可能な検索希望は、プロジェクトの構造化target fieldへ入力してください。自由記述のプロフィール指示には非公開情報が含まれ得るため、既定ではライブプロンプトに注入されません。

2. **プロジェクト。** プロジェクト作成または選択。1つのプロジェクトは、1つのjob search / prepセッションとして扱う。関心企業の例、職位、職種、対象地域、報酬水準、除外条件など、セッションの希望条件を自由に設定。

3. **計画 → 収集 → 評価 → 確定。** `opportunity-plan`は検証済みcompany universeを作成する任意の範囲限定モデル段階です。`search`はそのuniverse（またはseed-onlyモードで宣言したseed）から決定的に収集し、URL/JDの正規化と保存を行います。`score`は圧縮した求人batchを意味評価に送り、決定的なfinalize段階で重みを適用してスコアを書き込みます。これにより、決定的な求人検索とエージェント評価を分離します。エージェントは取得済みの根拠を評価しますが、求人を収集したり実際に応募するかを決定したりしません。星を付けて関心ポジションを**focused**として登録します。

   > [!IMPORTANT]
   > 準備系コマンドはfocusedポジションが1件以上ある場合のみ動作します。追求すると決めたポジション向けにStrategy、Resume、LinkedIn、Interview資料を作るための意図した設計です。

4. **準備。** focusedポジションを1件以上選択し、`prep`を選んで**Run**を押すと、4つの準備ワークフローをまとめて実行します。個別実行も可能で、結果は同名のPrepタブに表示されます。

   - **Strategy**（`prep-strategy`）— 関連ポジションをグループ化し、全体の応募戦略と優先順位、focused setの強み・弱み・準備経路を示します。
   - **Resume**（`prep-resume`）— job group別のtargeted resume draftを生成します。
   - **LinkedIn**（`prep-linkedin`）— 現在のLinkedInをレビューし、current → to-be形式の変更案を示します。
   - **Interview**（`prep-interview`）— 履歴書とtarget-position JDを分析し、想定質問、回答計画、履歴書ベースのstory bank、最近の会社/ポジションニュース、業界・会社glossaryを準備します。

5. **応募。** `apply`を選択して**Run**を実行すると、Focused登録済みポジションについてApplicationsタブにtrackerを作成し、各positionのapplication instructionを生成。安全上、実際の自動applyは行いません。応募後は利用者がtracker statusを手動更新でき、その状態はJob listにも自動反映。

> [!NOTE]
> web UIの実行中はterminalを開いたままにしてください。web UIはterminalプロセスのローカルcompanionです。

### CLI

すべてのCLIコマンドは、指定がない限り有効なプロフィールと有効なプロジェクトを基準に実行。

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

| コマンド | 期待される結果 |
|---|---|
| `rolenavi init --person you --focus ai-product --locations "San Francisco"` | プロフィール/プロジェクトの組み合わせを作成または有効化。`--companies`、`--role`、`--level`、`--comp-range`、`--negatives`でプロジェクト希望条件を指定可能。 |
| `rolenavi run profile-intake --person you` | 履歴書/資料と受け入れ済みLinkedIn current-source contentから`profiles/<person>/candidate-profile.md`と`profiles/<person>/evidence-map.md`を作成または更新。 |
| `rolenavi run opportunity-plan` | モデル利用可能なtarget preferenceから範囲限定のtyped company universeを任意で作成。 |
| `rolenavi run search` | 決定的なprovider-first discoveryを実行し、direct posting URL/JD snapshotを収集してraw Jobs storeとUI-visible Jobs viewを構築。既定ではscoreを実行しない。 |
| `rolenavi run score` | UIに表示中のJobs rowをrunner作成の圧縮batchで評価し、重み付きスコアと`fit_score`/`priority`を決定的に再計算して書き込む。 |
| `rolenavi run prep` | focusedポジション向けにstrategy、resume、LinkedIn、interview準備をまとめて実行。 |
| `rolenavi run prep-strategy` | グループ化されたapplication strategyとpriority planのみ生成。 |
| `rolenavi run prep-resume` | focused job group別のtargeted resume draftを生成。 |
| `rolenavi run prep-linkedin` | LinkedIn current → to-be改善案を生成。 |
| `rolenavi run prep-interview` | focusedポジション別のinterview packとstory bankを生成。 |
| `rolenavi run story-bank` | 履歴書根拠に基づく共有story bankを単独で再生成。 |
| `rolenavi run apply` | focusedポジション別のapplication instructionとtracker rowを生成。自動提出なし。 |
| `rolenavi export --public` / `--private` | 機密度で分離したexportとrevision manifestを明示的に作成。 |
| `rolenavi privacy audit` | 非公開内容を表示せず、ローカルruntime/telemetryの残存状況を報告。 |
| `rolenavi clean --runtime` | 保持データのdry-run manifestを表示。削除には`--apply`を追加。 |
| `rolenavi delete-person --person <slug>` | プロフィール/プロジェクト削除のdry-run manifestを表示。削除には`--apply`を追加。 |

プロジェクト切り替えは`rolenavi init --activate <code>`、1回だけのプロジェクト指定は`--project <code>`を使用。

## モデル設定

RoleNaviは、利用者のCodex CLI既定モデルや推論強度をそのまま継承しません。Codex実行時は作業別の明示設定を渡します。

既定設定:

| 作業 | モデル | 推論強度 |
|---|---|---|
| `opportunity-plan` | `gpt-5.5` | `medium` |
| `score` | `gpt-5.5` | `medium` |
| `profile-intake` | `gpt-5.5` | `high` |
| `prep-strategy` | `gpt-5.5` | `xhigh` |
| `prep`, `prep-resume`, `prep-linkedin`, `prep-interview`, `story-bank` | `gpt-5.5` | `high` |
| `apply` | `gpt-5.5` | `medium` |

`search`は既定で決定的ワークフローのため、任意のauto-scoringまたはlegacy search pathを明示的に有効化しない限りモデルを呼び出しません。

編集可能なファイルは、`rolenavi doctor`またはライブCodex実行時に`~/.rolenavi/model-profiles.json`へ作成。直接編集、または別のJSONファイル指定が可能:

```bash
ROLENAVI_MODEL_PROFILES=/path/to/model-profiles.json rolenavi run search
```

`--provider cli`使用時は、同じファイルの`external_cli`セクションから`{model}`、`{effort}`の値を渡す形式。

1回だけの上書き:

```bash
ROLENAVI_CODEX_MODEL=gpt-5.5 ROLENAVI_CODEX_EFFORT=high rolenavi run prep-resume
```

## 主な機能

| 機能 | 説明 |
|---|---|
| 決定的な求人検索 | 複数の情報源による調査、標準URL整理、重複除去、求人内容スナップショット、調査ログ作成。 |
| エージェント評価と決定的確定 | 圧縮した取得済み求人batchを意味評価し、明示的な重み・ゲート・分離された利用者修正記録により最終0–100点の優先順位を確定。 |
| 応募準備資料 | 対象グループ別の戦略、1ページDOCX履歴書バリエーション、LinkedInレビュー、ポジショニングメモの生成。 |
| 面接準備 | ポジション別の想定質問、回答方針、履歴書ベースのstory bank、会社/ポジションニュース、glossary、面接準備メモを生成。 |
| 応募手順 | ポジション別のローカル手順作成: リンク確認、必要資料、表示される質問、慎重に扱う項目、追跡表行の追加。 |
| 根拠に基づく準備資料 | 履歴書、LinkedIn、面接資料の主張をローカルのevidence mapに接続。 |
| 人が管理する応募フロー | 検索 → focusedポジションの選択 → 準備 → 手動応募。RoleNaviが利用者に代わって応募を送信することはありません。 |
| ローカル追跡表 | 利用者が管理する状態、次の行動、期限、メモの記録。 |
| 機密度分離SQLiteストア | 公開求人情報は`data/public-opportunities.db`、非公開の応募・pipeline状態は`private/pipeline.db`に保存。exportも明示実行し、分離を維持。 |
| ローカルデータ構造 | プロフィール、プロジェクト、生成ファイル、SQLiteストア、telemetryを端末に保存。privacy auditとdry-runの整理/削除コマンドで保持状態を確認可能。 |
| 防御的なローカル処理 | 原子的書き込み、パス検証、サイズ制限、deny-by-default privacy registryでローカル成果物とモデルパケットを保護。 |
| CLI拡張性 | 既定はCodex。開発者向けgeneric adapterで、利用者が認証した他のローカルagent CLIにも接続可能。 |

## 言語

GitHubにはREADMEの言語切り替え機能が標準ではありません。このリポジトリでは明示的なリンクを使用:

- [English](README.md)
- [한국어](README.ko.md)
- [繁體中文](README.zh-Hant.md)

---

このプロジェクトは非営利プロジェクトです。求人・転職関連事業者、代行会社、雇用主、求人掲示板とは提携していません。

License: MIT.
