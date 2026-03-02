# Reddit Mod Bot

A simple Reddit moderation bot for r/SuouYuki.

## What It Does

- **Image source lookup**: Uses SauceNAO API to find sources for image posts
- **AI post limit**: Limits AI-flaired posts to 1 per week
- **Health/stats API**: HTTP endpoints for monitoring

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Create .env file (copy from .env.example)
cp .env.example .env

# Run the bot
python main.py
```

Or with Docker:

```bash
docker compose up --build -d
```

## Environment Variables

- `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USERNAME`, `REDDIT_PASSWORD`, `REDDIT_USER_AGENT`
- `REDDIT_SUBREDDIT` - subreddit to moderate (default: SuouYuki)
- `SAUCENAO_API_KEY` - SauceNAO API key
- `REDIS_URL` - Redis URL (e.g., redis://localhost:6379)
- `PORT` - HTTP server port (default: 8080)

## API Endpoints

- `GET /` - Health check
- `GET /health` - Detailed health status
- `GET /stats` - Bot statistics
- `POST /reload` - Reload the bot
