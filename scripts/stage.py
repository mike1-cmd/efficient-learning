#!/usr/bin/env python3
"""六阶闭环阶段状态机：把"严格按顺序"从口头规则变成可校验的闸门。

用法:
    python stage.py init    <会话目录> --topic "主题"
    python stage.py status  <会话目录>                  # 我在哪一阶 + 今天该做什么
    python stage.py check   <会话目录>                  # 只校验，不推进
    python stage.py advance <会话目录>                  # 校验通过才推进一阶
    python stage.py advance <会话目录> --force "原因"    # 降级推进，强制留痕
    python stage.py retry   <会话目录> --concept "概念"  # 回测答错：退到第 3 阶只重做这一概念
    python stage.py reset   <会话目录> --reason "原因"   # 同概念连错 2 轮：归档旧轮，回第 0 阶重定靶
    python stage.py review  <会话目录>                  # 今天有没有到期复习

记忆调度（FSRS-4.5，见 scheduler.py）:
    python stage.py sync    <会话目录>                  # 从 02-core.md 概念表同步节点
    python stage.py nodes   <会话目录>                  # 列出每个概念的稳定度与到期日
    python stage.py due     <会话目录> [--cap 10]       # 到期队列，忘得最多的排前面
    python stage.py next    <会话目录>                  # 前置已满足、可学的下一个概念
    python stage.py grade   <会话目录> --concept "闭包" --grade 对 --confidence 4
    python stage.py stats   <会话目录>                  # 留存率 / 校准 / 遗忘榜

阶段:
    0 定靶 → 1 测 → 2 核心 → 3 实操 → 4 图谱 → 5 回测 → 6 闭环通过
    回测答错 → retry 退到 3（只做那一个概念）→ 4 → 5 重测
    同一概念连错 2 轮 → check 拦住 → reset 回 0

退出码: 0 通过（或无到期复习） / 1 未通过（或有复习待做） / 2 用法或文件错误
"""

import argparse
import datetime as dt
import json
import re
import shutil
import sys
import unicodedata
from pathlib import Path

# Windows 控制台默认 GBK，中文与 ●○ 会炸；先钉死 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scheduler as sch  # noqa: E402

STAGES = ["定靶", "测", "核心", "实操", "图谱", "回测"]
FILES = {
    0: "00-plan.md",
    1: "01-diagnose.md",
    2: "02-core.md",
    3: "03-practice.md",
    4: "04-knowledge-map.md",
    5: "04-knowledge-map.md",
}

# reset 时重建的五份文件，唯一事实来源仍是 assets/templates/
ALL_FILES = ["00-plan.md", "01-diagnose.md", "02-core.md", "03-practice.md", "04-knowledge-map.md"]
TEMPLATE_MAP = [
    ("learning-plan.md", "00-plan.md"),
    ("diagnose.md", "01-diagnose.md"),
    ("core.md", "02-core.md"),
    ("practice.md", "03-practice.md"),
    ("knowledge-map.md", "04-knowledge-map.md"),
]
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "assets" / "templates"

# 回测结果列的可接受写法
OK_MARKS = {"对", "✓", "√", "正确", "是", "通过"}
HARD_MARKS = {"勉强", "卡", "半对", "部分", "有点卡", "磕绊"}
BAD_MARKS = {"错", "✗", "×", "错误", "否", "没过"}
EMPTY_MARKS = {"", "______", "---", "…", "...", "-", "*", "待填", "TBD", "　", "/"}
INDEX_RE = re.compile(r"^(?:\d+|[①-⑳]|[一二三四五六七八九十]+)$")
SEP_RE = re.compile(r"\|[\s:\-|]+\|")
ANCHOR_RE = re.compile(r"^<!--\s*section:([a-z0-9\-]+)\s*-->$")

# 章节锚点：模板里写成 <!-- section:xxx -->，脚本优先按锚点找，
# 找不到才退回"标题严格相等"匹配（不再用"标题包含关键词"）。
SECTION_IDS = {
    "三栏结论": "conclusion",
    "核心概念": "core-concepts",
    "排除清单": "exclude",
    "动手清单": "actions",
    "四类错误自检": "self-check",
    "仍然模糊的点": "fuzzy",
    "回测": "retest",
    "复习计划": "review",
}

# 复习节奏（间隔重复）：节点、偏移天数、复习内容
REVIEW_NODES = [
    ("D+1", 1, "快速回测 3 题：打模糊点"),
    ("D+3", 3, "快速回测 3 题：打主关系"),
    ("D+7", 7, "快速回测 5 题：重走完整回测流程"),
    ("D+14", 14, "判断是否需要开新闭环"),
]

# status 给"今天该做什么"的一句话，按当前阶段取
NEXT_ACTIONS = {
    0: "填 00-plan.md 定靶四行：目标动作 / 成功标准（可验证）/ 时间预算 / 可交付成果",
    1: "先做 10–20 分钟真实小任务（禁查资料），再让 AI 出 8–12 题，最后填三栏：半懂、空白各至少 1 条",
    2: "把概念删到 5–9 个，每个补一句话定义 + 一个反例，再写清这阶段不学什么",
    3: "挑清单里第一个没勾的动作，做完把产物路径填进「产物」列，再打 [x]；最后补费曼输出",
    4: "自己画图谱（AI 不代笔），画完过四类错误自检，列 3 个仍然模糊的点",
    5: "连跑 5 次 `coach.py next` 按它给的概念+角度+难度出题，答完 `coach.py record`；5 题后 `coach.py sync` 写回测表，再 advance。答错跑 `stage.py retry --concept 概念名`",
}

VAGUE_STD = re.compile(
    r"差不多|大概|大致|基本|尽量|还行|了解|知道|熟悉|会用|看懂|会一点|看情况|心里有数|有点概念"
)


def is_vague_std(s: str) -> bool:
    """成功标准是否模糊：短句，或含含糊词且拿不出任何量化判据。"""
    s = (s or "").strip()
    if len(s) < 6:
        return True
    if VAGUE_STD.search(s) and not re.search(r"\d", s):
        return True
    return False


# 产物路径的粗识别：带分隔符或扩展名就当路径，必须真实存在
PATHISH = re.compile(r"[\\/]|\.[A-Za-z0-9]{1,5}$")


# ---------- 解析（纯函数，逐条显式规则，无模糊推断） ----------

def read(session: Path, name: str) -> str:
    p = session / name
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8", errors="replace")


def norm_heading(h: str) -> str:
    """标题归一化：去掉 # 号、序号（一、/1.）、尾部括号说明、尾部「· 主题」后缀。"""
    s = h.strip().lstrip("#").strip()
    s = re.sub(r"^[一二三四五六七八九十]+[、.]\s*", "", s)
    s = re.sub(r"^\d+[、.)]\s*", "", s)
    s = re.sub(r"[（(][^）)]*[）)]\s*$", "", s)
    s = re.sub(r"\s*[·｜|].*$", "", s)
    return s.strip(" 　:：*")


def _locate(lines: list, key: str, aliases=()) -> tuple | None:
    """定位章节正文的 [起, 止) 行号。锚点优先，标题严格相等兜底。"""
    aid = SECTION_IDS.get(key)
    if aid:
        want = f"<!-- section:{aid} -->"
        for i, ln in enumerate(lines):
            if ln.strip() == want:
                return i + 1, _body_end(lines, i + 1)
    names = {norm_heading(key)} | {norm_heading(a) for a in aliases}
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s.startswith("#"):
            continue
        if norm_heading(s) in names:
            return i + 1, _body_end(lines, i + 1)
    return None


def _body_end(lines: list, start: int) -> int:
    """正文到下一个标题/下一个锚点为止。"""
    for j in range(start, len(lines)):
        s = lines[j].strip()
        if s.startswith("#") or ANCHOR_RE.match(s):
            return j
    return len(lines)


def section(text: str, key: str, aliases=()) -> str:
    """取章节正文：锚点 <!-- section:id --> 优先，其次标题严格相等（不用包含匹配）。"""
    lines = text.splitlines()
    loc = _locate(lines, key, aliases)
    if loc is None:
        return ""
    return "\n".join(lines[loc[0]:loc[1]])


HEADER_FIRST = {"#", "项", "序号", "编号", "类别", "日期"}


def table_rows(sec: str) -> list:
    """表格数据行 → [[非空单元格...]]；表头与分隔行一律剔除。"""
    rows, last_was_data = [], False
    for ln in sec.splitlines():
        s = ln.strip()
        if not s.startswith("|"):
            last_was_data = False
            continue
        if SEP_RE.fullmatch(s):
            if last_was_data and rows:
                rows.pop()          # 分隔线上方那一行是表头，剔掉
            last_was_data = False
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        cells = [c for c in cells if c not in EMPTY_MARKS and not INDEX_RE.match(c)]
        if not cells or cells[0] in HEADER_FIRST:
            last_was_data = False
            continue
        rows.append(cells)
        last_was_data = True
    return rows


def table_matrix(sec: str) -> tuple:
    """保留列位的表格解析：(表头单元格, [数据行单元格...])。

    与 table_rows 的区别：不剔除空单元格、不剔序号列，所以能按列名取值。
    """
    lines = [ln.strip() for ln in sec.splitlines() if ln.strip().startswith("|")]
    if not lines:
        return [], []

    def cells(ln: str) -> list:
        return [c.strip() for c in ln.strip().strip("|").split("|")]

    header, start = [], 0
    if len(lines) >= 2 and SEP_RE.fullmatch(lines[1]):
        header, start = cells(lines[0]), 2
    rows = [cells(ln) for ln in lines[start:] if not SEP_RE.fullmatch(ln)]
    return header, rows


def col_index(header: list, *names: str) -> int | None:
    """按列名找列号；先精确后包含（列名带括号说明也能命中）。"""
    for n in names:
        for i, h in enumerate(header):
            if h == n:
                return i
    for n in names:
        for i, h in enumerate(header):
            if n in h:
                return i
    return None


def is_blank(cell: str) -> bool:
    c = (cell or "").strip()
    if c in EMPTY_MARKS:
        return True
    return set(c) <= set("-—– 　")


def content_lines(sec: str) -> list:
    """有实质内容的普通行（排除空行、纯符号行、纯序号行）。"""
    out = []
    for ln in sec.splitlines():
        s = ln.strip()
        if not s or s in EMPTY_MARKS:
            continue
        if re.fullmatch(r"[-*+]\s*", s) or re.fullmatch(r"\d+\.\s*", s):
            continue
        out.append(s)
    return out


def field_value(text: str, label: str) -> str:
    """取「标签：值」或表格「| 标签 | 值 |」的值；占位符不算填。"""
    for ln in text.splitlines():
        if label not in ln:
            continue
        if ln.lstrip().startswith(("#", ">")):
            continue
        if ln.strip().startswith("|"):
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            idx = next((i for i, c in enumerate(cells) if label in c), None)
            if idx is None:
                continue
            val = " ".join(c for c in cells[idx + 1:] if c not in EMPTY_MARKS).strip()
            if val and "______" not in val:
                return val
            continue
        tail = ln.split(label, 1)[1].replace("*", "").lstrip("：: ").strip()
        if "______" in tail:
            continue
        if tail:
            return tail
    return ""


# ---------- 各阶通过条件 ----------

def check_0(session: Path) -> list:
    """定靶：目标动作 / 成功标准（可验证）/ 时间预算 / 可交付成果，四项都不能空。"""
    t = read(session, FILES[0])
    if not t:
        return [f"缺少 {FILES[0]}，先跑 new_session.py 建目录"]
    miss = []
    if not field_value(t, "目标动作"):
        miss.append("00-plan.md 未填「目标动作」（要写成『学完能做什么』）")
    if not field_value(t, "可交付成果"):
        miss.append("00-plan.md 未填「可交付成果」")

    std = field_value(t, "成功标准")
    if not std:
        miss.append("00-plan.md 未填「成功标准」（怎么算学会了，要能被别人验证）")
    elif is_vague_std(std):
        miss.append(
            f"「成功标准」写的是「{std}」，太模糊没法验证；"
            "改成可验证的判据，例如「能独立跑通 X 并交付 Y」或带具体数字的句子"
        )

    budget = field_value(t, "时间预算")
    if not budget:
        miss.append("00-plan.md 未填「时间预算」（每天 / 每周投入多少时间）")
    elif not re.search(r"\d|分钟|小时|天|周|次|钟头", budget):
        miss.append(f"「时间预算」写的是「{budget}」，没有具体量；写成『每天 45 分钟』这类")
    return miss


def check_1(session: Path) -> list:
    """测：「半懂」和「空白」两栏必须各有至少一条，只填「已掌握」不算测过。"""
    t = read(session, FILES[1])
    if not t:
        return [f"缺少 {FILES[1]}"]
    miss = []
    if len(field_value(t, "任务")) < 4:
        miss.append("01-diagnose.md 的「真实任务」没写（探底任务必须落到文件里）")

    header, rows = table_matrix(section(t, "三栏结论", ("三栏判定", "诊断结论")))
    if not header:
        miss.append("01-diagnose.md 的「三栏结论」表没找到（表头被删了？照模板补回三列）")
    else:
        for name in ("半懂", "空白"):
            idx = col_index(header, name)
            if idx is None:
                miss.append(f"三栏结论表缺「{name}」这一列（三栏必须齐全）")
                continue
            filled = [r for r in rows if idx < len(r) and not is_blank(r[idx])]
            if not filled:
                if name == "半懂":
                    miss.append(
                        "「半懂」栏一条都没有 —— 找不到会用错的点，说明测得不够深，加题重测"
                    )
                else:
                    miss.append(
                        "「空白」栏一条都没有 —— 全都会就不用走这轮闭环，回去重新定靶"
                    )

    if not field_value(t, "一句话缺口"):
        miss.append("01-diagnose.md 未写「一句话缺口」")
    return miss


def check_2(session: Path) -> list:
    t = read(session, FILES[2])
    if not t:
        return [f"缺少 {FILES[2]}"]
    miss = []
    n = len(table_rows(section(t, "核心概念")))
    if n < 3:
        miss.append(f"02-core.md 核心概念只填了 {n} 个，至少要 3 个")
    if n > 9:
        miss.append(f"02-core.md 核心概念填了 {n} 个，超过 9 个说明没抓核心，必须删到 9 个以内")
    if not content_lines(section(t, "排除清单")):
        miss.append("02-core.md 的「排除清单」是空的（这一阶段明确不学什么）")
    return miss


def _artifact_missing(session: Path, cell: str) -> str | None:
    """产物列写的是路径时，校验它真的存在；返回第一个找不到的路径。

    只校验"看起来像路径"的 token（含分隔符或扩展名），
    像"口头讲给同事听"这种非文件产物不拦。
    """
    for token in re.split(r"[\s、,，;；]+", cell or ""):
        token = token.strip().strip("`'\"()（）[]")
        if not token or not PATHISH.search(token):
            continue
        p = Path(token).expanduser()
        if not p.is_absolute():
            p = session / token
        if not p.exists():
            return token
    return None


def check_3(session: Path) -> list:
    """实操：每个动作都要勾 [x] 且「产物」列非空；产物是路径就必须真实存在。

    注意用 table_matrix 而不是 table_rows：后者会剔掉空单元格，
    导致"产物填了但验收标准空着"时列位左移，误判成没打勾。
    """
    t = read(session, FILES[3])
    if not t:
        return [f"缺少 {FILES[3]}"]
    header, rows = table_matrix(section(t, "动手清单", ("动作清单", "实操清单")))
    if not rows:
        return ["03-practice.md 的动手清单是空的，每个概念至少一个动作"]

    idx_done = col_index(header, "完成")
    idx_out = col_index(header, "产物")
    if idx_done is None or idx_out is None:
        return ["03-practice.md 的动手清单缺「产物」或「完成」列（表头被改了？照模板补回）"]

    miss, unchecked, empty_out, ghost, feynman = [], [], [], [], []
    for i, r in enumerate(rows, 1):
        label = r[0].strip() if r and r[0].strip() else str(i)
        done = idx_done < len(r) and "[x]" in r[idx_done].lower()
        out = r[idx_out].strip() if idx_out < len(r) else ""
        if not done:
            unchecked.append(f"第 {label} 行")
        if is_blank(out):
            empty_out.append(f"第 {label} 行")
        else:
            bad = _artifact_missing(session, out)
            if bad:
                ghost.append(f"第 {label} 行的「{bad}」")
        if any("费曼" in c for c in r):
            feynman.append((label, done))

    if unchecked:
        miss.append(f"还有 {len(unchecked)} 个动作没勾 [x]：{'、'.join(unchecked)}")
    if empty_out:
        miss.append(
            f"「产物」列是空的：{'、'.join(empty_out)}"
            "（填文件路径或截图路径；打了勾但没产物，等于没做）"
        )
    if ghost:
        miss.append(
            f"这些产物路径不存在：{'、'.join(ghost)}"
            "——路径写错了，或者文件根本没生成。产物要能被打开，不能用描述代替"
        )
    if not feynman:
        miss.append("缺「费曼输出」这一行：用自己的话把整个主题讲一遍，录音或文字都算")
    elif not feynman[0][1]:
        miss.append("「费曼输出」这一行还没打勾：讲不出来的地方就是没懂的地方")
    return miss


def check_4(session: Path) -> list:
    t = read(session, FILES[4])
    if not t:
        return [f"缺少 {FILES[4]}"]
    miss = []
    checked = [r for r in table_rows(section(t, "四类错误自检"))
               if any(c.strip() in ("有", "无") for c in r)]
    if not checked:
        miss.append("04-knowledge-map.md 的「四类错误自检」有/无列一行都没填")
    amb = [l for l in section(t, "仍然模糊的点", ("仍然模糊", "仍然模糊的 3 个点")).splitlines()
           if re.match(r"^\s*\d+\.\s*\S", l)]
    if not amb:
        miss.append("04-knowledge-map.md 未列「仍然模糊的点」（至少 1 条，回测要用）")
    return miss


def parse_retest(session: Path) -> tuple:
    """回测表 → (表头, 题项列表)。

    题项 = {round, no, ok, grade, concept, conf}；ok 为 True/False/None（未判）。
    grade 映射到 FSRS 四档：对→3、勉强→2、错→1。轮次列缺失（旧会话）按第 1 轮算。
    """
    t = read(session, FILES[5])
    header, rows = table_matrix(section(t, "回测"))
    if not header:
        return [], []
    i_rnd = col_index(header, "轮次", "轮")
    i_no = col_index(header, "#", "序号", "编号")
    i_res = col_index(header, "结果")
    i_con = col_index(header, "概念")
    i_conf = col_index(header, "信心", "把握")
    if i_res is None:
        return header, []

    def cell(r: list, i: int | None) -> str:
        return r[i].strip() if i is not None and i < len(r) else ""

    items = []
    for r in rows:
        raw = cell(r, i_res)
        if raw in OK_MARKS:
            ok, g = True, 3
        elif raw in HARD_MARKS:
            ok, g = True, 2
        elif raw in BAD_MARKS:
            ok, g = False, 1
        else:
            ok, g = None, None
        rnd = 1
        m = re.search(r"\d+", cell(r, i_rnd))
        if m:
            rnd = int(m.group())
        conf = None
        mc = re.search(r"[1-5]", cell(r, i_conf))
        if mc:
            conf = int(mc.group())
        items.append({
            "round": rnd,
            "no": cell(r, i_no) or str(len(items) + 1),
            "ok": ok,
            "grade": g,
            "concept": re.sub(r"\s+", "", cell(r, i_con)),
            "conf": conf,
        })
    return header, items


def consecutive_wrong(items: list) -> list:
    """同一概念在相邻两个轮次都答错 → [(概念, [轮次...])]。"""
    by = {}
    for i in items:
        if i["ok"] is False and i["concept"]:
            by.setdefault(i["concept"], set()).add(i["round"])
    out = []
    for c, rounds in sorted(by.items()):
        rs = sorted(rounds)
        if any(r + 1 in rs for r in rs):
            out.append((c, rs))
    return out


def check_5(session: Path) -> list:
    """回测：最新一轮 ≥5 题且答对 ≥4；同概念连错 2 轮 → 强制回第 0 阶重定靶。"""
    header, items = parse_retest(session)
    if not header:
        return ["04-knowledge-map.md 的「回测」表没找到（表头被删了？照模板补回）"]
    if not items:
        return ["04-knowledge-map.md 的回测表缺「结果」列（表头被改了？照模板补回）"]

    judged = [i for i in items if i["ok"] is not None]
    if not judged:
        return ["回测表一题都没判：结果列填「对」或「错」，答对 ≥4 才算过"]

    miss = []
    latest = max(i["round"] for i in judged)
    cur = [i for i in judged if i["round"] == latest]
    ok = [i for i in cur if i["ok"]]
    wrong = [i for i in cur if not i["ok"]]

    if len(cur) < 5:
        miss.append(f"第 {latest} 轮只判了 {len(cur)} 题，一轮要 5 题（补到 5 题再判）")

    named = [i for i in cur if i["concept"]]
    if len(named) < 4:
        miss.append(
            f"第 {latest} 轮只有 {len(named)} 题填了「概念」列，至少要 4 题 —— "
            "没有概念名就没法给这个概念排复习，这几道题等于白测"
        )

    if len(ok) < 4:
        names = list(dict.fromkeys(
            i["concept"] or f"第 {i['no']} 题" for i in wrong))
        who = "、".join(names) or "（结果列没写概念名）"
        hint = "只补这一个" if len(names) == 1 else "只补这几个"
        miss.append(
            f"第 {latest} 轮答对 {len(ok)} 题，未达 4 题；"
            f"答错的是：{who} —— {hint}，跑 `stage.py retry {session.name} --concept \"概念名\"`"
        )

    stuck = consecutive_wrong(items)
    if stuck:
        detail = "；".join(f"「{c}」在第 {'、'.join(map(str, rs))} 轮都答错" for c, rs in stuck)
        miss.append(
            f"{detail} → 同一个概念连续 2 轮没补上，说明目标或方法定错了，不是再补一遍的事。"
            f"跑 `stage.py reset {session.name} --reason \"...\"` 回第 0 阶重新定靶"
        )
    return miss


CHECKS = {0: check_0, 1: check_1, 2: check_2, 3: check_3, 4: check_4, 5: check_5}


# ---------- 状态 ----------

def state_path(session: Path) -> Path:
    return session / ".stage.json"


def load_state(session: Path) -> dict:
    p = state_path(session)
    if not p.exists():
        return {"topic": "", "stage": 0, "cycle": 1, "history": [], "retries": []}
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"topic": "", "stage": 0, "cycle": 1, "history": [], "retries": []}
    st.setdefault("cycle", 1)
    st.setdefault("history", [])
    st.setdefault("retries", [])
    return st


def save_state(session: Path, st: dict) -> None:
    state_path(session).write_text(
        json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def bar(stage: int) -> str:
    return " ".join(f"{i}:{'●' if i <= stage else '○'}" for i in range(6))


def next_name(s: int) -> str:
    return STAGES[s + 1] if s + 1 < len(STAGES) else "闭环通过"


# ---------- 复习计划（间隔重复） ----------

def read_review(session: Path) -> list:
    """读 00-plan.md 复习计划表 → [{node,date,content,done}]；日期空或解析不了的行跳过。"""
    t = read(session, FILES[0])
    header, rows = table_matrix(section(t, "复习计划"))
    if not header or not rows:
        return []
    i_date = col_index(header, "日期")
    i_done = col_index(header, "完成")
    i_content = col_index(header, "复习内容", "内容")
    items = []
    for r in rows:
        if i_date is None or i_date >= len(r):
            continue
        raw = r[i_date].strip()
        try:
            d = dt.date.fromisoformat(raw)
        except ValueError:
            continue
        items.append({
            "node": r[0] if r else "",
            "date": d,
            "content": r[i_content] if i_content is not None and i_content < len(r) else "",
            "done": bool(i_done is not None and i_done < len(r) and "[x]" in r[i_done].lower()),
        })
    return items


def fill_review_plan(session: Path, base: dt.date, store=None) -> bool:
    """回测通过后写复习计划表。

    有节点库 → 按每个概念各自的 FSRS 到期日写（这才是真实复习队列，难概念密、易概念疏）；
    没有 → 退回固定 D+1/D+3/D+7/D+14，只当兜底提醒。
    """
    p = session / FILES[0]
    if not p.exists():
        return False
    store = store or sch.NodeStore(session)
    lines = p.read_text(encoding="utf-8").splitlines()

    body = ["", "| 节点 | 日期 | 复习内容 | 完成 | 结果 |", "|---|---|---|---|---|"]
    scheduled = sorted(
        (n for n in store.live() if n["reps"] > 0 and n.get("due")),
        key=lambda n: n["due"],
    )
    if scheduled:
        for n in scheduled:
            body.append(
                f"| {n['name']} | {n['due']} | 第 {n['reps'] + 1} 次复习（S={n['S']:.1f}d） | [ ] |  |"
            )
    else:
        for node, days, content in REVIEW_NODES:
            body.append(
                f"| {node} | {(base + dt.timedelta(days=days)).isoformat()} | {content} | [ ] |  |"
            )
    body.append("")

    loc = _locate(lines, "复习计划")
    if loc is None:
        lines += ["", "## 复习计划", "<!-- section:review -->"] + body
    else:
        lines = lines[:loc[0]] + body + lines[loc[1]:]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def fill_next_review_date(session: Path, base: dt.date, store=None) -> bool:
    """在 04-knowledge-map.md 记录下一次复习：取队列里最近到期的那个概念。"""
    p = session / FILES[5]
    if not p.exists():
        return False
    store = store or sch.NodeStore(session)
    sched = sorted(
        (n for n in store.live() if n["reps"] > 0 and n.get("due")),
        key=lambda n: n["due"],
    )
    if sched:
        line = (f"**下一轮复习日期**：{sched[0]['due']}"
                f"（「{sched[0]['name']}」，S={sched[0]['S']:.1f}d）")
    else:
        line = f"**下一轮复习日期**：{(base + dt.timedelta(days=1)).isoformat()}（D+1 快速回测 3 题，打模糊点）"
    lines = p.read_text(encoding="utf-8").splitlines()
    for i, ln in enumerate(lines):
        if "下一轮复习日期" in ln:
            lines[i] = line
            break
    else:
        lines += ["", line]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


# ---------- 命令 ----------

def cmd_status(session: Path) -> int:
    st = load_state(session)
    s = st["stage"]
    print(f"主题: {st.get('topic') or '(未命名)'}")
    if s >= 6:
        print(f"阶段: 闭环已跑完（第 {st.get('cycle', 1)} 轮）   {bar(5)}")
        print("说明: 闭环通过只代表「当场会」。要变成长期记忆，靠之后按遗忘曲线的召回。")
        print()
        print(review_hint(session))
        print("（完整的今日安排：`coach.py today " + str(session) + "`）")
        return 0
    print(f"当前: 第 {s} 阶 · {STAGES[s]}    {bar(s)}")
    print(f"下一阶: 第 {s + 1} 阶 · {next_name(s)}")
    miss = CHECKS[s](session)
    print("通过条件: " + ("已满足，可 advance" if not miss else "未满足（advance 会被拦）"))
    for m in miss:
        print(f"  - {m}")
    store = sch.NodeStore(session)
    due = store.due_list() if store.nodes else []
    if due:
        names = "、".join(n["name"] for n in due[:3])
        more = f" 等 {len(due)} 个" if len(due) > 3 else ""
        print(f"⚠ 今天还有到期复习：{names}{more} —— 先复习，再往下走")
    print()
    print(f"今天的下一步动作是：{NEXT_ACTIONS[s]}")
    return 0


def review_hint(session: Path) -> str:
    """闭环通过后的一句话：今天该复习什么，或者下次什么时候复习。"""
    store = sch.NodeStore(session)
    if store.nodes:
        today = dt.date.today()
        due = store.due_list(today)
        if due:
            head = (f"今天的下一步动作是：复习「{due[0]['name']}」"
                    f"（现在能想起的概率 {due[0]['R']:.0%}）")
            if len(due) > 1:
                head += f"，另有 {len(due) - 1} 个也到期"
            return head
        later = sorted(
            (n for n in store.live() if n["reps"] and n.get("due")),
            key=lambda n: n["due"],
        )
        if later:
            d = dt.date.fromisoformat(later[0]["due"])
            return (f"今天的下一步动作是：不用复习。下次「{later[0]['name']}」"
                    f"（{later[0]['due']}，还有 {(d - today).days} 天）")
        return "今天的下一步动作是：还有概念没编码，跑 `stage.py next` 看下一个该学什么"

    items = read_review(session)
    if not items:
        print("复习计划还没生成：本会话缺 00-plan.md 的「复习计划」表。")
        return "今天的下一步动作是：在 00-plan.md 补一张复习计划表（D+1 / D+3 / D+7 / D+14）"
    today = dt.date.today()
    due = [i for i in items if i["date"] <= today and not i["done"]]
    todo = [i for i in items if not i["done"] and i["date"] > today]
    if due:
        head = f"今天的下一步动作是：复习 {due[0]['node']} —— {due[0]['content']}"
        if len(due) > 1:
            head += f"（另有 {len(due) - 1} 项也到期了）"
        return head
    if todo:
        d = todo[0]
        return (f"今天的下一步动作是：不用复习。下次 {d['node']}（{d['date'].isoformat()}，"
                f"还有 {(d['date'] - today).days} 天）{d['content']}")
    return "今天的下一步动作是：复习已全部完成，判断是否开新一轮闭环"


def cmd_check(session: Path) -> int:
    st = load_state(session)
    s = st["stage"]
    if s >= 6:
        print("已闭环通过，无需校验。")
        return 0
    miss = CHECKS[s](session)
    if miss:
        print(f"✗ 第 {s} 阶（{STAGES[s]}）未达标，禁止进入第 {s + 1} 阶（{next_name(s)}）:")
        for m in miss:
            print(f"  - {m}")
        return 1
    print(f"✓ 第 {s} 阶（{STAGES[s]}）已达标，可进入第 {s + 1} 阶（{next_name(s)}）。")
    return 0


def cmd_advance(session: Path, force: str | None) -> int:
    st = load_state(session)
    s = st["stage"]
    if s >= 6:
        print("已闭环通过，不再推进。")
        return 0
    miss = CHECKS[s](session)
    if miss and not force:
        print(f"✗ 第 {s} 阶（{STAGES[s]}）未达标，禁止进入第 {s + 1} 阶（{next_name(s)}）:")
        for m in miss:
            print(f"  - {m}")
        print("\n把上面这些补上，再跑一次 advance。")
        return 1
    now = dt.datetime.now().isoformat(timespec="seconds")
    st["stage"] = s + 1
    st["history"].append({"from": s, "to": s + 1, "at": now,
                          "forced": bool(force), "reason": force or ""})
    save_state(session, st)
    if force:
        log = session / ".stage-log.md"
        if not log.exists():
            log.write_text("# 降级留痕\n\n", encoding="utf-8")
        with log.open("a", encoding="utf-8") as f:
            f.write(f"- {now} 跳过第{s}阶（{STAGES[s]}）→ 第{s+1}阶：{force}\n")
        print("⚠️ 已降级跳阶，留痕写入 .stage-log.md")
        print(f"产出首行必须写：⚠️ 已跳过第 {s} 阶（用户要求直给），本轮不计入闭环通过。")
    else:
        if st["stage"] >= len(STAGES):
            print(f"✓ 闭环通过：第 {s} 阶（{STAGES[s]}）已达标    {bar(5)}")
        else:
            print(f"✓ 进入第 {s + 1} 阶 · {next_name(s)}    {bar(st['stage'])}")

    # 第 2 阶通过 → 概念表刚定稿，把节点建起来
    if st["stage"] == 3 and not force:
        nstore, n_added, n_kept, n_lost = sync_nodes(session, quiet=True)
        if n_added or n_kept:
            print(f"✓ 概念节点已入库：{len(nstore.live())} 个（新增 {n_added}，沿用 {n_kept}）")
            if n_lost:
                print(f"  ! 不在概念表里的旧节点（保留历史，不删除）：{'、'.join(n_lost)}")

    if st["stage"] >= len(STAGES) and not force:
        base = dt.date.today()
        store = sch.NodeStore(session)
        if not store.nodes:
            store, _a, _k, _l = sync_nodes(session, store, quiet=True)
        r_added, r_skipped, r_unnamed = harvest_receipts(session, store)
        plan_ok = fill_review_plan(session, base, store)
        map_ok = fill_next_review_date(session, base, store)
        print()
        if r_added or r_skipped:
            print(f"✓ 回测收据已入库：新增 {r_added} 条"
                  + (f"，{r_skipped} 条已存在跳过" if r_skipped else ""))
        elif r_unnamed:
            print(f"! 回测结论没进复习队列：{r_unnamed} 道题对不上概念名")
        if plan_ok:
            sched = sorted(
                (n for n in store.live() if n["reps"] > 0 and n.get("due")),
                key=lambda n: n["due"],
            )
            if sched:
                print(f"✓ 复习已按每个概念的遗忘曲线排好：最近一个是「{sched[0]['name']}」{sched[0]['due']}")
            else:
                dates = "、".join(
                    f"{node} {(base + dt.timedelta(days=days)).isoformat()}"
                    for node, days, _ in REVIEW_NODES
                )
                print(f"✓ 复习计划已写入 00-plan.md：{dates}")
        else:
            print("! 没写进复习计划：00-plan.md 不存在")
        if map_ok:
            print("✓ 下一轮复习日期已写入 04-knowledge-map.md")
        print("  到日子跑 `stage.py review <会话目录>`，它会列出今天该复习哪几个概念。")

    for m in miss:
        print(f"  (遗留) {m}")
    return 0


# ---------- 回测答错的两条回路 ----------

def append_log(session: Path, line: str) -> None:
    log = session / ".stage-log.md"
    if not log.exists():
        log.write_text("# 降级与回路留痕\n\n", encoding="utf-8")
    with log.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def uncheck_action(session: Path, concept: str) -> list:
    """把 03-practice.md 中含该概念的动作行重新置为未完成，并清空产物列。

    只在「动手清单」章节内定位表格 —— 整文件扫第一个表头的做法，
    会在文件里多出别的表格时改错地方（卡点记录表就在同一个文件里）。

    返回 [(行标签, 原产物)]；没命中返回空列表。
    """
    p = session / FILES[3]
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8").splitlines()
    loc = _locate(lines, "动手清单", ("动作清单", "实操清单"))
    if loc is None:
        return []
    start, end = loc

    h = None
    for k in range(start, max(start, end - 1)):
        if lines[k].strip().startswith("|") and SEP_RE.fullmatch(lines[k + 1].strip()):
            h = k
            break
    if h is None:
        return []
    header = [c.strip() for c in lines[h].strip().strip("|").split("|")]
    i_out = col_index(header, "产物")
    i_done = col_index(header, "完成")
    if i_out is None or i_done is None:
        return []

    key = re.sub(r"\s+", "", concept)
    hit = []
    for k in range(h + 2, end):
        s = lines[k].strip()
        if not s.startswith("|"):
            break
        cells = [c.strip() for c in s.strip("|").split("|")]
        if key not in re.sub(r"\s+", "", " ".join(cells)):
            continue
        old = cells[i_out] if i_out < len(cells) else ""
        if i_done < len(cells):
            cells[i_done] = "[ ]"
        if i_out < len(cells):
            cells[i_out] = ""
        hit.append((cells[0], old))
        lines[k] = "| " + " | ".join(cells) + " |"
    if hit:
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return hit


def cmd_retry(session: Path, concept: str) -> int:
    """回测答错：退到第 3 阶，只重做含该概念的动作。"""
    concept = concept.strip()
    if not concept:
        print('要给一个概念名：--concept "闭包"')
        return 2
    st = load_state(session)
    if st["stage"] < 5:
        print(f"现在在第 {st['stage']} 阶（{STAGES[st['stage']]}），还没到回测，不用 retry。")
        return 2
    if st["stage"] > 5:
        print("已闭环通过。要补某个概念，直接做复习回测，不必退阶。")
        return 2

    hit = uncheck_action(session, concept)
    now = dt.datetime.now().isoformat(timespec="seconds")
    st["retries"].append({"concept": concept, "at": now, "rows": [h[0] for h in hit]})
    st["history"].append({"from": 5, "to": 3, "at": now, "retry": concept})
    st["stage"] = 3
    save_state(session, st)

    old = "；".join(f"第 {lbl} 行原产物 {o or '(空)'}" for lbl, o in hit) or "未匹配到动作行"
    append_log(session, f"- {now} retry「{concept}」→ 第 5 阶退回第 3 阶（{old}）")

    # 回测答错是这个技能能拿到的最强遗忘信号。只退阶不改记忆模型，
    # 等于承认"他忘了"却假装遗忘曲线没动过 —— 这里必须记一次 lapse。
    lapse_line = ""
    store = sch.NodeStore(session)
    if not store.nodes:
        store, _a, _k, _l = sync_nodes(session, store, quiet=True)
    target = store.get(concept) if store.nodes else None
    if target is None and store.nodes:
        cands = store.find_many(concept)
        target = cands[0] if len(cands) == 1 else None
    if target is not None:
        before_s = target["S"] if target["reps"] else 0.0
        target = store.grade(target["name"], 1, note=f"retry@{dt.date.today().isoformat()}")
        store.save()
        lapse_line = (f"  记忆模型已同步：{target['name']} S {before_s:.1f}d → "
                      f"{target['S']:.1f}d，遗忘 {target['lapses']} 次，"
                      f"下次复习 {target['due']}")

    print(f"✓ 已退回第 3 阶 · 实操    {bar(3)}")
    if lapse_line:
        print(lapse_line)
    else:
        print(f"  ! 节点库里没有「{concept}」，这次答错没进记忆模型。先跑 `stage.py sync`。")
    if hit:
        print(f"  已把 {len(hit)} 行动作置为未完成，并清空「产物」列：")
        for lbl, o in hit:
            print(f"    - 第 {lbl} 行（原产物：{o or '空'}）")
        print(f"  重做「{concept}」，把新产物路径填回去，再打 [x]。")
    else:
        print(f"  ! 03-practice.md 里没有含「{concept}」的动作行 —— 先补一行再重做。")
    print("  做完一路 advance：3 → 4 → 5，回到第 5 阶重测。")
    print("  记录已写入 .stage-log.md 与 .stage.json")
    return 0


def render_fresh(session: Path, topic: str, cycle: int) -> list:
    """用模板重建五份文件，返回缺失的模板名（非空表示没建成）。"""
    bad = []
    day = dt.date.today().isoformat()
    for tmpl, dest in TEMPLATE_MAP:
        src = TEMPLATE_DIR / tmpl
        if not src.is_file():
            bad.append(tmpl)
            continue
        text = (src.read_text(encoding="utf-8")
                   .replace("{主题}", topic)
                   .replace("{YYYY-MM-DD}", day)
                   .replace("{第几轮}", f"第 {cycle} 轮"))
        (session / dest).write_text(text, encoding="utf-8")
    return bad


def cmd_reset(session: Path, reason: str) -> int:
    """同概念连错 2 轮：归档当前轮全部文件，回第 0 阶重新定靶。"""
    st = load_state(session)
    old_stage = st["stage"]
    cycle = int(st.get("cycle", 1))
    arc = session / "archive" / f"round{cycle}"
    arc.mkdir(parents=True, exist_ok=True)
    moved = []
    for name in ALL_FILES:
        src = session / name
        if src.exists():
            shutil.copy2(src, arc / name)   # 先归档再重建，原件不丢
            moved.append(name)

    # 节点库跟着这一轮一起归档：新轮的记忆数据必须从零开始，
    # 否则上一轮的稳定度会污染这一轮的排程。
    lp = session / ".learning.json"
    if lp.exists():
        shutil.copy2(lp, arc / ".learning.json")
        moved.append(".learning.json")

    bad = render_fresh(session, st.get("topic", ""), cycle + 1)
    if bad:
        print(f"✗ 缺模板 {', '.join(bad)}，未重置（旧文件仍在 archive/round{cycle}/，未被覆盖）")
        return 2

    (session / ".learning.json").write_text(
        json.dumps({"version": 2, "topic": st.get("topic", ""),
                    "request_retention": sch.DEFAULT_RETENTION,
                    "nodes": {}, "reviews": 0}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    now = dt.datetime.now().isoformat(timespec="seconds")
    st["stage"] = 0
    st["cycle"] = cycle + 1
    st["history"].append({"from": old_stage, "to": 0, "at": now,
                          "reset": True, "reason": reason})
    save_state(session, st)
    append_log(
        session,
        f"- {now} reset（第 {cycle} 轮 → 第 {cycle + 1} 轮）：{reason or '未写原因'}"
        f"；已归档 {len(moved)} 个文件到 archive/round{cycle}/",
    )

    print(f"✓ 已回第 0 阶 · 定靶（第 {cycle + 1} 轮）    {bar(0)}")
    print(f"  旧轮已归档：archive/round{cycle}/（{len(moved)} 个文件），内容没丢")
    print("  五份文件已按模板重建，定靶必须重写：上一轮的成功标准或时间预算定错了，这次改掉。")
    print("  留痕：.stage-log.md")
    return 0


# ---------- 记忆调度（FSRS 收据与队列） ----------

def sync_nodes(session: Path, store=None, quiet: bool = False) -> tuple:
    """从 02-core.md 的概念表同步节点。返回 (store, 新增数, 保留数, 失联名)。"""
    text = read(session, FILES[2])
    concepts = sch.parse_concepts(section(text, "核心概念")) if text else []
    store = store or sch.NodeStore(session)
    if not store.data.get("topic"):
        store.data["topic"] = load_state(session).get("topic", "")
    if not concepts:
        if not quiet:
            print("02-core.md 里还没有概念表 —— 节点库建不起来。先过第 2 阶。")
        return store, 0, 0, []
    added, kept, lost = store.sync(concepts)
    store.save()
    if not quiet:
        print(f"✓ 节点库已同步：新增 {len(added)} 个，沿用 {kept} 个")
        if added:
            print("  " + "、".join(n["name"] for n in added))
        if lost:
            print(f"  ! 这些节点已不在概念表里（保留历史，不删除）：{'、'.join(lost)}")
    return store, len(added), kept, lost


def harvest_receipts(session: Path, store=None) -> tuple:
    """把回测表里已判定的题写成收据，按 轮次#题号 去重。

    这是"无收据不主张掌握"的落地点：只有进了节点库的评分才算数，
    markdown 表格里写个"对"但没有概念名，排不进复习队列。
    """
    _header, items = parse_retest(session)
    if not items:
        return 0, 0, 0
    store = store or sch.NodeStore(session)
    if not store.nodes:
        store, _a, _k, _l = sync_nodes(session, store, quiet=True)
    if not store.nodes:
        return 0, 0, len(items)

    added = skipped = unnamed = 0
    # 5 题里可能有两题打在同一个概念上。逐题记分等于同一份证据被数了两次，
    # 而且"一题对一题错"会被拆成两次评分、被平均成"勉强会"。按轮聚合成一条，取最低档。
    groups = {}
    for it in items:
        if it["ok"] is None:
            continue
        if not it["concept"]:
            unnamed += 1
            continue
        node = store.get(it["concept"])
        if node is None:
            unnamed += 1
            continue
        k = (it["round"], node["name"])
        if k not in groups:
            groups[k] = {"g": it["grade"], "conf": it["conf"], "nos": []}
        else:
            groups[k]["g"] = min(groups[k]["g"], it["grade"])
            if it["conf"] is not None:
                prev = groups[k]["conf"]
                groups[k]["conf"] = it["conf"] if prev is None else min(prev, it["conf"])
        groups[k]["nos"].append(str(it["no"]))

    for (rnd, name), g in groups.items():
        node = store.get(name)
        # 该轮只要已经收过（旧格式 r1#1 或新格式 r1#*），就整轮跳过，保证重跑幂等
        prefix = f"r{rnd}#"
        if any(str(e.get("note", "")).startswith(prefix) for e in node["events"]):
            skipped += 1
            continue
        store.grade(name, g["g"], g["conf"], note=f"r{rnd}#* {'/'.join(g['nos'])}")
        added += 1
    if added:
        store.save()
    return added, skipped, unnamed


def cmd_sync(session: Path) -> int:
    store, added, _kept, _lost = sync_nodes(session)
    if not store.nodes:
        return 2
    print(f"  共 {len(store.live())} 个概念节点，其中未编码 {sum(1 for n in store.live() if n['reps'] == 0)} 个")
    print("  下一步：`stage.py nodes` 看全局，或 `stage.py due` 看今天该复习什么")
    return 0


def _disp_width(s: str) -> int:
    """终端显示宽度：CJK 全角字符占 2 列。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def pad(s: str, width: int, right: bool = False) -> str:
    """按显示宽度补齐，中文列才对齐。"""
    s = str(s)
    gap = max(width - _disp_width(s), 0)
    return (" " * gap + s) if right else (s + " " * gap)


def cmd_nodes(session: Path) -> int:
    store = sch.NodeStore(session)
    if not store.nodes:
        print("节点库是空的。先填 02-core.md 的概念表，再跑 `stage.py sync`。")
        return 2
    today = dt.date.today()
    st = load_state(session)
    print(f"主题: {store.data.get('topic') or st.get('topic') or '(未命名)'}")
    print(f"概念节点 {len(store.live())} 个   目标留存率 {store.data.get('request_retention', 0.9):.0%}")
    print()
    print(pad("概念", 14) + pad("状态", 7) + pad("S(天)", 8, True)
          + pad("D", 6, True) + pad("复习", 5, True) + pad("遗忘", 5, True) + "  到期")
    print("-" * 50)
    for n in store.live():
        state = "未编码" if n["reps"] == 0 else "到期" if _due_soon(n, today) else "记住中"
        s = f"{n['S']:.1f}" if n["reps"] else "-"
        d = f"{n['D']:.1f}" if n["reps"] else "-"
        print(pad(n["name"], 14) + pad(state, 7) + pad(s, 8, True)
              + pad(d, 6, True) + pad(n["reps"], 5, True)
              + pad(n.get("lapses", 0), 5, True) + f"  {n.get('due') or '-'}")
    return 0


def _due_soon(node: dict, today: dt.date) -> bool:
    d = None
    try:
        d = dt.date.fromisoformat(node.get("due") or "")
    except ValueError:
        return False
    return d <= today


def _sibling_sessions(session: Path) -> list:
    """同父目录下其他带节点库的学习会话。"""
    parent = session.parent
    if not parent.is_dir():
        return []
    return [p for p in sorted(parent.iterdir())
            if p.is_dir() and p != session and (p / ".learning.json").is_file()]


def _due_all(session: Path, cap: int) -> int:
    """跨会话汇总。学三个主题就有三份独立的复习队列，没人会每天开三次终端。"""
    today = dt.date.today()
    rows, seen = [], []
    for sess in [session] + _sibling_sessions(session):
        store = sch.NodeStore(sess)
        if not store.nodes:
            continue
        seen.append(sess)
        topic = str(store.data.get("topic") or "").strip()
        if not topic:
            # 目录名形如 2026-11-14-闭包，去掉日期前缀才读得懂
            topic = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", sess.name) or sess.name
        for n in store.due_list(today):
            rows.append({"R": n["R"], "overdue": n["overdue"], "topic": topic,
                         "sess": sess, "node": n})

    if not rows:
        print(f"今天 {today.isoformat()}，{len(seen)} 个会话都没有到期的概念。")
        return 0

    rows.sort(key=lambda r: (r["R"], -r["overdue"]))
    total = len(rows)
    show = rows[:cap] if cap and cap < total else rows
    print(f"今天 {len({r['sess'] for r in rows})} 个会话共 {total} 个概念到期"
          f"（约 {sch.workload_minutes(total)} 分钟）"
          + (f"，先做 {len(show)} 个" if len(show) < total else "") + "：")
    print()
    for i, r in enumerate(show, 1):
        n = r["node"]
        print(f"  {i}. {pad(n['name'], 16)}{pad(r['topic'], 20)}"
              f"概率 {r['R']:.0%}   逾期 {r['overdue']} 天")
        if n.get("blocked_by"):
            print(f"     ↑ 前置 {'、'.join(n['blocked_by'])} 也到期了，先补它")
    print()
    print("答完按会话记分（一个概念只能记在它自己那个会话里）：")
    for sess in sorted({r["sess"] for r in rows}):
        print(f"  {sess}")
    print()
    print('  stage.py grade <会话目录> --concept "概念名" --grade 对 --evidence "他的原句"')
    return 1


def cmd_due(session: Path, cap: int, scan_all: bool = False) -> int:
    if scan_all:
        return _due_all(session, cap)
    store = sch.NodeStore(session)
    if not store.nodes:
        print("节点库是空的。先跑 `stage.py sync`。")
        return 2
    today = dt.date.today()
    due = store.due_list(today)
    if not due:
        later = sorted(
            (n for n in store.live() if n["reps"] and n.get("due")),
            key=lambda n: n["due"],
        )
        print(f"今天没有到期的概念。")
        if later:
            print(f"最近一个：{later[0]['name']}（{later[0]['due']}）")
        if any(n["reps"] == 0 for n in store.live()):
            print("还有未编码的概念，用 `stage.py next` 看下一个该学什么。")
        return 0

    total = len(due)
    if cap and cap < total:
        due = due[:cap]
    print(f"今天到期 {total} 个概念" + (f"，先做 {len(due)} 个" if len(due) < total else "")
          + f"，约 {sch.workload_minutes(len(due))} 分钟。忘得最多的排前面：")
    print()
    for i, n in enumerate(due, 1):
        print(f"  {i}. {n['name']}   现在能想起的概率 {n['R']:.0%}   逾期 {n['overdue']} 天")
    print()
    print("每答完一个当场记：`stage.py grade <会话> --concept \"概念名\" --grade 对 --confidence 4`")
    print("（--grade 收 错 / 勉强 / 对 / 轻松；信心 1-5 是答之前自己的把握）")
    return 1


def cmd_next(session: Path) -> int:
    store = sch.NodeStore(session)
    if not store.nodes:
        print("节点库是空的。先跑 `stage.py sync`。")
        return 2
    ready = store.frontier()
    if not ready:
        done = [n for n in store.live() if n["reps"] > 0]
        print("没有可学的下一个概念：要么全部编码过了，要么前置还没学完。")
        if done:
            print(f"已编码 {len(done)} 个，跑 `stage.py due` 看复习。")
        return 0
    print("前置都已满足、可以学的概念：")
    for n in ready[:5]:
        pre = "、".join(n.get("prereq", [])) or "无"
        print(f"  - {n['name']}（前置：{pre}）")
    print()
    print(f"下一个：{ready[0]['name']}")
    return 0


def cmd_grade(session: Path, concept: str, grade_raw: str,
              conf: int | None, date_s: str, evidence: str = "") -> int:
    store = sch.NodeStore(session)
    if not store.nodes:
        store, _a, _k, _l = sync_nodes(session, store, quiet=True)
    if not store.nodes:
        print("节点库是空的，也没有概念表可同步。先填 02-core.md。")
        return 2

    node = store.get(concept)
    if node is None:
        hits = store.find_many(concept)
        if len(hits) > 1:
            print(f"「{concept}」匹配到多个概念，写全名：{'、'.join(h['name'] for h in hits)}")
        else:
            print(f"节点库里没有「{concept}」。跑 `stage.py nodes` 看有哪些。")
        return 2

    g = sch.parse_grade(grade_raw)
    if g is None:
        print("--grade 收 1-4 或 错 / 勉强 / 对 / 轻松")
        return 2
    if conf is not None and not 1 <= conf <= 5:
        print("--confidence 是 1-5")
        return 2

    try:
        on = dt.date.fromisoformat(date_s) if date_s else dt.date.today()
    except ValueError:
        print("--date 要 YYYY-MM-DD")
        return 2
    # 未来日期会让 elapsed 归零、S 暴涨（一次误传能把 3.7 天推到 313 天）。
    # 复习只可能发生在今天或过去，未来的分数只能是假的。
    if on > dt.date.today():
        print(f"--date {on} 在未来。复习不能提前记分（今天是 {dt.date.today()}）。")
        return 2

    before_s, before_due = node["S"], node.get("due")
    pre = dict(node)          # store.grade 原地改 S/last，先留评分前的快照
    node = store.grade(node["name"], g, conf, on, evidence=evidence)
    store.save()
    # 同一份账要写两处：记忆曲线 + 答题日志。少写一处，报表和掌握度就看不见这次复习。
    try:
        import coach as co  # noqa: E402  （延迟导入：stage 不依赖 coach 也能跑）
        co.log_external_recall(session, pre, g, conf, evidence, on=on)
    except Exception as e:  # 日志写不进去不该影响记忆曲线已经落盘的事实
        print(f"  （答题日志没写成：{e}）")

    ev = node["events"][-1]
    print(f"✓ 已记收据：{node['name']} · {sch.GRADE_LABEL[g]}")
    if ev.get("R") is not None:
        print(f"  评分时的回忆概率估计 {ev['R']:.0%}")
    print(f"  稳定度 {before_s:.1f}d → {node['S']:.1f}d    下次复习 {node['due']}（{ev['interval']} 天后）")
    print(f"  累计复习 {node['reps']} 次，遗忘 {node.get('lapses', 0)} 次，难度 {node['D']:.1f}")
    if before_due and before_due != node["due"]:
        print(f"  （原计划 {before_due}，按这次表现重排了）")
    if g == 1:
        print("  答错 → 这个概念要回到第 3 阶重做，跑 `stage.py retry --concept \"概念名\"`")
    return 0


def cmd_stats(session: Path) -> int:
    store = sch.NodeStore(session)
    if not store.nodes:
        print("节点库是空的。先跑 `stage.py sync`。")
        return 2
    today = dt.date.today()
    live = store.live()
    due = store.due_list(today)

    print(f"=== {len(live)} 个概念 · 目标留存率 {store.data.get('request_retention', 0.9):.0%} ===")
    print(f"未编码 {sum(1 for n in live if n['reps'] == 0)}"
          f" · 在记 {sum(1 for n in live if n['reps'] and not _due_soon(n, today))}"
          f" · 今天到期 {len(due)}")
    if due:
        print(f"今日工作量约 {sch.workload_minutes(len(due))} 分钟")

    ret = store.measured_retention(today)
    print()
    print("--- 实测留存率（只报测到的） ---")
    labels = {"d7": "≤7 天", "d30": "8-30 天", "d90": ">30 天"}
    any_measured = False
    for k, (ok, total) in ret["buckets"].items():
        if total == 0:
            print(f"  {labels[k]:<9} 未测量")
            continue
        any_measured = True
        print(f"  {labels[k]:<9} {ok}/{total} = {ok / total:.0%}")
    if ret["unmeasured"]:
        print(f"  另有 {ret['unmeasured']} 个概念到期没复习 —— 算「未知」，不算「忘了」")
    if not any_measured:
        print("  还没有跨天的复习记录。留存率要等第一次复习之后才有数。")

    cal = store.calibration()
    print()
    print("--- 校准（信心 vs 实际） ---")
    if cal["n"] == 0:
        print("  还没有带信心的记录。回测时填「信心 1-5」才有这一项。")
    else:
        for b in cal["buckets"]:
            gap = b["hit"] - b["expect"]
            arrow = "高估" if gap < -0.15 else "低估" if gap > 0.15 else "准"
            print(f"  信心 {b['conf']}：{b['n']} 次，实际通过 {b['hit']:.0%}"
                  f"（预期 {b['expect']:.0%}）→ {arrow}")
        print(f"  整体偏差 {cal['bias']:+.2f}（正数=高估自己）· Brier {cal['brier']:.2f}（越小越准）")

    graded = [e for n in live for e in n.get("events", []) if e.get("grade") is not None]
    if graded:
        with_ev = sum(1 for e in graded if str(e.get("evidence", "")).strip())
        cov = with_ev / len(graded)
        print()
        print("--- 收据凭证 ---")
        print(f"  {with_ev}/{len(graded)} 条评分附了用户原话（{cov:.0%}）")
        if cov < 0.5:
            print("  低于一半 —— 说明判分主要靠印象，不靠证据。")
            print("  下次回测把用户原话记进 `--evidence \"他说的原句\"`。")

    board = store.lapse_board()
    if board:
        print()
        print("--- 反复遗忘榜（优先重做这些） ---")
        for name, lapses, reps, s in board:
            print(f"  {name}：忘 {lapses} 次 / 复习 {reps} 次，S={s:.1f}d")
    return 0


def _review_by_nodes(session: Path, store) -> int:
    """有节点库时，复习完全由 FSRS 队列决定，不再看固定间隔表。"""
    today = dt.date.today()
    due = store.due_list(today)
    if not due:
        later = sorted(
            (n for n in store.live() if n["reps"] and n.get("due")),
            key=lambda n: n["due"],
        )
        print(f"今天 {today.isoformat()} 没有到期的复习。")
        if later:
            n = later[0]
            days = (dt.date.fromisoformat(n["due"]) - today).days
            print(f"下次：{n['name']}（{n['due']}，还有 {days} 天，S={n['S']:.1f}d）")
        elif any(n["reps"] == 0 for n in store.live()):
            print("还有概念没编码完，用 `stage.py next` 看下一个该学什么。")
        return 0

    print(f"今天 {today.isoformat()} 有 {len(due)} 个概念到期"
          f"（约 {sch.workload_minutes(len(due))} 分钟）：")
    for i, n in enumerate(due, 1):
        print(f"  {i}. {n['name']}   现在能想起的概率 {n['R']:.0%}   逾期 {n['overdue']} 天")
        if n.get("blocked_by"):
            print(f"     ↑ 它的前置 {'、'.join(n['blocked_by'])} 也到期了 —— 先补前面那个再回来")
        if n.get("lapses"):
            print(f"     ！这个概念已经忘过 {n['lapses']} 次")
    print()
    print("一个一个来，别一次列全。无提示回忆的规则：不看书、不翻笔记、不给线索。")
    print("四步讲完再评分：是什么 → 什么时候失效 → 举个自己的例子 → 和谁有关。")
    print(f"  coach.py recall {session}          # 只出第一个，带限时和四步")
    print('  stage.py grade <会话> --concept "概念名" --grade 对 --confidence 4')
    print("答错的跑 `stage.py retry --concept \"概念名\"` 回第 3 阶只重做那一个。")
    return 1


def cmd_review(session: Path) -> int:
    """今天该复习什么。有节点库就走 FSRS 队列，没有才回退到 00-plan.md 的固定间隔表。"""
    store = sch.NodeStore(session)
    if store.nodes:
        return _review_by_nodes(session, store)

    items = read_review(session)
    if not items:
        print("复习计划还没生成：先过第 5 阶回测。"
              "advance 到闭环通过时，会按每个概念的遗忘曲线排好复习日期。")
        return 2
    today = dt.date.today()
    due = [i for i in items if i["date"] <= today and not i["done"]]
    todo = [i for i in items if not i["done"] and i["date"] > today]
    if due:
        print(f"今天 {today.isoformat()} 有 {len(due)} 项复习到期：")
        for i in due:
            print(f"  - {i['node']}（{i['date'].isoformat()}）{i['content']}")
        print("\n做完把「完成」列打 [x] 并填结果。连续答错的概念，回到第 3 阶只重做那一个。")
        return 1
    print(f"今天 {today.isoformat()} 没有到期的复习。")
    if todo:
        d = todo[0]
        print(f"下次：{d['node']}（{d['date'].isoformat()}，还有 {(d['date'] - today).days} 天）{d['content']}")
    else:
        print("复习已全部完成，判断是否开新一轮闭环。")
    return 0


def cmd_init(session: Path, topic: str) -> int:
    session.mkdir(parents=True, exist_ok=True)
    save_state(session, {"topic": topic, "stage": 0, "cycle": 1,
                         "history": [], "retries": []})
    store = sch.NodeStore(session)
    if not store.data.get("topic"):
        store.data["topic"] = topic
        store.save()
    print(f"✓ 状态机已初始化: 第 0 阶 · 定靶    {bar(0)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="六阶闭环阶段状态机 + FSRS 记忆调度")
    ap.add_argument("cmd", choices=[
        "init", "status", "check", "advance", "retry", "reset", "review",
        "sync", "nodes", "due", "next", "grade", "stats",
    ])
    ap.add_argument("session", help="学习会话目录")
    ap.add_argument("--topic", default="", help="init 时的主题")
    ap.add_argument("--force", default=None, help="降级推进的原因（会留痕）")
    ap.add_argument("--concept", default="", help="retry / grade 时的概念名或节点 id")
    ap.add_argument("--reason", default="", help="reset 时重定靶的原因（会留痕）")
    ap.add_argument("--grade", default="", help="grade 时的成绩：1-4 或 错/勉强/对/轻松")
    ap.add_argument("--confidence", type=int, default=None,
                    help="grade 时的信心 1-5（答之前自己的把握，用于校准）")
    ap.add_argument("--date", default="", help="grade 时的日期 YYYY-MM-DD，默认今天（不许填未来）")
    ap.add_argument("--evidence", default="",
                    help="grade 时用户的原话片段（收据凭证，没有就只是模型的自我印象）")
    ap.add_argument("--cap", type=int, default=0, help="due 时只列前 N 个")
    ap.add_argument("--all", action="store_true",
                    help="due 时汇总同目录下所有学习会话（学多个主题时用）")
    args = ap.parse_args()

    session = Path(args.session).expanduser().resolve()
    if not session.exists():
        print(f"目录不存在: {session}")
        return 2
    if args.cmd == "init":
        return cmd_init(session, args.topic)
    if args.cmd == "status":
        return cmd_status(session)
    if args.cmd == "check":
        return cmd_check(session)
    if args.cmd == "retry":
        return cmd_retry(session, args.concept)
    if args.cmd == "reset":
        return cmd_reset(session, args.reason)
    if args.cmd == "review":
        return cmd_review(session)
    if args.cmd == "sync":
        return cmd_sync(session)
    if args.cmd == "nodes":
        return cmd_nodes(session)
    if args.cmd == "due":
        return cmd_due(session, args.cap, args.all)
    if args.cmd == "next":
        return cmd_next(session)
    if args.cmd == "grade":
        if not args.concept or not args.grade:
            print('用法: stage.py grade <会话> --concept "闭包" --grade 对 [--confidence 4]')
            return 2
        return cmd_grade(session, args.concept, args.grade,
                         args.confidence, args.date, args.evidence)
    if args.cmd == "stats":
        return cmd_stats(session)
    return cmd_advance(session, args.force)


if __name__ == "__main__":
    sys.exit(main())
