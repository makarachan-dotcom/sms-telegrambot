# sms-telegrambot

A production-ready Telegram bot that sends SMS messages to Cambodian phone numbers **(+855)** using the [Infobip](https://www.infobip.com/) SMS API.

---

## Features

| Feature | Details |
|---|---|
| **Commands** | `/start`, `/help`, `/sendsms`, `/status`, `/cancel` |
| **Conversational wizard** | Step-by-step prompts to collect the destination number and the SMS body |
| **+855 validation** | Strict regex validation; accepts local (with/without leading 0) and international formats |
| **Infobip SMS API** | Uses Infobip's [SMS Advanced](https://www.infobip.com/docs/api/channels/sms/sms-messaging/outbound-sms/send-sms-message) endpoint |
| **Rate limiting** | Configurable per-user rolling-window rate limit (default: 5 SMS / 60 s) |
| **Error handling** | API timeouts, HTTP errors (401, 400, 429, 5xx), network failures |
| **Logging** | Rotating file logger (`logs/bot.log`) **+** console output |
| **Config** | All secrets and tunables live in a `.env` file |

---

## Requirements

- Python **3.10+**
- A [Telegram Bot Token](https://t.me/BotFather)
- An [Infobip account](https://portal.infobip.com/signup) with an API key

---

## Quick start

### 1 – Clone the repository

```bash
git clone https://github.com/makarachan-dotcom/sms-telegrambot.git
cd sms-telegrambot
```

### 2 – Create and activate a virtual environment (recommended)

```bash
python -m venv venv
# Linux / macOS
source venv/bin/activate
# Windows
venv\Scripts\activate
```

### 3 – Install dependencies

```bash
pip install -r requirements.txt
```

### 4 – Configure the `.env` file

```bash
cp .env.example .env
```

Open `.env` in your editor and fill in your real credentials:

```dotenv
# Telegram bot token (from @BotFather)
TELEGRAM_BOT_TOKEN=123456789:ABCdef...

# Your unique Infobip API base URL – found at https://portal.infobip.com/dev/api-keys
INFOBIP_BASE_URL=abc123.api.infobip.com

# Your Infobip API key
INFOBIP_API_KEY=your_infobip_api_key_here

# Optional – sender ID (alphanumeric ≤ 11 chars or a phone number)
INFOBIP_SENDER=InfoSMS

# Optional – rate-limiting
RATE_LIMIT_MAX_MESSAGES=5
RATE_LIMIT_WINDOW_SECONDS=60

# Optional – logging verbosity (DEBUG | INFO | WARNING | ERROR)
LOG_LEVEL=INFO
```

> **Never** commit the real `.env` file to version control – it is listed in `.gitignore`.

### 5 – Run the bot

```bash
python bot.py
```

The bot will start polling for updates. You should see log output similar to:

```
2024-01-01 12:00:00 | INFO     | sms_bot | Starting Infobip SMS Telegram Bot
2024-01-01 12:00:00 | INFO     | sms_bot | Infobip base URL: abc123.api.infobip.com
2024-01-01 12:00:00 | INFO     | sms_bot | Rate limit: 5 messages / 60s per user
```

---

## Usage

Open Telegram and start a chat with your bot.

### Commands

| Command | Description |
|---|---|
| `/start` | Welcome message with quick-start guide |
| `/help` | Full command reference and phone number format examples |
| `/sendsms` | Launch the two-step SMS wizard |
| `/status` | Show bot uptime and your personal send stats |
| `/cancel` | Cancel the current wizard at any step |

### Sending an SMS (`/sendsms`)

1. Type `/sendsms`.
2. Enter the destination phone number when prompted.  
   Accepted formats:

   | Input | Normalised to |
   |---|---|
   | `+85512345678` | `+85512345678` |
   | `85512345678` | `+85512345678` |
   | `012345678` | `+85512345678` |
   | `12345678` | `+85512345678` |

3. Type the SMS message text.
4. Review the confirmation card and tap **✅ Send** (or **❌ Cancel**).

---

## Phone number validation

Only Cambodian numbers (`+855`) are accepted.  
The subscriber portion must be **8–9 digits** long.

Internally, the bot normalises all accepted formats to `+855XXXXXXXXX` before sending to Infobip.

---

## Rate limiting

Each Telegram user is limited to **`RATE_LIMIT_MAX_MESSAGES`** (default: 5) sends within a rolling **`RATE_LIMIT_WINDOW_SECONDS`** (default: 60 s) window.  
Attempting to send beyond the limit returns a friendly message with the number of seconds until the window resets.

---

## Logging

- **Console** – coloured, human-readable output to `stdout`.
- **File** – rotating log at `logs/bot.log` (5 files × 5 MB each).

Set `LOG_LEVEL=DEBUG` for verbose output including full API payloads.

---

## Project structure

```
sms-telegrambot/
├── bot.py            # Main bot – all handlers, API client, rate limiter
├── requirements.txt  # Python dependencies
├── .env.example      # Environment variable template
├── .gitignore        # Excludes .env, logs/, __pycache__/, etc.
└── README.md         # This file
```

---

## Security notes

- Store credentials **only** in `.env` – never hard-code them.
- The `.env` file is excluded by `.gitignore`.
- All outbound HTTP calls use HTTPS.
- API key is sent in the `Authorization: App <key>` header, never in the URL.

---

## License

MIT
