"""蒸餾引擎：問卷 → 行為分析 → 人格卡 → （本人修正）。

管線刻意只有兩次 LLM 呼叫（分析 + 撰寫），不是 distilly 的多 agent 長鏈。
理由：這是 UGC 規模的產品，成本與延遲必須可控。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import prompts
from .provider import LLM
from .questions import QUESTIONS, ResponseSheet, render_for_prompt


@dataclass
class PersonaCard:
    name: str
    slug: str
    markdown: str
    analysis: dict[str, Any] = field(default_factory=dict)
    corrections: list[dict[str, Any]] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "slug": self.slug,
            "markdown": self.markdown,
            "analysis": self.analysis,
            "corrections": self.corrections,
            "coverage": self.coverage,
            "created_at": self.created_at,
            "model": self.model,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PersonaCard":
        return cls(
            name=d.get("name", ""),
            slug=d.get("slug", ""),
            markdown=d.get("markdown", ""),
            analysis=d.get("analysis", {}),
            corrections=d.get("corrections", []),
            coverage=d.get("coverage", {}),
            created_at=d.get("created_at", ""),
            model=d.get("model", ""),
        )

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{self.slug}.md").write_text(self.markdown, encoding="utf-8")
        (directory / f"{self.slug}.json").write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return directory / f"{self.slug}.md"

    @classmethod
    def load(cls, json_path: Path) -> "PersonaCard":
        return cls.from_dict(json.loads(json_path.read_text(encoding="utf-8")))

    def evidence_summary(self) -> dict[str, int]:
        """統計分析裡的證據等級分布——用來判斷這份人格卡有多少實據。"""
        counts = {"verbatim": 0, "pattern": 0, "impression": 0, "insufficient": 0}

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                lvl = node.get("level")
                if isinstance(lvl, str):
                    if lvl in counts:
                        counts[lvl] += 1
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
            elif isinstance(node, str):
                if "原材料不足" in node:
                    counts["insufficient"] += 1

        walk(self.analysis)
        return counts


def slugify(name: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", (name or "").strip()).strip("-")
    return s.lower() or f"player-{int(time.time())}"


def coverage_report(sheet: ResponseSheet) -> dict[str, Any]:
    cov = sheet.coverage()
    answered = [q.id for q in QUESTIONS if q.id in cov and cov[q.id] >= 15]
    thin = [q.id for q in QUESTIONS if q.id in cov and cov[q.id] < 15]
    skipped = [q.id for q in QUESTIONS if q.id not in cov]
    return {
        "answered": answered,
        "thin": thin,
        "skipped": skipped,
        "total_chars": sheet.total_chars(),
    }


def analyze(llm: LLM, sheet: ResponseSheet, name: str) -> dict[str, Any]:
    """第一次呼叫：問卷素材 → 結構化行為分析（含證據追溯）。"""
    user = prompts.ANALYZER_USER.format(name=name, sheet=render_for_prompt(sheet))
    return llm.complete_json(prompts.ANALYZER_SYSTEM, user, temperature=0.3)


def build_card(llm: LLM, analysis: dict[str, Any], name: str) -> str:
    """第二次呼叫：行為分析 → 可演出的人格卡。"""
    user = prompts.BUILDER_USER.format(
        name=name, analysis=json.dumps(analysis, ensure_ascii=False, indent=2)
    )
    return llm.complete(prompts.BUILDER_SYSTEM, user, temperature=0.7).strip()


def distill(llm: LLM, sheet: ResponseSheet, name: str) -> PersonaCard:
    """完整蒸餾流程。"""
    slug = slugify(name)
    coverage = coverage_report(sheet)
    analysis = analyze(llm, sheet, name)
    markdown = build_card(llm, analysis, name)
    return PersonaCard(
        name=name,
        slug=slug,
        markdown=markdown,
        analysis=analysis,
        coverage=coverage,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        model=f"{llm.spec.name if llm.spec else 'dry-run'}:{llm.model}",
    )


def distill_from_material(
    llm: LLM,
    name: str,
    docs: list[Any],
    source_coverage: dict[str, Any] | None = None,
) -> PersonaCard:
    """從網路素材（部落格／社群／影片）蒸餾。與問卷路線共用同樣的兩段式管線。"""
    from ..sources import render_docs_for_prompt

    usable = [d for d in docs if getattr(d, "usable", False)]
    if not usable:
        raise ValueError("沒有可用的素材，無法蒸餾")

    material = render_docs_for_prompt(usable)
    user = prompts.MATERIAL_ANALYZER_USER.format(
        name=name,
        n_docs=len(usable),
        total_chars=sum(d.chars for d in usable),
        docs=material,
    )
    analysis = llm.complete_json(prompts.MATERIAL_ANALYZER_SYSTEM, user, temperature=0.3)

    build_user = prompts.MATERIAL_BUILDER_USER.format(
        name=name, analysis=json.dumps(analysis, ensure_ascii=False, indent=2)
    )
    markdown = llm.complete(prompts.MATERIAL_BUILDER_SYSTEM, build_user, temperature=0.7).strip()

    return PersonaCard(
        name=name,
        slug=slugify(name),
        markdown=markdown,
        analysis=analysis,
        coverage=source_coverage or {},
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        model=f"{llm.spec.name if llm.spec else 'dry-run'}:{llm.model}",
    )


def distill_combined(
    llm: LLM,
    name: str,
    sheet: ResponseSheet,
    docs: list[Any],
    source_coverage: dict[str, Any] | None = None,
) -> PersonaCard:
    """合併路線：公開素材 + 問卷自述。

    素材是「他在網路上選擇留下的樣子」，問卷是「他此刻選擇說出來的樣子」。
    兩者放在一起，最容易看出落差——那正是人格最有意思的地方。
    """
    from ..sources import render_docs_for_prompt

    usable = [d for d in docs if getattr(d, "usable", False)]
    blocks: list[str] = []
    if usable:
        blocks.append("## 第一部分：網路公開素材\n\n" + render_docs_for_prompt(usable))
    qa = render_for_prompt(sheet)
    if sheet.non_empty():
        blocks.append(
            "## 第二部分：本人問卷自述\n"
            f"（共 {len(sheet.non_empty())} 題作答，"
            f"{sheet.total_chars()} 字）\n\n{qa}"
        )
    if not blocks:
        raise ValueError("沒有任何素材或問卷內容，無法蒸餾")

    material = "\n\n---\n\n".join(blocks)
    user = prompts.MATERIAL_ANALYZER_USER.format(
        name=name,
        n_docs=len(usable),
        total_chars=sum(d.chars for d in usable) + sheet.total_chars(),
        docs=material,
    ) + "\n\n注意：問卷自述屬於 `impression` 等級（他對自己的描述），" \
        "公開素材才是 `verbatim`／`pattern`。**當兩者衝突時，以公開素材的行為證據為主，" \
        "並在 `tensions` 裡把落差記下來，不要用自述蓋掉行為。**\n"

    analysis = llm.complete_json(prompts.MATERIAL_ANALYZER_SYSTEM, user, temperature=0.3)
    build_user = prompts.MATERIAL_BUILDER_USER.format(
        name=name, analysis=json.dumps(analysis, ensure_ascii=False, indent=2)
    )
    markdown = llm.complete(prompts.MATERIAL_BUILDER_SYSTEM, build_user, temperature=0.7).strip()

    cov = dict(source_coverage or {})
    cov["questionnaire_chars"] = sheet.total_chars()
    cov["questionnaire_answered"] = len(sheet.non_empty())
    return PersonaCard(
        name=name,
        slug=slugify(name),
        markdown=markdown,
        analysis=analysis,
        coverage=cov,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        model=f"{llm.spec.name if llm.spec else 'dry-run'}:{llm.model}",
    )


def apply_correction(
    llm: LLM, sheet: ResponseSheet, card: PersonaCard, feedback: str
) -> tuple[PersonaCard, list[dict[str, Any]], str]:
    """本人修正：把口語回饋轉成有型別的修正記錄，並更新人格卡。"""
    user = prompts.CORRECTION_USER.format(
        sheet=render_for_prompt(sheet), card=card.markdown, feedback=feedback
    )
    result = llm.complete_json(prompts.CORRECTION_SYSTEM, user, temperature=0.2)
    new_corrections = result.get("corrections") or []
    updated = result.get("updated_card") or card.markdown
    ack = result.get("acknowledgement") or ""

    if updated.strip() and updated.strip() != card.markdown.strip():
        card.markdown = updated
    card.corrections.extend(new_corrections)
    return card, new_corrections, ack
