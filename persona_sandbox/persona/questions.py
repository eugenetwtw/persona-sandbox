"""問卷模組：8 題對話式問卷。

設計原則（來自 distilly 的教訓）：
  - 問故事，不問形容詞。形容詞會得到罐頭標籤，故事才能抽出決策邏輯。
  - 每題標明「抽取意圖」，讓 LLM 知道這題在挖什麼，而不是自由發揮。
  - 答案太短時追問一次，不一開始就嚇跑使用者。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

QuestionKind = Literal["episode", "boundary", "self_image", "value"]


@dataclass(frozen=True)
class FollowUp:
    """追問規則：答案短於 min_chars 時觸發，每次隨機挑一句，避免罐頭感。"""

    min_chars: int
    prompts: tuple[str, ...]

    def pick(self, seed: int) -> str:
        return self.prompts[seed % len(self.prompts)]


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    intent: str  # 這題在挖什麼——會寫進 prompt 給 LLM
    kind: QuestionKind
    min_chars: int = 25
    follow_up: FollowUp | None = None
    optional: bool = False


QUESTIONS: tuple[Question, ...] = (
    Question(
        id="q1",
        text="最近一次你跟人意見不合，是什麼事？你最後怎麼處理？",
        intent="衝突處理模式、表達反對的方式（直接否定／提問質疑／沉默／轉移）",
        kind="episode",
        follow_up=FollowUp(
            min_chars=30,
            prompts=(
                "再多講一點——對方當下說了什麼？",
                "那件事後來怎麼收尾的？",
            ),
        ),
    ),
    Question(
        id="q2",
        text="講一件你當時很猶豫、最後還是做了的決定。",
        intent="決策門檻、風險偏好、事後如何合理化自己的選擇",
        kind="episode",
        follow_up=FollowUp(
            min_chars=30,
            prompts=(
                "你猶豫的點是什麼？",
                "如果重來一次，你還會做一樣的決定嗎？",
            ),
        ),
    ),
    Question(
        id="q3",
        text="有什麼事是別人常來找你的？朋友、同事、家人都算。",
        intent="自我認定的角色、被他人依賴的能力（他認為自己『有用』的地方）",
        kind="self_image",
        follow_up=FollowUp(
            min_chars=20,
            prompts=(
                "他們通常為什麼事來找你？",
                "最近一次是什麼情況？",
            ),
        ),
    ),
    Question(
        id="q4",
        text="你受不了別人做什麼事？講一個具體的例子。",
        intent="底線與反模式、道德潔癖的觸發點",
        kind="boundary",
        follow_up=FollowUp(
            min_chars=25,
            prompts=(
                "實際發生過嗎？當時你什麼反應？",
                "為什麼這件事特別讓你受不了？",
            ),
        ),
    ),
    Question(
        id="q5",
        text="有沒有一件事，你嘴上說不在意，但其實很在意？",
        intent="內在矛盾與張力——這是人格深度最重要的來源，矛盾不統一",
        kind="value",
        follow_up=FollowUp(
            min_chars=20,
            prompts=(
                "那你通常會怎麼表現出來？",
                "有人看出來過嗎？",
            ),
        ),
    ),
    Question(
        id="q6",
        text="如果有人請你幫一個你不想幫的忙，你會怎麼回他？",
        intent="【高解析度】拒絕策略——說『不』的方式最能區分人格",
        kind="boundary",
        follow_up=FollowUp(
            min_chars=20,
            prompts=(
                "直接說不？還是找理由？還是已讀不回？",
                "有沒有讓你很難拒絕的人？",
            ),
        ),
    ),
    Question(
        id="q7",
        text="你最近一次覺得很爽是什麼時候？發生什麼事？",
        intent="獎勵機制、在意什麼、什麼讓他主動推進",
        kind="episode",
        follow_up=FollowUp(
            min_chars=25,
            prompts=(
                "當下第一個念頭是什麼？",
                "你跟誰講了這件事？",
            ),
        ),
    ),
    Question(
        id="q8",
        text="如果要你描述你自己，你最不想被貼上哪個標籤？",
        intent="自我防衛機制、想被如何看見（正面與負面的自我形象落差）",
        kind="self_image",
        follow_up=FollowUp(
            min_chars=20,
            prompts=(
                "為什麼這個標籤讓你反感？",
                "你覺得別人容易這樣誤會你嗎？",
            ),
        ),
    ),
)


def question_by_id(qid: str) -> Question:
    for q in QUESTIONS:
        if q.id == qid:
            return q
    raise KeyError(f"未知題號：{qid}")


def total() -> int:
    return len(QUESTIONS)


@dataclass
class Answer:
    """使用者對某一題的回答，含追問紀錄。"""

    qid: str
    text: str = ""
    follow_ups: list[str] = field(default_factory=list)
    skipped: bool = False

    @property
    def full_text(self) -> str:
        parts = [self.text.strip()]
        parts.extend(f"（追問補充）{f.strip()}" for f in self.follow_ups if f.strip())
        return "\n".join(p for p in parts if p)


@dataclass
class ResponseSheet:
    """一份完整問卷。"""

    answers: list[Answer] = field(default_factory=list)

    def add(self, answer: Answer) -> None:
        self.answers.append(answer)

    def get(self, qid: str) -> Answer | None:
        for a in self.answers:
            if a.qid == qid:
                return a
        return None

    def non_empty(self) -> list[Answer]:
        return [a for a in self.answers if not a.skipped and a.full_text]

    def coverage(self) -> dict[str, int]:
        """每題的字數，用來判斷素材豐薄。"""
        return {a.qid: len(a.full_text) for a in self.answers if not a.skipped}

    def total_chars(self) -> int:
        return sum(len(a.full_text) for a in self.non_empty())


def render_for_prompt(sheet: ResponseSheet) -> str:
    """把問卷渲染成餵給 LLM 的文字。每題附上 intent，讓抽取有的放矢。"""
    blocks: list[str] = []
    for q in QUESTIONS:
        a = sheet.get(q.id)
        if a is None or a.skipped or not a.full_text:
            blocks.append(
                f"### [{q.id}] {q.text}\n"
                f"（抽取意圖：{q.intent}）\n"
                f"**（未作答 / 原材料不足）**"
            )
            continue
        blocks.append(
            f"### [{q.id}] {q.text}\n"
            f"（抽取意圖：{q.intent}）\n"
            f'**回答：**\n"""\n{a.full_text}\n"""'
        )
    return "\n\n".join(blocks)
