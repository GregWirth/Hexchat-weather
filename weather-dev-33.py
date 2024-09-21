import asyncio
import aiohttp
import logging
import time
import random
import signal
import json
import sys
from collections import defaultdict, deque
from contextlib import suppress
from urllib.parse import quote
import argparse
import logging.handlers
from cachetools import TTLCache
import re

# Configure logging with adjustable levels and log rotation
parser = argparse.ArgumentParser(description='IRC Weather Bot')
parser.add_argument('--log-level', default='INFO', help='Set the logging level (DEBUG, INFO, WARNING, ERROR)')
parser.add_argument('--config', default='config.json', help='Path to the configuration file')  # Allow custom config file
args = parser.parse_args()

logger = logging.getLogger('IrcBot')
logger.setLevel(getattr(logging, args.log_level.upper()))
handler = logging.handlers.TimedRotatingFileHandler('bot.log', when='midnight', backupCount=7)
formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# Load configuration from config.json or specified config file
with open(args.config, 'r') as config_file:
    config = json.load(config_file)

# Validate required configurations
REQUIRED_CONFIG_KEYS = ['HOST', 'PORT', 'USER', 'CHANNELS', 'API_KEY', 'USERNAME', 'PASSWORD']
for key in REQUIRED_CONFIG_KEYS:
    if key not in config:
        raise ValueError(f"Missing required configuration: {key}")

HOST = config['HOST']
PORT = config['PORT']
USER = config['USER']
CHANNELS = config['CHANNELS']
TRIGGER = config['TRIGGER']
RATE_LIMIT = config['RATE_LIMIT']
RATE_LIMIT_TIME = config['RATE_LIMIT_TIME']
IGNORE_TIME = config['IGNORE_TIME']
WAREZ_TRIGGER = config['WAREZ_TRIGGER']
WAREZ_FILE = config['WAREZ_FILE']
PING_INTERVAL = config['PING_INTERVAL']
PING_TIMEOUT = config['PING_TIMEOUT']

API_KEY = config['API_KEY']
ADMIN_USERS = config.get('ADMIN_USERS', [])
USERNAME = config['USERNAME']
PASSWORD = config['PASSWORD']

def sanitize_input(user_input):
    """Sanitize user input to prevent command injection and control characters."""
    sanitized = user_input.replace('\r', '').replace('\n', '').replace('\0', '')
    sanitized = re.sub(r'[\x00-\x1F\x7F]', '', sanitized)
    return sanitized.strip()

class IrcBot:
    """An IRC bot that provides weather information and responds to specific triggers."""

    def __init__(self):
        self.last_requests = defaultdict(lambda: deque(maxlen=RATE_LIMIT))
        self.global_request_times = deque(maxlen=RATE_LIMIT)
        self.warez_responder = WarezResponder(WAREZ_FILE)
        self.last_pong_time = time.time()
        self.reader = None
        self.writer = None
        self.lock = asyncio.Lock()
        self.reader_lock = asyncio.Lock()  # Lock for reading from the connection
        self.tasks = []
        self.weather_cache = TTLCache(maxsize=100, ttl=300)
        self.running = True
        self.message_semaphore = asyncio.Semaphore(1)  # Limit to 1 message at a time
        self.current_nick = USER  # Track the current nickname
        self.pending_channels = set()  # Channels that are pending to be joined

    async def connect(self):
        """Establish a connection to the IRC server and register the bot."""
        try:
            self.reader, self.writer = await asyncio.open_connection(HOST, PORT)
            await self.register()
            logger.info(f"Connected to IRC server as {self.current_nick}.")
            await self.wait_for_registration()
            await self.join_channels()
        except asyncio.TimeoutError:
            logger.error(f"Timed out while trying to connect to IRC server at {HOST}:{PORT}")
            raise
        except Exception as e:
            logger.error(f"Failed to connect to IRC: {e}")
            raise

    async def register(self):
        """Register the bot with the IRC server and authenticate."""
        if self.writer is None:
            logger.error("Cannot register, no active connection.")
            return

        self.current_nick = USER
        self.writer.write(f"NICK {self.current_nick}\r\n".encode())
        self.writer.write(f"USER {USER} 0 * :{USER}\r\n".encode())
        await self.writer.drain()
        logger.info("Sent NICK and USER commands.")

    async def wait_for_registration(self):
        """Wait for the server's response to nickname registration."""
        while True:
            async with self.reader_lock:
                line = await self.reader.readline()
            if not line:
                raise ConnectionError("Connection lost.")
            line = line.decode('utf-8', errors='ignore').strip()
            prefix, command, params = self.parse_irc_message(line)
            if command == '001':
                logger.info(f"Nickname {self.current_nick} accepted by server.")
                break
            elif command == '433':
                logger.warning(f"Nickname {self.current_nick} is already in use.")
                await self.handle_nickname_in_use()
                break
            elif command == 'NOTICE':
                notice_message = params[-1]
                if 'registered' in notice_message.lower() and 'identify' in notice_message.lower():
                    logger.info("Server requires identification. Sending IDENTIFY command.")
                    await self.authenticate()
            else:
                await self.process_line(line)

    async def handle_nickname_in_use(self):
        """Handle situation when the nickname is already in use by using GHOST command."""
        if self.writer is None:
            logger.warning("Cannot reclaim nickname, no active connection.")
            return

        logger.info(f"Attempting to reclaim nickname {USER} using NickServ GHOST command.")
        self.writer.write(f"PRIVMSG NickServ :GHOST {USER} {PASSWORD}\r\n".encode())
        await self.writer.drain()
        await asyncio.sleep(2)
        self.writer.write(f"NICK {USER}\r\n".encode())
        await self.writer.drain()
        self.current_nick = USER
        await self.wait_for_registration()

    async def authenticate(self):
        """Authenticate the bot with NickServ."""
        if self.writer is None:
            logger.error("Cannot authenticate, no active connection.")
            return

        auth_message = f"PRIVMSG NickServ :IDENTIFY {USERNAME} {PASSWORD}\r\n"
        self.writer.write(auth_message.encode())
        await self.writer.drain()
        logger.info(f"Sent NickServ IDENTIFY for user: {USERNAME}")

    async def join_channels(self):
        """Send JOIN commands for all channels and track join confirmations."""
        if self.writer is None:
            logger.error("Cannot join channels, no active connection.")
            return

        self.pending_channels = set(CHANNELS)  # Keep track of channels to join

        for channel in CHANNELS:
            logger.info(f"Joining channel {channel}")
            self.writer.write(f"JOIN {channel}\r\n".encode())
            await self.writer.drain()
            await asyncio.sleep(1)  # Add a small delay between JOIN commands

    async def ping_chanserv(self):
        """Ping ChanServ every 10 minutes and reconnect if no response within 5 minutes (total)."""
        while self.running:
            if self.writer is None:
                logger.warning("Cannot send PING to ChanServ, no active connection.")
                await self.reconnect()  # Reconnect if there is no active connection
                continue
            try:
                self.last_ping_time = time.time()  # Track the last time we pinged ChanServ
                logger.info("Sending PING to ChanServ")
                self.writer.write(f"PING ChanServ\r\n".encode())
                await self.writer.drain()

                # Wait for response for 3 minutes
                await asyncio.wait_for(self.ping_response_wait("ChanServ"), timeout=180)

                logger.info("Received PONG from ChanServ")
                await asyncio.sleep(600)  # Wait for 10 minutes before the next ping

            except asyncio.TimeoutError:
                logger.warning("No response from ChanServ after 3 minutes. Retrying...")
                
                # Send another ping after 3 minutes of no response
                if self.writer is None:
                    logger.error("No connection to send PING on retry.")
                    await self.reconnect()
                    break

                self.writer.write(f"PING ChanServ\r\n".encode())
                await self.writer.drain()

                # Wait for another 2 minutes for response
                try:
                    await asyncio.wait_for(self.ping_response_wait("ChanServ"), timeout=120)
                    logger.info("Received PONG from ChanServ on second attempt.")
                    await asyncio.sleep(600)  # Wait another 10 minutes before next ping
                except asyncio.TimeoutError:
                    logger.error("No response from ChanServ after second attempt. Reconnecting...")
                    await self.reconnect()  # Reconnect to the server if no response after retry
                    break
            except Exception as e:
                logger.error(f"Error during ping to ChanServ: {e}")
                await self.reconnect()  # Reconnect in case of any unexpected error
                break

    async def ping_response_wait(self, target):
        """Wait for a PONG response from the specified target."""
        self.ping_event = asyncio.Event()  # Reset event before each wait
        await self.ping_event.wait()  # Wait until we receive the PONG from ChanServ

    async def handle_messages(self):
        """Handle incoming messages from the IRC server."""
        while self.running:
            try:
                async with self.reader_lock:
                    line = await self.reader.readline()
                if not line:
                    raise ConnectionError("Connection lost: received empty response.")
                line = line.decode('utf-8', errors='ignore').strip()
                logger.debug(f"Received line: {line}")
                await self.process_line(line)
            except Exception as e:
                logger.error(f"Error in message handling: {e}")
                await self.reconnect()
                break

    async def process_line(self, line):
        """Process a single line from the IRC server."""
        prefix, command, params = self.parse_irc_message(line)

        if command == 'PING':
            await self.handle_ping(params)
        elif command == 'PONG':
            if params[0] == "ChanServ":
                logger.info("Received PONG from ChanServ")
                self.last_pong_time = time.time()  # Update the last time we received PONG from ChanServ
                self.ping_event.set()  # Notify the ping wait task that PONG was received
            else:
                logger.debug(f"Unhandled PONG from {params[0]}")
        elif command == '366':
            await self.handle_join_confirmation(params)
        elif command == 'PRIVMSG':
            await self.handle_privmsg(prefix, params)
        elif command == 'ERROR':
            logger.error(f"Server error: {' '.join(params)}")
            await self.reconnect()
        else:
            logger.debug(f"Unhandled message: {line}")

    def parse_irc_message(self, message):
        """Parse an IRC message into its prefix, command, and parameters."""
        prefix = ''
        trailing = []
        if not message:
            return None, None, None
        if message.startswith(':'):
            prefix, message = message[1:].split(' ', 1)
        if ' :' in message:
            message, trailing = message.split(' :', 1)
            args = message.split()
            args.append(trailing)
        else:
            args = message.split()
        command = args.pop(0)
        return prefix, command, args

    async def handle_ping(self, params):
        """Respond to server PING messages."""
        self.writer.write(f"PONG :{params[0]}\r\n".encode())
        await self.writer.drain()
        self.last_pong_time = time.time()
        logger.debug("Responded to PING with PONG.")

    async def handle_join_confirmation(self, params):
        """Handle the server's confirmation of joining a channel."""
        channel = params[1]
        if channel in self.pending_channels:
            self.pending_channels.remove(channel)
            logger.info(f"Successfully joined channel {channel}")
        else:
            logger.debug(f"Received unexpected join confirmation for {channel}")

    async def handle_privmsg(self, prefix, params):
        """Handle PRIVMSG commands."""
        user = prefix.split('!')[0]
        channel = params[0]
        message = params[1]

        # Ignore messages from the bot itself to prevent infinite loops
        if user == self.current_nick:
            logger.debug("Received a message from self; ignoring to prevent loops.")
            return

        if channel == self.current_nick:
            # This is a private message to the bot
            await self.handle_private_message(user, message)
        else:
            # Message in a channel
            if message.startswith(TRIGGER):
                location = message[len(TRIGGER):].strip()
                location = sanitize_input(location)  # Sanitize the location input
                await self.handle_weather_command(user, channel, location)
            elif WAREZ_TRIGGER in message:
                await self.handle_warez_command(channel)

    async def handle_private_message(self, user, message):
        """Handle private messages sent to the bot."""
        # Normalize the nickname for case-insensitive comparison
        normalized_user = user.lower()
        admin_users_normalized = [admin.lower() for admin in ADMIN_USERS]

        if normalized_user in admin_users_normalized:
            if message.startswith('.say '):
                # Extract channel and message
                try:
                    parts = message.split(' ', 2)
                    if len(parts) < 3:
                        raise ValueError("Invalid command format.")
                    _, channel, say_message = parts

                    # Automatically add '#' prefix if missing
                    if not channel.startswith(('#', '&')):
                        channel = f'#{channel}'

                    await self.send_message(channel, say_message)
                    logger.info(f"Admin {user} made the bot say in {channel}: {say_message}")
                except ValueError:
                    await self.send_message(user, "Usage: .say <channel> <message>")
            else:
                await self.send_message(user, "Unknown command.")
        else:
            await self.send_message(user, "You do not have permission to use this command.")

    async def handle_weather_command(self, user, channel, location):
        """Process the weather command and send weather information."""
        current_time = time.time()
        async with self.lock:
            # Global rate limit
            while self.global_request_times and current_time - self.global_request_times[0] > RATE_LIMIT_TIME:
                self.global_request_times.popleft()
            if len(self.global_request_times) >= RATE_LIMIT:
                remaining_time = RATE_LIMIT_TIME - (current_time - self.global_request_times[0])
                warning_msg = f"The bot is rate-limited. Try again in {int(remaining_time)} seconds."
                await self.send_message(channel, warning_msg)
                return
            self.global_request_times.append(current_time)

            # Per-user rate limit
            request_times = self.last_requests[user]
            while request_times and current_time - request_times[0] > RATE_LIMIT_TIME:
                request_times.popleft()
            if len(request_times) >= RATE_LIMIT:
                remaining_time = RATE_LIMIT_TIME - (current_time - request_times[0])
                warning_msg = f"You are being rate-limited, {user}. Try again in {int(remaining_time)} seconds."
                await self.send_message(channel, warning_msg)
                return
            request_times.append(current_time)

        await self.fetch_and_send_weather(channel, location, user)

    async def fetch_and_send_weather(self, channel, location, user):
        """Fetch weather data and send it to the channel."""
        cache_key = location.lower()
        if cache_key in self.weather_cache:
            data = self.weather_cache[cache_key]
            logger.info(f"Using cached weather data for {location}.")
        else:
            try:
                url = f"https://api.weatherapi.com/v1/forecast.json?key={API_KEY}&q={quote(location)}&days=1&aqi=no&alerts=no"
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            raise aiohttp.ClientError(f"HTTP error {resp.status}")
                        data = await resp.json()
                self.weather_cache[cache_key] = data
            except asyncio.TimeoutError:
                logger.error(f"Weather API request for {location} timed out.")
                error_msg = "Weather API request timed out."
                await self.send_message(channel, error_msg)
                return
            except Exception as e:
                logger.error(f"Error fetching weather data for {location}: {e}")
                error_msg = f"Error fetching weather information for {location}."
                await self.send_message(channel, error_msg)
                return

        # Extract and format the weather data
        try:
            location_info = data['location']
            current = data['current']
            forecast_day = data['forecast']['forecastday'][0]
            forecast = forecast_day['day']
            astro = forecast_day['astro']

            # Extract all the required fields
            name = location_info['name']
            region = location_info['region']
            country = location_info['country'].replace("United States of America", "USA")
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

            await self.send_message(channel, weather_message)
            logger.info(f"Sent weather info to {channel} for location '{location}' requested by user '{user}'.")
        except KeyError as e:
            logger.error(f"Missing expected data in API response for {location}: {e}")
            error_msg = "Received unexpected data from weather API."
            await self.send_message(channel, error_msg)
        except Exception as e:
            logger.error(f"Error processing weather data for {location}: {e}")
            error_msg = f"Error processing weather information for {location}."
            await self.send_message(channel, error_msg)

    async def handle_warez_command(self, channel):
        """Respond to the warez trigger."""
        response = self.warez_responder.get_random_response()
        await self.send_message(channel, response)
        logger.info(f"Sent warez response to {channel}.")

    async def send_message(self, channel, message):
        """Send a message to the IRC channel, splitting if too long."""
        max_length = 400  # Reserve space for protocol overhead
        message = sanitize_input(message)
        logger.debug(f"Attempting to send message to {channel}: {message}")
        async with self.message_semaphore:
            try:
                while len(message) > max_length:
                    part = message[:max_length]
                    self.writer.write(f"PRIVMSG {channel} :{part}\r\n".encode())
                    await self.writer.drain()
                    logger.debug(f"Sent message chunk to {channel}: {part}")
                    message = message[max_length:]
                    await asyncio.sleep(1)  # Delay to prevent flooding
                self.writer.write(f"PRIVMSG {channel} :{message}\r\n".encode())
                await self.writer.drain()
                logger.debug(f"Sent message to {channel}: {message}")
            except Exception as e:
                logger.error(f"Failed to send message to {channel}: {e}")

    async def run(self):
        """Run the bot."""
        self.tasks.append(asyncio.create_task(self.ping_chanserv()))  # Start pinging ChanServ

        while self.running:
            try:
                await self.connect()
                await self.handle_messages()
            except Exception as e:
                logger.error(f"Unhandled exception: {e}")
                await self.reconnect()

    async def reconnect(self):
        """Attempt to reconnect to the IRC server with indefinite retries and exponential backoff."""
        retry_delay = 10  # Start with a 10-second delay
        retries = 0
        MAX_BACKOFF = 300  # Cap the retry delay to 5 minutes
        while self.running:
            try:
                logger.info(f"Attempting to reconnect as {self.current_nick} (Attempt {retries + 1})...")
                await self.connect()
                await self.handle_messages()
                break  # Exit the loop if reconnection is successful
            except Exception as e:
                logger.error(f"Reconnection attempt failed: {e}")
                retries += 1
                logger.info(f"Retrying in {retry_delay} seconds (Attempt {retries})...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, MAX_BACKOFF)  # Exponential backoff

    async def cleanup(self):
        """Clean up resources on shutdown."""
        self.running = False
        for task in self.tasks:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if self.writer:
            try:
                self.writer.write("QUIT :Shutting down...\r\n".encode())
                await self.writer.drain()
            except ConnectionResetError:
                logger.error("Failed to send QUIT command. Connection was already closed.")
            finally:
                self.writer.close()
                await self.writer.wait_closed()
        logger.info("Cleaned up resources.")

class WarezResponder:
    """Responds with random messages from a predefined list when triggered."""

    def __init__(self, file_path):
        try:
            with open(file_path, 'r') as file:
                self.responses = [line.strip() for line in file if line.strip()]
        except FileNotFoundError:
            self.responses = ["No warez responses available."]

    def get_random_response(self):
        """Get a random response from the list."""
        return random.choice(self.responses) if self.responses else "No warez responses available."

if __name__ == "__main__":
    bot = IrcBot()

    async def main():
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.cleanup()))
            logger.info(f"Signal handler set for {sig}")
        await bot.run()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot shut down gracefully.")
        sys.exit(0)

