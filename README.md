# Hexchat-weather
IRC weather plugin for Hechat IRC client
Create file in directory main script resides named: insult-list.txt
Populate this file with random text to output to channel.
This is a timed event set by editing: INSULT_INTERVAL = 22 * 60 * 60  # 22 hours in seconds
Create file in directory main script resides named: api_key.txt
This contains your API key.
This script uses: https://www.weatherapi.com/
You can sign up for a free account.
Script uses #zz as a trigger.
Usage: #zz {zip code}
Usage: #zz {place name}
Returns weather information for zip codes or place names.
Can use USA and Canada zip codes.
