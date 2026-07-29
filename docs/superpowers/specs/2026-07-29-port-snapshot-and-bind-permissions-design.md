# Docker Manage 端口快照与挂载权限设计

日期：2026-07-29

## 目标

更新 `package-docker-app` skill 及其 CLI，使其满足两个行为：

- 最近一次完整成功打包所选择的端口暴露状态和宿主机映射端口会保存到项目的 `.docker-manage` 目录，并在下一次检查时作为默认值复用。
- 复制进部署归档的 bind mount 内容统一使用任意 UID 可读写的权限：目录 `0777`、普通文件 `0666`。

环境变量继续由 `.docker-manage/.env` 保存；端口使用独立的
`.docker-manage/ports.json`，避免把 CLI 内部状态混入业务环境变量命名空间。

## 当前问题

当前配置模块只读写 `.docker-manage/.env` 和 `PackagePlan.environment`。端口问题每次都由 Compose 或 Dockerfile 中重新发现的 `PortCandidate` 生成，因此用户上次选择的宿主机端口不会进入下一次 `inspect`。

bind mount 内容通过 `shutil.copy2()` 和 `shutil.copytree()` 复制。这两个接口保留源文件权限，而载荷根目录又以 `0700` 创建，因此离线服务器上的任意 UID 容器用户不一定能读写挂载内容。

## 端口快照

### 文件格式

新增 `.docker-manage/ports.json`，使用版本化的确定性 JSON：

```json
{
  "schema_version": 1,
  "ports": [
    {
      "service": "web",
      "container_port": 8000,
      "protocol": "tcp",
      "exposed": true,
      "host_port": 8322
    }
  ]
}
```

端口身份由 `service`、`container_port` 和 `protocol` 组成。记录按这三个字段排序。`exposed=false` 时 `host_port` 必须为 `null`；`exposed=true` 时 `host_port` 必须是有效端口号。

文件权限固定为 `0600`，父目录 `.docker-manage` 继续保持 `0700`。尽管端口本身不属于秘密，这与当前配置快照的私有状态策略保持一致。

### 读取规则

CLI 在确定性端口发现和模型补充完成后读取端口快照，只匹配本次已经发现的端口：

- 快照中不存在匹配项时，保持 Compose 或 Dockerfile 的当前默认行为。
- 匹配项 `exposed=true` 时，端口暴露问题默认选择 `yes`，宿主机端口问题默认使用快照中的 `host_port`。
- 匹配项 `exposed=false` 时，暴露问题默认选择 `no`；隐藏的宿主机端口问题仍不要求答案。
- 快照中的未知服务、已删除端口或不同协议条目忽略，不生成新问题。
- 当前端口值与 Compose 声明值不同不算冲突；提示同时展示当前配置值和声明值，默认优先采用当前配置值。

检查时读取到的当前选择保存在 `Inspection` 中，使同一个 `run_id` 的后续 `plan` 不受快照文件中途变化影响。

### 写入规则

端口快照从最终确认并实际执行的 `PackagePlan.ports` 完整生成，不保留已从项目中删除的旧条目。

写入时机与环境变量快照一致：

1. Docker 构建、拉取和导出完成。
2. 部署 Compose、制品 `.env`、manifest 和校验和验证完成。
3. 最终归档创建并验证成功。
4. CLI 更新 `.docker-manage/.env` 和 `.docker-manage/ports.json`，然后把任务转换为 `packaged`。

两个快照先分别写入同目录临时文件并 `fsync`。替换前保留旧内容；任一替换失败时恢复已经替换的另一个文件，并返回中文 `PackageError`。这提供进程内的回滚语义，避免正常错误路径留下环境变量和端口来自不同打包任务的配置。

`inspect`、`plan`、`--dry-run`、等待模型补充以及归档完成前失败的任务不得更新端口快照。

### 数据模型

为 `PortCandidate` 增加可选的当前选择字段，包含 `exposed` 和 `host_port`。字段缺失时维持旧状态文件的兼容行为。

端口问题生成器负责选择提示和默认值，规划器仍只消费完整答案，不直接重新读取项目级快照。

## bind mount 权限

### 适用范围

权限规范化只适用于选择复制进归档的 bind mount：

- bind source 是目录：该目录及其所有子目录设为 `0777`。
- bind source 是普通文件：设为 `0666`。
- bind 目录内的所有普通文件设为 `0666`。
- 符号链接本身不跟随，不修改链接指向的目标。

Compose `configs` 和 `secrets` 不自动放宽，继续保留源权限，避免把敏感文件变为所有用户可写。named volume 和选择保留服务器路径的外部 bind mount 不在本地复制，因此 CLI 不修改其权限。

### 执行位置

权限只修改 `.docker-manage/work/<run_id>/payload/files/` 下的副本，不修改原项目文件或外部服务器路径。

在依赖复制完成后、生成 manifest、校验和及归档之前规范化权限。这样 tar 成员保存的模式就是部署服务器解包后需要的模式。载荷根目录、部署 Compose、制品 `.env`、manifest、校验和、镜像归档和最终归档继续使用现有私有权限，不因 bind mount 的要求而放宽。

如果权限修改失败，CLI 返回中文 `PackageError` 并停止打包，不生成一个权限不确定的成功制品。

## Skill 契约更新

`SKILL.md` 增加以下规则：

- `.docker-manage/ports.json` 表示最近一次完整成功打包使用的项目级端口配置。
- 模型不得直接编辑该文件，只能由随附 CLI 读取和原子更新。
- 展示端口问题时明确区分当前端口配置与 Docker 声明值，并优先采用当前配置。
- 计划确认仍完整列出最终端口映射和省略的映射。
- 复制的 bind mount 目录和普通文件分别使用 `0777`、`0666`；原始项目文件不修改。

## 错误处理与兼容性

- `ports.json` 不存在：视为空快照，现有端口行为不变。
- 路径不是普通文件、JSON 无法解析、schema 版本未知、字段无效或端口越界：返回中文 `UsageError`，不静默回退。
- 旧 `state.json` 没有当前端口字段：仍能执行规划。
- 端口快照中的未知条目忽略。
- 快照写入失败：CLI 恢复已经替换的旧快照，任务不标记为 `packaged`；如果恢复本身失败，错误必须列出未恢复的具体路径并要求人工检查。
- bind mount 权限修改失败：任务失败，原项目权限保持不变。
- 答案 JSON、问题 ID、命令参数、计划哈希和归档目录结构保持兼容。

## 测试

### 单元测试

- 端口快照不存在时保持发现结果。
- 匹配键采用服务、容器端口和协议三元组。
- 已暴露与未暴露选择都能往返保存和读取。
- 未知条目不生成新端口。
- 无效 JSON、未知 schema、无效端口和不一致字段被拒绝。
- 快照输出排序稳定且权限为 `0600`。
- 当前端口成为问题默认值，同时保留声明值提示。
- 旧 `PortCandidate` JSON 在没有当前字段时仍可读取。
- 复制的 bind 目录递归为 `0777`，普通文件为 `0666`。
- bind 内符号链接不跟随，外部目标权限不变。
- `config` 和 `secret` 副本保持原权限。

### 集成与端到端测试

- 第一次完整打包选择新宿主机端口，随后断言 `ports.json` 保存该选择。
- 第二次 `inspect` 展示该值为当前配置，默认展开后计划继续使用该值。
- 选择不暴露的端口在下一次检查中继续默认不暴露。
- `inspect`、`plan`、dry-run 和失败打包不修改已有端口快照。
- 归档中的 bind 目录成员模式为 `0777`，普通文件成员模式为 `0666`。
- 模拟第二个快照替换失败，断言第一个快照恢复为旧内容。

### Skill 验证

- 更新 skill contract 测试，覆盖端口快照、成功写入时机和 bind 权限约束。
- 运行 skill 目录结构校验、Ruff、类型检查和全量 pytest。

## 非目标

- 不从历史 `.docker-manage/work/<run_id>/state.json` 推断当前端口。
- 不持久化应用名、版本、目标平台或第三方镜像选择。
- 不修改项目原始 bind source 的权限。
- 不修改选择保留的服务器路径权限。
- 不为 named volume 预设宿主机权限。
- 不把端口快照写入部署归档或业务 Compose `.env`。

## 完成标准

- 最近一次成功打包选择的端口暴露状态和宿主机端口会被下一次检查优先复用。
- 未成功完成的任务不会改变当前端口配置。
- 复制进归档的 bind mount 内容在解包后满足目录 `0777`、普通文件 `0666`。
- 原项目文件、Compose `configs`、Compose `secrets` 和外部服务器路径权限不受影响。
- 现有项目在没有 `ports.json` 时行为保持兼容。
- 全量自动化测试和 skill 校验通过。
