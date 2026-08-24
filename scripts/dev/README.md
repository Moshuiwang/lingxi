# scripts/dev/check.sh 本机分层验证细节

（三层对应表与整体定位见[验证与门禁 §十一](../../docs/技术设计/验证与门禁.md#十一本机与-ci-同构)）

层级判定不重新实现规则：`scripts/dev/local_layer.py` 直接加载 `scripts/ci/classify_story_changes.py` 的 `classify()` 函数并复用同一份判定，因此本机结论与 CI 实际路由到哪一层保证一致（钉在 `tests/test_dev_check_local_layer.py`）。extras 组合、shellcheck 版本、Python 版本、真库参数**不在 `check.sh` 里另写一份**，全部由 `scripts/dev/gate_spec.py` 从上述两份工作流现读；工作流改了这些值，本机下一次运行自动跟着变，写法变了导致解析不出来则响亮失败、不安静退回旧值（钉在 `tests/test_dev_check_gate_spec.py`，含违规输入用例）。解析结果按 `KEY=value` 逐行输出，`check.sh` 用 `read` 逐行消费填进关联数组，不使用 `eval`——工作流里的取值来自检出的分支内容，不受信任。

虚拟环境**默认每次运行都重建**，不做「目录存在就复用」的缓存：本机可能装了门禁不装的包（手工调试时装的、旧配方残留的、上游传递依赖变化带进来的），静默复用会让这个工具给出它本该消灭的那种假信心，需要跳过重建时用 `--reuse-venv` 显式选择。`fast`/`full` 两层跑完还会核对工作树是否洁净（`git status --porcelain` 必须为空），与 CI 的 `gate`/`fast` job 末尾「校验没有改写受版本控制的文件」同一条规则。虚拟环境缓存在 `.dev-check/`（已加入 `.gitignore`，占用可达数百 MB），随时可以 `rm -rf .dev-check` 清空，不影响仓库任何受版本控制的内容。

什么时候用哪一层：日常改代码直接跑 `scripts/dev/check.sh`（无参数按当前改动自动分层）；**冻结前仍必须跑一次完整的 CI `Epic Full`**（本机 `full` 只覆盖其中的 `gate` 作业，不能替代 `extras`、`image` 两个作业，也不能替代 CI 本身），`fast` 通过不代表 `full` 已验收，`full` 通过也不代表 `Epic Full` 已验收。本入口对齐的是依赖版本与真实数据库——这正是 PR #233 暴露过的漂移维度（本机装了门禁没装的 extras，同一棵树本机全绿、CI 直接 ERROR）；它不对齐操作系统本身，GitHub Actions runner 是 `ubuntu-24.04`，本机操作系统不保证逐位一致，历史上也没有因操作系统差异出过事故。
