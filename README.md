# majsoul-login

使用 [GitHub Action](https://github.com/features/actions) + [Selenium](https://selenium.dev) 实现雀魂每日自动登录，完成「良好市民」成就、拿到立直音乐。

浏览器 F12 抓包太麻烦，所以干脆用了 Selenium。

---

## 2026-09 修复说明（重要）

**症状**：`log.txt` 里从 **2026-05-25** 起每天都是 `Failed`，耗时也从约 50 秒骤降到约 30 秒；在此之前的 2025-02 ~ 2026-05-24 则一直 `Success`。脚本与 workflow 本身都没被人改过。

**根因**：雀魂 Web 客户端换引擎了。

| | 旧客户端（≤ 2026-05） | 新客户端（≥ 2026-05-25） |
|---|---|---|
| 渲染引擎 | LayaAir | **Unity WebGL** |
| 画布元素 | `<canvas id="layaCanvas">` | `<canvas id="unity-canvas">` |
| 登录输入框 | **真实 DOM**：`input[name=input]` / `input[name=input_password]` | **不存在**，整屏只有一块 canvas，UI 全部由 Unity 绘制 |
| 游戏域名 | `game.maj-soul.net` | 会自动 302 跳到 `game.maj-soul.com` |

旧代码第一步就是 `driver.find_element(By.ID, 'layaCanvas')`，新页面里根本没有这个元素，于是立刻抛 `NoSuchElementException` 被 `except` 吞掉 —— 表现就是「跑得比平时快、但静默失败」，这也是为什么日志里看不到任何原因。

**修复**：把「查 DOM」改成「看点像素」。

1. 用 `By.TAG_NAME, 'canvas'` 拿到画布；
2. 不再用固定 `sleep(20)` 赌界面加载好了 —— Unity WebGL 要联网下载资源包，实测 60~180 秒不等，固定 sleep 必然踩空。改成**轮询截图中「進入遊戲」金色按钮的像素占比**，占比 ≥ 30% 才认为登录界面真的渲染完了（实测加载页 0.0%、登录页 70.5%，区分度很好）；
3. Unity 收键盘事件而不是 DOM 输入框，所以用 `ActionChains.send_keys()` 把账号密码打进去；
4. 点击位置改成**相对画布尺寸的比例**，不再写死像素，窗口大小变了也不会偏；
5. 输入完比对输入框区域的像素变化，确认「点击没落空 + 键盘真的被 Unity 收到了」，再点登录；
6. 登录后同样用像素判断「金色按钮是否消失」来确认已经离开登录页，失败会重试。

> 顺带修掉了原来的另一个坑：旧脚本把异常 `except` 掉后只往 `log.txt` 写一个 `Failed`，不写原因，所以故障能静默持续 4 个月。现在失败原因会直接写进 `log.txt`，失败截图也会作为 artifact 上传。

---

## 使用方法

1. Fork 本仓库
2. 点击 `Settings` → `Secrets and variables` → `Actions` → `New repository secret`，依次配置 `EMAIL`、`PASSWD`
   - `EMAIL` 是雀魂的邮箱账号，如有多个账户用空格分隔，例如 `example@gmail.com example@qq.com`
   - `PASSWD` 是雀魂的密码，如有多个账户用空格分隔，与邮箱依次对应，例如 `grc28r7g3 pdtaw3fwag`
3. 先在 `Actions` 页面启用 Workflow，再选中 `Majsoul-Login`，把 scheduled workflow 启用
4. Enjoy it!

已在 workflow 里配置每日自动 push 运行结果，避免 60 天无活动导致 Actions 自动关闭。

## 运行时间

| cron (UTC) | 北京时间 |
|---|---|
| `0 21 * * *` | 05:00 |
| `0 9 * * *` | 17:00 |

每天跑两次：游戏维护或偶发网络抖动时，还有第二次机会拿到当天签到。GitHub 的 scheduled workflow 本身可能延迟几分钟到几十分钟，属正常现象。

## 可调参数（环境变量）

一般不用动，需要时在 workflow 的 `Majsoul login` 步骤里加 `env:` 即可。

| 变量 | 默认 | 说明 |
|---|---|---|
| `MAJ_URL` | `https://game.maj-soul.net/1/` | 游戏地址 |
| `MAJ_FORM_TIMEOUT` | `600` | 等待登录界面出现的最长秒数 |
| `MAJ_LOGIN_TIMEOUT` | `150` | 点登录后等待离开登录页的最长秒数 |
| `MAJ_ATTEMPTS` | `3` | 提交登录的最大重试次数 |
| `MAJ_LOBBY_SETTLE` | `45` | 登录成功后停留秒数，等大厅加载完 |
| `MAJ_CLAIM_CLICKS` | `3` | 登录后在画面中央点几下收掉每日奖励弹窗，设 `0` 关闭 |
| `MAJ_DEBUG` | `1` | 设为 `0` 关闭逐步骤调试截图 |

## 排错

失败时：

1. 先看 `log.txt` 最后一行，格式为
   `时间-Success/Failed-耗时s[-失败原因]`
   例如 `2026-09-12 21:20:55-Failed-486.10s-等待登录界面超时（480s）`
2. 再到对应 Actions run 页面下载 **`debug-screenshots`** artifact，里面是每一步的截图，能直接看出卡在哪一屏。

常见原因：

- **等待登录界面超时** —— 游戏官方在更新/维护，或 runner 网络太慢。等维护结束或手动 `Run workflow` 重试。
- **账号/密码未能填入输入框** —— 官方改了登录界面的布局，需要重新测量点击比例（见下）。
- **连续 3 次提交后仍停留在登录界面** —— 账号密码错误，或触发了验证码。

### 如果官方又改了界面

所有坐标都是 `login.py` 顶部的这几个「相对 canvas 尺寸的比例」常量，重新量一次即可：

```python
EMAIL_RATIO  = (0.24182, -0.21206)
PASSWD_RATIO = (0.24182, -0.10426)
LOGIN_RATIO  = (0.24102, 0.08865)
```

量法：把 `MAJ_DEBUG` 设为 `1` 跑一次，取 `debug/wait_form.png`，量出「電郵/帳號登錄」「密碼」「進入遊戲」三个控件中心相对画布中心的偏移，再除以画布宽高即可。
