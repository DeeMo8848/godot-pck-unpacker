# Godot PCK 解包工具

通用 Godot 游戏资源包（`.pck`）解包工具，支持**命令行**与**图形界面（EXE）**两种用法，
自动识别 PCK 版本，纹理（`.ctex` / `.stex`）自动提取内嵌图像并转为 PNG，音频（`.sample`）与字体（`.fontdata`）自动还原为可用的 `.wav` / `.ttf`。

> ⚠️ **版本支持声明（重要）**
> 本工具当前**仅对 Godot 4 做过真实解包验证**（在 Godot 4.4.1 的 269MB 资源包上成功提取全部 4566 个文件）。
> 对 Godot 3.x 与 Godot 4.5+ 虽已编写兼容代码，但**尚未用真实样本测试**，如遇问题欢迎提 Issue。
>
> **实测结果**：4566 个文件 → **1722 张 PNG + 42 个 WAV + 55 个 TTF**，0 错误，字体经 Windows 正常识别。

| PCK 版本 | 对应 Godot      | 状态                                              |
|----------|----------------|--------------------------------------------------|
| v2       | Godot 4.0–4.4  | ✅ 实测成功（Godot 4.4.1，真实解包 4566 文件）      |
| v3       | Godot 4.5+     | 🧪 代码已支持（新增 DirectoryOffset），未经实际测试 |
| v1       | Godot 3.x      | 🧪 代码已支持（绝对偏移、条目无 flags），未经实际测试 |
| —        | Godot 1/2      | ❌ 不支持（`PCKG` 魔数不同）                        |

---

## 功能特性

- 自动识别 PCK 版本（读 FormatVersion），无需手动指定
- 解包全部文件，保留 `res://` 目录结构
- 纹理自动转 PNG（Godot 4 的 `.ctex` 内为 WebP，提取后转 PNG；无 Pillow 时回退存 `.webp`）
- 音频自动还原为可播放的 `.wav`（解析 `AudioStreamWAV` 资源，自行封装 RIFF/WAVE）
- 字体自动还原为可安装的 `.ttf`/`.otf`（解压 `RSCC`/zstd 后提取内嵌字体）
- 利用 `.import` 映射把资源**归位到开发时的原始路径**（干净文件名，如 `Sprites/Logo_Layers/5.png`、`Sounds/Bump.wav`）
- 加密文件自动跳过并提示
- 可选按类型/扩展名筛选导出（命令行与界面均支持）
- 图形界面：拖拽添加、选择输出文件夹、按分类导出、进度条、可取消
- 界面选「全部」时分类复选框自动全选置灰，选「自定义分类」时自动清空

## 文件说明

| 文件 | 说明 |
|------|------|
| `godot_pck_unpacker.py` | 解包内核（命令行入口，自动识别版本、纹理转 PNG） |
| `godot_pck_gui.py`       | 图形界面（Tkinter，零额外基础依赖） |
| `godot-unpacker.py`      | 目录自带的原始参考工具（仅支持 Godot 3，本工具不依赖它） |
| `dist/GodotPCKUnpacker.exe` | 打包好的图形界面单文件版（见 Releases） |

> `data.pck` / `data_unpacked/` / `extracted_assets/` 等为本地测试资源与产物，**已在 `.gitignore` 排除，不入库**。

## 命令行用法

```bash
pip install pillow zstandard          # 可选依赖（强烈建议）
python godot_pck_unpacker.py game.pck # 一条命令全解包
```

| 参数 | 说明 |
|------|------|
| `pck` | **(必填)** .pck 文件路径 |
| `-o, --output DIR` | 输出目录（默认 `<pck名>_unpacked`） |
| `--no-convert` | 不把纹理转成 PNG/WebP，保留原始 `.ctex/.stex` |
| `--no-media` | 不还原音频/字体，保留原始 `.sample/.fontdata` |
| `--no-organize` | 不按 `.import` 映射归位，资源留在 `.godot/imported/` 下 |
| `--keep-webp` / `--keep-raw` | 转 PNG 时额外保留 `.webp` / 原始 `.ctex`（用于 Godot 工程重建） |
| `--no-meta` | 不导出 `.import/.remap` 元数据（约 1KB 纯文本） |
| `--include A,B` | 只导出这些路径前缀/分类（如 `Sprites,UI`） |
| `--exclude A,B` | 排除这些路径前缀（如 `.godot`） |
| `--ext x,y` | 只导出这些扩展名（源或转换后均可，如 `png`/`wav`/`ttf`） |
| `--list` | 仅列出包内文件，不解包（可配合筛选预览） |
| `-q, --quiet` | 减少输出 |

```bash
# 只要美术素材（输出干净开发原名 PNG）
python godot_pck_unpacker.py game.pck --ext png --no-meta

# 只要音频 + 字体（自动还原为可播放 .wav / 可安装 .ttf）
python godot_pck_unpacker.py game.pck --ext wav,ttf --include Sounds,Fonts

# 只提取 Sprites / UI 两个目录（先预览再提取）
python godot_pck_unpacker.py game.pck --list --include Sprites
python godot_pck_unpacker.py game.pck --include Sprites,UI

# 除引擎缓存目录外全导出
python godot_pck_unpacker.py game.pck --exclude .godot
```

> 完整参数说明、筛选规则与 FAQ 见 **[README_godot_pck_unpacker.md](README_godot_pck_unpacker.md)**。

## 图形界面 / EXE 用法

- 方式一（需本机有 Python）：`python godot_pck_gui.py`
- 方式二（无需 Python）：直接双击 Releases 里的 `GodotPCKUnpacker.exe`；也可把 `.pck` 拖到 exe 图标上自动加载

界面功能：浏览/拖拽选择 `.pck` → 选择输出文件夹 → 选「全部」或勾选分类（图像/音效/字体/脚本/场景/着色器/资源）→ 开始解包（带进度条、日志、取消）。

> PCK 里音频/字体是**打包后格式**（`.sample`/`.fontdata`），不是 `.wav`/`.ttf`。勾选「音频/字体还原为可用格式」（默认开）后会输出可直接播放/安装的 `.wav`/`.ttf`。

## 依赖

- 必须：Python 3 标准库
- 可选：Pillow（`pip install pillow`）—— 用于把 WebP 转成 PNG；没有则保存原始 WebP
- 可选：zstandard（`pip install zstandard`）—— 用于解压 `.fontdata` 还原字体；没有则跳过字体转换（不报错）

## 重新打包 EXE

```bash
pip install pyinstaller pillow zstandard
python -m PyInstaller --onefile --windowed --name GodotPCKUnpacker ^
    --hidden-import PIL --hidden-import PIL.Image --hidden-import zstandard godot_pck_gui.py
# 产物在 dist/GodotPCKUnpacker.exe
```

## 许可证

见 `LICENSE`。
