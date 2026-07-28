# Compose 插值短端口解析修复设计

## 背景

`docker-package-app` 在 `inspect` 阶段通过 `docker compose config --no-interpolate` 读取 Compose，因此短端口语法中的变量表达式会保留原文。当前 `_compose_port()` 使用 `rsplit(":", 2)`，会把 `${VAR:-default}`、`${VAR:?error}` 和 `${VAR:+replacement}` 内部的冒号误当作端口字段分隔符。

错误切分会产生被截断的 `host_ip`。该值随后经过规划阶段进入部署 Compose，最终导致 `docker compose config` 报插值格式错误。

## 目标

- `${PDF_TRANS_WEB_PORT:-8000}:8000` 解析为容器端口 `8000/tcp`，且 `host_ip` 为 `None`。
- 不在打包工具内实现 Compose 变量求值；非字面量主机端口保持未知，由现有 `port_host` 问题收集。
- 用户为上述端口指定主机端口 `8322` 后，部署 Compose 只包含有效的 `8322 -> 8000/tcp` 映射，不包含被截断的 `${PDF_TRANS_WEB_PORT`。
- 同样保护 `:-`、`:?` 和 `:+` 操作符中的冒号。
- 保持 IPv4 短语法和无 IP 短语法的现有行为。

## 范围

本修复只支持以下短语法字段形态：

- `CONTAINER`
- `HOST:CONTAINER`
- `IPV4:HOST:CONTAINER`

IPv6 短语法不在本次支持范围内，也不增加方括号或 IPv6 专用解析分支。Compose 长语法的数据模型和渲染逻辑不变。

## 方案

在 `cli.py` 中增加一个小型短端口字段切分函数。该函数从左到右扫描字符串，并维护 `${...}` 插值深度：

- 遇到 `${` 时进入一层插值。
- 在插值内部遇到的冒号按普通字符处理。
- 遇到匹配的 `}` 时退出一层插值。
- 只有插值深度为零时，冒号才切分端口字段。

切分结果只接受一至三个字段；其他字段数量视为无法识别的短端口声明。`_compose_port()` 继续用 `_integer_port()` 识别字面量端口：

- 一个字段表示容器端口。
- 两个字段表示主机端口和容器端口。
- 三个字段表示 IPv4、主机端口和容器端口。
- 两字段中的主机端口若是 `${...}` 表达式，则无法转换为整数，因此 `host_port=None`，同时不会产生 `host_ip`。

例如 `${PDF_TRANS_WEB_PORT:-8000}:8000` 的顶层字段为 `${PDF_TRANS_WEB_PORT:-8000}` 和 `8000`，最终得到：

```text
container_port=8000
protocol=tcp
host_ip=None
host_port=None
```

现有问题生成逻辑会把容器端口作为 `port_host` 的回退默认值；用户仍可明确输入 `8322`。

## 数据流

1. `inspect` 保留原始 Compose 插值文本。
2. 新切分逻辑只按顶层冒号识别短端口字段。
3. `Inspection` 保存 `8000/tcp` 候选，且不保存错误的 `host_ip`。
4. `plan` 从 `port_host` 答案读取 `8322`。
5. `render_deployment()` 生成长语法映射，其中 `published=8322`、`target=8000`、`protocol=tcp`，并省略 `host_ip`。
6. `validate_deployment()` 对生成文件执行 `docker compose config`。

## 错误处理

- 容器端口不是 `1..65535` 的整数时，保持现有行为并忽略该候选。
- 顶层字段超过三个、插值未闭合或字段无法组成受支持的 IPv4 短语法时，不构造错误的 `PortCandidate`。
- 本修复不尝试解释 `:?` 的错误文本或 `:+` 的替换值，也不读取环境变量来求值主机端口。

## 测试

### 单元回归测试

直接调用 `_compose_port()`，覆盖：

- `${PDF_TRANS_WEB_PORT:-8000}:8000`
- `${PDF_TRANS_WEB_PORT:?error}:8000`
- `${PDF_TRANS_WEB_PORT:+8322}:8000`

每个用例至少断言 `container_port == 8000`、`protocol == "tcp"`、`host_ip is None` 和 `host_port is None`。另保留字面量主机端口与 IPv4 主机地址的断言，防止修复破坏 `8322:8000` 和 `127.0.0.1:8322:8000`。

### 端到端回归测试

创建包含 `${PDF_TRANS_WEB_PORT:-8000}:8000` 的临时 Compose 项目，提供 `8322` 的 `port_host` 答案并完成 `inspect -> plan -> package`。从归档读取部署 Compose，断言：

- `target` 为 `8000`。
- `published` 为 `8322`。
- `protocol` 为 `tcp`。
- 端口映射中不存在 `host_ip`。
- 输出文本中不存在被截断的 `${PDF_TRANS_WEB_PORT`。

最后对生成的 Compose 和 `.env` 执行真实的 `docker compose config`，要求退出码为零。测试中的镜像构建和导出继续使用现有 fake Docker，避免依赖 Docker daemon。

## 非目标

- 不实现完整的 Compose 插值求值器。
- 不从 `${VAR:-default}` 中提取主机端口默认值。
- 不新增或保留 IPv6 短端口语法兼容承诺。
- 不修改端口问题、规划模型或部署渲染结构。
