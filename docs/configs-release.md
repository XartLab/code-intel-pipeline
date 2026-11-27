# configs-v* 配置基线发布合同

`configs-v*` 是一条与二进制发行（`v*`）平行的发布线，只认证**配置面**：
`orchestration/`、`templates/`、`overlays/`、`skills/`、`pipeline.config.json`、
`pipeline.config.example.json`。它解决的是「配置与契约的变更没有可核验的供应链输入」，
不解决「某个二进制版本有 bug」——后者只能靠发布带修复的二进制。

对应工单：#389。首个版本：`configs-v0.1.0`。

## 交付物

一个非 draft 的 GitHub Release，tag 匹配 `configs-v<semver>`，含五个资产：

| 资产 | 说明 |
|---|---|
| `code-intel-configs-<version>.tar.gz` | 配置包。从 `git archive HEAD` 生成，不含工作树内容；成员带 `code-intel-configs-<version>/` 前缀。 |
| `code-intel-configs-<version>.manifest.json` | 机器可读清单：`configSchema`、`configsVersion`、producer commit / tree / 生成时间、Code Intel 兼容区间、包内每个文件的 `sha256` 与字节数、每个额外资产的摘要。 |
| `code-intel-configs-<version>.sha256` | 校验文件：配置包、manifest、每个额外资产的 SHA-256。 |
| `verify_configs_release.py` | 干净环境校验器（纯标准库）。与仓库内 `scripts/verify_configs_release.py` 字节相同。 |
| `acceptance.json` | CI 验收入口的结果（`code-intel-configs-acceptance.v1`），含逐项检查与诊断。 |

manifest 的字段形状由 `code-intel-configs-manifest.v1` 固定，改字段就是改合同，
必须同时改 `scripts/verify_configs_release.py` 与 `tests/test_configs_release.py`。

## 兼容区间

唯一来源是 `configs/version.json` 的 `codeIntel` 块：

```json
"codeIntel": {
  "minVersion": "0.7.2-beta.7",
  "maxExclusive": "0.8.0",
  "requiresIssue": 123,
  "requiresFix": true,
  "note": "..."
}
```

- 比较用 SemVer 优先级（semver.org §11），不是字符串序、也不是数字前缀：
  `0.7.2-beta.10 > 0.7.2-beta.7`，`0.8.0-alpha.1 < 0.8.0`。
- **没有已发布二进制满足这个区间**。`v0.7.1` 与 `v0.7.2-beta.6` 都早于 #123 的修复
  `d131f3f`，`v0.7.1` 上仍会复现 `failureNode=evidence.graph`、
  `diagnostic="JSON input exceeds 8388608 bytes"`。配置包不修复二进制，这一点在 release
  notes 里显式声明。
- 区间之外一律 fail closed：校验器非零退出，并指出「实际版本 / 声明下限 / 原因」。
  绝不静默接受旧版本。

## 信任模型

校验器给的是**完整性 + 来源绑定**，不是身份证明：

1. 摘要证明资产在传输与存储中没有被改动或漂移；
2. 把配置包与「producer commit 处的仓库内容」逐文件比对，证明它确实产自那个提交
   （这一步是唯一把资产绑到源码修订上的检查，缺了它，一份自洽的伪造包能通过所有摘要检查）；
3. 兼容区间证明消费者手里的二进制是否被允许消费这份配置。

需要更强的来源证明时，用 release 上的 build provenance attestation：

```bash
gh attestation verify code-intel-configs-0.1.0.tar.gz --repo XartLab/code-intel-pipeline
```

发布线使用「合并到 main 即授权」，而不是二进制线的签名 tag（`docs/RELEASE_SIGNING.md`）。
这是**更弱**的授权策略，写在这里而不是隐含：配置基线由消费者校验器兜底，二进制的信任根
仍然只有签名 tag。

## 消费

```bash
# 1. 干净目录里下载并校验；2. 顺便把门禁装在二进制上
python scripts/verify_configs_release.py \
    --tag configs-v0.1.0 \
    --dir "$(mktemp -d)" \
    --code-intel "$(command -v code-intel)"
```

退出码：`0` 通过，`1` 校验失败（stderr 给可定位诊断），`2` 用法/环境错误。

诊断示例：

```
error: code-intel 0.7.1 is below the declared minimum 0.7.2-beta.7. This baseline
requires a Code Intel binary with the issue 123 fix (minVersion 0.7.2-beta.7); the
configuration package cannot repair an older installed binary.
```

其他可定位诊断：`asset <name> sha256 is <实际> but <manifest> declares <声明>`、
`bundle files differ from the repository at the producer commit: <路径>`、
`release <tag> assets differ from the expected set: missing=[...] unexpected=[...]`。

只校验不下载（CI 内、已组装好的目录）：

```bash
python scripts/verify_configs_release.py --tag configs-v0.1.0 --dist dist \
    --expect-commit "$GITHUB_SHA" --no-source
```

查看某个候选版本是否落在区间内（release notes 由它渲染）：

```bash
python scripts/verify_configs_release.py --classify \
    --against configs/version.json --versions 0.7.1 0.7.2-beta.6 0.7.2-beta.7
```

## 发布流程

`.github/workflows/configs-release.yml`：

| 触发 | 行为 |
|---|---|
| push 到 main 且改动落在配置面 | 构建 → 验收 → 打包 → 草稿发布 → 净环境校验 → 转正式 |
| 同一批路径的 pull_request | 同一条门禁，干跑，`contents: read`，不发布 |
| `workflow_dispatch`（仅 main） | 对当前 `configs/version.json` 重跑；tag 与版本文件不一致直接失败 |

- 版本号只由 `configs/version.json` 决定。改版本号 = 发布授权；审阅者在 diff 里能看到。
- 发布作业先建 **draft**，挂上资产后从空目录下载并校验；只有校验通过才
  `--draft=false` 转正。校验失败时不会留下一个「看起来已接受」的公开 release。
- 上传不使用 `--clobber`：已存在的资产不会被静默替换。
- 校验日志与 acceptance 报告作为该次 run 的 artifact 保存，run 链接写进 release notes。

## 验收入口

`.github/scripts/ci.py configs-acceptance` 在干净目录里做四件事：

1. `code-intel --version` 落在 manifest 声明区间内；
2. `code-intel orchestrate validate --json` 解析出的 manifest 路径，等于**解压出来的配置包**
   里的 `orchestration/integrations.json`（而不是构建时的 checkout）；
3. 在 fixture 仓库上跑 `--mode normal`，要求 `outcome=completed`、`exitCode=0`、
   `failureNode=null`，并且 `code_evidence.agent_slice` 与 `diagnosis.hospital-view` 产物可读；
4. evidence payload 边界：`10 MiB` 的合法 payload 通过（历史默认上限是 8 MiB），
   `64 MiB + 1 KiB` 仍被拒绝（合同上限没有被一起放大）。

输入在 `tests/fixtures/configs-baseline/`。payload 不入库，运行时生成。

## 下游接入门（k-atana）

在 `configs-v*` 可校验、且其声明区间内有已发布二进制之前，k-atana 的对应任务保持 blocked。
接入时只做三件事：锁版本与摘要、跑上面的校验器、跑一次 normal 消费 smoke。
不在 k-atana 内复制或修补上游实现。