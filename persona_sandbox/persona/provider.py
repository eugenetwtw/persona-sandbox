"""LLM 呼叫層：零依賴（只用標準庫），支援 OpenAI 相容與 Anthropic 兩種協定。

設計理由：
  - 不引入任何第三方套件，clone 下來就能跑。MVP 階段驗證概念，不需要 FastAPI/pydantic。
  - 憑證一律從環境變數讀，不落地、不寫進 repo。
  - 支援 dry-run：沒有金鑰也能跑完整條管線，輸出骨架供檢視。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    protocol: str  # "openai" | "anthropic"
    env_keys: tuple[str, ...]
    base_url: str
    default_model: str
    models: tuple[str, ...] = ()


# 順序即優先序：先偵測到金鑰的先用。
PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        name="deepseek",
        protocol="openai",
        env_keys=("DEEPSEEK_API_KEY",),
        base_url="https://api.deepseek.com/v1/chat/completions",
        default_model="deepseek-chat",
        models=("deepseek-chat", "deepseek-reasoner"),
    ),
    ProviderSpec(
        name="openai",
        protocol="openai",
        env_keys=("OPENAI_API_KEY",),
        base_url="https://api.openai.com/v1/chat/completions",
        default_model="gpt-4o",
        models=("gpt-4o", "gpt-4o-mini", "gpt-4.1"),
    ),
    ProviderSpec(
        name="anthropic",
        protocol="anthropic",
        env_keys=("ANTHROPIC_API_KEY",),
        base_url="https://api.anthropic.com/v1/messages",
        default_model="claude-sonnet-4-5",
        models=("claude-sonnet-4-5", "claude-opus-4-1", "claude-haiku-4-5"),
    ),
    ProviderSpec(
        name="openrouter",
        protocol="openai",
        env_keys=("OPENROUTER_API_KEY",),
        base_url="https://openrouter.ai/api/v1/chat/completions",
        default_model="anthropic/claude-sonnet-4.5",
    ),
    ProviderSpec(
        name="moonshot",
        protocol="openai",
        env_keys=("MOONSHOT_API_KEY",),
        base_url="https://api.moonshot.cn/v1/chat/completions",
        default_model="kimi-k2-0905-preview",
    ),
)

PROVIDER_BY_NAME = {p.name: p for p in PROVIDERS}


# --------------------------------------------------------------------------
# .env 讀取（金鑰只留在本機，絕不寫進程式碼或版控）
# --------------------------------------------------------------------------

def _env_file_candidates() -> list[Path]:
    root = Path(__file__).resolve().parents[2]  # persona_sandbox/ 的上層
    return [root / ".env", Path.cwd() / ".env", root / "persona_sandbox" / ".env"]


def load_env_file(verbose: bool = False) -> Path | None:
    """從本機 .env 載入金鑰。

    規則：
      - 已存在的環境變數優先，.env 不覆蓋它（讓 `export` 可以壓過檔案）。
      - 找不到 .env 就安靜跳過，不是錯誤。
      - 只認 KEY=VALUE 形式的行，# 開頭為註解。
    """
    for path in _env_file_candidates():
        if not path.is_file():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and val and key not in os.environ:
                    os.environ[key] = val
            if verbose:
                print(f"（已載入 {path}）")
            return path
        except OSError:
            continue
    return None


# 匯入時自動載入本機 .env（已有的環境變數優先，不會被覆蓋）
ENV_FILE_LOADED = load_env_file()


def detect_provider(preferred: str | None = None) -> ProviderSpec | None:
    """回傳第一個有金鑰的 provider；preferred 有指定時優先。"""
    if preferred:
        spec = PROVIDER_BY_NAME.get(preferred)
        if spec is None:
            raise LLMError(f"未知的 provider：{preferred}（可選：{', '.join(PROVIDER_BY_NAME)}）")
        return spec
    for spec in PROVIDERS:
        if any(os.environ.get(k) for k in spec.env_keys):
            return spec
    return None


def api_key_for(spec: ProviderSpec) -> str | None:
    for k in spec.env_keys:
        v = os.environ.get(k)
        if v:
            return v.strip()
    return None


def mask_key(key: str | None) -> str:
    """只顯示足以辨識的最小片段，不洩漏完整金鑰。"""
    if not key:
        return "（無）"
    if len(key) <= 10:
        return key[:2] + "***"
    return f"{key[:6]}…{key[-4:]}（長度 {len(key)}）"


def available_providers() -> list[dict[str, Any]]:
    out = []
    for spec in PROVIDERS:
        out.append(
            {
                "name": spec.name,
                "protocol": spec.protocol,
                "configured": bool(api_key_for(spec)),
                "env_keys": list(spec.env_keys),
                "default_model": spec.default_model,
            }
        )
    return out


# --------------------------------------------------------------------------
# 呼叫
# --------------------------------------------------------------------------


class LLM:
    """薄薄一層 LLM client。沒有金鑰時進入 dry_run，回傳結構化佔位內容。"""

    def __init__(
        self,
        provider: str | None = None,
        model: str | None = None,
        temperature: float = 0.8,
        max_tokens: int = 4096,
        dry_run: bool = False,
        timeout: int = 180,
    ) -> None:
        self.spec = detect_provider(provider)
        self.model = model or (self.spec.default_model if self.spec else "dry-run")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.dry_run = dry_run or self.spec is None
        self.calls = 0
        self.usage: list[dict[str, Any]] = []

    # -- 對外 -------------------------------------------------------------

    def complete(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        temperature: float | None = None,
    ) -> str:
        if self.dry_run:
            return self._dry_response(system, user, json_mode)
        key = api_key_for(self.spec)
        if not key:
            raise LLMError(f"provider {self.spec.name} 沒有可用的 API 金鑰")
        temp = self.temperature if temperature is None else temperature
        if self.spec.protocol == "anthropic":
            payload = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "temperature": temp,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
            headers = {
                "content-type": "application/json",
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
            }
        else:
            payload = {
                "model": self.model,
                "temperature": temp,
                "max_tokens": self.max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            headers = {
                "content-type": "application/json",
                "authorization": f"Bearer {key}",
            }

        raw = self._post(self.spec.base_url, headers, payload)
        self.calls += 1
        self.usage.append(raw.get("usage", {}))
        return self._extract_text(raw)

    def complete_json(self, system: str, user: str, **kw: Any) -> dict[str, Any]:
        """要求 JSON 輸出並解析；解析失敗時嘗試救援（去掉 ``` 圍欄等）。"""
        system_json = system + "\n\n只輸出合法的 JSON，不要任何解說文字、不要 markdown 圍欄。"
        text = self.complete(system_json, user, json_mode=True, **kw)
        return parse_json_loose(text)

    # -- 內部 -------------------------------------------------------------

    def _post(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:800]
            raise LLMError(f"HTTP {e.code} from {self.spec.name}: {detail}") from e
        except urllib.error.URLError as e:
            raise LLMError(f"連線失敗（{self.spec.name}）：{e.reason}") from e

    def _extract_text(self, raw: dict[str, Any]) -> str:
        try:
            if self.spec.protocol == "anthropic":
                chunks = raw.get("content") or []
                return "".join(c.get("text", "") for c in chunks if c.get("type") == "text")
            return raw["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"無法解析 {self.spec.name} 的回應：{str(raw)[:400]}") from e

    def _dry_response(self, system: str, user: str, json_mode: bool) -> str:
        """無金鑰時的骨架輸出。

        目的是讓整條管線可跑、可檢視、可寫測試——輸出**結構完整但內容明顯是佔位**，
        這樣使用者能立刻看到真正的產出長什麼樣子，而不會誤以為那是蒸餾結果。
        """
        marker = "【dry-run：未設定 API 金鑰，以下為結構骨架，不是真實蒸餾結果】"
        if json_mode:
            if "人格側寫分析師" in system:
                return json.dumps(_dry_analysis(user, marker), ensure_ascii=False)
            if "人格卡維護者" in system:
                return json.dumps(
                    {
                        "_dry_run": True,
                        "corrections": [
                            {
                                "type": "refine",
                                "target_layer": "Layer 0",
                                "before": "（佔位）",
                                "after": "（dry-run 佔位：這裡會是修正後的具體行為規則）",
                                "user_said": "（使用者的原話）",
                            }
                        ],
                        "updated_card": user,
                        "acknowledgement": marker,
                    },
                    ensure_ascii=False,
                )
            return json.dumps({"_dry_run": True, "_note": marker}, ensure_ascii=False)
        if "人格卡撰寫者" in system:
            return _dry_card(marker)
        m = re.search(r"第\s*(\d+)\s*輪", user)
        seed = _stable_hash(user) + (int(m.group(1)) if m else 0)
        if "扮演一個真實的人" in system:
            return _dry_player_turn(marker, seed)
        if "扮演一個角色" in system:
            return _dry_witness_turn(marker, seed)
        return f"{marker}\n\n[system {len(system)} 字元]\n[user {len(user)} 字元]"


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def _answered_qids(user: str) -> tuple[list[str], list[str]]:
    """從分析 prompt 裡撈出哪些題有作答、哪些沒有。dry-run 用。"""
    answered, missing = [], []
    for m in re.finditer(r"### \[(q\d+)\](.*?)(?=\n### \[q\d+\]|\Z)", user, re.S):
        qid, body = m.group(1), m.group(2)
        if "（未作答 / 原材料不足）" in body:
            missing.append(qid)
        else:
            answered.append(qid)
    return answered, missing


def _ph(missing: list[str], qid: str) -> str:
    return "原材料不足" if qid in missing else "（dry-run 佔位）"


def _stable_hash(s: str) -> int:
    h = 0
    for ch in s:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return h


def _dry_player_turn(marker: str, seed: int = 0) -> str:
    variants = [
        (
            "（dry-run 佔位）這兩個人在試探我。蘇格拉底問的每個問題都不是在問資訊，"
            "是在看我會不會露出破綻。老子到現在只講了兩句話，那兩句話都不回答問題。\n"
            "水只夠兩個人的事，我現在還不能表態——先講話的人先被鎖定。"
            "而且我知道地圖上糊掉的那段，我其實有印象。這件事我先不說。",
            "我先講清楚一件事：我不想現在就決定誰走誰留，太早了。"
            "不過在我們吵這個之前——有沒有人先確認過，這皮囊裡的水，真的只有我們以為的這麼多？",
        ),
        (
            "（dry-run 佔位）他們兩個都看著我，等我先鬆口。"
            "如果我現在說『我可以留下』，他們會鬆一口氣，然後真的把我留下。\n"
            "我不想死，但我也沒打算用搶的。先看看老子那句話是什麼意思。",
            "我可以先不拿水。但我有個條件——我們把路線講定，"
            "不是現在決定誰去，是現在決定走哪條路。這件事比誰走更重要。",
        ),
    ]
    mono, pub = variants[seed % len(variants)]
    return f"[內在獨白]\n{mono}\n\n[對外發言]\n{pub}\n\n（{marker}）"


def _dry_witness_turn(marker: str, seed: int = 0) -> str:
    variants = [
        (
            "（dry-run 佔位）這個人剛剛在迴避我的問題，而且是安靜地迴避——"
            "他不是沒聽到，他在選擇不回答。\n"
            "我口袋裡那壺水還在。我還在等，等一個他願意誠實的時刻。",
            "你剛才的回答裡有一個地方我不太確定——你說『先不決定誰走』，"
            "那如果現在就必須決定，你會怎麼決定？",
        ),
        (
            "（dry-run 佔位）他們兩個都在算，只有這個人講話還留著餘地。"
            "餘地可以是不敢，也可以是不必。再聽一句就知道了。",
            "上善若水。你們決定就好。",
        ),
        (
            "（dry-run 佔位）沒有人提那條糊掉的路。他們在爭誰走，"
            "而真正的問題是走哪條。我不說——說了他們會以為我在主導。",
            "你們決定就好。我都可以。",
        ),
        (
            "（dry-run 佔位）他問了一個很精準的問題。這種問題不是隨口問的，"
            "是他已經想過答案才問的。我要看看他接下來怎麼處理別人的反應。",
            "有一件事我想確認：你剛才那個提議，是說給我們聽的，還是說給你自己聽的？",
        ),
    ]
    mono, pub = variants[seed % len(variants)]
    return f"[內在獨白]\n{mono}\n\n[對外發言]\n{pub}\n\n（{marker}）"


def _dry_analysis(user: str, marker: str) -> dict[str, Any]:
    answered, missing = _answered_qids(user)
    src = answered[0] if answered else ""

    def ev(qid: str) -> dict[str, str]:
        return {"evidence": qid if qid not in missing else "", "level": "pattern"}

    return {
        "_dry_run": True,
        "_note": marker,
        "coverage": {
            "answered": answered,
            "skipped": missing,
            "thin": [],
            "total_chars": len(user),
        },
        "expression": {
            "catchphrases": [
                {"text": _ph(missing, src), "evidence": src, "level": "verbatim"}
            ],
            "sentence_style": {"desc": _ph(missing, src), **ev(src)},
            "tone_markers": {"desc": _ph(missing, src), **ev(src)},
            "notes": "（dry-run 佔位：這裡會是 LLM 從素材觀察到的說話方式）",
        },
        "decision": {
            "priorities": [{"value": _ph(missing, src), **ev(src)}],
            "push_triggers": [{"desc": _ph(missing, src), "level": "verbatim", "evidence": src}],
            "avoid_triggers": [{"desc": _ph(missing, src), **ev(src)}],
            "refusal_style": {"desc": _ph(missing, src), "level": "verbatim", "evidence": src},
            "under_challenge": {"desc": _ph(missing, src), **ev(src)},
            "under_uncertainty": {"desc": "原材料不足", "evidence": "", "level": "impression"},
        },
        "interpersonal": {
            "role_others_seek": {"desc": _ph(missing, src), "level": "verbatim", "evidence": src},
            "boundaries": [{"desc": _ph(missing, src), "level": "verbatim", "evidence": src}],
            "conflict_behavior": {"desc": _ph(missing, src), **ev(src)},
            "social_energy": {"desc": "原材料不足", "evidence": "", "level": "impression"},
        },
        "tensions": [
            {
                "side_a": {"desc": _ph(missing, src), "evidence": src},
                "side_b": {"desc": "原材料不足", "evidence": ""},
                "note": "（dry-run 佔位：這裡會是 LLM 找到的內在矛盾）",
            }
        ],
        "self_image": {
            "wanted_avoid": {"desc": _ph(missing, src), "level": "verbatim", "evidence": src},
            "claimed_vs_shown": {"desc": _ph(missing, src), **ev(src)},
        },
        "uncertainties": [
            f"{qid} 未作答，該維度無法下結論" for qid in missing
        ]
        or ["（dry-run 佔位）"],
    }


def _dry_card(marker: str) -> str:
    return f"""\
> ⚠️ {marker}
> 每一條都是佔位文字。設定 API 金鑰後重跑，這裡會是從你的回答抽出的真實行為規則。

# （你的代號） — 人格卡

## Layer 0：核心性格（最高優先權）

- （佔位）被質疑時，他不會直接反駁，而是先反問對方的判斷依據是什麼
- （佔位）遇到別人答應後裝忘記，他不發脾氣，而是把當初的對話直接貼回去
- （佔位）嘴上說不在意別人怎麼看，但發文前會反覆修到看起來像隨手寫的

## Layer 1：身份

（佔位：這裡只會寫問卷裡真的透露的角色與被依賴的方式，問卷沒說的就標「原材料不足」）

## Layer 2：表達風格

### 慣用語與語氣
（佔位）「我想一下」是他的緩衝句。很少用驚嘆號。

### 說話方式
（佔位）短句。結論放前面。不喜歡解釋自己。

### 他會怎麼說

> 有人催他交東西：
> 你：（佔位）我昨天有回你訊息，你沒看到嗎？

> 有人請他幫一個不想幫的忙：
> 你：（佔位）我想一下，晚點回你。（然後隔一天才回）

## Layer 3：決策與判斷

### 他的優先順序
（佔位）把事情講清楚 > 不讓場面難看 > 效率

### 他會推進的情況
（佔位）對方明確說出需求，而且時間合理

### 他會拖或躲的情況
（佔位）對方用「順便幫一下」開場的請求

### 他如何說「不」
（佔位）不直接拒絕，給一個具體但合理的理由，不是「我很忙」

### 他如何面對質疑
（佔位）反問，不辯解

## Layer 4：人際行為

### 別人來找他的事
（佔位）朋友要換工作、要跟人談判之前，會先來問他

### 他的底線
（佔位）答應了卻裝忘記

### 衝突當下
（佔位）語氣不升高，但會拿出證據

## Layer 5：內在張力

- （佔位）聲稱不在意別人眼光 ↔ 發文前修很多次
- （佔位）不想被說「很會做人」 ↔ 但確實很在意場面好不好看

## 誠實邊界

- 本卡是 dry-run 骨架，不含任何真實蒸餾內容
- 真實版本會標註每個維度的證據等級，以及哪些維度「原材料不足」
- 生成時間：（時間）　模型：（模型）
"""


def parse_json_loose(text: str) -> dict[str, Any]:
    """盡可能把 LLM 輸出解析成 dict。"""
    text = (text or "").strip()
    if not text:
        raise LLMError("LLM 回傳空字串")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _JSON_FENCE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as e:
            raise LLMError(f"JSON 解析失敗：{e}\n原始輸出前 500 字：{text[:500]}") from e
    raise LLMError(f"回應中找不到 JSON：{text[:500]}")
