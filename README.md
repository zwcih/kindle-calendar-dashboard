# Kindle Calendar Dashboard

将 CalDAV 日历和 Open-Meteo 天气预报渲染为适合 Kindle Paperwhite 3 的 `1072×1448` 三阶灰度 PNG。

## 特性

- 今天的未完成日程优先；今天结束后自动整屏切换到明天
- 今天仅剩 1–2 项时，利用空余区域补充明天的日程
- 短标题单行放大，长标题优先使用两行大字
- 紧凑的日期、天气、温度和降雨概率头部
- 支持全天事件、重复事件展开、取消事件和时区转换
- WebDAV 原子覆盖；远端内容未变化时跳过上传
- 配置、账号、端点、坐标和生成图片均与源码分离

## 数据流

```text
CalDAV calendar → dashboard.py → local grayscale PNG → optional WebDAV upload → Kindle
                         ↑
                  Open-Meteo forecast
```

## 安装

需要 Python 3.11+、Pillow 和支持中文的 Noto Sans CJK 字体。

```bash
git clone https://github.com/zwcih/kindle-calendar-dashboard.git
cd kindle-calendar-dashboard
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.local.json
```

编辑 `config.local.json`：

- `nextcloud.caldav_url`：CalDAV 日历集合 URL
- `nextcloud.webdav_url`：可选的 PNG 上传 URL
- `nextcloud.username`：Nextcloud 用户名
- `nextcloud.password_env`：保存应用密码的环境变量名
- `weather.latitude` / `weather.longitude`：天气位置
- `weather.timezone`：IANA 时区，例如 `Asia/Shanghai`
- `display`：目标屏幕尺寸
- `fonts`：本机中文字体路径

`config.local.json` 已被 `.gitignore` 排除，不会提交；字段规范见 `config.schema.json`。程序拒绝 HTTP 认证端点、明文凭据字段、非法坐标和非法密码变量名。密码不要写进 JSON；使用 Nextcloud 应用密码并通过环境变量提供：

```bash
export NEXTCLOUD_PASSWORD='your-app-password'
```

变量名应与 `nextcloud.password_env` 一致。也可以复制 `.env.example` 作为自己的凭据清单，但脚本不会自动读取 `.env`，避免意外泄漏。

## 运行

生成并上传：

```bash
python dashboard.py --output output/dashboard.png --days 1
```

跳过 WebDAV 上传（仍会通过 CalDAV 读取日历）：

```bash
python dashboard.py --no-upload --output output/dashboard.png --days 1
```

默认读取 `config.local.json`。也可指定其他配置文件：

```bash
KINDLE_DASHBOARD_CONFIG=/path/to/config.json python dashboard.py --no-upload
```

以下环境变量可以覆盖配置：

- `KINDLE_CALDAV_URL`
- `KINDLE_WEBDAV_URL`
- `KINDLE_NEXTCLOUD_USER`
- `KINDLE_WEATHER_LAT` / `KINDLE_WEATHER_LON`
- `KINDLE_TIMEZONE`

可通过 `--weather-file` 使用预先缓存的 Open-Meteo JSON。天气读取失败时，日历仍会使用现有缓存继续渲染。

## 自动更新

可使用任意任务调度器定期运行脚本。建议：

1. 使用只具备目标日历和目标上传目录权限的独立账号。
2. 使用应用密码，不使用主账号密码。
3. 将配置文件权限设为 `0600`。
4. 更新失败时保留远端上一张成功图片。

Kindle 设备端源码与部署说明见 [`kindle/`](kindle/README.md)。根目录的 `dashboard.py` 负责生成和上传图片；设备端负责下载、FBInk 显示、半小时调度及休眠恢复，两侧配置独立。不要将含私人日程的分享链接写进公开仓库。

## 测试

```bash
python -m unittest -v
python -m py_compile dashboard.py
```

## 隐私与安全

公开仓库不应包含：

- `config.local.json`、`.env` 或应用密码
- 真实 CalDAV/WebDAV 地址、用户名、坐标或只读分享链接
- 生成的日程图片和天气缓存
- 为个人日历执行的一次性迁移脚本

发布前建议再次运行敏感信息扫描，并检查 Git 历史，而不只是当前工作区。

## 许可证

MIT，见 `LICENSE`。
