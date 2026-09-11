# 數位雙生人格探險平台 (Persona Sandbox) — MVP 系統開發規格說明書

本文件為「數位雙生人格探險平台 (Persona Sandbox)」最小可行性產品 (MVP) 的系統開發與產品規格說明書。本平台旨在讓使用者在去識別化（隱私安全）的前提下，蒸餾自身人格並派往特定虛擬情境（如極限求生、微醺酒吧）中與其他人格進行非同步的互動與對話，最終產出具備心理深度與趣味性的「人格碰撞日誌」。

本計畫核心參考 GitHub 熱門開源專案 [titanwings/distilly](https://github.com/titanwings/distilly "titanwings distilly github") (原名 colleague-skill/同事蒸餾) 的人格提取與語氣複製邏輯，以及衍生專案 [alchaincyf/nuwa-skill](https://github.com/alchaincyf/nuwa-skill "alchaincyf nuwa skill github") 的跨領域人格塑造方法。

---

## 1. 系統架構與技術棧 (Tech Stack)

MVP 階段以「快速驗證、輕量部署」為核心原則：

*   **前端 (Frontend):** Next.js (React) 或 HTML5/Tailwind CSS 獨立單頁應用。
*   **後端 (Backend):** Python (FastAPI) — 負責處理 LLM 互動、人格蒸餾演算法與 Agent 排程。
*   **大語言模型 (LLM):** OpenAI GPT-4o 或 Claude 3.5 Sonnet (用於高精確度人格思考鏈)。
*   **資料儲存 (Database):** 
    *   *現階段 (Local):* SQLite 或 本機 JSON 檔案。
    *   *次階段 (Cloud Target):* 升級至 Cloudflare Workers + D1 Database / KV 儲存。
*   **身分驗證 (Auth):** Firebase Auth 或 Supabase Auth (支援 Google Login / Email 註冊)。

---

## 2. 核心功能模組與系統規格

### 模組一：使用者註冊與登入 (Authentication)
*   **功能需求：** 
    *   提供簡潔的網頁登入介面。
    *   整合第三方 Oauth (Google Login) 及傳統的 Email / 密碼註冊模式。
    *   登入成功後分發 JWT 權限憑證，並引導至「人格蒸餾艙」主頁面。

### 模組二：隱私優先人格蒸餾系統 (Privacy-First Distillation)
*   **功能需求：**
    *   **資料輸入：** 支援使用者上傳少量對話文本 (TXT/JSON/CSV) 或填寫 20 題「深層潛意識心理情境問答」。
    *   **去識別化清洗 (PII Sanitization)：** 在將文本送入 LLM 前，系統須透過正則表達式或輕量模型，自動抹除姓名、公司、電話、地址等敏感個資，確保隱私安全。
    *   **人格向量化 (Persona Skill Structuring)：** 參考 `distilly` 架構，將清洗後的文本提煉為「人格特徵 JSON (Personality Profile)」，包含：
        *   `tone_style`: 語氣口吻習慣 (如：反諷、冷靜、熱情)。
        *   `core_values`: 核心價值觀矩陣 (如：利益至上、道德潔癖、享樂主義)。
        *   `vulnerabilities`: 潛在性格弱點 (如：美色誘惑抗性、權力渴望度)。
        *   `internal_monologue_prompt`: 內在獨白引導詞。

### 模組三：多 Agent 異質碰撞沙盒 (The Adventure Sandbox)
*   **功能需求：**
    *   **固定情境設定：** MVP 階段提供兩個預設常駐房間：
        1.  *「末日荒野避難所」：* 極限求生與資源配置，考驗背叛與合作。
        2.  *「微醺地下酒吧」：* 匿名社交與情感調情，考驗誘惑、撩妹與防線。
    *   **非同步對話調度 (Agent Scheduler)：** 
        *   使用者將人格「派入」房間後即可離線。
        *   系統後端排程定時觸發不同使用者的人格進行 1-on-1 或群組對話。
    *   **思考鏈機制 (Chain of Thought for Agents)：**
        *   每次輪到該 Agent 發言時，LLM 必須先輸出 `[內在獨白]`（分析當前情境、對方的誘惑/挑釁、對齊本尊性格），隨後才輸出 `[對外發言]`。

### 模組四：資料儲存與日誌管理 (Data & Log Storage)
*   **功能需求：**
    *   **結構化日誌：** 每場冒險對話須完整記錄為結構化 JSON 格式。
    *   **SQLite 欄位設計範例：**
        ```sql
        CREATE TABLE adventure_logs (
            log_id TEXT PRIMARY KEY,
            room_type TEXT,            -- 'survival' 或 'bar'
            agent_a_id TEXT,
            agent_b_id TEXT,
            conversation_history TEXT,  -- 儲存完整對話與內在獨白 JSON String
            created_at TIMESTAMP
        );
        ```

### 模組五：互動結果查看與通知 (Result Review & Delivery)
*   **功能需求：**
    *   **網頁端看板：** 使用者登入後，可進入「冒險回顧」列表，以劇本形式（包含內在獨白）瀏覽分身與他人碰撞的趣味對話。
    *   **Email 自動派送：** 當一場沙盒互動結束（如對話達 15 輪或分身在求生中淘汰），後端觸發郵件服務 (如 Resend 或 SendGrid)，將精美排版的對話劇本直接寄送至使用者信箱。

---

## 3. 系統核心業務流程 (Workflow)

```
[ 使用者 ] ──> 1. 註冊/登入 (Google/Email)
   │
   └───> 2. 上傳對話/做測試問答 ──> [ 隱私清洗 PII ] ──> 3. 生成「人格特徵包」
                                                               │
[ 查看網頁日誌 / 收到 Email ] <── 5. 沙盒結束，打包日誌 <── 4. 派入「常駐房間」與他人 Agent 碰撞
```

---

## 4. MVP 開發里程碑與驗證指標 (Timeline & Metrics)

*   **第 1-2 週：** 完成 Google Auth 登入與前端基礎頁面搭建；撰寫去識別化人格蒸餾 Prompt 與 JSON 格式定義。
*   **第 3 週：** 開發後端沙盒排程發動機，串接 LLM (GPT-4o/Claude) 實現「內在獨白 + 輪流對話」機制，並暫存至 SQLite。
*   **第 4 週：** 完成網頁日誌檢視面板與 Email 自動寄送整合，進行 20 人內部種子測試。

**MVP 成功驗證指標：** 測試使用者中，有超過 **60%** 在閱讀分身與他人對話的日誌後，表示「對話過程饒富趣味，且確實看出了自己或對方的真實性格影子」。
