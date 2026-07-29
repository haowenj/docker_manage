# Docker Manage bind mount 服务器数据保留设计

日期：2026-07-29

## 目标

更新 `package-docker-app` skill 及其 CLI，使项目目录内的 bind mount
不再无条件复制进离线部署归档。每个 bind source 都必须在计划阶段明确选择：

- `copy`：复制本机内容到归档，并把部署 Compose 改写到归档内路径。
- `keep_server_path`：不复制本机内容，部署 Compose 保留原挂载路径。
- `abort`：中止本次打包。

这让配置目录仍可随包发布，同时允许数据库文件、上传文件和其他持久数据继续
使用服务器上的现有内容。

## 当前问题

当前实现只询问项目目录外的本地文件依赖。解析后位于项目目录内的 bind
mount 会在规划时自动设为 `copy`，打包时整个目录进入 `files/`，部署
Compose 的 source 也改写为 `./files/...`。

因此 `./data:/app/data` 这类挂载会把开发电脑中的 `data/` 放进归档。
用户在服务器的同一目录重复解压归档时，归档内同名成员会覆盖服务器文件，
导致测试环境原有数据被本机数据替换。

Docker named volume 目前不会作为文件依赖复制，不受这个问题影响。

## 方案选择

### 采用：逐个 bind source 明确选择

对项目内和项目外的每个不同 bind source 都生成文件处理问题。项目内路径
提供 `copy`、`keep_server_path` 和 `abort`；项目外路径继续只允许
`keep_server_path` 或 `abort`，因为 CLI 不允许复制项目目录外的内容。

这是采用方案，因为它不依赖目录名、容器目标路径或镜像类型进行猜测，计划中
的行为也能被用户直接核对。

### 未采用：按目录名自动识别数据目录

可以默认跳过名为 `data`、`uploads`、`storage` 的目录，但自定义目录会漏判，
同名配置目录也可能被误判。该方案不能可靠保证服务器数据安全。

### 未采用：默认保留所有 bind mount

这最保守，但会使应用配置、模板和静态资源不再随包发布，改变现有归档的主要
用途。逐项选择能保留两类需求。

## 问题与计划模型

### 问题生成

问题按解析后的 source 路径去重并稳定排序。提示必须展示：

- 使用该 source 的服务。
- Compose 中的原始 source。
- 解析后的本机绝对路径。
- 路径是否位于项目内。
- 估算大小。
- 每个选择的中文含义。

项目内 bind 的默认值为 `copy`，保持现有用户在接受默认值时的兼容行为；
数据目录必须显式选择 `keep_server_path`。项目外 bind 不设置默认值，继续
要求明确选择。

Compose `configs.file` 和 `secrets.file` 不新增保留选项，继续自动复制。
它们不是运行时可写数据目录，并且部署所需文件必须存在于制品中。

### 文件身份

处理决定不能只用解析后的路径作为内部身份。同一路径可能被不同服务引用，
也可能同时作为 bind、config 或 secret 使用。计划和物化阶段使用包含
`service`、`kind`、原始 Compose 值和解析路径的稳定身份，避免 bind 的
`keep_server_path` 冲掉 config 或 secret 的 `copy` 决定。

同一个 bind source 被多个服务引用时，共享一个用户问题，但计划中仍为每个
引用保留独立的文件分配记录。

### 计划展示

最终计划分别列出：

- 将复制进归档的 bind、config 和 secret。
- 保留服务器路径的 bind。
- 因选择 `abort` 而终止的路径。

`keep_server_path` 选择必须参与 `plan_hash`，用户确认后不能在打包阶段被
静默改变。

## 打包和 Compose 行为

### `copy`

- source 内容复制到载荷 `files/`。
- Compose source 改写为对应的 `./files/...`。
- bind 副本继续应用现有权限规则：目录 `0777`，普通文件 `0666`。
- config 和 secret 继续保留其原权限。

### `keep_server_path`

- source 的目录条目和内容都不进入载荷或最终归档。
- Compose source 保持原始值，不做路径改写。
- 相对路径如 `./data` 仍相对于服务器上 `compose.yaml` 所在目录解析。
- 绝对路径继续引用服务器上的相同绝对路径。
- manifest 的 `server_paths` 记录解析后的服务器依赖路径。
- CLI 不创建、不清空、不修改该服务器目录，也不改变其权限。

例如：

```yaml
services:
  app:
    volumes:
      - ./data:/app/data
      - ./config:/app/config:ro
```

若 `data` 选择 `keep_server_path`、`config` 选择 `copy`，归档包含
`files/config/`，但完全不包含 `data/` 或 `files/data/`。部署 Compose
保持 `./data:/app/data`，并把配置挂载改写为
`./files/config:/app/config:ro`。

## 重复解压语义

用户继续把新归档上传到服务器的同一部署目录并执行普通的覆盖式解压：

- `.env`、`compose.yaml`、`images.tar`、`manifest.json`、
  `checksums.sha256` 和选择 `copy` 的文件依赖可以被同名归档成员覆盖。
- 选择 `keep_server_path` 的 bind source 不存在于归档中，因此普通解压不会
  覆盖或删除服务器上的对应目录及内容。
- 该保证不覆盖会在解压前清空部署根目录的外部脚本，也不覆盖显式使用
  `tar --delete`、目录同步删除等额外操作；当前手动原地解压流程不包含这些
  操作。

如果服务器上被保留的路径不存在，归档自身不负责创建它。Docker Compose
能否创建缺失路径取决于挂载语法和 Docker 行为；最终计划应把该路径列为
服务器前置依赖，便于用户在启动前检查。

## 错误处理与兼容性

- 项目内 bind 接受 `copy`、`keep_server_path`、`abort`。
- 项目外 bind 接受 `keep_server_path`、`abort`；提交 `copy` 返回中文
  计划校验错误。
- `abort` 在计划阶段立即停止，不执行 Docker 构建、拉取或归档。
- 答案缺失或不在允许选项内时返回中文错误，并指出具体 source。
- 旧行为可通过对所有项目内 bind 接受默认 `copy` 获得。
- named volume、Compose config、Compose secret、端口、环境变量和镜像处理
  规则不变。
- 保留相对 source 时不得把它替换为开发电脑的绝对路径。

## 测试

### 单元测试

- 项目内 bind 生成包含三个选项且默认 `copy` 的问题。
- 项目外 bind 仍只允许 `keep_server_path` 或 `abort`。
- 相同 bind source 的多个引用共享一个问题。
- `copy` 计划包含载荷路径，`keep_server_path` 计划不包含载荷路径。
- `abort` 在计划阶段失败。
- 被保留的 bind 不被复制，也不出现在 rewrite 映射中。
- 被保留的相对 source 在渲染后的 Compose 中保持原值。
- 同一路径同时用于 bind 和 config 时，bind 可以保留而 config 仍被复制。
- named volume 不生成问题。

### 集成与端到端测试

- 对 `./data` 选择 `keep_server_path` 后成功生成归档。
- 归档不包含 `data/` 或 `files/data/` 的任何成员。
- 归档 Compose 仍包含 `./data` source。
- manifest 把该路径列为服务器依赖。
- 在预先包含服务器数据的部署目录上覆盖解压，原数据内容保持不变。
- 同一应用中的 `copy` bind 仍随包发布并覆盖其对应文件。
- 计划输出和 `plan_hash` 随 bind 选择变化。

### Skill 契约

`SKILL.md` 必须要求：

- 逐项展示 bind mount 的处理问题。
- 完整计划明确区分复制内容和保留服务器路径。
- `keep_server_path` 的本机内容不得进入归档。
- 服务器路径在重复原地解压时不会被归档成员覆盖。
- 未获得计划哈希确认前不得开始 Docker 变更。

## 非目标

- 不自动识别数据库或上传目录。
- 不导出或恢复 Docker named volume 数据。
- 不自动备份服务器现有数据。
- 不修改用户的原始 Compose。
- 不实现服务器端部署、目录清理或解压脚本。
- 不自动创建缺失的服务器目录。

## 完成标准

- 每个 bind source 都有明确、可审查的复制或保留决定。
- 选择 `keep_server_path` 后，本机 source 及其内容完全不进入归档。
- 部署 Compose 保留该 source 的原始相对或绝对路径。
- 手动在同一服务器目录重复覆盖解压时，保留路径中的现有数据不被归档覆盖。
- 选择 `copy` 的配置类 bind 继续按现有方式随包发布。
- config、secret、named volume 及其他现有流程保持兼容。
- 相关单元、集成、端到端和 skill contract 测试全部通过。
