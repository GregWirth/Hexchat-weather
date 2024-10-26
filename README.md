# IRC weather bot<br><br>
<br>
Runs on Linux Mint 21.x no idea if it works on anything else<br>
Requires various modules which can be installed with pip<br>
Written to use Python 3.10.12 but may work with older versions of 3.x<br>
Script joins selected IRC server and listens for commands.<br>
You can name the .py file anything you like {filename}.py<br>
Edit config.json with your info<br>
Run command: python3 {filename}.py --log-level {DEBUG INFO WARNING ERROR CRITICAL}<br>
A file will be created named: bot.log<br>
tail -f bot.log in another window<br>
This script uses: https://www.weatherapi.com/<br>
You can sign up for a free account.<br>
Script uses #zz as a trigger.<br>
Syntax: #zz {zip code}<br>
Syntax: #zz {place name}<br>
Returns weather information for zip codes or place names.<br>
Can use worldwide city names as well as USA and Canada zip codes.<br>
Script also has a command to send text/links to the channel from a file<br>
The text is selected randomly<br>
Usage: !warez<br>
Filename: warez-trigger.txt<br>
stab trigger reads one line randomls from a text file stab.txt and issues to channel <br>
Usage: #stab {username}<br>
<br>
Does nick registration<br>
Will issue ghost command to kill in use nick upon reconnect<br>
Detects netsplits and may attempt to reconnect<br>
Will cache API requests for 5 minutes to help reduce API hits<br> 
Will throttle users who request too quickly or too many times in a short period<br> 
Users can make up to 3 requests every 60 seconds.<br>
If they exceed this limit, they are throttled for up to 60 seconds from their first request <br>
Does input sanitation<br>
<br>
Despite the comprehensive refactoring and the meticulous integration of <br>
a plethora of enhancements designed to optimize the code's stability and <br>
reliability, there remains a non-negligible probability that you may encounter<br>
anomalous behaviors or unexpected phenomena. This potentiality arises due to <br>
the stochastic nature of environmental variables and the emergence of unforeseen <br>
edge cases inherent in complex systems.<br>
Factors such as hardware architecture variances, divergent operating system kernels, discrepancies <br>
in library versions, or even quantum-level computational fluctuations can introduce<br>
chaotic elements into the execution environment. The intricate interplay between software <br>
algorithms and the underlying computational substrate can lead to emergent properties that are <br>
not readily predictable through conventional deterministic models. Consequently, while the codebase<br>
has been engineered with rigorous adherence to best practices in software development and<br>
systems engineering, it's imperative to acknowledge that the multifaceted dynamics of real-world application <br>
could precipitate idiosyncratic issues not previously elucidated during the testing phases.<br>
