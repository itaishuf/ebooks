At this point, the api needs to be accessed from two fronts: iOS shortcuts and a basic web app. 
### iOS shortcuts 
A shortcut that lives inside greathe goodreads app and sends the goodreads url to the api. The other params the api needs will be hardcoded in the shortcuts app
### Web app
Components:
Input bar that can handle free text in multiple languages.
Status of active requests: what stage are they in the sending process. This status bar should read the logs the api generates, and if the api logs aren’t sufficient, the agent should create a markdown file with needs from the api for the api agent to add. This part needs the clearest user experience. 