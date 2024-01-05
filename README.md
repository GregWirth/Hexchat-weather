# IRC weather script<br><br>
Runs via telnet<br>
IRC weather plugin for Hexchat IRC client<br>
Create file in directory main script resides named: insult-list.txt<br>
Populate this file with random text to output to channel.<br>
This is a timed event set by editing: INSULT_INTERVAL = 22 * 60 * 60  # 22 hours in seconds<br>
Create file in directory main script resides named: api_key.txt<br>
This contains your API key.<br>
This script uses: https://www.weatherapi.com/<br>
You can sign up for a free account.<br>
Script uses #zz as a trigger.<br>
Usage: #zz {zip code}<br>
Usage: #zz {place name}<br>
Returns weather information for zip codes or place names.<br>
Can use USA and Canada zip codes.<br>
