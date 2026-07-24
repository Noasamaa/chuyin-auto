# 初音的青葱 · 自动登录 / 签到 / 守护灵寻宝

目标站：`https://www.yngal.com`（域名可在配置里改）

## 做什么

| 步骤 | 接口 | 说明 |
|------|------|------|
| 登录 | `POST /sign` | `email` + `md5(password)`，取 `token` |
| 每日访问 | `GET /addJf` | Header `X-Auth-Token`，首次访问领硬币 |
| 寻宝 | `GET /hunt` | 守护灵自动寻宝，失败码 `602/604/688` 停止 |
| 查信息 | `GET /getVip` | 昵称 / 等级 / 硬币 / 积分（辅助，失败不阻断） |

## 本地跑

```bash
cd 服务器小玩具/chuyin-auto
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml
chmod 600 config.yaml
# 编辑 config.yaml 填 email / password

python main.py            # 正式跑
python main.py --dry-run  # 只登录+查信息
python -m unittest discover -s tests -v
```

## 服务器部署（systemd 定时）

**必须以 root 安装。** 代码装到专属目录后属主为 `root`，定时任务以低权限用户 `chuyin-auto` 运行；普通用户不能改将要执行的脚本。

```bash
# 1) 本机准备源码（不要用可被他人写的目录当最终安装路径）
# 2) 上传到服务器临时目录
scp -r 服务器小玩具/chuyin-auto root@your-server:/tmp/chuyin-auto

# 3) 在服务器上 root 安装
ssh root@your-server 'bash /tmp/chuyin-auto/deploy/install.sh'
# 已是 root 时不要加 sudo（部分精简系统没有 sudo）
```

默认：

| 项 | 值 |
|----|-----|
| 安装目录 | `/opt/chuyin-auto`（仅允许 `/opt/chuyin-auto*` 或 `/srv/chuyin-auto*`） |
| 运行用户 | `chuyin-auto`（nologin 系统用户） |
| 定时 | 每天 09:15 |
| 单次时限 | systemd `RuntimeMaxSec=600` + 进程内 `job_max_seconds≤540` |
| 配置权限 | `640 root:chuyin-auto` |
| 日志目录 | `700`，日志文件 `600` + 轮转 |

自定义安装目录：

```bash
APP_DIR=/srv/chuyin-auto bash deploy/install.sh
# unit 会按 APP_DIR 渲染，不再写死 /opt
```

安全约束：

- `APP_DIR` / marker **禁止符号链接**；marker 必须是 **root 所有的普通文件**
- 非空目标无合法 marker 时拒绝 `rsync --delete`
- 同步 `--no-owner --no-group`，代码 `chown root`；**.venv / logs / config 不参与盲 chmod 644**
- 改代码请 **root 重新执行 install.sh**

常用命令：

```bash
systemctl start chuyin-auto.service
systemctl list-timers | grep chuyin
journalctl -u chuyin-auto.service -n 100
tail -f /opt/chuyin-auto/logs/run-$(date +%F).log
nano /opt/chuyin-auto/config.yaml
```

### 纯 cron（不用 systemd）

进程内仍有 `job_max_seconds` 总时限（默认 540s），慢滴流不会无限占进程：

```cron
15 9 * * * /opt/chuyin-auto/.venv/bin/python /opt/chuyin-auto/main.py
```

## 配置要点

- `domain`：非空主机名，不要带协议
- `accounts`：**list**，最多 20；元素 mapping + `email`/`password`
- `hunt_max`：1~50；`timeout`：1~60；`hunt_interval`：0~30
- `job_max_seconds`：30~540；与 `hunt_max×interval×账号` 估算冲突会直接 ConfigError
- `do_checkin` / `do_hunt` / `enabled`：**只能 YAML bool**（`"false"` 字符串会报错）
- 数值字段拒绝 bool（`hunt_max: true` 非法）

`config.yaml` 含密码，保持 `600`/`640`，不要提交 git。

## 成功 / 失败语义

| 情况 | 退出 / `ok` |
|------|-------------|
| 登录成功 + 签到 `0/10` + 寻宝 `0/200` 或停止码 `602/604/688` | 成功 |
| HTTP 非 2xx、响应过大、非 JSON、**任意重定向** | 失败 |
| 签到其它 code / 传输失败 | 失败（**仍会继续寻宝**） |
| 寻宝未知 code（即使带 `obj` items，如 999） | 失败 |
| `/hunt`、`/addJf` 超时 | **不自动重试**（防双倍扣次） |
| 配置错误 / 无启用账号 | 退出码 2 |
| `/getVip` 失败 | 不单独导致失败 |

## 目录

```
chuyin-auto/
  main.py
  config.example.yaml
  requirements.txt
  tests/
  deploy/
    install.sh
    chuyin-auto.service.in   # 安装时渲染
    chuyin-auto.timer
  logs/                      # 运行后生成
  config.yaml                # 本地自建，勿提交
```
