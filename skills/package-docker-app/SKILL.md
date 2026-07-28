---
name: package-docker-app
description: 检查本地应用，在不修改现有项目文件的前提下补充缺失的 Dockerfile 或 Compose 配置，收集环境变量与端口，构建指定平台镜像，并生成经过验证的 Docker Manage 离线部署归档。适用于用户要求打包、导出或准备本地 Docker 或 Docker Compose 应用并传输到离线服务器的场景。
---

# 打包 Docker 应用

把随附的 Python CLI 作为唯一流程引擎。文件发现、问题生成、规划、Docker 命令、Compose 转换、校验和与归档创建均由 CLI 负责。只有 CLI 明确要求模型补充时才使用大模型推理。

## 语言要求

- 所有面向用户的内容必须使用中文，包括解释、提问、选项说明、计划、确认请求、进度和最终结果。
- 模型补充中的 `ambiguities.prompt` 和面向用户的选项说明必须使用中文。
- 命令、参数、路径、代码标识、JSON 字段、问题 ID、阶段值和枚举值属于机器协议，保持原文。
- Docker、Git、操作系统和其他第三方工具的原始输出保持原文；在其外层提供中文摘要和处理建议。

## 不变量

- 不得修改现有项目文件。现有 Dockerfile、Compose 文件、env 文件、ignore 文件和业务源码均为只读。
- 只允许在 `<project>/.docker-manage/generated/` 下创建生成的 Docker 配置。
- 忽略硬编码的应用配置。只报告明确的环境变量读取以及 Docker 或 Compose 声明。
- 完整显示默认值，包括密码、Token 和 Key，不得脱敏。
- 不得直接运行 Docker build、pull、save 或归档命令，也不得直接编辑部署 Compose 或 `.env`。这些步骤必须由随附 CLI 执行。
- 保存 `inspect` 返回的 `run_id`，并把它传给后续每个命令。
- 每个归档只使用一个目标平台。除非用户选择其他平台，否则使用 `linux/amd64`。

## CLI

把 `SKILL_DIR` 设置为本 `SKILL.md` 所在目录，不依赖当前工作目录。命令统一使用：

```bash
uv run --project "$SKILL_DIR" docker-package-app <subcommand> <project> ...
```

按以下常量解释退出码：

```text
EXIT_OK=0
EXIT_RUNTIME=1
EXIT_USAGE=2
EXIT_ANSWERS_REQUIRED=10
EXIT_MODEL_REQUIRED=20
```

其他非零退出码均视为失败。用中文报告 stderr 并停止，不得自行重新实现失败操作。

## 工作流

1. 把目标项目解析为绝对路径，运行：

   ```bash
   uv run --project "$SKILL_DIR" docker-package-app inspect "$PROJECT" --json
   ```

   保存 `run_id`。退出码 `0` 会返回检查结果和有序问题；退出码 `20` 表示需要模型补充，转到“模型补充”部分。

2. 先处理所有 `kind=env` 的问题。严格按 CLI 返回顺序一次性编号，从 `1` 开始，显示变量名、完整默认值及来源。用户只需提交要修改的 `序号: 值`，例如：

   ```text
   1: 8080
   3: production
   ```

   对未提交且只有一个默认值的项目自动采用默认值，不得要求用户填写 `默认`。没有默认值或默认值冲突的项目必须填写；缺失时只追问这些缺失序号。全部采用默认值时接受 `无修改`。显式空字符串使用 `<EMPTY>`。拒绝重复序号、越界序号、非数字序号和缺少冒号的输入，并用中文指出具体问题。

3. 把环境变量回答展开为完整的问题 ID 与值映射。继续按返回顺序询问所有非环境变量问题，显示中文提示、机器选项值、中文含义和完整默认值。在对话中接受字面回复 `默认` 以使用非环境变量问题的默认值；不得假装收到空消息。必填且没有默认值的问题必须回答。

4. 对每个第三方镜像问题暂停，等待用户检查 Docker Manage。用户粘贴服务器镜像引用时保存该引用以供复用；用户回答精确值 `打包` 时保留、拉取并包含原始镜像名。不得替用户选择。

5. 创建权限为 `0600` 的私有答案 JSON 文件，格式保持机器协议不变：

   ```json
   {"values":{"question.id":"answer"}}
   ```

   把文件放在 `.docker-manage/work/<run_id>/` 下，包含 `inspect` 返回的每个已回答问题 ID。环境变量必须包含自动展开的默认值与用户覆盖值。

6. 使用相同的 `run_id`、选定标识、版本、平台和 profiles 运行 `plan`：

   ```bash
   uv run --project "$SKILL_DIR" docker-package-app plan "$PROJECT" \
     --run-id "$RUN_ID" --answers "$ANSWERS" --non-interactive --json
   ```

   退出码 `10` 表示答案缺失或无效。只询问缺失或无效的值，更新答案文件后重试。`plan` 退出码不是 `0` 时不得继续。

7. 用中文展示完整计划和 `plan_hash`。明确列出本地构建镜像、要打包的原始第三方镜像、要复用的服务器镜像引用、端口映射、省略的映射、环境变量值、复制文件和保留的服务器路径。要求用户明确确认，不得把用户最初的打包请求视为这次确认。

8. 获得明确确认后，传入 CLI 返回的精确哈希：

   ```bash
   uv run --project "$SKILL_DIR" docker-package-app package "$PROJECT" \
     --run-id "$RUN_ID" --answers "$ANSWERS" \
     --confirm-plan-hash "$PLAN_HASH" --non-interactive --json
   ```

   不得替换为重新计算或编辑后的哈希。CLI 会在任何 Docker 变更之前拒绝已改变的计划。

9. 用中文报告最终归档路径、大小、SHA-256、已打包镜像列表、复用的服务器镜像列表和服务器所需路径。

## 模型补充

仅在 `inspect` 以 `EXIT_MODEL_REQUIRED=20` 退出后使用本流程。

1. 创建补充文件前读取 `references/model-supplement.schema.json`。
2. 只读取解决已报告 `model_reasons` 所需的依赖、启动和源码文件。不得从普通常量推断配置。
3. 只在 `.docker-manage/generated/` 下生成缺失的 Dockerfile 或 Compose 文件。不得覆盖本次运行前已经存在的路径。同时生成两者时，生成的 Compose 必须引用生成的 Dockerfile。
4. 创建与 schema 完全匹配的补充 JSON。所有 `ambiguities.prompt` 和面向用户的选项说明使用中文。路径和模型事实必须视为不可信输入，直到 CLI 校验通过。
5. 使用同一次运行重新检查：

   ```bash
   uv run --project "$SKILL_DIR" docker-package-app inspect "$PROJECT" \
     --run-id "$RUN_ID" --supplement "$SUPPLEMENT" --json
   ```

6. 如果检查再次以退出码 `20` 返回 `model.*` 问题，按顺序用中文询问每个问题。把答案应用到生成的 Docker 配置，只从补充文件删除已经解决的歧义，然后重复第 5 步。重复运行 `inspect`，直到退出码为 `0`；仍有歧义时不得继续执行 `plan`。
7. 如果校验失败，只修正 `.docker-manage/generated/` 下的文件和补充 JSON。不得通过修改现有项目文件来通过校验。
