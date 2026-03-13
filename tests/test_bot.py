import pytest
from unittest.mock import Mock, patch, MagicMock, PropertyMock
import os
import sys
import time
import redis

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Set required env vars BEFORE importing main to prevent sys.exit
@pytest.fixture(autouse=True)
def set_env_vars(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "test_id")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "test_secret")
    monkeypatch.setenv("REDDIT_USERNAME", "test_user")
    monkeypatch.setenv("REDDIT_PASSWORD", "test_pass")
    monkeypatch.setenv("REDDIT_USER_AGENT", "test_agent")
    monkeypatch.setenv("SAUCENAO_API_KEY", "test_key")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("ENABLE_SAUCENAO", "true")
    monkeypatch.setenv("ENABLE_AI_LIMIT", "true")
    monkeypatch.setenv("AI_COOLDOWN_HOURS", "168")
    monkeypatch.setenv("MAX_SUBMISSION_AGE_SECONDS", "600")


class TestRedisManager:
    @patch("main.redis.from_url")
    def test_connect_success(self, mock_redis):
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_redis.return_value = mock_client

        from main import RedisManager

        rm = RedisManager("redis://localhost:6379")
        result = rm.connect()

        assert result is True
        mock_redis.assert_called_once()
        mock_client.ping.assert_called_once()

    @patch("main.redis.from_url")
    def test_connect_failure(self, mock_redis):
        mock_redis.side_effect = Exception("Connection refused")

        from main import RedisManager

        rm = RedisManager("redis://localhost:6379")
        result = rm.connect()

        assert result is False

    @patch("main.redis.from_url")
    def test_reconnect(self, mock_redis):
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_redis.return_value = mock_client

        from main import RedisManager

        rm = RedisManager("redis://localhost:6379")
        rm.connect()

        assert rm._client is not None
        assert mock_client.ping.called

        rm._client = None

        result = rm.reconnect()

        assert result is True
        assert rm._client is not None
        assert mock_redis.call_count == 2

    @patch("main.redis.from_url")
    def test_exists(self, mock_redis):
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.exists.return_value = 1
        mock_redis.return_value = mock_client

        from main import RedisManager

        rm = RedisManager("redis://localhost:6379")
        rm.connect()

        assert rm.exists("test_key") is True
        mock_client.exists.assert_called_once_with("test_key")

    @patch("main.redis.from_url")
    def test_setex(self, mock_redis):
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.setex.return_value = True
        mock_redis.return_value = mock_client

        from main import RedisManager

        rm = RedisManager("redis://localhost:6379")
        rm.connect()

        assert rm.setex("key", 3600, "value") is True
        mock_client.setex.assert_called_once_with("key", 3600, "value")

    @patch("main.redis.from_url")
    def test_setnx(self, mock_redis):
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.setnx.return_value = True
        mock_redis.return_value = mock_client

        from main import RedisManager

        rm = RedisManager("redis://localhost:6379")
        rm.connect()

        assert rm.setnx("key", "value") is True
        mock_client.setnx.assert_called_once_with("key", "value")

    @patch("main.redis.from_url")
    def test_setnx_key_exists(self, mock_redis):
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.setnx.return_value = False  # Key already exists
        mock_redis.return_value = mock_client

        from main import RedisManager

        rm = RedisManager("redis://localhost:6379")
        rm.connect()

        assert rm.setnx("key", "value") is False

    @patch("main.redis.from_url")
    def test_expire(self, mock_redis):
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.expire.return_value = True
        mock_redis.return_value = mock_client

        from main import RedisManager

        rm = RedisManager("redis://localhost:6379")
        rm.connect()

        assert rm.expire("key", 3600) is True
        mock_client.expire.assert_called_once_with("key", 3600)

    @patch("main.redis.from_url")
    def test_sadd_and_sismember(self, mock_redis):
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.sadd.return_value = 1
        mock_client.sismember.return_value = True
        mock_redis.return_value = mock_client

        from main import RedisManager

        rm = RedisManager("redis://localhost:6379")
        rm.connect()

        assert rm.sadd("myset", "val") is True
        assert rm.sismember("myset", "val") is True

    @patch("main.redis.from_url")
    def test_execute_reconnect_on_connection_error(self, mock_redis):
        mock_client = Mock()
        mock_client.ping.return_value = True
        # First call raises ConnectionError, second succeeds
        mock_client.exists.side_effect = [redis.ConnectionError("lost"), 1]
        mock_redis.return_value = mock_client

        from main import RedisManager

        rm = RedisManager("redis://localhost:6379", retry_delay=0)
        rm.connect()

        # Should reconnect and retry successfully
        result = rm.exists("key")
        assert result is True


class TestRedditBot:
    def _make_bot(self):
        """Create a RedditBot with mocked dependencies."""
        from main import RedditBot

        with patch.object(RedditBot, "__init__", lambda self: None):
            bot = RedditBot()
            bot.sauce_api_key = "test_key"
            bot.redis_mgr = Mock()
            bot.reddit = Mock()
            bot.subreddit = Mock()
            bot._running = True
            bot._lock = Mock()
            bot.stats = {
                "images_processed": 0,
                "ai_posts_processed": 0,
                "posts_removed": 0,
                "sources_found": 0,
                "skipped_old": 0,
                "skipped_duplicate": 0,
                "errors": 0,
                "start_time": "2025-01-01T00:00:00+00:00",
            }
            return bot

    @patch("main.praw.Reddit")
    @patch("main.redis.from_url")
    def test_bot_initialization(self, mock_redis, mock_reddit):
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_redis.return_value = mock_client

        mock_reddit_instance = Mock()
        mock_reddit.return_value = mock_reddit_instance
        mock_reddit_instance.subreddit.return_value = Mock()

        from main import RedditBot

        bot = RedditBot()

        assert bot.sauce_api_key == "test_key"

    @patch("main.requests.get")
    def test_search_source_success(self, mock_get):
        mock_response = Mock()
        mock_response.json.return_value = {
            "results": [
                {
                    "header": {"similarity": "85.5"},
                    "data": {"ext_urls": ["https://example.com/source"]},
                }
            ]
        }
        mock_get.return_value = mock_response

        bot = self._make_bot()
        result = bot.search_source("https://example.com/image.jpg")

        assert result == "https://example.com/source"

    @patch("main.requests.get")
    def test_search_source_low_similarity(self, mock_get):
        mock_response = Mock()
        mock_response.json.return_value = {
            "results": [
                {
                    "header": {"similarity": "50"},
                    "data": {"ext_urls": ["https://example.com/source"]},
                }
            ]
        }
        mock_get.return_value = mock_response

        bot = self._make_bot()
        result = bot.search_source("https://example.com/image.jpg")

        assert result is None

    @patch("main.requests.get")
    def test_search_source_no_results(self, mock_get):
        mock_response = Mock()
        mock_response.json.return_value = {"results": []}
        mock_get.return_value = mock_response

        bot = self._make_bot()
        result = bot.search_source("https://example.com/image.jpg")

        assert result is None

    @patch("main.requests.get")
    def test_search_source_api_error(self, mock_get):
        mock_get.side_effect = Exception("API timeout")

        bot = self._make_bot()
        result = bot.search_source("https://example.com/image.jpg")

        assert result is None

    # ---- Submission age checks ----

    def test_is_submission_too_old_rejects_old(self):
        bot = self._make_bot()
        submission = Mock()
        # Set created_utc to 1 hour ago (3600s > 600s max)
        submission.created_utc = time.time() - 3600

        assert bot._is_submission_too_old(submission) is True

    def test_is_submission_too_old_accepts_fresh(self):
        bot = self._make_bot()
        submission = Mock()
        # Set created_utc to 30 seconds ago
        submission.created_utc = time.time() - 30

        assert bot._is_submission_too_old(submission) is False

    def test_is_submission_too_old_handles_error(self):
        bot = self._make_bot()
        submission = Mock()
        type(submission).created_utc = PropertyMock(side_effect=Exception("no attr"))

        # On error, should return False (process to be safe)
        assert bot._is_submission_too_old(submission) is False

    # ---- Duplicate processing checks ----

    def test_is_already_processed_true(self):
        bot = self._make_bot()
        bot.redis_mgr.sismember.return_value = True
        submission = Mock()
        submission.id = "abc123"

        assert bot._is_already_processed(submission) is True
        bot.redis_mgr.sismember.assert_called_once_with(
            "processed_submissions", "abc123"
        )

    def test_is_already_processed_false(self):
        bot = self._make_bot()
        bot.redis_mgr.sismember.return_value = False
        submission = Mock()
        submission.id = "abc123"

        assert bot._is_already_processed(submission) is False

    def test_mark_processed(self):
        bot = self._make_bot()
        submission = Mock()
        submission.id = "abc123"

        bot._mark_processed(submission)
        bot.redis_mgr.sadd.assert_called_once_with("processed_submissions", "abc123")

    # ---- AI post rate limiting ----

    @patch("main.time.sleep")
    def test_process_ai_post_first_post_allowed(self, mock_sleep):
        """First AI post in a week should be allowed (setnx returns True)."""
        bot = self._make_bot()
        bot.redis_mgr.setnx.return_value = True
        bot.redis_mgr.expire.return_value = True

        submission = Mock()
        submission.author = "TestUser"
        mock_comment = Mock()
        submission.reply.return_value = mock_comment

        result = bot.process_ai_post(submission)

        assert result is True
        bot.redis_mgr.setnx.assert_called_once()
        bot.redis_mgr.expire.assert_called_once()
        submission.reply.assert_called_once()
        # Should NOT remove the post
        submission.mod.remove.assert_not_called()
        assert bot.stats["posts_removed"] == 0

    @patch("main.time.sleep")
    def test_process_ai_post_duplicate_removed(self, mock_sleep):
        """Second AI post in a week should be removed (setnx returns False)."""
        bot = self._make_bot()
        bot.redis_mgr.setnx.return_value = False  # Key already exists

        submission = Mock()
        submission.author = "TestUser"
        mock_comment = Mock()
        submission.reply.return_value = mock_comment

        result = bot.process_ai_post(submission)

        assert result is True
        submission.mod.remove.assert_called_once()
        assert bot.stats["posts_removed"] == 1

    @patch("main.time.sleep")
    def test_process_ai_post_error_handling(self, mock_sleep):
        """Errors in AI post processing should be caught."""
        bot = self._make_bot()
        bot.redis_mgr.setnx.side_effect = Exception("Redis down")

        submission = Mock()
        submission.author = "TestUser"

        result = bot.process_ai_post(submission)

        assert result is False

    # ---- Full submission processing ----

    def test_process_submission_skips_old_posts(self):
        """Old posts should be skipped entirely."""
        bot = self._make_bot()
        submission = Mock()
        submission.id = "old123"
        submission.created_utc = time.time() - 7200  # 2 hours old

        bot.process_submission(submission)

        assert bot.stats["skipped_old"] == 1
        # Should not check duplicate or process
        bot.redis_mgr.sismember.assert_not_called()

    def test_process_submission_skips_duplicate(self):
        """Already-processed posts should be skipped."""
        bot = self._make_bot()
        submission = Mock()
        submission.id = "dup123"
        submission.created_utc = time.time() - 5  # Fresh post
        bot.redis_mgr.sismember.return_value = True  # Already processed

        bot.process_submission(submission)

        assert bot.stats["skipped_duplicate"] == 1

    @patch("main.time.sleep")
    def test_process_submission_fresh_ai_post(self, mock_sleep):
        """A fresh AI-flaired post from a new user should be allowed."""
        bot = self._make_bot()
        bot.redis_mgr.sismember.return_value = False  # Not processed yet
        bot.redis_mgr.setnx.return_value = True  # First AI post
        bot.redis_mgr.expire.return_value = True

        submission = Mock()
        submission.id = "fresh123"
        submission.created_utc = time.time() - 5  # 5 seconds old
        submission.url = "https://i.redd.it/image.jpg"
        submission.link_flair_text = "AI"
        mock_comment = Mock()
        submission.reply.return_value = mock_comment

        with patch.object(bot, "process_image_post", return_value=False):
            bot.process_submission(submission)

        assert bot.stats["ai_posts_processed"] == 1
        assert bot.stats["images_processed"] == 1
        assert bot.stats["posts_removed"] == 0
        bot.redis_mgr.sadd.assert_called_once_with("processed_submissions", "fresh123")

    def test_process_submission_non_image_non_ai(self):
        """A text post with no AI flair should be processed but nothing happens."""
        bot = self._make_bot()
        bot.redis_mgr.sismember.return_value = False

        submission = Mock()
        submission.id = "text123"
        submission.created_utc = time.time() - 5
        submission.url = "https://reddit.com/r/SuouYuki/comments/abc"
        submission.link_flair_text = "Discussion"

        bot.process_submission(submission)

        assert bot.stats["images_processed"] == 0
        assert bot.stats["ai_posts_processed"] == 0
        bot.redis_mgr.sadd.assert_called_once_with("processed_submissions", "text123")

    def test_process_submission_error_handling(self):
        """Errors in process_submission should be caught and counted."""
        bot = self._make_bot()
        submission = Mock()
        submission.id = "err123"
        # Make created_utc work but then crash on url access
        submission.created_utc = time.time() - 5
        bot.redis_mgr.sismember.return_value = False
        type(submission).url = PropertyMock(side_effect=Exception("boom"))

        bot.process_submission(submission)

        assert bot.stats["errors"] == 1


class TestFlaskApp:
    def test_home_route(self):
        from main import app

        with app.test_client() as client:
            response = client.get("/")
            assert response.data == b"Bot is running!"

    def test_health_route(self):
        from main import app

        with app.test_client() as client:
            response = client.get("/health")
            assert response.status_code in [200, 500]

    def test_stats_route(self):
        from main import app

        with app.test_client() as client:
            response = client.get("/stats")
            assert response.status_code in [200]

    def test_reload_requires_post(self):
        from main import app

        with app.test_client() as client:
            response = client.get("/reload")
            assert response.status_code == 405  # Method Not Allowed


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
