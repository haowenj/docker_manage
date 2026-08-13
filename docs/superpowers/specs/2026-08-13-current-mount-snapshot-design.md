# Docker 应用当前挂载决策快照设计

日期：2026-08-13

## 目标

让重复打包真正复用最近一次完整成功打包时确认的 bind mount 决策：上次选择
`copy` 的挂载下次仍默认 `copy`，上次选择 `keep_server_path` 的挂载下次仍默认
`keep_server_path`。环境变量、端口和挂载决策只在完整成功打包后一起更新，失败任务
不得改变当前配置。

## 当前问题

项目目前只把环境变量写入 `.docker-manage/.env`，把端口写入
`.docker-manage/ports.json`。重复打包虽然会展示所有问题，但 bind mount 没有跨任务
快照：项目内挂载总是回退为通用默认值 `keep_server_path`，项目外挂载没有默认值。
因此“复用之前配置”实际没有覆盖挂载决策。

## 快照格式与身份

新增项目级私有快照 `.docker-manage/mounts.json`。格式采用带版本号的严格 JSON：

```json
{
  "schema_version": 1,
  "mounts": [
    {
      "resolved_path": "/absolute/resolved/path",
      "action": "copy"
    }
  ]
}
```

- 只记录 `kind=bind` 的成功计划项，不记录 Compose `configs` 或 `secrets`。
- 以规范化后的绝对 `resolved_path` 标识挂载；同一路径被多个服务共享时只保存一项。
- `action` 只允许 `copy` 或 `keep_server_path`。`abort` 不可能产生成功制品，因此不得
  进入快照。
- 快照权限为 `0600`，父目录继续使用 `0700`。
- 重复路径、非法动作、非普通文件或无法解析的 JSON 都视为无效快照并停止检查。
- 快照中已经不再出现在 Compose 里的旧路径予以忽略；Compose 新增或解析路径发生变化的
  bind mount 没有当前值，继续采用原有安全规则。

## 检查与问题默认值

`inspect` 在发现 bind mount 后读取 `.docker-manage/mounts.json`，把匹配路径的上次动作
附加到 `FileCandidate.current_action`。问题生成遵循以下优先级：

1. 匹配到合法的 `current_action` 时，将它作为问题默认值，并在提示中显示当前配置值及
   来源 `.docker-manage/mounts.json`。
2. 没有当前值的项目内 bind mount 继续默认 `keep_server_path`。
3. 没有当前值的项目外 bind mount 继续没有默认值，必须明确回答。

项目外挂载只允许 `keep_server_path` 或 `abort`。如果快照试图为项目外挂载附加 `copy`，
`inspect` 必须把快照视为无效并停止，不能展示或采用不安全默认值。

## 成功写入与回滚

完整打包流程在归档创建并验证后，从最终 `PackagePlan.files` 提取 bind mount 决策，调用扩展
后的 `write_current_configuration` 一起更新：

- `.docker-manage/.env`
- `.docker-manage/ports.json`
- `.docker-manage/mounts.json`

写入继续采用临时文件、`fsync` 和 `os.replace`。更新前保存三份旧快照的内容和权限；任何
一份写入失败时，把三份文件全部恢复到调用前状态。只有三份快照都更新成功后，运行状态才
进入 `PACKAGED`。`inspect`、`plan`、`--dry-run`、等待模型补充和失败任务都不得更新挂载
快照。

## 兼容性与重复打包判定

为兼容已经成功打包过的项目，重复打包模式仍由 `.docker-manage/.env` 与
`.docker-manage/ports.json` 同时存在且为普通文件来判定，不强制要求
`.docker-manage/mounts.json`：

- 旧项目没有挂载快照时仍进入重复打包模式，挂载使用现有通用默认值。
- 该次完整成功打包后自动生成 `mounts.json`，之后开始复用挂载决策。
- 路径存在但不是普通文件，或内容无效时，`inspect` 报错停止。

不得从历史 `.docker-manage/work/<run_id>/state.json`、旧答案 JSON、manifest 或归档推断
当前挂载决策。

## Skill 交互契约

更新 `package-docker-app` skill，明确“当前配置”由环境变量、端口和可选的挂载决策快照
组成。重复打包清单必须显示 bind mount 的当前配置值、来源、通用声明默认值和最终默认
答案；用户回复“无修改”时采用最终默认答案。旧项目没有快照时必须明确显示这是通用默认值，
不能声称它来自上次打包。

## 测试

- 单元测试验证挂载快照缺失、有效读取、重复路径、非法 JSON、非法动作和项目外 `copy`。
- 问题测试验证上次 `copy` 和 `keep_server_path` 均能覆盖通用默认值，新挂载仍使用原规则。
- 写入测试验证只保存 bind mount、共享路径去重、权限为 `0600`，以及第三份文件失败时三份
  快照完整回滚。
- 集成测试执行一次成功打包后再次 `inspect`，分别证明 `copy` 和
  `keep_server_path` 成为下一次默认值。
- 兼容性测试证明只有旧的环境变量与端口快照时仍是重复打包模式，并在下一次成功后补齐
  `mounts.json`。
- 更新 skill 契约测试，并运行完整测试套件与 skill 校验。

## 完成标准

- 重复打包能复用最近一次完整成功打包的每个 bind mount 决策。
- 新增或改变路径的挂载不会误用旧决策。
- 项目外挂载不会获得 `copy` 默认值。
- 失败任务不会留下部分更新的当前配置。
- 旧项目无需人工迁移即可继续打包并自动获得挂载快照。
