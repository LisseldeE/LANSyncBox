<p align="center">
  <img src="https://lisseldee.github.io/assets/images/webp/1-c1.webp" width="100%" alt="LANSyncBox">
</p>

<div align="center">

[![](https://img.shields.io/badge/-简体中文-3b82f6?style=flat)](https://github.com/LisseldeE/LANSyncBox/blob/pro/README.md) [![](https://img.shields.io/badge/-English-555555?style=flat)](https://github.com/LisseldeE/LANSyncBox/blob/pro/README_EN.md)

</div>

<p align="center">
  <a href="https://github.com/LisseldeE/LANSyncBox/releases/tag/pro-R1.0.0.0"><img src="https://img.shields.io/badge/releases-R1.0.0.0-3b82f6" alt="releases R1.0.0.0"></a>
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
  <a href="#开源声明">开源声明</a>
</p>

> ⚠️ **Beta 版本声明**：本分支（`pro`）为 [**LANSyncBox**](https://github.com/LisseldeE/LANSyncBox/tree/main) 的 **Pro** 版本，正在跟随新特性持续迭代。功能尚未完全稳定，可能会有较大改动。当前处于 Beta 测试开发阶段，请勿用于正式环境。

## 项目简介

LANSyncBox Pro 是 [LANSyncBox](https://github.com/LisseldeE/LANSyncBox/tree/main) 的升级版本，专注局域网多人协作。相比旧版，Pro 正在引入多项新能力——跨设备复制粘贴的**投递**、顶部拖拽**快捷添加**、收集模式等，持续迭代中。

## 版本对比

> 标准版的既有功能，Pro 全部保留；Pro 在其上叠加局域网协作新能力（Beta 规划中，仅列部分，细节可能随开发调整）。

| 功能 | 标准版 | Pro 版 |
| :--- | :---: | :---: |
| 实时文件同步 | ✔ | ✔ |
| 房间分享（6 位数字 + 密码校验） | ✔ | ✔ |
| 大文件传输（流式分块 · 断点续传） | ✔ | ✔ |
| 文件操作（增删改 · 只读预览） | ✔ | ✔ |
| 中英文界面实时切换 | ✔ | ✔ |
| **投递 · 跨设备复制粘贴** | — | ⭐ **新增** |
| **文本剪贴板广播** | — | ⭐ **新增** |
| **图片 / 文件端到端直投** | — | ⭐ **新增** |
| **顶部快捷添加** | — | ⭐ **新增** |
| **同步 / 收集双模式** | — | ⭐ **新增** |
| **去中心化网状同步** | — | ⭐ **新增** |

## 项目截图

| 主界面 | 同步界面 |
| :---: | :---: |
| ![主界面](https://lisseldee.github.io/assets/images/webp/1p-1.webp) | ![同步界面](https://lisseldee.github.io/assets/images/webp/1p-2.webp) |

## 新版本亮点（进行中）

> Pro 在文件同步之外，正在加入以下新能力（开发中，仅列部分）。

| 特性 | 说明 |
| :--- | :--- |
| **投递 · 跨设备复制粘贴** | 在 A 设备复制，到 B 设备 **Ctrl+V** 即达：**文本**经主机广播写入所有连接端系统剪贴板；**图片/文件**从复制端**端到端直连**拉取，一键投递到本地 |
| **顶部快捷添加** | 把文件/文件夹直接**拖到屏幕顶部**的放置条即可加入同步列表，无需在窗口中逐项操作 |
| **文件分布式直传** | 文件变更经网状广播派发，字节流在源端与接收端之间**点对点直连**传输，不经过主机转发 |
| **去中心化网状同步** | 同步不再依赖主机仲裁：任意设备离线，其余设备仍可互相同步收敛；加入房间无需主机在线，任意在线设备即可应答发现与验证 |
| **多文件并行传输** | 最多同时 **5 个文件并行传输**；同名文件的多份在途拉取自动串行，配合落盘后过时裁决，保证字节最终一致 |
| **同步逻辑版本号** | 加入房间只校验内部**同步逻辑版本号**，UI / 展示等非同步改动不再要求全部设备同步升级 |
| **同步 / 收集双模式** | 在实时同步之外新增**收集模式**：连接端文件单向提交主机，不广播到其余端 |

> 以上为规划中的 Beta 改动，具体细节与进度可能随开发调整，以实际版本为准。

## 核心特性（既有）

- **实时同步**：新增、修改、删除、重命名实时同步至所有连接端；初次连接自动全量对齐
- **房间分享**：自定义 6 位数字房间号，可选密码验证，加入时自动校验同步逻辑版本兼容
- **大文件传输**：流式分块、断点续传，失败重发不损坏文件；最多同时 5 个并行传输，同名拉取自动串行保证字节一致
- **文件操作**：添加、新建、复制、剪切、粘贴、删除、重命名；双击只读预览
- **界面体验**：Qt6 流畅界面，中英文实时切换，传输进度可视化

## 项目信息

- **项目名称**: LANSyncBox Pro
- **项目作者**: Lisselde_E
- **开源协议**: GNU General Public License v3.0
- **项目主页**: https://lisseldee.github.io/#1
- **项目仓库**: https://github.com/LisseldeE/LANSyncBox/tree/pro

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
  <a href="https://github.com/LisseldeE/LANSyncBox/releases">
    <img src="https://img.shields.io/badge/GitHub-Releases-181717?style=flat-square&logo=github&logoColor=white" alt="GitHub Releases">
  </a>
  &nbsp;&nbsp;
  <a href="https://gitee.com/Lisselde_E/LANSyncBox/releases">
    <img src="https://img.shields.io/badge/Gitee-镜像下载-C71D23?style=flat-square&logo=gitee&logoColor=white" alt="Gitee 镜像下载">
  </a>
</p>

> 💡 国内用户推荐使用 Gitee 镜像下载

## 使用方法

### 主机端（创建连接）

1. 点击"创建连接"按钮
2. 可选：设置密码保护
3. 点击创建，进入同步状态界面

### 连接端（加入连接）

1. 点击"加入连接"按钮
2. 输入或选择房间号
3. 如需密码，在加入界面直接输入并预验证
4. 验证失败时直接在加入界面提示，可即时修改信息重试
5. 点击连接，自动从主机全量同步一次

## 同步逻辑

### 端职责

| 端 | 职责 |
| :--- | :--- |
| **任意端（对等节点）** | 每端独立维护操作列表与文件状态（向量时钟），变更沿网状实时派发；任意端离线，其余端仍互相同步收敛 |
| **加入房间（去中心化）** | 无需主机在线：任意在线端都能应答发现、验证与引导新端加入，加入后自动全量对齐 |
| **读写权限** | 由房间创建端（主机端）管理各连接端的只读 / 读写权限 |
| **冲突处理** | 以最后修改时间较新的版本为准 |

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
- **Windows**：从 [GitHub Releases](https://github.com/LisseldeE/LANSyncBox/releases) 或 [Gitee 镜像](https://gitee.com/Lisselde_E/LANSyncBox/releases) 下载安装包，双击运行即可
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

## 反馈

**Beta 测试中，如有问题或新的创意欢迎和我联系！**

欢迎提交 Issue 和 Pull Request！

## 支持我

如果你觉得这个工具还不错，欢迎在爱发电上打赏支持我继续开发，非常感谢你的心意！

<a href="https://ifdian.net/a/lisseldee">
  <img src="https://img.shields.io/badge/爱发电-支持作者-018E96?style=for-the-badge" alt="支持作者">
</a>
