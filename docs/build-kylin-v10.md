# 麒麟 V10 SP1 安装与适配

适配目标为银河麒麟桌面 V10 SP1 2403、x86_64 / 海光 Hygon C86、内核 5.4、glibc 2.31。当前固定 Cockpit Tools 1.3.74。ARM、龙芯和其他麒麟版本尚未验证。

官方 Linux 安装包需要 glibc 2.34、WebKitGTK 4.1、libsoup 3 和 OpenSSL 3；麒麟此版本提供 glibc 2.31 与 WebKitGTK 4.0。仅修改 deb 依赖声明或把 4.0 库改名成 4.1 都不能解决 ABI 差异。

本分支采用安装层适配，复用官方应用及 sidecar，不修改业务逻辑。独立的 Ubuntu 22.04 运行库放在用户目录，通过 Bubblewrap 的进程挂载命名空间加载。系统的 glibc、APT 源、WebKit 和其他应用不需要修改。兼容环境保留原有文件、桌面会话、密钥环和网络访问；它不构成安全沙箱。

## 构建运行库

宿主机需要 Python 3.8+、curl、Docker、Bubblewrap、xdg-dbus-proxy 和 `glib-compile-schemas`（libglib2.0-bin）。Docker 只在构建时使用，日常启动不需要容器。安装阶段以普通桌面用户执行。

```bash
git clone https://github.com/ifD07/cockpit-tools.git
cd cockpit-tools
git checkout codex/kylin-v10-support
bash scripts/kylin/build-runtime.sh
```

脚本从官方发布页下载 deb，并核验 `scripts/kylin/upstream.json` 中固定的 SHA256。基础镜像固定到 digest，容器内使用阿里云 Ubuntu 镜像并保留 APT 签名验证；宿主软件源不变。生成物位于 `dist-kylin/`：

- `runtime.tar.gz`：应用、sidecar、私有库和必要资源。
- `SHA256SUMS`：运行库归档校验值。
- `packages.tsv`：本次构建实际使用的软件包版本。
- `upstream.json`：上游应用来源与校验值。

APT 安全更新会使不同日期的构建依赖版本不同。请保留上述完整输出，以便复用同一运行库；本流程不承诺逐字节可复现构建。不需要 GitHub 令牌即可下载公开安装包或构建运行库。

## 安装及启动

```bash
python3 scripts/kylin/install.py dist-kylin/runtime.tar.gz \
  --sha256 "$(cut -d ' ' -f 1 dist-kylin/SHA256SUMS)"

~/.local/bin/cockpit-tools --kylin-check
~/.local/bin/cockpit-tools
```

安装器要求归档校验值，并在创建菜单入口前检查真实 ELF 依赖。默认目录为 `~/.local/opt/cockpit-tools/1.3.74-kylin1`，启动命令为 `~/.local/bin/cockpit-tools`，应用菜单入口为 `Cockpit Tools`。已有安装目录或同名启动命令存在时会停止，避免覆盖。

宿主必须使用 merged-/usr 布局，即 `/lib -> /usr/lib`、`/lib64 -> /usr/lib64`，并存在 `/usr/libexec` 目录；安装器会检查这些条件。还需允许普通用户启动 Bubblewrap。若 `bwrap --ro-bind / / -- /usr/bin/true` 被系统策略拒绝，请由管理员按本机策略配置，安装器不会关闭系统安全策略。

运行时会合并宿主独有库与私有运行库。宿主的 Python、Node.js、git、IDE 安装路径和用户 HOME 仍然可见，不会把用户数据复制进运行库。应用使用原有 `~/.cockpit_tools` / 兼容旧目录规则。启动器会去除从 Electron 父进程继承的私有 OpenSSL 路径，防止其覆盖本运行库。

主程序在宿主可见路径上保留启动包装脚本，在兼容命名空间内把该路径挂载为原始 ELF。这样应用自动登记的 OAuth / deep-link 回调和重启命令也会经过同一启动器，避免从浏览器回调时绕过运行库。

安装器会重新编译宿主与运行库合并后的 GSettings XML，不能直接使用 Ubuntu 自带的编译缓存，否则麒麟独有的桌面配置项会丢失，可能导致被启动的 GTK 客户端崩溃。

## Codex 客户端登录

Codex / ChatGPT 桌面客户端应使用宿主运行库，不能继承 Cockpit 的私有 Ubuntu 库。配置原生可执行文件后，启动器仅在 Cockpit 的命名空间内将该路径映射到桥接脚本；桥接脚本恢复宿主根目录，再执行原始客户端。宿主上的客户端文件不被修改。宿主根目录镜像保留普通用户原有的读写权限，以便独立登录目录正常写入；这不是权限隔离机制。

为已有安装修复配置并登记客户端：

```bash
python3 scripts/kylin/install.py \
  --repair ~/.local/opt/cockpit-tools/1.3.74-kylin1 \
  --codex-client /absolute/path/to/usr/lib/chatgpt/ChatGPT
```

将最后一个参数替换为本机客户端原生 ELF 的绝对路径。在 Cockpit 设置中，把 Codex 客户端路径设为同一路径，然后退出并重新打开 Cockpit。新安装也可在安装命令中附加 `--codex-client`。不要使用会强制覆盖 `CODEX_HOME` 或 `--user-data-dir` 的并行客户端启动脚本，否则临时登录会写入其他账号目录。

桥接保留 Cockpit 生成的独立 `CODEX_HOME`、Electron 数据目录和启动参数，使用该客户端附带的 CLI；若安装目录提供 `compat/openssl/lib/libssl.so.3`，只为该客户端设置对应 OpenSSL 路径。麒麟旧 Mesa 使用 GTK3 与 SwiftShader，不添加会被上游辅助进程过滤器误判的 `--disable-gpu` 或 `--disable-setuid-sandbox` 参数。登录授权仍须由用户在新客户端中完成，不能仅以进程存在判定登录成功。

Cockpit 启动器清除从父进程继承的 `CODEX_HOME` 和 `CODEX_ELECTRON_USER_DATA_PATH`，使默认账号写入与默认客户端都使用 `~/.codex`；显式创建的受管实例和临时登录目录仍由 Cockpit 在启动客户端时设置。不会自动迁移或覆盖旧并行目录中的账号。

新安装及 `--repair` 会在兼容环境内重新查询合并后的 GTK 输入法模块，生成包含宿主 Fcitx 的 `immodules.cache`，并保留桌面会话的 `GTK_IM_MODULE` 与 `XMODIFIERS`。Ubuntu 原始缓存不包含麒麟宿主的 Fcitx，会导致文本框无法中文输入。修复后需要退出并重开 Cockpit，才会重新创建输入上下文。

仅修复 GSettings 缓存可使用 `--refresh-schemas PREFIX`。修复脚本不会修改 Cockpit 账号、客户端账号或系统库。

海光旧 Mesa 环境默认使用软件渲染和 X11。若已验证硬件渲染稳定，可对单次启动设置：

```bash
LIBGL_ALWAYS_SOFTWARE=0 ~/.local/bin/cockpit-tools
```

## 验证与更新

```bash
npm run test:kylin
bash -n scripts/kylin/build-runtime.sh
~/.local/bin/cockpit-tools --kylin-check
```

`--kylin-check` 使用私有动态加载器核验应用依赖，不会发送模型请求。实际桌面验证还应确认主窗口显示，应用日志出现“前端已就绪”，并检查 sidecar 与需管理的 IDE 能正常运行。仅进程存活不能证明前端可用。

2026-10-26 已在麒麟 V10 SP1 2403、Hygon C86-3G 实机完成安装和启动验证：主窗口显示为 1280 × 800，前端上报 `react_mounted`，deep-link 注册完成。主程序依赖检查通过；兼容环境内的宿主 git、Node.js、Python/SSL 均可执行。宿主 glibc 保持 2.31，应用使用私有 glibc 2.35。实际启动检查发现的 `/dev/null` 访问问题已通过显式 `--dev-bind /dev /dev` 修复，并加入回归测试。未代用户导入或切换 IDE 账号。

该安装不受系统 dpkg 管理，应用内不能按系统 deb 方式更新。升级时先更新 `upstream.json` 的版本、URL 和官方 SHA256，再重新构建。退出应用后保留旧安装目录，将旧的 `~/.local/bin/cockpit-tools` 软链接移走，再使用新的 `--prefix` 安装并验证。恢复旧软链接即可回退。不要在麒麟上直接安装上游的新 deb 来覆盖私有运行库。

退出 Cockpit 后，删除 `~/.local/bin/cockpit-tools` 软链接、`~/.local/share/applications/cockpit-tools-kylin.desktop` 和对应版本的私有安装目录即可卸载。账号数据不随卸载删除。

## 适配代码

- `scripts/kylin/Dockerfile`：构建应用私有运行库。
- `scripts/kylin/build-runtime.sh`：下载校验、构建与导出。
- `scripts/kylin/install.py`：校验归档、生成宿主资源合并视图、安装启动及菜单入口。
- `scripts/kylin/launcher.py`：兼容命名空间、环境清理与依赖诊断。
- `scripts/kylin/host-client.py`：宿主 Codex 客户端桥接，保留独立登录目录。
- `scripts/kylin/test_runtime.py`：合并视图、归档边界、环境与桌面入口回归测试。
