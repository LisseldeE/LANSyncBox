<p align="center">
  <img src="https://lisseldee.github.io/assets/images/webp/1-c1.webp" width="100%" alt="LANSyncBox">
</p>

<div align="center">

[![](https://img.shields.io/badge/-简体中文-3b82f6?style=flat)](https://github.com/LisseldeE/LANSyncBox/blob/pro/README.md) [![](https://img.shields.io/badge/-English-555555?style=flat)](https://github.com/LisseldeE/LANSyncBox/blob/pro/README_EN.md)

</div>

> ⚠️ **Beta 版本声明**：本分支（`pro`）为 **LANSyncBox** 的 **Pro** 版本，正在跟随新特性持续迭代。功能尚未完全稳定，可能会有较大改动。当前处于 Beta 测试开发阶段，请勿用于正式环境。

## 项目简介

LANSyncBox Pro 是 [LANSyncBox](https://github.com/LisseldeE/LANSyncBox) 的升级版本。专注局域网多人协作，增加大量新功能，提升用户体验。

## 新版本声明
Pro版本将会保持**开源**状态，不会转为收费模式。由于Pro版本涉及到底层修改，可能随后续迭代将无法与普通版本相互连接。

## 新版本亮点（进行中）

相比旧版，Pro 版正在引入以下改动：

| 特性 | 说明 |
| :--- | :--- |
| **局域网剪切板** | 在房间连接成功后生效，实现局域网内的**文本、图片、文件**跨设备一键复制粘贴 |
| **文件分布式直传** | 文件信息由主机分发，但粘贴时**直接对接复制端拉取（P2P），不经过主机转发** |
| **同步模式 / 收集模式** | 新增收集模式：连接端文件仅单向提交主机，不广播到其余端 |

> 以上为规划中的 Beta 改动，具体细节与进度可能随开发调整，以实际版本为准。

## 核心特性（既有）

- **实时同步**：新增、修改、删除、重命名实时同步至所有连接端；初次连接自动全量对齐
- **房间分享**：自定义 6 位数字房间号，可选密码验证，加入时自动校验版本兼容
- **大文件传输**：流式分块、断点续传，失败重发不损坏文件；最多同时传输 5 个并自动取消传输中变更
- **文件操作**：添加、新建、复制、剪切、粘贴、删除、重命名；双击只读预览
- **界面体验**：Qt6 流畅界面，中英文实时切换，传输进度可视化

## 使用方法

- **主机端（创建连接）**：点击"创建连接"→ 可选密码 → 创建，进入同步界面
- **连接端（加入连接）**：点击"加入连接"→ 输入/选择房间号 → 需密码则直接预验证 → 连接，自动全量同步一次

## 技术栈

- Python 3.x
- Qt6 (PySide6)
- 自定义 TCP 协议

## 开源声明

本项目采用 GNU General Public License v3.0 开源协议，详见 [LICENSE](https://github.com/LisseldeE/LANSyncBox/blob/main/LICENSE) 文件。

## 隐私政策

本项目不收集任何用户数据，详见 [隐私政策](https://github.com/LisseldeE/LANSyncBox/blob/main/privacy_policy.md) 文件。

## 反馈

**Beta 测试中，如有问题或新的创意欢迎和我联系！**

欢迎提交 Issue 和 Pull Request！

## 支持我

如果你觉得这个工具还不错，欢迎在爱发电上打赏支持我继续开发，非常感谢你的心意！

- **[爱发电主页](https://ifdian.net/a/lisseldee)** : [https://ifdian.net/a/lisseldee](https://ifdian.net/a/lisseldee)