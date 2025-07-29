## IOS Shortcut Client and FastAPI Server for eBook Downloading

ios shortcut available [here](https://www.icloud.com/shortcuts/66149e9ecd5c4ce1b9d4a50abcd03045)

## Setup
### Shourcut
1. Download the 'actions' app 
2. Change the Host to your pc name/ip address
3. Enter yours Kindle email address
4. Setup a private LAN between your devices for usage not on same network (i use tailsacle)

### Gmail Configuration
1. In your google account, [create](https://myaccount.google.com/apppasswords) an "App Password" for the use in this server.
2. Save the password in an environment variable with the following command:
3. `setx GMAIL_PASSWORD "abcd abcd abcd abcd"`
4. Save your Google account also to an environment variable
5. `setx GMAIL_ACCOUNT "name@gmail.com"`
6. :warning: **make sure to run those commands with admin privileges** :warning:

### Server
1. Install Python 3.10 or later 
2. Run setup.py


## Bugs
Sometimes an ad pops up which returns an uncaught execption. a simple re-run should fix that
