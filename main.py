import os
import sys
import praw
import time
from dotenv import load_dotenv
import redis
import logging
from datetime import datetime, timezone
from typing import Optional
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

# Maximum age (in seconds) of a submission to process.
# Posts older than this are silently skipped. Prevents the bot from
# acting on year-old posts that PRAW may return after a stream reconnect.
MAX_SUBMISSION_AGE_SECONDS = int(
    os.environ.get("MAX_SUBMISSION_AGE_SECONDS", "600")
)  # default 10 minutes


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
                except Exception:
                    pass
            self._client = None

        for attempt in range(self.max_retries):
            if self.connect():
                return True
            logger.warning(
                f"Redis reconnection attempt {attempt + 1}/{self.max_retries} failed, "
                f"retrying in {self.retry_delay}s..."
            )
            time.sleep(self.retry_delay)

        logger.error("Redis reconnection failed after all attempts")
        return False

    def _execute(self, operation):
        """Execute a Redis operation with automatic reconnect on failure."""
        try:
            if self._client is None:
                if not self.reconnect():
                    return None
            return operation(self._client)
        except (redis.ConnectionError, redis.TimeoutError) as e:
            logger.error(f"Redis error: {e}")
            if self.reconnect():
                try:
                    return operation(self._client)
                except Exception as e2:
                    logger.error(f"Redis retry failed: {e2}")
                    return None
            return None

    def exists(self, key: str) -> bool:
        result = self._execute(lambda r: r.exists(key))
        return bool(result) if result is not None else False

    def setex(self, key: str, ttl: int, value: str) -> bool:
        result = self._execute(lambda r: r.setex(key, ttl, value))
        return bool(result) if result is not None else False

    def setnx(self, key: str, value: str) -> bool:
        """Set key only if it does NOT already exist (atomic). Returns True if set."""
        result = self._execute(lambda r: r.setnx(key, value))
        return bool(result) if result is not None else False

    def expire(self, key: str, ttl: int) -> bool:
        """Set TTL on an existing key."""
        result = self._execute(lambda r: r.expire(key, ttl))
        return bool(result) if result is not None else False

    def sadd(self, key: str, value: str) -> bool:
        """Add a member to a set."""
        result = self._execute(lambda r: r.sadd(key, value))
        return bool(result) if result is not None else False

    def sismember(self, key: str, value: str) -> bool:
        """Check if a value is a member of a set."""
        result = self._execute(lambda r: r.sismember(key, value))
        return bool(result) if result is not None else False

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
            "skipped_old": 0,
            "skipped_duplicate": 0,
            "errors": 0,
            "start_time": datetime.now(timezone.utc).isoformat(),
        }

    def _is_submission_too_old(self, submission) -> bool:
        """Check if a submission is older than MAX_SUBMISSION_AGE_SECONDS.
        Prevents the bot from processing old posts that PRAW may serve
        after a stream reconnect or on startup."""
        try:
            created_utc = submission.created_utc
            now_utc = datetime.now(timezone.utc).timestamp()
            age_seconds = now_utc - created_utc
            if age_seconds > MAX_SUBMISSION_AGE_SECONDS:
                logger.info(
                    f"Skipping old submission {submission.id} "
                    f"(age: {age_seconds:.0f}s, max: {MAX_SUBMISSION_AGE_SECONDS}s)"
                )
                return True
            return False
        except Exception as e:
            logger.error(f"Error checking submission age: {e}")
            return False

    def _claim_submission(self, submission) -> bool:
        """Atomically claim a submission for processing using Redis SETNX.
        Returns True if THIS bot instance claimed it (first to call).
        Returns False if another instance already claimed it.
        This is the ONLY dedup mechanism -- it works across processes."""
        key = f"processed:{submission.id}"
        was_set = self.redis_mgr.setnx(key, datetime.now(timezone.utc).isoformat())
        if was_set:
            # Auto-expire after 24 hours to avoid unbounded growth
            self.redis_mgr.expire(key, 86400)
            return True
        logger.info(f"Submission {submission.id} already claimed, skipping")
        return False

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
        cooldown_hours = int(get_env("AI_COOLDOWN_HOURS", required=False) or 168)

        try:
            # Use SETNX (set-if-not-exists) for atomic check-and-set.
            # If setnx returns True  -> first AI post in the cooldown window -> allow
            # If setnx returns False -> key exists, user already posted -> remove
            was_set = self.redis_mgr.setnx(key, datetime.now(timezone.utc).isoformat())

            if was_set:
                # First AI post this week -- set the TTL and welcome the user
                self.redis_mgr.expire(key, cooldown_hours * 3600)

                reason = (
                    f"Hi u/{author}, welcome! This is your first AI image post "
                    f"this week. Please remember you cannot post another AI image "
                    f"until the {cooldown_hours // 24} day cooldown period.\n\n"
                    f"*I am a bot and this action was performed automatically.*"
                )

                comment = submission.reply(reason)
                comment.mod.distinguish(sticky=True)
                logger.info(f"Posted welcome comment for {author}'s first AI post")
                return True
            else:
                # User already posted within the cooldown window -> remove
                reason = (
                    f"Hi u/{author}, your post has been removed because you "
                    f"exceeded the AI image post limit (1 per week).\n\n"
                    f"Please wait for the cooldown of one week before posting "
                    f"another AI image.\n\n"
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

        except Exception as e:
            logger.error(f"Error processing AI post: {e}")
            return False

    def process_submission(self, submission):
        try:
            # --- Guard 1: Skip old posts ---
            if self._is_submission_too_old(submission):
                self.stats["skipped_old"] += 1
                return

            # --- Guard 2: Atomically claim this submission via Redis SETNX ---
            # If another process/worker already claimed it, skip.
            if not self._claim_submission(submission):
                self.stats["skipped_duplicate"] += 1
                return

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


# ---------------------------------------------------------------------------
# Flask app (health/stats only -- does NOT start the bot)
# ---------------------------------------------------------------------------
app = Flask(__name__)

# Shared Redis connection for health checks (connects lazily)
_health_redis: Optional[RedisManager] = None


def _get_health_redis() -> Optional[RedisManager]:
    global _health_redis
    if _health_redis is None:
        redis_url = os.environ.get("REDIS_URL", "")
        if redis_url:
            _health_redis = RedisManager(redis_url)
            _health_redis.connect()
    return _health_redis


@app.route("/")
def home():
    return "Bot is running!"


@app.route("/health")
def health():
    redis_status = "connected"
    try:
        mgr = _get_health_redis()
        client = mgr.get_client() if mgr else None
        if client:
            client.ping()
        else:
            redis_status = "disconnected"
    except Exception:
        redis_status = "disconnected"

    return jsonify(
        {
            "status": "healthy" if redis_status == "connected" else "degraded",
            "redis": redis_status,
        }
    )


@app.route("/stats")
def stats():
    return jsonify({"info": "Stats available via bot process logs"})


# ---------------------------------------------------------------------------
# CLI entry point: runs the bot directly (no gunicorn, no Flask)
# ---------------------------------------------------------------------------
def main():
    try:
        bot = RedditBot()
        logger.info("Bot initialized successfully")

        # Start Flask health server in a background thread
        port = int(os.environ.get("PORT", 8080))
        flask_thread = Thread(
            target=lambda: app.run(
                host="0.0.0.0", port=port, threaded=True, use_reloader=False
            ),
            daemon=True,
        )
        flask_thread.start()
        logger.info(f"Health server listening on port {port}")

        # Run the bot in the main thread (blocking)
        bot.run()

    except KeyboardInterrupt:
        logger.info("Received shutdown signal")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
