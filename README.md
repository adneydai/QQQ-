# QQQ 回撤微信提醒

这个项目每天拉取 QQQ 日线收盘价，计算两个回撤维度，并通过 PushPlus 推送到微信：

- 60 日波段回撤：最新收盘价相对最近 60 个交易日最高收盘价。
- 当年高点回撤：最新收盘价相对当年自然年内最高收盘价。

两个维度都会独立判断是否触达 `10% / 20% / 30%` 买入提醒档位。提醒只做信息提示，不自动交易，也不构成投资建议。

## 推送效果

标题示例：

```text
QQQ 回撤提醒：60日 -12.1% / 年内 -23.0%
```

正文示例：

```markdown
## QQQ 回撤温度计

数据日：2026-05-27
最新收盘价：$470.00

### 60 日波段回撤
60 日最高收盘价：$535.00
当前回撤：-12.1%

提示：触达 10% 买入提醒档位

### 当年高点回撤
当年最高收盘价：$610.00
当前回撤：-23.0%

提示：触达 20% 买入提醒档位

---
口径：均按收盘价计算；仅作提醒，不自动交易。
```

## GitHub Secrets

在仓库的 `Settings -> Secrets and variables -> Actions -> New repository secret` 添加：

- `ALPHA_VANTAGE_API_KEY`：Alpha Vantage 免费 API Key。
- `PUSHPLUS_TOKEN`：PushPlus 用户 token 或消息 token。

脚本不会把密钥写入日志。

## 定时运行

`.github/workflows/daily-alert.yml` 会在 `23:10 UTC` 运行，对应北京时间约早上 `07:10`。因为这是美股收盘后运行，北京时间周二到周六通常对应前一个美股交易日的数据。

如果 Alpha Vantage 还没有更新到当前纽约日期，脚本会跳过推送，避免重复发送旧数据。

## 本地测试

运行单元测试：

```bash
python3 -m unittest -v
```

手动运行真实推送：

```bash
export ALPHA_VANTAGE_API_KEY="你的 Alpha Vantage key"
export PUSHPLUS_TOKEN="你的 PushPlus token"
python3 -m src.qqq_drawdown_alert
```

GitHub Actions 里可以用 `workflow_dispatch` 手动触发一次。
