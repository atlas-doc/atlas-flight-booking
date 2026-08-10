# 安装说明

[English](installation.md)

## Agent 自动安装（推荐）

安装 Skill：

```bash
npx --yes skills add https://github.com/atlas-doc/atlas-flight-booking --skill atlas-flight-booking
```

Skill 第一次需要使用 Atlas Flight Booking 时，会检查 `atlas-flight`。如果 CLI 尚未安装，Agent 会在需要时通过 Astral 官方独立安装器自动安装 `uv`，再安装 CLI、验证版本并继续原来的航班任务。它不会额外增加一轮对话许可；宿主环境仍可能显示自身的命令执行审批。普通用户通常不需要自己安装这两个工具。

## 运行要求

- Windows、macOS 或 Linux；
- 使用 `npx` 安装 Skill，因此需要 Node.js；
- 可访问互联网，以便 Agent 获取 [uv](https://docs.astral.sh/uv/getting-started/installation/) 和已签名的 CLI 包。

如果缺少 `uv`，Agent 会先自动安装。随后 `uv` 会按需下载并管理 Python 3.12，不需要用户另外准备 Python 环境。

## 手动恢复 CLI

这是提供给技术支持和开发者的高级恢复方式。只有 Agent 无法完成自动安装时才需要使用。

```bash
uv tool install --python 3.12 atlas-flight-booking==0.3.9
```

验证安装：

```bash
atlas-flight --version
atlas-flight doctor --json
```

版本命令应返回 `atlas-flight 0.3.9`。

## 安装后找不到命令

查看 `uv` 安装可执行文件的目录：

```bash
uv tool dir --bin
```

让 `uv` 将该目录加入终端环境：

```bash
uv tool update-shell
```

关闭并重新打开终端，然后再次执行 `atlas-flight --version`。

## 重新安装或修复

```bash
uv tool install --force --python 3.12 atlas-flight-booking==0.3.9
```

重新安装 CLI 不会删除保存在操作系统安全凭据设施中的 Atlas 授权信息。

## 卸载

```bash
uv tool uninstall atlas-flight-booking
```

CLI 安装和 Atlas 授权是两件独立的事情。安装 CLI 不会自动授权账户；只有用户请求的任务需要授权时，Skill 才会启动授权流程。
