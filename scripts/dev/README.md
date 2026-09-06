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
