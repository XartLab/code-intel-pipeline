# configs 基线 fixture

`configs-v*` 发布的验收入口（`.github/scripts/ci.py configs-acceptance`）用这里的输入，
在干净环境里回答两个问题：配置包是否真的是被测二进制读取的那份配置，以及
`evidence.graph` 的 8 MiB 边界是否仍然存在且方向正确。

| 路径 | 用途 |
|---|---|
| `repo/` | 代表性下游仓库。验收把它复制成独立 git 仓库，跑 `--mode normal`，断言 `outcome=completed`、`exitCode=0`、`failureNode=null`，并且 `code_evidence.agent_slice` 与 `diagnosis.hospital-view` 产物可读。 |
| `evidence-request.json` | `code-intel-evidence-admissibility-request.v1` 的 good 形状（取自 `tests/fixtures/evidence-admissibility/good/`）。验收按用例改 `observation.payload.sha256` 后写入用例目录。 |
| （运行时生成） | `payload.json` 不入库：10 MiB 的合法 evidence payload 覆盖历史 8 MiB 默认上限，64 MiB + 1 KiB 覆盖合同上限仍然拒绝。入库会把仓库撑到几十 MB。 |

边界的两侧都断言，是因为只测“大 payload 通过”会让“合同上限被一起放大”这种回归也变绿。
历史缺陷（#123 / #508）正是扫描器在合同预算之下又套了一层固定 8 MiB 默认值。