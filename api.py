import aiohttp
import asyncio
from typing import Optional, Union

base_url_mexc = "https://contract.mexc.com"
base_url_dex = "https://api.dexscreener.com"

async def get_mexc_future_price(session, symbol):
    """Получение последней цены фьючерса с MEXC по символу."""
    url = f"{base_url_mexc}/api/v1/contract/ticker"

    try:
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.json()
                for s in data.get("data", []):
                    if s.get("symbol") == symbol:
                        last_price = s.get("lastPrice")
                        if last_price is not None:
                            return float(last_price)
                        else:
                            print(f"Поле 'lastPrice' отсутствует для {symbol}.")
                            return None
                print(f"Символ {symbol} не найден.")
            else:
                print(f"Ошибка запроса (MEXC): {response.status}, {await response.text()}")
    except Exception as e:
        print(f"Ошибка при получении данных с MEXC: {e}")

    return None

async def get_dex_price(session, net_token, token_address):
    """Получение цены токена с Dexscreener по адресу токена."""
    url = f"{base_url_dex}/latest/dex/pairs/{net_token}/{token_address}"

    try:
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.json()
                pairs = data.get("pairs")
                if pairs and pairs[0].get("priceUsd"):
                    return float(pairs[0]["priceUsd"])
                else:
                    print(f"Пара с адресом {token_address} не найдена или нет цены.")
            else:
                print(f"Ошибка запроса (Dexscreener): {response.status}, {await response.text()}")
    except Exception as e:
        print(f"Ошибка при получении данных с Dexscreener: {e}")

    return None

class TelegramNotifier:
    def __init__(self, token: str, chat_ids: list[int]):
        self.token = token
        self.chat_ids = chat_ids
        self.base_tg_url = f"https://api.telegram.org/bot{self.token}"
        self.send_text_endpoint = "/sendMessage"
        self.send_photo_endpoint = "/sendPhoto"
        self.delete_msg_endpoint = "/deleteMessage"

    async def _schedule_delete(self, chat_id: int, message_id: int, delay: Union[int, float]):
        await asyncio.sleep(delay)
        url = f"{self.base_tg_url}{self.delete_msg_endpoint}"
        params = {
            "chat_id": chat_id,
            "message_id": message_id
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=params) as resp:
                    if resp.status != 200:
                        print(f"Ошибка удаления сообщения: {await resp.text()}")
        except Exception as e:
            print(f"Ошибка при удалении сообщения: {e}")

    async def send(self, text: str, photo_bytes: bytes = None, auto_delete: Optional[Union[int, float]] = None):
        async with aiohttp.ClientSession() as session:
            for chat_id in self.chat_ids:
                if photo_bytes:
                    url = self.base_tg_url + self.send_photo_endpoint
                    data = aiohttp.FormData()
                    data.add_field("chat_id", str(chat_id))
                    data.add_field("caption", text)
                    data.add_field("parse_mode", "HTML")
                    data.add_field("disable_web_page_preview", "true")
                    data.add_field("disable_notification", "false")
                    data.add_field("photo", photo_bytes, filename="spread.png", content_type="image/png")
                else:
                    url = self.base_tg_url + self.send_text_endpoint
                    data = {
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                        "disable_notification": bool(auto_delete)
                    }

                try:
                    async with session.post(url, data=data) as resp:
                        if resp.status != 200:
                            print(f"Ошибка отправки сообщения: {await resp.text()}")
                            continue
                        response_json = await resp.json()
                        message_id = response_json.get("result", {}).get("message_id")

                        # Планируем удаление, если указано время
                        if auto_delete and message_id:
                            asyncio.create_task(self._schedule_delete(chat_id, message_id, auto_delete))
                except Exception as e:
                    print(f"Ошибка при запросе Telegram API: {e}")