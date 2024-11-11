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
                    data = await self.fetch
