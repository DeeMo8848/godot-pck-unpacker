# Godot PCK 解包工具

通用 Godot 游戏资源包（`.pck`）解包工具，支持**命令行**与**图形界面（EXE）**两种用法，
自动识别 PCK 版本，纹理（`.ctex` / `.stex`）自动提取内嵌图像并转为 PNG。

> ⚠️ **版本支持声明（重要）**
> 本工具当前**仅对 Godot 4 做过真实解包验证**（在 Godot 4.4.1 的 269MB 资源包上成功提取全部 4566 个文件）。
> 对 Godot 3.x 与 Godot 4.5+ 虽已编写兼容代码，但**尚未用真实样本测试**，如遇问题欢迎提 Issue。

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
- 利用 `.import` 映射把纹理**归位到开发时的原始路径**（干净文件名，如 `Sprites/Logo_Layers/5.png`）
- 加密文件自动跳过并提示
- 可选按类型/扩展名筛选导出（命令行与界面均支持）
- 图形界面：拖拽添加、选择输出文件夹、按分类导出、进度条、可取消

## 文件说明

| 文件 | 说明 |
|------|------|
| `godot_pck_unpacker.py` | 解包内核（命令行入口，自动识别版本、纹理转 PNG） |
| `godot_pck_gui.py`       | 图形界面（Tkinter，零额外基础依赖） |
| `godot-unpacker.py`      | 目录自带的原始参考工具（仅支持 Godot 3，本工具不依赖它） |
| `dist/GodotPCKUnpacker.exe` | 打包好的图形界面单文件版（见 Releases） |

## 命令行用法

```bash
# 基本解包（自动识别版本，纹理转 PNG 并归位）
python godot_pck_unpacker.py game.pck

# 指定输出目录
python godot_pck_unpacker.py game.pck -o out_dir

# 仅列出包内文件（支持筛选参数做预览）
python godot_pck_unpacker.py game.pck --list

# 按类型筛选：只导图像 + 音效 + UI 三类
python godot_pck_unpacker.py game.pck --include Sprites,Sounds,UI

# 转 PNG 时额外保留原始 .ctex（用于 Godot 工程重建）
python godot_pck_unpacker.py game.pck --keep-raw
```

筛选参数：`--include 前缀`、`--exclude 前缀`、`--ext 扩展名`（逗号或空格分隔）。

## 图形界面 / EXE 用法

- 方式一（需本机有 Python）：`python godot_pck_gui.py`
- 方式二（无需 Python）：直接双击 Releases 里的 `GodotPCKUnpacker.exe`；也可把 `.pck` 拖到 exe 图标上自动加载

界面功能：浏览/拖拽选择 `.pck` → 选择输出文件夹 → 选「全部」或勾选分类（图像/音效/字体/脚本/场景/着色器）→ 开始解包（带进度条、日志、取消）。

## 依赖

- 必须：Python 3 标准库
- 可选：Pillow（`pip install pillow`）—— 用于把 WebP 转成 PNG；没有则保存原始 WebP

## 重新打包 EXE

```bash
pip install pyinstaller pillow
python -m PyInstaller --onefile --windowed --name GodotPCKUnpacker ^
    --hidden-import PIL --hidden-import PIL.Image godot_pck_gui.py
# 产物在 dist/GodotPCKUnpacker.exe
```

## 许可证

见 `LICENSE`。
