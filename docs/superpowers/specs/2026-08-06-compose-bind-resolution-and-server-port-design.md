# Compose Bind 路径解析与 Server 端口配置设计

## 1. 背景

`docker_manage_server/compose.yaml` 使用
`${DOCKER_MANAGE_DATA_DIR:-${PWD}/data}` 作为 bind mount source、target 和容器内
`DATA_DIR`。宿主机与 server 容器必须看到同一个绝对路径，因为 server 容器通过
宿主机 Docker Socket 执行 Compose；宿主机 Docker daemon 必须能直接解析 server
容器生成的部署路径。

Docker Compose 在提供
`DOCKER_MANAGE_DATA_DIR=/data/docker-manage-server` 时能正确渲染为：

```yaml
source: /data/docker-manage-server
target: /data/docker-manage-server
```

当前 `docker-package-app inspect` 故意以 `--no-interpolate` 读取 Compose，随后把原始
`${DOCKER_MANAGE_DATA_DIR:-${PWD}/data}` 当作普通相对路径。该路径因此被误判为项目内
bind；`keep_server_path` 又把它改写为 `./files/...`，导致归档不再使用服务器上的
`/data/docker-manage-server`。

此外，server Compose 当前固定发布 `8000:8000`，直接启动项目时不能通过环境变量
避免宿主机端口冲突。

## 2. 目标

- 保留宿主机与 server 容器使用同一绝对数据路径的现有架构。
- 在打包计划阶段使用最终环境变量答案解析 Compose bind source。
- 将最终解析为项目外的 bind 保持为服务器路径，不复制、不改写为 `./files/...`。
- manifest 输出实际服务器所需路径 `/data/docker-manage-server`。
- 允许直接运行 server Compose 时通过 `DOCKER_MANAGE_SERVER_PORT` 配置宿主机端口，
  默认仍为 `8000`。
- 本次离线包使用宿主机端口 `6308`、容器端口 `8000/tcp`，目标平台为
  `linux/amd64`。

## 3. 非目标

- 不把 server 容器内 `DATA_DIR` 改回 `/app/data`。
- 不拆分宿主机 source 与容器 target；两者继续使用同一绝对路径。
- 不改变业务 API、镜像启动端口或 Docker Socket 挂载。
- 不允许将项目外 bind 内容复制进归档。

## 4. Server Compose 设计

数据目录保持现状：

```yaml
x-data-dir: &data-dir ${DOCKER_MANAGE_DATA_DIR:-${PWD}/data}

environment:
  DATA_DIR: *data-dir
volumes:
  - type: bind
    source: *data-dir
    target: *data-dir
```

端口声明改为：

```yaml
ports:
  - "${DOCKER_MANAGE_SERVER_PORT:-8000}:8000"
```

README 同步说明 `DOCKER_MANAGE_SERVER_PORT`，并展示服务器部署值：

```text
DOCKER_MANAGE_DATA_DIR=/data/docker-manage-server
DOCKER_MANAGE_SERVER_PORT=6308
```

## 5. 打包器数据流

### 5.1 Inspect

`inspect` 继续使用未插值 Compose，保留环境变量名称、默认值、来源及原始 bind
表达式。环境变量问题仍先于文件问题，现有答案协议保持不变。

### 5.2 Plan

`plan` 收到完整答案后，从 `env.<service>.<name>` 汇总 Compose 插值环境。Compose
插值是项目级的；同名变量如果在不同服务得到不同值，并且参与 Compose 插值，计划
必须在 Docker 变更前报错，不得任选其中一个值。

打包器使用 Docker Compose 自身进行插值，不自行实现 `${VAR:-default}` 解析器：

- 工作目录和 `PWD` 均设置为项目根目录；
- 传入最终环境变量答案；
- 保留 `--no-path-resolution`，避免把部署 Compose 污染为开发电脑绝对路径；
- 不使用 `--no-interpolate`，获得 bind source 的最终值。

打包器按服务、依赖类型和 Compose 条目顺序关联“原始未插值条目”与“最终已插值
条目”。计划中的 `FileAssignment.original_value` 保留原始表达式，
`resolved_path` 使用最终解析路径。

### 5.3 Package

文件物化以计划中的最终 `resolved_path` 为准：

- 项目外 `keep_server_path`：不复制内容，不改写部署 Compose source；manifest 的
  `server_paths` 记录最终绝对路径。
- 项目内 `keep_server_path`：继续改写为稳定的 `./files/<项目相对路径>`，本机内容
  不进入归档。
- 项目内 `copy`：继续复制到 `files/<项目相对路径>` 并设置规定权限。
- 项目外 `copy`：计划校验失败。

部署 Compose 保留 `${DOCKER_MANAGE_DATA_DIR:-${PWD}/data}`；归档 `.env` 提供
`DOCKER_MANAGE_DATA_DIR=/data/docker-manage-server`。服务器校验时该表达式展开为实际
绝对路径。

## 6. 错误处理与安全

- Docker Compose 插值失败、变量缺失或原始/最终条目无法一一对应时，`plan` 失败。
- 同名 Compose 插值变量存在多个最终答案时，`plan` 失败并显示变量名。
- 最终路径位置决定允许的文件动作；不能沿用 inspect 阶段对未插值字符串的错误
  项目内判断。
- 计划、部署 Compose、manifest 和最终结果不得包含开发电脑绝对路径。
- 所有失败必须发生在 Docker build、pull、save 之前。
- 计划继续由 `plan_hash` 保护；环境答案或路径解析结果变化都会改变哈希。

## 7. 测试设计

遵循 TDD，先添加能复现错误的失败测试：

1. 打包器单元测试：最终环境答案把变量 bind source 解析为项目外绝对路径。
2. 打包器计划测试：`keep_server_path` 使用最终路径，不生成 `./files/...`。
3. 打包器渲染/制品测试：部署 Compose 保留原始变量表达式，manifest 记录实际服务器
   路径，归档中不存在该 bind 内容。
4. 打包器 CLI 集成测试：完整 `inspect -> plan` 使用最终答案解析嵌套默认表达式。
5. server 集成测试：
   `DOCKER_MANAGE_DATA_DIR=/data/docker-manage-server` 时 source、target 和 `DATA_DIR`
   相同；`DOCKER_MANAGE_SERVER_PORT=6308` 时发布 `6308:8000/tcp`。

修改后运行两个项目的完整测试套件，并执行一次真实
`inspect -> plan -> package`。最终归档验收必须确认：

```text
端口：6308:8000/tcp
DATA_DIR=/data/docker-manage-server
DOCKER_MANAGE_DATA_DIR=/data/docker-manage-server
server_paths 包含 /data/docker-manage-server 和 /var/run/docker.sock
server_paths 不包含 ./files/${DOCKER_MANAGE_DATA_DIR...}
```

同时重新计算并核对归档大小、SHA-256、镜像列表和服务器所需路径。
