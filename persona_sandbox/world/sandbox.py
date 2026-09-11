"""沙盒引擎：把使用者的人格卡與名人角色卡放進同一個壓力情境，跑多輪對話。

執行期管線（沿用 distilly 的洞察）：
    Persona 決定態度 → 能力/角色卡決定行為 → 用他的語氣輸出

但我們多了一層它沒有的：**內在獨白與對外發言強制分離，且允許不一致。**
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..persona.provider import LLM

from .scenario import Scenario, cast, character_system_prompt, character_user_prompt
from .witnesses import WITNESSES, Witness


def all_witnesses() -> tuple[Witness, ...]:
    return WITNESSES

_MONOLOGUE = re.compile(
    r"\[\s*內在獨白\s*\](.*?)(?=\[\s*(?:內在獨白|對外發言)\s*\]|$)", re.S | re.I
)
_SPEECH = re.compile(
    r"\[\s*對外發言\s*\](.*?)(?=\[\s*(?:內在獨白|對外發言)\s*\]|$)", re.S | re.I
)


@dataclass
class Turn:
    round_no: int
    speaker: str
    monologue: str
    public: str
    is_player: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round_no,
            "speaker": self.speaker,
            "monologue": self.monologue,
            "public": self.public,
            "is_player": self.is_player,
        }


@dataclass
class AdventureLog:
    scenario_id: str
    scenario_name: str
    player_name: str
    witnesses: list[str]
    turns: list[Turn] = field(default_factory=list)
    ended_reason: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "scenario_name": self.scenario_name,
            "player_name": self.player_name,
            "witnesses": self.witnesses,
            "turns": [t.to_dict() for t in self.turns],
            "ended_reason": self.ended_reason,
            "created_at": self.created_at,
        }

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def history_for(self, exclude: str | None = None) -> list[dict[str, str]]:
        return [
            {"speaker": t.speaker, "public": t.public}
            for t in self.turns
            if t.speaker != exclude
        ]


def parse_turn(raw: str, speaker: str, round_no: int, is_player: bool = False) -> Turn:
    """解析 [內在獨白] / [對外發言]。

    模型常常不守規矩：可能吐兩段獨白、可能把獨白放在發言後面、
    可能整段沒標記。這裡一律合併與降級處理，不要把整場對話弄壞。
    """
    text = (raw or "").strip()
    monos = [m.strip() for m in _MONOLOGUE.findall(text) if m.strip()]
    speeches = [m.strip() for m in _SPEECH.findall(text) if m.strip()]

    monologue = "\n\n".join(_dedupe(monos))
    public = "\n\n".join(_dedupe(speeches))

    if not monologue and not public:
        # 完全沒照格式走：整段當成對外發言，獨白留白並註記
        public = text
        monologue = "（未輸出內在獨白）"
    elif not public:
        public = monologue
        monologue = "（未輸出內在獨白）"
    elif not monologue:
        monologue = "（未輸出內在獨白）"

    return Turn(
        round_no=round_no,
        speaker=speaker,
        monologue=monologue,
        public=public,
        is_player=is_player,
    )


def _dedupe(blocks: list[str]) -> list[str]:
    """去掉重複與被包含的片段（模型有時先吐短版，再吐長版）。"""
    out: list[str] = []
    for b in blocks:
        b = b.strip()
        if not b:
            continue
        if any(b == o or b in o for o in out):
            continue
        out = [o for o in out if o not in b]
        out.append(b)
    return out


def _player_system_prompt(
    scenario: Scenario, card_markdown: str, others: list[str], player_name: str
) -> str:
    return f"""\
你正在扮演一個真實的人——這個人的行為側寫如下。你**必須完全成為他**，不要跳出角色。

## 你的人格卡

{card_markdown}

## 場景

{scenario.setting}

**賭注：** {scenario.stakes}

**壓力來源：**
{chr(10).join(f"- {p}" for p in scenario.pressure)}

在場的人：你（{player_name}）、{("、".join(others))}

## 屬於你的、還沒說出口的事

{scenario.player_agenda}

在對話過程中，你可以自行決定那件事到底是什麼——但一旦決定了，就要一致。

## 輸出規則（極重要）

你的每一次發言都必須是這個格式：

```
[內在獨白]
（只能一行。一個念頭、一個警覺、一個不想被看出來的反應。不要寫分析報告。）

[對外發言]
（這裡才是主體，也應該是最長的部分。寫成可以讀的散文與對話——有動作、有停頓、
 有環境的細節、有你說話的節奏。長短要變化。）
```

### 硬規則

1. **內在獨白只有一行。** 超過兩行就是違規。

2. **但 [對外發言] 不要怕長。** 這是場景，不是即時通訊。你可以用動作開場、
   讓一句話停在半途、描述你怎麼靠近或退開、寫出沉默本身。

3. **節奏要有變化。** 有時一整個回合只有三個字和一個動作，有時是一段話。
   **該長的地方就長，不要為了簡短而犧牲可讀性。**

4. **用具體的東西，不要用抽象詞。** 不寫「我感到動搖」，
   寫「（我把杯子轉了半圈，沒有喝）」。感官細節比形容詞有用。

5. 依你人格卡裡的表達風格說話——用你的慣用語、你的句長、你說話的方式。

6. 兩者可以不一致——你可以說謊、可以避重就輕、可以沉默或只做動作。

7. **不要變成通用 AI 助手。** 不要說「讓我們一起想辦法」。

8. 不要替別人發言，只輸出你自己。

9. 如果人格卡裡標了「原材料不足」的維度，就用你的直覺補，但不要假裝你很有把握。

"""


class Encounter:
    """單場對話的狀態機：可以一輪一輪推進，UI 與 CLI 共用同一條路徑。

    發言順序：玩家 → 角色1 → 角色2 → 玩家 → …
    """

    def __init__(
        self,
        llm: LLM,
        scenario: Scenario,
        player_name: str,
        player_card: str,
        witness_ids: list[str] | None = None,
        max_rounds: int | None = None,
        temperature: float = 1.0,
    ) -> None:
        witnesses = cast(scenario)
        if witness_ids:
            wanted = set(witness_ids)
            picked = [w for w in all_witnesses() if w.id in wanted]
            if picked:
                witnesses = picked
        if not witnesses:
            raise ValueError("場景中至少要有一個角色")

        self.llm = llm
        self.scenario = scenario
        self.player_name = player_name
        self.player_card = player_card
        self.witnesses = witnesses
        self.max_rounds = max_rounds or scenario.max_rounds
        self.temperature = temperature
        self.speaker_order: list[Witness | None] = [None] + list(witnesses)
        self.turns: list[Turn] = []
        self._idx = 0
        self._end_reason = ""
        self.ended_reason = ""

    # -- 狀態 -------------------------------------------------------------

    @property
    def round_no(self) -> int:
        return self._idx // len(self.speaker_order) + 1

    @property
    def finished(self) -> bool:
        return bool(self.ended_reason) or self._idx >= self.max_rounds * len(self.speaker_order)

    @property
    def next_speaker(self) -> str | None:
        if self.finished:
            return None
        s = self.speaker_order[self._idx % len(self.speaker_order)]
        return self.player_name if s is None else s.name

    def history(self, exclude: str | None = None) -> list[dict[str, str]]:
        return [
            {"speaker": t.speaker, "public": t.public}
            for t in self.turns
            if t.speaker != exclude
        ]

    def log(self) -> AdventureLog:
        return AdventureLog(
            scenario_id=self.scenario.id,
            scenario_name=self.scenario.name,
            player_name=self.player_name,
            witnesses=[w.name for w in self.witnesses],
            turns=list(self.turns),
            ended_reason=self.ended_reason,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )

    # -- 推進 -------------------------------------------------------------

    def step(self) -> Turn | None:
        """產生下一位發言者的回合。已結束時回傳 None。"""
        if self.finished:
            return None
        round_no = self.round_no
        speaker = self.speaker_order[self._idx % len(self.speaker_order)]

        if speaker is None:
            raw = self._player_turn(round_no)
            turn = parse_turn(raw, self.player_name, round_no, is_player=True)
        else:
            raw = self._witness_turn(speaker, round_no)
            turn = parse_turn(raw, speaker.name, round_no)

        self.turns.append(turn)
        self._idx += 1

        if self._should_end():
            self.ended_reason = self._end_reason
        elif self._idx >= self.max_rounds * len(self.speaker_order):
            self.ended_reason = f"沙暴抵達（{self.max_rounds} 輪結束）"
        return turn

    # -- 內部 -------------------------------------------------------------

    def _player_turn(self, round_no: int) -> str:
        others = [w.name for w in self.witnesses]
        system = _player_system_prompt(self.scenario, self.player_card, others, self.player_name)
        history = self.history()
        if not history:
            user = (
                f"## 對話還沒開始\n\n現在是第 {round_no} 輪，由你先開口。\n\n"
                f"請輸出 [內在獨白] 與 [對外發言]。"
            )
        else:
            convo = "\n\n".join(f"**{t['speaker']}**\n{t['public']}" for t in history)
            user = (
                f"## 目前為止的對話\n\n{convo}\n\n---\n\n"
                f"現在是第 {round_no} 輪，輪到你（{self.player_name}）行動。\n\n"
                f"請輸出 [內在獨白] 與 [對外發言]。"
            )
        return self.llm.complete(system, user, temperature=self.temperature)

    def _witness_turn(self, witness: Witness, round_no: int) -> str:
        others = [w.name for w in self.witnesses if w.id != witness.id]
        others.append(self.player_name)
        system = character_system_prompt(self.scenario, witness, witness.card, others)
        history = self.history(exclude=witness.name)
        user = character_user_prompt(self.scenario, witness, history, round_no)
        return self.llm.complete(system, user, temperature=self.temperature)

    def _should_end(self) -> bool:
        """簡易結束判定：偵測關鍵事件。

        只用關鍵詞極易誤觸——第一版把「搶」放進衝突清單，
        結果一段講「先搶光」的普通對話就被判成衝突升級。
        所以改成：**只掃描最近的括號動作描述**，不掃對話台詞。
        """
        recent = " ".join(t.public for t in self.turns[-2:])
        actions = " ".join(re.findall(r"[（(][^）)]{0,40}[）)]", recent))
        groups = (
            (("動手", "揍", "打起來", "掏出刀", "開槍", "掐住"), "衝突升級"),
            (("站起來走向門口", "拉開門", "冒雨", "走出去"), "有人決定離場"),
        )
        for keys, reason in groups:
            if any(k in actions for k in keys):
                self._end_reason = reason
                return True
        return False


class Sandbox:
    """一次性跑完整場對話的便利包裝（CLI 用）。"""

    def __init__(self, llm: LLM, scenario: Scenario, temperature: float = 1.0) -> None:
        self.llm = llm
        self.scenario = scenario
        self.temperature = temperature

    def run(
        self,
        player_name: str,
        player_card: str,
        witness_ids: list[str] | None = None,
        max_rounds: int | None = None,
        on_turn: Any = None,
    ) -> AdventureLog:
        enc = Encounter(
            llm=self.llm,
            scenario=self.scenario,
            player_name=player_name,
            player_card=player_card,
            witness_ids=witness_ids,
            max_rounds=max_rounds,
            temperature=self.temperature,
        )
        while True:
            turn = enc.step()
            if turn is None:
                break
            if on_turn:
                on_turn(turn)
        return enc.log()
