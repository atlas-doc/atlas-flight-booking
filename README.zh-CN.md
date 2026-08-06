# Atlas Flight Booking

[English](README.md)

Atlas Flight Booking 是一套面向 Agent 的航班搜索与预订 CLI 和 Skill。它支持查询实时航班、验价、选择行李或座位、创建订单、余额支付和查询出票状态。

Skill 负责对话流程和用户确认；`atlas-flight` CLI 负责授权、安全凭据存储、接口调用、统一输出和副作用保护。

用户只需要安装 Skill。首次使用时，Skill 会检查 CLI；如果缺失，它会先说明原因并征得用户同意，再由 Agent 完成安装。电脑只需要具备 [uv](https://docs.astral.sh/uv/getting-started/installation/)，不需要另外准备 Python 环境。

## 支持的流程

- 浏览器授权和最长 120 秒的有限轮询；
- 实时航班搜索和统一报价比较；
- 价格与库存验证，包括涨价和降价处理；
- 可选的行李与座位服务；
- 通过 stdin 或已有本地 JSON 文件一次性提交乘机人信息；
- 创建订单并返回脱敏支付摘要和 Atlas 订单链接；
- 用户明确确认后执行一次余额支付；
- 最长 120 秒出票轮询及后续订单查询。

行李或座位不可用不会阻断主预订流程。当前版本不支持退票、取消、改签、信用卡支付或其他售后操作。

## 安装 Skill

```bash
npx --yes skills add https://github.com/atlas-doc/atlas-flight-booking --skill atlas-flight-booking
```

Skill 启动后会检查 CLI。如果没有安装，它会先解释用途并征得用户同意，不会擅自安装软件。

## 开始搜索

安装 Skill 后，用户可以直接用自然语言告诉 Agent 航线、日期和乘客数量。对应的 CLI 命令示例为：

```bash
atlas-flight search \
  --origin KUL \
  --destination SIN \
  --depart 2026-08-20 \
  --adults 1 \
  --json
```

所有子命令只返回一个稳定的 JSON 对象。Agent 根据 `code` 决策，原样保留不透明 ID，不读取凭据或内部路由。

## 安全边界

- Agent 会说明授权用途，并等待用户在浏览器完成授权。
- 验价上涨时必须重新获得用户明确确认。
- 乘机人资料采用一次性输入，不写入持久化预订状态或结构化错误。
- 支付前必须展示当前脱敏摘要和 Atlas 订单链接。
- 支付确认 ID 只能使用一次；生单或支付结果不明确时禁止重试副作用命令。
- 凭据和私有流程数据使用操作系统安全凭据设施保存，不提供明文降级方案。

## 本地开发与离线验证

```bash
uv sync --frozen
uv run --frozen pytest -q
uv run --frozen ruff check .
uv run --frozen mypy src/atlas_cli
bash tests/skill/validate-skill.sh skills/atlas-flight-booking
uv run --frozen python -m scripts.scan_secrets .
uv build
```

这些检查使用 mock 和 fixture，不代表线上订票已经验证。线上验收必须使用经批准的账户和数据，由人工执行。

## 许可证

本项目使用 [Apache License 2.0](LICENSE)。
