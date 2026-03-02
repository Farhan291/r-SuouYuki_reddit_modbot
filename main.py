import os
import sys
import praw
import time
from dotenv import load_dotenv
import redis
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from contextlib import contextmanager
import requests
from threading import Thread, Lock
from flask import Flask, jsonify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("bot.log")],
)
logger = logging.getLogger(__name__)

# Load environment variables from .env if present
load_dotenv()


class RedisManager:
    def __init__(self, redis_url: str, max_retries: int = 5, retry_delay: int = 5):
        self.redis_url = redis_url
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._client: Optional[redis.Redis] = None
        self._lock = Lock()

    def connect(self) -> bool:
        with self._lock:
            if self._client is None:
                try:
                    self._client = redis.from_url(
                        self.redis_url,
                        decode_responses=True,
                        socket_connect_timeout=10,
                        socket_timeout=10,
                    )
                    self._client.ping()
                    logger.info("Redis connected successfully")
                    return True
                except Exception as e:
                    logger.error(f"Redis connection failed: {e}")
                    return False
            return True

    def reconnect(self) -> bool:
        logger.warning("Attempting to reconnect to Redis...")
        with self._lock:
            if self._client:
                try:
                    self._client.close()
                except:
                    pass
            self._client = None

        for attempt in range(self.max_retries):
            if self.connect():
                return True
            logger.warning(
                f"Redis reconnection attempt {attempt + 1}/{self.max_retries} failed, retrying in {self.retry_delay}s..."
            )
            time.sleep(self.retry_delay)

        logger.error("Redis reconnection failed after all attempts")
        return False

    @contextmanager
    def safe_operation(self):
        try:
            yield self._client
        except redis.ConnectionError as e:
            logger.error(f"Redis connection error: {e}")
            if self.reconnect():
                yield self._client
            else:
                raise
        except redis.TimeoutError as e:
            logger.error(f"Redis timeout error: {e}")
            if self.reconnect():
                yield self._client
            else:
                raise

    def exists(self, key: str) -> bool:
        with self.safe_operation() as r:
            return bool(r.exists(key)) if r else False

    def setex(self, key: str, time: int, value: str) -> bool:
        with self.safe_operation() as r:
            return bool(r.setex(key, time, value)) if r else False

    def get_client(self) -> Optional[redis.Redis]:
        return self._client


def get_env(var: str, required: bool = True) -> str:
    value = os.environ.get(var)
    if required and not value:
        logger.error(f"Required environment variable {var} is not set")
        sys.exit(1)
    return value or ""


class RedditBot:
    def __init__(self):
        self.reddit = praw.Reddit(
            client_id=get_env("REDDIT_CLIENT_ID"),
            client_secret=get_env("REDDIT_CLIENT_SECRET"),
            username=get_env("REDDIT_USERNAME"),
            password=get_env("REDDIT_PASSWORD"),
            user_agent=get_env("REDDIT_USER_AGENT"),
        )
        self.sauce_api_key = get_env("SAUCENAO_API_KEY")
        self.subreddit = self.reddit.subreddit(
            get_env("REDDIT_SUBREDDIT", required=False) or "SuouYuki"
        )

        redis_url = get_env("REDIS_URL")
        self.redis_mgr = RedisManager(redis_url)

        if not self.redis_mgr.connect():
            logger.error("Failed to connect to Redis, exiting...")
            sys.exit(1)

        self._running = True
        self._lock = Lock()
        self.stats = {
            "images_processed": 0,
            "ai_posts_processed": 0,
            "posts_removed": 0,
            "sources_found": 0,
            "errors": 0,
            "start_time": datetime.now(timezone.utc).isoformat(),
        }

    def search_source(self, url: str) -> Optional[str]:
        try:
            params = {"api_key": self.sauce_api_key, "url": url, "output_type": 2}
            response = requests.get(
                "https://saucenao.com/search.php", params=params, timeout=15
            )
            data = response.json()

            if "results" in data and len(data["results"]) > 0:
                best = data["results"][0]
                similarity = best.get("header", {}).get("similarity", "0")

                if float(similarity) >= float(
                    get_env("SAUCENAO_MIN_SIMILARITY", required=False) or 70
                ):
                    ext_urls = best["data"].get("ext_urls", [])
                    if ext_urls:
                        return ext_urls[0]

            return None
        except Exception as e:
            logger.error(f"SauceNAO error: {e}")
            return None

    def process_image_post(self, submission) -> bool:
        url = str(submission.url)

        if not any(
            url.lower().endswith(ext)
            for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]
        ):
            return False

        source = self.search_source(url)
        if source:
            try:
                comment = submission.reply(
                    f"**Source:** {source}\n\n"
                    f"*I am a bot and this action was performed automatically. "
                    f"Contact moderators if you think this was an error.*"
                )
                comment.mod.distinguish(sticky=True)
                comment.mod.approve()
                logger.info(f"Posted source comment for {submission.id}: {source}")
                return True
            except Exception as e:
                logger.error(f"Error posting source comment: {e}")

        return False

    def process_ai_post(self, submission) -> bool:
        author = str(submission.author)
        key = f"ai_post:{author}"

        try:
            if self.redis_mgr.exists(key):
                reason = (
                    f"Hi u/{author}, your post has been removed because you exceeded "
                    f"the AI image post limit (1 per week).\n\n"
                    f"Please wait for the cooldown of one week before posting another AI image.\n\n"
                    f"Contact moderators if you think this was an error.\n\n"
                    f"*I am a bot and this action was performed automatically.*"
                )

                comment = submission.reply(reason)
                comment.mod.distinguish(sticky=True)
                comment.mod.approve()

                time.sleep(10)
                submission.mod.remove()

                self.stats["posts_removed"] += 1
                logger.info(f"Removed AI post from {author} - exceeded weekly limit")
                return True
            else:
                cooldown_hours = int(
                    get_env("AI_COOLDOWN_HOURS", required=False) or 168
                )
                self.redis_mgr.setex(
                    key, cooldown_hours * 3600, datetime.now(timezone.utc).isoformat()
                )

                reason = (
                    f"Hi u/{author}, welcome! This is your first AI image post this week. "
                    f"Please remember you cannot post another AI image until the {cooldown_hours // 24} day cooldown period.\n\n"
                    f"*I am a bot and this action was performed automatically.*"
                )

                comment = submission.reply(reason)
                comment.mod.distinguish(sticky=True)
                logger.info(f"Posted welcome comment for {author}'s first AI post")
                return True

        except Exception as e:
            logger.error(f"Error processing AI post: {e}")
            return False

    def is_banned_user(self, author) -> bool:
        """Check if user is banned from the subreddit."""
        try:
            key = f"banned:{author}"
            return self.redis_mgr.exists(key)
        except Exception:
            return False

    def process_submission(self, submission):
        try:
            is_image = any(
                submission.url.lower().endswith(ext)
                for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]
            )
            is_ai = submission.link_flair_text == "AI"

            if is_image and get_env("ENABLE_SAUCENAO", required=False) != "false":
                if self.process_image_post(submission):
                    self.stats["sources_found"] += 1

            if is_ai and get_env("ENABLE_AI_LIMIT", required=False) != "false":
                if self.process_ai_post(submission):
                    self.stats["ai_posts_processed"] += 1

            if is_image:
                self.stats["images_processed"] += 1

        except Exception as e:
            logger.error(f"Error processing submission {submission.id}: {e}")
            self.stats["errors"] += 1

    def run(self):
        logger.info(f"Starting bot for r/{self.subreddit}")

        while self._running:
            try:
                # Process submissions
                for submission in self.subreddit.stream.submissions(skip_existing=True):
                    if not self._running:
                        break
                    self.process_submission(submission)

            except Exception as e:
                logger.error(f"Error in stream: {e}, reconnecting in 30s...")
                self.stats["errors"] += 1
                time.sleep(30)

    def stop(self):
        with self._lock:
            self._running = False
        logger.info("Bot stopped gracefully")


app = Flask(__name__)
bot: Optional[RedditBot] = None


@app.route("/")
def home():
    return "Bot is running!"


@app.route("/health")
def health():
    redis_status = "connected"
    try:
        client = bot.redis_mgr.get_client() if bot and bot.redis_mgr else None
        if client:
            client.ping()
    except Exception:
        redis_status = "disconnected"

    return jsonify(
        {
            "status": "healthy" if redis_status == "connected" else "degraded",
            "redis": redis_status,
            "stats": bot.stats if bot else {},
        }
    )


@app.route("/stats")
def stats():
    if bot:
        return jsonify(bot.stats)
    return jsonify({"error": "Bot not initialized"})


@app.route("/reload")
def reload():
    global bot
    try:
        if bot:
            bot.stop()
        time.sleep(2)
        bot = RedditBot()
        bot_thread = Thread(target=bot.run, daemon=True)
        bot_thread.start()
        return jsonify({"status": "reloaded"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def main():
    global bot
    try:
        bot = RedditBot()
        logger.info("Bot initialized successfully")

        bot_thread = Thread(target=bot.run, daemon=True)
        bot_thread.start()

        port = int(os.environ.get("PORT", 8080))
        app.run(host="0.0.0.0", port=port, threaded=True)

    except KeyboardInterrupt:
        logger.info("Received shutdown signal")
        if bot:
            bot.stop()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
