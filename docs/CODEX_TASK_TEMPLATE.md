# Codex Task Template

## 当前 migration

- Head：`20260716_0013`

## 唯一业务目标

- [只写一个可验收目标]

## 修改范围

- [允许修改的模块、文件或端口]

## 禁止范围

- [不得修改的业务、项目、数据库或端口]

## 核心业务规则

- [本任务必须保持的身份、金额、来源追踪或权威数据规则]

## 最小测试

- [本任务新增或扩展的定向测试]
- 验证级别：Quick / Core / Full

## 执行脚本

- `scripts\verify_quick.bat [测试文件或节点表达式]`
- 或 `scripts\verify_core.bat`
- 仅在明确要求时使用 `scripts\verify_full.bat`

## 固定输出格式

1. 修改文件
2. 修改内容
3. 测试结果
4. 是否可合并
