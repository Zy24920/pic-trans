# 桌面翻译（文本翻译 + 截图图片翻译）

基于百度翻译 API 的 Windows 桌面小工具：文本自动翻译 + 全局热键截图图片翻译（OCR + 译文贴合回图）。

## 文件结构

```
pic_trans/
├── fanyi_pro_v2.py            # 主程序（推荐，修复版：持久化/日志/若干 UX 修复）
├── fanyi_pro.py               # 主程序（原始版本，保留作对照）
├── baidu_pic_translate.py     # 百度图片翻译 V2.0 核心库（GUI 共用，也可独立当 CLI 用）
├── textrans.py                # 文本翻译模块（百度文本翻译 API 封装）
├── test_baidu_pic_translate.py# 核心库单元测试（62 例，纯本地不发网络请求）
└── requirements.txt           # 运行依赖
```

## 安装

```powershell
pip install -r requirements.txt
```

要求 Windows 10+、Python 3.9+。

## 配置

1. **图片翻译凭证**：编辑 `fanyi_pro_v2.py` 顶部的 `BAIDU_APPID / BAIDU_TOKEN`，
   或设置环境变量 `BAIDU_FANYI_APPID / BAIDU_FANYI_TOKEN`（环境变量优先）。
   图片翻译开通见 <https://fanyi-api.baidu.com/product/233>。
2. **文本翻译凭证**：编辑 `textrans.py` 顶部的 `appid / appkey`
   （百度文本翻译 API，与图片翻译相互独立）。
   未配置时文本翻译不可用，截图翻译不受影响。

## 使用

```powershell
python fanyi_pro_v2.py        # 带控制台运行
pythonw fanyi_pro_v2.py       # 无窗口后台运行（错误请查日志）
```

- **文本翻译**：粘贴/输入自动翻译（可关），Ctrl+Enter 手动触发，右键复制译文。
- **截图翻译**：全局热键 `Ctrl + Alt + D` → 框选区域 → 悬浮窗展示
  译文图 / 原图 / 对比 / 文本 四种视图，支持切换语言重译、复制、保存、置顶、缩放。

### 图片翻译 CLI（baidu_pic_translate.py 单独使用）

```powershell
python baidu_pic_translate.py -i test.png -t en -p 1 -o output
python baidu_pic_translate.py -i imgs/ -t en -p 2 --compose --workers 4 -o output
python baidu_pic_translate.py --list-langs --list-modes
```

## 数据与日志位置

| 内容 | 路径 |
|---|---|
| 语言/开关设置 | `%APPDATA%\fanyi_pro_v2\config.json` |
| 运行日志（1MB×3 滚动） | `%APPDATA%\fanyi_pro_v2\logs\fanyi_pro_v2.log` |

## 运行测试

```powershell
python -m unittest discover -v
```

覆盖：Base64 编解码、图片格式判断、rect/points 解析、错误码清洗、
参数校验、区块合成、结果落盘、网络层重试分类（mock，不联网）。

## 打包分发（PyInstaller）

```powershell
pip install pyinstaller
pyinstaller --noconsole --onefile --name FanyiPro --hidden-import textrans fanyi_pro_v2.py
```

- 产物在 `dist/FanyiPro.exe`，双击即可运行（无控制台窗口）。
- `--hidden-import textrans`：textrans 是 try/except 动态导入，需显式声明才会打进包；
  若不使用文本翻译功能可去掉该参数。
- 若想双击脚本直接运行而不打包，把 `fanyi_pro_v2.py` 复制一份改后缀为 `.pyw` 即可。
