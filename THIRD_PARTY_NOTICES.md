# 第三方声明

FaceMask Studio 的应用源码采用 MIT 许可。Python、Tcl/Tk、OpenCV、NumPy、Pillow、人脸模型及视频工具保留各自的许可和版权；请参阅 [`licenses/`](licenses/) 和[详细资源说明](docs/third-party.md)。本项目没有修改这些第三方组件的源码。

## 公开 Windows 包

公开 EXE 包包含 OpenCV Windows 视频库 `opencv_videoio_ffmpeg*.dll`。该库包含 LGPLv2.1-or-later FFmpeg 代码。发布附件 `FaceMaskStudio-1.0.0-OpenCV-FFmpeg-sources.zip` 提供与该库的上游构建版本对应的源码、许可证、构建脚本和 `SOURCE-MANIFEST.json`。应用源码和构建入口在本仓库中公开；库位于普通 `_internal` 文件目录中，未作替换限制。

公开包**不包含独立的 Gyan `assets/ffmpeg.exe`**。视频编码还需要这个文件：可在软件使用前执行包内 `prepare_video.ps1`，从 Gyan 官方发布地址下载指定版本并校验 SHA-256。该准备脚本需要联网；FaceMask Studio 的启动、识别、跟踪和媒体处理使用本地资源，不执行下载。也可在联网电脑完成准备后把整个目录复制到离线电脑。

Gyan 9.0.2 essentials 是 GPLv3 静态构建，包含多个外部库。其主源码、配置和版本信息分别见[上游源码提交](https://github.com/FFmpeg/FFmpeg/commit/946fcce07b)、[构建网站](https://www.gyan.dev/ffmpeg/builds/)和保留的 [`FFmpeg-build-README.txt`](licenses/FFmpeg-build-README.txt)。本项目没有把这些有限材料声明为该独立程序的完整对应源码，也未把该程序再发布到本项目的二进制附件中。

## 模型

YuNet 使用 OpenCV Zoo 声明的 MIT 许可；SFace 模型目录声明 Apache-2.0 许可。SFace 训练数据来源的进一步澄清见[上游讨论 #313](https://github.com/opencv/opencv_zoo/issues/313)。模型内容、固定提交与校验值记录在 [`resources/manifest.json`](resources/manifest.json)。

## 安全说明

官方来源校验和离线设计不等同于杀毒认证。本项目不声称获得第三方“无毒”认证，也不承诺所有人脸都能被检出。详细的离线限制与媒体处理限制见 README 和[资源说明](docs/third-party.md)。
