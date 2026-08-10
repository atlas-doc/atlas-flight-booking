<div align="center">
  <a href="https://atlaslovestravel.com/?utm_source=skill">
    <img src="assets/atlas-logo.svg" alt="Atlas" width="180">
  </a>
  <h1>Atlas Flight Booking</h1>
  <p>面向 Agent 的实时航班搜索与预订能力。</p>
  <p>
    <a href="https://pypi.org/project/atlas-flight-booking/"><img src="https://img.shields.io/pypi/v/atlas-flight-booking?label=PyPI" alt="PyPI 版本"></a>
    <a href="https://github.com/atlas-doc/atlas-flight-booking/releases/latest"><img src="https://img.shields.io/github/v/release/atlas-doc/atlas-flight-booking?label=release" alt="最新版本"></a>
    <a href="https://github.com/atlas-doc/atlas-flight-booking/stargazers"><img src="https://img.shields.io/github/stars/atlas-doc/atlas-flight-booking?style=flat" alt="GitHub Stars"></a>
    <a href="LICENSE"><img src="https://img.shields.io/github/license/atlas-doc/atlas-flight-booking" alt="Apache 2.0 许可证"></a>
  </p>
  <p>
    <a href="https://atlaslovestravel.com/?utm_source=skill"><img src="https://img.shields.io/badge/Website-atlaslovestravel.com-ffcd0a?labelColor=336699" alt="Atlas 官网"></a>
    <a href="https://x.com/AtlasLCC"><img src="https://img.shields.io/badge/X-@AtlasLCC-000000?logo=x&amp;logoColor=white" alt="Atlas X"></a>
    <a href="https://www.linkedin.com/company/atlaslovestravel/"><img src="https://img.shields.io/badge/LinkedIn-Atlas-0A66C2?logo=linkedin&amp;logoColor=white" alt="Atlas LinkedIn"></a>
  </p>
</div>

[English](README.md)

Atlas Flight Booking 是一套面向 Agent 的航班搜索与预订 CLI 和 Skill。它支持查询实时航班、验价、选择行李或座位、创建订单、余额支付和查询出票状态。

Skill 负责对话流程和用户确认；`atlas-flight` CLI 负责授权、安全凭据存储、接口调用、统一输出和副作用保护。

用户只需要安装 Skill。第一次发起航班任务时，Skill 会检查 CLI；如果 CLI 或 `uv` 缺失，Agent 会通过官方安装方式自动准备所需工具，不再增加一轮对话许可。宿主环境仍可能显示自身的命令执行审批提示。用户不需要另外准备 Python 环境。

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

Skill 启动后会检查 CLI。如果没有安装，Agent 会在需要时通过 Astral 官方独立安装器自动安装 `uv`，然后执行 `uv tool install --python 3.12 atlas-flight-booking==0.3.9`，从 [PyPI](https://pypi.org/project/atlas-flight-booking/) 安装已发布的 Atlas CLI。用户不需要单独安装这两个工具；只有自动安装实际失败时，Agent 才会停止并提供简短的恢复说明。

[安装详情与故障排查 →](docs/installation.zh-CN.md)

## 在 Sandbox 演练完整预订流程

Atlas Flight Booking 默认使用生产服务。查询实时票价以及做出真实购买决策时，应保持默认配置。

只有在付费前想完整演练正向预订流程，或者已付费客户需要进行回归测试时，才需要切换到 Sandbox。Sandbox 使用测试数据，不会创建真实的生产订单或产生真实扣款。

完成 Atlas 授权后，由用户在终端手动执行：

```bash
atlas-flight environment use sandbox --json
```

切换后继续使用同一个 Skill 和相同的公开命令，无需重新安装，也不需要为 Agent 提供另一套操作说明。切换只会修改 CLI 的本地服务配置。切换前获得的报价会失效，请重新搜索后再继续。

需要恢复实时票价和生产预订时，在终端执行：

```bash
atlas-flight environment use production --json
```

Sandbox 中的价格和库存都是测试数据，不能作为真实购买决策的依据。

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

## 许可证

本项目使用 [Apache License 2.0](LICENSE)。
