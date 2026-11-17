# 一句话安装

把这个仓库装进你自己的 WorkBuddy，说这一句话就够：

> 帮我把 https://github.com/mike1-cmd/efficient-learning 装成技能

装完**新开一个对话**，说「我要学 XX」，第一句回答必须是 `【第 0 阶 · 定靶】`。

---

## 下面是给 AI 看的执行步骤

用户把上面那句话发给你时，按这五步做，一步都不许省。

### 1. 取到技能目录

优先用 git（快，以后能升级）：

```bash
git clone --depth 1 https://github.com/mike1-cmd/efficient-learning /tmp/efficient-learning
```

没有 git，就下载 zip：

```bash
curl -L -o /tmp/el.zip https://github.com/mike1-cmd/efficient-learning/archive/refs/heads/main.zip
# 解压后目录名是 efficient-learning-main
```

两条路都不通（网络问题）时，直接告诉用户失败原因，不要假装装好了。

### 2. 复制到技能目录

```bash
# Windows
DEST="$HOME/.workbuddy/skills/efficient-learning"
# macOS / Linux 同上，$HOME 就是家目录

mkdir -p "$(dirname "$DEST")"
rm -rf "$DEST"
cp -r /tmp/efficient-learning "$DEST"
```

**必须检查目标路径长这样**，不能把 `SKILL.md` 摊在 `skills/` 根下：

```
~/.workbuddy/skills/efficient-learning/SKILL.md
~/.workbuddy/skills/efficient-learning/scripts/stage.py
```

### 3. 校验装对了

```bash
python -c "import sys,pathlib; p=pathlib.Path.home()/'.workbuddy/skills/efficient-learning/SKILL.md'; t=p.read_text(encoding='utf-8'); print('frontmatter ok' if t.startswith('---') and 'name: efficient-learning' in t else 'BROKEN')"
```

输出不是 `frontmatter ok` 就停下来查，别往下走。

### 4. 跑一遍自测（这一步不能跳）

```bash
cd ~/.workbuddy/skills/efficient-learning && python scripts/selftest.py
```

要看到 `✓ 全部通过：295 项断言`。

- `python` 版本低于 3.10 → 找 WorkBuddy 自带的：`~/.workbuddy/binaries/python/versions/*/python`
- 报错 → 把原始报错贴给用户，不要自己改代码

### 5. 告诉用户怎么开始

> 装好了，路径是 `~/.workbuddy/skills/efficient-learning`。
> **新开一个对话**（当前对话读不到新技能），说「我要学 XX」。
> 第一句应该是 `【第 0 阶 · 定靶】`，没出现就是没加载上。

---

## 为什么必须新开对话

WorkBuddy 在会话启动时加载技能清单，当前会话的清单是固定的。
装完不新开，会出现「装好了但叫不出来」——这是最常见的误判，先说清楚。

---

## 装完是什么样

你不用记命令。AI 会按六阶带你走，每阶结尾只给你一个动作。
命令都是它在后台跑的，你只在它要产物的时候给它东西。

想自己看进度，两个命令够用：

```bash
cd ~/.workbuddy/skills/efficient-learning
python scripts/coach.py today <你的学习目录>    # 今天做这一件
python scripts/coach.py report <你的学习目录>   # 每个概念到什么程度了
```

学习数据全在你自己的 `./learn/` 目录里，跟技能目录分开，升级技能不会丢。
