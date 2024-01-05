import asyncio
import time
import xml.etree.ElementTree as ET
import chardet
import random
from collections import defaultdict
from urllib.parse import quote
import aiohttp
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Read API_KEY from file
with open("api_key.txt", "r") as key_file:
    API_KEY = key_file.read().strip()

HOST = "irc.server.example"
PORT = 6667
USER = "user-name"
CHANNELS = ["#channel01", "#channel02"]
TRIGGER = "#zz"
RATE_LIMIT = 7
RATE_LIMIT_TIME = 60
IGNORE_TIME = 180
INSULT_TRIGGER = "#insult"
INSULT_CHANNEL = "#channel01"
INSULTS_FILE = "insult-list.txt"
INSULT_INTERVAL = 22 * 60 * 60  # 22 hours in seconds

def sanitize(text):
    """
    Sanitize input text by escaping special characters and limiting length.
    """
    if not text:
        return ""
    # Limit the length to avoid excessively long input
    text = text[:100] 
    # Replace potentially dangerous characters
    return text.translate(str.maketrans({
        "\n": r"\n",
        "\r": r"\r",
        "\t": r"\t",
        "\0": r"\0",
        "\"": r"\"",
        "'": r"\'",
        "\\": r"\\"
    }))

async def connect_to_irc():
    reader, writer = await asyncio.open_connection(HOST, PORT) 
    
    nick_command = f"NICK {USER}\r\n"
    user_command = f"USER {USER} 0 * :{USER}\r\n"
    
    writer.write(nick_command.encode())
    await asyncio.sleep(1)  # Introduce a short delay
    writer.write(user_command.encode())

    for channel in CHANNELS:
        join_command = f"JOIN {channel}\r\n"
        writer.write(join_command.encode())

    return reader, writer

class InsultGenerator:
    def __init__(self, file_path):
        with open(file_path, 'r') as file:
            self.insults = [line.strip() for line in file]
        self.used_insults = []

    def get_random_insult(self):
        if not self.used_insults:
            self.used_insults = random.sample(self.insults, k=len(self.insults))

        if not self.used_insults:
            return "No insults available."

        insult = self.used_insults.pop()
        return insult

async def main():
    reader, writer = await connect_to_irc()

    last_requests = defaultdict(list)
    last_insult_time = time.time()
    insult_generator = InsultGenerator(INSULTS_FILE)

    while True:
        try:
            line = await reader.readuntil(b"\n")

            encoding = chardet.detect(line)['encoding']
            line = sanitize(line.decode(encoding, errors='replace').strip())
            logger.info(line)  # Log all server responses
            parts = line.split()

            if parts[0] == "PING":
                writer.write(f"PONG {parts[1]}\n".encode('utf-8'))

            elif len(parts) > 3 and parts[1] == "PRIVMSG":
                channel = parts[2]

                if parts[3] == f":{TRIGGER}":
                    user = sanitize(parts[0][1:].split('!')[0])
                    location = sanitize(" ".join(parts[4:]) if len(parts) >= 5 else None)
                    if len([t for t in last_requests[user] if time.time() - t < RATE_LIMIT_TIME]) > RATE_LIMIT:
                        writer.write(f"PRIVMSG {channel} :You are being rate limited.\n".encode('utf-8'))
                        continue

                    last_requests[user].append(time.time())
                    last_requests[user] = [t for t in last_requests[user] if time.time() - t < IGNORE_TIME]

                    try:
                        url = f"https://api.weatherapi.com/v1/forecast.xml?key={API_KEY}&q={quote(location)}"

                        async with aiohttp.ClientSession() as session:
                            async with session.get(url) as resp:
                                response = await resp.text()
                        root = ET.fromstring(response)

                        name = root.find(".//name").text
                        region = root.find(".//region").text
                        country = root.find(".//country").text
                        temp_f = root.find(".//temp_f").text
                        temp_c = root.find(".//temp_c").text
                        wind_mph = root.find('current/wind_mph').text
                        wind_kph = root.find('current/wind_kph').text
                        wind_degree = root.find('current/wind_degree').text
                        wind_dir = root.find('current/wind_dir').text
                        precip_mm = root.find('current/precip_mm').text
                        precip_in = root.find('current/precip_in').text
                        humidity = root.find('current/humidity').text
                        gust_mph = root.find('current/gust_mph').text
                        gust_kph = root.find('current/gust_kph').text
                        condition_text = root.find('current/condition/text').text
                        moon_phase = root.find(".//moon_phase").text
                        sunrise = root.find(".//sunrise").text
                        sunset = root.find(".//sunset").text
                        daily_chance_of_rain = root.find(".//daily_chance_of_rain").text
                        maxwind_mph = root.find(".//maxwind_mph").text
                        maxwind_kph = root.find(".//maxwind_kph").text
                        totalsnow_cm = root.find(".//totalsnow_cm").text

                        writer.write(f"PRIVMSG {channel} :{name}, {region}, {country} | {temp_f}°F / {temp_c}°C | {condition_text} | Humidity: {humidity}% | Wind Speed: {wind_mph}mph / {wind_kph}kph | Wind Direction: {wind_degree}° ({wind_dir}) | Gust Speed: {gust_mph}mph / {gust_kph}kph | Precipitation: {precip_in}in / {precip_mm}mm | Moon: {moon_phase} | Sunrise: {sunrise} | Sunset: {sunset} | Chance of Rain: {daily_chance_of_rain}% | Chance of Snow: {totalsnow_cm}%\n".encode('utf-8'))

                    except aiohttp.ClientError as e:
                        logger.error(f"Error in HTTP request: {e}")
                        writer.write(f"PRIVMSG {channel} :Error in fetching weather information.\n".encode('utf-8'))

                elif parts[3] == f":{INSULT_TRIGGER}" and len(parts) >= 5:
                    channel = parts[2]
                    insulted_user = sanitize(parts[4])
                    insult = insult_generator.get_random_insult()
                    writer.write(f"PRIVMSG {channel} :{insulted_user}, {insult}\n".encode('utf-8'))

                current_time = time.time()
                if current_time - last_insult_time > INSULT_INTERVAL:
                    insult = insult_generator.get_random_insult()
                    writer.write(f"PRIVMSG {INSULT_CHANNEL} :{insult}\n".encode('utf-8'))
                    last_insult_time = current_time

                elif len(parts) > 3 and parts[1] == "PRIVMSG" and parts[3].startswith(":\x01VERSION"):
                    sender = parts[0][1:].split('!')[0]
                    writer.write(f"NOTICE {sender} :\x01VERSION loldongs it's telnet\x01\n".encode('utf-8'))

        except Exception as e:
            logger.error(f"Unexpected error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
