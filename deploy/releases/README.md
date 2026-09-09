# 版本发布操作

状态：发布分支方案已由产品负责人在 [#669](https://github.com/Moshuiwang/lingxi/issues/669) 批准；实际工作流与设置验证结果在该工作项登记。

## 从开发到候选

main 持续研发，功能范围齐备时从明确提交创建 `release/X.Y`，以后只接收该版本需要的修复。release 分支通过 PR 与 `Epic Full` 后合并；`Release Candidate` 独立打包四镜像并创建 `vX.Y.Z-rc.N` 预发布。N 来自该工作流运行序号，可能不连续。main 合并不再打包。

包内 `pyproject.version` 是目标正式版本 `X.Y.Z`；候选序号属于发布记录，不写进包内版本。由此同一份候选镜像能提升为正式版，不需要为去掉 rc 改源码或重打包。候选身份始终由完整提交、镜像摘要与候选标签共同决定，不能只看包内版本判断可否上生产。

预发布附件 `release-manifest.json` 的新 schema 2 保存源码、四镜像摘要、迁移头、构建记录和非秘密控制包摘要；`lingxi-control.tar` 在同一候选提交只打包一次，包索引 schema revision 1 精确列出文件、权限、来源和摘要。上传中断可继续，既有不同内容不覆盖。正式版本标签和附件发布后不得改写；有变更建立新候选。

## 验收后提升

验收必须针对附件里的四镜像。使用匹配版本的独立测试库；同一测试机器人只允许一个客户端。必须核对实际前后版本的恢复路径，不能把旧代码可以启动当作业务回退成功。

验收者在 `acceptance/<候选标签>.json` 留一份小记录，PR 合入 main，代码所有者为产品负责人。记录字段如下（占位符不能直接用于提升）：

```json
{
  "schema": 1,
  "candidate_tag": "v2.4.0-rc.42",
  "manifest_sha256": "候选附件归一化后的完整 SHA256",
  "result": "passed",
  "evidence_url": "https://github.com/Moshuiwang/lingxi/issues/工作项号#issuecomment-评论号",
  "recovery": {
    "previous_tag": "v2.3.2",
    "instructions": "实际可执行的恢复步骤与数据库兼容边界",
    "evidence_url": "https://github.com/Moshuiwang/lingxi/issues/工作项号#issuecomment-评论号"
  }
}
```

摘要计算使用 `release_manifest.fingerprint()` 的固定 JSON 排序与换行规则；直接对预发布附件运行 `sha256sum` 可得到同值。记录是有权限人员对证据的确认，不是系统自行证明业务答案正确；无真实证据不得填 passed。不适用的真实旅程须在证据记录逐项解释，由代码所有者审查。

在 GitHub Actions 选择 `Release Promotion`、分支 main、填候选标签。工作流先只读核对，再将同四镜像及同摘要控制包发布为 `vX.Y.Z`，全程不构建、不部署。候选没通过、记录缺失或不一致均拒绝。

## 选择部署版本

发布者先将 `LINGXI_GH_COMMAND` 设置为本机已批准 GitHub 机器身份入口的绝对路径；缺失时拒绝执行，不能回落个人 gh 登录。GitHub Actions 内使用作业原生令牌。随后只读查 Release 并生成不含凭据的镜像变量：

```sh
python3 scripts/ci/release_manifest.py resolve \
  --repository Moshuiwang/lingxi --tag v2.4.0 \
  --environment production --output /tmp/lingxi-release-images.env \
  --manifest-output /tmp/lingxi-release-manifest.json
```

测试环境将 environment 改为 stage，可选择 rc。生产拒绝 rc，且必须回读正式版本所引用的候选、成功构建与 main 中的验收记录。该命令不修改现有配置、不接触数据库、不运行 Compose。部署器复用此入口形成完整固定清单；使用旧命令直接指定镜像的人工操作无法由本脚本拦截，不得声称已存在覆盖所有生产入口的强制控制。

**首次切换以前的历史正式标签**没有此附件，不能伪装成新流程验证过的正式版。既有生产继续运行；历史灾备回退按原发布记录单独核对。不要为让选择器通过补造历史验收记录。

## 热修与版本退役

常规修复先 main，再只同步独立修复到维护线；紧急修复可先从实际生产标签开始，在维护线发布补丁后补 main 与其他受支持版本。发布记录登记每条修复的同步去向，不能把整个 main 合入旧维护线。

当前只维护实际生产线及准备上线的下一版。旧版停止支持后不再更新分支，保留正式标签、镜像和验收记录；不在本任务删除历史分支或镜像。

## 固定计划与一次批准

新部署入口为控制包内 `deploy/lingxi_deploy.py`。首次引导由独立 Ops 安装：核对包摘要、使用固定源码、建立版本化只读目录及私有状态/主机契约目录；不下载或执行最新 main 脚本。部署安装主体为 root，管理只读代码和引用；受限 relay 主体无 sudo/Docker 权限。完整格式与运行条件见[控制包契约](../control/README.md)。真实 stage、正式提升和生产首用仍各自待授权及验收。

下面路径与 ID 都由本次 Ops 计划给定；批准材料必须 0600，命令不会替人生成批准。四操作使用同一组固定输入参数：

```sh
python3 "$CONTROL_ROOT/deploy/lingxi_deploy.py" \
  --host-contract "$HOST_CONTRACT" --public-config "$PUBLIC_CONFIG" \
  --state-directory "$STATE_DIRECTORY" plan --request "$REQUEST" --dry-run
```

确认完整差异与窗口后，去掉 `--dry-run` 保存计划；随后将尾部改为 `apply "$PLAN_ID" --approval "$APPROVAL"` 执行。断线后用 `status "$PLAN_ID"` 只读查进度，同一 `apply` 接续。完成后再次 apply 只核对、不重建。恢复需另存 operation=recover 且引用原部署的计划，并使用 `recover "$RECOVERY_PLAN_ID" --approval "$RECOVERY_APPROVAL"`；不能复用 apply 批准。

新 schema 缺控制包直接拒绝。旧 schema 1 的选择必须显式 `--allow-legacy`，仅作历史核对；历史恢复记录另绑定原部署、原四镜像、发布证据、兼容证明、恢复工具包和入口停用收据；没有新清单的原始标签使用计划内 historical 类型，不捏造维护分支或构建编号，不向历史 Release 补造验收清单。存在新格式阶段或动态名单但无兼容消费者时不能恢复旧版。

正式记录另外保存提升时 main 提交/运行编号、验收文件路径/提交/摘要，业务候选提交保持不变。批准前可以回读 main 资格；接续只使用原窗口及固定制品，明确撤销记录会阻止继续。发布包不含任何私有环境文件、真实公钥或业务数据。
