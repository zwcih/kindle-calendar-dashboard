# Kindle Calendar Dashboard

包含**服务端图片生成器**与 **Kindle 客户端**：服务端将 CalDAV 日历和 Open-Meteo 天气预报渲染为适合 Kindle Paperwhite 3 的 `1072×1448` 三阶灰度 PNG，并可上传到 WebDAV；客户端下载图片、通过 FBInk 显示，在专用模式中定时刷新并进入真实内核休眠。两侧独立运行、配置分离，Kindle 不需要 Python。

| 部分 | 源码 | 职责 |
| --- | --- | --- |
| 服务端 | `dashboard.py`、`update_no_model.py` | 获取日历和天气、生成图片、可选 WebDAV 上传；由用户安排服务端调度 |
| 客户端 | `kindle/kindle-dashboard/`、`kindle/documents/` | 手动刷新、UTC+8 06:30–22:00 半小时刷新、真实休眠、触摸退出与界面恢复 |

已有可用图片地址时，可直接按下方客户端快速开始部署；需要自行生成图片则继续阅读[服务端安装](#服务端安装)。

## Kindle 客户端快速开始

需要已安装、可从书库执行 shell 入口的 **libkh**、`/mnt/us/libkh/bin/fbink`、支持严格 HTTPS/TLS 的 `curl`、有效 CA/系统时间，以及设备已有的 LIPC、Upstart、Linux `/proc` 与 `flock -n FD`。目标设备的 BusyBox ash 已实测支持锁竞争、跨 `nohup setsid /bin/sh` 继承，以及父进程被强杀后子进程继续持锁；缺少所需能力会明确拒绝，不自动安装依赖或提供不安全回退。

专用模式只针对已核验的 PW3 接口：`max77696-rtc.0` 相对 RTC 闹钟、`/sys/power/state`、含休眠计数的 `/proc/uptime`、`lab126_gui` 及 ARM32 `cyttsp4_mt` 触屏。**不保证所有 PW3 或固件兼容**，也不包含越狱、固件升级或开机钩子。

1. 确保服务端已生成并上传图片，取得可直接下载 PNG 的 HTTPS 地址；上传地址和客户端读取地址不一定相同。客户端不接收服务端 CalDAV/WebDAV 密码。
2. 将 [`kindle/kindle-dashboard/config.example.conf`](kindle/kindle-dashboard/config.example.conf) 复制为同目录的 `config.local.conf`，仅在本地填入 `IMAGE_URL` 和 Kindle 已保存网络的 `WIFI_SSID`。配置是字面 `KEY=value` 数据：不加引号、不写 `export`、不执行 shell 展开，不存 Wi-Fi 密码。使用 UTF-8 无 BOM、LF；这个私有文件已被 Git 忽略，空白模板不能直接运行。
3. 将仓库 `kindle/` 内两个目录的内容分别合并到 **Kindle USB 根目录**的同名目录，不能把外层 `kindle` 套进去。最终应为：

```text
documents/
  calendar-manual-refresh.sh
  calendar-dedicated-start.sh
  calendar-dedicated-recover.sh
kindle-dashboard/
  calendar-dedicated.sh
  calendar-auto-refresh.sh
  calendar-config.sh
  calendar-lock.sh
  config.example.conf
  config.local.conf              # 本地私有配置，不在仓库中
```

4. 安全弹出 USB，保持 Kindle 唤醒并连接已保存的 Wi-Fi，从书库打开“手动刷新日程”。首次成功后才会建立 `kindle-dashboard/dashboard.png` 缓存。
5. 缓存已建立后打开“启用日程专用模式（半小时）”。它停止 GUI、关闭前光，按 **UTC+8 06:30–22:00 每半小时**刷新；其间真实休眠，夜间等待次日 06:30。

使用时：**电源键唤醒 → 单指短按松开刷新 → 单指按住至少 2 秒再松开退出**；也可从书库运行“退出日程专用模式”。退出恢复 GUI、Home、原前光和原生睡眠。多指期间不识别新单指手势，直到所有触点释放。电量未知或不超过 20% 且未充电时不联网；手动请求只跳过安静时段，不跳过电量与所有权检查。

可选动态图片接口：显式配置 `IMAGE_MODE=dynamic`，让 `IMAGE_URL` 指向自己的 HTTPS 服务，并单独创建忽略的 `image-auth.local.conf` 保存 Bearer 数据。客户端 POST 经校验的电量与充电 JSON，禁用 `Expect:`，不跟随跳转，不在参数或日志中放 Bearer；配置缺失则失败，不回退 GET。省略新模式配置仍按原静态 GET 工作。返回的完整 PNG 直接走原 FBInk 和原子缓存链，电量导致图片字节变化时会实际刷新；客户端不叠字、不编辑 PNG。配置格式、FAT 凭据存储风险和运行快照见[动态 PNG 配置](kindle/README.md#可选服务端动态-png)。本仓库不部署动态 HTTP 服务；动态独立手动刷新已在目标 Kindle 完成 HTTP 200、整图显示、原子缓存和正常退出验收。

**从旧版升级**：先用旧入口正常退出并确认恢复，再正常重启 Kindle（不是恢复出厂设置或固件升级），之后连接 USB 更新全部四个运行脚本／库及三个书库入口，保留有效私有配置，安全弹出后再使用。不要混用旧运行快照，也不要另留一套旧命名入口来启动旧版本。不要手工删除旧 `.lock` 目录或新版 `.flock` 文件来“解锁”；启动者、worker 与存活子进程的互斥由内核锁管理。

原静态稳定主链已实测独立手动下载／显示／缓存、专用模式启动与再次启动、真实休眠、单指刷新和长按退出的完整恢复。本次动态协议目标测试 13 项及完整回归 94 项已在 Linux `/bin/sh=dash` 通过，包含真实本地 TLS/curl、并发、严格 SIGKILL 与恢复。**动态专用模式的新快照、手势和睡眠尚未真机验收**，不沿用静态版本的成功作为通过证据；完整崩溃套件、物理多指序列、半小时自动时隙及整夜运行仍有设备验证缺口。详见[客户端依赖、配置与排障](kindle/README.md)。

## 服务端特性

- 今天的未完成日程优先；今天结束后自动整屏切换到明天
- 今天仅剩 1–2 项时，利用空余区域补充明天的日程
- 短标题单行放大，长标题优先使用两行大字
- 紧凑的日期、天气、温度和降雨概率头部
- 支持全天事件、重复事件展开、取消事件和时区转换
- 本地 PNG 原子替换；WebDAV 远端内容未变化时跳过上传
- 独立的无模型更新器：严格校验今天/明天天气，原子更新缓存，失败时降级
- 配置、账号、端点、坐标和生成图片均与源码分离

## 数据流

```text
update_no_model.py (standalone, no model/agent runtime)
  ├─ Open-Meteo → validate two local dates/numbers → atomic weather cache
  │                 failure → valid same-date cache or no weather
  └─ dashboard.py functions: CalDAV → local grayscale PNG → optional WebDAV PUT
                                                           ↓ HTTPS PNG
Kindle: calendar-auto-refresh.sh → validate PNG → FBInk → atomic local cache
        calendar-dedicated.sh → half-hour schedule / real suspend / UI recovery
```

更新器只依赖 Python 标准库和已有 `dashboard.py`（使用 Pillow），不需要 OpenClaw、模型 API、聊天会话或模型凭据。

## 服务端安装

需要 Python 3.11+、Pillow、IANA 时区数据库和支持中文的 Noto Sans CJK 字体。

```bash
git clone https://github.com/zwcih/kindle-calendar-dashboard.git
cd kindle-calendar-dashboard
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.local.json
```

编辑 `config.local.json`，替换示例值：

- `nextcloud.caldav_url`：CalDAV 日历集合 HTTPS URL
- `nextcloud.webdav_url`：PNG 上传 HTTPS URL；使用 `--no-upload` 时可省略
- `nextcloud.username`：Nextcloud 用户名
- `nextcloud.password_env`：保存应用密码的环境变量名，不是密码本身
- `weather.latitude` / `weather.longitude`：天气位置
- `weather.timezone`：IANA 时区，例如 `Asia/Shanghai`
- `display`：目标屏幕尺寸
- `fonts`：本机中文字体路径

`config.local.json` 已被 `.gitignore` 排除；字段规范见 `config.schema.json`。现有配置加载器拒绝未知字段、HTTP 认证端点、明文凭据字段、非法坐标和非法密码变量名，但并不执行完整 JSON Schema 校验。更新器另要求实际使用的端点有主机名且不含 URL 内嵌账号/密码、空白或片段。

密码仅从 `nextcloud.password_env` 指定的环境变量读取。不会调用凭据管理 CLI，也不会自动读取 `.env`。交互式 Bash 中可隐藏输入，避免把密码字面量写入历史：

```bash
read -r -s -p 'Nextcloud app password: ' NEXTCLOUD_PASSWORD; printf '\n'
export NEXTCLOUD_PASSWORD
```

变量名应与配置一致。`.env.example` 只是变量清单。定时任务应由调度器安全注入环境，或读取仓库外、权限为 `0600` 的专用环境文件；不要把密码放在命令行、源码或公开任务定义中。

默认读取脚本目录下的 `config.local.json`。也可设置：

```bash
export KINDLE_DASHBOARD_CONFIG=/path/to/config.json
```

以下环境变量覆盖配置：

- `KINDLE_CALDAV_URL`
- `KINDLE_WEBDAV_URL`
- `KINDLE_NEXTCLOUD_USER`
- `KINDLE_WEATHER_LAT` / `KINDLE_WEATHER_LON`
- `KINDLE_TIMEZONE`

注意现有加载器的边界：若 JSON 中一项坐标是数值 `0`、另一项非零，会被其配对检查误判。无需改源码，可将两项坐标都通过上述环境变量以字符串提供。更新器自身接受合法零坐标。

## 推荐运行方式：无模型更新器

从仓库目录运行，刷新天气、读取日历、渲染并上传：

```bash
python update_no_model.py
```

指定本地输出及天气缓存：

```bash
python update_no_model.py --output output/dashboard.png --weather-file output/weather.json
```

只跳过上传：

```bash
python update_no_model.py --no-upload
```

**`--no-upload` 不是离线模式**：仍会请求 Open-Meteo 天气和通过认证的 CalDAV 日历，也仍会写本地缓存和图片。即使缓存存在，每次运行仍尝试刷新天气。更新器没有离线运行开关；真正离线的是下述测试。CalDAV 读取也需要应用密码。

`--output`、`--weather-file` 的相对路径相对于当前工作目录，默认分别为 `output/dashboard.png`、`output/weather.json`。更新器不改变工作目录；调度器必须设置正确工作目录或传绝对路径。输出、缓存和所选配置/入口源码路径不能相同。选择专用、非符号链接的输出路径，不要指向其他重要文件。

更新器复用 `dashboard.fetch_events(..., 1, ...)`（该接口同时查询到次日）、`parse_weather_payload`、`render(..., 1)` 和 `upload`，不调用具有不同天气回退语义的 `dashboard.main()`。天气始终是今天和明天两天。

### 天气校验、缓存和降级

- 请求固定公共 Open-Meteo HTTPS forecast API，传配置坐标、IANA 时区、`forecast_days=2`、摄氏温度及四个 daily 字段。天气请求不携带 Nextcloud 凭据。
- 单次请求超时参数为 20 秒，最多读取 128 KiB + 1 字节检测超限；不重试。此超时不是整个任务的总时限。
- `daily.time` 必须**恰好按顺序等于配置时区的今天、明天**；拒绝旧日期、未来错位、缺日、重复、倒序或额外日期。四个数值数组都必须恰好有两个元素。
- 数值必须是有限 JSON 数字，拒绝字符串、布尔值、null、NaN 和无穷。天气代码必须属于受支持 WMO 代码集合；整数值浮点代码（如 `3.0`）可接受。
- 每天温度必须满足 `-100 ≤ 最低温 ≤ 最高温 ≤ 70`（摄氏），最大降雨概率为 `0–100`。这是防止异常数据的合理性边界，不是预报准确性保证。显示沿用现有解析器：温度四舍五入，非整数降雨概率截断为整数。
- 如果 JSON 提供 `daily_units`，必须匹配 ISO 日期、WMO 代码、摄氏和百分比；兼容不带单位元数据的旧缓存。
- 只有通过全部校验的数据才写同目录临时文件，flush、fsync 后通过 `os.replace` 替换缓存。网络、解析、校验或提交前写入失败时，**旧缓存逐字节保留**；不会先清空或删除它。临时文件会尝试清理。
- 刷新失败（包括无法保存新数据）时，只使用通过同样日期/数值校验的旧缓存；旧缓存缺失、过期、损坏、过大或不可读则传空天气列表，页面显示“天气暂缺”。不会再由渲染器发起第二次天气请求。
- 天气阶段后重新取本地时间；如跨过午夜，旧日期数据不用于渲染，而重新检查缓存或显示天气暂缺。页面使用日历请求前的时间快照，不会在慢日历请求/渲染期间再次切换日期。

“新鲜”仅指覆盖当前这两个本地日期：不检查缓存的当天生成时间、不设置小时级 TTL，也不验证缓存来源/地点。换位置或时区后，应换用新的专用缓存路径或自行移走旧缓存，避免在首次刷新失败时沿用另一个位置的预报。

### 更新器退出码与失败行为

| 退出码 | 含义 |
| --- | --- |
| `0` | 新天气已写缓存，日历和渲染成功；上传成功、远端内容不变或显式禁用上传 |
| `1` | 日历获取、天气转换、PNG 渲染或 WebDAV 上传失败；优先于天气降级状态 |
| `2` | CLI 参数错误，或配置/凭据/依赖加载失败；不开始天气刷新或渲染（`--help` 返回 `0`） |
| `3` | 日历和渲染成功、上传成功/无需上传，但天气刷新失败或跨午夜失效；使用有效旧缓存或无天气 |

普通错误日志只含阶段和异常类型，不打印密码、端点、坐标、响应正文或日程内容。成功日志标明 `weather=fresh/cache/missing` 和 `upload=uploaded/unchanged/disabled`。中断/强制终止不映射到以上应用退出码。

天气失败不阻断日历更新。日历获取或渲染失败时不会调用上传；缓存刷新是独立提交，之后渲染/上传失败不会回滚已成功刷新过的缓存。PNG 使用现有渲染器的本地原子替换；上传失败可能已经产生新本地 PNG。现有 WebDAV 实现是先 GET 比较再 PUT，**不是临时远端文件加 MOVE 的事务**：上传出错后远端是否变化由服务器/故障阶段决定，不能保证保留旧图。

## 直接运行渲染器

保留原有入口：

```bash
python dashboard.py --output output/dashboard.png --days 1
python dashboard.py --no-upload --output output/dashboard.png --days 1
```

直接入口也不是离线模式。它总会读 CalDAV；`--weather-file` 存在时尝试读取该 JSON，不存在时才请求天气。缓存解析/天气请求出错时显示天气暂缺，**不会自动刷新已存在的缓存，也不执行更新器的严格两日校验**。直接入口的退出码为 `0/1/2`，没有更新器的天气降级码 `3`；推荐定时任务使用更新器。

## 独立调度示例

这些只是供用户部署的模板，脚本不会创建或更改任何调度任务。替换所有 `/path/to/...`，选用一种调度方式，并避免多个任务同时操作同一缓存/图片。更新器没有内置互斥锁；原子替换只防半写文件，不防多进程交错或旧进程覆盖新进程。缓存写入不执行父目录 fsync，不承诺断电后的持久性；强制终止或清理权限故障可能留下临时文件。调度器负责防重入、超时、重试和日志轮转。

### cron（Linux，使用 flock 防重入）

示例中凭据来自仓库外、仅任务账号可读的 shell 环境文件。文件本身是受信任的 shell 输入，不要 source 不可信文件：

```cron
*/30 * * * * /usr/bin/flock -n /path/to/dashboard.lock /bin/sh -c 'cd /path/to/kindle-calendar-dashboard && set -a && . /path/to/dashboard-credentials.env && set +a && exec .venv/bin/python update_no_model.py' >> /path/to/dashboard-update.log 2>&1
```

环境文件提供所配置的密码变量，也可提供 `KINDLE_DASHBOARD_CONFIG` 等覆盖值。cron 不继承交互式终端中临时 export 的密码。`flock` 锁冲突也可能返回非零，它的退出码不是更新器退出码。

### systemd 用户定时器（可选替代 cron）

`kindle-dashboard.service` 示例：

```ini
[Unit]
Description=Refresh Kindle calendar dashboard

[Service]
Type=oneshot
WorkingDirectory=/path/to/kindle-calendar-dashboard
EnvironmentFile=/path/to/dashboard-credentials.env
ExecStart=/path/to/kindle-calendar-dashboard/.venv/bin/python update_no_model.py
TimeoutStartSec=180
```

`kindle-dashboard.timer` 示例：

```ini
[Unit]
Description=Refresh Kindle dashboard every half hour

[Timer]
OnCalendar=*-*-* *:00,30:00
Persistent=true

[Install]
WantedBy=timers.target
```

systemd 的环境文件应使用 `NAME=value` 赋值，不写 `export`，也不依赖 shell 命令或变量展开；它与上面的 shell source 语义不同。由用户将模板放到自己的 systemd 用户单元目录、审阅后启用，例如 `systemctl --user enable --now kindle-dashboard.timer`。同一 service 活跃期间不会再次启动，但独立手动/cron 进程仍需自行互斥。默认 systemd 将退出码 `3` 标记为失败，便于发现天气退化；如果只需记录日志而不告警，可在 `[Service]` 显式增加 `SuccessExitStatus=3`。无人登录时是否运行取决于用户服务/linger 的宿主机配置。

建议使用只具备目标日历读取和目标上传目录权限的独立账号、Nextcloud 应用密码，并保护配置和日志。客户端通过配置的 HTTPS PNG 地址读取生成结果，使用 FBInk 显示；服务端调度和客户端刷新是两套独立调度，不要把这里的 cron/systemd 模板安装到 Kindle。不要将私人日程分享链接写进公开仓库。

## 离线测试与语法检查

从仓库目录运行；使用公共示例配置而不是个人配置，并清除自己额外设置的 `KINDLE_*` 环境覆盖值。测试不需要应用密码、不执行私有脚本、不调用真实网络或上传，文件写入均在系统临时目录。新测试为天气 HTTP 和渲染/上传编排提供 mocks，并使用 socket 拦截防止意外联网；不会声称已经实测第三方服务。

```bash
PYTHONDONTWRITEBYTECODE=1 KINDLE_DASHBOARD_CONFIG=config.example.json python -m unittest -v
PYTHONDONTWRITEBYTECODE=1 python - <<'PY'
from pathlib import Path
for name in ('dashboard.py', 'test_dashboard.py', 'update_no_model.py', 'test_update_no_model.py'):
    compile(Path(name).read_bytes(), name, 'exec')
print('syntax checks passed; no bytecode written')
PY
git diff --check
```

覆盖正常请求、严格日期/数值/单位校验、超限响应、网络/HTTP/写入失败、旧缓存精确保留、临时文件清理、缺失或不可读缓存、午夜跨日、凭据与端点检查、路径冲突、上传禁用/未变化、阶段失败优先级及退出码。CI 的 `unittest` 自动发现新测试（Python 3.11/3.12/3.13），语法检查同时覆盖两个入口及两个测试文件。测试验证编排接口和数据解析，不验证真实 CalDAV/WebDAV/Open-Meteo 可用性、服务器 PUT 原子性、真实字体视觉效果、调度器或 Kindle 刷屏。

客户端隔离测试运行 `python -m unittest -v test_kindle test_kindle_dynamic test_kindle_locks`：手势、配置及打包用例需要 POSIX `sh`，动态协议用例用真实 curl 和标准库本地 HTTPS 服务（openssl 临时测试证书），真实锁并发用例另外需要 Linux `/proc` 和 `flock`；Linux 专用用例在 Windows 明确跳过。它们不运行完整设备脚本、不访问 USB或第三方服务；测试方法与验证边界见 [Kindle 设备端](kindle/README.md#本机开发检查)。

## 隐私与安全

公开仓库不应包含：

- `config.local.json`、`.env` 或应用密码
- 真实 CalDAV/WebDAV 地址、用户名、坐标或只读分享链接
- 生成的日程图片、天气缓存、运行日志和凭据环境文件
- 为个人日历执行的一次性迁移脚本

`.gitignore` 不保护手工打包整个目录，也不会清除 Git 历史中已有的敏感内容。发布应基于明确审阅的跟踪文件清单或干净 checkout；不要将 `.local/`、本地配置、输出等复制进发布包。发布前检查敏感信息及 Git 历史。最终审计、凭据轮换和发布由仓库所有者负责。

## 许可证

MIT，见 `LICENSE`。

## 安全与隐私

请遵守 [SECURITY.md](SECURITY.md)。提交前执行 `python3 scripts/check_privacy.py --staged`；CI 自动检查完整 Git 历史。规则只能辅助发现风险，发布前仍需人工审阅个人信息和凭据。

## 看板排版与客户端状态区

时间范围采用三行居中：简写时段（上/下）与开始时间、横线、简写时段与结束时间；小时不补前导零。日期和标题按可用空间调整，长标题优先两行。仅屏幕显示的第一项定时活动在进行中或两小时内开始（含边界）时黑底白字突出；其他活动不高亮。

静态生成器固定预留底部 **36 像素纯白区域**，在标准 `1072×1448` 图片上对应 `x=0..1071, y=1412..1447`。日程框与该区域之间留出间隔，静态生成器不生成或缓存设备状态。可选的外部动态 PNG 服务可在该源图底部绘制电量/充电信息，再返回完整 `1072×1448` 8 位灰度 PNG；客户端只负责 POST 与整图显示，不实现本地 overlay。静态原图、分享地址及生成器自动更新行为不变。
