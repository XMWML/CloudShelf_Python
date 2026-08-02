# CloudShelf Python

CloudShelf 的跨平台 Python + Tkinter 文件管理器版本。它保留了原版的核心布局和工作流：左侧连接列表、中间远端文件浏览区，以及底部传输任务列表。

![CloudShelf icon](CloudShelf-icon.png)

## 功能

- 保存和切换多个 FTP、SFTP、WebDAV 连接
- 远端目录浏览、双击进入、返回上级和刷新
- 新建文件夹、上传、下载、重命名、删除、复制、移动
- 支持上传本地文件夹和递归下载远端文件夹，并保留目录层级
- 同步规则管理：添加、编辑、启用/停用、立即执行和定时检查
- 支持本地上传、远端下载、本地删除同步到远端、远端删除同步到本地
- 同步冲突支持保留最新、保留本地、保留远端和保留两份
- SFTP 连接支持密码、SSH Agent、私钥路径和主机密钥策略配置
- 支持远端项目属性、剪切/复制/粘贴和 `Ctrl/Command` 常用快捷键
- 支持目录后退/前进历史、连接右键菜单和连接删除
- 右键文件菜单和传输状态显示
- macOS/Linux/Windows 启动脚本
- 仅使用 Python 标准库，不需要安装第三方 Python 包
- 安装可选的 `tkinterdnd2` 后支持 Finder/Explorer 文件拖拽上传

## 项目结构

```text
cloudshelf.py              Tkinter 界面和交互控制器
cloudshelf_core/paths.py   远端路径与大小格式化
cloudshelf_core/remote.py  FTP、SFTP、WebDAV 远端客户端
cloudshelf_core/storage.py 配置加载、迁移和安全写入
cloudshelf_core/sync.py    与协议无关的同步引擎
tests/test_core.py         核心逻辑测试
```

## 安装与运行

需要 Python 3.10+，并且 Python 发行版包含 Tkinter。

```bash
git clone https://github.com/XMWML/CloudShelf_Python.git
cd CloudShelf_Python
python3 cloudshelf.py
```

也可以安装为命令行应用：

```bash
python3 -m pip install .
cloudshelf
```

安装可选的桌面适配层：

```bash
python3 -m pip install '.[desktop]'
```

在 macOS/Linux 上也可以运行 `run.command`；Windows 上运行 `run.bat`。

运行核心测试：

```bash
python3 -m unittest discover -s tests -v
```

### 平台依赖

- FTP 和 WebDAV 使用 Python 标准库实现。
- SFTP 使用系统 OpenSSH `sftp` 客户端。Windows 需要启用或安装 OpenSSH Client。
- Linux 发行版通常需要单独安装 Tk，例如 Debian/Ubuntu 的 `python3-tk`。
- 拖拽上传为可选增强：`python3 -m pip install tkinterdnd2`。

## 连接配置

连接配置保存在：

```text
~/.cloudshelf/connections.json
```

服务器地址、端口、用户名、密码和远端根目录在连接窗口中填写。WebDAV 默认使用 HTTPS；如果使用 HTTP，请在配置中将 `tls` 设为 `false`。

## 安全说明

连接配置文件会以仅当前用户可读写的权限保存。安装可选的 `keyring` 包后，密码优先保存到当前系统的凭据存储，配置文件中不再保存密码：

```bash
python3 -m pip install keyring
```

未安装 `keyring` 时，为保持零依赖兼容性，密码会保存在本地连接配置中。请勿将该文件提交到版本控制或共享给他人。

## 已知限制

- 当前传输任务显示状态和字节级进度（WebDAV）或文件级进度（FTP/SFTP）。
- WebDAV 任务同时显示近期传输速率。
- 文件夹传输支持暂停、继续、取消和失败后重试；单个底层网络请求会在完成后响应任务控制。
- 传输使用最多 3 个后台工作线程，超出的任务会排队。
- 设置中可配置 1 到 8 个传输并发数；后台错误记录在 `~/.cloudshelf/cloudshelf.log`。
- 自动同步使用跨平台轮询指纹，不依赖系统专用文件监控 API。
- FTP 操作依赖系统 `curl`，SFTP 操作依赖系统 OpenSSH 客户端。
- 不提供 Finder、Windows Explorer 或 Linux 文件管理器中的系统级网络挂载。

## 项目来源

本项目是 [CloudShelf](https://github.com/XMWML/CloudShelf) macOS 原生版本的跨平台重写尝试，使用 Tkinter 替代 AppKit。

## License

暂未指定许可证。公开使用或二次分发前，请先补充合适的 License 文件。
