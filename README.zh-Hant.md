<div align="center">

# ☕ RoleScout

**用於職缺研究與申請準備的本機優先 AI 工具。**

[English](README.md) | [한국어](README.ko.md) | [日本語](README.ja.md) | 繁體中文

</div>

---

<p align="center">
  <img src="assets/demo.gif" alt="RoleScout 工作流程示範" width="960">
</p>

## 概覽

RoleScout 是為正在準備職涯轉換、尋找新機會的人設計的工具，目標是減少職缺研究與申請準備所需的時間。

RoleScout 不營運託管後端，也不會把使用者的職涯資料收集到 RoleScout 伺服器。來源檔案、資料庫與產生的文件都保留在本機裝置上。不過，即時合成執行仍會透過你已驗證的 CLI，將最小化的工作流程資料包傳送給所選的模型供應商；預設為 Codex。Codex 合成會在一次性的 staging 目錄中，以唯讀 sandbox 啟動，停用 shell/unified-exec/apps/web search 與對話記錄，並只使用 allowlist 中的程序環境。RoleScout 會在第一次即時執行前顯示供應商通知，並說明每個工作流程使用的資料類別。聯絡人、申請狀態、歷史薪酬、工作許可、LinkedIn URL 與不相關的私人筆記，預設都不會放入模型提示；目標薪酬則屬於允許模型使用的搜尋偏好。

可強制執行的界線與剩餘風險記載於 [`references/privacy-threat-model.md`](references/privacy-threat-model.md)。

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

履歷 DOCX 的視覺渲染 QA 為選配。沒有下列工具時，RoleScout 仍可產生 DOCX 並執行結構檢查，但單頁版面驗證會記錄為 blocked。

```bash
# 渲染檢查器使用的 Python 套件
python -m pip install -e ".[render]"
# 同時包含試算表 extra
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

選配渲染工具組為 `pdf2image`（Python）、LibreOffice/`soffice` 與 Poppler/`pdftoppm`。

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

連接外部 CLI（僅供開發者使用；RoleScout 無法驗證任意 CLI 的 sandbox）：

```bash
rolescout run search \
  --provider cli \
  --llm-name glm \
  --llm-cmd 'your-agent run --model {model} --effort {effort}'
```

提示會透過標準輸入傳入。`{root}` 與 `{project}` 會解析為一次性的 staging 目錄；`{model}` 與 `{effort}` 來自模型設定檔。由於程序清單可能暴露資料包內容，預設會拒絕 `{prompt}` argv placeholder。

只有在檢查外部供應商的檔案系統與工具隔離後，才設定 `ROLESCOUT_ENABLE_UNSANDBOXED_CLI=1`。程序會從一次性的 staging 目錄啟動，並使用 allowlist 環境。

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

1. **個人資料 — 請先建立。** 新增姓名、LinkedIn URL 與履歷。支援格式：**PDF、DOCX、Markdown（`.md`）、`.txt`、HTML**。需要時加入補充資料。儲存個人資料或上傳履歷時，會在背景啟動 `profile-intake`：先以確定性抽取建立範圍受限的 source packet，再將 typed model output 具體化為 `candidate-profile.md` 與 `evidence-map.md`。LinkedIn URL 只是本機指標；目前 LinkedIn 證據必須來自支援的 import/capture 路徑。

   > [!NOTE]
   > **常駐指示預設只保留在本機。** 可與模型分享的搜尋偏好，請填入專案的結構化 target field。自由文字的個人資料指示可能包含私人資訊，因此預設不會注入即時提示。

2. **專案。** 建立或選擇專案。一個專案可視為一個 job search / prep session。可自由設定此 session 的偏好：關注公司範例、目標職級、目標職務、地點、薪資範圍、排除條件與其他限制。

3. **規劃 → 擷取 → 評估 → 定稿。** `opportunity-plan` 是選配且範圍受限的模型階段，會寫入已驗證的 company universe。`search` 會從該 universe（或 seed-only 模式中宣告的 seed）進行確定性擷取，包括 URL/JD 正規化與儲存。`score` 會將壓縮的職缺 batch 用於語意評估，再由確定性的 finalize 階段套用權重並寫入分數。按星號即可把感興趣的職位登記為 **focused**。

   > [!IMPORTANT]
   > 準備類命令至少需要 1 個 focused 職位。這是刻意的設計：針對你決定要爭取的職位，產生 Strategy、Resume、LinkedIn 與 Interview 資料。

4. **準備。** 選擇至少 1 個 focused 職位後，選擇 `prep` 並按 **Run**，即可一次執行四個準備工作流程。也可以逐一執行；結果會顯示在同名的 Prep 分頁中。

   - **Strategy**（`prep-strategy`）— 將相關職位分組、建立整體申請策略與優先順序，並說明 focused set 的優勢、弱點與準備路徑。
   - **Resume**（`prep-resume`）— 依 job group 產生 targeted resume draft。
   - **LinkedIn**（`prep-linkedin`）— 檢視目前 LinkedIn，並以 current → to-be 形式呈現建議修改。
   - **Interview**（`prep-interview`）— 分析履歷與 target-position JD，準備可能問題、回答計畫、履歷式 story bank、近期公司/職位新聞，以及產業與公司 glossary。

5. **申請。** 選擇 `apply` 並按 **Run**，會針對 Focused 職位在 Applications 分頁建立 tracker，並為每個 position 產生 application instruction。基於安全考量，不會實際自動 apply。使用者自行送出申請後，可以手動更新 tracker status；該狀態也會自動反映到 Job list。

> [!NOTE]
> web UI 執行期間請保持 terminal 開啟。web UI 是 terminal 程序的本機 companion。

### CLI

所有 CLI 命令在未另外指定時，都會使用目前啟用的個人資料與專案。

```bash
rolescout init --person you --focus ai-product --locations "San Francisco"
rolescout run profile-intake --person you
rolescout run opportunity-plan
rolescout run search
rolescout run score
rolescout run prep
rolescout run prep-strategy
rolescout run prep-resume
rolescout run prep-linkedin
rolescout run prep-interview
rolescout run story-bank
rolescout run apply
rolescout export --public
rolescout privacy audit
```

| 命令 | 預期結果 |
|---|---|
| `rolescout init --person you --focus ai-product --locations "San Francisco"` | 建立或啟用個人資料/專案組合。可用 `--companies`、`--role`、`--level`、`--comp-range`、`--negatives` 設定專案偏好。 |
| `rolescout run profile-intake --person you` | 從履歷/資料與已接受的 LinkedIn current-source content 建立或更新 `profiles/<person>/candidate-profile.md` 和 `profiles/<person>/evidence-map.md`。 |
| `rolescout run opportunity-plan` | 以允許模型使用的 target preference，選擇性建立範圍受限的 typed company universe。 |
| `rolescout run search` | 執行確定性的 provider-first discovery，擷取 direct posting URL/JD snapshot，寫入 raw Jobs store 並建立 UI-visible Jobs view；預設不執行 scoring。 |
| `rolescout run score` | 以 runner 建立的壓縮 batch 評估目前 UI 中的每個 Jobs row，再確定性重算加權分數並寫回 `fit_score`/`priority`。 |
| `rolescout run prep` | 針對 focused 職位一次執行 strategy、resume、LinkedIn 與 interview 準備。 |
| `rolescout run prep-strategy` | 只產生分組後的 application strategy 與 priority plan。 |
| `rolescout run prep-resume` | 依 focused job group 產生 targeted resume draft。 |
| `rolescout run prep-linkedin` | 產生 LinkedIn current → to-be 建議。 |
| `rolescout run prep-interview` | 為 focused 職位產生 interview pack 與 story bank。 |
| `rolescout run story-bank` | 獨立重建以履歷證據為基礎的共用 story bank。 |
| `rolescout run apply` | 為 focused 職位建立 application instruction 與 tracker row；不會自動提交。 |
| `rolescout export --public` / `--private` | 明確建立依敏感度分離的 export 與 revision manifest。 |
| `rolescout privacy audit` | 不顯示私人內容，回報本機 runtime/telemetry 的留存狀況。 |
| `rolescout clean --runtime` | 顯示留存資料的 dry-run manifest；加入 `--apply` 才會刪除。 |
| `rolescout delete-person --person <slug>` | 顯示刪除個人資料/專案的 dry-run manifest；加入 `--apply` 才會刪除。 |

使用 `rolescout init --activate <code>` 切換專案，或用 `--project <code>` 指定單次執行的專案。

## 模型設定

RoleScout 不會直接沿用使用者的 Codex CLI 預設模型或推理強度。使用 Codex 執行時，會依照各流程傳入明確設定。

預設設定：

| 流程 | 模型 | 推理強度 |
|---|---|---|
| `opportunity-plan` | `gpt-5.5` | `medium` |
| `score` | `gpt-5.5` | `medium` |
| `profile-intake` | `gpt-5.5` | `high` |
| `prep-strategy` | `gpt-5.5` | `xhigh` |
| `prep`, `prep-resume`, `prep-linkedin`, `prep-interview`, `story-bank` | `gpt-5.5` | `high` |
| `apply` | `gpt-5.5` | `medium` |

`search` 預設為確定性工作流程，因此除非明確啟用選配 auto-scoring 或 legacy search path，否則不會呼叫模型。

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
| 敏感度分離儲存 | 公開職缺資訊位於 `data/public-opportunities.db`；私人 pipeline 狀態位於 `private/pipeline.db`。export 需明確執行，且維持分離。 |
| 本機資料結構 | 個人資料、專案、產生檔案、SQLite 儲存區與 telemetry 均保存在本機。可用 privacy audit 與 dry-run 清理/刪除命令檢查留存狀態。 |
| CLI 彈性 | 預設使用 Codex，亦提供僅供開發者使用的 generic adapter，可連接其他由使用者驗證的本機 agent CLI。 |

## 語言

GitHub 沒有內建 README 語言切換功能。本儲存庫使用明確連結：

- [English](README.md)
- [한국어](README.ko.md)
- [日本語](README.ja.md)

---

本專案為非營利專案。未與招募公司、求職仲介、雇主或職缺看板合作。

License: MIT.
