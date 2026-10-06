# FaceMaskStudio · 离线人脸马赛克工作室

在 Windows 上批量遮挡图片和视频中的人脸。保留你指定的主角，为其余检测到的人脸添加马赛克；视频中的遮挡区域会随人物移动更新。所有识别、跟踪和导出均在本机完成。

适合需要保留主角、保护路人面部隐私的日常素材处理。提供中文图形界面和 Windows 便携版，无需上传素材，也无需安装 Python。发布包解压即可处理图片；视频导出需要先准备本地编码器。

[下载 Windows 便携版](https://github.com/Anesthesia-ovo/face-mask-studio/releases) · [详细使用说明](docs/usage.md) · [离线设计与验证](docs/security.md)

## 功能

| 功能 | 说明 |
| --- | --- |
| 图片与视频批处理 | 混合添加文件，或导入文件夹及子文件夹；逐项显示状态和进度 |
| 自动保留主角 | 默认保留每帧最大的人脸，遮挡其余检测到的人脸 |
| 手动调整 | 点击识别框增减需要保留的人脸；也可遮挡全部人脸 |
| 运动跟踪 | 逐帧检测配合光流跟踪，随移动更新遮挡区域，并短时补偿漏检 |
| 手工补框 | 对漏检区域拖框添加遮挡；视频中的补框从选定编辑帧开始跟随 |
| 三种效果 | 马赛克、高斯模糊、纯色遮挡，可调整强度、扩边和检测精度 |
| 预览与导出 | 对照原图和遮挡效果；视频可选择保留音轨；支持取消与失败提示 |
| 本地文件保护 | 独立导出新文件；重复导出自动改名；图片输出移除原始 EXIF 元数据 |

“背景人物”由最大人脸或手动选择的保留对象来区分。程序不分析真实景深，也不能保证检测到每张脸。请在分享成品前检查小脸、侧脸、遮挡、转身和转场片段。

## 开始使用

1. 在 [Releases](https://github.com/Anesthesia-ovo/face-mask-studio/releases) 下载 Windows x64 便携包，解压到本地文件夹。
2. 双击 `FaceMaskStudio.exe`。保留同目录的 `_internal` 文件夹，完整移动整个程序目录。
3. 如需视频导出，在解压目录运行附带的 `准备视频组件.ps1`，一次性下载并核验 FFmpeg。具体方法见下方说明。
4. 添加素材，在预览中确认绿色保留框和橙色遮挡框。
5. 使用“自动保留最大人脸”，或点击识别框调整需要保留的人物。对漏检区域选择“拖框补充遮挡”。
6. 选择效果和导出目录，点击开始批量处理，再检查导出结果。

适用系统：**Windows 10 / 11，64 位**。下载软件和首次准备视频组件需要联网；准备完成后，日常使用不需要联网。

### 首次准备视频组件

公开发布包不包含独立的 Gyan FFmpeg 编码器。打开解压目录中的 PowerShell，运行：

```powershell
powershell -ExecutionPolicy Bypass -File ".\准备视频组件.ps1"
```

脚本从固定 Gyan 地址下载 ZIP，检查 SHA-256，并将 `ffmpeg.exe` 放入 `_internal/assets/`。它只负责一次性准备，不会上传素材；GUI 程序不会自动调用下载脚本。也可以在有网络的电脑准备好视频组件，再将完整程序目录复制到离线电脑。图片处理无需这一步。

源码仓库中的对应脚本为 [`scripts/prepare_video.ps1`](scripts/prepare_video.ps1)。公开分发含 FFmpeg 的自制安装包时，需要另外遵守其 GPL 许可证与对应源码提供要求，见第三方说明。

### 格式

| 类型 | 输入 | 输出 |
| --- | --- | --- |
| 图片 | JPEG、PNG、BMP、TIFF、WebP 单帧图片 | PNG；保留透明通道，应用 EXIF 方向 |
| 视频 | MP4、MOV、MKV、AVI、WebM、M4V、WMV、MPEG、MTS、M2TS 等常见容器 | MP4 / H.264；可选 AAC 音轨 |

具体能否读取取决于文件中的编码和文件完整性。动态 GIF 和其他多帧图片需要先转换为视频。

部分相机保存的 `.jpg` 内部是 MPO 多图片容器。1.0.1 起支持读取并处理首张主图，附加图片不会导出；动画图片和多页 TIFF 仍不按单张照片处理。

## 人物选择与跟踪

- **自动保留最大人脸**：适合主角始终靠近镜头、脸部面积明显较大的素材。人物大小发生交替时，保留对象可能切换。
- **手动保留指定人脸**：在清晰的参考帧中点击识别框，可保留一个或多个人物。视频逐帧使用本地人脸特征进行匹配；选择只作用于当前文件。
- **遮挡全部人脸**：无需保留主角时使用。

手工补框从参考帧向后跟随，参考帧之前不会应用该补框。出现明显转场或持续跟踪失败时，补框会停止，并在日志中提示。预览是单帧检查，导出视频才会显示完整跟随效果。详细操作见[使用说明](docs/usage.md)。

## 从源码运行

开发环境使用 **Windows x64 / Python 3.12**；已验证 Python 3.12.14。安装依赖和获取资源发生在开发或打包阶段，需要联网；程序运行时不会下载资源。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe scripts\fetch_resources.py
.\.venv\Scripts\python.exe src\launcher.py
```

下载脚本从固定来源获取 YuNet、SFace 和 FFmpeg，并验证 SHA-256。已有资源时可在离线环境中检查：

```powershell
.\.venv\Scripts\python.exe scripts\fetch_resources.py --verify-only
```

运行依赖版本：OpenCV 4.14.0.94、NumPy 2.2.6、Pillow 12.3.0；打包使用 PyInstaller 6.22.3。固定资源记录见 [`resources/manifest.json`](resources/manifest.json)。

## 构建 Windows 便携版

在仓库根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1 -Python "C:\path\to\python.exe"
```

提供 Python 3.12 x64 的实际路径。脚本准备 `.venv`、安装固定依赖、获取并验证资源、运行测试，然后打包。已有 `.venv` 时可直接运行 `powershell -ExecutionPolicy Bypass -File .\build.ps1`。

在依赖和资源已经准备好的离线开发环境中：

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1 -SkipInstall -SkipDownload
```

`-SkipDownload` 仍会检查本地资源哈希。产物位于 `dist/FaceMaskStudio/FaceMaskStudio.exe`，分发时需要完整保留 `dist/FaceMaskStudio` 文件夹。

```powershell
# 引擎测试
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_engine.py -v

# 打包程序自检（需要已准备 FFmpeg）：检查网络限制、模型加载、图片遮挡和视频编码
.\dist\FaceMaskStudio\FaceMaskStudio.exe --self-test .\self-test.json
```

## 离线与安全

程序没有账号登录、遥测、上传、自动更新或运行时下载功能。入口禁用 Python 网络连接；输入仅允许本地磁盘文件，媒体处理限制本地协议与允许的格式。构建下载脚本不包含在 EXE 中；发布包的独立视频准备脚本仅在你主动运行时联网。

资源来源和哈希已经核验，功能及离线限制已通过测试。首个 Windows 构建未获得有效的杀毒扫描结果，且未进行代码签名；来源核验不能替代杀毒扫描，也不能证明“绝对无毒”。具体记录和边界见[安全说明](docs/security.md)。

## 已知限制

- 远处小脸、侧脸、快速运动和遮挡可能漏检；光流只提供短时间补偿。
- 手动人物匹配可能失败，无法可靠匹配时会采用保守遮挡。识别框、马赛克或模糊均不能单独保证人物无法辨认。
- 视频按原视频平均帧率导出为固定帧率。手机可变帧率素材可能发生节奏或音画同步变化，需要复核。
- 不提供时间线、多段关键帧编辑、实时摄像头或云端处理。处理速度取决于 CPU、素材分辨率和检测精度。

## 项目结构

```text
src/                    中文界面、处理引擎、离线入口与自检
assets/app.ico          程序图标
models/                 构建时下载的本地模型（不提交大文件）
scripts/fetch_resources.py
scripts/prepare_video.ps1 发布包视频编码器的一次性准备脚本
resources/manifest.json 固定资源来源与 SHA-256
tests/                  引擎测试
docs/                   使用与安全说明
licenses/               第三方许可证
build.ps1               Windows 构建入口
```

## 许可证与反馈

本项目原创代码使用 [MIT 许可证](LICENSE)。模型、运行库和 FFmpeg 遵循各自的许可证；完整说明见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) 与 [`licenses/`](licenses/)。第三方许可证不会因本项目采用 MIT 而改变。

欢迎通过 [Issues](https://github.com/Anesthesia-ovo/face-mask-studio/issues) 提交问题。请附上系统版本、操作步骤、错误日志与不含隐私的复现素材；不要公开上传需要保护的人脸原片。
