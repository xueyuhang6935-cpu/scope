# GitHub 上传说明

上传范围为当前 `Scope` 仓库。上层个人简历、原始数据、旧项目、设计备份不属于本次上传范围。

## 已准备的文件

- 根目录 `README.md`：项目定位、架构、功能、评测边界与文档入口。
- `docs/getting-started.md`：跨平台安装、账号和数据准备限制。
- `docs/runtime-reference.md`：原 README 的运行说明与历史资料，原相对链接已调整。
- `.env.example`：保留现有占位配置，不复制真实 `.env`。
- `.gitignore`：补充 SQLite3、数据库转储、本地凭据和编辑器文件排除。
- `.gitattributes`：文本换行与常见二进制文件规则。

没有创建 LICENSE：授权方式需由项目权利人选择。公开可见不等于获得任意使用、修改或再分发授权，第三方依赖、图片、字体和数据集仍遵循各自条款。

## 本次检查范围（2026-10-28，北京时间）

检查当前 Git 跟踪文件与未忽略的新文件；补充文件后以最终核验结果为准。

- 初查 407 个候选路径，没有超过 10 MiB 的文件。
- 对可读取 UTF-8 文本检查常见 OpenAI/GitHub/AWS 令牌形态和私钥头，未命中；此检查不是完整密钥审计。
- 历史角色文档中发现一处个人邮箱，已替换为匿名说明；业务角色事实保留。
- 当前索引中环境文件只有 `.env.example`，未跟踪真实 `.env`。
- 未检查完整 Git 提交历史中的凭据、个人信息或已删除的大文件；直接推送会上传当前分支可达的历史，公开前须检查。
- 未重新安装依赖、启动服务、运行构建或业务回归；本次是文档与发布文件准备。

最终复查：当前存在的候选文件共 405 个，无超过 10 MiB 文件；上述三类有限模式均未命中。新增展示与操作文档的本地链接全部可解析，`.env`、数据库、依赖、测试产物和本地凭据的忽略探针通过；`.env.example` 与两个依赖锁文件仍可上传。跟踪文件的 `git diff --check` 通过。此结果不覆盖完整历史、任意形式的密钥或第三方授权核查。

后续上传检查补充：对当时 HEAD 可达的 316 个历史 blob 完成敏感文件名、大文件和上述令牌/私钥模式检查，无命中；不是完整历史隐私或密钥审计。用户已指定远程为 `https://github.com/xueyuhang6935-cpu/scope`，当前 HTTPS 访问因缺少 GitHub 登录凭据失败，详见 [上传记录](../plan/20261028-github-upload.md)。

历史文档中的 `test-results/`、`data/` 和上层目录证据链接有意保留，但这些内容不随仓库发布，部分链接将不可访问；不能据此宣称证据附件已公开。

## 提交与推送

在 GitHub 创建空仓库，建议先选 Private，不自动添加 README 或许可证。

在本地仓库根目录检查：

```bash
git status --short
git add --dry-run .
```

确认文件范围后暂存并审阅：

```bash
git add .
git diff --cached --stat
git diff --cached
git commit -m "feat: prepare logistics investigation project for GitHub"
```

仓库已有大量业务改动，`git add .` 会同时暂存这些改动，不仅是本次新增文档。若只提交部分文件，应逐项指定路径；但首次上传要包含当前物流实现，不能只提交 README。

使用自己的 Git 姓名与邮箱；可选择 GitHub 设置提供的 noreply 邮箱，不在示例中代填个人身份。Git 提交元数据中的邮箱也会随历史上传。

将下列 URL 替换为自己的空仓库地址：

```bash
git remote add origin https://github.com/YOUR_USERNAME/scope-logistics-agent.git
git push -u origin HEAD
```

当前已有 Git 仓库，无需 `git init`。以上命令上传当前分支；不要强制推送或覆盖现有远程历史。首次推送后确认 GitHub 默认分支，再添加仓库描述和主题标签。

建议描述：`基于 FastAPI、React 和 LangGraph 的物流风险调查与人机协同应用，支持时点快照、证据校验、审批和内部跟进。`

建议 Topics：`langgraph`、`fastapi`、`react`、`typescript`、`logistics`、`llm`、`agent`。
