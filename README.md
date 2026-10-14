# cull3fr

哈苏 X2D II 的轻量选片工具。在浏览器里用键盘快速过片，打 1 到 5 星和红黄绿颜色标签，最后一次性写进 Phocus 的 `.phos`，回到 Phocus 里直接按星级、颜色筛选导出。

A lightweight culling tool for Hasselblad X2D II shoots. Rate and colour-label photos in the browser with the keyboard, then write all marks into Phocus `.phos` sidecars in one go.

> 本项目与 Hasselblad 无关。Hasselblad、Phocus、X2D 是 Hasselblad 的商标。

## 它做什么

- **看图快**：不解码 1 亿像素的 RAW，直接读相机已经写在文件里的预览图（X2D II 的 3FR 和 HEIC 里各有一张 3888×2918 的 JPEG）。
- **键盘选片**：`1` 到 `5` 打星，`6` `7` `8` 打绿、黄、红（和 Phocus 的按键一样），打完自动跳到下一张。
- **100% 查看**：按空格看原图像素；放大状态下翻页会停在同一位置，方便比较连拍的焦点。
- **RAW 和 HEIC 自动配对**：同名的 `3FR` 和 `HEIC` / `HEIF` / `JPG` 算作一张。两边评分不一致时按 RAW 显示，并标出「不一致」。
- **选片过程不碰照片**：标记实时存在照片文件夹里的 `.cull3fr.json`，关掉再开会接着上次。
- **全部写入**：点一次，把改过的星级和颜色写进 `.phos`。写前自动备份，可以撤销。
- **按颜色分文件夹（可选）**：把照片连同 `.phos` 移到 `_green` / `_yellow` / `_red`，可以撤销。
- **筛选**：全部、未标、绿、黄、红、1★ 以上、3★ 以上、不一致、未写入。

## 要求

- macOS：用系统自带的 `sips` 生成 100% 大图，用 `osascript` 弹出选文件夹窗口。
- Python 3.9 或更新（系统自带、Homebrew、Anaconda 等都可以），不需要安装任何第三方包。
- 测试过的组合：Hasselblad X2D II 100C + Phocus 4.x 写出的 `.phos`（Version 31；X2D II 需要 Phocus 4.0 以上）。其他机型、其他 Phocus 版本没有测过，见下面「已知限制」。

## 安装和使用

1. 下载 `cull3fr.py`（或者 clone 整个仓库），放在一个固定的位置，例如 `~/cull3fr/`。
2. 打开终端运行：
   ```
   python3 ~/cull3fr/cull3fr.py
   ```
   在弹出的窗口里选这次拍摄的文件夹，浏览器会自动打开。
3. **以后不用终端**：在网页里点「? 快捷键」，再点「生成 cull3fr App」，或者运行 `python3 cull3fr.py --install-app`。会生成 `~/Applications/cull3fr.app`（深色方块加绿黄红三个点的图标），之后用 Spotlight 搜「cull3fr」或从 Dock 双击打开。App 里记的是 `cull3fr.py` 和当时所用 Python 的完整路径，生成以后不要再移动 `cull3fr.py`；换了 Python 要重新生成。第一次打开时 macOS 可能会问能否访问桌面或外接硬盘上的文件，点「允许」。
4. 选片：键盘打星、打颜色。左下角 📁 可以换文件夹。
5. 写入：点「全部写入」，弹窗里会先列出要改哪些照片、哪些写不进去，确认后写入。**写入前请退出 Phocus。**
6. 回到 Phocus 打开这个文件夹，按星级或颜色筛选后导出。
7. 用完点右下角「退出」。网页关掉 60 分钟后也会自动退出。

## 快捷键

| 按键 | 作用 |
|---|---|
| `→` `←` | 下一张、上一张 |
| `1` … `5` | 打星；`0` 清除星级 |
| `6` `7` `8` | 绿、黄、红（也可以用 `G` `Y` `R`）；`9` 清除颜色 |
| `⇧` + 上面的键 | 打标但不跳下一张，方便同一张既打星又打颜色 |
| `A` | 自动跳下一张：开 / 关 |
| `空格` 或单击图片 | 100% 查看；拖动平移；再按一次退出 |
| `F` / `⇧F` | 切换筛选 |
| `E` | 全部写入 |
| `O` | 换文件夹 |

## 写进 Phocus 的方式

下面是对照 Phocus 4.x 实际写出的 `.phos` 得出的：

- `.phos` 是一个 XML plist，放在照片旁边，文件名是「照片完整文件名 + `.phos`」，例如 `B0000001.3FR.phos`。
- **颜色**：顶层整数 `Approval`，绿 = 0，黄 = 1，红 = 2。1 也是 Phocus 的默认值，所以在 Phocus 里黄色和「没打过颜色」看起来一样；需要单独拿出黄色时，用「按颜色分文件夹」。
- **星级**：顶层字符串 `XMPData`，里面是一段 XMP，星级在 `<xap:Rating>N</xap:Rating>`。没打过星的 `.phos` 没有这个键，工具会按 Phocus 第一次打星时的写法补上。
- **相机机内评分**：在 3FR 文件内部的 XMP（`xap:Rating`）。工具只读，不改。

写到哪里：

| 照片 | 写到 |
|---|---|
| 3FR（不管有没有同名 HEIC / JPG） | `X.3FR.phos` |
| 只有 HEIC，没有 RAW | `X.HEIC.phos` |
| FFF | 不写（FFF 的标签存在文件内部，工具不改照片文件） |
| 只有 JPG | 不写 |

先拷 HEIC、后拷 3FR 的情况：只有 HEIC 时写过的标记，3FR 到了之后会自动带过去，下次「全部写入」写进 3FR 的 `.phos`。

### 安全措施

- **照片文件（3FR / HEIC / JPG）从不修改。**
- 只改 `.phos` 里的 `Approval` 和 `XMPData` 星级这两个值：先确认能把原文件逐字节重新生成出来，再在此基础上修改，所以其他字节保证不变。格式对不上（其他版本、二进制 plist 等）的 `.phos` 一律跳过，并在写入结果里列出来。
- 只写你在工具里改过的照片。
- 每次写入前把原 `.phos` 备份到 `.cull3fr_backup/`，先记撤销日志再改文件；写之前再确认一次文件没被别的程序改过。
- 写入成功后，工具里的标记让位给文件本身：之后在 Phocus 里做的修改，不会被下一次写入覆盖。
- 撤销只在文件还是工具写的那个版本时才还原；之后又被 Phocus 改过的，保留现状。
- 本地网页服务只接受本机请求，每次启动生成一个随机口令，你浏览的其他网站调用不了它。
- 同一时间只运行一个；再次打开时会把文件夹交给已经在运行的那个。

## 已知限制

- **写入时请退出 Phocus。** Phocus 开着时，之后保存可能会覆盖刚写的值。工具会检测 Phocus 是否在运行并提醒。
- **只测过 X2D II + Phocus 4.x。** 其他机型或 Phocus 版本写出的 `.phos` 如果格式不同，会被跳过，不会写错。
- **只有 HEIC 的照片、而且还没有 `.phos` 时**：Phocus 会在 HEIC 的 `.phos` 基线里存一个按这张图算出来的色温，工具算不出来，写的是 0。这个 0 在 Phocus 里是否等于「按原样显示」还没有确认，写完请在 Phocus 里看一眼颜色。已经有 `.phos` 的 HEIC 不受影响。
- 新建 3FR `.phos` 用的是 Phocus「只打标、不调参数」时写出的基线设置，没有按镜头逐一核对。
- 改写 `.phos` 时不保留 macOS 扩展属性（Finder 信息）。
- 生成的启动器是本机的 AppleScript 小程序，只有本机签名（ad-hoc），没有 Apple 开发者签名。

## 文件放在哪里

| 位置 | 内容 |
|---|---|
| 照片文件夹 `.cull3fr.json` | 标记 |
| 照片文件夹 `.cull3fr_log.json` | 撤销记录 |
| 照片文件夹 `.cull3fr_backup/` | 每次写入前的 `.phos` 备份 |
| `~/Library/Caches/cull3fr/` | 预览缓存（30 天没用或总量超过 3 GB 自动清理） |
| `~/Library/Logs/cull3fr.log` | 从 App 启动时的日志 |

照片文件夹里的这几个都以点开头，Finder 默认不显示（`⌘⇧.` 可以显示）。照片卡、相机这类只读位置，标记会存到缓存目录里。

## 命令行

```
python3 cull3fr.py [文件夹]           # 打开（不填会弹窗选）
python3 cull3fr.py 文件夹 --inspect   # 只打印每张照片读到的星级、颜色，不开界面
python3 cull3fr.py 文件夹 --undo      # 撤销最近一次写入或移动（工具没在运行时）
python3 cull3fr.py --install-app      # 生成 ~/Applications/cull3fr.app
python3 cull3fr.py --port 8765        # 指定端口（默认 8765，被占用会自动换）
```

## 卸载

删除 `cull3fr.py`、`~/Applications/cull3fr.app`、`~/Library/Caches/cull3fr/`、`~/Library/Logs/cull3fr.log`。照片文件夹里的 `.cull3fr*` 可以留着也可以删，删掉不影响照片和 `.phos`。

## 更新记录

- **0.5.1**：修复「生成 cull3fr App」生成的 App 打不开（提示 not supported on this Mac）的问题，并加上图标。0.5 生成过 App 的，换成新的 `cull3fr.py` 后重新生成一次即可，旧的会被替换。

## 从 HB Cull 升级

0.5 以前这个工具叫 HB Cull（文件名 `hb_cull.py`）。新版本第一次打开一个文件夹时，会自动接着用里面的 `.hb_cull.json`（标记）和 `.hb_cull_log.json`（撤销记录），旧文件不动。旧的 `HB Cull.app`、`hb_cull.py`、`~/Library/Caches/hb_cull/` 可以删掉。

## 测试

```
python3 tests/test_cull3fr.py
```

- 不需要真实照片：测试用的 `.phos`、3FR、HEIC 都在运行时生成，仓库里不含任何照片或 sidecar 样本。
- 浏览器相关的测试需要 `pip install playwright`，没装会跳过。
- 设置 `CULL3FR_SAMPLES=<本机放 X2D II 3FR 和 HEIC 的文件夹>` 会多跑一项真实文件测试，只读不写。不要把这个文件夹提交到仓库。

## License

MIT，见 [LICENSE](LICENSE)。
