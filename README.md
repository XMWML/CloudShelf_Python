# CloudShelf Python

CloudShelf 的跨平台 Python + Tkinter 文件管理器版本。它保留了原版的核心布局和工作流：左侧连接列表、中间远端文件浏览区，以及底部传输任务列表。

![CloudShelf icon](CloudShelf-icon.png)

## 功能

- 保存和切换多个 FTP、SFTP、WebDAV 连接
- 远端目录浏览、双击进入、返回上级和刷新
- 新建文件夹、上传、下载、重命名、删除
- 右键文件菜单和传输状态显示
- macOS/Linux/Windows 启动脚本
- 仅使用 Python 标准库，不需要安装第三方 Python 包

## 安装与运行

需要 Python 3.10+，并且 Python 发行版包含 Tkinter。

```bash
git clone https://github.com/XMWML/CloudShelf_Python.git
cd CloudShelf_Python
python3 cloudshelf.py
```

在 macOS/Linux 上也可以运行 `run.command`；Windows 上运行 `run.bat`。

### 平台依赖

- FTP 和 WebDAV 使用 Python 标准库实现。
- SFTP 使用系统 OpenSSH `sftp` 客户端。Windows 需要启用或安装 OpenSSH Client。
- Linux 发行版通常需要单独安装 Tk，例如 Debian/Ubuntu 的 `python3-tk`。

## 连接配置

连接配置保存在：

```text
~/.cloudshelf/connections.json
```

服务器地址、端口、用户名、密码和远端根目录在连接窗口中填写。WebDAV 默认使用 HTTPS；如果使用 HTTP，请在配置中将 `tls` 设为 `false`。

## 安全说明

当前版本为了保持跨平台和零依赖，将密码保存在本地连接配置文件中。请确保该文件权限仅当前用户可读，并不要把个人配置文件提交到 Git。后续可以接入 Windows Credential Manager、macOS Keychain 和 Linux Secret Service。

## 已知限制

- SFTP 私钥选择、SSH Agent 和主机密钥策略尚未加入图形化配置。
- 当前传输任务显示状态，但没有进度条和暂停/取消按钮。
- 同步规则和递归复制功能仍在完善中。
- 不提供 Finder、Windows Explorer 或 Linux 文件管理器中的系统级网络挂载。

## 项目来源

本项目是 [CloudShelf](https://github.com/XMWML/CloudShelf) macOS 原生版本的跨平台重写尝试，使用 Tkinter 替代 AppKit。

## License

暂未指定许可证。公开使用或二次分发前，请先补充合适的 License 文件。
