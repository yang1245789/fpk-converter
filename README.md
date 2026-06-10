# 飞牛 NAS 视频自动转码工具

面向飞牛 fnOS 的本地自用视频转码应用。它会定时扫描授权目录，把符合条件的视频串行转码为 H.264/H.265，并支持 Intel Quick Sync Video（QSV）硬件编码。

当前可用版本：`v1.0.47`。该版本已在实际环境中成功调用 GPU 转码，并加入启动前功能自检、Root 硬件设备访问、飞牛 mediasrv QSV 环境复用、日志轮转和当前文件进度显示。

## 下载与安装

从 [Releases](https://github.com/yang1245789/fpk-converter/releases) 下载最新版 `fpkconverter.fpk`，在飞牛 NAS 应用中心手动安装。

安装后请进入：

```text
应用 → 视频转码 → 应用设置 → 授权目录
```

给需要监控的视频目录授予读写权限，例如：

```text
/vol3/1000/PORN
```

本应用的硬件转码版使用飞牛官方手册中的 Root 权限模式运行，目的是让 ffmpeg 能访问 `/dev/dri/renderD*` GPU 设备。请只在信任安装包来源、明确需要本地硬件转码时使用。

## GPU 转码说明

勾选 GPU 加速后，应用会强制使用 Intel QSV：

- H.264 目标编码使用 `h264_qsv`
- H.265 目标编码使用 `hevc_qsv`
- QSV 失败时停止当前文件，不自动降级 CPU
- 启动前会用极短测试视频先验证 QSV 是否可用

应用会优先复用飞牛 mediasrv 的运行环境：

```text
ffmpeg=/usr/trim/lib/mediasrv/ffmpeg
LIBVA_DRIVER_NAME=iHD
LIBVA_DRIVERS_PATH=/usr/trim/lib/mediasrv/dri
qsv_device=/dev/dri/renderD*
```

如果系统存在多个 `renderD*` 设备，应用会按顺序尝试。

## CPU 参与是正常的

即使 GPU 转码已经正常工作，CPU 仍会参与一部分流程。这不是异常，也不代表“没有使用 GPU”。

CPU 通常负责：

- 读取源文件和磁盘 IO
- 解复用容器，例如 MP4/MKV
- 音频流 copy 或封装
- 把视频帧送入 QSV 编码器
- MP4 `faststart` 处理
- 进度解析、日志、数据库记录和 Web UI 状态更新

当前版本主要把“视频编码”交给 QSV/GPU。源视频解码、文件封装和系统调度仍可能消耗 CPU，所以转码时看到 CPU 有占用是正常现象。

## 启动前自检

点击启动后，应用不会立刻扫描目录，而是先检查：

- 监控目录是否可读
- 临时目录是否可写
- `ffmpeg` 是否可运行
- `ffprobe` 是否可运行
- 勾选 GPU 时，QSV 是否能完成 1 秒测试编码

只有自检通过，才会进入正式扫描和转码流程。自检失败时不会扫描目录，也不会等待 300 秒。

## 功能特性

- 定时扫描授权目录，不使用 inotify
- 串行转码，一次只处理一个文件
- 文件加入队列后等待 300 秒，避免文件尚未写完就开始转码
- 当前文件进度条
- 失败文件受控重试，避免坏文件无限循环
- `converter.log` 自动轮转
- `ffmpeg_*.log` / `ffprobe_*.log` 自动清理
- SQLite 记录已处理文件，避免重复转码
- 转码后更小时才替换原文件
- 同路径替换前先备份，失败时保留原文件

## 配置说明

| 配置项 | 说明 | 默认值 |
|---|---|---|
| 监控文件夹 | 要自动扫描的视频目录，必须先在 fnOS 授权目录中授予读写权限 | 空 |
| CRF | 质量系数，范围限制为 18-32，越小质量越高 | 23 |
| 编码预设 | CPU 编码预设；GPU 模式下 QSV 使用 medium | medium |
| 线程数 | ffmpeg 线程数，限制为 1-4 | 1 |
| 编码器 | H.264 或 H.265 | H.264 |
| 格式 | MP4 或 MKV | MP4 |
| GPU 加速 | Intel QSV 硬件编码 | 开启 |

## 运行日志

页面会显示最近转码输出。正常 GPU 启动时应能看到类似日志：

```text
=== 启动前功能自检 ===
ffmpeg 自检通过
ffprobe 自检通过
GPU/QSV 自检通过: /dev/dri/renderD128
启动前功能自检通过，进入正式扫描流程
QSV 尝试 1/1: 使用设备 /dev/dri/renderD128
转码进度: 42.5% | 当前文件: ...
```

如果出现 `No VA display found`、`Failed to create a VAAPI device` 或 `无权读写 /dev/dri/renderD128`，请确认安装的是 `v1.0.46` 或更新版本，并重新启动应用。

## 自行打包

```bash
git clone https://github.com/yang1245789/fpk-converter.git
cd fpk-converter
fnpack build
```

构建产物为：

```text
fpkconverter.fpk
```

## 版本历史

| 版本 | 说明 |
|---|---|
| v1.0.47 | 增加启动前功能自检，通过后才进入扫描和正式转码 |
| v1.0.46 | 使用 Root 权限模式访问 `/dev/dri/renderD*`，解决应用用户无法访问 GPU 设备的问题 |
| v1.0.45 | 复用飞牛 mediasrv 的 ffmpeg/VAAPI/QSV 环境，并显式指定 `-qsv_device` |
| v1.0.44 | 增加 GPU 诊断和日志轮转 |
| v1.0.43 | GPU 模式强制只用 QSV，失败不降级 CPU |
| v1.0.42 | 增加 300 秒稳定等待、失败受控重试和当前文件进度 |
| v1.0.41 | 按飞牛官方授权目录机制重构路径访问 |

## 注意事项

1. 首次使用必须在 fnOS 应用设置中授权监控目录。
2. GPU 模式仍会看到 CPU 参与，这是正常现象。
3. 应用会替换原文件，请先确认目录内没有唯一重要数据。
4. Root 硬件转码版适合手动安装自用，不适合发布到第三方应用中心审核场景。
5. 如需停止转码，请使用页面上的“停止”按钮。

## 开源协议

MIT License
