<p align="center">
  <img src="https://lisseldee.github.io/assets/images/webp/1-c1.webp" width="100%" alt="LANSyncBox">
</p>

<div align="center">

[![](https://img.shields.io/badge/-简体中文-3b82f6?style=flat)](https://github.com/LisseldeE/LANSyncBox/blob/pro/README.md) [![](https://img.shields.io/badge/-English-555555?style=flat)](https://github.com/LisseldeE/LANSyncBox/blob/pro/README_EN.md)

</div>

<p align="center">
  <a href="https://github.com/LisseldeE/LANSyncBox/releases/tag/pro-R1.1.0.0"><img src="https://img.shields.io/badge/releases-R1.1.0.0-3b82f6" alt="releases R1.1.0.0"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/LisseldeE/LANSyncBox" alt="开源协议"></a>
  <img src="https://img.shields.io/badge/platform-Windows-blue?logo=windows" alt="支持平台">
  <img src="https://img.shields.io/badge/platform-Linux-orange?logo=linux" alt="支持平台">
</p>

<p align="center">
  <a href="#项目信息">项目信息</a> |
  <a href="#系统支持">下载</a> |
  <a href="#使用方法">使用方法</a> |
  <a href="#同步逻辑">同步逻辑</a> |
  <a href="#安装与运行">安装与运行</a> |
  <a href="#开源声明">其他声明</a>
</p>

> **版本声明**：本分支为 [**LANSyncBox**](https://github.com/LisseldeE/LANSyncBox/tree/main) 的 **Pro** 版本，当前已经完成了去中心化重构，经过多轮内测，功能基本完善。若您在使用过程中遇到问题，请及时反馈。

## 项目简介

LANSyncBox Pro 是一款跨平台、专注局域网多人协作的文件实时同步工具。无需公网，支持大文件流式传输、串行化发送与断点续传，保证多连接高并发场景下的数据可靠与传输稳定。相比旧版，Pro 采用了全新的去中心化分布式架构，解决了单点故障影响整条链路的问题。此外还引入了多项新能力：跨设备复制粘贴的**投递**、顶部拖拽**快捷添加**、收集模式、权限管理等。

## 版本对比

| 功能 | 标准版 | Pro 版 |
| :--- | :---: | :---: |
| 实时文件同步 | ✔ | ✔ |
| 房间分享（6 位数字 + 密码校验） | ✔ | ✔ |
| 大文件传输（流式分块 · 断点续传） | ✔ | ✔ |
| 文件操作（增删改 · 只读预览） | ✔ | ✔ |
| 中英文界面快捷切换 | ✔ | ✔ |
| **投递 · 跨设备复制粘贴** | — | ⭐ **新增** |
| **文本剪贴板广播** | — | ⭐ **新增** |
| **图片 / 文件端到端直投** | — | ⭐ **新增** |
| **顶部快捷添加** | — | ⭐ **新增** |
| **同步 / 收集双模式** | — | ⭐ **新增** |
| **去中心化分布式架构** | — | ⭐ **新增** |

## 项目截图

| 主界面 | 同步界面 |
| :---: | :---: |
| ![主界面](https://lisseldee.github.io/assets/images/webp/1p-1.webp) | ![同步界面](https://lisseldee.github.io/assets/images/webp/1p-2.webp) |

## 项目信息

- **项目名称**: LANSyncBox Pro
- **项目作者**: Lisselde_E
- **开源协议**: GNU General Public License v3.0
- **项目主页**: https://lisseldee.github.io/#1
- **项目仓库**: https://github.com/LisseldeE/LANSyncBox/tree/pro

## 核心特性（既有）

- **实时同步**：新增、修改、删除、重命名实时同步至所有连接端；初次连接自动全量对齐
- **房间分享**：自定义 6 位数字房间号，可选密码验证，加入时自动校验同步逻辑版本兼容
- **大文件传输**：流式分块、断点续传，失败重发不损坏文件；最多同时 5 个并行传输，同名拉取自动串行保证字节一致
- **文件操作**：添加、新建、复制、剪切、粘贴、删除、重命名；双击只读预览
- **界面体验**：Qt6 流畅界面，中英文实时切换，传输进度可视化

## 系统支持

<div align="center">

| 操作系统 | x64 (AMD64) | ARM64 |
| :--- | :---: | :---: |
| Windows | ✅ | ❌ |
| Linux | ✅ | ❌ |
| macOS | ❌ | ❌ |

</div>

> 目前仅覆盖 Windows 10/11 与 Linux (x64)；macOS 支持已在计划中

## 下载

<p align="center">
  <a href="https://apps.microsoft.com/detail/9n4g6w3rm3q6?referrer=appbadge&mode=full" target="_blank"  rel="noopener noreferrer">
	  <img src="https://get.microsoft.com/images/zh-cn%20dark.svg" width="200"/>
  </a>
</p>

<p align="center">
  <a href="https://github.com/LisseldeE/LANSyncBox/blob/pro/downloadwizard/zh_cn.md#LANSyncBox-Pro">
    <img src="https://img.shields.io/badge/GitHub%20Releases-Download-181717?style=for-the-badge&logo=github&logoColor=white" alt="GitHub Releases"/>
  </a>
</p>

## 使用方法

### 主机端（创建连接）

1. 点击"创建连接"按钮
2. 可选：设置密码保护
3. 点击创建，进入同步状态界面

### 连接端（加入连接）

1. 点击"加入连接"按钮
2. 输入或选择房间号
3. 如需密码，会引导进入密码输入界面
4. 加入房间开始同步

## 同步逻辑

### 端职责

| 端 | 职责 |
| :--- | :--- |
| **任意端（对等节点）** | 每端独立维护操作列表与文件状态（向量时钟），变更沿网状实时派发；任意端离线，其余端仍互相同步收敛 |
| **加入房间（去中心化）** | 无需主机在线：任意在线端都能应答发现、验证与引导新端加入，加入后自动全量对齐 |
| **读写权限** | 由房间创建端（主机端）管理各连接端的只读 / 读写权限 |
| **冲突处理** | 版本向量、逻辑钟、时间戳三层保险，确保冲突解决的正确性 |

### 同步机制

| 机制 | 说明 |
| :--- | :--- |
| **实时同步** | 通过操作列表记录并实时派发文件变更 |
| **传输协议** | TCP + 自定义协议 |
| **大文件流式传输** | 分块流式传输，避免整文件载入内存 |
| **并发控制** | 最多同时 5 个文件并行传输；同名文件的多份在途拉取自动串行，配合落盘后过时裁决，保证字节收敛一致 |
| **可恢复发送** | 发送中断仅续发剩余部分，不重发已发送数据；接收端繁忙时短暂退避，保持延迟真实 |
| **完整性校验** | 接收完成时校验文件大小，不完整自动丢弃 |
| **传输取消与清理** | 支持主动取消（窗口关闭 / 用户取消），中断即清理 `.tcp_*.part` 临时文件，不留半成品参与同步 |
| **版本校验** | 加入房间只校验内部同步逻辑版本号（SYNC_LOGIC_VERSION），UI / 展示等非同步改动不强制全员升级 |
| **节点离线** | 去中心化网状：任意节点断线，其余节点继续互相同步收敛，重连后自动增量补齐 |

## 更新日志

详见 [更新日志](https://github.com/LisseldeE/LANSyncBox/blob/pro/CHANGELOG.md)

## 技术栈

- Python 3.x
- Qt6 (PySide6)
- 自定义 TCP 协议

## 安装与运行

### 系统要求
- Windows 10 或更高版本（64位）
- Linux x64（amd64）发行版

### 安装方式
- **Windows**：从 [GitHub Releases](https://github.com/LisseldeE/LANSyncBox/releases) 下载安装包，双击运行即可
- **Linux（deb 包）**：从 [GitHub Releases](https://github.com/LisseldeE/LANSyncBox/releases) 下载对应架构的 `.deb` 安装包，在终端中执行以下命令安装：
  ```bash
  sudo apt install -y ./lansyncbox_*.deb
  ```

### 运行
- **Windows**：安装完成后，从开始菜单或桌面快捷方式启动 LANSyncBox Pro
- **Linux**：从应用菜单（Activities）中搜索 LANSyncBox 并启动；若运行时提示缺少系统依赖，请先执行：
  ```bash
  sudo apt install -y libxcb-cursor0 libgl1 libxkbcommon-x11-0
  ```

## 开源声明

本项目采用 GNU General Public License v3.0 开源协议，详见 [LICENSE](https://github.com/LisseldeE/LANSyncBox/blob/pro/LICENSE) 文件。

## 隐私政策

本项目不收集任何用户数据，详见 [隐私政策](https://github.com/LisseldeE/LANSyncBox/blob/pro/privacy_policy.md) 文件。

## 商业合作
本项目采用 GPL-3.0 协议开源。如果你希望在闭源商业场景中使用，或需要定制开发与技术支持，欢迎通过以下方式联系：

**联系方式**：Lisselde.E@outlook.com

**合作流程**：
1. 说明你的使用场景与需求
2. 确认授权范围与费用
3. 签署授权协议
4. 获取商业授权与技术支持

**授权范围**：
- 闭源商用授权
- 企业部署授权
- 定制开发与技术支持

## 反馈

**公开测试中，如有问题或新的创意欢迎和我联系！**

欢迎提交 Issue 和 Pull Request！
邮箱：Lisselde.E@outlook.com

## 支持我

如果你觉得这个工具还不错，欢迎在爱发电上赞助项目支持我继续开发，非常感谢你的心意！

<a href="https://ifdian.net/a/lisseldee">
  <img src="https://img.shields.io/badge/爱发电-支持作者-018E96?style=for-the-badge" alt="支持作者">
</a>
