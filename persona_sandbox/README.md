# 人格沙盒 · Persona Sandbox（MVP）

把你自己蒸餾成一張**人格卡**，然後派進一個壓力情境，跟歷史人物一起做決定。

這是 `persona_sandbox_mvp_spec.md` 的最小可跑實作。範圍刻意縮到很小：
**問卷 → 蒸餾 → 本人確認 → 一個場景 → 兩輪對話**，先驗證「這樣好不好玩、準不準」，
再決定要不要蓋 auth、排程、寄信那一整套。

---

## 快速開始

零依賴，只要 Python 3.9+（開發環境用 3.13）。

```bash
cd /Users/pe/Downloads/persona-distill

# 1. 看有沒有可用的 LLM 金鑰
python3 -m persona_sandbox providers

# 2. 設定其中一個（任一即可）
export DEEPSEEK_API_KEY=sk-...        # 最便宜，建議先用這個
# export OPENAI_API_KEY=sk-...
# export ANTHROPIC_API_KEY=sk-ant-...

# 3a. 網頁版
python3 -m persona_sandbox.server
#    打開 http://127.0.0.1:8800

# 3b. 或命令列版
python3 -m persona_sandbox demo        # 用內建示範素材跑完整條管線
python3 -m persona_sandbox ask         # 跑問卷（互動）
```

**沒有金鑰也能跑。** 會進入 `dry-run` 模式，輸出結構完整但標示為佔位的骨架，
讓你先看清楚產出長什麼樣子。

---

## 它怎麼運作

```
8 題問卷 ──→ 行為分析（結構化 JSON，每條附證據等級）
              │
              ├──→ 人格卡（六層，具體行為＋具體台詞）
              │       │
              │       └──→ 【本人確認】──→ 修正記錄（typed，累積生效）
              │
              └──→ 沙盒：壓力情境 + 2 位歷史人物
                      └──→ 每回合輸出 [內在獨白] + [對外發言]（允許不一致）
```

### 只用兩次 LLM 呼叫

| 步驟 | 做什麼 |
|---|---|
| 1. 分析 | 問卷素材 → 行為分析 JSON，每條標 `verbatim` / `pattern` / `impression` |
| 2. 撰寫 | 行為分析 → 人格卡 Markdown |
| （之後） | 修正與沙盒各自一次呼叫 |

不是 distilly 的多 agent 長鏈。理由：這是 UGC 規模的產品，成本與延遲必須可控。

---

## 與 `distilly` 的關鍵差異

我們抄了它的架構，但**反轉了它的知識來源**。

| | `titanwings/distilly` | 本專案 |
|---|---|---|
| 優先序 | 手動標籤 **>** 素材分析 | 素材 **>** 模式 **>** 印象 |
| 人格來源 | 預先寫死的標籤表（15 個個性＋10 個企業文化標籤） | 只從素材抽，沒有罐頭標籤 |
| 輸出 | 五層（L1-L5） | 六層（Layer 0-5）＋證據追溯 |
| 證據 | 無 | 每條結論標等級與題號 |
| 缺口處理 | 標「原材料不足」 | 同，且列進 `uncertainties` |
| 修正 | Correction 層 | 同，且修正記錄**有型別**（add/refine/remove/tension） |

具體差別：distilly 的 `persona_analyzer.md` 第 11 行寫「有衝突時以手動標籤為準」，
所以素材只是裝飾——蒸出來的角色像職場漫畫人物。我們要蒸的是真人，
所以**素材優先，而且本人能否決**。

---

## 檔案結構

```
persona_sandbox/
├── __main__.py           # CLI：providers / ask / demo / distill / correct / play
├── server.py             # 零依賴 HTTP server（http.server）
├── persona/
│   ├── questions.py      # 8 題問卷 + 追問規則 + 素材體檢
│   ├── prompts.py        # 蒸餾 prompt（分析／撰寫／修正）
│   ├── provider.py       # LLM adapter：OpenAI 相容 + Anthropic，含 dry-run
│   └── engine.py         # 蒸餾管線 + PersonaCard
├── world/
│   ├── witnesses.py      # 名人角色卡（蘇格拉底、老子）
│   ├── scenario.py       # 壓力情境（稀缺 + 不對稱資訊 + 隱藏議程）
│   └── sandbox.py        # Encounter 狀態機 + Sandbox 包裝
├── web/index.html        # 單頁 UI
└── out/                  # 產出（answers / cards / logs）
```

---

## 設計決策（為什麼這樣做）

**1. 問故事，不問形容詞。**
「你是內向還是外向」得到的是罐頭標籤；「講一件你後悔的決定」才能抽出決策邏輯。
第 6 題（怎麼拒絕人）是刻意的——**說「不」的方式是人格的最高解析度切片**。
第 5 題（嘴上說不在意但其實在意）在抓矛盾，矛盾是最有價值的素材。

**2. 本人確認是品質的來源，不是合規的步驟。**
問卷素材是最弱的素材（`impression` 等級）。本人說「對，這是我」的確認訊號，
比多問 50 題值錢。所以修正層不是附屬功能，是核心。

**3. 名人只是鉤子，壓力才是黏著。**
「跟愛因斯坦聊天」新鮮感 3 分鐘就沒了。場景內建**稀缺**（水只夠兩個人）、
**不對稱資訊**（地圖有一段糊掉）、**隱藏議程**（每個人都有不能公開的目標），
逼出真實反應。

**4. 內在獨白與對外發言必須分開，而且允許不一致。**
兩者一致的對話是廢話。**兩者出現落差的地方，才是人格。**
UI 會自動標記「獨白與發言不一致」的回合。

**5. 名人只收已故者。**
已故無肖像權／名譽權主張主體，且留下大量可引用的一手文本。
第一版內容池的建議：只收已故歷史人物。

---

## 已知限制

- **沒有真實 LLM 跑過的輸出。** 開發環境沒有 API 金鑰，所有示範都是 dry-run 骨架。
  這是目前最大的未驗證風險：**人格卡的品質還沒被人眼評分過。**
- **沒有評估機制。** 沒有保真度評分、沒有「這像不像我」的量表。
  女媧有 [fidelity-scorecard](https://github.com/alchaincyf/nuwa-skill/blob/main/references/fidelity-scorecard.md)，
  之後應該抄一套。
- **結束判定是關鍵詞比對**，不是 LLM 裁判（見 `sandbox.py:_should_end`）。
- **沒有持久化。** Session 在記憶體，重啟就沒了。產出只落檔在 `out/`。
- **沒有隱私處理。** 目前沒有 PII 清洗。因為 MVP 是本地執行，資料不出機器；
  一旦要上線，這一塊必須補（見下方警告）。
- **角色卡只有 2 位。** 場景只有 1 個。

---

## ⚠️ 給未來上線版本的警告

原 spec 第 34 行寫「去識別化清洗 (PII Sanitization)：透過正則表達式或輕量模型，
自動抹除姓名、公司、電話、地址等敏感個資」。

**正則表達式抹不掉語意指紋。** 一個人獨特的說話方式、口頭禪、在意的事，
本身就是辨識資訊。真正的匿名化遠比這難。

而且原 spec 的架構是「使用者上傳 → 你的後端 → OpenAI/Claude → 存 SQLite」，
這等於把使用者的私人對話送到第三方，**跟「隱私優先」的宣稱有落差**。

本 MVP 之所以還算安全，是因為它**完全在本機執行，資料不出機器**——
`distilly` 的論文也是這樣宣告的（"All data is processed and stored locally;
no raw content leaves the user's machine"）。

要做成產品時，這個決定必須重新面對，不能靠一句「已去識別化」帶過。

---

## 授權與出處

架構靈感來自 [titanwings/distilly](https://github.com/titanwings/distilly)（MIT）與
[alchaincyf/nuwa-skill](https://github.com/alchaincyf/nuwa-skill)（MIT）。
本專案為獨立實作。
