# Godot PCK 解包工具 (godot_pck_unpacker.py)

通用 Godot 资源包 (.pck) 解包器，自动识别版本，纹理自动转 PNG。

## 支持的版本
| PCK 版本 | 对应 Godot | 状态 |
|---------|-----------|------|
| v1 | Godot 3.x | 代码支持 (未实测样本) |
| v2 | Godot 4.0–4.4 | ✅ 已实测 (Godot 4.4.1, 真实解包 4566 文件) |
| v3 | Godot 4.5+ | 代码支持 (未实测样本) |

## 用法
```bash
# 基本解包 (自动识别版本, 纹理转 PNG 并按原始路径归位)
python godot_pck_unpacker.py game.pck

# 指定输出目录
python godot_pck_unpacker.py game.pck -o out_dir

# 只列出包内文件, 不解包 (支持下面所有筛选参数做预览)
python godot_pck_unpacker.py game.pck --list

# 不转换纹理 (保留原始 .ctex/.stex)
python godot_pck_unpacker.py game.pck --no-convert

# 不按原始路径归位 (纹理只放在 .godot/imported/ 下)
python godot_pck_unpacker.py game.pck --no-organize

# 额外保留 .webp 原文件 / 额外保留原始 .ctex (用于工程重建)
python godot_pck_unpacker.py game.pck --keep-webp --keep-raw

# 跳过 .import/.remap 导入元数据 (仅保留真实资源, 输出更干净)
python godot_pck_unpacker.py game.pck --no-meta
```

## 选择性导出 (按分类/扩展名筛选)
默认会导出**全部**资源。可用下面参数挑要导出的部分：

| 参数 | 作用 | 示例 |
|------|------|------|
| `--include` | 只导出这些**路径前缀** (文件夹/分类) | `--include Sprites,Sounds,UI` |
| `--exclude` | 排除这些路径前缀 | `--exclude .godot` |
| `--ext`     | 只导出这些**源扩展名** | `--ext ctex,scn,gd` |

- 多个值用逗号或空格分隔，如 `Sprites,Sounds` 或 `Sprites Sounds`
- `--include`/`--exclude` 按路径片段匹配：含 `/` 当路径片段；以 `.` 开头（如 `.import`、`.godot`）同时按**扩展名(后缀)**和**目录**匹配；其余当目录前缀
- `--ext` 按**包内原始扩展名**匹配（`.ctex` 才是纹理源，归位后变 `.png`）
- 纹理筛选以**归位后的原始路径**为准，所以 `--include Sprites` 能正确捞出归位到 `Sprites/` 的 PNG
- `--list` 也会应用同样的筛选，方便先预览再提取

```bash
# 只导出美术: Sprites + UI 两类的归位 PNG
python godot_pck_unpacker.py game.pck --include Sprites,UI

# 先看看 Sprites 里有哪些 (预览)
python godot_pck_unpacker.py game.pck --list --include Sprites

# 导出所有场景和脚本 (Godot 4 脚本是编译后的 .gdc)
python godot_pck_unpacker.py game.pck --ext scn,gdc

# 除 .godot 缓存目录外全导出
python godot_pck_unpacker.py game.pck --exclude .godot
```

## 输出说明
- 所有文件按 `res://` 虚拟路径解包到输出目录
- 纹理 (`.ctex` / `.stex`) 自动提取内嵌图像并转成 PNG：
  - 优先按 `.import` / `.remap` 映射归位到开发时的原始路径 (如 `Sprites/Logo_Layers/5.png`)
  - 无法映射时，用 `.ctex` 文件名反推原始名（`foo.png-<哈希>.ctex` → `foo.png`），落在 `.godot/imported/` 下
  - 没有 Pillow 时回退保存为 `.webp` (浏览器/系统照片查看器可直接打开)
- **关于 `.import` / `.remap`**：这俩是 Godot 的**导入元数据**（约 1KB 纯文本），记录纹理等资源的导入方式，**不是图片本身**——把它们改后缀成 `.png` 也打不开。真实图片永远在 `.ctex` 里，本工具会转成 PNG。默认**会一并导出**全部资源（不做跳过）；若想让输出更干净、只保留真实资源，用 CLI `--no-meta` 或 GUI 勾选“跳过 .import/.remap 元数据”。
- 加密文件 (PCK_FILE_ENCRYPTED) 自动跳过并提示

## 依赖
- 必须: Python 3 标准库
- 可选: Pillow (`pip install pillow`) — 用于把 WebP 转成 PNG；没有则保存原始 WebP

## 实现要点 (便于自行修改)
- `parse_header()`: 按版本读 header (v2/v3 含 FileOffsetBase/DirectoryOffset)
- `parse_directory()`: 目录区在 `header_size` 之后 (v3 用 DirectoryOffset)；条目含 path/offset/size/md5/(flags)
- 偏移计算: v2/v3 为 `FileOffsetBase + 相对偏移`；v1 为绝对偏移
- `convert_texture()`: `.ctex`(GST2) 内搜 `RIFF...WEBP` 提取；`.stex` 同法尽力提取

> 注: 编译后的脚本 `.gdc` 属于代码 (Godot 4 已加密编译)，本工具不解包代码逻辑。

---

## 图形界面版 (godot_pck_gui.py / GodotPCKUnpacker.exe)

不想用命令行的日常用法：纯 Python + Tkinter 写的界面，**零额外基础依赖**（Tkinter 是标准库），纹理转 PNG 仍可选项 Pillow。

### 运行方式
```bash
# 方式一: 源码 (需本机有 Python)
python godot_pck_gui.py

# 方式二: 直接双击打包好的 exe (无需 Python 环境)
dist\GodotPCKUnpacker.exe
# 也可把 .pck 文件直接拖到 exe 图标上 (Windows 会作为 argv[1] 传入并自动加载)
```

### 界面功能
- **源文件**：点击「浏览...」选择 `.pck`，或把 `.pck` 直接拖拽到窗口（Windows 支持拖拽）
- **输出文件夹**：点击「选择...」指定；不选则默认 `<pck名>_unpacked`
- **导出类型**：
  - `全部` —— 导出包内所有资源
  - `自定义分类` —— 勾选 图像 / 音效 / 字体 / 脚本 / 场景 / 着色器 中的一个或多个
    - 图像 = `.ctex/.stex`（自动转 PNG 并归位到原始路径）+ 原始位图
    - 音效 = `.wav/.ogg/.mp3/.opus/.flac` 等
    - 字体 / 脚本 / 场景 / 着色器 按对应扩展名筛选
- **选项**：纹理归位到原始路径（默认开）、纹理转为 PNG（默认开）、额外保留原始纹理（用于 Godot 工程重建）、跳过 .import/.remap 元数据（默认**不**跳过；勾选后只保留真实资源，输出更干净）
- **进度条 + 日志 + 取消**：解包在后台线程进行，不卡界面；可随时点「取消」中止
- 选好文件后会自动**分析**并显示 `Godot 版本 | 共 N 文件 | 各分类数量`，方便决定导出哪些

### 打包 exe（如需自行重新构建）
```bash
pip install pyinstaller pillow
python -m PyInstaller --onefile --windowed --name GodotPCKUnpacker \
    --hidden-import PIL --hidden-import PIL.Image godot_pck_gui.py
# 产物在 dist/GodotPCKUnpacker.exe
```

> exe 与 `godot_pck_unpacker.py` 共用同一套解包内核（`unpack()` 通过回调驱动），
> 行为、版本兼容、纹理处理完全一致。
