# Bind Mount 默认保留服务器路径设计

**日期：** 2026-07-30

## 目标

把项目目录内 bind mount 处理问题的默认值从 `copy` 改为
`keep_server_path`。这样，首次部署后再次打包时默认不会重复复制挂载目录；
需要随归档复制内容时，用户仍可显式选择 `copy`。

## 范围

- 项目内 bind mount 的选项保持
  `copy`、`keep_server_path`、`abort` 不变。
- 项目内 bind mount 的默认值改为 `keep_server_path`。
- 项目外本地依赖仍只提供 `keep_server_path` 和 `abort`，且不新增默认值。
- 答案文件仍必须显式包含最终决定。
- `copy`、`keep_server_path` 和 `abort` 的计划、物化、渲染及部署行为均不变。

## 实现

修改问题构建逻辑中项目内 bind mount 的默认值。同步更新技能契约说明、
单元测试和集成测试中的默认值断言。无需引入部署历史检测、全局配置或新的
数据模型字段。

## 验证

- 单元测试验证项目内 bind mount 默认值为 `keep_server_path`，且三个选项不变。
- 单元测试验证项目外依赖的选项及无默认值行为不变。
- 集成测试验证 CLI `inspect` 输出的新默认值，并继续覆盖显式选择 `copy` 与
  `keep_server_path` 的两条路径。
- 运行相关测试及完整测试套件，确认没有行为回归。
