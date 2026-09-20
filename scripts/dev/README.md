# scripts/dev/check.sh 本机分层验证细节

（整体定位与门禁语义见[验证与门禁 §十一](../../docs/技术设计/验证与门禁.md#十一本机与-ci-同构)；本文只写实现细节与本机操作细节，不重复门禁含义。）

`scripts/dev/check.sh`（Issue #236）架在 `scripts/ci/verify_repository.sh` / `scripts/ci/verify_docs.sh` 之上：建出与门禁逐位一致的依赖环境，并按改动路径只跑 CI 会对本次改动跑的那一层。它不改变 `verify_repository.sh` 的语义，后者仍是冻结前的权威入口。

## 三层各自跑什么、环境从哪来

| 本机层级 | 等价的 CI 结论 | 环境从哪来 | 不做什么 |
| --- | --- | --- | --- |
| `docs` | `Story / docs`、`Epic Full / docs` | 无需安装 | 只跑 `verify_docs.sh`，不装依赖、不起真库或 Docker |
| `fast` | `Story / code fast` | extras/shellcheck/ruff/Python 版本现读自 `story.yml` 的 `fast` job | 不启动真库、不构建镜像、不跑 Node 依赖校验（`workers/oauth-bridge` 自有 `package-lock.json`） |
| `full` | `Epic Full / gate`（仅此一个作业） | extras/shellcheck/ruff/Python 版本、真库镜像与认证方式现读自 `ci.yml` 的 `gate` job；额外起同配方真库容器 | 不构建四镜像、不做部署契约核对与双路径构建比对（见 `Epic Full / image`）；不跑 `extras` 六条，也不跑 Node 依赖校验 |

## 判定与取值都不另写一份

层级判定不重新实现规则：`scripts/dev/local_layer.py` 直接加载 `scripts/ci/classify_story_changes.py` 的 `classify()` 函数并复用同一份判定，因此本机结论与 CI 实际路由到哪一层保证一致（钉在 `tests/test_dev_check_local_layer.py`）。extras 组合、shellcheck 版本、ruff 版本、Python 版本、真库参数**不在 `check.sh` 里另写一份**，全部由 `scripts/dev/gate_spec.py` 从上述两份工作流现读；工作流改了这些值，本机下一次运行自动跟着变，写法变了导致解析不出来则响亮失败、不安静退回旧值（钉在 `tests/test_dev_check_gate_spec.py`，含违规输入用例）。解析结果按 `KEY=value` 逐行输出，`check.sh` 用 `read` 逐行消费填进关联数组，不使用 `eval`——工作流里的取值来自检出的分支内容，不受信任。`verify_repository.sh` 额外用现读到的 `RUFF_VERSION` 与 PATH 上实际的 `ruff --version` 交叉核对，不一致直接判红（防 PATH 上还有另一个未预期版本的 ruff 抢先命中）。ruff 规则集本身（select/ignore/per-file-ignores）住在 `pyproject.toml`，不是从工作流现读的对象。

## 虚拟环境与工作树判定

虚拟环境**默认每次运行都重建**，不做「目录存在就复用」的缓存：本机可能装了门禁不装的包（手工调试时装的、旧配方残留的、上游传递依赖变化带进来的），静默复用会让这个工具给出它本该消灭的那种假信心，需要跳过重建时用 `--reuse-venv` 显式选择。虚拟环境缓存在 `.dev-check/`（已加入 `.gitignore`，占用可达数百 MB），随时可以 `rm -rf .dev-check` 清空，不影响仓库任何受版本控制的内容。

`fast`/`full` 两层跑完会做一次工作树判定，与 CI 的 `gate`/`fast` job 末尾「校验没有改写受版本控制的文件」同一条规则。**它比较的是「开跑前的快照」与「跑完后的状态」，不是要求工作树为空**（Issue #261）：脚本在做任何事之前先记一次 `git status --porcelain` 作为基线（`initial_git_status_snapshot`），跑完后再取一次，**只把相对该基线新增的条目判红**；开跑前就已经存在的未提交改动是 dev-loop 下的正常状态，照原样放行并在输出里点名。因此这条检查**不证明「文件内容零变化」**，只证明「验证过程本身没有改写受版本控制的文件」——想确认工作树干净要自己另跑 `git status`。

## 什么时候用哪一层

日常改代码直接跑 `scripts/dev/check.sh`（无参数按当前改动自动分层）；**冻结前仍必须跑一次完整的 CI `Epic Full`**（本机 `full` 只覆盖其中的 `gate` 作业，不能替代 `extras`、`image` 两个作业，也不能替代 CI 本身），`fast` 通过不代表 `full` 已验收，`full` 通过也不代表 `Epic Full` 已验收。本入口对齐的是依赖版本与真实数据库——这正是 PR #233 暴露过的漂移维度（本机装了门禁没装的 extras，同一棵树本机全绿、CI 直接 ERROR）；它不对齐操作系统本身，GitHub Actions runner 是 `ubuntu-24.04`，本机操作系统不保证逐位一致，历史上也没有因操作系统差异出过事故。

## 远程串行完整门禁（`scripts/dev/remote_check.sh`）

候选冻结前那一次 `full` 可以不占本机：`scripts/dev/remote_check.sh [<提交>]` 把指定提交（默认 `HEAD`，只检提交、不检工作树）送到一台可配置的 Linux 主机，在那里的检出上跑**同一个** `scripts/dev/check.sh`，再把结论收回来。门禁本体、配方现读、一次性真库容器都是 `check.sh` 自己的，本脚本不改分层、不改断言、不分片，也不替你传 `--reuse-venv`；结论与本机 `full` 同构，但同样**只等价于 `Epic Full / gate`**，不替代 CI。主机、用户、连接方式、目录、容器名全部来自配置，脚本里一处不写死。

**怎么跑**：复制 [`remote.env.example`](remote.env.example) 为 `<仓库根>/.dev-check/remote.env`（gitignored），改两项必填（`LINGXI_REMOTE_SSH` 连接命令整串、`LINGXI_REMOTE_WORKDIR` 远端检出目录），然后 `scripts/dev/remote_check.sh <sha>`。默认传输是 `git bundle` 经同一条 SSH 送过去（候选冻结后、推送 GitHub 之前就能跑，不要求远端能访问 GitHub），`LINGXI_REMOTE_TRANSPORT=fetch` 改为远端自己 `git fetch origin`（只适用于已推送的提交）。远端全文日志回传到 `.dev-check/remote-runs/<sha>-<UTC时刻>.log`，最后一行是结论摘要：`结论=<通过|失败> 提交=<sha> 用时=<秒> Ran=<N> 峰值内存=<KB|未知> 日志=<路径>`。退出码：0 通过；远端 `check.sh` 非零时**原样透传**；78 配置错误；69 连不上 / 预检失败 / 远端中断；74 传输或日志回传失败；70 结果判定失败（解析不到 `Ran N`、残留容器、远端工作树变脏）。SSH 断开、远端中断、日志回不来、解析不到 `Ran N`，一律非零，不判绿。

**目标主机一次性开通清单**（通用，不绑定具体机器；这是唯一需要真人做的部分）：

| 项 | 要求 | 为什么 |
| --- | --- | --- |
| 系统 | Linux x86_64（与 CI runner、生产同架构；Ubuntu 24.04 与 CI runner 同系统最省心） | `full` 对齐的是依赖版本与真库，系统级差异不在门禁范围内，但架构不同会让 wheel 选择漂移 |
| Docker | 已安装，登录用户能直接 `docker run`（在 `docker` 组） | `check.sh full` 起一次性 `postgres` 容器 |
| Python | 与 `ci.yml` 现读一致的版本化二进制（当前 `python3.12`）+ `venv` 模块带 `pip` | `check.sh` 建 venv 时优先找版本化二进制，版本不符响亮失败 |
| git | 支持 `git -C`、`git bundle`、按 SHA `fetch` | 传输与 `checkout --detach` |
| bash | 5.x 在 PATH 上（登录 shell 可以是任何 POSIX shell） | 远端步骤以 `bash -c` 执行，`check.sh` 自身也要 bash 4.3+ |
| `/usr/bin/time` | GNU time（`time` 包） | 取 `Maximum resident set size` 作内存峰值；缺了脚本仍能跑，峰值记「未知」 |
| `timeout` | coreutils | 只有配置了 `LINGXI_REMOTE_TIMEOUT_SECONDS` 才需要 |
| SSH 接入 | 本机那条 `LINGXI_REMOTE_SSH` 命令能**免交互**连上（公钥或 Tailscale SSH），登录用户即上面装好 Docker 的用户 | 脚本每次运行要连同一主机五六次，任何一次要口令就等于挂死 |
| 仓库检出 | `git clone <仓库> <目录>` 一次，之后保持工作树干净 | 脚本每次 `checkout --detach` 到目标提交，脏了拒绝 |
| 资源 | 磁盘：venv 数百 MB + `postgres:16-alpine` 镜像；内存：`full` 单进程峰值实测约 850 MB | 低于 2 GiB 内存的主机请配 swap |

**换主机**：只改配置，不改代码——在新主机做完上表 → `git clone` 一个检出 → 再写一份 `.dev-check/remote-b.env`（只有 `LINGXI_REMOTE_SSH` 与 `LINGXI_REMOTE_WORKDIR` 不同）→ `LINGXI_REMOTE_ENV_FILE=.dev-check/remote-b.env scripts/dev/remote_check.sh <sha>`。同一台机器上多份检出（例如给两条批次分别留一个）也是同样的办法。「配置不写死」的证明方式：换主机全程只新增一份配置文件，仓库 `git diff` 为空；脚本里唯一的主机字样是用法说明里的占位 `gate-host`。

**残留清理责任表**（谁建谁清）：

| 东西 | 谁建 | 谁清、怎么清 |
| --- | --- | --- |
| 本机 bundle 与临时 ref（`.dev-check/remote-tmp.*`、`refs/lingxi-remote-check/<sha>`） | 本脚本 | trap 自删；异常中断后若有残留：`rm -rf .dev-check/remote-tmp.*`、`git update-ref -d refs/lingxi-remote-check/<sha>` |
| 远端 bundle 与临时目录（`<workdir>/.dev-check/remote-tmp.*`） | 本脚本 | trap 自删；SSH 断开导致没删成时脚本会打印远端路径，到远端 `rm -rf` 即可 |
| 远端一次性真库容器（`LINGXI_REMOTE_PG_NAME`） | `check.sh` | `check.sh` 自清；本脚本结束时 `docker ps -a` 回读一次，残留即非零并提示 `docker rm -f <名>` |
| 远端检出与 venv（`<workdir>`、`<workdir>/.dev-check/venv-full`） | 开通者 | **保留**：检出留给下一次直接 `checkout`，venv 是否复用由 `check.sh` 自己的规则决定（默认每次重建）。不再用这台主机时 `rm -rf <workdir>` 即回到开通前 |
| 本机日志（`.dev-check/remote-runs/*.log`） | 本脚本 | 保留作证据，随时可删；gitignored |
