import asyncio
import aiohttp
import logging
import time
import random
from collections import defaultdict, deque
from urllib.parse import quote

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Read API_KEY from file
try:
    with open("api_key.txt", "r") as key_file:
        API_KEY = key_file.read().strip()
except FileNotFoundError:
    logger.error("API key file not found.")
    exit(1)

HOST = "irc.server"
PORT = 6667
USER = "user-nick"
CHANNELS = ["#channel01", "#channel02"]
TRIGGER = "#zz"
RATE_LIMIT = 7
RATE_LIMIT_TIME = 60
IGNORE_TIME = 180
WAREZ_TRIGGER = "!warez"
WAREZ_FILE = "warez-trigger.txt"

# Ping and reconnect configuration
PING_INTERVAL = 5 * 60  # 5 minutes in seconds
PING_TIMEOUT = 3 * 60   # 3 minutes in seconds

class IrcBot:
    def __init__(self):
        self.last_requests = defaultdict(lambda: deque(maxlen=RATE_LIMIT))
        self.warez_responder = WarezResponder(WAREZ_FILE)
        self.last_pong_time = time.time()
        self.reader = None
        self.writer = None

    async def connect(self):
        try:
            self.reader, self.writer = await asyncio.open_connection(HOST, PORT)
            await self.register()
            await self.join_channels()
            logger.info("Connected to IRC server.")
        except Exception as e:
            logger.error(f"Failed to connect to IRC: {e}")
            exit(1)

    async def register(self):
        self.writer.write(f"NICK {USER}\r\n".encode())
        self.writer.write(f"USER {USER} 0 * :{USER}\r\n".encode())
        await self.writer.drain()

    async def join_channels(self):
        for channel in CHANNELS:
            self.writer.write(f"JOIN {channel}\r\n".encode())
            await self.writer.drain()

    async def ping_server(self):
        try:
            while True:
                self.writer.write(f"PING :{HOST}\r\n".encode())
                await self.writer.drain()
                await asyncio.sleep(PING_INTERVAL)
        except Exception as e:
            logger.error(f"Ping failed: {e}")

    async def handle_messages(self):
        while True:
            try:
                line = await self.reader.readline()
                if not line:
                    logger.warning("Connection closed by the server.")
                    break

                line = line.decode('utf-8', errors='ignore').strip()
                logger.info(line)
                await self.process_line(line)
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                break

    async def process_line(self, line):
        if line.startswith("PING"):
            await self.handle_ping(line)
        elif "PRIVMSG" in line:
            await self.handle_privmsg(line)
        else:
            pass  # Handle other message types if needed

    async def handle_ping(self, line):
        self.writer.write(f"PONG :{HOST}\r\n".encode())
        await self.writer.drain()
        self.last_pong_time = time.time()
        logger.info("Responded to PING with PONG.")

    async def handle_privmsg(self, line):
        parts = line.split()
        if len(parts) < 4:
            return  # Not enough parts to process
        prefix = parts[0]
        command = parts[1]
        channel = parts[2]
        message = ' '.join(parts[3:])[1:]
        user = prefix.split('!')[0][1:]

        if message.startswith(TRIGGER):
            location = message[len(TRIGGER):].strip()
            await self.handle_weather_command(user, channel, location)
        elif WAREZ_TRIGGER in message:
            await self.handle_warez_command(channel)

    async def handle_weather_command(self, user, channel, location):
        current_time = time.time()
        request_times = self.last_requests[user]

        # Remove old requests outside the rate limit window
        while request_times and current_time - request_times[0] > RATE_LIMIT_TIME:
            request_times.popleft()

        if len(request_times) >= RATE_LIMIT:
            warning_msg = f"PRIVMSG {channel} :You are being rate limited, {user}.\r\n"
            self.writer.write(warning_msg.encode())
            await self.writer.drain()
            logger.warning(f"Rate limit exceeded for user {user}.")
            return

        request_times.append(current_time)
        await self.fetch_and_send_weather(channel, location)

    async def fetch_and_send_weather(self, channel, location):
        try:
            url = f"https://api.weatherapi.com/v1/forecast.json?key={API_KEY}&q={quote(location)}&days=1&aqi=no&alerts=no"
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        raise aiohttp.ClientError(f"HTTP error {resp.status}")
                    data = await resp.json()

            location_info = data['location']
            current = data['current']
            forecast_day = data['forecast']['forecastday'][0]
            forecast = forecast_day['day']
            astro = forecast_day['astro']

            # Extract all the required fields
            name = location_info['name']
            region = location_info['region']
            country = location_info['country']
            temp_f = current['temp_f']
            temp_c = current['temp_c']
            condition_text = current['condition']['text']
            wind_mph = current['wind_mph']
            wind_kph = current['wind_kph']
            wind_degree = current['wind_degree']
            wind_dir = current['wind_dir']
            gust_mph = current['gust_mph']
            gust_kph = current['gust_kph']
            precip_mm = current['precip_mm']
            precip_in = current['precip_in']
            humidity = current['humidity']

            mintemp_f = forecast['mintemp_f']
            mintemp_c = forecast['mintemp_c']
            maxtemp_f = forecast['maxtemp_f']
            maxtemp_c = forecast['maxtemp_c']
            maxwind_mph = forecast['maxwind_mph']
            maxwind_kph = forecast['maxwind_kph']
            daily_chance_of_rain = forecast.get('daily_chance_of_rain', 0)
            daily_chance_of_snow = forecast.get('daily_chance_of_snow', 0)
            totalsnow_cm = forecast.get('totalsnow_cm', 0)
            try:
                totalsnow_cm = float(totalsnow_cm)
            except (ValueError, TypeError):
                totalsnow_cm = 0.0

            if totalsnow_cm > 0:
                totalsnow_in = totalsnow_cm / 2.54  # Convert cm to inches
                snow_message = f"Total Snow: {totalsnow_cm} cm / {totalsnow_in:.2f} in"
            else:
                snow_message = "Total Snow: 0 cm / 0 in"

            moon_phase = astro['moon_phase']
            sunrise = astro['sunrise']
            sunset = astro['sunset']

            weather_message = (
                f"{name}, {region}, {country} | "
                f"Current Temp: {temp_f}°F / {temp_c}°C | "
                f"Min Temp: {mintemp_f}°F / {mintemp_c}°C | "
                f"Max Temp: {maxtemp_f}°F / {maxtemp_c}°C | "
                f"Condition: {condition_text} | "
                f"Humidity: {humidity}% | "
                f"Wind: {wind_mph} mph / {wind_kph} kph "
                f"({wind_degree}°, {wind_dir}) | "
                f"Gusts: {gust_mph} mph / {gust_kph} kph | "
                f"Precipitation: {precip_in} in / {precip_mm} mm | "
                f"Moon Phase: {moon_phase} | "
                f"Sunrise: {sunrise} | Sunset: {sunset} | "
                f"Chance of Rain: {daily_chance_of_rain}% | "
                f"Chance of Snow: {daily_chance_of_snow}% | "
                f"{snow_message}"
            )

            self.writer.write(f"PRIVMSG {channel} :{weather_message}\r\n".encode())
            await self.writer.drain()
            logger.info(f"Sent weather info to {channel}.")

        except Exception as e:
            logger.error(f"Error fetching weather data: {e}")
            error_msg = f"PRIVMSG {channel} :Error fetching weather information.\r\n"
            self.writer.write(error_msg.encode())
            await self.writer.drain()

    async def handle_warez_command(self, channel):
        response = self.warez_responder.get_random_response()
        self.writer.write(f"PRIVMSG {channel} :{response}\r\n".encode())
        await self.writer.drain()
        logger.info(f"Sent warez response to {channel}.")

    async def run(self):
        await self.connect()
        asyncio.create_task(self.ping_server())
        await self.handle_messages()

class WarezResponder:
    def __init__(self, file_path):
        try:
            with open(file_path, 'r') as file:
                self.responses = [line.strip() for line in file if line.strip()]
        except FileNotFoundError:
            self.responses = ["No warez responses available."]

    def get_random_response(self):
        return random.choice(self.responses) if self.responses else "No warez responses available."

if __name__ == "__main__":
    bot = IrcBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Bot shut down gracefully.")

