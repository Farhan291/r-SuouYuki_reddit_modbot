import os
import praw
import time
import redis 
from datetime import datetime, timedelta
import requests
from threading import Thread
from flask import Flask

reddit = praw.Reddit(
    client_id=os.environ["REDDIT_CLIENT_ID"],
    client_secret=os.environ["REDDIT_CLIENT_SECRET"],
    username=os.environ["REDDIT_USERNAME"],
    password=os.environ["REDDIT_PASSWORD"],
    user_agent=os.environ["REDDIT_USER_AGENT"]
)

redis_url=os.environ["REDIS_URL"]
r = redis.from_url(redis_url, decode_responses=True)

sauce = os.environ["SAUCENAO_API_KEY"]
subreddit = reddit.subreddit("SuouYuki")
#ai_posts = {}

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

def run_bot():
    #global ai_posts
    for submission in subreddit.stream.submissions(skip_existing=True):
        try:
            url = str(submission.url)
            
            try:
                params = {"api_key": sauce, "url": url, "output_type": 2}
                response = requests.get("https://saucenao.com/search.php", params=params, timeout=15)
                data = response.json()

                if "results" in data and len(data["results"]) > 0:
                    best = data["results"][0]
                    ext_urls = best["data"].get("ext_urls", [])
                    if ext_urls:
                        comment = submission.reply(
                            f"Source: {ext_urls[0]}\n\n"
                            "*I am bot and this action was performed automatically*"
                        )
                        comment.mod.distinguish(sticky=True)
            except Exception as e:
                print(f"SauceNAO error: {e}")

            if submission.link_flair_text == "AI":
                author = str(submission.author)
                post_time = datetime.utcfromtimestamp(submission.created_utc)
                
                key = f"ai_post:{author}"
                if r.exists(key):
                    comment = submission.reply(
                        f"Hi {author}, your post has been removed because you exceeded the AI image post limit (1 per week).\n\n"
                        "Please wait for the cooldown of one week before posting another AI image.\n\n"
                        "Contact moderators if you think this was an error.\n\n"
                        "*I am bot and this action was performed automatically*"
                    )
                    comment.mod.distinguish(sticky=True)
                    time.sleep(10)
                    submission.mod.remove()
                else:
                    r.setex(key, timedelta(weeks=1), post_time.isoformat())
                    comment = submission.reply(
                        f"Hi u/{author}, this is your first AI image post this week. "
                        "Please remember you cannot post another AI image until one week cooldown."
                    )
                    comment.mod.distinguish(sticky=True)

        except Exception as e:
            print(f"General error: {e}")
            time.sleep(30)
            

bot_thread = Thread(target=run_bot, daemon=True)
bot_thread.start()

print("Bot thread started, Flask app ready.")
