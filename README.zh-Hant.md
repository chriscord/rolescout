<div align="center">

# ☕ RoleScout

**用於職缺研究與申請準備的本機優先 AI 工具。**

[English](README.md) | [한국어](README.ko.md) | [日本語](README.ja.md) | 繁體中文

</div>

---

## 概覽

RoleScout 是為正在準備職涯轉換、尋找新機會的人設計的工具，目標是減少職缺研究與申請準備所需的時間。

RoleScout 不營運託管後端，也不把使用者的職涯資料收集到 RoleScout 伺服器。履歷、關注公司、專案資料、產生的文件與追蹤表資料都保存在本機裝置上。即時模型執行會透過使用者自行登入的本機 CLI 進行，預設為 Codex。

輸入目標地區、關注公司範例與目標職級後，RoleScout 會協助研究相關職缺、整理與摘要內容、評估適合度，並排出優先準備的順序。

## 安裝

必要項目：

- Git
- Python 3.10 或更新版本
- 用於安裝 Codex CLI 的 Node.js/npm
- 用於即時模型執行的 ChatGPT/Codex 帳號

連接預設模型 CLI：

```bash
npm install -g @openai/codex
codex login
```

macOS 或 Linux：

```bash
git clone https://github.com/chriscord/rolescout
cd rolescout
./tools/setup.sh
source .venv/bin/activate
```

Windows PowerShell：

```powershell
git clone https://github.com/chriscord/rolescout
cd rolescout
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip setuptools
python -m pip install -e ".[xlsx]"
```

如果 Python 版本太舊：

```bash
# macOS，使用 Homebrew
brew install python@3.12

# Ubuntu/Debian
sudo apt update
sudo apt install python3.12 python3.12-venv
```

```powershell
# Windows
winget install Python.Python.3.12
```

連接其他本機 CLI：

```bash
rolescout run search \
  --provider cli \
  --llm-name glm \
  --llm-cmd 'your-agent run --model {model} --effort {effort}'
```

`{prompt}` 會替換為執行提示；若省略，提示會透過標準輸入傳入。也可以使用 `{root}`、`{project}`、`{model}` 與 `{effort}`。`{model}` 與 `{effort}` 來自模型設定檔，請依照使用的 CLI 調整模板。

## 安裝完成後確認

```bash
rolescout --version
```

預期輸出：

```text
rolescout 0.1.0
```

接著執行：

```bash
rolescout doctor
```

## 使用方式

```bash
rolescout web
```

瀏覽器會自動開啟 `http://127.0.0.1:8787`。介面僅限 loopback，不會被託管公開。

1. **個人資料 — 請先建立。** 新增姓名、LinkedIn URL 與履歷。支援格式：**PDF、DOCX、Markdown（`.md`）、`.txt`**（也支援 `.doc` / `.html`）。需要時加入補充資料。研究、評分與準備都以這份個人資料為基礎。

> **常駐指示（選填，建議填寫）。** 在個人資料中以自由文字撰寫的指引，RoleScout 會在每次執行時帶入：優先事項、限制條件與想強調的重點。核准界線一律優先；RoleScout 絕不會代替你提交或送出任何內容。可用來記錄履歷中不明顯的偏好、需要直接處理的空窗期、想強調的經驗或搬遷限制。

2. **專案。** 建立或選擇專案。一個專案可視為一個 job search / prep session。可自由設定此 session 的偏好：關注公司範例、目標職級、目標職務、地點、薪資範圍、排除條件與其他限制。

3. **搜尋。** 在畫面右側 Chat session 面板選擇 `search`，再按 **Run**。RoleScout 會檢查多個 ATS 與公司職涯頁面，因此可能需要一些時間。完成後，Jobs 分頁下會產生研究過的 job list。第一次 search run 也會依目前履歷/個人資料自動計算 fit score。可按 Jobs 清單左側的星號，將感興趣的職位登記為 **focused**。

> **重要。** 準備類命令需要至少 1 個 focused 職位才會執行。這是刻意設計：這些命令的目的，是針對你已選定的關注職位產生 strategy、resume、LinkedIn 與 interview 準備。

4. **準備。** 先選擇至少 1 個 focused 職位，再選擇 `prep` 並按 **Run**。會一次執行 `prep-strategy`、`prep-resume`、`prep-linkedin` 與 `prep-interview`。也可以分別選擇單一命令執行。產生的結果可在 Prep 分頁的 **Strategy**、**Resume**、**LinkedIn**、**Interview** 查看。

**Strategy。** 將相關職位分組，建立整體 application 策略並提示 priority。整理 focused set 對應的優勢、弱點與準備方向。

**Resume。** 依 job group 產生 targeted resume draft。

**LinkedIn。** 檢視目前 LinkedIn 項目，並以 current → to-be 形式呈現建議修改。

**Interview。** 分析履歷與 target position JD，準備可能問題與回答方向、基於履歷的 story bank、公司/職位近期新聞，以及產業與公司 glossary。

5. **申請。** 選擇 `apply` 並按 **Run**，會針對 Focused 職位在 Applications 分頁建立 tracker，並為每個 position 產生 application instruction。基於安全考量，不會實際自動 apply。使用者自行送出申請後，可以手動更新 tracker status；該狀態也會自動反映到 Job list。

### 注意事項

web UI 開啟期間需要保持 terminal 執行。web UI 是輔助 terminal 程序的本機介面。

### CLI

所有 CLI 命令在未另外指定時，都會使用目前啟用的個人資料與專案。

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

| 命令 | 預期結果 |
|---|---|
| `rolescout init --person you --focus ai-product --locations "San Francisco"` | 建立或啟用個人資料/專案組合。可用 `--companies`、`--role`、`--level`、`--comp-range`、`--negatives` 設定專案偏好。 |
| `rolescout run search` | 建立 opportunity thesis、研究相關來源、寫入 Jobs list，並在首次 search 後執行一次 scoring。 |
| `rolescout run score` | 依目前專案偏好與 scoring model 重新評估 Jobs list。 |
| `rolescout run prep` | 針對 focused 職位一次執行 strategy、resume、LinkedIn 與 interview 準備。 |
| `rolescout run prep-strategy` | 只產生分組後的 application strategy 與 priority plan。 |
| `rolescout run prep-resume` | 依 focused job group 產生 targeted resume draft。 |
| `rolescout run prep-linkedin` | 產生 LinkedIn current → to-be 建議。 |
| `rolescout run prep-interview` | 為 focused 職位產生 interview pack 與 story bank。 |
| `rolescout run apply` | 為 focused 職位建立 application instruction 與 tracker row；不會自動提交。 |

使用 `rolescout init --activate <code>` 切換專案，或用 `--project <code>` 指定單次執行的專案。

## 模型設定

RoleScout 不會直接沿用使用者的 Codex CLI 預設模型或推理強度。使用 Codex 執行時，會依照各流程傳入明確設定。

預設設定：

| 流程 | 模型 | 推理強度 |
|---|---|---|
| `search` | `gpt-5.5` | `medium` |
| `score` | `gpt-5.5` | `medium` |
| `prep-strategy` | `gpt-5.5` | `xhigh` |
| `prep`, `prep-resume`, `prep-linkedin`, `prep-interview` | `gpt-5.5` | `high` |
| `apply` | `gpt-5.5` | `medium` |

可編輯檔案會在 `rolescout doctor` 或即時 Codex 執行檢查模型設定時建立於 `~/.rolescout/model-profiles.json`。可以直接編輯該檔案，或指定另一個 JSON 檔：

```bash
ROLESCOUT_MODEL_PROFILES=/path/to/model-profiles.json rolescout run search
```

使用 `--provider cli` 時，會從同一檔案的 `external_cli` 區段傳入 `{model}` 與 `{effort}`。

單次執行覆寫：

```bash
ROLESCOUT_CODEX_MODEL=gpt-5.5 ROLESCOUT_CODEX_EFFORT=high rolescout run prep-resume
```

## 主要功能

| 功能 | 說明 |
|---|---|
| 職缺研究 | 多來源研究、標準 URL 整理、去重、職缺描述快照與研究紀錄。 |
| 適合度評估 | 依明確標準進行 0-100 分優先排序，並分開記錄使用者修正。 |
| 申請準備資料 | 目標群組策略、單頁 DOCX 履歷版本、LinkedIn 檢討與定位筆記。 |
| 面試準備 | 依職位產生可能問題、回答方向、履歷式 story bank、公司/職位新聞、glossary 與面試準備筆記。 |
| 申請步驟說明 | 各職位的本機步驟說明：連結確認、所需資料、可見問題、敏感欄位處理與追蹤表列。 |
| 證據管理 | 履歷與個人資料建議的主要內容需連結到本機 evidence map。 |
| 本機追蹤表 | 由使用者管理的狀態、下一步、期限與備註。 |
| 本機資料結構 | 個人資料、專案、產生檔案、SQLite 儲存區與 telemetry 均保存在本機。 |
| CLI 彈性 | 預設使用 Codex，也可連接其他由使用者自行驗證的本機代理 CLI。 |

## 語言

GitHub 沒有內建 README 語言切換功能。本儲存庫使用明確連結：

- [English](README.md)
- [한국어](README.ko.md)
- [日本語](README.ja.md)

---

本專案為非營利專案。未與招募公司、求職仲介、雇主或職缺看板合作。

License: MIT.
