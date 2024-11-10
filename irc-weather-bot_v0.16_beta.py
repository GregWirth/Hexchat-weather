import asyncio
import aiohttp
import logging
import time
import random
import signal
import json
import sys
import os
from collections import defaultdict, deque
from urllib.parse import quote
import argparse
import logging.handlers
from cachetools import TTLCache
import re
from contextlib import suppress

# Configure logging with adjustable levels and log rotation
parser = argparse.ArgumentParser(description='IRC Weather Bot')
parser.add_argument('--log-level', default='INFO', help='Set the logging level (DEBUG, INFO, WARNING, ERROR)')
parser.add_argument('--config', default='config.json', help='Path to the configuration file')
args = parser.parse_args()

logger = logging.getLogger('IrcBot')
logger.setLevel(getattr(logging, args.log_level.upper()))
handler = logging.handlers.TimedRotatingFileHandler('bot.log', when='midnight', backupCount=7)
formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# Load configuration from config.json or specified config file
try:
    with open(args.config, 'r') as config_file:
        config = json.load(config_file)
except FileNotFoundError:
    logger.error(f"Configuration file {args.config} not found.")
    sys.exit(1)
except json.JSONDecodeError as e:
    logger.error(f"Error parsing configuration file: {e}")
    sys.exit(1)

# Validate required configurations
REQUIRED_CONFIG_KEYS = [
    'HOST', 'PORT', 'USER', 'CHANNELS', 'API_KEY', 'USERNAME', 'PASSWORD',
    'TRIGGER', 'RATE_LIMIT', 'RATE_LIMIT_TIME', 'GLOBAL_RATE_LIMIT', 'GLOBAL_RATE_LIMIT_TIME',
    'IGNORE_TIME', 'WAREZ_TRIGGER', 'WAREZ_FILE', 'PING_INTERVAL', 'PING_TIMEOUT',
    'STAB_TRIGGER', 'STAB_FILE'
]
for key in REQUIRED_CONFIG_KEYS:
    if key not in config:
        logger.error(f"Missing required configuration: {key}")
        sys.exit(1)

HOST = config['HOST']
PORT = config['PORT']
USER = config['USER']
CHANNELS = config['CHANNELS']
TRIGGER = config['TRIGGER']
RATE_LIMIT = config['RATE_LIMIT']
RATE_LIMIT_TIME = config['RATE_LIMIT_TIME']
GLOBAL_RATE_LIMIT = config['GLOBAL_RATE_LIMIT']
GLOBAL_RATE_LIMIT_TIME = config['GLOBAL_RATE_LIMIT_TIME']
IGNORE_TIME = config['IGNORE_TIME']
WAREZ_TRIGGER = config['WAREZ_TRIGGER']
WAREZ_FILE = config['WAREZ_FILE']
PING_INTERVAL = config['PING_INTERVAL']
PING_TIMEOUT = config['PING_TIMEOUT']

API_KEY = config['API_KEY']
ADMIN_USERS = config.get('ADMIN_USERS', [])
USERNAME = config['USERNAME']
PASSWORD = config['PASSWORD']

STAB_TRIGGER = config['STAB_TRIGGER']
STAB_FILE = config['STAB_FILE']

def sanitize_input(user_input):
    """Sanitize user input to prevent command injection and control characters."""
    sanitized = user_input.replace('\r', '').replace('\n', '').replace('\0', '')
    sanitized = re.sub(r'[\x00-\x1F\x7F]', '', sanitized)
    sanitized = sanitized.strip()
    # Limit input length to prevent flooding
    if len(sanitized) > 400:
        sanitized = sanitized[:400]
    # Allow necessary punctuation and symbols
    sanitized = re.sub(r'[^\w\s,.\-:|°%/()"]', '', sanitized)
    return sanitized

class ReconnectNeeded(Exception):
    """Custom exception to signal that a reconnection is needed."""
    pass

class WarezResponder:
    """Responds with random messages from a predefined list when triggered."""

    def __init__(self, file_path):
        try:
            with open(file_path, 'r') as file:
                self.responses = [line.strip() for line in file if line.strip()]
        except FileNotFoundError:
            logger.error(f"Warez file {file_path} not found.")
            self.responses = ["No warez responses available."]
        except Exception as e:
            logger.error(f"Error loading warez responses: {e}")
            self.responses = ["No warez responses available."]

    def get_random_response(self):
        """Get a random response from the list."""
        return random.choice(self.responses) if self.responses else "No warez responses available."

class StabResponder:
    """Responds with random messages from a predefined list when triggered,
    ensuring all lines are used before repeating. Dynamically reloads the file when it changes."""

    def __init__(self, file_path):
        self.file_path = file_path
        self.last_modified_time = None
        self.responses = []
        self.available_responses = []
        self.load_responses()

    def load_responses(self):
        """Load responses from the file and update the last modified time."""
        try:
            current_modified_time = os.path.getmtime(self.file_path)
            if self.last_modified_time != current_modified_time:
                with open(self.file_path, 'r') as file:
                    self.responses = [line.strip() for line in file if line.strip()]
                self.last_modified_time = current_modified_time
                logger.info(f"Reloaded responses from {self.file_path}.")
                # Reset available_responses to start a new cycle with updated responses
                self.available_responses = []
        except FileNotFoundError:
            logger.error(f"Stab file {self.file_path} not found.")
            self.responses = ["No stab responses available."]
            self.available_responses = []
        except Exception as e:
            logger.error(f"Error loading stab responses: {e}")
            self.responses = ["No stab responses available."]
            self.available_responses = []

    def get_random_response(self):
        """Get a random response from the list without repeating until all have been used."""
        # Check if the file has been modified and reload if necessary
        self.load_responses()

        if not self.responses:
            return "No stab responses available."

        if not self.available_responses:
            self.available_responses = self.responses.copy()
            random.shuffle(self.available_responses)
            logger.debug("Shuffled stab responses for a new cycle.")

        response = self.available_responses.pop()
        return response

class IrcBot:
    """An IRC bot that provides weather information and responds to specific triggers."""

    def __init__(self):
        self.last_requests = defaultdict(lambda: deque(maxlen=RATE_LIMIT))
        self.global_request_times = deque(maxlen=GLOBAL_RATE_LIMIT)
        self.warez_responder = WarezResponder(WAREZ_FILE)
        self.stab_responder = StabResponder(STAB_FILE)
        self.last_pong_time = time.time()
        self.reader = None
        self.writer = None
        self.lock = asyncio.Lock()
        self.reader_lock = asyncio.Lock()
        self.writer_lock = asyncio.Lock()
        self.tasks = []
        self.weather_cache = TTLCache(maxsize=100, ttl=300)
        self.running = True
        self.message_semaphore = asyncio.Semaphore(1)
        self.current_nick = USER
        self.pending_channels = set()
        self.reconnect_lock = asyncio.Lock()
        self.connection_established = asyncio.Event()
        self.authenticated = asyncio.Event()

    async def connect(self):
        """Establish a connection to the IRC server and register the bot."""
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(HOST, PORT), timeout=30)
            await self.register()
            logger.info(f"Connected to IRC server as {self.current_nick}.")
            await self.wait_for_registration()
            # Authenticate after MOTD is fully received
            await self.authenticate()
            auth_success = await self.wait_for_nickserv_response("You are now identified for")
            if not auth_success:
                logger.error("Authentication failed. Exiting.")
                await self.cleanup()
                sys.exit(1)
            logger.info("Authenticated with NickServ successfully.")
            await self.join_channels()
            self.connection_established.set()
        except Exception as e:
            logger.error(f"Failed to connect to IRC: {e}")
            raise

    async def register(self):
        """Register the bot with the IRC server."""
        if self.writer is None:
            logger.error("Cannot register, no active connection.")
            return

        async with self.writer_lock:
            self.current_nick = USER
            self.writer.write(f"NICK {self.current_nick}\r\n".encode('utf-8'))
            self.writer.write(f"USER {USERNAME} 0 * :{USERNAME}\r\n".encode('utf-8'))
            await self.writer.drain()
        logger.info("Sent NICK and USER commands.")

    async def read_line_with_timeout(self, timeout=300):
        """Read a line from the server with a timeout and reader lock."""
        try:
            async with self.reader_lock:
                line = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
            if not line:
                return None
            return line.decode('utf-8', errors='replace').strip()
        except asyncio.TimeoutError:
            logger.error("Timed out reading from server.")
            raise
        except Exception as e:
            logger.error(f"Error reading from server: {e}")
            raise

    async def wait_for_registration(self):
        """Wait for the server's response to nickname registration."""
        while True:
            try:
                line = await self.read_line_with_timeout()
                if line is None:
                    logger.error("Connection lost during registration.")
                    raise ReconnectNeeded()
                logger.debug(f"Received line during registration: {line}")
                prefix, command, params = self.parse_irc_message(line)
                if command == '001':
                    logger.info(f"Received welcome message from server.")
                    # Continue waiting for end of MOTD
                elif command == '376' or command == '422':
                    logger.info("End of MOTD received.")
                    break  # Now we can proceed
                elif command == '433':
                    logger.warning(f"Nickname {self.current_nick} is already in use.")
                    await self.handle_nickname_in_use()
                    break
                elif command == '451':
                    logger.error("Received ERR_NOTREGISTERED: You have not registered.")
                    await self.register()
                elif command == 'PING':
                    await self.handle_ping(params)
                else:
                    logger.debug(f"Ignoring message during registration: {line}")
            except ConnectionError as e:
                logger.error(f"Connection error during registration: {e}")
                raise ReconnectNeeded()
            except asyncio.TimeoutError:
                logger.error("Timed out waiting for registration response.")
                raise ReconnectNeeded()
            except Exception as e:
                logger.error(f"Error during registration: {e}")
                raise ReconnectNeeded()

    def parse_irc_message(self, message):
        """Parse an IRC message into its prefix, command, and parameters."""
        try:
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
        except ValueError as e:
            logger.error(f"Failed to parse IRC message: {message} Error: {e}")
            return None, None, None

    async def handle_ping(self, params):
        """Respond to server PING messages."""
        if self.writer is None:
            logger.error("Cannot respond to PING, no active connection.")
            return
        async with self.writer_lock:
            self.writer.write(f"PONG :{params[0]}\r\n".encode('utf-8'))
            await self.writer.drain()
        self.last_pong_time = time.time()
        logger.debug("Responded to PING with PONG.")

    async def reconnect(self):
        """Attempt to reconnect to the IRC server with retries and exponential backoff."""
        async with self.reconnect_lock:
            retry_delay = 10
            retries = 0
            MAX_BACKOFF = 300
            logger.info("Attempting to reconnect to IRC server...")
            await self.close_connection()
            while self.running:
                try:
                    logger.info(f"Reconnecting as {self.current_nick} (Attempt {retries + 1})...")
                    await self.connect()
                    break
                except Exception as e:
                    logger.exception(f"Reconnection attempt failed: {e}")
                    retries += 1
                    if retries >= 10:
                        logger.error("Exceeded maximum reconnection attempts. Exiting.")
                        await self.cleanup()
                        sys.exit(1)
                    logger.info(f"Retrying in {retry_delay} seconds (Attempt {retries})...")
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, MAX_BACKOFF)

    async def close_connection(self):
        """Close the existing IRC connection."""
        self.connection_established.clear()
        if self.writer:
            try:
                async with self.writer_lock:
                    self.writer.write("QUIT :Reconnecting...\r\n".encode('utf-8'))
                    await self.writer.drain()
            except Exception as e:
                logger.error(f"Error sending QUIT command: {e}")
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception as e:
                logger.error(f"Error closing writer: {e}")
            self.writer = None
            self.reader = None

    async def join_channels(self):
        """Send JOIN commands for all channels."""
        if self.writer is None:
            logger.error("Cannot join channels, no active connection.")
            return

        self.pending_channels = set(CHANNELS)

        for channel in CHANNELS:
            logger.info(f"Joining channel {channel}")
            async with self.writer_lock:
                self.writer.write(f"JOIN {channel}\r\n".encode('utf-8'))
                await self.writer.drain()
            await asyncio.sleep(1)

    async def handle_privmsg(self, prefix, params):
        """Handle PRIVMSG commands."""
        try:
            user = prefix.split('!')[0]
            channel = params[0]
            message = params[1].strip()
            message_lower = message.lower()

            if user.lower() == self.current_nick.lower():
                logger.debug("Received a message from self; ignoring to prevent loops.")
                return

            # Check for CTCP messages
            if message.startswith('\x01') and message.endswith('\x01'):
                ctcp_command = message.strip('\x01')
                if ctcp_command.upper() == 'VERSION':
                    await self.handle_ctcp_version(user)
                else:
                    logger.debug(f"Received unsupported CTCP command from {user}: {ctcp_command}")
                return

            if channel == self.current_nick:
                await self.handle_private_message(user, message)
            else:
                if message_lower.startswith(TRIGGER.lower()):
                    args = message[len(TRIGGER):].strip()
                    args = sanitize_input(args)
                    forecast = False
                    if '--forecast' in args:
                        args = args.replace('--forecast', '').strip()
                        forecast = True
                    await self.handle_weather_command(user, channel, args, forecast)
                elif WAREZ_TRIGGER.lower() in message_lower:
                    await self.handle_warez_command(channel)
                elif message_lower.startswith(STAB_TRIGGER.lower()):
                    # Extract username after the trigger
                    stab_target = message[len(STAB_TRIGGER):].strip()
                    if stab_target:
                        stab_target = sanitize_input(stab_target)
                        await self.handle_stab_command(channel, stab_target)
        except Exception as e:
            logger.exception(f"Exception in handle_privmsg: {e}")

    async def handle_ctcp_version(self, user):
        """Respond to a CTCP VERSION request."""
        version_reply = "Atari 800 MOS 6502 @ 1.8MHz"
        ctcp_response = f"\x01VERSION {version_reply}\x01"
        await self.send_notice(user, ctcp_response)
        logger.info(f"Responded to CTCP VERSION request from {user}.")

    async def send_notice(self, target, message):
        """Send a NOTICE to the specified target."""
        if self.writer is None:
            logger.error("Cannot send NOTICE, no active connection.")
            return
        async with self.writer_lock:
            self.writer.write(f"NOTICE {target} :{message}\r\n".encode('utf-8'))
            await self.writer.drain()
        logger.debug(f"Sent NOTICE to {target}: {message}")

    async def handle_stab_command(self, channel, target_user):
        """Respond to the stab trigger with the specified target user."""
        response_line = self.stab_responder.get_random_response()
        message = f"hftb stabs {target_user} {response_line}"
        await self.send_message(channel, message)
        logger.info(f"Sent stab response to {channel} targeting {target_user}.")

    async def handle_private_message(self, user, message):
        """Handle private messages sent to the bot."""
        response = "I'm currently not set up to handle private messages."
        await self.send_message(user, response)
        logger.info(f"Sent private message response to {user}.")

    async def handle_weather_command(self, user, channel, location, forecast=False):
        """Process the weather command and send weather information."""
        current_time = time.time()
        async with self.lock:
            # Global rate limit
            while self.global_request_times and current_time - self.global_request_times[0] > GLOBAL_RATE_LIMIT_TIME:
                self.global_request_times.popleft()
            if len(self.global_request_times) >= GLOBAL_RATE_LIMIT:
                remaining_time = GLOBAL_RATE_LIMIT_TIME - (current_time - self.global_request_times[0])
                warning_msg = f"The bot is currently handling many requests. Please try again in {int(remaining_time)} seconds."
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

        await self.fetch_and_send_weather(channel, location, user, forecast)

    async def fetch_and_send_weather(self, channel, location, user, forecast=False):
        """Fetch weather data and send it to the channel."""
        try:
            cache_key = f"{location.lower()}_{forecast}"
            if cache_key in self.weather_cache:
                data = self.weather_cache[cache_key]
                logger.info(f"Using cached weather data for {location}.")
            else:
                days = 2 if forecast else 1
                url = f"https://api.weatherapi.com/v1/forecast.json?key={API_KEY}&q={quote(location)}&days={days}&aqi=no&alerts=no"
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                        elif resp.status == 401:
                            logger.error("Unauthorized access. Check your API key.")
                            error_msg = "Unauthorized access to weather API. Please check the API key."
                            await self.send_message(channel, error_msg)
                            return
                        elif resp.status == 404:
                            logger.error(f"Location '{location}' not found.")
                            error_msg = f"Location '{location}' not found."
                            await self.send_message(channel, error_msg)
                            return
                        else:
                            logger.error(f"HTTP error {resp.status} when fetching weather data for {location}.")
                            error_msg = f"Error fetching weather information for {location}."
                            await self.send_message(channel, error_msg)
                            return
                self.weather_cache[cache_key] = data

            # Extract and format the weather data
            weather_message = self.format_weather_data(data, forecast)
            await self.send_message(channel, weather_message)
            logger.info(f"Sent weather info to {channel} for location '{location}' requested by user '{user}'.")
        except asyncio.TimeoutError:
            logger.error(f"Weather API request for {location} timed out.")
            error_msg = "Weather API request timed out."
            await self.send_message(channel, error_msg)
        except aiohttp.ClientError as e:
            logger.error(f"HTTP error when fetching weather data for {location}: {e}")
            error_msg = f"Error fetching weather information for {location}."
            await self.send_message(channel, error_msg)
        except KeyError as e:
            logger.error(f"Missing expected data in API response for {location}: {e}")
            error_msg = "Received unexpected data from weather API."
            await self.send_message(channel, error_msg)
        except Exception as e:
            logger.exception(f"Exception in fetch_and_send_weather: {e}")
            error_msg = f"Error processing weather information for {location}."
            await self.send_message(channel, error_msg)

    def format_weather_data(self, data, forecast=False):
        """Format the weather data into a message string."""
        try:
            location_info = data.get('location', {})
            current = data.get('current', {})
            forecast_days = data.get('forecast', {}).get('forecastday', [])

            # Extract location details
            name = location_info.get('name', 'Unknown')
            region = location_info.get('region', '')
            country = location_info.get('country', '').replace("United States of America", "USA")

            if forecast:
                # Format forecast for each day
                messages = []
                for day_data in forecast_days:
                    date = day_data.get('date', 'N/A')
                    day = day_data.get('day', {})
                    condition = day.get('condition', {}).get('text', 'N/A')
                    avgtemp_f = day.get('avgtemp_f', 'N/A')
                    avgtemp_c = day.get('avgtemp_c', 'N/A')
                    maxtemp_f = day.get('maxtemp_f', 'N/A')
                    maxtemp_c = day.get('maxtemp_c', 'N/A')
                    mintemp_f = day.get('mintemp_f', 'N/A')
                    mintemp_c = day.get('mintemp_c', 'N/A')
                    daily_chance_of_rain = day.get('daily_chance_of_rain', 'N/A')
                    daily_chance_of_snow = day.get('daily_chance_of_snow', 'N/A')
                    totalsnow_cm = day.get('totalsnow_cm', 0.0)
                    try:
                        totalsnow_cm = float(totalsnow_cm)
                    except (ValueError, TypeError):
                        totalsnow_cm = 0.0
                    totalsnow_in = totalsnow_cm / 2.54 if totalsnow_cm > 0 else 0.0
                    snow_message = f"Total Snow: {totalsnow_cm} cm / {totalsnow_in:.2f} in"

                    message = (
                        f"Forecast for {date} | "
                        f"Condition: {condition} | "
                        f"Avg Temp: {avgtemp_f}°F / {avgtemp_c}°C | "
                        f"Min Temp: {mintemp_f}°F / {mintemp_c}°C | "
                        f"Max Temp: {maxtemp_f}°F / {maxtemp_c}°C | "
                        f"Chance of Rain: {daily_chance_of_rain}% | "
                        f"Chance of Snow: {daily_chance_of_snow}% | "
                        f"{snow_message}"
                    )
                    messages.append(message)
                weather_message = f"{name}, {region}, {country} | " + ' | '.join(messages)
            else:
                # Extract current weather details
                temp_f = current.get('temp_f', 'N/A')
                temp_c = current.get('temp_c', 'N/A')
                condition_text = current.get('condition', {}).get('text', 'N/A')
                wind_mph = current.get('wind_mph', 'N/A')
                wind_kph = current.get('wind_kph', 'N/A')
                wind_degree = current.get('wind_degree', 'N/A')
                wind_dir = current.get('wind_dir', 'N/A')
                gust_mph = current.get('gust_mph', 'N/A')
                gust_kph = current.get('gust_kph', 'N/A')
                precip_mm = current.get('precip_mm', 'N/A')
                precip_in = current.get('precip_in', 'N/A')
                humidity = current.get('humidity', 'N/A')

                # Use the first forecast day for additional data
                forecast_day = forecast_days[0] if forecast_days else {}
                forecast_data = forecast_day.get('day', {})
                astro = forecast_day.get('astro', {})

                mintemp_f = forecast_data.get('mintemp_f', 'N/A')
                mintemp_c = forecast_data.get('mintemp_c', 'N/A')
                maxtemp_f = forecast_data.get('maxtemp_f', 'N/A')
                maxtemp_c = forecast_data.get('maxtemp_c', 'N/A')
                daily_chance_of_rain = forecast_data.get('daily_chance_of_rain', 0)
                daily_chance_of_snow = forecast_data.get('daily_chance_of_snow', 0)
                totalsnow_cm = forecast_data.get('totalsnow_cm', 0.0)
                try:
                    totalsnow_cm = float(totalsnow_cm)
                except (ValueError, TypeError):
                    totalsnow_cm = 0.0
                totalsnow_in = totalsnow_cm / 2.54 if totalsnow_cm > 0 else 0.0
                snow_message = f"Total Snow: {totalsnow_cm} cm / {totalsnow_in:.2f} in"

                moon_phase = astro.get('moon_phase', 'N/A')
                sunrise = astro.get('sunrise', 'N/A')
                sunset = astro.get('sunset', 'N/A')

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
            return weather_message
        except Exception as e:
            logger.exception(f"Exception in format_weather_data: {e}")
            return "Error formatting weather data."

    async def handle_warez_command(self, channel):
        """Respond to the warez trigger."""
        response = self.warez_responder.get_random_response()
        await self.send_message(channel, response)
        logger.info(f"Sent warez response to {channel}.")

    async def send_message(self, channel, message):
        """Send a message to the IRC channel, splitting if too long."""
        max_length = 512 - len(f"PRIVMSG {channel} :\r\n") - 2
        message = sanitize_input(message)
        logger.debug(f"Attempting to send message to {channel}: {message}")
        async with self.message_semaphore:
            try:
                while message:
                    part = message[:max_length]
                    await self.send_privmsg(channel, part)
                    logger.debug(f"Sent message chunk to {channel}: {part}")
                    message = message[max_length:]
                    await asyncio.sleep(1)
            except ConnectionResetError:
                logger.error(f"Connection reset while sending message to {channel}.")
                raise ReconnectNeeded()
            except Exception as e:
                logger.error(f"Failed to send message to {channel}: {e}")
                raise ReconnectNeeded()

    async def send_privmsg(self, target, message):
        """Send a PRIVMSG to the specified target."""
        if self.writer is None:
            logger.error("Cannot send message, no active connection.")
            return
        async with self.writer_lock:
            self.writer.write(f"PRIVMSG {target} :{message}\r\n".encode('utf-8'))
            await self.writer.drain()

    async def run(self):
        """Run the bot."""
        while self.running:
            try:
                logger.info("Starting connection to IRC server...")
                await self.connect()
                logger.info("Starting to handle messages...")
                await self.start_background_tasks()
                await self.handle_messages()
            except ReconnectNeeded:
                logger.info("Reconnect needed, reconnecting...")
                await self.cleanup_tasks()
                await self.reconnect()
            except Exception as e:
                logger.exception(f"Unhandled exception in run: {e}")
                await self.cleanup_tasks()
                await self.reconnect()

    async def start_background_tasks(self):
        """Start background tasks for connection monitoring."""
        self.tasks.append(asyncio.create_task(self.monitor_connection()))

    async def monitor_connection(self):
        """Monitor the connection and send PING messages as needed."""
        try:
            while self.running:
                await asyncio.sleep(PING_INTERVAL)
                if not self.running:
                    break
                current_time = time.time()
                if current_time - self.last_pong_time > PING_TIMEOUT:
                    logger.warning("No PONG received within timeout. Reconnecting...")
                    raise ReconnectNeeded()
                else:
                    await self.send_ping()
        except asyncio.CancelledError:
            logger.info("monitor_connection task cancelled.")
        except ReconnectNeeded:
            raise
        except Exception as e:
            logger.exception(f"Exception in monitor_connection: {e}")
            raise ReconnectNeeded()

    async def send_ping(self):
        """Send a PING message to the server."""
        if self.writer is None:
            logger.error("Cannot send PING, no active connection.")
            return
        async with self.writer_lock:
            self.writer.write(f"PING :{self.current_nick}\r\n".encode('utf-8'))
            await self.writer.drain()
        logger.debug("Sent PING to server.")

    async def cleanup_tasks(self):
        """Cancel all running tasks."""
        for task in self.tasks:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self.tasks.clear()

    async def handle_messages(self):
        """Handle incoming messages from the IRC server."""
        try:
            while self.running:
                try:
                    line = await self.read_line_with_timeout()
                    if line is None:
                        raise ConnectionError("Connection lost: received empty response.")
                    logger.debug(f"Received line: {line}")
                    await self.process_line(line)
                except asyncio.CancelledError:
                    logger.info("handle_messages task cancelled.")
                    raise
                except ConnectionError as e:
                    logger.error(f"Connection error in message handling: {e}")
                    raise ReconnectNeeded()
                except asyncio.TimeoutError:
                    logger.error("Timed out while waiting for messages.")
                    raise ReconnectNeeded()
                except Exception as e:
                    logger.exception(f"Unhandled exception in handle_messages: {e}")
                    raise ReconnectNeeded()
        except ReconnectNeeded:
            raise

    async def process_line(self, line):
        """Process a single line from the IRC server."""
        try:
            prefix, command, params = self.parse_irc_message(line)
            logger.debug(f"Prefix: {prefix}, Command: {command}, Params: {params}")

            if command == 'PING':
                await self.handle_ping(params)
            elif command == 'PONG':
                await self.handle_pong(params)
            elif command == 'NOTICE':
                await self.handle_notice(prefix, params)
            elif command == 'PRIVMSG':
                await self.handle_privmsg(prefix, params)
            elif command == 'ERROR':
                error_message = ' '.join(params)
                logger.error(f"Server error: {error_message}")
                if "closing link" in error_message.lower():
                    logger.warning("Possible netsplit detected. Attempting to reconnect...")
                    raise ReconnectNeeded()
                else:
                    raise ReconnectNeeded()
            elif command == 'KICK':
                await self.handle_kick(prefix, params)
            elif command == '433':
                logger.warning("Nickname is already in use.")
                await self.handle_nickname_in_use()
            else:
                logger.debug(f"Unhandled message: {line}")
        except Exception as e:
            logger.exception(f"Unhandled exception in process_line: {e}")

    async def handle_pong(self, params):
        """Handle PONG responses from the server."""
        logger.debug(f"Received PONG from {params[0]}")
        self.last_pong_time = time.time()

    async def handle_kick(self, prefix, params):
        """Handle being kicked from a channel."""
        channel = params[0]
        kicked_nick = params[1]
        if kicked_nick == self.current_nick:
            logger.warning(f"Kicked from {channel}. Attempting to rejoin...")
            await asyncio.sleep(5)
            await self.join_channels()

    async def handle_notice(self, prefix, params):
        """Handle NOTICE messages from the server."""
        sender_nick = prefix.split('!')[0]
        message = params[-1]
        logger.info(f"Received NOTICE from {sender_nick}: {message}")

        if sender_nick.lower() == 'nickserv':
            if 'identify' in message.lower() and 'registered' in message.lower():
                logger.info("NickServ is requesting identification. Sending IDENTIFY command.")
                await self.authenticate()
            elif 'you are now identified for' in message.lower():
                logger.info("Successfully identified with NickServ.")
                self.authenticated.set()
            elif 'invalid password' in message.lower():
                logger.error("Invalid password provided to NickServ.")
                await self.cleanup()
                sys.exit(1)

    async def handle_nickname_in_use(self):
        """Handle situation when the nickname is already in use."""
        if self.writer is None:
            logger.warning("Cannot reclaim nickname, no active connection.")
            return

        max_attempts = 5
        attempt = 0
        base_nick = USER

        while attempt < max_attempts:
            alternate_nick = f"{base_nick}_{attempt}"
            logger.info(f"Nickname {self.current_nick} is in use. Trying alternate nickname {alternate_nick}")
            async with self.writer_lock:
                self.writer.write(f"NICK {alternate_nick}\r\n".encode('utf-8'))
                await self.writer.drain()
            self.current_nick = alternate_nick

            # Wait for server acknowledgment
            result = await self.wait_for_nickname_response()
            if result == 'success':
                logger.info(f"Nickname changed to {self.current_nick}")
                break
            elif result == 'in_use':
                attempt += 1
                continue
            else:
                logger.error("Unexpected response when trying to change nickname.")
                attempt += 1

        if attempt >= max_attempts:
            logger.error("Failed to register after multiple attempts.")
            await self.cleanup()
            sys.exit(1)

        # Wait for registration to complete
        await self.wait_for_registration()

        # Authenticate with NickServ
        await self.authenticate()
        auth_success = await self.wait_for_nickserv_response("You are now identified for")
        if not auth_success:
            logger.error("Authentication failed after nickname change. Exiting.")
            await self.cleanup()
            sys.exit(1)

        # Attempt to GHOST the original nickname
        logger.info(f"Attempting to reclaim nickname {USER} using NickServ GHOST command.")
        await self.send_privmsg("NickServ", f"GHOST {USER} {PASSWORD}")

        # Wait for NickServ confirmation
        success = await self.wait_for_nickserv_response("has been ghosted")
        if success:
            logger.info(f"Successfully ghosted {USER}. Changing nickname back.")
            # Change back to the original nickname
            async with self.writer_lock:
                self.writer.write(f"NICK {USER}\r\n".encode('utf-8'))
                await self.writer.drain()
            self.current_nick = USER

            # Wait for the server to acknowledge the nickname change
            result = await self.wait_for_nickname_response()
            if result != 'success':
                logger.error(f"Failed to change back to original nickname {USER}. Continuing with {self.current_nick}")
            else:
                await self.wait_for_registration()
        else:
            logger.error("Failed to ghost the original nickname. Continuing with alternate nickname.")

    async def wait_for_nickname_response(self):
        """Wait for the server's response to the NICK command."""
        attempts = 0
        max_attempts = 10
        while attempts < max_attempts:
            attempts += 1
            try:
                line = await self.read_line_with_timeout(timeout=30)
                if line is None:
                    raise ConnectionError("Connection lost.")
                logger.debug(f"Received line during nickname change: {line}")
                prefix, command, params = self.parse_irc_message(line)
                if command == '001':
                    logger.info(f"Nickname {self.current_nick} accepted by server.")
                    return 'success'
                elif command == '433':
                    logger.warning(f"Nickname {self.current_nick} is already in use.")
                    return 'in_use'
                elif command == 'PING':
                    await self.handle_ping(params)
                else:
                    logger.debug(f"Unhandled message during nickname change: {line}")
            except Exception as e:
                logger.error(f"Error while waiting for nickname response: {e}")
                return 'error'
        logger.error("Maximum attempts reached while waiting for nickname response.")
        return 'error'

    async def wait_for_nickserv_response(self, expected_message):
        """Wait for a specific response from NickServ."""
        attempts = 0
        max_attempts = 20
        while attempts < max_attempts:
            attempts += 1
            try:
                line = await self.read_line_with_timeout(timeout=30)
                if line is None:
                    logger.error("Connection lost while waiting for NickServ response.")
                    return False
                logger.debug(f"Received line waiting for NickServ response: {line}")
                prefix, command, params = self.parse_irc_message(line)
                if command == 'NOTICE':
                    sender_nick = prefix.split('!')[0]
                    message = params[-1]
                    if sender_nick.lower() == 'nickserv' and expected_message.lower() in message.lower():
                        logger.info(f"Received expected NickServ message: {message}")
                        return True
                elif command == 'PING':
                    await self.handle_ping(params)
                else:
                    logger.debug(f"Unhandled message while waiting for NickServ response: {line}")
            except Exception as e:
                logger.error(f"Error while waiting for NickServ response: {e}")
                return False
        logger.error("Maximum attempts reached while waiting for NickServ response.")
        return False

    async def authenticate(self):
        """Authenticate the bot with NickServ."""
        if self.writer is None:
            logger.error("Cannot authenticate, no active connection.")
            return

        # Use the current nickname for identification
        await self.send_privmsg("NickServ", f"IDENTIFY {USERNAME} {PASSWORD}")
        logger.info(f"Sent NickServ IDENTIFY command for nick {self.current_nick}.")

    async def cleanup(self):
        """Clean up resources on shutdown."""
        self.running = False
        await self.cleanup_tasks()
        await self.close_connection()
        logger.info("Cleaned up resources.")

if __name__ == "__main__":
    bot = IrcBot()

    async def main():
        try:
            await bot.run()
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received. Shutting down...")
            if bot.running:
                await bot.cleanup()
        except Exception as e:
            logger.exception(f"Unhandled exception in main: {e}")
            if bot.running:
                await bot.cleanup()

    # Handle SIGTERM on Unix systems
    if hasattr(signal, 'SIGTERM'):
        def handle_sigterm(signum, frame):
            logger.info("SIGTERM received. Shutting down...")
            asyncio.create_task(bot.cleanup())

        signal.signal(signal.SIGTERM, handle_sigterm)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot shut down gracefully.")
        sys.exit(0)

