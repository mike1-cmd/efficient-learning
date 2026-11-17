#!/usr/bin/env python3
"""coach.py —— 取题引擎：决定「下一个问什么、问多难、从哪个角度问」。

它不重复实现记忆调度，而是坐在 scheduler.py 的 NodeStore 之上：
NodeStore 负责「这个概念的 S/D/到期日」，本文件负责「现在该拿哪道题考它」。

三个开源内核的移植（都 MIT）：
- chuma-beep/mathua：五类取题桶 review / remediation / practice / new / resume；
  掌握度按「证据是否过关」判，不按连对题数判 —— 它自己的
  docs/mastery-semantics.md 记录了反例：用连对 30 题判掌握，90% 正确率的人
  一次通过率只有 4.2%，掌握变成炼狱。本引擎不用连击数。
- open-spaced-repetition：可提取度曲线与 S/D 更新，直接复用 scheduler.NodeStore。
- Li-Evan/Bloom：learning-log.jsonl 只追加、先读摘要再下钻的记忆分层。

数据落在三处：
  <会话>/.items.json      题库（题面 + 角度 + 难度 + 评分标准）
  <会话>/.attempts.jsonl  每次答题的原始记录，只追加
  <学习根>/_coach/events.jsonl  跨主题的学员档案，只追加

零依赖，纯标准库。要求 Python 3.10+（与 stage.py / scheduler.py 一致）。
"""

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scheduler as sch  # noqa: E402

# ---------- 常量 ----------

ANGLES = ["定义", "边界", "反例", "操作", "关系", "迁移"]
"""六个出题角度。同一概念换角度问，才叫「不同的证据」；
同一个角度问三遍只是把一句话复述三次。"""

ANGLE_HINT = {
    "定义": "它是什么、不是什么，用一句话说清，不许背原话",
    "边界": "什么情况下它会失效 / 不成立 / 不适用",
    "反例": "给一个看起来对、实际错的用法，让他指出错在哪",
    "操作": "给我一步一步怎么做，先做哪步、会看到什么",
    "关系": "它和另一个概念的因果关系或包含关系，方向是哪边",
    "迁移": "换一个陌生场景，还认不认得出来",
}

LEVEL_HINT = {
    1: "认得出：给四个选项能挑对",
    2: "说得出：用自己的话讲一遍",
    3: "用得上：在一个小任务里用对",
    4: "改得动：给一段错的，能指出并改对",
    5: "造得出：从零做一个需要它的东西",
}

ANGLE_RECALL = "回忆"
"""无提示自由回忆。它不是第七个出题角度 —— 出题是「给你一个问题」，回忆是「什么都不给」。

两者的区别正是「当场会」和「真的记得」的区别：有提示的题，题干本身就是线索；
无提示回忆没有线索，能想起来才算进长时记忆。所以它单独记一类证据，不参与角度覆盖。
"""

TARGET_P_LO = 0.70      # 目标成功率下界
TARGET_P_HI = 0.85      # 目标成功率上界
BETA_STEP = 0.8         # 每级难度的区分度
ANGLES_FOR_MASTERY = 4  # 判「当场会」至少要覆盖几个角度
S_FOR_MASTERY = 7.0     # 判「已掌握」要求稳定度（天）
SUCCESS_DAYS_FOR_MASTERY = 2   # 至少在几个不同的日子成功提取过

FLUENCY_RATIO = 2.5     # 答对但超过中位数这么多倍 → 按「勉强」算
GLOBAL_BASE_MIN = 8     # 冷启动基线：同难度全局样本至少这么多条才敢用
GRADE_WEIGHT = {1: 0.0, 2: 0.6, 3: 1.0, 4: 1.0}   # 错 0 / 勉强 0.6 / 对 1 / 轻松 1

EMPTY_MARKS = {"", "______", "---", "…", "...", "-", "*", "待填", "TBD", "/", "　"}


# ---------- 小工具 ----------

def today() -> str:
    return sch.today_iso()


def _date(s):
    try:
        return sch.dt.date.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


def _num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _w(s: str) -> int:
    """显示宽度：中文和全角符号占两格，不算清楚表格会歪。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _w(s))


def _rpad(s: str, width: int) -> str:
    """按显示宽度右对齐 —— 中文列名用 Python 的 >6 会按字符数算，表格会歪。"""
    return " " * max(0, width - _w(s)) + s


def read_json(p: Path, default):
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def append_jsonl(p: Path, row: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(p: Path) -> list:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def norm_angle(a: str) -> str:
    """角度名归一：接受英文键与别名，统一成中文标准名。

    回忆也走这个入口，但它不在 ANGLES 里 —— 它不参与角度覆盖，只算留存证据。
    """
    a = (a or "").strip().lower().replace(" ", "")
    alias = {
        "definition": "定义", "define": "定义", "what": "定义",
        "boundary": "边界", "edge": "边界", "limit": "边界",
        "counterexample": "反例", "counter": "反例", "wrong": "反例",
        "procedure": "操作", "how": "操作", "step": "操作",
        "relation": "关系", "relationship": "关系", "link": "关系",
        "transfer": "迁移", "apply": "迁移", "new-scene": "迁移",
        "recall": ANGLE_RECALL, "free": ANGLE_RECALL,
        "自由回忆": ANGLE_RECALL, "回想": ANGLE_RECALL, "无提示": ANGLE_RECALL,
    }
    if a in alias:
        return alias[a]
    for name in ANGLES + [ANGLE_RECALL]:
        if a == name:
            return name
    return ""


PUNCT = set(" \t\n，。、；：！？,.;:!?（）()【】[]\"'“”‘’")


def bigrams(s: str) -> set:
    s = "".join(ch for ch in (s or "") if ch not in PUNCT)
    return {s[i:i + 2] for i in range(len(s) - 1)} or ({s} if s else set())


def similar(a: str, b: str, threshold: float = 0.75) -> bool:
    """题面相似度：字符二元组 Jaccard。换几个词、调个顺序照样能认出来。

    只靠哈希只能拦一字不差的重复；模型换个问法，同一道题就又进库了。
    """
    A, B = bigrams(a), bigrams(b)
    if not A or not B:
        return False
    return len(A & B) / len(A | B) >= threshold


def stem_key(stem: str) -> str:
    """题面指纹：去空白与标点后取哈希，用来拦重复出题。"""
    s = re.sub(r"[\s，。、；：！？,.;:!?（）()【】\[\]\"'“”‘’]", "", stem or "")
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


# ---------- 题库 ----------

class ItemBank:
    """题库，落在 <会话>/.items.json。

    item = {id, concept, angle, level, stem, rubric[], key, created, served, last, results[]}
    同一 (概念, 角度) 下题面不许重复 —— 重复就是拿同一道题假装换了个角度。
    """

    def __init__(self, session: Path):
        self.session = Path(session)
        self.path = self.session / ".items.json"
        raw = read_json(self.path, {})
        if not isinstance(raw, dict):
            raw = {}
        raw.setdefault("version", 1)
        raw.setdefault("items", {})
        self.data = raw

    @property
    def items(self) -> dict:
        return self.data["items"]

    def save(self) -> None:
        self.session.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def add(self, concept: str, angle: str, level: int, stem: str,
            rubric: list, key: str = "") -> tuple:
        """返回 (item, 是否新建)。题面重复时返回已存在的题并标记 False。"""
        fp = stem_key(stem)
        for it in self.items.values():
            if it.get("fp") == fp:
                return it, False
            if it.get("concept") == concept.strip() and similar(it.get("stem", ""), stem):
                return it, False
        i = 1
        while f"q{i}" in self.items:
            i += 1
        item = {
            "id": f"q{i}",
            "concept": concept.strip(),
            "angle": angle,
            "level": int(level),
            "stem": stem.strip(),
            "rubric": [r.strip() for r in rubric if str(r).strip()],
            "key": key.strip(),
            "fp": fp,
            "created": today(),
            "served": 0,
            "last": None,
            "results": [],
        }
        self.items[item["id"]] = item
        return item, True

    def for_concept(self, concept: str, angle: str = "", level: int = 0) -> list:
        out = []
        for it in self.items.values():
            if it.get("concept") != concept.strip():
                continue
            if angle and it.get("angle") != angle:
                continue
            if level and int(it.get("level", 0)) != int(level):
                continue
            out.append(it)
        # 出题次数少的优先，其次最久没出过的优先
        out.sort(key=lambda x: (x.get("served", 0), x.get("last") or ""))
        return out

    def pick(self, concept: str, angle: str, level: int) -> dict | None:
        """只在同一角度内找：同难度先，否则同角度任意难度。

        绝不跨角度兜底 —— 目标角度是「边界」，却拿一道「定义」题顶上，
        等于假装测过了边界。
        """
        got = self.for_concept(concept, angle, level) or self.for_concept(concept, angle)
        return got[0] if got else None

    def mark_served(self, item_id: str) -> dict | None:
        it = self.items.get(item_id)
        if it:
            it["served"] = int(it.get("served", 0)) + 1
            it["last"] = today()
        return it


# ---------- 答题记录与掌握度 ----------

class AttemptLog:
    """<会话>/.attempts.jsonl，只追加。"""

    def __init__(self, session: Path):
        self.session = Path(session)
        self.path = self.session / ".attempts.jsonl"

    def rows(self) -> list:
        return read_jsonl(self.path)

    def add(self, row: dict) -> None:
        append_jsonl(self.path, row)


def concept_attempts(rows: list, concept) -> list:
    name = (concept.get("name", "") if isinstance(concept, dict) else concept) or ""
    return [r for r in rows if (r.get("concept") or "").strip() == name.strip()]


def global_seconds(root: Path) -> dict:
    """跨主题的「难度 → 用时中位数」，用于流畅度冷启动。

    一个概念的前 4 条没有自己的基线。这时不能判流畅度（拿一道题定基准更糟），
    但也不能永远不判 —— 换用同难度的全局中位数做临时基线，样本够了再切回自己的。
    """
    per = {}
    for e in read_jsonl(Path(root) / "_coach" / "events.jsonl"):
        lv, sec = e.get("level"), e.get("seconds")
        if not sec or not lv or e.get("angle") == ANGLE_RECALL:
            continue
        try:
            lv, sec = int(lv), int(sec)
        except (TypeError, ValueError):
            continue
        if lv >= 1:
            per.setdefault(lv, []).append(sec)
    return {lv: statistics.median(v) for lv, v in per.items() if len(v) >= GLOBAL_BASE_MIN}


def success_days(rows: list, ok: list) -> list:
    """按日期去重后的「成功提取」日子。同一天答对十次仍只算一天。

    这条是「真的记得」的唯一硬证据：记忆要跨过睡眠才算稳定，
    当天连着答对只证明「还在工作记忆里」。
    """
    return sorted({r.get("at") for r, o in zip(rows, ok) if o and r.get("at")})


def span_days(days: list) -> int:
    if len(days) < 2:
        return 0
    return (_date(days[-1]) - _date(days[0])).days


def mastery(rows: list, node: dict | None, gsec: dict | None = None) -> dict:
    """一个概念的掌握度，分两档：当场会 → 已掌握。

    证据分两类：
    - 出题记录（六个角度）：证明「换个问法还认得」，构成当场会。
    - 回忆记录（ANGLE_RECALL）：什么都不给还能想起来，证明进了长时记忆。

    score = 0.30·角度覆盖 + 0.20·近期正确率（按证据量打折） + 0.20·稳定度
          + 0.15·最高通过难度 + 0.15·跨天提取
    「当场会」= 角度≥4 + 近期正确率≥0.75 + 最高通过难度≥3。
    「已掌握」= 当场会 + 至少 2 个不同日子成功提取 + S≥7 天。

    为什么要拆两档：同一天里答对四道题，只能说明「现在会」。
    当天就宣布「学会」，等于把工作记忆里的东西当成长期记忆 —— 这正是学完就忘的成因。
    """
    n = len(rows)
    if n == 0:
        return {
            "n": 0, "ability": 0.0, "score": 0.0, "acc": 0.0, "recent": 0.0, "angles": [],
            "max_level": 0, "S": 0.0, "mastered": False, "practiced": False,
            "delayed": False, "success_days": [], "span": 0,
            "median_sec": None, "slowed": 0, "baseline_src": None,
            "R": None, "state": "未开始", "gaps": ["还没答过"],
        }

    qrows = [r for r in rows if r.get("angle") != ANGLE_RECALL]
    secs = [int(r["seconds"]) for r in qrows if r.get("seconds")]

    # 流畅度基线：本概念自己的中位数优先；不足 4 条时，按每行的难度取全局中位数
    self_base = statistics.median(secs) if len(secs) >= 4 else None
    used_global = False

    def base_for(r):
        nonlocal used_global
        if self_base:
            return self_base
        if not gsec:
            return None
        lv = r.get("level")
        try:
            lv = int(lv)
        except (TypeError, ValueError):
            return None
        b = gsec.get(lv) if lv >= 1 else None
        used_global = used_global or bool(b)
        return b

    grades = [int(r.get("grade", 1)) for r in rows]
    eff, slowed = [], 0
    for g, r in zip(grades, rows):
        # 回忆一次写三分钟是正常的，不拿它和「答一道题」的秒数比
        if r.get("angle") == ANGLE_RECALL:
            eff.append(g)
            continue
        e, cut = fluency_adjusted_grade(g, r.get("seconds"), base_for(r))
        slowed += 1 if cut else 0
        eff.append(e)

    # 通过线是「勉强及以上」（g>=2），与 stage.py 的回测判法一致。
    ok = [g >= 2 for g in eff]
    recent = eff[-3:]
    acc = sum(ok) / n
    recent_acc = sum(GRADE_WEIGHT[g] for g in recent) / len(recent)
    passed_angles = sorted({r["angle"] for r, o in zip(rows, ok)
                            if r.get("angle") in ANGLES and o})
    max_level = max([int(r.get("level", 0)) for r, o in zip(rows, ok)
                     if o and int(r.get("level", 0) or 0) >= 1] or [0])
    days = success_days(rows, ok)
    span = span_days(days)
    delayed = len(days) >= SUCCESS_DAYS_FOR_MASTERY and span >= 1
    s = _num((node or {}).get("S", 0.0))

    # 现在还能想起的概率：复习队列和召回命令都靠它排序
    r_now = None
    last = _date((node or {}).get("last"))
    if node and node.get("reps", 0) and last and s > 0:
        elapsed = max((_date(today()) - last).days, 0)
        r_now = round(sch.forgetting_curve(elapsed, s), 3)

    acc_weight = min(1.0, n / 4.0)
    score = (
        0.30 * len(passed_angles) / len(ANGLES)
        + 0.20 * recent_acc * acc_weight
        + 0.20 * min(1.0, s / 21.0)
        + 0.15 * min(1.0, max_level / 5.0)
        + 0.15 * (min(1.0, len(days) / 3.0) if delayed else 0.0)
    )

    practiced = (
        len(passed_angles) >= ANGLES_FOR_MASTERY
        and recent_acc >= 0.75
        and max_level >= 3
    )
    mastered = practiced and delayed and s >= S_FOR_MASTERY

    gaps = []
    if len(passed_angles) < ANGLES_FOR_MASTERY:
        gaps.append(f"角度 {len(passed_angles)}/{ANGLES_FOR_MASTERY}")
    if max_level < 3:
        gaps.append(f"最高通过难度 {max_level}/3")
    if recent_acc < 0.75:
        gaps.append(f"近三次正确率 {recent_acc:.0%}")
    if not delayed:
        gaps.append(f"跨天提取 {len(days)}/{SUCCESS_DAYS_FOR_MASTERY} 天")
    if s < S_FOR_MASTERY:
        gaps.append(f"稳定度 {s:.1f}/{S_FOR_MASTERY:.0f} 天")

    if practiced and mastered:
        state = "已掌握"
    elif practiced and delayed:
        state = "跨天验证过，待巩固"
    elif practiced:
        state = "当场会（未跨天验证）"
    elif recent_acc < 0.6:
        state = "在补弱"
    else:
        state = "在学"

    ability = (
        0.45 * len(passed_angles) / len(ANGLES)
        + 0.35 * min(1.0, max_level / 5.0)
        + 0.20 * recent_acc * min(1.0, n / 4.0)
    )
    return {
        "n": n,
        "ability": round(clamp(ability, 0.0, 1.0), 3),
        "score": round(clamp(score, 0.0, 1.0), 3),
        "acc": round(acc, 3),
        "recent": round(recent_acc, 3),
        "angles": passed_angles,
        "max_level": max_level,
        "S": round(s, 2),
        "mastered": mastered,
        "practiced": practiced,
        "delayed": delayed,
        "success_days": days,
        "span": span,
        "R": r_now,
        "state": state,
        "gaps": gaps,
        "slowed": slowed,
        "baseline_src": "self" if self_base else ("global" if used_global else None),
        "median_sec": int(statistics.median(secs)) if secs else None,
    }


def fluency_adjusted_grade(grade: int, seconds, baseline, ratio: float = FLUENCY_RATIO) -> tuple:
    """答对但慢到离谱 → 降成「勉强」。返回 (有效成绩, 是否降级)。

    流畅度本身就是掌握的一部分：45 秒才想起一个 12 秒该反应过来的东西，
    在真实场景里就是不会 —— 考试超时、现场答不上来，都一样。
    mathua 把这条写成了硬阈值（正确但超过该概念时间阈值不算过），本函数是同一思路的软化版：
    不判失败，只是把「对」记成「勉强」，证据变弱但不归零。

    baseline 缺失或样本不足时不做调整 —— 拿一道题定基准，比不调更糟。
    """
    if grade >= 3 and seconds and baseline and seconds > ratio * baseline:
        return 2, True
    return grade, False


def theta_of(score: float) -> float:
    """掌握度 → 能力值（1PL IRT 的 theta）。

    把 score 直接当作「一道难度 3 的题的预计通过率」，取 logit 即可：
    m=0.5 → theta=0（五五开），m=0.8 → theta≈1.39（八成会做）。
    这样不用额外拟合参数，掌握度和难度就落在同一把尺子上。
    """
    m = clamp(score, 0.03, 0.97)
    return math.log(m / (1.0 - m))


DEFAULT_MODEL = {
    "difficulty_offset": 0.0,   # 正数 = 模型低估了你（实际比预测更容易）
    "retention_offset": 0.0,    # 正数 = 遗忘曲线太悲观（实际记得比它预测的多）
    "updated": None,
    "fit_n": 0,
    "notes": [],
}
"""个人校准参数，落在 <学习根>/_coach/model.json。

这两个数是「这套模型对你这个人的系统性偏差」。全局参数只有一套，
但每个人的表现都系统性偏离模型：有人总比预测稳，有人总比预测差。
不修这个偏差，难度带和复习间隔就一直偏，而且偏得一致 —— 越用越偏。
"""


def load_model(root: Path) -> dict:
    raw = read_json(Path(root) / "_coach" / "model.json", {})
    m = dict(DEFAULT_MODEL)
    if isinstance(raw, dict):
        m.update({k: raw[k] for k in DEFAULT_MODEL if k in raw})
    return m


def save_model(root: Path, m: dict) -> None:
    p = Path(root) / "_coach" / "model.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


def model_offset(session: Path) -> float:
    return _num(load_model(Path(session).parent).get("difficulty_offset"), 0.0)


def level_prob(ability: float, level: int, offset: float = 0.0) -> float:
    """能力值 + 难度 → 预计做对率。offset 是个人校准的难度偏移。"""
    return sigmoid(theta_of(ability) - (level - 3) * BETA_STEP + offset)


def target_level(ability: float, passed_level: int = 0, offset: float = 0.0) -> tuple:
    """挑一个难度，使预计做对率落在 70–85%。

    p = sigmoid(theta - beta)，beta = (level-3)·0.8。
    解 p = 0.775 得 beta = theta - 1.24，再反推 level。

    两条约束会覆盖「目标带」：
    - 不低于已通过的难度（passed_level）：考回更低一档是浪费他的时间。
    - 若已通过那档仍然高于 85%，再往上探一档。
    到 1 或 5 顶格时如实说明边界。
    """
    theta = theta_of(ability)
    lv = int(clamp(round(3 + (theta - 1.24 + offset) / BETA_STEP), 1, 5))
    if passed_level and lv < passed_level:
        floor_lv = int(clamp(passed_level, 1, 5))
        # 只有「那一档现在仍有五成以上把握」才抬回去；低于五成说明那次通过更像蒙的，
        # 或者已经退化了 —— 按当前能力出题，别拿旧成绩压他。
        if level_prob(ability, floor_lv, offset) >= 0.55:
            lv = floor_lv
            if level_prob(ability, lv, offset) > TARGET_P_HI and lv < 5:
                lv += 1
    p = level_prob(ability, lv, offset)
    at_edge = (lv == 1 and p < TARGET_P_LO) or (lv == 5 and p > TARGET_P_HI)
    return lv, round(p, 2), at_edge


# ---------- 取题 ----------

def is_practice_only(node: dict, on: str | None = None) -> bool:
    """今天已经给这个概念记过一次分了？是 → 这次只算练习，不动 S/D。

    一份证据不能算两遍。若每次练习都喂进 FSRS，间隔会被虚抬：
    今天刚练完 3 遍，曲线却说「三周后再来」。
    """
    return bool(node) and node.get("reps", 0) > 0 and node.get("last") == (on or today())


def prereq_met(node: dict, store: sch.NodeStore) -> bool:
    return all(store.nodes.get(p, {}).get("reps", 0) > 0
               for p in node.get("prereq_ids", []))


def centrality(node: dict, store: sch.NodeStore) -> float:
    """被多少概念当前置 —— 地基越靠下，越该先学。"""
    names = {n["name"] for n in store.live()}
    hit = sum(1 for n in store.live() if node["name"] in (n.get("prereq") or []))
    return hit / max(len(names) - 1, 1)


def unfinished_actions(session: Path) -> set:
    """从 03-practice.md 里找出还没打勾的动作对应的概念。"""
    p = Path(session) / "03-practice.md"
    if not p.exists():
        return set()
    out = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s.startswith("|") or "[ ]" not in s:
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 2:
            continue
        concept = cells[1]
        if concept and concept not in EMPTY_MARKS and not concept.startswith("#"):
            out.add(concept)
    return out


def candidates(session: Path, store: sch.NodeStore, rows: list,
               on=None, gsec=None) -> list:
    """五类取题桶，各给一个优先级分数。"""
    on = on or sch.dt.date.today()
    todo = unfinished_actions(session)
    out = []
    for node in store.live():
        name = node["name"]
        crows = concept_attempts(rows, name)
        m = mastery(crows, node, gsec)
        due = _date(node.get("due"))
        base = None

        if node["reps"] > 0 and due and due <= on:
            elapsed = max((on - _date(node["last"])).days, 0) if node.get("last") else 0
            r = sch.forgetting_curve(elapsed, node["S"])
            base = ("review", 2.0 + 2.0 * (1.0 - r) + 0.4 * (1.0 - m["score"]),
                    f"到期了，现在能想起的概率 {r:.0%}")
        elif name in todo:
            base = ("resume", 1.4 + 0.3 * (1.0 - m["score"]),
                    "这个概念的动作还没做完")
        elif node["reps"] > 0 and (m["recent"] < 0.6 or node.get("lapses", 0) >= 2) \
                and not m["mastered"]:
            base = ("remediation", 1.3 + 1.0 * (1.0 - m["score"])
                    + 0.2 * min(node.get("lapses", 0), 3),
                    f"最近答对率 {m['recent']:.0%}"
                    + (f"，已经忘了 {node['lapses']} 次" if node.get("lapses") else ""))
        elif node["reps"] > 0 and not m["mastered"]:
            gap = "、".join(m["gaps"][:2]) or "跨天验证"
            base = ("practice", 0.9 + 0.9 * (1.0 - m["score"]),
                    f"{m['state']}，还差 {gap}")
        elif node["reps"] == 0 and prereq_met(node, store):
            base = ("new", 0.7 + 0.3 * centrality(node, store),
                    "前置都会了，可以开始" if node.get("prereq")
                    else "没有前置，可以直接开始")

        if base:
            bucket, score, why = base
            if node.get("retired"):
                continue
            out.append({
                "bucket": bucket, "node": node, "mastery": m,
                "score": round(score, 3), "why": why,
            })
    out.sort(key=lambda c: -c["score"])
    return out


def angle_history(root: Path) -> dict:
    """跨主题的角度正确率：{角度: (正确率, 样本数)}，样本 <3 的不算。"""
    per = {}
    for e in read_jsonl(root / "_coach" / "events.jsonl"):
        a = e.get("angle")
        if not a:
            continue
        per.setdefault(a, []).append(1 if int(e.get("grade", 1)) >= 2 else 0)
    return {a: (sum(v) / len(v), len(v)) for a, v in per.items() if len(v) >= 3}


def weak_angles(root: Path, floor: float = 0.5) -> list:
    """历史正确率低于 floor 的角度，按从差到好排。"""
    hist = angle_history(root)
    bad = [(a, acc) for a, (acc, _) in hist.items() if acc < floor]
    bad.sort(key=lambda x: x[1])
    return [a for a, _ in bad]


def pick_angle(m: dict, rows: list, bucket: str, weak=()) -> tuple:
    """决定这次问哪个角度。返回 (角度, 为什么是这个角度)。

    三条规则，按优先级：
    1. 补弱时先回到刚答错的那个角度 —— 在没补上的角度旁边换角度，只是多制造一次失败。
    2. 否则挑从没测过的角度 —— 没测过的角度是信息量最大的一题。
    3. 六个都测过，就挑最久没问的。
    """
    if bucket == "remediation":
        wrong = [r for r in rows if int(r.get("grade", 1)) == 1 and r.get("angle")]
        if wrong:
            a = wrong[-1]["angle"]
            return a, "刚在这个角度答错，先补到过关再换角度"

    seen = {}
    for r in rows:
        if r.get("angle"):
            seen[r["angle"]] = r.get("at", "")
    fresh = [a for a in ANGLES if a not in seen]
    if fresh:
        # 跨主题档案里弱的角度先问：别处总在「边界」上错，这里就别等轮到它
        hit = [a for a in fresh if a in weak]
        if hit:
            return hit[0], "6 个角度里还没测过这个，而且你在别的主题上最容易错这种问法"
        return fresh[0], "6 个角度里还没测过这个"

    passed = set(m["angles"])
    rest = [a for a in ANGLES if a not in passed]
    if rest:
        return rest[0], "测过但没过，换个角度从侧面验证"

    return min(ANGLES, key=lambda a: seen.get(a, "")), "6 个角度都过了，挑最久没问的保持新鲜"


def cmd_next(session: Path, as_json: bool = False) -> int:
    store = sch.NodeStore(session)
    if not store.live():
        print("还没有概念节点。先在第 2 阶写 02-core.md 的概念表，再跑：")
        print(f"  python scripts/stage.py sync {session}")
        return 2

    rows = AttemptLog(session).rows()
    weak = weak_angles(session.parent)
    gsec = global_seconds(session.parent)
    cands = candidates(session, store, rows, gsec=gsec)
    if not cands:
        if all(mastery(concept_attempts(rows, n["name"]), n, gsec)["mastered"]
               for n in store.live()):
            print("全部概念都已掌握（跨天验证过）。今天只做召回，没有到期就休息。")
            return 0
        print("没有可出的题：剩下的概念前置都没满足。先学前置：")
        for n in store.live():
            if n["reps"] == 0 and not prereq_met(n, store):
                print(f"  - {n['name']}  ← 前置：{'、'.join(n.get('prereq') or [])}")
        return 2

    top = cands[0]
    node, m = top["node"], top["mastery"]
    rows_this = concept_attempts(rows, node["name"])
    angle, angle_why = pick_angle(m, rows_this, top["bucket"], weak)
    if m["n"] == 0:
        # 第一次碰这个概念：先探底，不套目标带（没有掌握度可比，套了也是假的）
        level, p, at_edge = 1, 0.5, False
        first_look = True
    else:
        level, p, at_edge = target_level(m["ability"], m["max_level"], model_offset(session))
        first_look = False
    bank = ItemBank(session)
    item = bank.pick(node["name"], angle, level)

    payload = {
        "bucket": top["bucket"],
        "concept": node["name"],
        "angle": angle,
        "angle_why": angle_why,
        "level": level,
        "p_expected": p,
        "at_edge": at_edge,
        "mastery": m,
        "why": top["why"],
        "item": item["id"] if item else None,
        "stem": item["stem"] if item else None,
        "rubric": item["rubric"] if item else [],
        "alternatives": [c["node"]["name"] for c in cands[1:4]],
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    bucket_label = {
        "review": "到期复习", "remediation": "补弱", "practice": "练熟",
        "new": "新概念", "resume": "续上次没做完的",
    }[top["bucket"]]
    st = read_json(session / ".stage.json", {})
    if int(st.get("stage", -1)) == 5:
        rnd = int(st.get("cycle", 1) or 1)
        done = len([a for a in rows
                    if int(a.get("stage", -1)) == 5 and int(a.get("round", -1)) == rnd])
        print(f"【取题】{bucket_label} · {node['name']}　（本轮第 {done + 1}/5 题）")
    else:
        print(f"【取题】{bucket_label} · {node['name']}")
    print(f"桶      {top['bucket']}（{bucket_label}）")
    print(f"角度    {angle}　← {angle_why}")
    print(f"难度    {level}/5"
          + ("　← 第一次碰这个概念，先探底，不套目标带" if first_look
             else f"（预计做对率 {p:.0%}，目标带 {TARGET_P_LO:.0%}–{TARGET_P_HI:.0%}）"
                  + ("　⚠ 已到最浅一档仍低于目标带 → 印象接近零，先讲再问，别硬考"
                     if level == 1 and p < TARGET_P_LO
                     else "　⚠ 已到最难一档仍高于目标带 → 这个概念可以判掌握了"
                     if level == 5 and p > TARGET_P_HI
                     else "　← 这一档你已经通过过，不降回去；低于目标带是刻意加难"
                     if level > 1 and p < TARGET_P_LO else "")))
    print(f"概念    {node['name']}　{m['state']}"
          f"（掌握度 {m['score']:.2f} · 角度 {len(m['angles'])}/6 · "
          f"最高通过难度 {m['max_level']} · 稳定度 {m['S']:.1f} 天）")
    print(f"理由    {top['why']}")
    print(f"考什么  {ANGLE_HINT[angle]}；难度对应「{LEVEL_HINT[level]}」")
    if item:
        lv_note = "" if int(item.get("level", lv)) == lv else f"，库里那道是难度 {item['level']}"
        print(f"题库    命中已有题 {item['id']}（出过 {item.get('served', 0)} 次{lv_note}）")
    else:
        print("题库    没命中 → 现场出，出完用 coach.py add 存进去，下次不会重复")
    return 0


# ---------- 记录答题 ----------

def parse_grade(v) -> int | None:
    return sch.parse_grade(v)


def cmd_record(session: Path, concept: str, item_id: str, grade, seconds,
               confidence, evidence: str, angle: str, level) -> int:
    g = parse_grade(grade)
    if g is None:
        print(f"成绩要 1-4 或 错/勉强/对/轻松，收到的是 {grade!r}")
        return 2

    store = sch.NodeStore(session)
    node = store.get(concept)
    if node is None:
        print(f"概念库里没有「{concept}」。现有："
              + "、".join(n["name"] for n in store.live()[:12]))
        return 2

    bank = ItemBank(session)
    item = bank.items.get(item_id) if item_id else None
    if item and item.get("concept") and item["concept"] != node["name"]:
        print(f"题目 {item_id} 属于「{item['concept']}」，跟概念「{node['name']}」对不上")
        return 2

    a = norm_angle(angle or (item or {}).get("angle", ""))
    if not a:
        print("要知道这道题考的是哪个角度，六个选一个：" + "、".join(ANGLES))
        return 2
    lv = int(level or (item or {}).get("level", 0) or 0)
    if a == ANGLE_RECALL:
        # 回忆不按难度分档：能想起来就是能想起来，不存在「难一点的回忆」
        lv = 0
    elif not 1 <= lv <= 5:
        print("难度要 1-5")
        return 2

    if not (evidence or "").strip():
        print("⚠ 没带用户原话（--evidence），这条只算模型的印象，stats 里凭证率会掉。")

    # 先记旧掌握度，用于事后看这一步把预测推对了没有
    gsec = global_seconds(session.parent)
    before = mastery(concept_attempts(AttemptLog(session).rows(), node["name"]), node, gsec)

    # 回忆的预测不是「难度模型的预计做对率」，而是遗忘曲线给的「现在还想得起吗」。
    # 两者是不同的问题，混在一个字段里就没法分别对账。
    r_pred = None
    if a == ANGLE_RECALL and node.get("reps", 0) and node.get("last") and node.get("S", 0) > 0:
        elapsed = max((_date(today()) - _date(node["last"])).days, 0)
        r_pred = round(sch.forgetting_curve(elapsed, node["S"]), 3)

    # 同一天第二次及以后的答题只算练习，不动 S/D。
    # 一份证据不能算两遍：若每次都喂进 FSRS，间隔会被虚抬到「今天刚练完却说三周后再来」。
    same_day = is_practice_only(node)
    if not same_day:
        store.grade(node["name"], g, confidence=confidence,
                    evidence=evidence or "", note=f"角度 {a} · 难度 {lv}")
        store.save()

    stage_now = int(read_json(session / ".stage.json", {}).get("stage", -1))
    round_now = int(read_json(session / ".stage.json", {}).get("cycle", 1) or 1)
    row = {
        "at": today(),
        "stage": stage_now,
        "round": round_now,
        "concept": node["name"],
        "item": item_id or None,
        "angle": a,
        "level": lv,
        "grade": g,
        "seconds": int(seconds) if seconds else None,
        "confidence": int(confidence) if confidence else None,
        "evidence": (evidence or "")[:500],
        "mastery_before": before["score"],
        # 第一次接触这个概念时不给预测：没有基线，算出来的 3% 是编的，
        # 还会把 calibration 的 Brier 分数拉成假差。
        "p_expected": (None if (before["n"] == 0 or a == ANGLE_RECALL)
                       else round(level_prob(before["ability"], lv, model_offset(session)), 3)),
        "p_recall": r_pred,
    }
    AttemptLog(session).add(row)

    # 跨主题档案：同一份事件抄一份到学习根，profile 靠它算你的长期毛病
    root = session.parent
    append_jsonl(root / "_coach" / "events.jsonl",
                 {**row, "topic": session.name, "S": node["S"], "due": node["due"]})

    if item:
        item.setdefault("results", []).append(g)
        bank.mark_served(item_id)
        bank.save()

    after = mastery(concept_attempts(AttemptLog(session).rows(), node), node, gsec)
    head = f"已记：{node['name']} · {a}"
    if lv:
        head += f" · 难度 {lv}"
    print(head + f" · {sch.GRADE_LABEL[g]}"
          + ("（今天已记过分，这条算练习）" if same_day else ""))
    print(f"掌握度 {before['score']:.2f} → {after['score']:.2f}　{after['state']}"
          + ("" if after["mastered"] else
             ("　还差：" + "、".join(after["gaps"]) if after["gaps"] else "")))
    if same_day:
        print(f"记忆曲线没动：稳定度仍是 {node['S']:.1f} 天，到期 {node['due']}。"
              "同一天的重复练习不再算一次复习。")
    else:
        print(f"稳定度 {node['S']:.1f} 天，下次到期 {node['due']}（约 "
              f"{sch.interval_for(node['S'], store.data.get('request_retention', sch.DEFAULT_RETENTION))} 天后）")
    if row["seconds"] and after["median_sec"]:
        print(f"用时 {row['seconds']} 秒，这个概念的用时中位数 {after['median_sec']} 秒")
    if after.get("slowed"):
        print(f"注意：这个概念有 {after['slowed']} 条答对但用时超过中位数 {FLUENCY_RATIO} 倍，"
              "已按「勉强」计入 —— 想得起来不等于想得快。")
    missing = [x for x in ANGLES if x not in after["angles"]]
    if missing and not after["mastered"]:
        print("还没过关的角度：" + "、".join(missing))
    return 0


def log_external_recall(session: Path, node: dict, g: int, confidence=None,
                        evidence: str = "", seconds=None, on=None) -> dict:
    """给 stage.py grade 用：把一次回忆补进答题日志。

    两个入口写两套账是最容易裂的地方：stage.py 改了记忆曲线，coach 的报表和掌握度
    却看不见，用户会同时看到「已复习 3 次」和「未开始」。这里补上同一份账。

    node 必须是**评分前**的快照 —— store.grade 会原地改 S 和 last。
    """
    session = Path(session)
    day = on or _date(today())
    before = mastery(concept_attempts(AttemptLog(session).rows(), node["name"]), node,
                     global_seconds(session.parent))
    r_pred = None
    if node.get("reps", 0) and node.get("last") and _num(node.get("S"), 0) > 0:
        elapsed = max((day - _date(node["last"])).days, 0)
        r_pred = round(sch.forgetting_curve(elapsed, node["S"]), 3)
    st = read_json(session / ".stage.json", {})
    row = {
        "at": day.isoformat(),
        "stage": int(st.get("stage", -1)),
        "round": int(st.get("cycle", 1) or 1),
        "concept": node["name"],
        "item": None,
        "angle": ANGLE_RECALL,
        "level": 0,
        "grade": int(g),
        "seconds": int(seconds) if seconds else None,
        "confidence": int(confidence) if confidence else None,
        "evidence": (evidence or "")[:500],
        "mastery_before": before["score"],
        "p_expected": None,
        "p_recall": r_pred,
    }
    AttemptLog(session).add(row)
    append_jsonl(session.parent / "_coach" / "events.jsonl",
                 {**row, "topic": session.name, "S": node.get("S"), "due": node.get("due")})
    return row


def cmd_add(session: Path, concept: str, angle: str, level: int,
            stem: str, rubric: str, key: str) -> int:
    a = norm_angle(angle)
    if not a:
        print("角度要六选一：" + "、".join(ANGLES))
        return 2
    if not 1 <= int(level) <= 5:
        print("难度要 1-5")
        return 2
    rub = [x for x in re.split(r"[;；]\s*", rubric or "") if x.strip()]
    if len(rub) < 2:
        print("评分标准至少两条，用分号隔开。只有一条等于没标准。")
        return 2
    bank = ItemBank(session)
    item, created = bank.add(concept, a, level, stem, rub, key)
    if created:
        bank.save()
        print(f"入库 {item['id']}：{concept} · {a} · 难度 {level}（评分标准 {len(rub)} 条）")
    else:
        print(f"这道题已经在库里了（{item['id']}），没有重复入库。")
    return 0


# ---------- 报表 ----------

GRADE_TEXT = {1: "错", 2: "勉强", 3: "对", 4: "对"}


def _cells(line: str) -> list:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_sep(line: str) -> bool:
    """表格分隔行（|---|---|）。不用正则：反斜杠在多种写入路径下会被改写。"""
    body = line.strip().strip("|")
    return bool(body) and all(ch in "-:| \t" for ch in body)


def _is_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and not _is_sep(s)


def retest_block(lines: list) -> tuple:
    """定位回测表：只认「轮次」表头下面那一张表。

    早先按「整篇找形如 | 1 | 的行」写，结果把「四类错误自检」表的行也吞了 ——
    那张表的首列也是序号 1-5，第六列同样空着。
    """
    head = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith("|") and _cells(ln) and _cells(ln)[0] == "轮次":
            head = i
            break
    if head is None:
        return None, []
    rows = []
    for i in range(head + 1, len(lines)):
        s = lines[i].strip()
        if not s.startswith("|"):
            break
        if _is_sep(s):
            continue
        rows.append(i)
    return head, rows


def cmd_sync(session: Path) -> int:
    """把本轮的答题记录写进 04-knowledge-map.md 的回测表。

    两半必须接上：答题数据在 .attempts.jsonl，台阶门禁读的是回测表。
    少了这一步，用户答完 5 题仍然卡在第 5 阶，因为表是空的。
    只同步「第 5 阶 + 当前轮次」的记录 —— 第 3 阶的练习不该混进回测。
    """
    st = read_json(session / ".stage.json", {})
    rnd = int(st.get("cycle", 1) or 1)
    path = session / "04-knowledge-map.md"
    if not path.exists():
        print(f"{path.name} 不在，先建会话或补回模板")
        return 2

    mine = [a for a in AttemptLog(session).rows()
            if int(a.get("stage", -1)) == 5 and int(a.get("round", -1)) == rnd]
    if not mine:
        print("本轮还没有第 5 阶的答题记录。先跑 coach.py next 出题、record 记分。")
        return 2

    lines = path.read_text(encoding="utf-8").splitlines()
    head, rows = retest_block(lines)
    if head is None:
        print("04-knowledge-map.md 里找不到「轮次」表头的回测表，照模板补回这一节")
        return 2

    def cells(i):
        return _cells(lines[i])

    def is_round(i):
        c = cells(i)
        return len(c) >= 6 and c[0].isdigit() and int(c[0]) == rnd

    judged = [i for i in rows if is_round(i) and cells(i)[5]]
    blanks = [i for i in rows if is_round(i) and not cells(i)[5]]

    todo = mine[len(judged):]
    if not todo:
        print(f"回测表已是最新：第 {rnd} 轮共 {len(judged)} 行，没有新的答题记录要写。")
        return 0

    def fmt(no, a):
        stem = (a.get("stem") or "").replace("|", "／")
        if not stem:
            stem = f"（{a['angle']}角度 · 难度{a['level']}）"
        note = f"{a['angle']}·难度{a['level']}"
        if a.get("seconds"):
            note += f"·{a['seconds']}s"
        conf = a.get("confidence") or ""
        return (f"| {rnd} | {no} | {a['concept']} | {stem[:40]} | {conf} | "
                f"{GRADE_TEXT.get(int(a['grade']), '对')} | {note} |")

    slots = list(blanks)
    insert_at = (max(rows) + 1) if rows else head + 2
    extra = 0
    for k, a in enumerate(todo):
        row = fmt(len(judged) + k + 1, a)
        if slots:
            lines[slots.pop(0)] = row
        else:
            lines.insert(insert_at + extra, row)
            extra += 1

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已写进回测表：第 {rnd} 轮 {len(judged) + len(todo)} 行"
          f"（本次新增 {len(todo)}）。接着跑 stage.py check 看还差什么。")
    return 0


MIN_FIT = 20       # 少于此数不写模型：20 条以下，拟合出来的只是噪声


def _logit(p: float) -> float:
    p = clamp(float(p), 0.01, 0.99)
    return math.log(p / (1.0 - p))


def fit_offset(pairs: list, lo: float = -1.5, hi: float = 1.5,
               step: float = 0.05) -> tuple:
    """一维网格搜索：找让 Brier 最小的 logit 偏移。

    只需要一个参数，就不该上梯度下降。搜索 61 个点足够，
    而且结果可复现、可解释 —— 偏移 +0.4 就是「实际比预测稳定高两成」。
    """
    if not pairs:
        return 0.0, 1.0, 1.0

    def brier(d):
        s = 0.0
        for p, a in pairs:
            s += (sigmoid(_logit(p) + d) - a) ** 2
        return s / len(pairs)

    base = brier(0.0)
    best_d, best = 0.0, base
    n = int(round((hi - lo) / step))
    for i in range(n + 1):
        d = lo + i * step
        b = brier(d)
        if b < best - 1e-12:
            best_d, best = d, b
    return round(best_d, 2), round(best, 3), round(base, 3)


def retention_pairs(store) -> list:
    """遗忘曲线的对账数据：每次复习时模型给的 R，和实际有没有想起来。

    只取跨天的复习 —— 同一天的重复回忆不是留存测试。
    """
    out = []
    for n in store.live():
        prev = None
        for e in n.get("events", []):
            cur = _date(e.get("at"))
            if cur and prev and (cur - prev).days >= 1 and e.get("R") is not None:
                out.append((float(e["R"]), 1 if int(e.get("grade", 1)) >= 2 else 0))
            if cur:
                prev = cur
    return out


def cmd_calibration(session: Path, apply: bool = False) -> int:
    """两个模型的账，一起对：难度准不准、遗忘曲线准不准。

    每道题都报一个「预计 78%」，每次复习都报一个「还能想起 62%」。
    不核对，这两个数字就是装饰。核对完还能把系统性偏差写回模型 ——
    只喊「下次注意」的校准，等于没校准。
    """
    root = Path(session).parent
    store = sch.NodeStore(session)
    rows = AttemptLog(session).rows()
    model = load_model(root)

    print("=== 难度模型：预计做对率 vs 实际 ===")
    dp = [(float(a["p_expected"]), 1 if int(a.get("grade", 1)) >= 2 else 0)
          for a in rows if a.get("p_expected") is not None]
    d_off = None
    if len(dp) < 5:
        print(f"只有 {len(dp)} 条带预测的答题，样本不够（至少 5 条）。")
        print("这不是「没问题」，是「还没法测」——别把测不了当成测得准。")
    else:
        d_off, b_after, b_base = fit_offset(dp)
        # 全对或全错时最优解在无穷远处，网格只会顶到边界 —— 那个数不是偏移，是边界。
        degenerate = len({a for _, a in dp}) < 2
        if degenerate:
            d_off = None
            print(f"样本 {len(dp)} 条 · Brier {b_base:.3f}")
            print("⚠ 这批数据全对或全错，拟合不出偏移 —— 只能说题太简单或太难，"
                  "不要据此改模型。")
        else:
            print(f"样本 {len(dp)} 条 · Brier {b_base:.3f} → 校准后 {b_after:.3f}"
                  f" · 建议偏移 {d_off:+.2f}")
            if len(dp) < MIN_FIT:
                print(f"⚠ 不到 {MIN_FIT} 条，这个偏移只能当方向：错一条就能让它跳 0.3。"
                      "现在写进模型会把噪声当规律。")
        print(f"{'预测区间':<12}{'题数':>5}{'预测':>8}{'实际':>8}")
        print("-" * 36)
        for lo, hi in ((0.0, 0.5), (0.5, 0.7), (0.7, 0.85), (0.85, 1.01)):
            sub = [(p, a) for p, a in dp if lo <= p < hi]
            if not sub:
                continue
            print(f"{lo:.2f}–{min(hi, 1.0):.2f}   {len(sub):>6}"
                  f"{sum(p for p, _ in sub) / len(sub):>8.0%}"
                  f"{sum(a for _, a in sub) / len(sub):>8.0%}")

    print()
    print("=== 遗忘曲线：复习时预测的想起概率 vs 实际 ===")
    rp = retention_pairs(store)
    r_off = None
    if len(rp) < 5:
        print(f"只有 {len(rp)} 次跨天复习，样本不够（至少 5 次）。")
        print("这条要等复习真发生几轮才有数 —— 现在没有结论，不是「曲线是准的」。")
    else:
        r_off, rb_after, rb_base = fit_offset(rp)
        got = sum(a for _, a in rp) / len(rp)
        pred = sum(p for p, _ in rp) / len(rp)
        degenerate_r = len({a for _, a in rp}) < 2
        if degenerate_r:
            r_off = None
        print(f"样本 {len(rp)} 次 · 平均预测 {pred:.0%}，实际想起 {got:.0%}"
              f"（Brier {rb_base:.3f}"
              + ("" if degenerate_r else f" → {rb_after:.3f}") + "）")
        verdict = ("曲线偏保守：你比它以为的记得牢，间隔可以拉长"
                   if got - pred > 0.08 else
                   "曲线偏乐观：实际忘得比它预测的多，间隔该缩短"
                   if pred - got > 0.08 else "曲线基本准")
        print(f"→ {verdict}"
              + ("" if r_off is None else f"（建议偏移 {r_off:+.2f}）"))
        if degenerate_r:
            print("⚠ 全是同一个结果，算不出偏移 —— 只能说这批复习要么全过要么全崩。")
        elif len(rp) < MIN_FIT:
            print(f"⚠ 不到 {MIN_FIT} 次，别据此大改间隔。")

    if not apply:
        if not (len(dp) >= 5 or len(rp) >= 5):
            print()
            print("两个模型都没到能下结论的样本量 —— 退出码 2，表示「还没有结论」。")
            return 2
        print()
        print("加 --apply 把上面两个偏移写进个人模型。只在样本够（≥20）时才写。")
        return 0

    # ---------- 写回 ----------
    changed, notes = [], []
    if d_off is not None and len(dp) >= MIN_FIT:
        old = _num(model.get("difficulty_offset"), 0.0)
        model["difficulty_offset"] = d_off
        model["fit_n"] = len(dp)
        changed.append(f"难度偏移 {old:+.2f} → {d_off:+.2f}")
    elif d_off is not None:
        notes.append(f"难度偏移有建议值 {d_off:+.2f}，但只有 {len(dp)} 条，没写")

    if r_off is not None and len(rp) >= MIN_FIT:
        old = _num(model.get("retention_offset"), 0.0)
        model["retention_offset"] = r_off
        changed.append(f"留存偏移 {old:+.2f} → {r_off:+.2f}")
        # 实测留存明显高于目标，就把目标下调（间隔拉长）；反之收紧
        got = sum(a for _, a in rp) / len(rp)
        target = _num(store.data.get("request_retention"), sch.DEFAULT_RETENTION)
        if got - target > 0.05:
            new_t = round(clamp(target - 0.02, 0.80, 0.95), 2)
            if new_t != target:
                store.data["request_retention"] = new_t
                store.save()
                changed.append(f"目标留存 {target:.0%} → {new_t:.0%}（实测 {got:.0%}，间隔会变长）")
        elif target - got > 0.05:
            new_t = round(clamp(target + 0.02, 0.80, 0.95), 2)
            if new_t != target:
                store.data["request_retention"] = new_t
                store.save()
                changed.append(f"目标留存 {target:.0%} → {new_t:.0%}（实测 {got:.0%}，间隔会变短）")
    elif r_off is not None:
        notes.append(f"留存偏移有建议值 {r_off:+.2f}，但只有 {len(rp)} 次，没写")

    print()
    for n in notes:
        print(f"- {n}")
    if not changed:
        print("没有可写的东西：样本不够。模型保持原样。")
        return 2
    model["updated"] = today()
    save_model(root, model)
    print(f"已写入 {root / '_coach' / 'model.json'}：")
    for c in changed:
        print(f"  - {c}")
    print("下一题起生效。")
    return 0


# ---------- 召回：什么都不给，看还想得起吗 ----------

RECALL_STEPS = [
    ("是什么", "一句话说清它是什么。不许用这个词自己解释自己。"),
    ("什么时候失效", "什么情况下它会失效、不成立，或者你会用错。"),
    ("举个例子", "举一个你自己写过的例子；没有就现编一个真实场景。"),
    ("和谁有关", "它和「{other}」是什么关系，谁决定谁。"),
]


def recall_prompt(node: dict) -> list:
    """一次回忆要覆盖的四步。不是四个角度，是一段完整回忆的骨架。"""
    pre = (node.get("prereq") or [])
    other = pre[0] if pre else "它的前置概念"
    return [f"  {i}. {name}：{hint.format(other=other)}"
            for i, (name, hint) in enumerate(RECALL_STEPS, 1)]


def _streak(dates: set) -> int:
    """连续学习天数：从今天往回数，今天没有就从昨天开始数。"""
    if not dates:
        return 0
    cur = _date(today())
    if cur.isoformat() not in dates:
        cur -= sch.dt.timedelta(days=1)
    n = 0
    while cur.isoformat() in dates:
        n += 1
        cur -= sch.dt.timedelta(days=1)
    return n


def activity_dates(session: Path) -> set:
    out = {r.get("at") for r in AttemptLog(session).rows() if r.get("at")}
    for n in sch.NodeStore(session).live():
        for e in n.get("events", []):
            if e.get("at"):
                out.add(e["at"])
    return out


def _recall_limit(m: dict) -> int:
    """回忆限时：按这个概念的答题用时中位数放大三倍，夹在 60–180 秒。"""
    med = m.get("median_sec") or 0
    return int(clamp(round(3 * med), 60, 180)) if med else 90


def cmd_recall(session: Path, concept: str = "", n: int = 1,
               as_json: bool = False, show_key: bool = False) -> int:
    """今天的无提示回忆。答之前不给任何线索 —— 包括评分标准。

    有提示的题，题干本身就是线索；无提示回忆没有线索，能想起来才算真的进了长时记忆。
    它也比重刷三道题省时间：一分钟能试三个概念，想不起来的立刻暴露。
    """
    store = sch.NodeStore(session)
    if not store.live():
        print("概念库是空的。先跑 stage.py sync。")
        return 2

    rows = AttemptLog(session).rows()
    gsec = global_seconds(session.parent)
    bank = ItemBank(session)

    if concept:
        node = store.get(concept)
        if node is None:
            print(f"没有「{concept}」这个概念。现有："
                  + "、".join(x["name"] for x in store.live()[:12]))
            return 2
        m = mastery(concept_attempts(rows, node["name"]), node, gsec)
        queue = [{**node, "R": m["R"], "overdue": 0, "blocked_by": []}]
    else:
        queue = store.due_list()
        if not queue:
            later = sorted((x for x in store.live() if x["reps"] and x.get("due")),
                           key=lambda x: x["due"])
            msg = f"今天（{today()}）没有到期的召回。"
            if later:
                x = later[0]
                d = (_date(x["due"]) - _date(today())).days
                msg += f"下次：{x['name']}（{x['due']}，还有 {d} 天）。"
            else:
                msg += "还没有复习中的概念 —— 先走完一轮闭环。"
            print(msg)
            return 0

    picked = queue[:max(1, n)]

    if as_json:
        payload = []
        for item in picked:
            m = mastery(concept_attempts(rows, item["name"]), item, gsec)
            rub = []
            for it in bank.for_concept(item["name"]):
                rub = it.get("rubric") or []
                if rub:
                    break
            payload.append({
                "concept": item["name"], "R": item.get("R"), "S": item["S"],
                "due": item.get("due"), "last": item.get("last"),
                "overdue": item.get("overdue"), "limit_sec": _recall_limit(m),
                "prompts": recall_prompt(item), "rubric": rub,
                "state": m["state"], "gaps": m["gaps"],
                "prereq": item.get("prereq") or [],
            })
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    total = len(queue)
    item = picked[0]
    m = mastery(concept_attempts(rows, item["name"]), item, gsec)
    limit = _recall_limit(m)
    r = item.get("R")
    r_txt = f"现在还能想起的概率 {r:.0%}" if r is not None else "还没测过留存"
    print(f"【召回 1/{total}】{item['name']}　{r_txt}")
    if item.get("overdue"):
        extra = f"，已经忘过 {item['lapses']} 次" if item.get("lapses") else ""
        print(f"逾期 {item['overdue']} 天{extra}")
    print()
    print("规则：不看书、不翻笔记、不问我。先讲，讲完再对。")
    print(f"限时 {limit} 秒，按这四步讲：")
    for ln in recall_prompt(item):
        print(ln)
    print()
    print("评三档：四步都说到 = 对（3）· 说了一半 = 勉强（2）· 想不起来 = 错（1）")
    print("记：")
    print(f'  coach.py record {session} --concept "{item["name"]}" --angle 回忆 '
          f'--grade 对 --seconds {limit} --confidence 4 --evidence "他的原句"')
    rub = []
    for it in bank.for_concept(item["name"]):
        rub = it.get("rubric") or []
        if rub:
            break
    if rub:
        if show_key:
            print()
            print("评分标准（答完才能看，答之前看等于给自己提示）：")
            for x in rub[:5]:
                print(f"  - {x}")
        else:
            print(f"题库里有这道题的 {len(rub)} 条评分标准。答完再加 --key 看 —— 现在看等于给你提示。")
    if total > 1:
        print()
        print(f"还有 {total - 1} 个到期。做完这一个再跑一次 —— 一次只做一个。")
    return 1


def cmd_today(session: Path) -> int:
    """今天该做什么：把到期、补弱、新概念、连续天数压成一句话。"""
    store = sch.NodeStore(session)
    if not store.live():
        print("概念库是空的。先在第 2 阶把概念表写出来，再跑 stage.py sync。")
        return 2
    rows = AttemptLog(session).rows()
    gsec = global_seconds(session.parent)
    due = store.due_list()
    cands = candidates(session, store, rows, gsec=gsec)
    buckets = {}
    for c in cands:
        buckets.setdefault(c["bucket"], []).append(c)

    st = _streak(activity_dates(session))
    print(f"【今天】{today()}" + (f"　连续第 {st} 天" if st else ""))
    print()
    if due:
        print(f"到期召回   {len(due)} 个（约 {sch.workload_minutes(len(due))} 分钟）")
    else:
        print("到期召回   0 个")
    if buckets.get("remediation"):
        c = buckets["remediation"][0]
        print(f"补弱       {len(buckets['remediation'])} 个（{c['node']['name']}：{c['why']}）")
    if buckets.get("practice"):
        print(f"练熟       {len(buckets['practice'])} 个")
    if buckets.get("new"):
        names = "、".join(c["node"]["name"] for c in buckets["new"][:3])
        print(f"可开新概念 {len(buckets['new'])} 个（{names}）")

    ret = store.measured_retention()
    if ret["unmeasured"]:
        print()
        print(f"⚠ 已经到期但没做的复习 {ret['unmeasured']} 次 —— 这些不算「忘了」，算「没测」。"
              "欠着不做，间隔会一直往前滚。")

    # 跳过前置直接学后置，是最常见的「学得动但接不上」的来源
    holes = []
    for n in store.live():
        if n["reps"] > 0:
            miss = [p for p in n.get("prereq_ids", [])
                    if store.nodes.get(p, {}).get("reps", 0) == 0]
            if miss:
                holes.append((n["name"], [store.nodes[p]["name"] for p in miss]))
    if holes:
        name, pre = holes[0]
        print()
        print(f"⚠ 你已经在学「{name}」，但它依赖「{'、'.join(pre)}」还没编码 —— "
              "现在能答对，多半是靠背下来的，换个场景就会断。")

    print()
    if due:
        top = due[0]
        r = top.get("R")
        if r is not None:
            drop = ""
            if top.get("S"):
                tomorrow = sch.forgetting_curve(top.get("overdue", 0) + 1, top["S"])
                drop = f"，拖到明天约 {tomorrow:.0%}"
            print(f"现在做这一件：召回「{top['name']}」（{r:.0%} 还记得{drop}）")
        else:
            print(f"现在做这一件：召回「{top['name']}」")
        print(f"  coach.py recall {session}")
        print()
        print("今天不先开新概念。到期的先清完 —— 复习欠账会和今天的新内容抢同一段记忆。")
        return 1
    if buckets.get("remediation"):
        c = buckets["remediation"][0]
        print(f"现在做这一件：补弱「{c['node']['name']}」—— {c['why']}")
        print(f"  coach.py next {session}")
        return 1
    if buckets.get("new"):
        c = buckets["new"][0]
        print(f"现在做这一件：开始「{c['node']['name']}」")
        print(f"  coach.py next {session}")
        return 1
    if buckets.get("practice"):
        c = buckets["practice"][0]
        nxt = sorted((n for n in store.live() if n["reps"] and n.get("due")),
                     key=lambda n: n["due"])
        when = f"下次召回「{nxt[0]['name']}」是 {nxt[0]['due']}" if nxt else "还没有排下次召回"
        print(f"今天不用练「{c['node']['name']}」—— {c['why']}。")
        print(f"{when}。")
        print()
        print("同一天再多练，不会让它记得更久；让它记得更久的是间隔。今天可以停。")
        print(f"真想加练就跑 coach.py next {session}，它会挑最该动的那一个。")
        return 0
    print("没有到期、没有待补弱、没有新概念 —— 今天什么都不用做。")
    print(f"想加练就跑 coach.py next {session}，它会挑最该动的那一个。")
    return 0


def cmd_alias(session: Path, target: str, aliases: list) -> int:
    """给概念登记别名。同一概念换写法，连错判定和掌握度会当成两个。"""
    store = sch.NodeStore(session)
    node = store.get(target)
    if node is None:
        print(f"没有「{target}」这个概念。现有："
              + "、".join(n["name"] for n in store.live()[:12]))
        return 2
    got = store.add_alias(node["name"], aliases)
    if not got.get("aliases"):
        print("没加进去：别名和本名归一后一样，或者本来就是空的。")
        return 2
    store.save()
    print(f"「{node['name']}」的别名：" + "、".join(got["aliases"]))
    print("以后 record / 连错判定 / 掌握度都按同一个概念算。")
    return 0


def cmd_report(session: Path) -> int:
    store = sch.NodeStore(session)
    rows = AttemptLog(session).rows()
    bank = ItemBank(session)
    if not store.live():
        print("概念库是空的，先 sync。")
        return 2
    gsec = global_seconds(session.parent)

    ms = {n["name"]: mastery(concept_attempts(rows, n["name"]), n, gsec) for n in store.live()}
    print(f"概念 {len(store.live())} 个 · 答题 {len(rows)} 次 · 题库 {len(bank.items)} 道")
    print(f"已掌握 {sum(1 for m in ms.values() if m['mastered'])}"
          f" · 当场会 {sum(1 for m in ms.values() if m['practiced'] and not m['mastered'])}"
          f" · 在学 {sum(1 for m in ms.values() if m['n'] and not m['practiced'])}"
          f" · 未开始 {sum(1 for m in ms.values() if not m['n'])}")
    print()
    print(_pad("概念", 14) + _rpad("掌握", 6) + _rpad("角度", 6)
          + _rpad("难度", 5) + _rpad("稳定", 6) + _rpad("想起", 6) + "  状态")
    print("-" * 74)
    for node in sorted(store.live(), key=lambda n: n["name"]):
        m = ms[node["name"]]
        r = "—" if m["R"] is None else f"{m['R']:.0%}"
        name = node["name"][:12]
        print(f"{_pad(name, 14)}{m['score']:>6.2f}{len(m['angles']):>4}/6"
              f"{m['max_level']:>5}{m['S']:>6.1f}{r:>6}  {m['state']}")
        if m["n"] and not m["mastered"] and m["gaps"]:
            print(f"{'':<14}└ 还差：{'、'.join(m['gaps'])}")

    gaps = []
    for node in store.live():
        for a in ANGLES:
            if a not in ms[node["name"]]["angles"]:
                gaps.append((node["name"], a))
    if gaps:
        print()
        print(f"没过关的角度共 {len(gaps)} 个，前 6 个：")
        for name, a in gaps[:6]:
            print(f"  - {name} · {a}：{ANGLE_HINT[a]}")
    return 0


def cmd_profile(root: Path) -> int:
    """跨主题的学员档案：你长期是什么毛病。"""
    events = read_jsonl(root / "_coach" / "events.jsonl")
    if not events:
        print(f"{root / '_coach' / 'events.jsonl'} 还没有记录。答过题之后再来。")
        return 2

    topics = sorted({e.get("topic", "") for e in events})
    print(f"跨 {len(topics)} 个主题 · 共 {len(events)} 次答题")
    print()

    print("按角度：你在哪种问法下最容易错")
    print(f"{'角度':<8}{'次数':>6}{'正确率':>9}")
    for a in ANGLES:
        sub = [e for e in events if e.get("angle") == a]
        if not sub:
            continue
        acc = sum(1 for e in sub if int(e.get("grade", 1)) >= 3) / len(sub)
        print(f"{a:<8}{len(sub):>6}{acc:>9.0%}")
    print()

    print("按难度：你卡在哪一级")
    print(f"{'难度':<6}{'次数':>6}{'正确率':>9}{'用时中位':>10}")
    for lv in range(1, 6):
        sub = [e for e in events if int(e.get("level", 0)) == lv]
        if not sub:
            continue
        acc = sum(1 for e in sub if int(e.get("grade", 1)) >= 3) / len(sub)
        secs = [int(e["seconds"]) for e in sub if e.get("seconds")]
        med = int(statistics.median(secs)) if secs else 0
        print(f"{lv:<6}{len(sub):>6}{acc:>9.0%}{(str(med) + ' 秒') if med else '—':>10}")

    conf = [(int(e["confidence"]), int(e.get("grade", 1)) >= 3)
            for e in events if e.get("confidence")]
    if conf:
        bias = sum(0.2 * c - (1.0 if ok else 0.0) for c, ok in conf) / len(conf)
        print()
        print(f"校准偏差 {bias:+.2f}"
              + ("　→ 你系统性地高估自己" if bias > 0.1
                 else "　→ 低估自己，可以更敢跳" if bias < -0.1 else "　→ 判断挺准"))

    wrong = [e for e in events if int(e.get("grade", 1)) == 1]
    if wrong:
        from collections import Counter
        top = Counter(e.get("concept", "") for e in wrong).most_common(5)
        print()
        print("跨主题反复答错的概念：")
        for name, cnt in top:
            print(f"  - {name}：错 {cnt} 次")

    ev = [e for e in events if e.get("evidence")]
    print()
    print(f"凭证率 {len(ev) / len(events):.0%}（带用户原话的答题占比，低于 50% 说明判分靠印象）")
    return 0


# ---------- 入口 ----------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="取题引擎：下一个问什么、问多难、从哪个角度问",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""用法:
    python coach.py today   <会话目录>              # 今天该做什么（只有一个动作）
    python coach.py next    <会话目录> [--json]      # 下一道题：概念/角度/难度/理由
    python coach.py recall  <会话目录> [--n 1] [--json] [--key]
                                                     # 无提示回忆：什么都不给，看还想得起吗
    python coach.py record  <会话目录> --concept "闭包" --grade 对 \\
                            --angle 边界 --level 3 --seconds 42 \\
                            --confidence 4 --evidence "他的原句" [--item q1]
    python coach.py add     <会话目录> --concept "闭包" --angle 边界 --level 3 \\
                            --stem "题面" --rubric "要点一;要点二"
    python coach.py sync    <会话目录>              # 本轮答题写进 04-knowledge-map.md 的回测表
    python coach.py report  <会话目录>              # 每个概念的两级状态与缺口
    python coach.py calibration <会话目录> [--apply]  # 两个模型对账；--apply 写回个人偏差
    python coach.py alias   <会话目录> --concept "闭包" --as "JS 闭包" "js闭包"
                                                     # 同一概念的别名（换写法不再算两个）
    python coach.py profile <学习根目录>            # 跨主题：你长期是什么毛病

取题桶: review 到期 / remediation 补弱 / practice 练熟 / new 新概念 / resume 续做
角度:   定义 / 边界 / 反例 / 操作 / 关系 / 迁移（回忆另算，不占角度覆盖）
难度:   1 认得出 · 2 说得出 · 3 用得上 · 4 改得动 · 5 造得出
掌握:   当场会（六角度·难度≥3）→ 已掌握（再加跨天提取≥2 天 + S≥7 天）

退出码: 0 正常 / 1 有到期召回 / 2 用法或数据错误
""")
    ap.add_argument("cmd", choices=[
        "today", "next", "recall", "record", "add", "sync",
        "report", "profile", "calibration", "alias",
    ])
    ap.add_argument("session", help="会话目录（profile 时是学习根目录）")
    ap.add_argument("--json", action="store_true", help="next 输出机器可读 JSON")
    ap.add_argument("--concept", default="", help="概念名")
    ap.add_argument("--angle", default="", help="出题角度")
    ap.add_argument("--level", type=int, default=0, help="难度 1-5")
    ap.add_argument("--stem", default="", help="题面")
    ap.add_argument("--rubric", default="", help="评分标准，分号隔开")
    ap.add_argument("--key", default="", help="参考答案（只在需要时写）")
    ap.add_argument("--item", default="", help="题目 id")
    ap.add_argument("--grade", default="", help="1-4 或 错/勉强/对/轻松")
    ap.add_argument("--seconds", type=int, default=0, help="答题用时（秒）")
    ap.add_argument("--confidence", type=int, default=0, help="答题前的把握 1-5")
    ap.add_argument("--evidence", default="", help="用户原话片段（收据）")
    ap.add_argument("--n", type=int, default=1, help="recall 一次列几个（默认 1）")
    ap.add_argument("--apply", action="store_true", help="calibration 时把偏差写回个人模型")
    ap.add_argument("--as", dest="aliases", nargs="*", default=None,
                    help="alias 时的别名列表")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    path = Path(args.session).expanduser().resolve()
    if not path.exists():
        print(f"目录不存在: {path}")
        return 2

    if args.cmd == "today":
        return cmd_today(path)
    if args.cmd == "next":
        return cmd_next(path, args.json)
    if args.cmd == "recall":
        return cmd_recall(path, args.concept, args.n, args.json, bool(args.key))
    if args.cmd == "sync":
        return cmd_sync(path)
    if args.cmd == "calibration":
        return cmd_calibration(path, args.apply)
    if args.cmd == "alias":
        if not args.concept or not args.aliases:
            print('用法：coach.py alias <会话> --concept "闭包" --as "JS 闭包"')
            return 2
        return cmd_alias(path, args.concept, args.aliases)
    if args.cmd == "report":
        return cmd_report(path)
    if args.cmd == "profile":
        return cmd_profile(path)
    if args.cmd == "add":
        if not args.stem.strip():
            print("要给 --stem 题面")
            return 2
        return cmd_add(path, args.concept, args.angle, args.level,
                       args.stem, args.rubric, args.key)
    if not args.concept:
        print("要给 --concept 概念名")
        return 2
    return cmd_record(path, args.concept, args.item, args.grade, args.seconds,
                      args.confidence, args.evidence, args.angle, args.level)


if __name__ == "__main__":
    raise SystemExit(main())
