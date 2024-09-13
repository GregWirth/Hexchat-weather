# IRC weather bot<br><br>
<br>
Script runs, joins selected IRC server and listens for commands.<br>
Removed in latest version: Create file in directory main script resides named: insult-list.txt<br>
Removed in latest version: Populate this file with random text to output to channel.<br>
Removed in latest version: This is a timed event set by editing: INSULT_INTERVAL = 22 * 60 * 60  # 22 hours in seconds<br>
Create file in directory main script resides named: api_key.txt<br>
This contains your API key.<br>
This script uses: https://www.weatherapi.com/<br>
You can sign up for a free account.<br>
Script uses #zz as a trigger.<br>
Usage: #zz {zip code}<br>
Usage: #zz {place name}<br>
Returns weather information for zip codes or place names.<br>
Can use USA and Canada zip codes.<br>
