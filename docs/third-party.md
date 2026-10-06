# 第三方资源与许可

FaceMask Studio 自编写的应用源码采用仓库根目录的 MIT 许可。下列第三方组件保留各自许可；MIT 许可不会替代第三方组件的许可。

## 组件清单

| 组件 | 本项目使用版本 | 许可 / 来源 |
| --- | --- | --- |
| Python | 3.12.14 | PSF 许可及所带第三方声明；[Python](https://www.python.org/) |
| Tcl / Tk | Python 运行时随带版本 | BSD 类许可；见 `licenses/Tcl-license.terms`、`licenses/Tk-license.terms` |
| NumPy | 2.2.6 | BSD 及所带第三方声明；[发布页](https://pypi.org/project/numpy/2.2.6/) |
| Pillow | 12.3.0 | HPND 类许可及所带图像库声明；[发布页](https://pypi.org/project/pillow/12.3.0/) |
| OpenCV / opencv-python | 4.14.0 / 4.14.0.94 | OpenCV 为 Apache-2.0；Python 打包脚本为 MIT；Windows 视频库含 LGPL FFmpeg；[官方说明](https://github.com/opencv/opencv-python#licensing) |
| YuNet | `face_detection_yunet_2023mar.onnx` | MIT；[模型目录](https://github.com/opencv/opencv_zoo/tree/f12e12798e8314f7c074a6656816c048dcc95b7a/models/face_detection_yunet) |
| SFace | `face_recognition_sface_2021dec.onnx` | 模型目录声明 Apache-2.0；[模型目录](https://github.com/opencv/opencv_zoo/tree/ba91a3b91d00d76e86540d4013f944bd6b514e39/models/face_recognition_sface) |
| 独立 FFmpeg 程序 | Gyan 9.0.2 essentials x64 | GPLv3 静态构建，包含多个第三方库；[构建发布页](https://www.gyan.dev/ffmpeg/builds/) |
| PyInstaller | 6.22.3 | GPL 及 bootloader 例外；打包工具，见 `licenses/pyinstaller-COPYING.txt` |

完整声明文本保存在仓库 `licenses/`。下载脚本的固定地址、文件长度和 SHA-256 保存在 `resources/manifest.json`。哈希用于确认取得的文件与指定版本一致。

## 模型来源

YuNet 文件固定到 OpenCV Zoo 提交 `f12e12798e8314f7c074a6656816c048dcc95b7a`；SFace 固定到提交 `ba91a3b91d00d76e86540d4013f944bd6b514e39`。构建脚本验证实际 ONNX 内容的 SHA-256，而不是 Git LFS 指针文本。

SFace 目录声明适用于目录中全部文件的 Apache-2.0 许可。关于该模型训练数据及商业使用来源的进一步澄清，可参阅[上游讨论 #313](https://github.com/opencv/opencv_zoo/issues/313)。本项目没有独立验证训练数据权利。

## 构建与运行

源码仓库不提交大型模型、独立 `ffmpeg.exe`、Python 环境或已打包依赖。开发者在构建阶段从上述上游下载资源；应用启动和媒体处理使用已准备好的本地文件，不执行资源下载。

软件按本地文件工作流设计：无账户登录、遥测、云识别或自动更新，拒绝远程媒体地址与网络共享路径；Python 网络调用及媒体协议在代码中作了限制。这是应用行为和代码限制说明，不代表操作系统防火墙级隔离。版本哈希、代码审查和离线设计也不等同于第三方杀毒认证。

## 重新分发二进制

重新发布带第三方依赖的 EXE 压缩包时，需要核实对应二进制版本的许可要求，并保留版权及许可文本。尤其要区分以下两份 FFmpeg：

1. **独立 `assets/ffmpeg.exe`**：Gyan 9.0.2 essentials 是 GPLv3 静态构建。上游标识主源码提交为 [`946fcce07b`](https://github.com/FFmpeg/FFmpeg/commit/946fcce07b)。`licenses/FFmpeg-build-README.txt` 记录配置和外部库版本。完整对应源码还应覆盖实际链接的非系统库、所用补丁及构建脚本；仅提供 FFmpeg 主仓库的一个标签和 GPL 文本不能证明已提供全部对应源码。
2. **`opencv_videoio_ffmpeg*.dll`**：OpenCV Windows 解码库是运行时加载的 LGPL 库。OpenCV 4.14.0 的[下载配置](https://github.com/opencv/opencv/blob/4.14.0/3rdparty/ffmpeg/ffmpeg.cmake)固定到 `opencv_3rdparty` 提交 [`bd9418020a5c342be979c56d6e6434261959d3af`](https://github.com/opencv/opencv_3rdparty/tree/bd9418020a5c342be979c56d6e6434261959d3af)。该提交的 `ffmpeg/download_src.sh`、`archive_src.sh` 和构建脚本记录了 FFmpeg `n7.1`、libvpx `v1.16.0`、aom `v3.14.1`、OpenH264 `v2.5.0` 接口头文件及相应 OpenCV 源码的组成。

[FFmpeg 官方许可说明](https://ffmpeg.org/legal.html)建议提供与所发布库完全对应的源码和构建说明。GPLv3 第 6 条及 LGPL 对二进制和对应源码的提供方式有进一步要求，详见保留的完整许可文本。发布时应在二进制下载位置明确给出对应源码的取得方式，并保证相关材料可取得。

公开 Windows 包所用 OpenCV 视频库的[对应源码附件](https://github.com/Anesthesia-ovo/face-mask-studio/releases/download/v1.0.0/FaceMaskStudio-1.0.0-OpenCV-FFmpeg-sources.zip)保存在 1.0.0 发布页，1.0.1 继续使用相同组件和这份源码。附件包含上述固定版本的完整库源码、完整 OpenCV 子模块、构建脚本、来源清单和重建说明。发布库的 MD5 与 OpenCV 下载配置中的固定值一致；每份取得的源码均记录 SHA-256。该库未在当前 Windows 主机重新编译，因此没有声称编译产物可逐字节复现。

公开 Windows 包不包含独立 Gyan `ffmpeg.exe`；这个组件由用户在使用前通过包内 `准备视频组件.ps1` 从官方上游取得并校验。该独立程序的完整对应源码尚未在本项目收集齐。
