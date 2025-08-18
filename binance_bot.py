#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import json
import asyncio
import aiohttp
import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time
from telegram import (
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Update
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
    Application
)
from config import TOKEN, CHAT_IDS, BINANCE_API_KEY, BINANCE_API_SECRET, DEFAULT_INTERVAL
import hmac
import hashlib
from decimal import Decimal
import uuid

# 设置日志
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# 配置文件路径
USER_DATA_DIR = "user_data"
os.makedirs(USER_DATA_DIR, exist_ok=True)

# K线参数配置
INTERVALS = {
    "5m": "5分钟",
    "15m": "15分钟",
    "60m": "60分钟",
    "240m": "4小时"
}

# Binance API实际支持的间隔
BINANCE_INTERVALS = {
    "5m": "5m",
    "15m": "15m",
    "60m": "1h",  # Binance使用1h表示60分钟
    "240m": "4h"  # Binance使用4h表示4小时
}

# 市场类型中文名
MARKET_TYPE_NAMES = {
    "spot": "现货",
    "contract": "合约",
    "futures": "合约"
}

# 监控类型中文名
MONITOR_TYPE_NAMES = {
    "price": "价格异动",
    "macd": "MACD交叉",
    "ma": "MA交叉"
}

# 自动交易模式
TRADE_MODES = {
    "ma": "MA交叉交易",
    "macd": "MACD交叉交易",
    "mamacd": "MA+MACD联合交易"
}

# 持仓方向
POSITION_SIDE = {
    "LONG": "多头",
    "SHORT": "空头"
}

# API错误代码
API_ERROR_CODES = {
    -1021: "时间戳过期",
    -1022: "无效的API签名",
    -2015: "无效的API密钥",
    -2014: "API密钥格式无效",
    -1102: "强制参数为空或格式错误",
    -1013: "无效金额",
    -2010: "新订单被拒绝",
    -2011: "订单取消被拒绝"
}

# 主菜单
main_menu = [
    ["1️⃣ 添加监控币种", "2️⃣ 删除监控币种"],
    ["3️⃣ 开启监控", "4️⃣ 关闭监控"],
    ["5️⃣ 自动交易设置", "6️⃣ 启动/关闭自动交易"],
    ["7️⃣ 查看状态", "8️⃣ 帮助"]
]
reply_markup = ReplyKeyboardMarkup(main_menu, resize_keyboard=True)
 
# 返回菜单
back_menu = [["↩️ 返回主菜单", "❌ 取消"]]
back_markup = ReplyKeyboardMarkup(back_menu, resize_keyboard=True)

# 服务器时间偏移量（用于与Binance API时间同步）
time_offset = 0
last_sync_time = 0  # 上次同步时间的时间戳

# --- Binance API 工具函数 ---
def generate_signature(api_secret, data):
    """生成签名"""
    return hmac.new(api_secret.encode('utf-8'), data.encode('utf-8'), hashlib.sha256).hexdigest()

async def binance_api_request(method, endpoint, params, api_key, api_secret, is_futures=False, retry_on_time_error=True):
    """发送Binance API请求，增加时间错误重试机制"""
    global time_offset, last_sync_time
    
    base_url = "https://api.binance.com"
    if is_futures:
        base_url = "https://fapi.binance.com"
    
    # 添加时间戳参数
    timestamp = int(time.time() * 1000) + time_offset
    params["timestamp"] = timestamp
    
    # 创建查询字符串
    query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
    signature = generate_signature(api_secret, query_string)
    url = f"{base_url}{endpoint}?{query_string}&signature={signature}"
    
    headers = {"X-MBX-APIKEY": api_key}
    
    async with aiohttp.ClientSession() as session:
        try:
            if method == "GET":
                async with session.get(url, headers=headers) as resp:
                    response = await handle_response(resp)
            elif method == "POST":
                async with session.post(url, headers=headers, data=params) as resp:
                    response = await handle_response(resp)
            elif method == "DELETE":
                async with session.delete(url, headers=headers) as resp:
                    response = await handle_response(resp)
            
            # 检查是否需要重试（时间错误）
            if retry_on_time_error and response and "error" in response and response.get("code") in [-1021, -1022]:
                logger.warning(f"时间同步错误，重新同步时间并重试: {response.get('msg')}")
                await sync_binance_time()
                return await binance_api_request(method, endpoint, params, api_key, api_secret, is_futures, False)
            
            return response
        except Exception as e:
            logger.error(f"API请求出错: {e}")
            return None

async def handle_response(resp):
    """处理API响应"""
    if resp.status == 200:
        return await resp.json()
    else:
        error_text = await resp.text()
        try:
            error_data = json.loads(error_text)
            error_code = error_data.get('code', '未知错误')
            error_msg = error_data.get('msg', '无错误信息')
            
            # 获取友好的错误消息
            friendly_msg = API_ERROR_CODES.get(error_code, error_msg)
            logger.error(f"API错误: {resp.status} - 错误代码: {error_code}, 消息: {friendly_msg}")
            
            # 返回错误代码和消息
            return {
                "error": True,
                "code": error_code,
                "msg": friendly_msg
            }
        except json.JSONDecodeError:
            logger.error(f"API错误: {resp.status} - {error_text}")
            return {
                "error": True,
                "code": resp.status,
                "msg": error_text
            }

# --- 数据管理 ---
def get_user_file(user_id):
    return os.path.join(USER_DATA_DIR, f"{user_id}.json")

def load_user_data(user_id):
    file_path = get_user_file(user_id)
    if os.path.exists(file_path):
        try:
            with open(file_path, "r") as f:
                data = json.load(f)
                # 确保数据结构兼容
                if "monitors" not in data:
                    data["monitors"] = {
                        "price": {"enabled": False},
                        "macd": {"enabled": False},
                        "ma": {"enabled": False}
                    }
                if "active" not in data:
                    data["active"] = False
                    
                # 自动交易数据结构
                if "auto_trading" not in data:
                    data["auto_trading"] = {
                        "enabled": False,
                        "mode": None,  # ma, macd, mamacd
                        "symbols": []  # 自动交易对列表
                    }
                # 确保symbols字段存在
                if "symbols" not in data["auto_trading"]:
                    data["auto_trading"]["symbols"] = []
                    
                # 用户API密钥
                if "binance_api" not in data:
                    data["binance_api"] = {
                        "key": "",
                        "secret": ""
                    }
                    
                # 系统持仓记录
                if "system_positions" not in data:
                    data["system_positions"] = {}
                    
                # 迁移旧数据结构 - 确保每个监控项都是独立字典
                new_symbols = []
                for symbol in data.get("symbols", []):
                    if isinstance(symbol, dict):
                        # 如果是字典格式，直接使用
                        if symbol.get("type") == "futures":
                            symbol["type"] = "contract"
                        new_symbols.append(symbol)
                    else:
                        # 如果是旧格式（字符串），转换为字典
                        new_symbols.append({
                            "symbol": symbol,
                            "type": "spot",
                            "monitor": "price",
                            "interval": "15m",
                            "threshold": 5.0
                        })
                data["symbols"] = new_symbols
                return data
        except Exception as e:
            logger.error(f"加载用户数据失败: {e}")
            # 创建新的数据结构
            return {
                "symbols": [],
                "monitors": {
                    "price": {"enabled": False},
                    "macd": {"enabled": False},
                    "ma": {"enabled": False}
                },
                "active": False,
                "auto_trading": {
                    "enabled": False,
                    "mode": None,
                    "symbols": []
                },
                "binance_api": {
                    "key": "",
                    "secret": ""
                },
                "system_positions": {}
            }
    # 默认配置
    return {
        "symbols": [],
        "monitors": {
            "price": {"enabled": False},
            "macd": {"enabled": False},
            "ma": {"enabled": False}
        },
        "active": False,
        "auto_trading": {
            "enabled": False,
            "mode": None,
            "symbols": []
        },
        "binance_api": {
            "key": "",
            "secret": ""
        },
        "system_positions": {}
    }

def save_user_data(user_id, data):
    file_path = get_user_file(user_id)
    try:
        with open(file_path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"保存用户数据失败: {e}")

# --- 时间同步函数 ---
async def sync_binance_time():
    """同步Binance服务器时间"""
    global time_offset, last_sync_time
    url = "https://api.binance.com/api/v3/time"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    server_time = data['serverTime']
                    local_time = int(time.time() * 1000)
                    time_offset = server_time - local_time
                    last_sync_time = time.time()
                    logger.info(f"时间同步完成，服务器时间偏移: {time_offset}ms")
                else:
                    error_text = await resp.text()
                    logger.error(f"时间同步失败: {resp.status} - {error_text}")
        except Exception as e:
            logger.error(f"时间同步出错: {e}")

# --- Binance API 辅助函数 ---
async def get_klines(symbol, interval, market_type="spot", limit=100):
    """获取K线数据"""
    # 确保symbol是大写的
    symbol = symbol.upper()
    
    # 检查合约市场是否需要USDT后缀
    if market_type in ["contract", "futures"] and not symbol.endswith("USDT"):
        symbol += "USDT"
    
    # 使用正确的API端点
    base_url = "https://api.binance.com/api/v3"
    if market_type in ["contract", "futures"]:
        base_url = "https://fapi.binance.com/fapi/v1"
    
    # 将用户间隔转换为Binance API间隔
    binance_interval = BINANCE_INTERVALS.get(interval, interval)
    
    url = f"{base_url}/klines?symbol={symbol}&interval={binance_interval}&limit={limit}"
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    error_text = await resp.text()
                    logger.error(f"获取K线失败: {symbol} {binance_interval} {resp.status} - {error_text}")
                    return None
        except Exception as e:
            logger.error(f"获取K线数据失败: {e}")
            return None

def klines_to_dataframe(klines):
    """将K线数据转换为DataFrame"""
    if not klines:
        return None
        
    df = pd.DataFrame(klines, columns=[
        'timestamp', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_asset_volume', 'number_of_trades',
        'taker_buy_base', 'taker_buy_quote', 'ignore'
    ])
    
    # 转换数据类型
    numeric_cols = ['open', 'high', 'low', 'close', 'volume']
    df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, axis=1)
    
    # 转换时间戳（应用时间偏移）
    df['timestamp'] = pd.to_datetime(df['timestamp'] + time_offset, unit='ms')
    df.set_index('timestamp', inplace=True)
    
    return df

# --- 技术指标计算 ---
def calculate_ma(df, window=20):
    """计算移动平均线"""
    return df['close'].rolling(window=window).mean()

def calculate_ema(df, window):
    """计算指数移动平均线"""
    return df['close'].ewm(span=window, adjust=False).mean()

def calculate_macd(df, fast=12, slow=26, signal=9):
    """计算MACD指标"""
    ema_fast = calculate_ema(df, fast)
    ema_slow = calculate_ema(df, slow)
    macd = ema_fast - ema_slow
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    histogram = macd - signal_line
    return macd, signal_line, histogram

# --- API请求合并管理器 ---
class APIManager:
    """API请求合并管理器"""
    def __init__(self):
        self.pending_requests = {}
        self.data_cache = {}
        self.lock = asyncio.Lock()
        self.last_request_time = {}
    
    async def get_kline(self, symbol, interval, market_type="spot", limit=100):
        """获取K线数据（带合并功能）"""
        cache_key = f"{symbol}_{interval}_{market_type}_{limit}"
        
        # 检查缓存是否有效（5秒内有效）
        current_time = time.time()
        if cache_key in self.data_cache:
            data, timestamp = self.data_cache[cache_key]
            if current_time - timestamp < 5:  # 5秒内缓存有效
                return data
        
        async with self.lock:
            # 检查是否已有相同请求在处理中
            if cache_key in self.pending_requests:
                # 创建新的future等待结果
                future = asyncio.Future()
                self.pending_requests[cache_key].append(future)
                # 等待已有请求完成
                return await future
            
            # 创建新的请求组
            self.pending_requests[cache_key] = []
            
            try:
                # 执行实际API调用
                kline_data = await get_klines(symbol, interval, market_type, limit)
                
                # 更新缓存
                self.data_cache[cache_key] = (kline_data, current_time)
                
                # 通知所有等待此结果的请求
                for future in self.pending_requests[cache_key]:
                    if not future.done():
                        future.set_result(kline_data)
                
                return kline_data
            except Exception as e:
                # 处理错误
                for future in self.pending_requests[cache_key]:
                    if not future.done():
                        future.set_exception(e)
                raise e
            finally:
                # 清理请求组
                del self.pending_requests[cache_key]

# 全局API管理器实例
api_manager = APIManager()

# --- 自动交易管理 ---
class TradeManager:
    def __init__(self):
        self.positions = {}
    
    async def place_market_order(self, user_id, symbol, side, amount, leverage=None):
        """下市价单"""
        try:
            user_data = load_user_data(user_id)
            api_key = user_data['binance_api']['key']
            api_secret = user_data['binance_api']['secret']
            
            symbol = symbol.upper()
            
            # 设置杠杆
            if leverage:
                leverage_resp = await self.change_leverage(user_id, symbol, leverage)
                if leverage_resp and "error" in leverage_resp:
                    return leverage_resp
            
            # 获取当前价格计算数量
            ticker = await self.get_ticker_price(user_id, symbol, is_futures=True)
            if not ticker or 'price' not in ticker:
                logger.error("无法获取当前价格")
                return {"error": True, "msg": "无法获取当前价格"}
                
            current_price = float(ticker['price'])
            quantity = amount / current_price
            quantity = round(quantity, 3)  # 保留3位小数
            
            params = {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": quantity,
            }
            
            # 交易前同步时间
            await sync_binance_time()
            
            response = await binance_api_request("POST", "/fapi/v1/order", params, api_key, api_secret, is_futures=True)
            
            # 记录系统持仓
            if response and "orderId" in response:
                user_data = load_user_data(user_id)
                system_positions = user_data.get("system_positions", {})
                
                if symbol not in system_positions:
                    system_positions[symbol] = []
                    
                system_positions[symbol].append({
                    "order_id": response["orderId"],
                    "symbol": symbol,
                    "side": side,
                    "quantity": quantity,
                    "entry_price": current_price,
                    "leverage": leverage,
                    "amount": amount,
                    "timestamp": datetime.now().isoformat()
                })
                user_data["system_positions"] = system_positions
                save_user_data(user_id, user_data)
            
            return response
        except Exception as e:
            logger.error(f"下单失败: {e}")
            return {"error": True, "msg": str(e)}
    
    async def change_leverage(self, user_id, symbol, leverage):
        """更改杠杆"""
        try:
            user_data = load_user_data(user_id)
            api_key = user_data['binance_api']['key']
            api_secret = user_data['binance_api']['secret']
            
            params = {
                "symbol": symbol,
                "leverage": leverage,
            }
            
            # 交易前同步时间
            await sync_binance_time()
            
            response = await binance_api_request("POST", "/fapi/v1/leverage", params, api_key, api_secret, is_futures=True)
            return response
        except Exception as e:
            logger.error(f"设置杠杆失败: {e}")
            return {"error": True, "msg": str(e)}
    
    async def close_position(self, user_id, symbol):
        """平仓"""
        try:
            user_data = load_user_data(user_id)
            api_key = user_data['binance_api']['key']
            api_secret = user_data['binance_api']['secret']
            
            # 更新持仓
            await self.update_positions(user_id)
            
            if symbol not in self.positions:
                return {"error": True, "msg": "没有持仓可平"}
                
            position = self.positions[symbol]
            side = "SELL" if position['side'] == "LONG" else "BUY"
            
            params = {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": position['quantity'],
                "reduceOnly": "true",
            }
            
            # 交易前同步时间
            await sync_binance_time()
            
            response = await binance_api_request("POST", "/fapi/v1/order", params, api_key, api_secret, is_futures=True)
            if response and "error" in response:
                return response
            elif response:
                # 移除系统持仓记录
                if symbol in self.positions:
                    del self.positions[symbol]
                    
                # 更新系统持仓记录
                user_data = load_user_data(user_id)
                system_positions = user_data.get("system_positions", {})
                if symbol in system_positions:
                    del system_positions[symbol]
                    user_data["system_positions"] = system_positions
                    save_user_data(user_id, user_data)
                    
                return True
            return {"error": True, "msg": "平仓失败"}
        except Exception as e:
            logger.error(f"平仓失败: {e}")
            return {"error": True, "msg": str(e)}
    
    async def update_positions(self, user_id):
        """更新持仓信息"""
        try:
            user_data = load_user_data(user_id)
            api_key = user_data['binance_api']['key']
            api_secret = user_data['binance_api']['secret']
            
            params = {}
            response = await binance_api_request("GET", "/fapi/v2/positionRisk", params, api_key, api_secret, is_futures=True)
            if response and "error" in response:
                return False
                
            self.positions = {}
            for pos in response:
                position_amt = float(pos['positionAmt'])
                if position_amt != 0:
                    # 在单向持仓模式下，positionSide可能不存在，我们通过持仓正负判断方向
                    if 'positionSide' in pos:
                        side = pos['positionSide']
                        if side == 'BOTH':
                            # 如果为BOTH，则用持仓正负判断
                            side = "LONG" if position_amt > 0 else "SHORT"
                    else:
                        side = "LONG" if position_amt > 0 else "SHORT"
                    
                    # 注意：未实现盈亏字段为'unRealizedProfit'
                    unrealized_profit = float(pos['unRealizedProfit'])
                    
                    self.positions[pos['symbol']] = {
                        'side': side,
                        'leverage': int(pos['leverage']),
                        'quantity': abs(position_amt),
                        'entry_price': float(pos['entryPrice']),
                        'mark_price': float(pos['markPrice']),
                        'unrealized_profit': unrealized_profit
                    }
            return True
        except Exception as e:
            logger.error(f"更新持仓失败: {e}")
            return False
    
    async def get_ticker_price(self, user_id, symbol, is_futures=False):
        """获取当前价格"""
        try:
            user_data = load_user_data(user_id)
            api_key = user_data['binance_api']['key']
            api_secret = user_data['binance_api']['secret']
            
            endpoint = "/api/v3/ticker/price" if not is_futures else "/fapi/v1/ticker/price"
            params = {"symbol": symbol.upper()}
            return await binance_api_request("GET", endpoint, params, api_key, api_secret, is_futures)
        except Exception as e:
            logger.error(f"获取价格失败: {e}")
            return None

# 全局交易管理器
trade_manager = TradeManager()

# 自动交易状态管理
class AutoTradeTask:
    def __init__(self, app):
        self.app = app
        self.active = False
    
    async def execute_trade(self, user_id, symbol, market_type, signal_type, config, api_key, api_secret):
        """执行交易逻辑"""
        if market_type not in ["contract", "futures"]:
            logger.warning("自动交易仅支持合约市场")
            return
            
        # 获取当前持仓方向
        await trade_manager.update_positions(user_id)
        current_position = trade_manager.positions.get(symbol, None)
        current_side = current_position['side'] if current_position else None
        
        # 确定交易方向
        new_side = "LONG" if signal_type == "golden" else "SHORT"
        
        # 平仓逻辑
        if current_position:
            # 信号反转需要先平仓
            if (current_side == "LONG" and new_side == "SHORT") or \
               (current_side == "SHORT" and new_side == "LONG"):
                close_resp = await trade_manager.close_position(user_id, symbol)
                if close_resp and "error" in close_resp:
                    error_msg = close_resp.get('msg', '未知错误')
                    message = (
                        f"❌ 自动交易平仓失败: {symbol}\n"
                        f"• 原因: {error_msg}\n"
                        f"• 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                    await self.app.bot.send_message(chat_id=user_id, text=message)
                    return
        
        # 开新仓
        response = await trade_manager.place_market_order(
            user_id=user_id,
            symbol=symbol,
            side="BUY" if new_side == "LONG" else "SELL",
            amount=config['amount'],
            leverage=config['leverage']
        )
        
        if response and "error" in response:
            error_msg = response.get('msg', '未知错误')
            message = (
                f"❌ 自动交易执行失败: {symbol}\n"
                f"• 原因: {error_msg}\n"
                f"• 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            await self.app.bot.send_message(chat_id=user_id, text=message)
            return
        elif response:
            # 更新本地持仓记录
            await trade_manager.update_positions(user_id)
            
            # 发送交易通知
            direction = "做多" if new_side == "LONG" else "做空"
            message = (
                f"✅ 自动交易执行: {symbol}\n"
                f"• 方向: {direction}\n"
                f"• 金额: ${config['amount']}\n"
                f"• 杠杆: {config['leverage']}x\n"
                f"• 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            await self.app.bot.send_message(chat_id=user_id, text=message)
            
            # 设置止盈止损
            await self.set_tp_sl(user_id, symbol, config, new_side, api_key, api_secret)

    async def set_tp_sl(self, user_id, symbol, config, side, api_key, api_secret):
        """设置止盈止损"""
        try:
            # 获取当前价格
            ticker = await trade_manager.get_ticker_price(user_id, symbol, is_futures=True)
            if not ticker or 'price' not in ticker:
                logger.error("无法获取当前价格，无法设置止盈止损")
                return
                
            current_price = float(ticker['price'])
            
            # 计算止盈止损价格
            if side == "LONG":
                if config['tp'] > 0:
                    tp_price = current_price * (1 + config['tp']/100)
                if config['sl'] > 0:
                    sl_price = current_price * (1 - config['sl']/100)
            else:
                if config['tp'] > 0:
                    tp_price = current_price * (1 - config['tp']/100)
                if config['sl'] > 0:
                    sl_price = current_price * (1 + config['sl']/100)
            
            # 创建止盈止损订单
            if config['tp'] > 0:
                tp_params = {
                    "symbol": symbol,
                    "side": "SELL" if side == "LONG" else "BUY",
                    "type": "TAKE_PROFIT_MARKET",
                    "stopPrice": round(tp_price, 4),
                    "closePosition": "true",
                }
                
                # 交易前同步时间
                await sync_binance_time()
                
                tp_resp = await binance_api_request("POST", "/fapi/v1/order", tp_params, api_key, api_secret, is_futures=True)
                if tp_resp and "error" in tp_resp:
                    error_msg = tp_resp.get('msg', '未知错误')
                    message = f"❌ 设置止盈失败: {symbol}\n原因: {error_msg}"
                    await self.app.bot.send_message(chat_id=user_id, text=message)
            
            if config['sl'] > 0:
                sl_params = {
                    "symbol": symbol,
                    "side": "SELL" if side == "LONG" else "BUY",
                    "type": "STOP_MARKET",
                    "stopPrice": round(sl_price, 4),
                    "closePosition": "true",
                }
                
                # 交易前同步时间
                await sync_binance_time()
                
                sl_resp = await binance_api_request("POST", "/fapi/v1/order", sl_params, api_key, api_secret, is_futures=True)
                if sl_resp and "error" in sl_resp:
                    error_msg = sl_resp.get('msg', '未知错误')
                    message = f"❌ 设置止损失败: {symbol}\n原因: {error_msg}"
                    await self.app.bot.send_message(chat_id=user_id, text=message)
            
        except Exception as e:
            logger.error(f"设置止盈止损失败: {e}")
            # 记录详细错误信息
            logger.error(f"止盈止损设置参数: symbol={symbol}, side={side}, 止盈={config['tp']}%, 止损={config['sl']}%")
            logger.error(f"当前价格: {current_price if 'current_price' in locals() else 'N/A'}")

# 全局自动交易任务
auto_trade_task = None

# --- 监控任务 ---
class MonitorTask:
    def __init__(self, app):
        self.app = app
        self.price_history = {}
        self.macd_cross_state = {}
        self.ma_cross_state = {}
        self.active = True
        self.task = None
        self.last_detection = {}  # 记录每个检测的上次检测时间
        self.high_frequency_mode = False  # 高频检测模式标志
        self.next_check_time = 0  # 下次检查时间

    async def run(self):
        """监控主循环 - 合并API版本"""
        logger.info("监控任务已启动")
        await sync_binance_time()
        
        while self.active:
            try:
                current_time = time.time()
                
                # 高频检测模式（每5秒检查一次）
                if self.high_frequency_mode:
                    # 检查是否退出高频模式
                    if current_time > self.next_check_time:
                        self.high_frequency_mode = False
                        logger.info("退出高频检测模式")
                    
                    # 等待5秒
                    await asyncio.sleep(5)
                else:
                    # 低频检测模式（每60秒检查一次）
                    await asyncio.sleep(60)
                
                # 收集所有需要检测的任务
                detection_tasks = []
                current_timestamp = time.time()
                
                # 获取所有用户文件
                user_files = [f for f in os.listdir(USER_DATA_DIR) if f.endswith('.json')]
                
                for user_file in user_files:
                    try:
                        user_id = int(user_file.split('.')[0])
                        user_data = load_user_data(user_id)
                        
                        # 检查用户是否启用监控
                        if not user_data.get('active', False):
                            continue
                        
                        # 处理用户的所有币种
                        for symbol_info in user_data['symbols']:
                            symbol = symbol_info['symbol']
                            market_type = symbol_info['type']
                            monitor_type = symbol_info.get('monitor', 'price')  # 默认价格监控
                            
                            # 只处理启用状态的监控
                            if not user_data['monitors'][monitor_type]['enabled']:
                                continue
                            
                            # 计算K线周期结束时间
                            if monitor_type == "price":
                                interval_str = symbol_info.get('interval', '15m')
                            else:
                                interval_str = DEFAULT_INTERVAL
                            
                            # 转换为秒数
                            interval_seconds = {
                                '5m': 300,
                                '15m': 900,
                                '60m': 3600,
                                '240m': 14400
                            }.get(interval_str, 900)  # 默认15分钟
                            
                            current_timestamp = time.time()
                            last_end_timestamp = (current_timestamp // interval_seconds) * interval_seconds
                            next_end_timestamp = last_end_timestamp + interval_seconds
                            time_diff = next_end_timestamp - current_timestamp
                            
                            # 检测窗口：结束前10秒到0秒
                            detection_window = 0 <= time_diff <= 10
                            
                            # 生成唯一检测键
                            detection_key = (user_id, symbol, market_type, interval_str, monitor_type, next_end_timestamp)
                            
                            # 检查是否需要检测
                            if detection_window:
                                # 检查是否已经检测过这个周期
                                if self.last_detection.get(detection_key, False):
                                    continue
                                
                                # 标记为已检测
                                self.last_detection[detection_key] = True
                                
                                # 添加到检测任务列表
                                detection_tasks.append(
                                    self.execute_detection(user_id, symbol, market_type, symbol_info)
                                )
                                
                                # 进入高频检测模式
                                if not self.high_frequency_mode:
                                    self.high_frequency_mode = True
                                    self.next_check_time = next_end_timestamp + 10  # 结束时间后10秒退出高频模式
                                    logger.info(f"进入高频检测模式，将持续到 {datetime.fromtimestamp(self.next_check_time)}")
                    
                    except Exception as e:
                        logger.error(f"处理用户文件时出错: {e}")
                        continue
                
                # 并发执行所有检测任务
                if detection_tasks:
                    logger.info(f"本次扫描发现 {len(detection_tasks)} 个检测任务")
                    results = await asyncio.gather(*detection_tasks, return_exceptions=True)
                    
                    # 处理错误结果
                    for result in results:
                        if isinstance(result, Exception):
                            logger.error(f"检测任务执行失败: {result}")
                else:
                    logger.debug("本次扫描未发现需要检测的任务")
                
            except Exception as e:
                logger.error(f"监控主循环出错: {e}")
                await asyncio.sleep(10)
    
    async def execute_detection(self, user_id, symbol, market_type, symbol_info):
        """执行单个检测任务（使用合并API）"""
        monitor_type = symbol_info.get('monitor', 'price')
        
        try:
            if monitor_type == "price":
                interval_str = symbol_info.get('interval', '15m')
                # 使用合并API获取K线数据
                kline_data = await api_manager.get_kline(
                    symbol, 
                    interval_str, 
                    market_type=market_type,
                    limit=2
                )
                await self.check_price_change(
                    user_id, symbol, market_type, symbol_info, kline_data
                )
            
            elif monitor_type == "macd":
                # 使用合并API获取K线数据
                kline_data = await api_manager.get_kline(
                    symbol, 
                    DEFAULT_INTERVAL, 
                    market_type=market_type,
                    limit=100  # MACD需要更多数据
                )
                await self.check_macd(
                    user_id, symbol, market_type, kline_data
                )
            
            elif monitor_type == "ma":
                # 使用合并API获取K线数据
                kline_data = await api_manager.get_kline(
                    symbol, 
                    DEFAULT_INTERVAL, 
                    market_type=market_type,
                    limit=100  # MA需要更多数据
                )
                await self.check_ma_cross(
                    user_id, symbol, market_type, kline_data
                )
        except Exception as e:
            logger.error(f"检测任务出错: {e}")
            raise
    
    async def check_price_change(self, user_id, symbol, market_type, symbol_info, kline_data):
        """检查价格异动"""
        try:
            # 从用户配置获取阈值
            threshold = symbol_info.get('threshold', 
                user_data.get('monitors', {}).get('price', {}).get('threshold', 3.0))
            
            # 解析K线数据
            if len(kline_data) < 2:
                logger.warning(f"K线数据不足，无法检测价格变化: {symbol}")
                return
                
            current_close = float(kline_data[-1][4])
            prev_close = float(kline_data[-2][4])
            
            # 计算价格变化百分比
            change_percent = ((current_close - prev_close) / prev_close) * 100
            
            # 检查是否超过阈值
            if abs(change_percent) > threshold:
                direction = "上涨" if change_percent > 0 else "下跌"
                interval_str = symbol_info.get('interval', '15m')
                message = (
                    f"🚨 价格异动警报: {symbol} ({MARKET_TYPE_NAMES.get(market_type, market_type)}) - {INTERVALS.get(interval_str, interval_str)}\n"
                    f"• 变化: {abs(change_percent):.2f}% ({direction})\n"
                    f"• 前价: {prev_close:.4f}\n"
                    f"• 现价: {current_close:.4f}\n"
                    f"• 阈值: {threshold}%\n"
                    f"• 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                await self.app.bot.send_message(chat_id=user_id, text=message)
                
        except Exception as e:
            logger.error(f"价格异动监控出错: {e}")

    async def check_macd(self, user_id, symbol, market_type, kline_data):
        """检查MACD交叉 - 使用配置的DEFAULT_INTERVAL"""
        try:
            # 使用配置的DEFAULT_INTERVAL获取K线数据
            if not kline_data or len(kline_data) < 50:
                logger.warning(f"无法获取足够的K线数据: {symbol} {DEFAULT_INTERVAL}")
                return
                
            df = klines_to_dataframe(kline_data)
            if df is None or len(df) < 50:
                return
                
            # 计算MACD
            macd, signal, _ = calculate_macd(df)
            
            # 检查是否形成交叉
            key = f"{user_id}_{symbol}_{market_type}"
            
            # 获取最后两个数据点
            if len(macd) < 2 or len(signal) < 2:
                return
                
            current_macd = macd.iloc[-1]
            prev_macd = macd.iloc[-2]
            current_signal = signal.iloc[-1]
            prev_signal = signal.iloc[-2]
            
            # 初始化当前状态
            current_state = None
            
            # 金叉检测：MACD从下方穿越信号线
            if prev_macd < prev_signal and current_macd > current_signal:
                current_state = "golden"
            # 死叉检测：MACD从上方穿越信号线
            elif prev_macd > prev_signal and current_macd < current_signal:
                current_state = "dead"
            
            # 获取上一次状态
            last_state = self.macd_cross_state.get(key, None)
            
            # 如果状态发生变化（且不是初始状态None），则发送通知
            if current_state is not None and current_state != last_state:
                if current_state == "golden":
                    message = (
                        f"📈 MACD金叉信号: {symbol} ({MARKET_TYPE_NAMES.get(market_type, market_type)}) - {INTERVALS.get(DEFAULT_INTERVAL, DEFAULT_INTERVAL)}\n"
                        f"• MACD: {current_macd:.4f}\n"
                        f"• 信号线: {current_signal:.4f}\n"
                        f"• 价格: {df['close'].iloc[-1]:.4f}\n"
                        f"• 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                else:
                    message = (
                        f"📉 MACD死叉信号: {symbol} ({MARKET_TYPE_NAMES.get(market_type, market_type)}) - {INTERVALS.get(DEFAULT_INTERVAL, DEFAULT_INTERVAL)}\n"
                        f"• MACD: {current_macd:.4f}\n"
                        f"• 信号线: {current_signal:.4f}\n"
                        f"• 价格: {df['close'].iloc[-1]:.4f}\n"
                        f"• 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                await self.app.bot.send_message(chat_id=user_id, text=message)
                # 更新状态
                self.macd_cross_state[key] = current_state
                
                # 处理交易信号
                await self.handle_trading_signal(user_id, symbol, market_type, current_state, "macd")
            
        except Exception as e:
            logger.error(f"MACD监控出错: {e}")

    async def check_ma_cross(self, user_id, symbol, market_type, kline_data):
        """检查MA交叉 - 使用配置的DEFAULT_INTERVAL"""
        try:
            # 使用配置的DEFAULT_INTERVAL获取K线数据
            if not kline_data or len(kline_data) < 30:
                logger.warning(f"无法获取足够的K线数据: {symbol} {DEFAULT_INTERVAL}")
                return
                
            df = klines_to_dataframe(kline_data)
            if df is None or len(df) < 30:
                return
                
            # 计算MA指标
            ma9 = calculate_ma(df, 9)
            ma26 = calculate_ma(df, 26)
            
            # 检查是否形成交叉
            key = f"{user_id}_{symbol}_{market_type}"
            
            # 获取最后两个数据点
            if len(ma9) < 2 or len(ma26) < 2:
                return
                
            current_ma9 = ma9.iloc[-1]
            prev_ma9 = ma9.iloc[-2]
            current_ma26 = ma26.iloc[-1]
            prev_ma26 = ma26.iloc[-2]
            
            # 初始化当前状态
            current_state = None
            
            # 金叉检测：MA9从下方穿越MA26
            if prev_ma9 < prev_ma26 and current_ma9 > current_ma26:
                current_state = "golden"
            # 死叉检测：MA9从上方穿越MA26
            elif prev_ma9 > prev_ma26 and current_ma9 < current_ma26:
                current_state = "dead"
            
            # 获取上一次状态
            last_state = self.ma_cross_state.get(key, None)
            
            if current_state is not None and current_state != last_state:
                if current_state == "golden":
                    message = (
                        f"📈 MA金叉信号: {symbol} ({MARKET_TYPE_NAMES.get(market_type, market_type)}) - {INTERVALS.get(DEFAULT_INTERVAL, DEFAULT_INTERVAL)}\n"
                        f"• MA9: {current_ma9:.4f}\n"
                        f"• MA26: {current_ma26:.4f}\n"
                        f"• 价格: {df['close'].iloc[-1]:.4f}\n"
                        f"• 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                else:
                    message = (
                        f"📉 MA死叉信号: {symbol} ({MARKET_TYPE_NAMES.get(market_type, market_type)}) - {INTERVALS.get(DEFAULT_INTERVAL, DEFAULT_INTERVAL)}\n"
                        f"• MA9: {current_ma9:.4f}\n"
                        f"• MA26: {current_ma26:.4f}\n"
                        f"• 价格: {df['close'].iloc[-1]:.4f}\n"
                        f"• 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                await self.app.bot.send_message(chat_id=user_id, text=message)
                self.ma_cross_state[key] = current_state
                
                # 处理交易信号
                await self.handle_trading_signal(user_id, symbol, market_type, current_state, "ma")
            
        except Exception as e:
            logger.error(f"MA交叉监控出错: {e}")
    
    async def handle_trading_signal(self, user_id, symbol, market_type, signal_type, monitor_type):
        """处理交易信号"""
        try:
            user_data = load_user_data(user_id)
            
            # 检查自动交易是否启用
            auto_trading = user_data.get('auto_trading', {})
            if not auto_trading.get('enabled', False):
                return
                
            # 检查API密钥是否设置
            binance_api = user_data.get('binance_api', {})
            api_key = binance_api.get('key', '')
            api_secret = binance_api.get('secret', '')
            if not api_key or not api_secret:
                logger.warning(f"用户 {user_id} 未设置API密钥，无法执行交易")
                return
            
            # 检查交易模式
            trade_mode = auto_trading.get('mode')
            if not trade_mode:
                return
                
            # 检查交易对是否在自动交易列表中
            trade_symbols = auto_trading.get('symbols', [])
            symbol_config = next((s for s in trade_symbols if s['symbol'] == symbol), None)
            if not symbol_config:
                return
                
            # MA模式交易
            if trade_mode == "ma" and monitor_type == "ma":
                await auto_trade_task.execute_trade(
                    user_id, 
                    symbol, 
                    market_type, 
                    signal_type, 
                    symbol_config,
                    api_key,
                    api_secret
                )
            
            # MACD模式交易
            elif trade_mode == "macd" and monitor_type == "macd":
                await auto_trade_task.execute_trade(
                    user_id, 
                    symbol, 
                    market_type, 
                    signal_type, 
                    symbol_config,
                    api_key,
                    api_secret
                )
            
            # MA+MACD联合模式
            elif trade_mode == "mamacd" and monitor_type == "ma":
                # 检查MACD条件
                if await self.check_macd_condition(symbol, market_type):
                    await auto_trade_task.execute_trade(
                        user_id, 
                        symbol, 
                        market_type, 
                        signal_type, 
                        symbol_config,
                        api_key,
                        api_secret
                    )
    
        except Exception as e:
            logger.error(f"处理交易信号失败: {e}")
    
    async def check_macd_condition(self, symbol, market_type):
        """检查MACD条件（MA+MACD模式使用）"""
        try:
            kline_data = await api_manager.get_kline(
                symbol, 
                DEFAULT_INTERVAL, 
                market_type=market_type,
                limit=100
            )
            if not kline_data or len(kline_data) < 50:
                return False
                
            df = klines_to_dataframe(kline_data)
            if df is None or len(df) < 50:
                return False
                
            # 计算MACD
            macd, signal, _ = calculate_macd(df)
            
            # 检查MACD线是否在信号线上方
            return macd.iloc[-1] > signal.iloc[-1]
            
        except Exception as e:
            logger.error(f"检查MACD条件失败: {e}")
            return False


# --- 监控管理 ---
monitor_task = None

async def start_monitor(app):
    """启动监控任务"""
    global monitor_task, auto_trade_task
    if monitor_task is None or not monitor_task.active:
        monitor_task = MonitorTask(app)
        auto_trade_task = AutoTradeTask(app)
        # 在事件循环中启动监控任务
        monitor_task.task = asyncio.create_task(monitor_task.run())
        logger.info("监控任务已启动")
        return True
    return False

async def stop_monitor():
    """停止监控任务"""
    global monitor_task
    if monitor_task:
        monitor_task.active = False
        # 等待任务完成
        if monitor_task.task:
            await monitor_task.task
        monitor_task = None
        logger.info("监控任务已停止")
        return True
    return False

# --- 用户状态管理 ---
user_states = {}

def set_user_state(user_id, state, data=None):
    """设置用户状态"""
    if data is None:
        data = {}
    user_states[user_id] = {"state": state, "data": data}

def get_user_state(user_id):
    """获取用户状态"""
    return user_states.get(user_id, {})

def clear_user_state(user_id):
    """清除用户状态"""
    if user_id in user_states:
        del user_states[user_id]

# --- 按钮回调 ---
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data_parts = query.data.split(":")

    # 确保用户已授权
    if user_id not in CHAT_IDS:
        await query.message.reply_text("您未获得使用此机器人的授权")
        return

    try:
        if data_parts[0] == "select_type":
            symbol = data_parts[1]
            market_type = data_parts[2]
            monitor_type = data_parts[3]
            
            # 保存到用户状态，不立即添加
            set_user_state(user_id, "add_symbol_config", {
                "symbol": symbol.upper(),
                "type": market_type,
                "monitor": monitor_type
            })
            
            await query.edit_message_text(f"已选择 {symbol} ({MARKET_TYPE_NAMES.get(market_type, market_type)}) 的{MONITOR_TYPE_NAMES.get(monitor_type, monitor_type)}监控")
            
            # 根据监控类型进行下一步
            if monitor_type == "price":
                # 价格异动需要选择周期
                keyboard = [
                    [InlineKeyboardButton("5分钟", callback_data=f"select_interval:5m")],
                    [InlineKeyboardButton("15分钟", callback_data=f"select_interval:15m")],
                    [InlineKeyboardButton("60分钟", callback_data=f"select_interval:60m")]
                ]
                await query.message.reply_text(
                    f"请选择 {symbol} 的价格异动监控周期:",
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                # 非价格监控，直接完成配置
                state_info = get_user_state(user_id)
                config = state_info.get("data", {})
                
                # 添加到监控列表
                user_data = load_user_data(user_id)
                user_data["symbols"].append(config)
                save_user_data(user_id, user_data)
                
                # 询问是否继续添加
                keyboard = [
                    [InlineKeyboardButton("✅ 继续添加", callback_data=f"continue_add:{monitor_type}")],
                    [InlineKeyboardButton("❌ 完成添加", callback_data=f"finish_add:{monitor_type}")]
                ]
                await query.message.reply_text(
                    "是否继续添加交易对?",
                    reply_markup=InlineKeyboardMarkup(keyboard))
        
        elif data_parts[0] == "select_interval":
            interval = data_parts[1]
            state_info = get_user_state(user_id)
            config = state_info.get("data", {})
            config["interval"] = interval
            
            # 更新用户状态
            set_user_state(user_id, "set_price_threshold", config)
            await query.edit_message_text(f"已选择 {INTERVALS.get(interval, interval)}周期")
            
            await query.message.reply_text(
                "请输入价格异动的阈值百分比（例如：0.5）:",
                reply_markup=back_markup)
        
        # 处理继续添加回调
        elif data_parts[0] == "continue_add":
            monitor_type = data_parts[1]
            set_user_state(user_id, f"add_symbol:{monitor_type}")
            await query.message.reply_text(
                f"请输入要添加{MONITOR_TYPE_NAMES.get(monitor_type, monitor_type)}监控的交易对（例如：BTCUSDT）:",
                reply_markup=back_markup)
        
        elif data_parts[0] == "finish_add":
            monitor_type = data_parts[1]
            user_data = load_user_data(user_id)
            
            # 询问是否立即开启监控
            keyboard = [
                [InlineKeyboardButton("✅ 立即开启", callback_data=f"enable_now:{monitor_type}")],
                [InlineKeyboardButton("❌ 稍后开启", callback_data="back_to_main")]
            ]
            
            # 获取已添加的币种列表（带详细信息）
            symbols_list = []
            for s in user_data['symbols']:
                if s['monitor'] == monitor_type:
                    if monitor_type == "price":
                        symbols_list.append(
                            f"• {s['symbol']}（{MARKET_TYPE_NAMES.get(s['type'], s['type'])}) 周期: {INTERVALS.get(s.get('interval', '15m'), '15分钟')} 阈值: {s.get('threshold', 5.0)}%"
                        )
                    else:
                        symbols_list.append(
                            f"• {s['symbol']}（{MARKET_TYPE_NAMES.get(s['type'], s['type'])})"
                        )
            
            symbols_list_text = "\n".join(symbols_list) if symbols_list else "无"
            
            await query.message.reply_text(
                f"已添加以下{MONITOR_TYPE_NAMES.get(monitor_type, monitor_type)}监控的交易对:\n{symbols_list_text}\n\n是否立即开启{MONITOR_TYPE_NAMES.get(monitor_type, monitor_type)}监控?",
                reply_markup=InlineKeyboardMarkup(keyboard))
        
        elif data_parts[0] == "enable_now":
            monitor_type = data_parts[1]
            user_data = load_user_data(user_id)
            
            # 启用监控
            user_data['monitors'][monitor_type]['enabled'] = True
            user_data['active'] = True
            save_user_data(user_id, user_data)
            
            # 获取监控币种列表（带详细信息）
            symbols_list = []
            for s in user_data['symbols']:
                if s['monitor'] == monitor_type:
                    if monitor_type == "price":
                        symbols_list.append(
                            f"• {s['symbol']}（{MARKET_TYPE_NAMES.get(s['type'], s['type'])}) 周期: {INTERVALS.get(s.get('interval', '15m'), '15分钟')} 阈值: {s.get('threshold', 5.0)}%"
                        )
                    else:
                        symbols_list.append(
                            f"• {s['symbol']}（{MARKET_TYPE_NAMES.get(s['type'], s['type'])})"
                        )
            
            symbols_list_text = "\n".join(symbols_list) if symbols_list else "无"
            
            await query.message.reply_text(
                f"✅ 已开启{MONITOR_TYPE_NAMES.get(monitor_type, monitor_type)}监控\n\n监控币种列表:\n{symbols_list_text}",
                reply_markup=reply_markup)
            clear_user_state(user_id)
        
        elif data_parts[0] == "back_to_main":
            clear_user_state(user_id)
            await query.message.reply_text(
                "已返回主菜单",
                reply_markup=reply_markup)
        
        elif data_parts[0] == "select_monitor":
            monitor_type = data_parts[1]
            set_user_state(user_id, f"add_symbol:{monitor_type}")
            await query.message.reply_text(
                f"请输入要添加{MONITOR_TYPE_NAMES.get(monitor_type, monitor_type)}监控的交易对（例如：BTCUSDT）:",
                reply_markup=back_markup)
        
        elif data_parts[0] == "remove_monitor":
            monitor_type = data_parts[1]
            user_data = load_user_data(user_id)
            
            # 获取指定监控类型的交易对
            symbols = [s for s in user_data['symbols'] if s['monitor'] == monitor_type]
            
            if not symbols:
                await query.message.reply_text(
                    f"当前没有{MONITOR_TYPE_NAMES.get(monitor_type, monitor_type)}监控的交易对",
                    reply_markup=reply_markup)
                return
            
            # 显示交易对列表（带详细信息）
            symbols_list = []
            for i, s in enumerate(symbols):
                if monitor_type == "price":
                    symbols_list.append(
                        f"{i+1}. {s['symbol']}（{MARKET_TYPE_NAMES.get(s['type'], s['type'])}) 周期: {INTERVALS.get(s.get('interval', '15m'), '15分钟')} 阈值: {s.get('threshold', 5.0)}%"
                    )
                else:
                    symbols_list.append(
                        f"{i+1}. {s['symbol']}（{MARKET_TYPE_NAMES.get(s['type'], s['type'])})"
                    )
            
            symbols_list_text = "\n".join(symbols_list)
            
            set_user_state(user_id, f"remove_symbol:{monitor_type}")
            await query.message.reply_text(
                f"请选择要删除的交易对:\n{symbols_list_text}\n\n请输入编号:",
                reply_markup=back_markup)
        
        elif data_parts[0] == "enable_monitor":
            monitor_type = data_parts[1]
            user_data = load_user_data(user_id)
            
            if monitor_type == "all":
                # 启用所有监控
                for mt in user_data["monitors"]:
                    user_data["monitors"][mt]["enabled"] = True
                user_data["active"] = True
                monitor_type_str = "全部监控"
            else:
                user_data["monitors"][monitor_type]["enabled"] = True
                user_data["active"] = True
                monitor_type_str = MONITOR_TYPE_NAMES.get(monitor_type, monitor_type)
            
            save_user_data(user_id, user_data)
            
            # 获取监控币种列表（按类型分组显示）
            groups = {"price": [], "macd": [], "ma": []}
            for s in user_data['symbols']:
                if monitor_type == "all" or s['monitor'] == monitor_type:
                    groups[s['monitor']].append(s)
            
            symbols_list_text = ""
            for mt, symbols in groups.items():
                if symbols:
                    symbols_list_text += f"\n{MONITOR_TYPE_NAMES.get(mt, mt)}监控:\n"
                    for s in symbols:
                        if mt == "price":
                            symbols_list_text += f"• {s['symbol']}（{MARKET_TYPE_NAMES.get(s['type'], s['type'])}) 周期: {INTERVALS.get(s.get('interval', '15m'), '15分钟')} 阈值: {s.get('threshold', 5.0)}%\n"
                        else:
                            symbols_list_text += f"• {s['symbol']}（{MARKET_TYPE_NAMES.get(s['type'], s['type'])}) \n"
            
            if not symbols_list_text:
                symbols_list_text = "无"
            
            await query.message.reply_text(
                f"✅ 已开启{monitor_type_str}监控\n\n监控币种列表:{symbols_list_text}",
                reply_markup=reply_markup)
            clear_user_state(user_id)
        
        elif data_parts[0] == "disable_monitor":
            monitor_type = data_parts[1]
            user_data = load_user_data(user_id)
            
            if monitor_type == "all":
                # 禁用所有监控
                for mt in user_data["monitors"]:
                    user_data["monitors"][mt]["enabled"] = False
                user_data["active"] = False
                monitor_type_str = "全部监控"
                await query.message.reply_text(
                    f"✅ 已关闭{monitor_type_str}",
                    reply_markup=reply_markup)
            else:
                user_data["monitors"][monitor_type]["enabled"] = False
                monitor_type_str = MONITOR_TYPE_NAMES.get(monitor_type, monitor_type)
                
                # 检查是否还有任何监控启用
                any_enabled = any(user_data["monitors"][mt]["enabled"] for mt in user_data["monitors"])
                user_data["active"] = any_enabled
            
                save_user_data(user_id, user_data)
                await query.message.reply_text(
                    f"✅ 已关闭{monitor_type_str}监控",
                    reply_markup=reply_markup)
            clear_user_state(user_id)
        
        # 自动交易设置回调
        elif data_parts[0] == "auto_trade":
            action = data_parts[1]
            
            if action == "add":
                set_user_state(user_id, "auto_trade:add_symbol")
                await query.message.reply_text(
                    "请输入要添加自动交易的交易对（例如：BTCUSDT）:",
                    reply_markup=back_markup)
                    
            elif action == "remove":
                user_data = load_user_data(user_id)
                symbols = user_data['auto_trading'].get('symbols', [])
                
                if not symbols:
                    await query.message.reply_text("当前没有自动交易对", reply_markup=reply_markup)
                    return
                    
                # 显示交易对列表
                symbols_list = []
                for i, s in enumerate(symbols):
                    symbols_list.append(
                        f"{i+1}. {s['symbol']} 杠杆: {s['leverage']}x 金额: ${s['amount']} 止盈: {s['tp']}% 止损: {s['sl']}%"
                    )
                    
                set_user_state(user_id, "auto_trade:remove_symbol")
                await query.message.reply_text(
                    f"请选择要删除的交易对:\n" + "\n".join(symbols_list) + "\n\n请输入编号:",
                    reply_markup=back_markup)
                    
            elif action == "set_mode":
                keyboard = [
                    [InlineKeyboardButton("MA交叉交易", callback_data="set_trade_mode:ma")],
                    [InlineKeyboardButton("MACD交叉交易", callback_data="set_trade_mode:macd")],
                    [InlineKeyboardButton("MA+MACD联合交易", callback_data="set_trade_mode:mamacd")],
                    [InlineKeyboardButton("↩️ 返回", callback_data="auto_trade:back")]
                ]
                await query.message.reply_text(
                    "请选择自动交易模式:",
                    reply_markup=InlineKeyboardMarkup(keyboard))
            
            elif action == "set_api":
                set_user_state(user_id, "auto_trade:set_api_key")
                # 明确区分API密钥和密钥
                await query.message.reply_text(
                    "请输入您的Binance API密钥（Key）:",
                    reply_markup=back_markup)
                    
            elif action == "back":
                clear_user_state(user_id)
                await query.message.reply_text(
                    "已返回自动交易设置",
                    reply_markup=reply_markup)
        
        elif data_parts[0] == "set_trade_mode":
            mode = data_parts[1]
            user_data = load_user_data(user_id)
            user_data['auto_trading']['mode'] = mode
            save_user_data(user_id, user_data)
            
            await query.message.reply_text(
                f"✅ 已设置交易模式: {TRADE_MODES.get(mode, mode)}",
                reply_markup=reply_markup)
    
    except Exception as e:
        logger.error(f"按钮回调处理出错: {e}", exc_info=True)
        await query.message.reply_text(
            "处理您的请求时出错，请重试",
            reply_markup=reply_markup)

# --- 命令处理 ---
async def start(update, context):
    user_id = update.effective_chat.id
    
    # 检查用户是否授权
    if user_id not in CHAT_IDS:
        await update.message.reply_text("您未获得使用此机器人的授权")
        return
    
    try:
        # 初始化用户数据
        user_data = load_user_data(user_id)
        save_user_data(user_id, user_data)
        
        await update.message.reply_text(
            "👋欢迎使用币安量化管家📊\n请使用下方菜单开始操作",
            reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"启动命令出错: {e}")
        await update.message.reply_text("初始化失败，请稍后再试")

async def add_symbol(update, context):
    user_id = update.effective_chat.id
    
    # 选择监控类型
    keyboard = [
        [InlineKeyboardButton("1. 价格异动监控", callback_data="select_monitor:price")],
        [InlineKeyboardButton("2. MACD交叉监控", callback_data="select_monitor:macd")],
        [InlineKeyboardButton("3. MA交叉监控", callback_data="select_monitor:ma")],
        [InlineKeyboardButton("↩️ 返回主菜单", callback_data="back_to_main")]
    ]
    
    await update.message.reply_text(
        "请选择要添加的监控类型:",
        reply_markup=InlineKeyboardMarkup(keyboard))

async def remove_symbol(update, context):
    user_id = update.effective_chat.id
    user_data = load_user_data(user_id)
    
    if not user_data["symbols"]:
        await update.message.reply_text("当前没有监控的交易对", reply_markup=reply_markup)
        return
    
    # 选择监控类型
    keyboard = [
        [InlineKeyboardButton("1. 价格异动监控", callback_data="remove_monitor:price")],
        [InlineKeyboardButton("2. MACD交叉监控", callback_data="remove_monitor:macd")],
        [InlineKeyboardButton("3. MA交叉监控", callback_data="remove_monitor:ma")],
        [InlineKeyboardButton("↩️ 返回主菜单", callback_data="back_to_main")]
    ]
    
    await update.message.reply_text(
        "请选择要删除的监控类型:",
        reply_markup=InlineKeyboardMarkup(keyboard))

async def enable_monitoring(update, context):
    user_id = update.effective_chat.id
    user_data = load_user_data(user_id)
    
    if not user_data["symbols"]:
        await update.message.reply_text("请先添加交易对", reply_markup=reply_markup)
        return
    
    # 创建监控类型选择键盘
    keyboard = [
        [InlineKeyboardButton("1. 价格异动监控", callback_data="enable_monitor:price")],
        [InlineKeyboardButton("2. MACD交叉监控", callback_data="enable_monitor:macd")],
        [InlineKeyboardButton("3. MA交叉监控", callback_data="enable_monitor:ma")],
        [InlineKeyboardButton("4. 全部监控", callback_data="enable_monitor:all")],
        [InlineKeyboardButton("↩️ 返回主菜单", callback_data="back_to_main")]
    ]
    
    await update.message.reply_text(
        "请选择要开启的监控类型:",
        reply_markup=InlineKeyboardMarkup(keyboard))

async def disable_monitoring(update, context):
    user_id = update.effective_chat.id
    user_data = load_user_data(user_id)
    
    if not user_data["active"]:
        await update.message.reply_text("监控尚未开启", reply_markup=reply_markup)
        return
    
    # 创建监控类型选择键盘
    keyboard = [
        [InlineKeyboardButton("1. 价格异动监控", callback_data="disable_monitor:price")],
        [InlineKeyboardButton("2. MACD交叉监控", callback_data="disable_monitor:macd")],
        [InlineKeyboardButton("3. MA交叉监控", callback_data="disable_monitor:ma")],
        [InlineKeyboardButton("4. 全部监控", callback_data="disable_monitor:all")],
        [InlineKeyboardButton("↩️ 返回主菜单", callback_data="back_to_main")]
    ]
    
    await update.message.reply_text(
        "请选择要关闭的监控类型:",
        reply_markup=InlineKeyboardMarkup(keyboard))

async def auto_trading_settings(update, context):
    """自动交易设置菜单"""
    user_id = update.effective_chat.id
    keyboard = [
        [InlineKeyboardButton("1. 设置API密钥", callback_data="auto_trade:set_api")],
        [InlineKeyboardButton("2. 添加交易对", callback_data="auto_trade:add")],
        [InlineKeyboardButton("3. 删除交易对", callback_data="auto_trade:remove")],
        [InlineKeyboardButton("4. 设置交易模式", callback_data="auto_trade:set_mode")],
        [InlineKeyboardButton("↩️ 返回主菜单", callback_data="back_to_main")]
    ]
    await update.message.reply_text(
        "自动交易设置:",
        reply_markup=InlineKeyboardMarkup(keyboard))

async def toggle_auto_trading(update, context):
    """启动/关闭自动交易"""
    user_id = update.effective_chat.id
    user_data = load_user_data(user_id)
    
    auto_trading = user_data.get('auto_trading', {})
    binance_api = user_data.get('binance_api', {})
    enabled = auto_trading.get('enabled', False)
    
    if enabled:
        # 关闭自动交易
        auto_trading['enabled'] = False
        message = "✅ 自动交易已关闭"
    else:
        # 检查API密钥是否设置
        if not binance_api.get('key') or not binance_api.get('secret'):
            await update.message.reply_text("请先设置API密钥", reply_markup=reply_markup)
            return
            
        # 检查是否配置了交易对和模式
        if not auto_trading.get('symbols') or not auto_trading.get('mode'):
            await update.message.reply_text("请先配置交易对和交易模式", reply_markup=reply_markup)
            return
            
        # 开启自动交易
        auto_trading['enabled'] = True
        
        # 构建自动交易明细
        trade_details = "\n\n自动交易明细:"
        for symbol in auto_trading['symbols']:
            trade_details += (
                f"\n• {symbol['symbol']}（合约）"
                f"\n  杠杆: {symbol['leverage']}x"
                f"\n  下单金额: ${symbol['amount']}"
                f"\n  止盈: {symbol['tp']}%"
                f"\n  止损: {symbol['sl']}%"
            )
            
        message = f"✅ 自动交易已启动{trade_details}"
    
    save_user_data(user_id, user_data)
    await update.message.reply_text(message, reply_markup=reply_markup)

async def show_status(update, context):
    user_id = update.effective_chat.id
    try:
        user_data = load_user_data(user_id)
        
        status = "🔴 监控已停止"
        if user_data["active"]:
            status = "🟢 监控运行中"
        
        # 按监控类型分组
        monitor_groups = {
            "price": [],
            "macd": [],
            "ma": []
        }

        for s in user_data['symbols']:
            monitor_type = s.get('monitor', 'price')
            if monitor_type in monitor_groups:
                monitor_groups[monitor_type].append(s)
        
        # 价格异动监控状态
        price_status = "🟢 已启用" if user_data["monitors"]["price"]["enabled"] else "🔴 已禁用"
        price_list = "\n".join([
            f"  • {s['symbol']}（{MARKET_TYPE_NAMES.get(s['type'], s['type'])}) 周期: {INTERVALS.get(s.get('interval', '15m'), '15分钟')} 阈值: {s.get('threshold', 5.0)}%"
            for s in monitor_groups["price"]
        ]) if monitor_groups["price"] else "  无"
        
        # MACD监控状态
        macd_status = "🟢 已启用" if user_data["monitors"]["macd"]["enabled"] else "🔴 已禁用"
        macd_list = "\n".join([
            f"  • {s['symbol']}（{MARKET_TYPE_NAMES.get(s['type'], s['type'])})" 
            for s in monitor_groups["macd"]
        ]) if monitor_groups["macd"] else "  无"
        
        # MA监控状态
        ma_status = "🟢 已启用" if user_data["monitors"]["ma"]["enabled"] else "🔴 已禁用"
        ma_list = "\n".join([
            f"  • {s['symbol']}（{MARKET_TYPE_NAMES.get(s['type'], s['type'])})" 
            for s in monitor_groups["ma"]
        ]) if monitor_groups["ma"] else "  无"
        
        # 显示当前使用的K线周期
        ma_macd_interval = INTERVALS.get(DEFAULT_INTERVAL, DEFAULT_INTERVAL)
        
        # 自动交易状态
        auto_trading = user_data.get('auto_trading', {})
        auto_status = "🟢 已启用" if auto_trading.get('enabled', False) else "🔴 已禁用"
        mode = TRADE_MODES.get(auto_trading.get('mode', ''), "未设置")
        
        # 自动交易对列表
        auto_symbols = auto_trading.get('symbols', [])
        auto_list = "\n".join([
            f"  • {s['symbol']} 杠杆: {s['leverage']}x 金额: ${s['amount']} 止盈: {s['tp']}% 止损: {s['sl']}%"
            for s in auto_symbols
        ]) if auto_symbols else "  无"
        
        # API密钥状态
        binance_api = user_data.get('binance_api', {})
        api_status = "🟢 已设置" if binance_api.get('key') and binance_api.get('secret') else "🔴 未设置"
        
        # 持仓列表
        system_positions_list = "  无系统持仓"
        other_positions_list = "  无非系统持仓"
        
        if binance_api.get('key') and binance_api.get('secret'):
            try:
                # 更新账户持仓
                success = await trade_manager.update_positions(user_id)
                if not success:
                    system_positions_list = "  更新持仓失败"
                    other_positions_list = "  更新持仓失败"
                else:
                    positions = trade_manager.positions
                    
                    # 获取系统持仓记录
                    system_positions = user_data.get('system_positions', {})
                    
                    # 分离系统持仓和非系统持仓
                    system_positions_items = []
                    other_positions_items = []
                    
                    for symbol, pos in positions.items():
                        # 检查是否为系统持仓
                        if symbol in system_positions:
                            # 获取系统持仓详情
                            sys_pos = system_positions[symbol]
                            if isinstance(sys_pos, list) and len(sys_pos) > 0:
                                # 取最新的一条系统持仓记录
                                latest_sys_pos = sys_pos[-1]
                                system_positions_items.append(
                                    f"  • {symbol} {POSITION_SIDE[pos['side']]} {latest_sys_pos.get('leverage', 'N/A')}x "
                                    f"数量: {pos['quantity']:.4f} "
                                    f"开仓价: {latest_sys_pos.get('entry_price', 'N/A'):.4f} "
                                    f"盈亏: ${pos['unrealized_profit']:.2f}"
                                )
                            else:
                                # 如果系统持仓记录异常，仍然显示
                                system_positions_items.append(
                                    f"  • {symbol} {POSITION_SIDE[pos['side']]} {pos.get('leverage', 'N/A')}x "
                                    f"数量: {pos['quantity']:.4f} "
                                    f"盈亏: ${pos['unrealized_profit']:.2f}"
                                )
                        else:
                            # 非系统持仓
                            other_positions_items.append(
                                f"  • {symbol} {POSITION_SIDE[pos['side']]} {pos.get('leverage', 'N/A')}x "
                                f"数量: {pos['quantity']:.4f} "
                                f"开仓价: {pos.get('entry_price', 'N/A'):.4f} "
                                f"盈亏: ${pos['unrealized_profit']:.2f}"
                            )
                    
                    # 格式化持仓列表
                    if system_positions_items:
                        system_positions_list = "\n".join(system_positions_items)
                    if other_positions_items:
                        other_positions_list = "\n".join(other_positions_items)
                
            except Exception as e:
                logger.error(f"更新持仓失败: {e}")
                system_positions_list = "  更新持仓失败"
                other_positions_list = "  更新持仓失败"
        
        message = (
            f"📊 监控状态: {status}\n\n"
            f"1️⃣ 价格异动监控: {price_status}\n"
            f"   监控币种:\n{price_list}\n\n"
            f"2️⃣ MACD交叉监控: {macd_status}\n"
            f"   监控币种:\n{macd_list}\n\n"
            f"3️⃣ MA交叉监控: {ma_status}\n"
            f"   监控币种:\n{ma_list}\n\n"
            f"📈 MACD和MA监控使用 {ma_macd_interval} K线周期\n\n"
            f"🔧 自动交易状态: {auto_status}\n"
            f"   API状态: {api_status}\n"
            f"   交易模式: {mode}\n"
            f"   交易对列表:\n{auto_list}\n\n"
            f"💰 系统持仓列表（由机器人自动开仓）:\n{system_positions_list}\n\n"
            f"💰 非系统持仓列表（用户手动开仓）:\n{other_positions_list}"
        )
        
        await update.message.reply_text(message, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"显示状态出错: {e}", exc_info=True)
        await update.message.reply_text("获取状态失败，请稍后再试")

async def show_help(update, context):
    # 获取当前配置的MA/MACD周期
    ma_macd_interval = INTERVALS.get(DEFAULT_INTERVAL, DEFAULT_INTERVAL)
    
    help_text = (
        "📚 币安监控机器人使用指南\n\n"
        "1️⃣ 添加监控币种 - 添加新的监控币种\n"
        "2️⃣ 删除监控币种 - 删除现有监控币种\n"
        "3️⃣ 开启监控 - 启动价格监控\n"
        "4️⃣ 关闭监控 - 暂停价格监控\n"
        "5️⃣ 自动交易设置 - 设置自动交易参数\n"
        "6️⃣ 启动/关闭自动交易 - 开关自动交易功能\n"
        "7️⃣ 查看状态 - 查看当前监控和交易状态\n"
        "8️⃣ 帮助 - 显示使用指南\n\n"
        "监控类型说明:\n"
        "• 价格异动监控: 检测指定周期内价格波动超过设定阈值\n"
        "• MACD交叉监控: 检测MACD指标的金叉/死叉信号（基于{ma_macd_interval}K线）\n"
        "• MA交叉监控: 检测MA9和MA26的交叉信号（基于{ma_macd_interval}K线）\n\n"
        "自动交易模式:\n"
        "• MA交叉交易: 以MA交叉信号为交易信号\n"
        "• MACD交叉交易: 以MACD交叉信号为交易信号\n"
        "• MA+MACD联合交易: 以MA信号为主，验证MACD指标>0后交易\n\n"
        "🔄 服务器时间每5分钟与Binance同步一次\n"
        "⏱ 所有监控数据每分钟刷新一次"
    ).format(ma_macd_interval=ma_macd_interval)
    
    await update.message.reply_text(help_text, reply_markup=reply_markup)

async def handle_message(update, context):
    user_id = update.effective_chat.id
    text = update.message.text.strip()
    
    # 检查用户是否授权
    if user_id not in CHAT_IDS:
        await update.message.reply_text("您未获得使用此机器人的授权")
        return
    
    # 记录当前状态
    state_info = get_user_state(user_id)
    state = state_info.get("state", "")
    logger.info(f"用户 {user_id} 当前状态: {state}")
    
    try:
        # 处理取消/返回命令
        if text in ["❌ 取消", "取消", "↩️ 返回主菜单"]:
            clear_user_state(user_id)
            await update.message.reply_text("已返回主菜单", reply_markup=reply_markup)
            return
        
        # 优先处理状态中的输入
        if state.startswith("add_symbol:"):
            monitor_type = state.split(":")[1]
            
            # 验证交易对格式
            if not (len(text) >= 5 and text.isalnum()):
                await update.message.reply_text("无效的交易对格式，请重新输入（例如：BTCUSDT）")
                return
            
            # 创建市场类型选择键盘
            keyboard = [
                [InlineKeyboardButton("现货", callback_data=f"select_type:{text.upper()}:spot:{monitor_type}")],
                [InlineKeyboardButton("合约", callback_data=f"select_type:{text.upper()}:contract:{monitor_type}")]
            ]
            
            await update.message.reply_text(
                f"请选择 {text.upper()} 的市场类型:",
                reply_markup=InlineKeyboardMarkup(keyboard))
            
            clear_user_state(user_id)
            return
        
        elif state.startswith("remove_symbol:"):
            monitor_type = state.split(":")[1]
            try:
                idx = int(text) - 1
                user_data = load_user_data(user_id)
                
                # 获取指定监控类型的交易对
                symbols = [s for s in user_data['symbols'] if s['monitor'] == monitor_type]
                
                if 0 <= idx < len(symbols):
                    # 从原始列表中删除
                    symbol_to_remove = symbols[idx]
                    user_data['symbols'] = [s for s in user_data['symbols'] if s != symbol_to_remove]
                    
                    save_user_data(user_id, user_data)
                    
                    # 合并两条消息为一条
                    message = f"已删除 {symbol_to_remove['symbol']}（{MARKET_TYPE_NAMES.get(symbol_to_remove['type'], symbol_to_remove['type'])})\n\n"
                    # 添加剩余列表
                    symbols_list = []
                    for i, s in enumerate([s for s in user_data['symbols'] if s['monitor'] == monitor_type]):
                        if monitor_type == "price":
                            symbols_list.append(
                                f"{i+1}. {s['symbol']}（{MARKET_TYPE_NAMES.get(s['type'], s['type'])}) 周期: {INTERVALS.get(s.get('interval', '15m'), '15分钟')} 阈值: {s.get('threshold', 5.0)}%"
                            )
                        else:
                            symbols_list.append(
                                f"{i+1}. {s['symbol']}（{MARKET_TYPE_NAMES.get(s['type'], s['type'])})"
                            )
                    message += f"剩余{MONITOR_TYPE_NAMES.get(monitor_type, monitor_type)}监控的交易对:\n\n" + "\n".join(symbols_list) + "\n\n请输入编号删除或输入'取消'返回主菜单:"
                    
                    await update.message.reply_text(message, reply_markup=back_markup)
                else:
                    await update.message.reply_text("无效的编号，请重新输入")
            except ValueError:
                await update.message.reply_text("请输入有效的编号（例如：1）")
            return
        
        elif state == "set_price_threshold":
            try:
                threshold = float(text)
                if threshold <= 0 or threshold > 50:
                    await update.message.reply_text("阈值必须在0.1到50之间，请重新输入")
                    return
                    
                user_data = load_user_data(user_id)
                state_info = get_user_state(user_id)
                config = state_info.get("data", {})
                config["threshold"] = threshold
                
                # 添加完整的监控配置
                user_data["symbols"].append(config)
                save_user_data(user_id, user_data)
                logger.info(f"用户 {user_id} 添加 {config['symbol']} 监控: 周期{config.get('interval','')} 阈值{threshold}%")
                
                # 询问是否继续添加
                keyboard = [
                    [InlineKeyboardButton("✅ 继续添加", callback_data=f"continue_add:price")],
                    [InlineKeyboardButton("❌ 完成添加", callback_data=f"finish_add:price")]
                ]
                reply_markup_kb = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(
                    f"已为 {config['symbol']}（{MARKET_TYPE_NAMES.get(config['type'], config['type'])}) 添加价格异动监控: 周期{INTERVALS.get(config.get('interval','15m'), '15分钟')} 阈值{threshold}%\n\n是否继续添加交易对?",
                    reply_markup=reply_markup_kb)
                # 清除状态
                clear_user_state(user_id)
                return
            except ValueError:
                await update.message.reply_text("请输入有效的数字（例如：0.5）")
                return
        
        # 自动交易状态处理
        elif state == "auto_trade:add_symbol":
            symbol = text.upper()
            
            # 验证交易对格式
            if not (len(symbol) >= 5 and symbol.isalnum()):
                await update.message.reply_text("无效的交易对格式，请重新输入（例如：BTCUSDT）")
                return
                
            # 保存到状态
            set_user_state(user_id, "auto_trade:set_leverage", {"symbol": symbol})
            await update.message.reply_text(
                f"请设置 {symbol} 的合约杠杆（1-125，例如：10）:",
                reply_markup=back_markup)
            return
                
        elif state == "auto_trade:set_leverage":
            try:
                leverage = int(text)
                if leverage < 1 or leverage > 125:
                    await update.message.reply_text("杠杆必须在1-125之间，请重新输入")
                    return
                    
                state_info = get_user_state(user_id)
                config = state_info.get("data", {})
                config["leverage"] = leverage
                set_user_state(user_id, "auto_trade:set_amount", config)
                await update.message.reply_text(
                    f"请设置下单金额（USDT）（例如：100）:",
                    reply_markup=back_markup)
                return
                    
            except ValueError:
                await update.message.reply_text("请输入有效的整数")
                return
                
        elif state == "auto_trade:set_amount":
            try:
                amount = float(text)
                if amount <= 0:
                    await update.message.reply_text("金额必须大于0，请重新输入")
                    return
                    
                state_info = get_user_state(user_id)
                config = state_info.get("data", {})
                config["amount"] = amount
                set_user_state(user_id, "auto_trade:set_tp", config)
                await update.message.reply_text(
                    f"请设置止盈百分比（例如：5，输入0表示不设置）:",
                    reply_markup=back_markup)
                return
                    
            except ValueError:
                await update.message.reply_text("请输入有效的数字")
                return
                
        elif state == "auto_trade:set_tp":
            try:
                tp = float(text)
                if tp < 0:  # 允许0值
                    await update.message.reply_text("止盈不能为负数，请重新输入")
                    return
                    
                state_info = get_user_state(user_id)
                config = state_info.get("data", {})
                config["tp"] = tp
                set_user_state(user_id, "auto_trade:set_sl", config)
                await update.message.reply_text(
                    f"请设置止损百分比（例如：3，输入0表示不设置）:",
                    reply_markup=back_markup)
                return
                    
            except ValueError:
                await update.message.reply_text("请输入有效的数字")
                return
                
        elif state == "auto_trade:set_sl":
            try:
                sl = float(text)
                if sl < 0:  # 允许0值
                    await update.message.reply_text("止损不能为负数，请重新输入")
                    return
                    
                state_info = get_user_state(user_id)
                config = state_info.get("data", {})
                config["sl"] = sl
                
                # 添加到自动交易列表
                user_data = load_user_data(user_id)
                
                # 确保auto_trading和symbols字段存在
                if 'auto_trading' not in user_data:
                    user_data['auto_trading'] = {'symbols': []}
                elif 'symbols' not in user_data['auto_trading']:
                    user_data['auto_trading']['symbols'] = []
                
                user_data['auto_trading']['symbols'].append(config)
                save_user_data(user_id, user_data)
                
                # 显示添加结果
                tp_text = f"{config['tp']}%" if config['tp'] > 0 else "不设置"
                sl_text = f"{config['sl']}%" if config['sl'] > 0 else "不设置"
                
                # 询问是否继续添加
                keyboard = [
                    [InlineKeyboardButton("✅ 继续添加", callback_data="auto_trade:add")],
                    [InlineKeyboardButton("❌ 完成添加", callback_data="auto_trade:back")]
                ]
                
                await update.message.reply_text(
                    f"✅ 已添加自动交易对: {config['symbol']}\n"
                    f"• 杠杆: {config['leverage']}x\n"
                    f"• 金额: ${config['amount']}\n"
                    f"• 止盈: {tp_text}\n"
                    f"• 止损: {sl_text}\n\n"
                    "是否继续添加交易对?",
                    reply_markup=InlineKeyboardMarkup(keyboard))
                    
                clear_user_state(user_id)
                return
                
            except ValueError:
                await update.message.reply_text("请输入有效的数字")
                return
                
        elif state == "auto_trade:remove_symbol":
            try:
                # 检查是否为空输入
                if not text.strip():
                    # 重新发送提示
                    user_data = load_user_data(user_id)
                    symbols = user_data['auto_trading'].get('symbols', [])
                    
                    if not symbols:
                        await update.message.reply_text("当前没有自动交易对", reply_markup=reply_markup)
                        return
                        
                    # 显示交易对列表
                    symbols_list = []
                    for i, s in enumerate(symbols):
                        symbols_list.append(
                            f"{i+1}. {s['symbol']} 杠杆: {s['leverage']}x 金额: ${s['amount']} 止盈: {s['tp']}% 止损: {s['sl']}%"
                        )
                    
                    await update.message.reply_text(
                        f"请选择要删除的交易对:\n" + "\n".join(symbols_list) + "\n\n请输入编号:",
                        reply_markup=back_markup)
                    return
                    
                idx = int(text) - 1
                user_data = load_user_data(user_id)
                
                # 确保auto_trading和symbols字段存在
                if 'auto_trading' not in user_data or 'symbols' not in user_data['auto_trading']:
                    await update.message.reply_text("当前没有自动交易对", reply_markup=reply_markup)
                    return
                    
                symbols = user_data['auto_trading']['symbols']
                
                if 0 <= idx < len(symbols):
                    removed = symbols.pop(idx)
                    save_user_data(user_id, user_data)
                    
                    # 检查是否还有交易对
                    if not user_data['auto_trading']['symbols']:
                        await update.message.reply_text(
                            f"✅ 已删除自动交易对: {removed['symbol']}\n\n所有自动交易对已删除",
                            reply_markup=reply_markup)
                        clear_user_state(user_id)
                        return
                    
                    # 显示剩余交易对
                    symbols_list = []
                    for i, s in enumerate(user_data['auto_trading']['symbols']):
                        symbols_list.append(
                            f"{i+1}. {s['symbol']} 杠杆: {s['leverage']}x 金额: ${s['amount']} 止盈: {s['tp']}% 止损: {s['sl']}%"
                        )
                    
                    # 只发送一条合并的消息
                    await update.message.reply_text(
                        f"✅ 已删除自动交易对: {removed['symbol']}\n\n"
                        f"剩余自动交易对:\n" + "\n".join(symbols_list) + "\n\n请输入编号继续删除或输入'取消'返回主菜单:",
                        reply_markup=back_markup)
                else:
                    await update.message.reply_text("无效的编号，请重新输入")
                    return
                    
            except ValueError:
                # 处理无效输入
                await update.message.reply_text("请输入有效的编号")
                # 重新显示剩余交易对
                user_data = load_user_data(user_id)
                symbols = user_data['auto_trading'].get('symbols', [])
                if symbols:
                    symbols_list = []
                    for i, s in enumerate(symbols):
                        symbols_list.append(
                            f"{i+1}. {s['symbol']} 杠杆: {s['leverage']}x 金额: ${s['amount']} 止盈: {s['tp']}% 止损: {s['sl']}%"
                        )
                    
                    await update.message.reply_text(
                        f"剩余自动交易对:\n" + "\n".join(symbols_list) + "\n\n请输入编号:",
                        reply_markup=back_markup)
                return
                
        elif state == "auto_trade:set_api_key":
            api_key = text.strip()
            set_user_state(user_id, "auto_trade:set_api_secret", {"key": api_key})
            # 明确提示输入密钥（Secret Key）
            await update.message.reply_text(
                "请输入您的Binance 密钥（Secret Key）:",
                reply_markup=back_markup)
            return
                
        elif state == "auto_trade:set_api_secret":
            api_secret = text.strip()
            state_info = get_user_state(user_id)
            config = state_info.get("data", {})
            config["secret"] = api_secret
            
            # 保存API密钥
            user_data = load_user_data(user_id)
            user_data['binance_api'] = config
            save_user_data(user_id, user_data)
            
            await update.message.reply_text(
                "✅ API密钥设置成功\n请注意：请确保该API密钥具有交易权限（尤其是合约交易权限），并且IP白名单已设置（如果需要）",
                reply_markup=reply_markup)
            clear_user_state(user_id)
            return
        
        # 处理主菜单命令（仅在无状态时处理）
        if text in ["1️⃣ 添加监控币种", "1"] and not state:
            await add_symbol(update, context)
        elif text in ["2️⃣ 删除监控币种", "2"] and not state:
            await remove_symbol(update, context)
        elif text in ["3️⃣ 开启监控", "3"] and not state:
            await enable_monitoring(update, context)
        elif text in ["4️⃣ 关闭监控", "4"] and not state:
            await disable_monitoring(update, context)
        elif text in ["5️⃣ 自动交易设置", "5"] and not state:
            await auto_trading_settings(update, context)
        elif text in ["6️⃣ 启动/关闭自动交易", "6"] and not state:
            await toggle_auto_trading(update, context)
        elif text in ["7️⃣ 查看状态", "7"] and not state:
            await show_status(update, context)
        elif text in ["8️⃣ 帮助", "8"] and not state:
            await show_help(update, context)
        else:
            # 检查是否在删除状态下
            if state == "auto_trade:remove_symbol":
                # 重新发送提示
                user_data = load_user_data(user_id)
                symbols = user_data['auto_trading'].get('symbols', [])
                
                if symbols:
                    symbols_list = []
                    for i, s in enumerate(symbols):
                        symbols_list.append(
                            f"{i+1}. {s['symbol']} 杠杆: {s['leverage']}x 金额: ${s['amount']} 止盈: {s['tp']}% 止损: {s['sl']}%"
                        )
                    
                    await update.message.reply_text(
                        f"请选择要删除的交易对:\n" + "\n".join(symbols_list) + "\n\n请输入编号:",
                        reply_markup=back_markup)
                else:
                    await update.message.reply_text("当前没有自动交易对", reply_markup=reply_markup)
                    clear_user_state(user_id)
            else:
                await update.message.reply_text("无法识别的命令，请使用菜单操作", reply_markup=reply_markup)
            
    except Exception as e:
        logger.error(f"消息处理出错: {e}", exc_info=True)
        await update.message.reply_text("处理您的消息时出错，请重试")

# 错误处理器
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("处理更新时出现异常", exc_info=context.error)
    if update and isinstance(update, Update) and update.message:
        await update.message.reply_text("处理您的请求时出错，请稍后再试")

# 在应用启动后启动监控任务
async def on_startup(application):
    await start_monitor(application)
    logger.info("应用初始化完成，监控任务已启动")

# 在应用停止时停止监控任务
async def on_shutdown(application):
    await stop_monitor()
    logger.info("应用已停止")

# --- 主程序 ---
if __name__ == "__main__":
    # 创建应用实例，并设置启动和停止回调
    application = (
        Application.builder()
        .token(TOKEN)
        .post_init(on_startup)  # 启动回调
        .post_shutdown(on_shutdown)  # 关闭回调
        .build()
    )
    
    # 添加错误处理器
    application.add_error_handler(error_handler)
    
    # 添加命令处理器
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # 添加快捷命令
    application.add_handler(MessageHandler(filters.Regex(r'^1️⃣ 添加监控币种$|^1$'), add_symbol))
    application.add_handler(MessageHandler(filters.Regex(r'^2️⃣ 删除监控币种$|^2$'), remove_symbol))
    application.add_handler(MessageHandler(filters.Regex(r'^3️⃣ 开启监控$|^3$'), enable_monitoring))
    application.add_handler(MessageHandler(filters.Regex(r'^4️⃣ 关闭监控$|^4$'), disable_monitoring))
    application.add_handler(MessageHandler(filters.Regex(r'^5️⃣ 自动交易设置$|^5$'), auto_trading_settings))
    application.add_handler(MessageHandler(filters.Regex(r'^6️⃣ 启动/关闭自动交易$|^6$'), toggle_auto_trading))
    application.add_handler(MessageHandler(filters.Regex(r'^7️⃣ 查看状态$|^7$'), show_status))
    application.add_handler(MessageHandler(filters.Regex(r'^8️⃣ 帮助$|^8$'), show_help))
    
    logger.info("机器人已启动")
    application.run_polling()
