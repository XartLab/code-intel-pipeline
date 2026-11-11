# 工厂常驻循环（factory/bin/loop）运行说明

`factory/bin/loop` 是工厂的常驻循环：它自己不用模型、不做判断，模型只在它起的 worker 里跑。
每一轮：`job queue` 看每张 `factory` 单；对 `new` 的单，在在途单少于 `--slots` 时 `job create`；
对每张在途单（`resume`、`stalled`）`job drive`；`job drive` 打印的每个 `DISPATCH` 交给后端起 worker；
然后等下一轮。本文只陈述 `factory/bin/loop` 的文档字符串与 `--help`、`factory/bin/job --help` 里能核对的事实。

文中 `<仓库>` 指装了工厂的检出目录，`<单号>` 指 `factory/jobs/` 下的单子目录名。

## 1. 前提

- WSL 或 Linux。原生 Windows 不行（`job init` 的自检会拒绝）。
- Python 3.15 以上：`factory/bin/job init` 的自检里有 `python >= 3.15` 检查。
- `gh` 已登录。
- Orca 在跑：`orca status --json` 输出里的 `result.runtime.state` 是 `ready`。

## 2. 自检

```bash
factory/bin/job doctor    # 起跑前检查；有 FAIL 先修
factory/bin/job queue     # 每张 factory 单的下一步
```

## 3. 空跑一轮

```bash
factory/bin/loop <仓库> --backend print --agent claude --once
```

`print` 后端只打印每个 `DISPATCH`，不起任何 worker。但这一轮里 `job create` 仍会读 GitHub 上 issue 的内容和标签，
并在本地 `factory/jobs/<单号>/` 建单；它不写 GitHub。

## 4. 真跑

```bash
factory/bin/loop <仓库> --backend orca --agent claude --slots 1
```

- `--slots`（默认 3）是 loop 自己的在途单上限。loop 不读 `factory/config.json` 的 `concurrency`。
- `--backend` 和 `--agent` 不给时，取 `factory/config.json` 里 `loop` 一节的 `backend` 和 `agent`
  （本仓库是 `orca` 和 `claude`）；两处都没有，loop 以 2 退出并说明要补什么。
- `--interval` 是两轮之间的秒数，默认 120。`--once` 只跑一轮就退出。
- 一个仓库只跑一个 loop。脚本本身不加锁，这是约定。

## 5. 去哪看

- `factory/bin/job summary`：所有单的表（issue、阶段、PR、花费、用时、intake 模型）。
- `factory/jobs/.loop-heartbeat`：每轮写一个 UTC 时间戳；整轮失败的那一轮不写。
- `factory/jobs/<单号>/dispatch.jsonl`：已起 worker 的回执。
- `factory/jobs/<单号>/events.jsonl`：阶段变化。
- `factory/jobs/<单号>/pending.jsonl`：额度耗尽而没起成的 `DISPATCH`，下一轮先试。

`factory/jobs/` 在 `.gitignore` 里（`/factory/jobs/`），以上文件都只在本机。

## 6. 常见日志

| 日志 | 含义 |
| --- | --- |
| `not started: orca exited N` | `worker-start` 失败；这个 worker 没起，下一轮重试。 |
| `not started: usage limit` | 额度耗尽；这一轮不再起 worker，没起的 `DISPATCH` 记进该单的 `pending.jsonl`，下一轮先试。 |
| `not started: no .claude/agents/<role>.md` | 缺这个角色的提示文件。 |
| `ERROR round:` | 整轮失败，不写心跳；下一轮重试，`--once` 时以 1 退出。 |
| `STOP` | 轮到人了：日志里的这一行表示下一步要人来做。 |

loop 不修复任何东西：出错时由人读日志。
