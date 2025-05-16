from api import get_dex_price, get_mexc_future_price, TelegramNotifier
import asyncio
import aiohttp
# import random
import pytz
from datetime import datetime, timezone
from collections import deque
from textwrap import dedent
import matplotlib.pyplot as plt
from scipy.interpolate import make_interp_spline
import numpy as np
import io
import os
import traceback

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
# BOT_TOKEN = "8174052403:AAFmaix3loe7-5GeafwNbc_GEJuGsefCnmo"
# CHAT_ID = "610822492"

SYMBOL_DATA = {
    "TIBBIR": ('base', '0x0c3b466104545efa096b8f944c1e524e1d0d4888')
}
SYMBOL = "TIBBIR"

# SETTINGS //////:

# Timing:
interval_map = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "10m": 600
}
tfr = "1m"
SPREAD_REFRESH_INTERVAL = interval_map[tfr]
PRICE_REFRESH_INTERVAL = 2

# Strayegy:
WINDOW=60 # minute
HIST_SPREAD_LIMIT = 10_000
DIRECTION_MODE = 3 # 1 -- Long only, 2 -- Short only, 3 -- Long + Short:
DEVIATION = 0.89 # hvh
FIXED_THRESHOLD = {
    "is_active": False,
    "val": 3.0 # %
}
EXIT_THRESHOLD=0.21
CALC_SPREAD_METHOD = 'a'

# Utils:
TMZ = "Europe/Kyiv"
PLOT_WINDOW = 60 # minute
SAVE_TO_FILE = False

# def generate_mock_spread_data(count=1440, spread_range=(-4, 1)) -> list[tuple[str, float]]:
#     now = datetime.now()
#     spread_data = []

#     for i in range(count):
#         time_label = (now - timedelta(seconds=(count - i) * 15)).strftime("%H:%M:%S")  # каждые 15 секунд
#         spread = round(random.uniform(*spread_range), 3)
#         spread_data.append((time_label, spread))
    
#     return spread_data

class SpreadPlotGenerator:
    def __init__(self):
        pass

    def generate_plot_image(self, now, spread_data: list[tuple[str, float]], style: int = 0) -> bytes:
        if (not spread_data) or (len(spread_data) < PLOT_WINDOW):
            return None

        spread_data = spread_data[-PLOT_WINDOW:]
        _, spreads = zip(*spread_data)

        plt.figure(figsize=(8, 4))
        plt.axhline(0, color='gray', linestyle='--', linewidth=1)

        if style == 0:
            plt.plot(spreads, marker='o', linestyle='-', color='blue')

        elif style == 1:
            x = np.arange(len(spreads))
            x_new = np.linspace(x.min(), x.max(), 300)
            spl = make_interp_spline(x, spreads, k=3)
            y_smooth = spl(x_new)
            plt.plot(x_new, y_smooth, color='green')

        elif style == 2:
            plt.bar(range(len(spreads)), spreads, color='purple')

        elif style == 3:
            plt.scatter(range(len(spreads)), spreads, color='orange')

        elif style == 4:
            plt.plot(spreads, color='red')
            plt.fill_between(range(len(spreads)), spreads, 0, alpha=0.3, color='red')

        else:
            raise ValueError("Недопустимый стиль. Используйте значение от 0 до 4.")

        plt.title("История Spread (%)")
        plt.ylabel("Spread %")
        plt.tight_layout()

        buffer = io.BytesIO()
        plt.savefig(buffer, format='png')

        # if SAVE_TO_FILE:
        #     plt.savefig(f"IMG/{now}.png")

        plt.close()
        buffer.seek(0)
        return buffer.read()

class TimeControl():
    def __init__(self):  
        self.tz_location = pytz.timezone(TMZ)
        self.last_fetch_timestamp = None
        self.bollinger_data = ()

    def get_date_time_now(self):
        now = datetime.now(self.tz_location)
        return now.strftime("%Y-%m-%d %H:%M:%S")

    def get_date_time_ms(self):
        now = datetime.now().astimezone(self.tz_location)
        return int(now.timestamp() * 1000)

    def is_new_spread_refresh_interval(self):
        now = datetime.now(timezone.utc)
        current_timestamp = int(now.timestamp())
        nearest_timestamp = (current_timestamp // SPREAD_REFRESH_INTERVAL) * SPREAD_REFRESH_INTERVAL

        if self.last_fetch_timestamp is None or nearest_timestamp > self.last_fetch_timestamp:
            self.last_fetch_timestamp = nearest_timestamp
            return True
        return False

class Indicators:
    @staticmethod
    def hvh_spread_calc(spread_pct_data, last_spread):
        """
        Простой HVH-индикатор для списка кортежей (timestamp, spread)
        - spread_pct_data: list of tuples (timestamp, spread_value)
        - WINDOW: количество последних значений для анализа
        - DEVIATION: множитель отклонения (например, 0.89)
        Returns: 1 (long), -1 (short), 0 (нейтрально)
        """
        if (not FIXED_THRESHOLD["is_active"]) and (len(spread_pct_data) < WINDOW):
            return 0

        if not FIXED_THRESHOLD["is_active"]:
            recent_spreads = [val for _, val in spread_pct_data[-WINDOW:]]
            last_positive = [val for val in recent_spreads if val > 0]
            last_negative = [val for val in recent_spreads if val < 0]
            # if not last_positive or not last_negative:
            #     return 0

            highest = max(last_positive, default=0) * DEVIATION
            lowest = min(last_negative, default=0) * DEVIATION
        else:
            highest = FIXED_THRESHOLD["val"]
            lowest = -FIXED_THRESHOLD["val"]

        if lowest != 0 and last_spread < lowest:
            return 1
        elif highest != 0 and last_spread > highest:
            return -1
        
        return 0

class SIGNAL:
    def __init__(self):
        self.indicators = Indicators()
        self.in_position_long = False 
        self.in_position_short = False
    
    @staticmethod
    def is_exit_signal(current_spread):
        return abs(current_spread) < EXIT_THRESHOLD

    def signals_collector(self, spread_pct_data, current_spread):
        instruction = []

        if self.is_exit_signal(current_spread):
            if self.in_position_long:
                instruction.append(("LONG", "is_closing"))
                self.in_position_long = False
                
            if self.in_position_short:                    
                instruction.append(("SHORT", "is_closing"))
                self.in_position_short = False

        signal = self.indicators.hvh_spread_calc(spread_pct_data, current_spread)
        if not signal:
            return instruction

        if not self.in_position_long:
            if signal == 1 and current_spread < 0:    
                self.in_position_long = DIRECTION_MODE in [1,3] 
                if self.in_position_long:              
                    instruction.append(("LONG", "is_opening"))                 

        if not self.in_position_short:
            if signal == -1 and current_spread > 0:     
                self.in_position_short = DIRECTION_MODE in [2,3]
                if self.in_position_short:          
                    instruction.append(("SHORT", "is_opening"))                    

        return instruction

class Main():
    def __init__(self):
        self.spread_pct = None 
        self.spread_pct_data = deque(maxlen=HIST_SPREAD_LIMIT)
        self.time_control = TimeControl()
        self.signals_inst = SIGNAL()
        self.notifier = TelegramNotifier(
            token=BOT_TOKEN,
            chat_ids=[CHAT_ID]  # твой chat_id или список chat_id'ов
        )
        self.spread_plotter = SpreadPlotGenerator()

    @staticmethod
    def format_signal_message(symbol, position_side, action, spread, mexc_price, dex_price, token_address, net_token, dt_str):
        if action == "is_opening":
            action_msg = "Открываем"
            emoji = "🟢" if position_side == "LONG" else "🔴"
        elif action == "is_closing":
            action_msg = "Закрываем"
            emoji = "🔒"
        else:
            action_msg = "Действие"
            emoji = "⚠️"

        return dedent(f"""\
            [{dt_str}] {emoji} [{symbol}][{action_msg}][{position_side}]
            ⚖️ Spread: {spread:.4f}%
            💲 MEXC Price: {mexc_price}
            💲 DEX Price: {dex_price}
            📊 MEXC: https://www.mexc.com/ru-RU/futures/{symbol}_USDT?type=linear_swap
            🧪 Dexscreener: https://dexscreener.com/{net_token}/{token_address}
        """)
    
    @staticmethod
    def calc_spread(price_a: float, price_b: float, method: str = 'a') -> float:
        """
        Универсальный расчёт спреда между двумя ценами.

        :param price_a: Первая цена (обычно MEXC или базовая).
        :param price_b: Вторая цена (обычно DEX или сравнительная).
        :param method: Метод расчёта:
            - 'a' — (a - b) / a * 100 (спред от price_a)
            - 'b' — (a - b) / b * 100 (спред от price_b)
            - 'ratio' — (a / b - 1) * 100 (относительный спред)
        :return: Спред в процентах
        """
        if method == 'a':
            return (price_a - price_b) / price_a * 100
        elif method == 'b':
            return (price_a - price_b) / price_b * 100
        elif method == 'ratio':
            return (price_a / price_b - 1) * 100
        else:
            raise ValueError(f"Unknown method '{method}'. Choose from 'a', 'b', or 'ratio'.")

    async def _run(self):
        refresh_counter = 0
        plot_raport_counter = 0
        first_iter = True
        mexc_price = None
        dex_price = None
        net_token = SYMBOL_DATA[SYMBOL][0]
        token_address = SYMBOL_DATA[SYMBOL][1]

        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    if refresh_counter >= PRICE_REFRESH_INTERVAL:
                        refresh_counter = 0
                        mexc_price = await get_mexc_future_price(session, SYMBOL + "_USDT")
                        dex_price = await get_dex_price(session, net_token, token_address)                        
                        self.spread_pct = self.calc_spread(mexc_price, dex_price, CALC_SPREAD_METHOD)
                        # print(self.spread_pct)  
                             
                    is_refresh_time = self.time_control.is_new_spread_refresh_interval()         

                    if is_refresh_time:
                        if self.spread_pct is not None:
                            now = self.time_control.get_date_time_now()
                            self.spread_pct_data.append((now, self.spread_pct))
                            msg = f"📢 [{now}][{SYMBOL}]:\nSpread: {self.spread_pct:.4f} %"                            
                            await self.notifier.send(msg, photo_bytes=None, auto_delete=120)                                

                    if not self.spread_pct_data:
                        continue

                    if is_refresh_time and not first_iter:                    
                        # self.spread_pct_data = generate_mock_spread_data() # test
                        plot_raport_counter = 0
                        dt_str = self.time_control.get_date_time_now()
                        plot_bytes = self.spread_plotter.generate_plot_image(dt_str, list(self.spread_pct_data), style=1) 
                        if plot_bytes:
                            await self.notifier.send(f"[{dt_str}][{SYMBOL}]:\n", photo_bytes=plot_bytes, auto_delete=120)
                        
                    instruction = self.signals_inst.signals_collector(list(self.spread_pct_data), self.spread_pct)
                    if not instruction:
                        continue
                    
                    dt_str = self.time_control.get_date_time_now()
                    plot_bytes = self.spread_plotter.generate_plot_image(dt_str, list(self.spread_pct_data), style=4)                    
                    for position_side, action in instruction:
                        if action in {"is_opening", "is_closing"}:              
                            msg = self.format_signal_message(
                                SYMBOL + "_USDT", position_side, action,
                                self.spread_pct, mexc_price, dex_price,
                                token_address, net_token, dt_str
                            )

                            print(msg)
                            await self.notifier.send(msg, photo_bytes=plot_bytes)

                except Exception as ex:
                    print(f"_run error: {ex}")
                    traceback.print_exc()

                finally:
                    refresh_counter += 1
                    plot_raport_counter += 1
                    if first_iter: first_iter = False
                    await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(Main()._run())