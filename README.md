# ma/macd/价格异动监控-ma/macd/ma+macd自动交易
## 三种监控模式/三种交易模式
## api储存至用户私人文件，其余人员无访问权限，安全可控
## 智能识别机器人订单和手动订单
## 止损止盈手动管理-信号反转先平后开，单向持仓，盈亏可控
## 集中api调用，对齐k线，减少频繁调用

**克隆仓库**
```bash
git clone https://github.com/rennengfeng/Binance_Trade_BOT.git
cd Binance_Trade_BOT
```

**安装依赖**
```bash
pip3 install -r requirements.txt  #或者 pip install -r requirements.txt
```

**安装时间同步**
```bash
sudo apt install ntpdate
sudo ntpdate pool.ntp.org
```

**配置文件`config.py`修改**

TOKEN = "Telegram Bot Token"

CHAT_ID = "chat_id1,chat_id2"   #多用户","隔开

Binance API 密钥 (可选，用户可在TG中设置自己的密钥)
这些密钥仅作为示例格式，用户可在TG中覆盖
BINANCE_API_KEY = ""  # 可选
BINANCE_API_SECRET = ""  # 可选

**启动**
```bash
python3 binance_bot.py #或者 python binance_bot.py
```
在你的TG_BOT中发送/start,根据提示进行设置。
