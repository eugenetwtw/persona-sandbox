"""CLI：問卷 → 蒸餾 → 沙盒 → 日誌。

用法：
    python3 -m persona_sandbox providers          # 看有哪些 LLM provider 可用
    python3 -m persona_sandbox ask                # 跑問卷（互動）
    python3 -m persona_sandbox demo               # 用示範素材跑完整條管線（不需金鑰）
    python3 -m persona_sandbox distill --answers answers.json
    python3 -m persona_sandbox play --card out/cards/x.json --witnesses socrates,laozi
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .persona.engine import PersonaCard, apply_correction, distill, slugify
from .persona.provider import LLM, LLMError, available_providers, detect_provider
from .persona.questions import QUESTIONS, Answer, ResponseSheet, question_by_id
from .world.scenario import SCENARIO_BY_ID, SCENARIOS
from .world.sandbox import AdventureLog, Sandbox

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"

# --------------------------------------------------------------------------
# 示範素材：一個刻意有矛盾、有具體故事的虛構角色，用來驗證管線
# --------------------------------------------------------------------------

DEMO_SHEET: dict[str, str] = {
    "q1": (
        "上個月開會的時候，我跟主管對於要不要接一個案子意見不一樣。他覺得先接下來再說，"
        "我覺得我們人力根本不夠。我當下沒有直接反駁他，我只是問了一句「那如果月中之前做不完，"
        "誰來跟客戶解釋？」。會議室就安靜了。後來那個案子還是接了，但主管自己去跟客戶談延期。"
        "我沒有再說什麼。"
    ),
    "q2": (
        "兩年前我辭掉一份薪水不錯的工作去接一個自由接案。我猶豫了大概三個月，"
        "每天都在算存款可以撐多久。最後讓我下決定的不是我算出來的那個數字，"
        "是我有一天發現我已經連續三個月週一早上不想起床。我就提離職了。"
        "現在收入比那時候少，但我沒有後悔過。"
    ),
    "q3": (
        "朋友要換工作、要跟另一半談分手、要跟家裡吵架，都會先來問我。"
        "我妹說我是「專門幫人把事情講清楚的那種人」。"
        "但我自己遇到事情的時候，我反而不太問人。"
    ),
    "q4": (
        "我很受不了人家答應了事情然後裝忘記。不是做不到喔，是裝忘記。"
        "有一次一個合作對象跟我約好週三給東西，週三沒給，週四我問他，他說「我以為是下週」。"
        "我當下沒有生氣，我直接把我們當初的對話截圖貼回去，然後說「那我們重排一下時間」"
        "——但我心裡已經把這個人歸類了。"
    ),
    "q5": (
        "我嘴上一直說我不在乎別人怎麼看我。但其實我在乎。"
        "我發文之前會修很多次，修到看起來像隨手寫的。"
        "有一次我朋友說「你真的很不在意別人眼光耶」，我笑了一下，但我記到現在。"
    ),
    "q6": (
        "我會先說「我想一下」，然後可能隔一天才回。"
        "如果真的很不想做，我會給一個很具體但看起來很合理的理由，不是說「我很忙」。"
        "但如果是我在意的人，我通常會直接做，然後心裡有點不爽。"
    ),
    "q7": (
        "上上週我把一個拖了半年的東西終於弄完。沒有誰稱讚我，我自己坐在電腦前笑出來。"
        "然後我打給我妹，跟她說「我弄完了」，她說「恭喜」，就這樣。但那一下我很爽。"
    ),
    "q8": (
        "我最不想被說「你很會做人」。"
        "聽到這個我會覺得好像我做的每件事都是在算計。"
        "我只是不想讓場面難看而已，這跟會不會做人是兩件事。"
    ),
}

DEMO_NAME = "示範玩家"


def _sheet_from_dict(data: dict[str, str]) -> ResponseSheet:
    sheet = ResponseSheet()
    for q in QUESTIONS:
        text = (data.get(q.id) or "").strip()
        sheet.add(Answer(qid=q.id, text=text, skipped=not text))
    return sheet


# --------------------------------------------------------------------------
# 指令
# --------------------------------------------------------------------------


def cmd_providers(_args: argparse.Namespace) -> int:
    from .persona.provider import ENV_FILE_LOADED, PROVIDER_BY_NAME, api_key_for, mask_key

    print("\n可用的 LLM provider（依偵測優先序）：\n")
    print(f"  {'名稱':<12} {'狀態':<10} {'環境變數':<24} 預設模型")
    print("  " + "-" * 74)
    for p in available_providers():
        status = "✅ 已設定" if p["configured"] else "—  未設定"
        print(
            f"  {p['name']:<12} {status:<10} {','.join(p['env_keys']):<24} {p['default_model']}"
        )
    print()
    if ENV_FILE_LOADED:
        print(f"  金鑰來源：{ENV_FILE_LOADED}")
        for p in available_providers():
            if p["configured"]:
                print(f"    {p['name']:<10} {mask_key(api_key_for(PROVIDER_BY_NAME[p['name']]))}")
        print()
    if not any(p["configured"] for p in available_providers()):
        print("  目前沒有任何 provider 設定金鑰。")
        print("  執行這個設定（金鑰只會存在本機 .env，不會上傳）：")
        print("    python3 -m persona_sandbox setup")
        print("  未設定時會以 dry-run 模式跑通管線（輸出結構骨架）。\n")
    return 0


def cmd_setup(_args: argparse.Namespace) -> int:
    """把 API 金鑰寫進本機 .env。金鑰不經過任何第三方。"""
    from .persona.provider import PROVIDERS, mask_key

    root = Path(__file__).resolve().parent.parent
    env_path = root / ".env"

    print("\n設定 LLM 金鑰")
    print("=" * 62)
    print("金鑰只會寫進本機檔案，且該檔案已被 .gitignore 排除。")
    print("建議用一把專用的、有額度上限的 key，不要用主帳號的金鑰。\n")
    print("可選 provider：")
    for i, spec in enumerate(PROVIDERS, 1):
        print(f"  [{i}] {spec.name:<12} {spec.default_model:<30} {spec.env_keys[0]}")
    print()

    try:
        choice = input("選一個（直接 Enter = 1 deepseek）：").strip() or "1"
    except (EOFError, KeyboardInterrupt):
        print("\n取消。")
        return 1
    try:
        spec = PROVIDERS[int(choice) - 1]
    except (ValueError, IndexError):
        print("❌ 無效的選擇。", file=sys.stderr)
        return 2

    try:
        key = input(f"貼上 {spec.name} 的 API key：").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n取消。")
        return 1
    if not key:
        print("❌ 沒有輸入金鑰。", file=sys.stderr)
        return 2
    print(f"   收到：{mask_key(key)}")

    existing: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip()

    var = spec.env_keys[0]
    existing[var] = key
    body = [
        "# Persona Sandbox — 本機金鑰。已被 .gitignore 排除，請勿提交。",
        f"# 設定時間：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    body += [f"{k}={v}" for k, v in existing.items()]
    env_path.write_text("\n".join(body) + "\n", encoding="utf-8")
    env_path.chmod(0o600)

    print(f"\n✅ 已寫入 {env_path}")
    print(f"   權限：{oct(env_path.stat().st_mode)[-3:]}（只有你能讀）")
    print(f"   環境變數：{var}")
    print("\n下一步：驗證金鑰能不能用")
    print("   python3 -m persona_sandbox doctor\n")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """用一次極小的呼叫驗證金鑰。不洩漏金鑰，只回報狀態。"""
    from .persona.provider import ENV_FILE_LOADED, api_key_for, mask_key

    llm = _make_llm(args)
    spec = llm.spec
    print("\n連線檢查")
    print("=" * 62)
    print(f"  .env                {ENV_FILE_LOADED or '（未找到）'}")
    if spec is None:
        print("  provider            無（沒有任何金鑰）")
        print("\n❌ 找不到可用的 provider。先跑：python3 -m persona_sandbox setup")
        return 1
    print(f"  provider            {spec.name}")
    print(f"  協定                {spec.protocol}")
    print(f"  模型                {llm.model}")
    print(f"  金鑰                {mask_key(api_key_for(spec))}")
    print("\n  ⟳ 送一個最小請求…")

    try:
        t0 = time.time()
        out = llm.complete(
            "你是一個測試端點。只回覆兩個字：可以", "請回覆「可以」。", temperature=0
        )
        dt = time.time() - t0
    except LLMError as e:
        print(f"\n❌ 失敗：{e}\n")
        print("   常見原因：金鑰打錯、額度用完、模型名稱不支援、網路不通。")
        return 1

    snippet = (out or "").strip().replace("\n", " ")[:60]
    print(f"\n✅ 成功（{dt:.1f} 秒）")
    print(f"   回覆：{snippet!r}")
    print("\n現在可以跑真實蒸餾了：")
    print("   python3 -m persona_sandbox demo      # 用示範素材")
    print("   python3 -m persona_sandbox ask       # 填自己的問卷\n")
    return 0


def cmd_db(args: argparse.Namespace) -> int:
    """把資料庫工具接進主 CLI：python3 -m persona_sandbox db stats"""
    from . import db as dbmod

    extra = list(getattr(args, "db_args", []) or [])
    return dbmod.main(extra)


def cmd_ask(args: argparse.Namespace) -> int:
    name = args.name or input("你的代號（隨便取，不會上傳）：").strip() or "匿名"
    print(f"\n你好，{name}。接下來 8 個問題，大概 3 分鐘。")
    print("講故事就好，不用想怎麼講才對。不想答的直接按 Enter 跳過。")
    print("（可以打 :skip 跳過，:quit 中途離開）\n")

    sheet = ResponseSheet()
    seed = 0
    for i, q in enumerate(QUESTIONS, 1):
        print(f"\n[{i}/{len(QUESTIONS)}] {q.text}")
        try:
            text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n（中斷）")
            break
        if text == ":quit":
            break
        if text == ":skip" or not text:
            sheet.add(Answer(qid=q.id, skipped=True))
            continue
        answer = Answer(qid=q.id, text=text)

        if q.follow_up and len(text) < q.follow_up.min_chars:
            print(f"  ↳ {q.follow_up.pick(seed)}")
            seed += 1
            try:
                extra = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                extra = ""
            if extra and extra not in (":skip", ":quit"):
                answer.follow_ups.append(extra)

        sheet.add(answer)

    out_path = OUT / "answers" / f"{slugify(name)}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "name": name,
                "answers": [
                    {"qid": a.qid, "text": a.text, "follow_ups": a.follow_ups, "skipped": a.skipped}
                    for a in sheet.answers
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n✅ 問卷已存到 {out_path}")

    # 同時寫進 SQLite（使用者 + session + 每個問答事件）
    try:
        from .store import Store

        store = Store()
        uid = store.get_or_create_user(name, slugify(name), note="CLI ask")
        sid = f"cli-q-{slugify(name)}-{int(time.time())}"
        store.start_session(sid, uid, "questionnaire", label=name,
                            meta={"source": "cli ask"})
        for a in sheet.answers:
            store.add_event(
                sid, "skip" if a.skipped else "answer", name,
                {"qid": a.qid, "text": a.text, "follow_ups": a.follow_ups,
                 "skipped": a.skipped},
            )
        print(f"   已寫入資料庫（session {sid}）")
        print(f"   下一步：python3 -m persona_sandbox distill --answers {out_path}")
    except Exception as e:  # noqa: BLE001
        print(f"   ⚠️  資料庫寫入失敗（JSON 仍已存檔）：{type(e).__name__}: {e}")

    print("\n── 素材體檢 ──")
    _print_coverage(sheet)
    return 0


def _print_coverage(sheet: ResponseSheet) -> None:
    from .persona.engine import coverage_report

    cov = coverage_report(sheet)
    print(f"  總字數：{cov['total_chars']}")
    print(f"  有效作答：{len(cov['answered'])} 題  {cov['answered']}")
    if cov["thin"]:
        print(f"  ⚠️  太短（<15 字）：{cov['thin']}")
    if cov["skipped"]:
        print(f"  · 略過：{cov['skipped']}")
    n = cov["total_chars"]
    if n < 150:
        print("  ⚠️  素材偏薄，蒸餾結果會大量標註「原材料不足」。這不是 bug，是誠實。")
    elif n < 400:
        print("  ✓ 素材中等，可以蒸出堪用的人格卡。")
    else:
        print("  ✓ 素材充足。")


def _load_answers(path: Path) -> tuple[str, ResponseSheet]:
    data = json.loads(path.read_text(encoding="utf-8"))
    name = data.get("name") or "匿名"
    sheet = ResponseSheet()
    for a in data.get("answers", []):
        sheet.add(
            Answer(
                qid=a["qid"],
                text=a.get("text", ""),
                follow_ups=a.get("follow_ups", []),
                skipped=a.get("skipped", False),
            )
        )
    return name, sheet


def _make_llm(args: argparse.Namespace) -> LLM:
    try:
        return LLM(provider=args.provider, model=args.model, dry_run=args.dry_run)
    except LLMError as e:
        print(f"❌ {e}", file=sys.stderr)
        raise SystemExit(2)


def _do_distill(llm: LLM, name: str, sheet: ResponseSheet) -> PersonaCard:
    if llm.dry_run:
        print("⚠️  沒有偵測到 API 金鑰 → dry-run 模式。")
        print("    管線會跑完，但輸出是結構骨架，不是真實蒸餾結果。")
        print("    設定 DEEPSEEK_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY 其一即可。\n")
    print(f"⟳ 蒸餾中（{llm.spec.name if llm.spec else 'dry-run'} / {llm.model}）…")
    card = distill(llm, sheet, name)
    path = card.save(OUT / "cards")
    print(f"✅ 人格卡已存到 {path}\n")

    # 寫進 SQLite：使用者 + 蒸餾結果 + 參數 + 證據
    try:
        from .store import Store

        store = Store(provider=(llm.spec.name if llm.spec else "dry-run"), model=llm.model)
        uid = store.get_or_create_user(name, slugify(name), note="CLI distill")
        did = store.add_distillation(
            uid, card.markdown,
            provider=(llm.spec.name if llm.spec else "dry-run"),
            model=llm.model,
            params={"temperature_analysis": 0.3, "temperature_build": 0.7,
                    "source": "cli distill", "dry_run": llm.dry_run},
            coverage=card.coverage,
            evidence=card.evidence_summary(),
            analysis=card.analysis,
        )
        print(f"   已寫入資料庫（distillation #{did}）")
    except Exception as e:  # noqa: BLE001
        print(f"   ⚠️  資料庫寫入失敗（卡片檔案仍已存檔）：{type(e).__name__}: {e}")

    ev = card.evidence_summary()
    print("── 證據組成 ──")
    print(
        f"  verbatim（原話）{ev['verbatim']}  ·  pattern（模式）{ev['pattern']}"
        f"  ·  impression（自述）{ev['impression']}  ·  原材料不足 {ev['insufficient']}"
    )
    return card


def cmd_demo(args: argparse.Namespace) -> int:
    """不需要金鑰也能跑完整條管線的端到端示範。"""
    print("=" * 74)
    print("  DEMO：用內建示範素材跑完整條管線")
    print("=" * 74)
    sheet = _sheet_from_dict(DEMO_SHEET)
    _print_coverage(sheet)
    print()

    llm = _make_llm(args)
    card = _do_distill(llm, DEMO_NAME, sheet)

    if args.dry_run or llm.dry_run:
        print("\n（dry-run 模式，跳過沙盒。設定金鑰後重跑即可看到完整對話。）")
        return 0

    print("\n" + "=" * 74)
    print("  沙盒：末日荒野避難所")
    print("=" * 74 + "\n")
    log = _run_sandbox(llm, card, args.witnesses, args.rounds, verbose=True)
    _save_log(log)
    return 0


def cmd_distill(args: argparse.Namespace) -> int:
    name, sheet = _load_answers(Path(args.answers))
    _print_coverage(sheet)
    print()
    llm = _make_llm(args)
    card = _do_distill(llm, name, sheet)
    print("\n" + card.markdown if args.show else "")
    return 0


def cmd_correct(args: argparse.Namespace) -> int:
    """本人修正：對自己的人格卡提出意見。"""
    card = PersonaCard.load(Path(args.card))
    sheet = ResponseSheet()
    if args.answers and Path(args.answers).exists():
        _, sheet = _load_answers(Path(args.answers))
    llm = _make_llm(args)
    print(f"目前人格卡：{card.name}（{len(card.corrections)} 筆修正紀錄）")
    print("輸入你的意見（例如「我不會直接說不，我通常是已讀不回」），空行結束。\n")
    lines: list[str] = []
    while True:
        try:
            line = input("> ")
        except (EOFError, KeyboardInterrupt):
            break
        if not line.strip():
            break
        lines.append(line)
    if not lines:
        print("沒有輸入，結束。")
        return 0
    feedback = "\n".join(lines)
    card, new, ack = apply_correction(llm, sheet, card, feedback)
    card.save(OUT / "cards")
    if ack:
        print(f"\n💬 {ack}")
    print(f"\n📝 新增 {len(new)} 筆修正：")
    for c in new:
        print(f"   [{c.get('type')}] {c.get('target_layer')}: {c.get('after', '')[:80]}")
    print(f"\n✅ 更新後的人格卡已存回 {OUT / 'cards'}")
    return 0


def _run_sandbox(
    llm: LLM,
    card: PersonaCard,
    witness_ids: list[str] | None,
    rounds: int | None,
    verbose: bool = True,
) -> AdventureLog:
    scenario = SCENARIOS[0]
    box = Sandbox(llm, scenario)

    def on_turn(turn: Any) -> None:
        if not verbose:
            return
        who = f"{turn.speaker}" + ("（你）" if turn.is_player else "")
        print(f"\n┌─ 第 {turn.round_no} 輪 · {who} " + "─" * max(0, 46 - len(who)))
        print("│ [內在獨白]")
        for line in turn.monologue.splitlines() or [""]:
            print(f"│   {line}")
        print("│ [對外發言]")
        for line in turn.public.splitlines() or [""]:
            print(f"│   {line}")
        print("└" + "─" * 56)

    return box.run(
        player_name=card.name,
        player_card=card.markdown,
        witness_ids=witness_ids,
        max_rounds=rounds,
        on_turn=on_turn,
    )


def cmd_play(args: argparse.Namespace) -> int:
    card = PersonaCard.load(Path(args.card))
    llm = _make_llm(args)
    print(f"玩家：{card.name}")
    print(f"場景：{SCENARIOS[0].name}\n")
    log = _run_sandbox(llm, card, args.witnesses, args.rounds, verbose=True)
    _save_log(log)
    return 0


def _save_log(log: AdventureLog) -> None:
    path = OUT / "logs" / f"{log.scenario_id}-{slugify(log.player_name)}.json"
    log.save(path)
    md = OUT / "logs" / f"{log.scenario_id}-{slugify(log.player_name)}.md"
    md.write_text(render_log_markdown(log), encoding="utf-8")
    print(f"\n✅ 日誌已存到：\n   {path}\n   {md}")


def render_log_markdown(log: AdventureLog) -> str:
    out = [
        f"# {log.scenario_name} — 冒險日誌",
        "",
        f"- 玩家：**{log.player_name}**",
        f"- 同場：{ '、'.join(log.witnesses) }",
        f"- 結束原因：{log.ended_reason or '—'}",
        f"- 生成時間：{log.created_at}",
        "",
        "---",
        "",
    ]
    for t in log.turns:
        who = f"**{t.speaker}**" + ("（你）" if t.is_player else "")
        out.append(f"### 第 {t.round_no} 輪 · {who}")
        out.append("")
        out.append("> **[內在獨白]**")
        for line in t.monologue.splitlines():
            out.append(f"> {line}")
        out.append(">")
        out.append("> **[對外發言]**")
        for line in t.public.splitlines():
            out.append(f"> {line}")
        out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="persona_sandbox",
        description="數位雙生人格探險平台 — MVP",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--provider", help="指定 LLM provider（deepseek/openai/anthropic/...）")
    ap.add_argument("--model", help="指定模型")
    ap.add_argument("--dry-run", action="store_true", help="不呼叫 LLM，只跑結構")

    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("providers", help="列出可用的 LLM provider").set_defaults(func=cmd_providers)

    sub.add_parser("setup", help="把 API 金鑰寫進本機 .env").set_defaults(func=cmd_setup)

    sub.add_parser("doctor", help="驗證金鑰能不能用").set_defaults(func=cmd_doctor)

    p_db = sub.add_parser("db", help="資料庫工具（stats/users/sessions/cards/migrate/export）")
    p_db.add_argument("db_args", nargs=argparse.REMAINDER,
                      help="子指令，例：stats / users / migrate / export <slug>")
    p_db.set_defaults(func=cmd_db)

    p_ask = sub.add_parser("ask", help="跑問卷")
    p_ask.add_argument("--name", help="你的代號")
    p_ask.set_defaults(func=cmd_ask)

    p_demo = sub.add_parser("demo", help="用示範素材跑完整條管線")
    p_demo.add_argument("--witnesses", help="同場角色，逗號分隔（socrates,laozi）")
    p_demo.add_argument("--rounds", type=int, help="輪數")
    p_demo.set_defaults(func=cmd_demo)

    p_dis = sub.add_parser("distill", help="從問卷答案蒸餾人格卡")
    p_dis.add_argument("--answers", required=True, help="answers.json 路徑")
    p_dis.add_argument("--show", action="store_true", help="直接印出人格卡")
    p_dis.set_defaults(func=cmd_distill)

    p_cor = sub.add_parser("correct", help="對已有人格卡提出修正")
    p_cor.add_argument("--card", required=True, help="人格卡 JSON 路徑")
    p_cor.add_argument("--answers", help="原始問卷答案（提供會更準）")
    p_cor.set_defaults(func=cmd_correct)

    p_play = sub.add_parser("play", help="把人格卡派進沙盒")
    p_play.add_argument("--card", required=True, help="人格卡 JSON 路徑")
    p_play.add_argument("--witnesses", help="同場角色，逗號分隔")
    p_play.add_argument("--rounds", type=int, help="輪數")
    p_play.set_defaults(func=cmd_play)

    args = ap.parse_args(argv)
    if getattr(args, "witnesses", None) and isinstance(args.witnesses, str):
        args.witnesses = [w.strip() for w in args.witnesses.split(",") if w.strip()]
    # 再讀一次 .env：可能在本次執行中被 setup 建立，或 cwd 不同
    from .persona.provider import load_env_file

    load_env_file()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
