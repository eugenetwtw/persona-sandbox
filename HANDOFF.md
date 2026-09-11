# HANDOFF — 人格沙盒（Persona Sandbox）

> 交接文件。寫於 2026-09-11。
> 讀者是「接下來要接手這個專案的人或 agent」。
> 假設你沒看過先前的對話，這份文件要讓你在 15 分鐘內能接手、跑起來、知道哪裡有坑。

---

## 0. 三件要先知道的事

**1. 有兩把 API 金鑰在 `.env` 裡，其中一把曾經出現在對話紀錄中，必須輪替。**

| 變數 | 用途 | 狀態 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 蒸餾與對話的 LLM | ⚠️ **曾以明文出現在對話中，建議撤銷重發** |
| `FIRECRAWL_API_KEY` | 抓取公開網頁素材 | ⚠️ 同上 |

`.env` 權限是 `600`，已被 `.gitignore` 排除，程式碼裡沒有金鑰。但**金鑰本身應該視為已洩漏**。
輪替方式：到後台重新產生，然後 `python3 -m persona_sandbox setup` 或直接改 `.env`。

**2. 伺服器現在正在跑**：http://127.0.0.1:8800（背景 job `bash-10`）。
它是本機服務，不是產品。沒有 auth、沒有佇列、沒有錯誤監控。

**3. 這是一個「驗證概念」的 MVP，不是可以上線的東西。**
它的唯一目的是回答一個問題：**「把一個人蒸餾成 AI，然後讓他跟別人碰撞，好不好玩、準不準？」**
這個問題目前只被非正式地回答過（使用者自己看過幾份輸出）。**沒有量化評估。**

---

## 1. 這個專案是什麼

把一個人的數位痕跡（問卷自述、部落格、社群貼文、email）蒸餾成一張**人格卡**，
然後把人格卡派進一個**壓力情境**，跟歷史人物（或其他人的分身）對話。

三條路線：

```
              ┌─ 8 題問卷 ────────────┐
              │                       │
輸入 ─────────┼─ 公開網址（Firecrawl）─┼─→ 行為分析(JSON) ─→ 人格卡(Markdown)
              │                       │        │
              └─ 上傳檔案(.eml/.json)─┘        └─→ 證據追溯表
                       │
                       └─→ 本人確認 / 修正（typed correction）

人格卡 ─→ 壓力情境 + 名人角色卡 ─→ 多輪對話（內在獨白 + 對外發言分離）
```

**「蒸餾」不是模型訓練。** 它是「用 LLM 把素材壓縮成一份結構化文件，執行時當 system prompt 塞回去」。
沒有 fine-tune、沒有 embedding、沒有向量庫。產物是一份可讀、可手改、可版控的 Markdown。

---

## 2. 快速開始

```bash
cd /Users/pe/Downloads/persona-distill

# 看金鑰狀態
python3 -m persona_sandbox providers

# 驗證金鑰能用（會送一個最小請求）
python3 -m persona_sandbox doctor

# 網頁版
python3 -m persona_sandbox.server              # → http://127.0.0.1:8800
python3 -m persona_sandbox.server --port 9000  # 換 port
python3 -m persona_sandbox.server --dry-run    # 無金鑰模式，輸出結構骨架

# 命令列版
python3 -m persona_sandbox demo                # 用內建示範素材跑完整條管線
python3 -m persona_sandbox ask                 # 互動問卷

# 資料庫
python3 -m persona_sandbox db stats
python3 -m persona_sandbox db users
python3 -m persona_sandbox db cards --user 張渝江
python3 -m persona_sandbox db export 張渝江     # 完整匯出（含對話史）
```

**零依賴**：只用 Python 標準庫（3.9+，開發環境 3.13）。沒有 requirements.txt 要裝。

---

## 3. 檔案地圖

```
persona_sandbox/
├── __main__.py         594 行  CLI 入口（providers/setup/doctor/db/ask/demo/distill/correct/play）
├── server.py           650 行  零依賴 HTTP server（http.server），16 個 API 端點
├── store.py            382 行  SQLite 存取層
├── db.py               302 行  資料庫工具 + 從 out/*.json 遷移
├── sources.py          598 行  Firecrawl 抓取 + 檔案/email 解析 + 素材排序
├── persona/
│   ├── questions.py    217 行  8 題問卷 + 追問規則 + 素材體檢
│   ├── prompts.py      406 行  所有 prompt（分析/撰寫/修正，問卷路線與素材路線各一套）
│   ├── provider.py     541 行  LLM adapter（OpenAI 相容 + Anthropic）+ dry-run
│   └── engine.py       252 行  蒸餾管線 + PersonaCard
├── world/
│   ├── witnesses.py    439 行  名人角色卡（蘇格拉底、老子、賈伯斯、瑪麗蓮·夢露）
│   ├── scenario.py     325 行  壓力情境 + 輸出格式規則 + 原型議程
│   └── sandbox.py      378 行  Encounter 狀態機 + parse_turn + _should_end
├── web/index.html      652 行  單頁 UI（vanilla JS，無框架）
└── out/                       產出（answers / cards / logs）
```

---

## 4. 資料庫

檔案：`persona_sandbox.db`（616 KB，WAL 模式）。6 張表：

| 表 | 筆數 | 存什麼 |
|---|---|---|
| `users` | 7 | 使用者（slug 唯一） |
| `sessions` | 22 | 一次完整互動（`questionnaire` 或 `encounter`） |
| `events` | 142 | **append-only**。每題問答、每輪發言、每次修正、每次蒸餾 |
| `turns` | 91 | 沙盒對話的結構化副本（方便查詢） |
| `distillations` | 12 | 人格卡 + **參數**（provider、model、temperature、prompt 來源、來源網址） |
| `distill_evidence` | 344 | **證據追溯**：這條結論來自哪一題／哪個來源、什麼等級 |

**設計原則**：`events` 只寫不改（可回溯、可重建）；每筆蒸餾都記下來源 session 與當時參數。

遷移工具：`python3 -m persona_sandbox.db migrate` 會把 `out/*.json` 匯入 DB（可重複執行，以 hash 去重）。
接上 SQLite 之前的舊資料已經全部遷移完成。

---

## 5. 核心設計決策（含理由）

### 5.1 為什麼只用兩次 LLM 呼叫蒸餾

| 步驟 | 呼叫 | temperature |
|---|---|---|
| 分析 | 素材 → 行為分析 JSON（每條附證據等級） | 0.3 |
| 撰寫 | 分析 → 人格卡 Markdown | 0.7 |
| 修正 | 口語回饋 → typed correction | 0.2 |
| 對話 | 每回合一次 | **1.0** |

不是 `distilly` 的 6-agent 長鏈。理由：UGC 規模的成本與延遲撐不住。

### 5.2 與 `titanwings/distilly` 的關鍵差異：**反轉了知識來源**

`distilly` 的 `prompts/persona_analyzer.md` 第 11 行寫：

> **优先级规则：手动标签 > 文件分析。有冲突时以手动标签为准**

意思是素材只是裝飾——蒸出來的角色由使用者勾的標籤決定，所以像職場漫畫人物。

**本專案反過來**：`素材 > 模式 > 印象`。每一條結論都必須附證據等級與來源代號（題號 `q1` 或來源 `S1`），
素材不足就寫「原材料不足」，不准編。這是蒸真人與蒸原型的根本差別。

### 5.3 問卷設計：問故事，不問形容詞

8 題裡有兩題是刻意設計的：
- **第 6 題（你怎麼拒絕人）**——說「不」的方式是人格的最高解析度切片
- **第 5 題（嘴上說不在意但其實在意）**——在抓矛盾，矛盾是最有價值的素材

### 5.4 壓力情境的三要素

「跟名人聊天」新鮮感三分鐘就沒了。真正讓人格顯形的是壓力，所以場景必須內建：

1. **稀缺** — 沙漠場景的水只夠兩個人
2. **不對稱資訊** — 每個人知道的東西不一樣且不能全說
3. **隱藏議程** — 每個角色都有不能公開的目標

`scenario.py` 有 `ARCHETYPE_AGENDAS`，沒被特別寫過議程的角色會依原型（對抗型／合作型／誘惑型）
拿到通用議程。**所以任何新角色卡都能直接進場，不用為每個人重寫劇本。**

### 5.5 內在獨白 vs 對外發言：**這是產品的核心價值**

兩者一致的對話是廢話。**兩者出現落差的地方，才是人格。**

但格式走過一次大改：

| | 第一版 | 現在 |
|---|---|---|
| 獨白 | 150-500 字 | **一行，28-53 字** |
| 對話 | 附屬品 | **主體** |
| 對話/獨白比 | 約 1.5 倍 | **20 倍** |

原因有兩個：**(a)** 舊格式讀不下去——敘述壓過對話；**(b)** 16×16 的規模下每輪 32 段長獨白會讓 token 爆炸。

**教訓：簡短 ≠ 可讀。** 現在的規則是「獨白壓成一行，但對話不要怕長」——
允許動作開場、允許一句話停在半途、允許節奏變化。

### 5.6 名人只收已故者

已故無肖像權／名譽權主張主體，且留下大量可引用的一手文本。
目前 4 位角色全部 `deceased=True`。**新增角色時請維持這個原則。**

---

## 6. 外部 API 的實測結果（重要，別重複踩）

### Firecrawl 對某些站台是「政策拒收」，不是抓不到

實測結果（2026-09-11）：

| 站台 | 結果 |
|---|---|
| WordPress / Medium / 一般部落格 | ✅ 正常 |
| X / Twitter | ✅ 正常（1519 字，含貼文全文） |
| Wikipedia | ✅ |
| **Facebook** | ❌ `403: we do not support this site` |
| **Instagram / Threads / LinkedIn** | ❌ 同上 |
| **YouTube** | ⚠️ 回 200，但內容是 **Google 401 錯誤頁** |

**三個關鍵發現：**

1. **Facebook 的拒絕是 Firecrawl 的政策，不是你的頁面不公開。**
   我實測直接抓 `m.facebook.com` 回 **HTTP 200**，標題正確。但 Facebook 貼文是 JS 渲染，
   靜態 HTML 只有 metadata（讚數、簡介），**拿不到貼文內文**。

2. **`/search` 端點不受「不支援此站」限制。** 所以 `sources.py` 有 `_search_fallback()`：
   被拒收的站台會自動改用搜尋代理，撈被索引的公開內容。品質較低（標記為 `search_fallback`），
   但 Facebook 頁面仍能撈到約 1,100 字。

3. **YouTube 的 401 錯誤頁一度污染過蒸餾結果。** `sources.py` 現在有 `_looks_like_error_page()`
   專門攔截這個。新增站台時請注意類似的「200 但內容是錯誤頁」陷阱。

**給使用者的正確做法**：Facebook 要拿完整資料，用官方匯出
（設定 → 你的 Facebook 資訊 → 下載你的資訊 → JSON），再從 UI 的「② 上傳檔案」丟進去。
那條路完全合法且資料最完整。

---

## 7. 已修過的 bug（別改回去）

| bug | 症狀 | 修法 |
|---|---|---|
| `_SPEECH` regex greedy | 對外發言把後面第二段獨白吃掉 | 加 lookahead，`_dedupe()` 合併重複區塊 |
| 場景結束誤判 | 對話出現「搶」字就被判衝突升級，第 6 輪硬切 | 只掃描**括號內的動作描述**，不掃台詞 |
| YouTube 錯誤頁 | Google 401 頁被當成內容餵進蒸餾 | `_looks_like_error_page()` 攔截 |
| 來源編號不一致 | 報告寫「S1 是錯誤頁」，但 UI 的 S1 是部落格 | `rank_docs()` 統一排序，兩處共用 |
| `/api/upload` multipart | `_read_json()` 先讀走 body，導致 500 | multipart 時 `_read_json()` 直接回 `{}` |
| cast() 的 WITNESSES | `NameError`，進場景就爆 | `scenario.py` 補 import |
| Sandbox 溫度太低 | 對話太平，沒有火花 | 0.9 → **1.0** |
| 瑪麗蓮角色卡太保守 | 被寫成哲學系講師，完全沒有誘惑 | 重寫：明確納入身體語言與測試機制 |

---

## 8. 現有內容

**場景（2 個）**

| id | 名稱 | 輪數 | 機制 |
|---|---|---|---|
| `survival` | 末日荒野避難所 | 15 | 稀缺（水只夠兩人）+ 不對稱資訊（地圖糊掉）|
| `motel` | 公路盡頭的汽車旅館 | 14 | 封閉空間 + 誘惑 + 一張床 |

**角色（4 位，全部已故）**

| id | 角色 | 原型 | 來源 |
|---|---|---|---|
| `socrates` | 蘇格拉底 | 對抗型 | 柏拉圖早期對話錄 |
| `laozi` | 老子 | 合作型 | 道德經（王弼本） |
| `jobs` | 賈伯斯 | 對抗型 | 史丹佛演講、發表會、Isaacson 傳記 |
| `marilyn` | 瑪麗蓮·夢露 | 誘惑型 | 《My Story》、1962 Life 專訪、私人筆記 |

**已產出的人格卡**：`out/cards/`（張渝江、我自己、示範玩家等）
**已產出的劇本**：`out/logs/`（含兩份手工排版的完整劇本）

---

## 9. 已知限制（誠實清單）

**架構面**
- ❌ **沒有 auth**。任何人連到 port 就能用
- ❌ **沒有非同步排程**。一輪一個同步 HTTP 請求，10 人同時玩就卡死
- ❌ **encounter 狀態在記憶體**（`ENCOUNTERS` dict）。伺服器重啟，進行中的對話就沒了
  （**已完成的回合有寫進 DB**，只有進行中的狀態會掉）
- ❌ **素材池在記憶體**（`SOURCE_POOL` dict）。同上
- ❌ **沒有成本計算**。不知道一場對話花多少錢
- ❌ **沒有速率限制**。一個使用者可以無限燒 token

**品質面**
- ❌ **沒有量化評估**。沒有保真度評分、「這像不像我」的量表、A/B 測試
- ❌ **沒有 PII 清洗**。目前靠「完全本機執行」作為隱私論述
- ⚠️ **`_should_end()` 是關鍵詞比對**，不是 LLM 裁判（見 `sandbox.py`）
- ⚠️ **UI 只用 headless Chrome 驗過首頁**。互動流程是用 API 直接打通的，
  **沒有真的用滑鼠點過每一條路徑**

**資料面**
- ⚠️ **證據引用是來源代號（S1/S2），不是逐句引用**。無法回答「這條結論出自哪一句」
- ⚠️ **Facebook 素材品質受限**（見第 6 節）

---

## 10. 建議的下一步（按優先序）

### P0 — 驗證，不是開發

**這件事沒做完，其他都是白做：拿現在的人格卡給 10 個人看，問「這像不像一個人」。**
如果反應是「喔還不錯」，就別往下做了。如果是「這真的好像我」或「我要傳給朋友」，
那才值得投入下面的工程。

### P1 — 成本計算（最便宜、資訊量最大）

加 token 計數與費用統計，跑完一場就顯示「這場花了 $X」。
**這是目前完全沒有的數字，而它決定後面所有架構決策。**

### P2 — 兩真人的分身碰撞（階段 3）

做成「兩人房」，同步就好，不需要排程器。
這是驗證核心樂趣的最短路徑，也是 16×16 的縮小版預演。
（現在房間裡只有「使用者 + 名人」，還沒有「使用者 + 使用者的分身」。）

### P3 — 非同步排程（階段 4）

**在 P1、P2 沒驗證之前不要做這個。** 設計草案：

```
POST /encounters → encounters 表（pending）
        ↓
排程器（每 30 秒）→ 找 next_turn_at <= now
        ↓
worker 領一個 → 讀狀態 → 1 次 LLM → 寫 turns → 算下次時間 → 排下一次
```

核心原則：**每個回合都是獨立的、可重啟的工作，不是長跑程序。**
好處：可恢復、可控制成本、可觀測。

### P4 — 16×16

**這不是把現在的東西放大而已。** 16 個分身對 16 個分身會產生 256 條對話線，
「誰跟誰講話、什麼時候講」是全新的排程與敘事問題。
但**格式已經準備好了**（獨白一行、對話為主）——這是當初改格式的主要理由。

---

## 11. 給接手者的三個提醒

1. **不要相信「看起來像真的」的輸出。** 內在獨白是模型推測，不是本人的真實想法。
   每一份產出檔案的結尾都有這條警告，請保留它。

2. **不要為了簡短而犧牲可讀性。** 這是被使用者明確糾正過的設計錯誤。
   現在的標準是：獨白一行（5%），對話為主（95%）。

3. **動 prompt 之前先跑一次基線。** `persona_sandbox demo` 用的是內建示範素材，
   結果可重現。改 prompt 後跑同一份素材對比，才知道有沒有改好。

---

## 12. 外部參考

| repo | 星數 | 對本專案的意義 |
|---|---|---|
| [titanwings/distilly](https://github.com/titanwings/distilly)（原 colleague-skill） | 24.6k | **架構靈感來源**。六層人格結構、Correction 層、版本快照都抄它。但知識來源（標籤優先）被反轉 |
| [alchaincyf/nuwa-skill](https://github.com/alchaincyf/nuwa-skill) | 32.4k | 方法論參考：**三重驗證**（跨域複現／生成力／排他性）、誠實邊界、保真度評分卡 |
| [agenmod/immortal-skill](https://github.com/agenmod/immortal-skill) | 1k | **證據分級**（verbatim/artifact/impression）與矛盾保留，本專案直接採用 |
| [mliu98/awesome-human-distillation](https://github.com/mliu98/awesome-human-distillation) | 743 | 210 個蒸餾 skill 的索引，需求調研用 |

技術報告：`.repos/distilly/colleague_skill.pdf`（上海 AI Lab，系統描述，**無評估數據**）

---

## 13. 重啟伺服器的 SOP

```bash
# 1. 確認舊的關掉
pkill -f "persona_sandbox.server"

# 2. 啟動（背景）
cd /Users/pe/Downloads/persona-distill
nohup python3 -m persona_sandbox.server --port 8800 > /tmp/persona-server.log 2>&1 &

# 3. 驗證
sleep 3
curl -sS -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:8800/
curl -sS http://127.0.0.1:8800/api/providers | python3 -m json.tool | head -5

# 4. 看 DB 狀態
python3 -m persona_sandbox db stats
```

啟動訊息會顯示 LLM 模式與 DB 路徑，例如：

```
Persona Sandbox MVP
  → http://127.0.0.1:8800
  LLM: deepseek / deepseek-chat
  DB : /Users/pe/Downloads/persona-distill/persona_sandbox.db  (616.0 KB)
       使用者 7 · session 22 · 事件 142 · 回合 91 · 人格卡 12
```

---

*交接文件結束。有問題先看第 9 節的已知限制，那裡列的東西比我記得的還誠實。*
