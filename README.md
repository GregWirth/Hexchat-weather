# IRC weather bot<br><br>
<br>
Script runs, joins selected IRC server and listens for commands.<br>
Requires various modules which can be installed with pip<br>
Edit config.json with your info<br>
Run command: python3 weather-dev-10.py --log-level INFO<br>
A file will be created named: bot.log<br>
tail -f bot.log in another window<br>
Script does not currently run in the background<br>
This script uses: https://www.weatherapi.com/<br>
You can sign up for a free account.<br>
Script uses #zz as a trigger.<br>
Usage: #zz {zip code}<br>
Usage: #zz {place name}<br>
Returns weather information for zip codes or place names.<br>
Can use USA and Canada zip codes.<br>
