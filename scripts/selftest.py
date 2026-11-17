#!/usr/bin/env python3
"""回归测试：FSRS 数学、节点库、门禁判据。

改完脚本先跑这个，再交付。

    python selftest.py

不碰真实学习数据，全部在临时目录里跑。
退出码 0 = 全过；1 = 有断言失败。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coach as co  # noqa: E402
import scheduler as sch  # noqa: E402
import stage  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        pass

PASS = 0
FAIL = []
CASE = [""]


def section(name: str) -> None:
    CASE[0] = name
    print(f"\n-- {name}")


def ok(cond, msg: str) -> None:
    global PASS
    if cond:
        PASS += 1
    else:
        FAIL.append(f"[{CASE[0]}] {msg}")


def eq(a, b, msg: str) -> None:
    ok(a == b, f"{msg}（期望 {b!r}，实际 {a!r}）")


def near(a, b, msg: str, tol: float = 1e-6) -> None:
    ok(abs(a - b) <= tol, f"{msg}（期望 ≈{b}，实际 {a}）")


# ---------- FSRS 数学 ----------

def test_math() -> None:
    section("FSRS 数学")

    eq(sch.forgetting_curve(0, 5), 1.0, "刚复习完的回忆概率是 1")
    near(sch.forgetting_curve(5, 5), 0.9, "间隔等于稳定度时留存约 90%", 1e-3)

    rs = [sch.forgetting_curve(t, 10) for t in (0, 1, 5, 30, 100, 365)]
    ok(all(rs[i] > rs[i + 1] for i in range(len(rs) - 1)), "回忆概率随时间单调下降")
    ok(0 < rs[-1] < 1, "一年后仍在 (0,1) 之间")

    eq(sch.init_stability(1), sch.W[0], "首次答错的 S0 = w[0]")
    eq(sch.init_stability(3), sch.W[2], "首次答对的 S0 = w[2]")
    ok(sch.init_stability(4) > sch.init_stability(3) > sch.init_stability(2),
       "S0 随评分递增")

    ok(sch.init_difficulty(1) > sch.init_difficulty(4), "答错的初始难度更高")
    ok(1 <= sch.init_difficulty(1) <= 10, "难度被 clamp 在 1-10")

    eq(sch.interval_for(1.0), 1, "S=1 时间隔约 1 天")
    ivs = [sch.interval_for(s) for s in (1, 5, 20, 100)]
    ok(all(ivs[i] < ivs[i + 1] for i in range(len(ivs) - 1)), "间隔随稳定度单调增长")
    eq(sch.interval_for(0.01), sch.MIN_INTERVAL, "间隔不低于下限")
    eq(sch.interval_for(999999), sch.MAX_INTERVAL, "间隔不超过上限")

    s, d = 3.7, 5.0
    up = sch.next_recall_stability(d, s, 0.9, 3)
    ok(up > s, "答对后稳定度增长")
    down = sch.next_forget_stability(d, s, 0.9)
    ok(down < s, "答错后稳定度衰减")
    ok(sch.next_recall_stability(d, s, 0.9, 4) > sch.next_recall_stability(d, s, 0.9, 3),
       "轻松 > 对 的增益")
    ok(sch.next_recall_stability(d, s, 0.9, 2) < sch.next_recall_stability(d, s, 0.9, 3),
       "勉强 < 对 的增益")
    ok(sch.next_recall_stability(d, s, 0.5, 3) > sch.next_recall_stability(d, s, 0.95, 3),
       "快忘光时答对，增益更大")

    eq(sch.parse_grade(1), 1, "parse_grade 数字 1")
    eq(sch.parse_grade(4), 4, "parse_grade 数字 4")
    eq(sch.parse_grade("对"), 3, "parse_grade 中文 对")
    eq(sch.parse_grade("勉强"), 2, "parse_grade 中文 勉强")
    eq(sch.parse_grade("错"), 1, "parse_grade 中文 错")
    eq(sch.parse_grade("轻松"), 4, "parse_grade 中文 轻松")
    eq(sch.parse_grade("很好"), None, "parse_grade 拒绝乱写")
    eq(sch.parse_grade(9), None, "parse_grade 拒绝越界")


# ---------- 概念表解析 ----------

def test_parse_concepts() -> None:
    section("概念表解析")
    block = """
| # | 概念 | 前置概念（可空） | 一句话定义 |
|---|------|-----------------|-----------|
| 1 | 词法作用域 |  | 变量在哪定义就属于哪个作用域 |
| 2 | 闭包 | 词法作用域 | 函数记住定义时的作用域 |
| 3 | 捕获时机 | 闭包、词法作用域 | 抓的是变量本身 |
"""
    got = sch.parse_concepts(block)
    eq(len(got), 3, "解析出 3 个概念")
    eq(got[0], ("词法作用域", []), "空前置解析为空列表")
    eq(got[1], ("闭包", ["词法作用域"]), "单前置")
    eq(got[2], ("捕获时机", ["闭包", "词法作用域"]), "顿号分隔的多前置")

    eq(sch.parse_concepts("| 名称 | 说明 |\n|---|---|\n| a | b |"), [],
       "表头没有「概念」列就不认")
    eq(sch.parse_concepts(""), [], "空输入返回空")
    eq(sch.parse_concepts("| # | 概念 | 前置 |\n|---|---|---|\n| 1 |  |  |"), [],
       "概念名为空的行跳过")


# ---------- 节点库 ----------

def test_store(tmp: Path) -> None:
    section("节点库")
    s = sch.NodeStore(tmp)
    eq(len(s.nodes), 0, "新库是空的")

    added, kept, lost = s.sync([("A", []), ("B", ["A"]), ("C", ["B"])])
    eq(len(added), 3, "首次同步新增 3 个")
    eq(kept, 0, "首次没有沿用")
    eq(lost, [], "首次没有失联")
    s.save()

    s2 = sch.NodeStore(tmp)
    eq(len(s2.nodes), 3, "落盘后重新加载仍是 3 个")
    a = s2.get("A")
    eq(a["S"], 0.0, "新节点稳定度为 0")
    eq(a["reps"], 0, "新节点未编码")

    # 第二次同步：不重复建，保留已有状态
    s2.grade("A", 3)
    s_after = s2.get("A")["S"]
    added2, kept2, _ = s2.sync([("A", []), ("B", ["A"]), ("C", ["B"]), ("D", ["A"])])
    eq(len(added2), 1, "只新增真正新的那个")
    eq(kept2, 3, "其余三个沿用")
    eq(s2.get("A")["S"], s_after, "再同步不动已有的稳定度")

    added3, _, lost3 = s2.sync([("A", []), ("B", ["A"])])
    eq(len(added3), 0, "没有新概念时不新增")
    eq(sorted(lost3), ["C", "D"], "消失的概念被报出来但不删除")
    eq(len(s2.nodes), 4, "失联节点仍留在库里")

    # 前置关系转 id
    b = s2.get("B")
    eq(b["prereq_ids"], [s2.get("A")["id"]], "前置名转成了节点 id")

    # 精确 / 唯一子串 / 歧义
    eq(s2.get("A")["name"], "A", "按名字精确命中")
    s2.sync([("A", []), ("AB", []), ("B", [])])
    eq(s2.get("A")["name"], "A", "有歧义时优先精确匹配")
    eq(s2.get("ZZ"), None, "查不到返回 None")

    section("节点库 · 评分与调度")
    clean = tmp / "clean"
    clean.mkdir(exist_ok=True)
    t = sch.NodeStore(clean)
    t.sync([("概念", [])])
    n = t.grade("概念", 3)
    eq(n["reps"], 1, "首次评分计入复习次数")
    ok(n["due"] is not None, "首次评分后有到期日")
    eq(n["lapses"], 0, "首次评分不算遗忘")

    # 同一天再评一次：几乎不增进长期记忆
    n = t.grade("概念", 3)
    ok(n["S"] < 100, "同日重复评分不会把稳定度推到天上")

    # 往后推 10 天答错
    later = dt.date.today() + dt.timedelta(days=10)
    s_before = n["S"]
    n = t.grade("概念", 1, on=later)
    eq(n["lapses"], 1, "答错记一次遗忘")
    ok(n["S"] < s_before, "答错后稳定度下降")
    due_day = dt.date.fromisoformat(n["due"])
    ok(due_day <= later + dt.timedelta(days=3), "答错后很快到期（几天内）")
    eq(t.due_list(due_day)[0]["name"], "概念", "到到期日出现在队列里")
    eq(t.due_list(due_day - dt.timedelta(days=1)), [], "没到期时不在队列里")

    section("节点库 · 队列与指标")
    clean2 = tmp / "clean2"
    clean2.mkdir(exist_ok=True)
    d2 = sch.NodeStore(clean2)
    d2.sync([("根", []), ("子", ["根"]), ("孙", ["子"])])
    eq([x["name"] for x in d2.frontier()], ["根"], "只有根节点前置满足")
    d2.grade("根", 3)
    eq([x["name"] for x in d2.frontier()], ["子"], "根编码后轮到子")
    eq(d2.due_list(dt.date.today()), [], "刚编码的还没到期")

    ret = d2.measured_retention()
    eq(ret["measured"], 0, "没有跨天复习时留存率是未测量，不是 0%")

    # 到期队列按"忘得最多"排序，不是按到期先后
    d3 = tmp / "clean3"
    d3.mkdir(exist_ok=True)
    e3 = sch.NodeStore(d3)
    e3.sync([("牢", []), ("半", []), ("忘", [])])
    base = dt.date(2026, 1, 1)
    e3.grade("牢", 4, on=base)   # S 大
    e3.grade("半", 3, on=base)
    e3.grade("忘", 1, on=base)   # S 小
    order = [n["name"] for n in e3.due_list(dt.date(2026, 6, 1))]
    eq(order, ["忘", "半", "牢"], "到期队列按当前遗忘程度排序，忘得最多的第一")

    e3.get("牢")["due"] = "2026-01-02"   # 到得更早但记得更牢
    e3.get("忘")["due"] = "2026-05-30"
    order = [n["name"] for n in e3.due_list(dt.date(2026, 6, 1))]
    eq(order[0], "忘", "逾期久不如忘得狠：仍按回忆概率排")

    # 造两条跨天事件
    e = d2.get("根")
    e["events"] = [
        {"at": "2026-01-01", "grade": 3, "conf": 4, "R": None, "S": 3.0, "interval": 3, "note": ""},
        {"at": "2026-01-06", "grade": 3, "conf": 4, "R": 0.9, "S": 8.0, "interval": 8, "note": ""},
        {"at": "2026-01-20", "grade": 1, "conf": 4, "R": 0.8, "S": 2.0, "interval": 2, "note": ""},
    ]
    e["last"] = "2026-01-20"
    e["due"] = "2026-01-22"
    ret = d2.measured_retention(dt.date(2026, 2, 1))
    eq(ret["buckets"]["d7"], [1, 1], "5 天间隔：1 次 1 成")
    eq(ret["buckets"]["d30"], [0, 1], "14 天间隔：1 次 0 成")
    eq(ret["unmeasured"], 1, "已到期未复习的计入 unmeasured")

    cal = d2.calibration()
    eq(cal["n"], 2, "校准只算有回忆概率的复习事件（首次编码没有可校的预测）")
    ok(cal["bias"] is not None, "算出校准偏差")


# ---------- 门禁判据 ----------

def write(p: Path, text: str) -> None:
    p.write_text(text, encoding="utf-8")


def test_gates(tmp: Path) -> None:
    section("门禁 · 第 0 阶定靶")
    g = tmp / "gate0"
    g.mkdir()
    ok(stage.check_0(g), "缺文件时报错")
    write(g / "00-plan.md", """
## 一、定靶
<!-- section:target -->

| 项 | 内容 |
|---|---|
| 目标动作（学完能做什么） | 能写出带闭包的事件监听器 |
| **成功标准（怎么算学会，要能被别人验证）** | 熟悉闭包 |
| 时间预算（每天 / 每周投入多少时间） | 每天 45 分钟 |
| **可交付成果（1 个）** | 一个 demo |
""")
    miss = stage.check_0(g)
    ok(any("太模糊" in m for m in miss), "「熟悉闭包」被判模糊")

    write(g / "00-plan.md", """
## 一、定靶
<!-- section:target -->

| 项 | 内容 |
|---|---|
| 目标动作（学完能做什么） | 能写出带闭包的事件监听器 |
| **成功标准（怎么算学会，要能被别人验证）** | 能独立写出 3 个闭包用例并说清 1 个泄漏场景 |
| 时间预算（每天 / 每周投入多少时间） | 每天 45 分钟 |
| **可交付成果（1 个）** | 一个 demo |
""")
    eq(stage.check_0(g), [], "带数字的具体标准放行")

    write(g / "00-plan.md", """
## 一、定靶
<!-- section:target -->

| 项 | 内容 |
|---|---|
| 目标动作（学完能做什么） | 能写出带闭包的事件监听器 |
| **成功标准（怎么算学会，要能被别人验证）** | 能跑通就行 |
| 时间预算（每天 / 每周投入多少时间） | 有空就学 |
| **可交付成果（1 个）** | 一个 demo |
""")
    miss = stage.check_0(g)
    ok(any("时间预算" in m for m in miss), "「有空就学」没有具体量，被拦")

    section("门禁 · 第 3 阶产物")
    g3 = tmp / "gate3"
    g3.mkdir()
    acts = """
## 一、动手清单
<!-- section:actions -->

| # | 对应概念 | 动作 | 级别 | 时长 | 产物（路径） | 验收标准 | 完成 |
|---|---|---|---|---|---|---|---|
| 1 | 闭包 | 写计数器 | 改 | 25m | out/a.js | 能跑 | [x] |
| 2 | 闭包 | 费曼输出 | 讲 | 20m |  | 讲通 | [ ] |
"""
    write(g3 / "03-practice.md", acts)
    miss = stage.check_3(g3)
    ok(any("不存在" in m for m in miss), "产物路径不存在被拦")
    ok(any("没勾" in m for m in miss), "未打勾被拦")

    (g3 / "out").mkdir()
    write(g3 / "out" / "a.js", "//")
    write(g3 / "03-practice.md", acts.replace("| 讲 | 20m |  | 讲通 | [ ] |",
                                              "| 讲 | 20m | out/a.js | 讲通 | [x] |"))
    eq(stage.check_3(g3), [], "产物存在且全部打勾后放行")

    # 回归：产物填了但验收标准空着，曾因列位左移误判为未打勾
    write(g3 / "03-practice.md", """
## 一、动手清单
<!-- section:actions -->

| # | 对应概念 | 动作 | 级别 | 时长 | 产物（路径） | 验收标准 | 完成 |
|---|---|---|---|---|---|---|---|
| 1 | 闭包 | 写计数器 | 改 | 25m | out/a.js |  | [x] |
| 2 | 整个主题 | 费曼输出 | 讲 | 20m | out/a.js |  | [x] |
""")
    eq(stage.check_3(g3), [], "验收标准空着不该让列位错乱（历史 bug 回归）")

    section("门禁 · 第 5 阶回测")
    g5 = tmp / "gate5"
    g5.mkdir()
    write(g5 / "04-knowledge-map.md", """
## 六、回测
<!-- section:retest -->

| 轮次 | # | 概念 | 题 | 信心 1-5 | 结果（对/勉强/错） | 备注 |
|---|---|---|---|---|---|---|
| 1 | 1 | A | q1 | 4 | 对 |  |
| 1 | 2 | B | q2 | 4 | 对 |  |
| 1 | 3 | C | q3 | 4 | 对 |  |
| 1 | 4 |  | q4 | 4 | 对 |  |
| 1 | 5 |  | q5 | 3 | 错 |  |
""")
    _h, items = stage.parse_retest(g5)
    eq(len(items), 5, "解析出 5 题")
    eq([i["grade"] for i in items], [3, 3, 3, 3, 1], "对→3、错→1")
    eq(items[0]["conf"], 4, "解析信心")
    miss = stage.check_5(g5)
    ok(any("概念」列" in m for m in miss), "有题没填概念名被拦")

    write(g5 / "04-knowledge-map.md", """
## 六、回测
<!-- section:retest -->

| 轮次 | # | 概念 | 题 | 信心 1-5 | 结果（对/勉强/错） | 备注 |
|---|---|---|---|---|---|---|
| 1 | 1 | A | q1 | 4 | 对 |  |
| 1 | 2 | B | q2 | 4 | 勉强 |  |
| 1 | 3 | C | q3 | 4 | 对 |  |
| 1 | 4 | D | q4 | 4 | 对 |  |
| 1 | 5 | E | q5 | 3 | 对 |  |
""")
    eq(stage.check_5(g5), [], "5 题全判、概念齐全、对 5 题 → 通过")
    _h, items = stage.parse_retest(g5)
    eq(items[1]["ok"], True, "「勉强」算过")
    eq(items[1]["grade"], 2, "「勉强」记为 grade 2")

    section("门禁 · 连错与轮次")
    write(g5 / "04-knowledge-map.md", """
## 六、回测
<!-- section:retest -->

| 轮次 | # | 概念 | 题 | 信心 1-5 | 结果（对/勉强/错） | 备注 |
|---|---|---|---|---|---|---|
| 1 | 1 | 闭包 | q1 | 4 | 错 |  |
| 1 | 2 | B | q2 | 4 | 对 |  |
| 1 | 3 | C | q3 | 4 | 对 |  |
| 1 | 4 | D | q4 | 4 | 对 |  |
| 1 | 5 | E | q5 | 4 | 对 |  |
| 2 | 1 | 闭包 | q1 | 4 | 错 |  |
| 2 | 2 | B | q2 | 4 | 对 |  |
| 2 | 3 | C | q3 | 4 | 对 |  |
| 2 | 4 | D | q4 | 4 | 对 |  |
| 2 | 5 | E | q5 | 4 | 对 |  |
""")
    miss = stage.check_5(g5)
    ok(any("连续 2 轮" in m for m in miss), "同概念连错 2 轮触发重定靶提示")
    ok(any("reset" in m for m in miss), "给出 reset 命令")


# ---------- 端到端 ----------

def test_e2e(tmp: Path) -> None:
    section("端到端 · 建会话到排复习")
    root = tmp / "e2e"
    root.mkdir()
    sess = root / "s"
    sess.mkdir()
    stage.render_fresh(sess, "测试主题", 1)
    stage.save_state(sess, {"topic": "测试主题", "stage": 0, "cycle": 1,
                            "history": [], "retries": []})

    # 直接造一份能过 2 阶的核心文件
    write(sess / "02-core.md", """
# 核心骨架

## 一、核心概念（5–9 个）
<!-- section:core-concepts -->

| # | 概念 | 前置概念 | 一句话定义 | 类比 | 反例 | 最小可练动作 |
|---|---|---|---|---|---|---|
| 1 | 甲 |  | d | a | e | m |
| 2 | 乙 | 甲 | d | a | e | m |
| 3 | 丙 | 乙 | d | a | e | m |

## 三、排除清单（这一阶段不学）
<!-- section:exclude -->

- 不学丁
""")
    store, added, _kept, _lost = stage.sync_nodes(sess, quiet=True)
    eq(added, 3, "sync_nodes 建了 3 个节点")
    eq(store.get("乙")["prereq"], ["甲"], "前置关系落库")

    # 造回测结论并收成收据
    write(sess / "04-knowledge-map.md", """
## 六、回测
<!-- section:retest -->

| 轮次 | # | 概念 | 题 | 信心 1-5 | 结果（对/勉强/错） | 备注 |
|---|---|---|---|---|---|---|
| 1 | 1 | 甲 | q1 | 4 | 对 |  |
| 1 | 2 | 乙 | q2 | 4 | 对 |  |
| 1 | 3 | 丙 | q3 | 4 | 错 |  |
| 1 | 4 | 甲 | q4 | 4 | 对 |  |
| 1 | 5 | 乙 | q5 | 4 | 对 |  |
""")
    n_added, _sk, n_unnamed = stage.harvest_receipts(sess)
    eq(n_added, 3, "5 题聚合成 3 条收据（甲、乙各被问两次，只算一份证据）")
    eq(n_unnamed, 0, "没有对不上概念的题")

    stub = sch.NodeStore(sess)
    eq(stub.get("甲")["reps"], 1, "同轮被问两次的概念只记一次复习")
    eq(stub.get("甲")["events"][-1]["grade"], 3, "两题都对 → 取该档")
    eq(len([e for e in stub.get("甲")["events"]]), 1, "甲只有一条事件，没有重复计数")

    n2, sk2, _u = stage.harvest_receipts(sess)
    eq(n2, 0, "重复 harvest 不重复计")
    eq(sk2, 3, "重复的 3 条被跳过")

    st = sch.NodeStore(sess)
    eq(st.get("丙")["lapses"], 0, "首次就答错不算 lapse（lapse 是记住过又忘了）")
    ok(st.get("丙")["S"] < st.get("甲")["S"], "答错的概念稳定度更低")
    ok(st.get("丙")["due"] < st.get("甲")["due"], "答错的概念排得更近")

    # reset 归档
    section("端到端 · 重定靶归档")
    stage.cmd_reset(sess, "测试连错")
    ok((sess / "archive" / "round1" / ".learning.json").exists(), "节点库随轮次归档")
    fresh = sch.NodeStore(sess)
    eq(len(fresh.nodes), 0, "新轮节点库清空")
    st_after = stage.load_state(sess)
    eq(st_after["cycle"], 2, "轮次 +1")
    eq(st_after["stage"], 0, "回到第 0 阶")


def test_answer_signal(tmp: Path) -> None:
    """回测答错必须进记忆模型；未来日期不能记分；前置阻塞要排序。"""
    section("修复 · 答错信号进记忆模型")
    sess = tmp / "sig"
    sess.mkdir()
    stage.render_fresh(sess, "信号", 1)
    stage.save_state(sess, {"topic": "信号", "stage": 5, "cycle": 1,
                            "history": [], "retries": []})

    past = dt.date.today() - dt.timedelta(days=10)
    st = sch.NodeStore(sess)
    st.sync([("甲", []), ("乙", ["甲"])])
    st.grade("甲", 3, on=past)
    st.grade("乙", 3, on=past)
    st.save()
    s_before = sch.NodeStore(sess).get("甲")["S"]

    write(sess / "03-practice.md", """
# 实操

## 一、动手清单
<!-- section:actions -->

| # | 概念 | 动作 | 抄改造 | 时间 | 产物 | 验收标准 | 完成 |
|---|---|---|---|---|---|---|---|
| 1 | 甲 | 做 | 造 | 10m | out/a.txt | 能跑 | [x] |
""")
    rc = stage.cmd_retry(sess, "甲")
    eq(rc, 0, "retry 正常返回")

    after = sch.NodeStore(sess)
    eq(after.get("甲")["lapses"], 1, "retry 记了一次遗忘（这是之前漏掉的信号）")
    ok(after.get("甲")["S"] < s_before, "retry 后稳定度下降")
    eq(after.get("乙")["lapses"], 0, "没答错的概念不受影响")

    section("修复 · 未来日期与凭证")
    rc = stage.cmd_grade(sess, "乙", "对", 4,
                         (dt.date.today() + dt.timedelta(days=30)).isoformat())
    eq(rc, 2, "未来日期被拒绝记分")
    eq(sch.NodeStore(sess).get("乙")["reps"], 1, "被拒后节点没被改")

    rc = stage.cmd_grade(sess, "乙", "对", 4, "", "他说：闭包是函数带着定义时的作用域走")
    eq(rc, 0, "带凭证的评分正常")
    ev = sch.NodeStore(sess).get("乙")["events"][-1]
    ok(ev["evidence"].startswith("他说"), "原话凭证写进了事件")

    section("修复 · 前置阻塞排序")
    d = sch.NodeStore(tmp / "order")
    d.sync([("地基", []), ("楼", ["地基"])])
    y = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    last = (dt.date.today() - dt.timedelta(days=2)).isoformat()
    d.data["nodes"][d.get("地基")["id"]].update(
        {"S": 100.0, "D": 3.0, "reps": 2, "last": last, "due": y})
    d.data["nodes"][d.get("楼")["id"]].update(
        {"S": 1.0, "D": 6.0, "reps": 2, "last": last, "due": y})
    d.save()
    q = d.due_list()
    eq([n["name"] for n in q], ["地基", "楼"], "前置也到期时，哪怕它更扎实也先补它")
    eq(q[1]["blocked_by"], ["地基"], "后置标出了阻塞它的前置")

    section("修复 · 跨会话汇总")
    root = tmp / "multi"
    root.mkdir()
    for nm, topic in (("a", "闭包"), ("b", "索引")):
        s = root / nm
        s.mkdir()
        stage.save_state(s, {"topic": topic, "stage": 2, "cycle": 1,
                             "history": [], "retries": []})
        write(s / "02-core.md", """
## 一、核心概念（5–9 个）
<!-- section:core-concepts -->

| # | 概念 | 前置概念 | 一句话定义 | 类比 | 反例 | 最小可练动作 |
|---|---|---|---|---|---|---|
| 1 | 核心 |  | d | a | e | m |
""")
        st, added, _k, _l = stage.sync_nodes(s, quiet=True)
        eq(added, 1, f"会话 {nm} 建了节点")
        eq(st.data.get("topic"), topic, "sync 时把主题写进节点库（跨会话汇总要用）")
        st.grade("核心", 3, on=dt.date.today() - dt.timedelta(days=20))
        st.save()

    rc = stage.cmd_due(root / "a", 0, True)
    eq(rc, 1, "跨会话有到期项时返回 1")
    rc = stage.cmd_due(root / "a", 0, False)
    eq(rc, 1, "单会话模式仍然只看自己")


def test_coach(tmp: Path) -> None:
    """取题引擎：掌握度按证据、难度落在目标带、题库不跨角度、同日不重复喂曲线。"""
    section("取题引擎 · 掌握度按证据算")
    eq(co.mastery([], None)["n"], 0, "没答过 n=0")
    ok(not co.mastery([], None)["mastered"], "没答过不算掌握")

    one = [{"angle": "定义", "grade": 3, "level": 3, "seconds": 30}]
    m1 = co.mastery(one, {"S": 10.0})
    ok(m1["score"] < 0.35, "只过一个角度，掌握度上不去")
    ok(not m1["mastered"], "一个角度不算掌握")

    four = [{"angle": a, "grade": 3, "level": 3, "seconds": 30, "at": "2026-11-01"}
            for a in ("定义", "边界", "反例", "操作")]
    m4 = co.mastery(four, {"S": 10.0})
    ok(m4["practiced"], "四角度 + 最高难度 3 → 当场会")
    ok(not m4["mastered"], "同一天全对不算掌握：还没跨过一天")
    ok("跨天" in " ".join(m4["gaps"]), "缺口里写明差跨天提取")
    ok(m4["score"] > m1["score"], "证据越多掌握度越高")

    # 换一天再成功提取一次，才叫「真的记得」
    cross = four[:3] + [{"angle": "操作", "grade": 3, "level": 3,
                         "seconds": 30, "at": "2026-11-05"}]
    m5 = co.mastery(cross, {"S": 10.0})
    ok(m5["delayed"], "两个不同日子成功提取 → 算跨天")
    ok(m5["mastered"], "当场会 + 跨天 + S≥7 → 已掌握")
    ok(m5["score"] > m4["score"], "跨天证据让分数上去")
    ok(not co.mastery(four[:3], {"S": 10.0})["practiced"], "三个角度不够")
    ok(not co.mastery(cross, {"S": 3.0})["mastered"], "稳定度不足不算掌握")
    low = [{"angle": a, "grade": 3, "level": 2, "seconds": 30, "at": "2026-11-01"}
           for a in ("定义", "边界", "反例", "操作")]
    ok(not co.mastery(low, {"S": 10.0})["practiced"], "最高通过难度只有 2 不算当场会")
    shaky = [{"angle": a, "grade": 1 if i == 3 else 3, "level": 3,
              "seconds": 30, "at": "2026-11-01"}
             for i, a in enumerate(("定义", "边界", "反例", "操作"))]
    ok(not co.mastery(shaky, {"S": 10.0})["practiced"], "有答错的不算当场会")

    grind = [{"angle": "定义", "grade": 3, "level": 4, "seconds": 20,
              "at": f"2026-11-{i % 28 + 1:02d}"} for i in range(30)]
    ok(not co.mastery(grind, {"S": 50.0})["mastered"],
       "同一角度跨 28 天连对 30 次仍不判掌握（角度覆盖不足）")
    ok(not co.mastery(grind, {"S": 50.0})["practiced"],
       "不数连击：连对 30 次换不来「当场会」")

    section("取题引擎 · 难度目标带")
    for sc in (0.35, 0.5, 0.65, 0.78, 0.85, 0.91, 0.96):
        lv, p, edge = co.target_level(sc)
        ok(1 <= lv <= 5, f"掌握度 {sc} 的难度落在 1-5")
        ok(edge or co.TARGET_P_LO - 0.01 <= p <= co.TARGET_P_HI + 0.01,
           f"掌握度 {sc} 的预计正确率 {p} 落在 {co.TARGET_P_LO}-{co.TARGET_P_HI}")
    eq(co.target_level(0.0)[0], 1, "零基础给最浅一档")
    ok(co.target_level(0.0)[2], "零基础标记到边界")
    eq(co.target_level(0.96)[1] > 0.7, True, "高掌握度也保持七成以上把握")
    ladder = [co.target_level(s)[0] for s in (0.2, 0.5, 0.7, 0.85, 0.96)]
    ok(all(ladder[i] <= ladder[i + 1] for i in range(len(ladder) - 1)),
       f"难度随掌握度单调不降（{ladder}）")

    # 能力值不含稳定度：今天刚练熟（S 很小）不该被判成只能做浅题
    eq(co.mastery(four, {"S": 3.0})["ability"], co.mastery(four, {"S": 10.0})["ability"],
       "能力值不受稳定度影响")
    ok(co.mastery(four, {"S": 3.0})["score"] < co.mastery(four, {"S": 10.0})["score"],
       "稳定度只影响掌握度，不影响能力值")

    # 难度地板：已经通过 4 档，就不该考回 2 档
    eq(co.target_level(0.82, 4)[0], 4, "已通过难度 4 时不降回去")
    eq(co.target_level(0.62, 4)[0], 2, "旧成绩已退化到五成以下时不硬撑高难度")
    ok(co.level_prob(0.8, 1) > co.level_prob(0.8, 5), "同一能力下，难度越高越可能做错")

    section("取题引擎 · 题库")
    bank = co.ItemBank(tmp / "bank")
    it, created = bank.add("甲", "边界", 2, "题面 A", ["要点一", "要点二"])
    ok(created and it["id"] == "q1", "首次入库拿到 q1")
    dup, created2 = bank.add("甲", "边界", 4, "题面  A ", ["x", "y"])
    ok(not created2 and dup["id"] == "q1", "题面重复不入库（忽略空白差异）")
    bank.add("甲", "定义", 3, "题面 B", ["p", "q"])
    eq(bank.pick("甲", "边界", 2)["id"], "q1", "同角度同难度命中")
    ok(bank.pick("甲", "反例", 2) is None, "库里没有反例题时不跨角度兜底")
    bank.mark_served("q1")
    eq(bank.items["q1"]["served"], 1, "出题次数 +1")
    eq(co.norm_angle("boundary"), "边界", "英文角度键可识别")
    eq(co.norm_angle("乱七八糟"), "", "非法角度返回空")

    section("取题引擎 · 五类取题桶")
    s1 = tmp / "cb-new"
    s1.mkdir()
    st = sch.NodeStore(s1)
    st.sync([("地基", []), ("墙", ["地基"]), ("顶", ["墙"])])
    st.save()
    cs = co.candidates(s1, sch.NodeStore(s1), [])
    eq([c["node"]["name"] for c in cs], ["地基"], "只有无前置的地基可学")
    eq(cs[0]["bucket"], "new", "未学概念进新概念桶")

    wrong = [{"concept": "地基", "angle": "定义", "grade": 1, "level": 1,
              "seconds": 40, "at": dt.date.today().isoformat()}]
    s2 = tmp / "cb-bad"
    s2.mkdir()
    st2 = sch.NodeStore(s2)
    st2.sync([("地基", [])])
    st2.grade("地基", 1, on=dt.date.today() - dt.timedelta(days=1))
    # 把到期日推到未来：这条测的是「答错要不要进补弱桶」，不是「到期该不该复习」
    st2.data["nodes"][st2.get("地基")["id"]].update(
        {"due": (dt.date.today() + dt.timedelta(days=5)).isoformat()})
    st2.save()
    cs = co.candidates(s2, sch.NodeStore(s2), wrong)
    eq(cs[0]["bucket"], "remediation", "答错后进补弱桶")
    eq(co.pick_angle(co.mastery(wrong, None), wrong, "remediation")[0], "定义",
       "补弱时先回到刚答错的那个角度")

    s3 = tmp / "cb-due"
    s3.mkdir()
    st3 = sch.NodeStore(s3)
    st3.sync([("甲", []), ("乙", ["甲"])])
    y = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    st3.data["nodes"][st3.get("甲")["id"]].update(
        {"S": 2.0, "D": 3.0, "reps": 2, "last": (dt.date.today() - dt.timedelta(days=4)).isoformat(),
         "due": y})
    st3.save()
    cs = co.candidates(s3, sch.NodeStore(s3), [])
    eq(cs[0]["bucket"], "review", "到期概念进复习桶且排最前")

    section("取题引擎 · 同日练习不重复喂曲线")
    s4 = tmp / "cb-rec"
    s4.mkdir()
    st4 = sch.NodeStore(s4)
    st4.sync([("甲", [])])
    st4.save()
    eq(co.cmd_record(s4, "不存在", "", "对", 0, 0, "", "定义", 1), 2, "概念不存在被拒")
    eq(co.cmd_record(s4, "甲", "", "对", 0, 0, "", "瞎写", 1), 2, "非法角度被拒")
    eq(co.cmd_record(s4, "甲", "", "对", 0, 0, "", "定义", 9), 2, "难度越界被拒")
    eq(co.cmd_record(s4, "甲", "", "对", 30, 4, "定义位置决定", "定义", 2), 0,
       "首次评分通过")
    node = sch.NodeStore(s4).get("甲")
    s_first, reps_first = node["S"], node["reps"]
    ok(s_first > 0, "首次评分后有稳定度")
    eq(co.cmd_record(s4, "甲", "", "对", 25, 4, "再讲一遍", "定义", 3), 0,
       "同日第二次评分通过")
    node2 = sch.NodeStore(s4).get("甲")
    near(node2["S"], s_first, "同一天第二次答题不动记忆曲线")
    eq(node2["reps"], reps_first, "同日练习不计入复习次数")
    ok(co.is_practice_only(node2), "同日已被记为练习")
    ok(not co.is_practice_only({**node2, "last": None}), "换个日子不算练习")

    rows = co.AttemptLog(s4).rows()
    eq(len(rows), 2, "两条答题都落进 .attempts.jsonl")
    ok(all(r["angle"] == "定义" for r in rows), "角度写进了答题记录")
    ok((s4.parent / "_coach" / "events.jsonl").exists(), "跨主题档案写了事件")

    empty = tmp / "cb-empty"
    empty.mkdir()
    eq(co.cmd_next(empty), 2, "没有概念节点时 next 返回 2")

    section("取题引擎 · 通过线与流畅度")
    # 「勉强」在 stage.py 里算过，在掌握度里也必须算过 —— 两套标准打架是最隐蔽的错
    soft = [{"angle": a, "grade": 2, "level": 3, "seconds": 30}
            for a in ("定义", "边界", "反例", "操作")]
    ms = co.mastery(soft, {"S": 10.0})
    eq(len(ms["angles"]), 4, "「勉强」也计入已过关的角度")
    ok(not ms["mastered"], "全是勉强，正确率不够，不判掌握")
    ok(ms["recent"] < 0.7, "勉强按 0.6 权重算，不按满分算")

    eq(co.fluency_adjusted_grade(3, 120, 40)[0], 2, "答对但慢过中位数 2.5 倍 → 降成勉强")
    ok(co.fluency_adjusted_grade(3, 120, 40)[1], "降级时标记出来")
    eq(co.fluency_adjusted_grade(3, 100, 40)[0], 3, "正好 2.5 倍不降级（阈值是「超过」）")
    eq(co.fluency_adjusted_grade(3, 50, 40)[0], 3, "正常速度不降级")
    eq(co.fluency_adjusted_grade(3, 500, None)[0], 3, "没有基线时不调（拿一道题定基准更糟）")
    eq(co.fluency_adjusted_grade(1, 500, 40)[0], 1, "答错的不因为慢而变好")

    # 5 条里 4 条 20 秒、1 条 300 秒：中位数 20，那一条该被降级
    slow = [{"angle": a, "grade": 3, "level": 3, "seconds": s}
            for a, s in (("定义", 20), ("边界", 20), ("反例", 20), ("操作", 20), ("关系", 300))]
    mslow = co.mastery(slow, {"S": 10.0})
    eq(mslow["slowed"], 1, "慢到 15 倍的那一条被标出来")
    ok(mslow["recent"] < 1.0, "慢作答拉低近期正确率")

    section("取题引擎 · 题面近似重复")
    ok(co.similar("循环结束时 i 是多少，回调看到的是几", "循环结束时 i 是几，回调看到的是多少"),
       "换词换顺序仍认作同一道题")
    ok(not co.similar("闭包捕获的是值还是引用", "装饰器会丢掉哪些元信息"),
       "不同题目不会被误判为重复")
    bank2 = co.ItemBank(tmp / "bank2")
    bank2.add("甲", "边界", 2, "循环结束时 i 是多少，回调看到的是几", ["a", "b"])
    got, created3 = bank2.add("甲", "边界", 3, "循环结束时 i 是几，回调看到的是多少", ["a", "b"])
    ok(not created3, "近似题面不入库")

    section("取题引擎 · 跨主题弱角度优先")
    root2 = tmp / "weak"
    (root2 / "_coach").mkdir(parents=True)
    for _ in range(3):
        co.append_jsonl(root2 / "_coach" / "events.jsonl",
                        {"at": "2026-11-01", "topic": "t", "concept": "x",
                         "angle": "边界", "level": 2, "grade": 1})
    for _ in range(3):
        co.append_jsonl(root2 / "_coach" / "events.jsonl",
                        {"at": "2026-11-02", "topic": "t", "concept": "y",
                         "angle": "操作", "level": 2, "grade": 4})
    eq(co.weak_angles(root2), ["边界"], "历史正确率为 0 的角度进弱项榜")
    a_pick, why = co.pick_angle(co.mastery([], None), [], "new", co.weak_angles(root2))
    eq(a_pick, "边界", "没测过的角度里，跨主题弱项先问")
    ok("最容易错" in why, "选它的理由写清楚了")

    section("取题引擎 · 难度模型校准")
    s9 = tmp / "cb-cal"
    s9.mkdir()
    st9 = sch.NodeStore(s9)
    st9.sync([("甲", [])])
    st9.save()
    eq(co.cmd_calibration(s9), 2, "样本不足时返回 2，不假装通过")
    rows = co.AttemptLog(s9).rows() or []
    for g in ("对", "错", "对", "对", "错", "对"):
        co.cmd_record(s9, "甲", "", g, 30, 3, "原话", "定义", 3)
    rows = co.AttemptLog(s9).rows()
    ok(rows and rows[0]["p_expected"] is None, "第一次接触不预测（没基线就不编数字）")
    ok(len([r for r in rows if r["p_expected"] is not None]) == 5,
       "之后每条都留下预测，供对账")
    eq(co.cmd_calibration(s9), 0, "样本够了就能出对账表")

    section("取题引擎 · 回测表同步")
    s5 = tmp / "cb-sync"
    s5.mkdir()
    eq(stage.render_fresh(s5, "同步", 1), [], "模板渲染成功")
    stage.save_state(s5, {"topic": "同步", "stage": 5, "cycle": 1,
                          "history": [], "retries": []})
    st5 = sch.NodeStore(s5)
    st5.sync([("甲", []), ("乙", []), ("丙", [])])
    st5.save()
    eq(co.cmd_sync(s5), 2, "没有第 5 阶答题记录时 sync 返回 2")

    for c, a, g in [("甲", "定义", "对"), ("乙", "边界", "对"), ("丙", "关系", "对"),
                    ("甲", "反例", "错"), ("乙", "迁移", "对")]:
        eq(co.cmd_record(s5, c, "", g, 40, 3, "原话", a, 3), 0, f"记分 {c}·{a}")
    eq(co.cmd_sync(s5), 0, "sync 写入成功")
    txt = (s5 / "04-knowledge-map.md").read_text(encoding="utf-8")
    ok("| 1 | 1 | 甲 |" in txt, "占位行被真实记录替换，不是追加在后面")
    ok("（定义角度" in txt, "没题面时用角度与难度兜底")
    lines = txt.splitlines()
    head, rows = co.retest_block(lines)
    eq(len(rows), 5, "回测表正好 5 行，没有残留空占位行")
    ok(all(co._cells(lines[i])[5] for i in rows), "5 行都填了结果，占位行没剩下")
    guard = [ln for ln in txt.splitlines() if "漏节点" in ln]
    ok(guard and guard[0].count("|") == 5, "四类错误自检表没被写坏")

    before = txt
    eq(co.cmd_sync(s5), 0, "重复 sync 仍返回 0")
    eq((s5 / "04-knowledge-map.md").read_text(encoding="utf-8"), before,
       "重复 sync 不改文件（幂等）")

    # 第 3 阶的练习记录不该混进回测
    stage.save_state(s5, {"topic": "同步", "stage": 3, "cycle": 1,
                          "history": [], "retries": []})
    co.cmd_record(s5, "丙", "", "对", 40, 3, "原话", "定义", 2)
    stage.save_state(s5, {"topic": "同步", "stage": 5, "cycle": 1,
                          "history": [], "retries": []})
    eq(co.cmd_sync(s5), 0, "第 3 阶记录不影响 sync")
    eq((s5 / "04-knowledge-map.md").read_text(encoding="utf-8"), before,
       "第 3 阶的练习没被写进回测表")

    section("取题引擎 · 跨主题档案")
    eq(co.cmd_profile(tmp / "prof-empty"), 2, "没记录时 profile 返回 2")
    root = tmp / "prof"
    (root / "_coach").mkdir(parents=True)
    events = [
        {"at": "2026-11-01", "topic": "t1", "concept": "甲", "angle": "边界",
         "level": 1, "grade": 1, "seconds": 40, "confidence": 5, "evidence": "原句"},
        {"at": "2026-11-02", "topic": "t1", "concept": "甲", "angle": "边界",
         "level": 1, "grade": 3, "seconds": 30, "confidence": 3, "evidence": "原句"},
        {"at": "2026-11-03", "topic": "t2", "concept": "乙", "angle": "迁移",
         "level": 3, "grade": 1, "seconds": 60, "confidence": 2},
    ]
    for e in events:
        co.append_jsonl(root / "_coach" / "events.jsonl", e)
    eq(co.cmd_profile(root), 0, "profile 汇总正常")
    ok(len(co.read_jsonl(root / "_coach" / "events.jsonl")) == 3, "事件只追加")


def test_coach_loop(tmp: Path) -> None:
    """教练闭环：两级掌握、无提示召回、今日入口、模型拟合、归一别名、双入口同账。"""
    section("教练闭环 · 回忆是独立证据，不占角度")
    s = tmp / "loop"
    s.mkdir()
    stage.render_fresh(s, "闭包", 1)
    stage.save_state(s, {"topic": "闭包", "stage": 5, "cycle": 1,
                         "history": [], "retries": []})
    st = sch.NodeStore(s)
    st.sync([("闭包", []), ("作用域", [])])
    st.save()

    for a, lv in [("定义", 3), ("边界", 3), ("反例", 3), ("操作", 4), ("关系", 3), ("迁移", 3)]:
        eq(co.cmd_record(s, "闭包", "", "对", 40, 4, "原话", a, lv), 0, f"记 {a}")
    m = co.mastery(co.concept_attempts(co.AttemptLog(s).rows(), "闭包"), sch.NodeStore(s).get("闭包"))
    ok(m["practiced"] and not m["mastered"], "同日六角度 → 当场会，但不算掌握")
    eq(len(m["angles"]), 6, "六个角度都算覆盖")

    eq(co.cmd_record(s, "闭包", "", "对", 90, 4, "他完整讲了一遍", "回忆", 0), 0, "回忆记分通过")
    row = co.AttemptLog(s).rows()[-1]
    eq(row["angle"], "回忆", "回忆单独记一类")
    eq(row["level"], 0, "回忆没有难度档")
    ok(row["p_expected"] is None, "回忆不产生难度预测")
    ok(row["p_recall"] is not None, "回忆会记下遗忘曲线给的预测")
    m2 = co.mastery(co.concept_attempts(co.AttemptLog(s).rows(), "闭包"),
                    sch.NodeStore(s).get("闭包"))
    eq(len(m2["angles"]), 6, "回忆不增加角度覆盖")
    ok("回忆" not in m2["angles"], "回忆不是第七个角度")

    section("教练闭环 · 跨过一天才算掌握")
    rows = co.AttemptLog(s).rows()
    yday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    rows2 = rows + [{"at": yday, "concept": "闭包", "angle": "定义", "level": 3,
                     "grade": 3, "seconds": 40}]
    flat = co.concept_attempts(rows2, "闭包")
    m3 = co.mastery(flat, sch.NodeStore(s).get("闭包"))
    ok(m3["delayed"], "有跨天成功提取 → delayed")
    ok(not m3["mastered"], "但稳定度不够，仍不算掌握")
    ok(any("稳定度" in g for g in m3["gaps"]), "缺口里点名稳定度")
    ok(co.mastery(flat, {"S": 10.0})["mastered"], "当场会 + 跨天 + S≥7 → 已掌握")

    same_day = [dict(r) for r in flat]
    for r in same_day:
        r["at"] = dt.date.today().isoformat()
    ok(not co.mastery(same_day, {"S": 10.0})["delayed"],
       "同一天答一万次也只有一天：不算跨天")

    section("教练闭环 · 无提示召回")
    eq(co.cmd_add(s, "闭包", "边界", 3, "这个场景里它会不会失效",
                  "说出失效条件;指出替代方案", ""), 0, "先入一道带标准的题")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = co.cmd_recall(s, "闭包", 1, True, False)
    eq(code, 1, "指定概念强制召回，退出码 1")
    payload = json.loads(buf.getvalue())
    eq(payload[0]["concept"], "闭包", "JSON 里给出概念")
    ok(60 <= payload[0]["limit_sec"] <= 180, "限时夹在 60–180 秒")
    eq(len(payload[0]["prompts"]), 4, "回忆四步：是什么/何时失效/举例/和谁有关")
    ok(payload[0]["rubric"] == [] or isinstance(payload[0]["rubric"], list), "评分标准字段存在")

    eq(co.cmd_recall(s, "没有这个", 1, False, False), 2, "概念不存在退出 2")
    # 未到期时默认队列为空 → 0。改节点必须用同一个 store 实例，否则 save 的是旧对象
    live = sch.NodeStore(s)
    live.get("闭包")["due"] = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    live.save()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        eq(co.cmd_recall(s, "", 1, False, False), 0, "没有到期时退出 0")
    ok("下次" in buf.getvalue(), "会告诉你下次是哪天")

    live = sch.NodeStore(s)
    live.get("闭包")["due"] = yday
    live.save()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = co.cmd_recall(s, "", 1, False, False)
    eq(code, 1, "到期时退出 1")
    ok("不看书" in buf.getvalue() and "限时" in buf.getvalue(), "召回说明写清了规则与限时")
    ok("评分标准" in buf.getvalue(), "默认提示题库有标准但先不给看")

    section("教练闭环 · 今日只给一个动作")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = co.cmd_today(s)
    out = buf.getvalue()
    eq(code, 1, "有到期时退出 1")
    ok("现在做这一件" in out, "只给一个动作")
    ok("recall" in out, "指向召回命令")
    ok("连续第" in out, "显示连续天数")

    live = sch.NodeStore(s)
    live.get("闭包")["reps"] = 0
    live.get("闭包")["due"] = None
    live.save()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = co.cmd_today(s)
    eq(code, 1, "有可开新概念时也退出 1")
    ok("next" in buf.getvalue(), "指向取题命令")

    # 只剩「当场会但没到期」时，today 应该说「今天可以停」，而不是硬塞一个动作
    sx = tmp / "practice-only"
    sx.mkdir()
    stage.render_fresh(sx, "只差跨天", 1)
    stage.save_state(sx, {"topic": "只差跨天", "stage": 5, "cycle": 1,
                          "history": [], "retries": []})
    stx = sch.NodeStore(sx)
    stx.sync([("甲", [])])
    stx.save()
    for a in ("定义", "边界", "反例", "操作"):
        co.cmd_record(sx, "甲", "", "对", 40, 4, "原话", a, 3)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = co.cmd_today(sx)
    eq(code, 0, "只剩当场会、未到期时退出 0")
    ok("今天可以停" in buf.getvalue(), "明确告诉用户不用硬练")
    ok("下次召回" in buf.getvalue(), "给出下次召回日期")

    section("教练闭环 · 模型偏移可拟合")
    low = [(0.8, 1)] * 12 + [(0.8, 0)] * 8      # 实际 60%，低于预测
    d, after, base = co.fit_offset(low)
    ok(d < 0, f"实际比预测差 → 偏移为负（{d}）")
    ok(after <= base + 1e-9, "校准后 Brier 不差于校准前")
    high = [(0.5, 1)] * 18 + [(0.5, 0)] * 2     # 实际 90%，高于预测
    d2, _, _ = co.fit_offset(high)
    ok(d2 > 0, f"实际比预测好 → 偏移为正（{d2}）")
    near(co.fit_offset([])[0], 0.0, "没有样本不瞎调")

    s2 = tmp / "fit"
    s2.mkdir()
    stage.render_fresh(s2, "拟合", 1)
    stage.save_state(s2, {"topic": "拟合", "stage": 5, "cycle": 1,
                          "history": [], "retries": []})
    st2 = sch.NodeStore(s2)
    st2.sync([("甲", [])])
    st2.save()
    log = co.AttemptLog(s2)
    for i in range(24):
        log.add({"at": dt.date.today().isoformat(), "concept": "甲", "angle": "定义",
                 "level": 3, "grade": 4 if i % 3 == 0 else 1, "seconds": 30,
                 "p_expected": 0.8, "p_recall": None, "confidence": 4})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        eq(co.cmd_calibration(s2, apply=True), 0, "样本够时 --apply 写入成功")
    model = json.loads((tmp / "_coach" / "model.json").read_text(encoding="utf-8"))
    ok(model["difficulty_offset"] < 0, "实际比预测差 → 写进负偏移")
    ok(abs(co.model_offset(s2) - model["difficulty_offset"]) < 1e-9, "取题时读得到这个偏移")
    ok(co.level_prob(0.6, 3, -0.5) < co.level_prob(0.6, 3, 0.0), "负偏移让预计做对率下降")

    s3 = tmp / "fit-small"
    s3.mkdir()
    stage.render_fresh(s3, "少量", 1)
    stage.save_state(s3, {"topic": "少量", "stage": 5, "cycle": 1,
                          "history": [], "retries": []})
    st3 = sch.NodeStore(s3)
    st3.sync([("甲", [])])
    st3.save()
    for i in range(6):
        co.AttemptLog(s3).add({"at": dt.date.today().isoformat(), "concept": "甲",
                               "angle": "定义", "level": 3, "grade": 4 if i % 2 else 1,
                               "seconds": 30, "p_expected": 0.8})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        eq(co.cmd_calibration(s3, apply=True), 2, "样本不足时 --apply 拒绝写入")
    ok("没写" in buf.getvalue(), "拒绝时说明为什么没写")

    section("教练闭环 · 流畅度冷启动基线")
    root = tmp / "gb"
    (root / "_coach").mkdir(parents=True)
    for i in range(7):
        co.append_jsonl(root / "_coach" / "events.jsonl",
                        {"level": 3, "seconds": 60, "angle": "定义", "at": "2026-11-01"})
    eq(co.global_seconds(root), {}, "样本不足 8 条不建基线")
    co.append_jsonl(root / "_coach" / "events.jsonl",
                    {"level": 3, "seconds": 60, "angle": "定义", "at": "2026-11-01"})
    eq(co.global_seconds(root)[3], 60, "满 8 条才形成同难度基线")
    cold = [{"angle": "定义", "grade": 3, "level": 3, "seconds": 300, "at": "2026-11-01"}]
    eq(co.mastery(cold, {"S": 1.0})["slowed"], 0, "没有基线时不判流畅度")
    mcold = co.mastery(cold, {"S": 1.0}, {3: 60})
    eq(mcold["slowed"], 1, "有全局基线时，超 2.5 倍照样降级")
    eq(mcold["baseline_src"], "global", "标明用的是全局基线（弱于自己的）")
    warm = [{"angle": a, "grade": 3, "level": 3, "seconds": s, "at": "2026-11-01"}
            for a, s in (("定义", 20), ("边界", 20), ("反例", 20), ("操作", 20))]
    eq(co.mastery(warm, {"S": 1.0}, {3: 60})["baseline_src"], "self",
       "自己够 4 条就用自己的基线，不用全局的")

    section("教练闭环 · 概念名归一与别名")
    nm = tmp / "nm"
    nm.mkdir()
    stn = sch.NodeStore(nm)
    stn.sync([("闭包", []), ("闭包编译器", [])])
    stn.save()
    eq(stn.get("闭包")["name"], "闭包", "精确名优先")
    eq(stn.get("闭包编译器")["name"], "闭包编译器", "相似名不被误并")
    stn = sch.NodeStore(nm)
    stn.add_alias("闭包", ["JS 闭包"])
    stn.save()
    eq(sch.NodeStore(nm).get("js闭包")["name"], "闭包", "别名忽略大小写与空格命中")
    eq(sch.NodeStore(nm).get("ＪＳ 闭包")["name"], "闭包", "全角写法也认")
    eq(sch.norm_key("JS 闭包"), sch.norm_key("js闭包"), "归一后同一个键")
    stn = sch.NodeStore(nm)
    stn.sync([("闭包", []), ("闭包编译器", []), ("js闭包", [])])
    eq(len([n for n in stn.live() if n["name"] in ("闭包", "js闭包")]), 1,
       "归一后相同的概念不重复建节点")

    section("教练闭环 · 两个入口写同一份账")
    s4 = tmp / "two-ways"
    s4.mkdir()
    stage.render_fresh(s4, "同账", 1)
    stage.save_state(s4, {"topic": "同账", "stage": 6, "cycle": 1,
                          "history": [], "retries": []})
    st4 = sch.NodeStore(s4)
    st4.sync([("甲", [])])
    st4.save()
    eq(stage.cmd_grade(s4, "甲", "对", 4, "", "他说：就是那个东西"), 0, "stage.grade 正常")
    rows = co.AttemptLog(s4).rows()
    eq(len(rows), 1, "stage.grade 也写进答题日志")
    eq(rows[0]["angle"], "回忆", "外部入口按回忆记")
    ok(rows[0]["evidence"].startswith("他说"), "证据跟着一起写进去")
    ok(rows[0]["p_recall"] is None, "第一次接触没有留存预测（没基线就不编）")

    # 第二次跨天：应带上按旧 S 算的 R
    yday2 = (dt.date.today() - dt.timedelta(days=2)).isoformat()
    live4 = sch.NodeStore(s4)
    live4.get("甲")["last"] = yday2
    live4.get("甲")["reps"] = 1
    live4.save()
    with contextlib.redirect_stdout(io.StringIO()):
        stage.cmd_grade(s4, "甲", "对", 4, "", "他又讲了一遍")
    rows = co.AttemptLog(s4).rows()
    ok(rows[-1]["p_recall"] is not None, "第二次起留下留存预测，供对账")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="el-selftest-"))
    try:
        test_math()
        test_parse_concepts()
        test_store(tmp)
        test_gates(tmp)
        test_e2e(tmp)
        test_answer_signal(tmp)
        test_coach(tmp)
        test_coach_loop(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAIL:
        print(f"✗ {len(FAIL)} 项失败 / {PASS + len(FAIL)} 项")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print(f"✓ 全部通过：{PASS} 项断言")
    return 0


if __name__ == "__main__":
    sys.exit(main())
