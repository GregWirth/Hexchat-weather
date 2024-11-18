import argparse
import json
import logging
import logging.handlers
import random
import re
import sys
import aiohttp
import asyncio
from urllib.parse import quote

# Command-line arguments
parser = argparse.ArgumentParser(description='IRC Weather Bot')
parser.add_argument('--log-level', default='INFO', help='Set the logging level (DEBUG, INFO, WARNING, ERROR)')
parser.add_argument('--config', default='config.json', help='Path to the configuration file')
args = parser.parse_args()

# Logging setup
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
        self.responses = []
        self.used_responses = set()
        self.load_responses()

    def load_responses(self):
        """Load responses from the file."""
        try:
            with open(self.file_path, 'r') as file:
                self.responses = [line.strip() for line in file if line.strip()]
                self.used_responses.clear()
        except FileNotFoundError:
            logger.error(f"Stab file {self.file_path} not found.")
            self.responses = ["No stab responses available."]
        except Exception as e:
            logger.error(f"Error loading stab responses: {e}")
            self.responses = ["No stab responses available."]

    def get_random_response(self):
        """Get a random response from the list, ensuring all lines are used before repeating."""
        if not self.responses:
            return "No stab responses available."
        available_responses = set(self.responses) - self.used_responses
        if not available_responses:
            self.used_responses.clear()
            available_responses = set(self.responses)
        response = random.choice(list(available_responses))
        self.used_responses.add(response)
        return response

class IrcBot:
    """An IRC bot that provides weather information and responds to specific triggers."""

    def __init__(self):
        self.reader = None
        self.writer = None
        self.weather_cache = {}
        self.last_requests = {}
        self.global_request_times = []
        self.ping_task = None
        self.ping_timeout_task = None
        self.warez_responder = WarezResponder(WAREZ_FILE)
        self.stab_responder = StabResponder(STAB_FILE)

    async def connect(self):
        """Connect to the IRC server."""
        while True:
            try:
                self.reader, self.writer = await asyncio.open_connection(HOST, PORT)
                logger.info(f"Connected to {HOST}:{PORT}")
                break
            except Exception as e:
                logger.error(f"Failed to connect to {HOST}:{PORT} - {e}")
                await asyncio.sleep(5)

    async def register(self):
        """Register the bot with the IRC server."""
        self.writer.write(f"NICK {USERNAME}\r\n".encode())
        self.writer.write(f"USER {USERNAME} 0 * :{USERNAME}\r\n".encode())
        await self.writer.drain()

    async def read_line_with_timeout(self, timeout=300):
        """Read a line from the server with a timeout."""
        try:
            return await asyncio.wait_for(self.reader.readline(), timeout=timeout)
        except asyncio.TimeoutError:
            raise ReconnectNeeded

    async def wait_for_registration(self):
        """Wait for the registration to complete."""
        while True:
            line = await self.read_line_with_timeout()
            if b' 001 ' in line:
                logger.info("Registration complete.")
                break

    def parse_irc_message(self, message):
        """Parse an IRC message into its components."""
        prefix = ''
        trailing = []
        if message[0] == ':':
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
        """Handle PING messages from the server."""
        response = f"PONG {params[0]}\r\n"
        self.writer.write(response.encode())
        await self.writer.drain()

    async def reconnect(self):
        """Reconnect to the server."""
        logger.info("Reconnecting...")
        if self.writer:
            self.writer.close()
        await self.connect()
        await self.register()
        await self.wait_for_registration()

    async def close_connection(self):
        """Close the connection to the server."""
        if self.writer:
            self.writer.close()
        if self.reader:
            await self.reader.wait_closed()

    async def join_channels(self):
        """Join the configured channels."""
        for channel in CHANNELS:
            self.writer.write(f"JOIN {channel}\r\n".encode())
            await self.writer.drain()

    async def handle_privmsg(self, prefix, params):
        """Handle PRIVMSG commands from the server."""
        user = prefix.split('!')[0]
        channel, message = params
        message = sanitize_input(message)
        if message.startswith(TRIGGER):
            command = message[len(TRIGGER):].strip().lower()
            if command.startswith('weather'):
                location = command.split(' ', 1)[1] if ' ' in command else ''
                await self.handle_weather_command(user, channel, location)
            elif command.startswith('warez'):
                await self.handle_warez_command(channel)
            elif command.startswith('stab'):
                target_user = command.split(' ', 1)[1] if ' ' in command else ''
                await self.handle_stab_command(channel, target_user)

    async def handle_ctcp_version(self, user):
        """Handle CTCP VERSION requests."""
        response = f"NOTICE {user} :\x01VERSION IRC Weather Bot\x01\r\n"
        self.writer.write(response.encode())
        await self.writer.drain()

    async def send_notice(self, target, message):
        """Send a NOTICE message to a user or channel."""
        notice_message = f"NOTICE {target} :{message}\r\n"
        self.writer.write(notice_message.encode())
        await self.writer.drain()

    async def handle_stab_command(self, channel, target_user):
        """Handle the stab command."""
        response = self.stab_responder.get_random_response()
        stab_message = f"{target_user}: {response}"
        await self.send_message(channel, stab_message)

    async def handle_private_message(self, user, message):
        """Handle private messages sent to the bot."""
        sanitized_message = sanitize_input(message)
        await self.send_notice(user, f"Received your message: {sanitized_message}")

    async def handle_weather_command(self, user, channel, location, forecast=False):
        """Handle weather commands."""
        if not location:
            await self.send_message(channel, "Please specify a location.")
            return

        current_time = asyncio.get_event_loop().time()
        if user not in self.last_requests:
            self.last_requests[user] = []
        request_times = self.last_requests[user]

        # Global rate limit
        while self.global_request_times and current_time - self.global_request_times[0] > GLOBAL_RATE_LIMIT_TIME:
            self.global_request_times.pop(0)
        if len(self.global_request_times) >= GLOBAL_RATE_LIMIT:
            remaining_time = GLOBAL_RATE_LIMIT_TIME - (current_time - self.global_request_times[0])
            warning_msg = f"The bot is currently handling many requests. Please try again in {int(remaining_time)} seconds."
            await self.send_message(channel, warning_msg)
            return
        self.global_request_times.append(current_time)

        # Per-user rate limit
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
            country = location_info.get('country', '')
            location_str = f"{name}, {region}, {country}".strip(', ')

            # Extract current weather details
            temp_c = current.get('temp_c', 'N/A')
            condition = current.get('condition', {}).get('text', 'N/A')
            wind_kph = current.get('wind_kph', 'N/A')
            humidity = current.get('humidity', 'N/A')

            message = (f"Weather for {location_str}: {temp_c}°C, {condition}, "
                       f"wind {wind_kph} kph, humidity {humidity}%.")

            if forecast and forecast_days:
                forecast_message = " Forecast: "
                for day in forecast_days:
                    date = day.get('date', 'N/A')
                    day_temp = day.get('day', {}).get('avgtemp_c', 'N/A')
                    day_condition = day.get('day', {}).get('condition', {}).get('text', 'N/A')
                    forecast_message += (f"{date}: {day_temp}°C, {day_condition}. ")
                message += forecast_message.strip()

            return message
        except Exception as e:
            logger.exception(f"Exception in format_weather_data: {e}")
            return "Error formatting weather data."

    async def handle_warez_command(self, channel):
        """Handle the warez command."""
        response = self.warez_responder.get_random_response()
        await self.send_message(channel, response)

    async def send_message(self, channel, message):
        """Send a message to a channel."""
        self.writer.write(f"PRIVMSG {channel} :{message}\r\n".encode())
        await self.writer.drain()

    async def send_privmsg(self, target, message):
        """Send a private message to a user."""
        self.writer.write(f"PRIVMSG {target} :{message}\r\n".encode())
        await self.writer.drain()

    async def run(self):
        """Run the bot."""
        await self.connect()
        await self.register()
        await self.wait_for_registration()
        await self.join_channels()
        await self.start_background_tasks()
        await self.handle_messages()

    async def start_background_tasks(self):
        """Start background tasks for the bot."""
        self.ping_task = asyncio.create_task(self.monitor_connection())
        self.ping_timeout_task = asyncio.create_task(self.send_ping())

    async def monitor_connection(self):
        """Monitor the connection and reconnect if needed."""
        while True:
            try:
                await self.read_line_with_timeout(PING_INTERVAL + PING_TIMEOUT)
            except ReconnectNeeded:
                await self.reconnect()

    async def send_ping(self):
        """Send PING messages to the server periodically."""
        while True:
            await asyncio.sleep(PING_INTERVAL)
            self.writer.write(f"PING {HOST}\r\n".encode())
            await self.writer.drain()
            try:
                await asyncio.wait_for(self.read_line_with_timeout(PING_TIMEOUT), timeout=PING_TIMEOUT)
            except asyncio.TimeoutError:
                raise ReconnectNeeded

    async def cleanup_tasks(self):
        """Clean up background tasks."""
        if self.ping_task:
            self.ping_task.cancel()
        if self.ping_timeout_task:
            self.ping_timeout_task.cancel()

    async def handle_messages(self):
        """Handle incoming messages from the server."""
        while True:
            try:
                line = await self.read_line_with_timeout()
                prefix, command, params = self.parse_irc_message(line.decode())
                if command == 'PING':
                    await self.handle_ping(params)
                elif command == 'PRIVMSG':
                    await self.handle_privmsg(prefix, params)
                elif command == 'NOTICE' and params[1].startswith('\x01VERSION'):
                    await self.handle_ctcp_version(prefix.split('!')[0])
            except ReconnectNeeded:
                await self.reconnect()

    async def process_line(self, line):
        """Process a line of input from the server."""
        prefix, command, params = self.parse_irc_message(line.decode())
        if command == 'PING':
            await self.handle_ping(params)
        elif command == 'PRIVMSG':
