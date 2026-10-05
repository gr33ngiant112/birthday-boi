# birthday-boi

## A Discord bot that remembers your birthday and everyone elses.

### Developing

1. Install pyenv

```bash
git clone https://github.com/pyenv/pyenv.git ~/.pyenv
echo 'export PATH="$HOME/.pyenv/bin:$PATH"' >> ~/.bashrc
echo 'eval "$(pyenv init --path)"' >> ~/.bashrc
source ~/.bashrc
```

2. Install python 3.12.6 and set it globally. Make sure you have glibc and dependent tooling available to build a Python distribution. If you have trouble building Python, You can find solutions to common issues with installing these tools [here](https://github.com/pyenv/pyenv/wiki/Common-build-problems)

```bash
# If you're on ubuntu, you'll need these packages
sudo apt-get update
sudo apt-get install \
    build-essential \
    libssl-dev \
    zlib1g-dev \
    libbz2-dev \
    libreadline-dev \
    libsqlite3-dev \
    libffi-dev \
    liblzma-dev \
    tk-dev \
    libgdbm-dev \
    libncurses5-dev \
    libncursesw5-dev \
    xz-utils \
    libexpat1-dev \
    libdb5.3-dev \
    libmpdec-dev \
    libtinfo-dev \
    uuid-dev \
    libgmp-dev

# Install python3 and set it globally
pyenv install 3.12.6
pyenv global 3.12.6
exec zsh # or bash
```

3. Create the virtual environment

```bash
sudo apt install python3-venv 
python3 -m venv .
source venv/bin/activate   
```

### Installing

1. Register the bot as a discord app [here](https://discord.com/developers/applications). Remember to _save the token_.

### Inviting the bot

Invite the bot with only the access it uses. Replace `<CLIENT_ID>` with your application's client ID from the Developer Portal:

```
https://discord.com/oauth2/authorize?client_id=<CLIENT_ID>&scope=bot%20applications.commands&permissions=68608
```

- Scopes: `bot` and `applications.commands` (the slash commands).
- Permissions: View Channels, Send Messages and Read Message History (`68608`). The bot replies to mentions and posts the monthly list in #general, and Discord requires Read Message History to reply to a message.
- Never grant Administrator or Mention Everyone; the bot needs neither.
- On the Developer Portal's Bot page, leave the Message Content and Server Members privileged intents off. `bot.py` uses `discord.Intents.default()`, which requests neither.
