# 项目根目录 Docker 配置复用设计

日期：2026-08-20

## 目标

当项目缺少 Dockerfile 或 Compose 文件时，模型补充流程默认在项目根目录创建缺失的标准 Docker 配置。后续打包直接通过现有文件发现逻辑读取这些文件，不再依赖 supplement JSON，也不重复生成配置。

## 行为规则

1. 项目根目录的 `Dockerfile`、`Dockerfile.*`、`compose.yaml`、`compose.yml`、`docker-compose.yaml`、`docker-compose.yml` 以及标准 override 文件属于项目正式配置。
2. 仅在 `inspect` 报告对应文件缺失时创建缺失文件；已有项目文件只读，模型补充和打包流程不得覆盖。
3. 新生成的 Compose 必须引用项目根目录中的 Dockerfile；不能再默认引用 `.docker-manage/generated/` 路径。
4. 下一次 `inspect` 不传 `--supplement` 时，CLI 自动发现根目录配置并正常进入后续流程。
5. 为兼容既有项目，`.docker-manage/generated/` 下的旧 supplement 文件仍然有效；新模型补充优先使用项目根目录。
6. 打包流程不负责判断业务代码是否需要修改 Dockerfile。依赖、系统命令、环境变量、端口或启动命令变化时，由 AI coding 阶段维护项目根目录配置；打包阶段只读取、分析和校验。

## 安全边界

- supplement 中的根目录生成路径只允许是项目根目录下的标准 Dockerfile 或 Compose 文件名，拒绝任意项目文件路径。
- `.docker-manage/generated/` 仍允许作为兼容的模型生成目录。
- supplement 引用的文件必须在 `inspect --supplement` 前已经存在；CLI 只校验和读取，不负责写入模型生成内容。
- 现有根目录配置不会因重复打包或 supplement 校验而被覆盖。

## 兼容性与非目标

- 不改变答案 JSON、快照、计划哈希、Docker 构建和归档协议。
- 不在本次加入源码指纹、依赖变更自动判定或模型自动重写机制。
- 不删除或迁移已有 `.docker-manage/generated/` 文件。

## 验证标准

- supplement 可以合法引用缺失后生成的根目录 Dockerfile/Compose。
- 任意越界或非标准根目录路径仍被拒绝。
- 根目录生成文件在新一轮无 supplement 的 `inspect` 中自动被发现。
- 旧 `.docker-manage/generated/` 流程继续通过。
- skill 文档明确一次性根目录生成、禁止覆盖和后续复用规则。
