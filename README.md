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
