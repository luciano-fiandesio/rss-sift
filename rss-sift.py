import argparse
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

import pytz
import replicate
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from bs4 import BeautifulSoup
from croniter import croniter
from feedgen.feed import FeedGenerator
from flask import Flask, Response, redirect, render_template, request, url_for
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from waitress import serve

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        RotatingFileHandler("app.log", maxBytes=10 * 1024 * 1024, backupCount=1),
        logging.StreamHandler(),
    ],
)

logging.getLogger("httpx").setLevel(logging.WARNING)

app = Flask(__name__)
Base = declarative_base()


# Define the SQLite database model
class FeedData(Base):
    __tablename__ = "feed_data"
    id = Column(Integer, primary_key=True)
    feed_name = Column(String)
    title = Column(String)
    link = Column(String)
    additional_info = Column(Text)
    hash = Column(String, unique=True)
    skip_ai = Column(Boolean, default=False)
    created = Column(DateTime)


class FeedMeta(Base):
    __tablename__ = "feed_meta"
    id = Column(Integer, primary_key=True)
    feed_name = Column(String, unique=True)
    last_fetched = Column(DateTime)


# Setup the SQLite database
engine = create_engine("sqlite:///feeds.db")
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
session = Session()

# Load configuration
with open("config.json") as config_file:
    config = json.load(config_file)

# Get the API token from environment variable
replicate_api_token = os.getenv("REPLICATE_API_TOKEN")
if not replicate_api_token:
    raise ValueError("REPLICATE_API_TOKEN environment variable is not set")


# Get the timezone from the configuration file
timezone = pytz.timezone(config.get("timezone", "UTC"))
target_year = config.get("target_year", 2025)


def evaluate_entry(title, html_snippet):
    try:
        input = {
            "prompt": f"""Your task is to:
1. Determine if the book title pertains to one or more of these topics:
   - software development, machine learning, AI, DevOps, programming languages
   - front-end development, software development best practices
   - large language models (chatgpt, llama), data science, networking
   - software architecture, technical leadership

2. Extract the published year from the HTML (look for "Published", "Date", or any year like 2024, 2025, 2026).

Respond with exactly TWO lines:
- First line: "yes" or "no" (only yes/no, nothing else)
- Second line: the 4-digit year (e.g., 2025) or "unknown" if not found. Make sure the year is a valid 4-digit number, do not split it into multiple parts.

Example responses:
yes
2025
or
no
unknown

Here is the book title: "{title}"
Here is the HTML snippet containing the post:
{html_snippet}
""",
            "system_prompt": "You are an expert and helpful software developer and data expert",
        }

        output = replicate.run("meta/meta-llama-3-70b-instruct", input=input)

        # Join all output parts, handling year fragments that may be split
        full_response = " ".join(output)

        # Check if year is split (e.g., "202" "5" -> "2025")
        year_candidate = full_response
        for _ in range(3):
            year_candidate = re.sub(
                r"(19|20)\s*\d\s*\d?",
                lambda m: m.group().replace(" ", ""),
                year_candidate,
            )

        # Extract first word for is_interesting check
        first_word = (
            full_response.split()[0].lower().strip() if full_response.split() else ""
        )
        is_interesting = "yes" in first_word

        # Extract 4-digit year
        published_year = None
        year_match = re.search(r"\b(19|20)\d{2}\b", year_candidate)
        if year_match:
            published_year = int(year_match.group())

        logging.info(f"full AI Response: {output}")
        logging.info(
            f"Parsed response: is_interesting={is_interesting}, published_year={published_year}"
        )

        return is_interesting, published_year
    except Exception as e:
        logging.error(f"Error while checking title with AI: {e}")
        return False, None


def fetch_html(url):
    try:
        response = requests.get(url)
        response.raise_for_status()  # Raise an error for bad status codes
        return response.text
    except requests.RequestException as e:
        logging.error(f"Error fetching HTML from {url}: {e}")
        return None


def generate_hash(title, link, additional_info):
    hash_input = f"{title}{link}{additional_info}".encode("utf-8")
    return hashlib.sha256(hash_input).hexdigest()


def parse_and_store_feed(feed_name, url_to_fetch, url_prefix):
    logging.info(f"Fetching feed: {feed_name} from {url_to_fetch}")
    html_content = fetch_html(url_to_fetch)
    if html_content is None:
        logging.error(f"Failed to fetch feed: {feed_name}")
        return
    soup = BeautifulSoup(html_content, "html.parser")

    rows = soup.find_all("div", class_="row")
    for row in rows:
        article = row.find("div", class_="article")
        if not article:
            continue

        h1_tag = article.find("h1")
        if not h1_tag:
            continue

        title_tag = h1_tag.find("a", class_="title-link")
        if not title_tag:
            continue

        title = title_tag.get_text(strip=True)
        link = title_tag["href"]

        text_center_divs = article.find_all("div", class_="text-center")
        additional_info = None
        for div in text_center_divs:
            if div.b:
                additional_info = div.get_text(strip=True)
                break

        if not additional_info:
            continue

        entry_hash = generate_hash(title, link, additional_info)
        existing_entry = session.query(FeedData).filter_by(hash=entry_hash).first()
        if existing_entry:
            logging.info(f"Skipping duplicate entry: {title[:50]}...")
            continue

        logging.info(f"Entry additional_info: {additional_info[:100]}...")

        # Get the article HTML for AI evaluation, excluding post-date which is when the entry was posted, not published
        article_copy = BeautifulSoup(str(article), "html.parser")
        post_date = article_copy.find("div", class_="post-date")
        if post_date:
            post_date.decompose()
        article_html = str(article_copy)

        logging.info(f"Processing NEW entry: {title[:50]}...")
        is_interesting, published_year = evaluate_entry(title, article_html)

        # Determine if entry should be included in RSS feed
        # Skip if: year is too old OR AI marked as not interesting
        year_ok = published_year is None or published_year >= target_year - 1
        ai_ok = is_interesting

        # Always save the entry to avoid reprocessing, mark as skipped if rejected
        skip_ai = not (year_ok and ai_ok)

        if skip_ai:
            if not year_ok:
                logging.info(
                    f"Rejecting entry (year={published_year}, target={target_year}, grace={target_year - 1}): {title[:50]}..."
                )
            if not ai_ok:
                logging.info(f"Rejecting entry (AI not interested): {title[:50]}...")
        else:
            logging.info(
                f"Saved entry: {title[:50]}... (year={published_year}, interesting={is_interesting})"
            )

        feed_data = FeedData(
            feed_name=feed_name,
            title=title,
            link=f"{url_prefix}{link}",
            additional_info=additional_info,
            hash=entry_hash,
            skip_ai=skip_ai,
            created=datetime.now(timezone),
        )
        session.add(feed_data)

    # Update last fetched time
    now = datetime.now(timezone)
    feed_meta = session.query(FeedMeta).filter_by(feed_name=feed_name).first()
    if not feed_meta:
        feed_meta = FeedMeta(feed_name=feed_name, last_fetched=now)
        session.add(feed_meta)
    else:
        feed_meta.last_fetched = now

    session.commit()
    logging.info(f"Completed fetching feed: {feed_name}")


def generate_rss_feed(feed_name):
    fg = FeedGenerator()
    fg.title(f"{feed_name} RSS Feed")
    fg.link(href=f"http://{feed_name}.rss")
    fg.description(f"This is the RSS feed for {feed_name}")

    feed_entries = (
        session.query(FeedData)
        .filter_by(feed_name=feed_name, skip_ai=False)
        .order_by(FeedData.created.desc())
        .limit(100)
        .all()
    )
    for entry in feed_entries:
        fe = fg.add_entry()
        fe.title(entry.title)
        fe.link(href=entry.link)
        fe.description(entry.additional_info)
        fe.guid(entry.link)

    rss_feed = fg.rss_str(pretty=True)
    return rss_feed.decode("utf-8")


@app.route("/")
def index():
    feed_meta = session.query(FeedMeta).all()
    return render_template("index.html", feeds=feed_meta)


@app.route("/<feed_name>/rss.xml")
def rss_feed(feed_name):
    try:
        rss_feed = generate_rss_feed(feed_name)
        return Response(rss_feed, mimetype="application/rss+xml")
    except Exception as e:
        logging.error(f"Error generating RSS feed for {feed_name}: {e}")
        return str(e), 500


@app.route("/fetch_all_feeds", methods=["POST"])
def fetch_all_feeds():
    try:
        for feed in config["feeds"]:
            parse_and_store_feed(feed["name"], feed["url_to_fetch"], feed["url_prefix"])
        return "All feeds fetched successfully", 200
    except Exception as e:
        logging.error(f"Error fetching all feeds: {e}")
        return str(e), 500


@app.route("/fetch_feed", methods=["POST"])
def fetch_feed():
    feed_name = request.form.get("feed_name")
    feed = next((f for f in config["feeds"] if f["name"] == feed_name), None)
    if not feed:
        return "Feed not found", 404

    try:
        parse_and_store_feed(feed["name"], feed["url_to_fetch"], feed["url_prefix"])
        return redirect(url_for("index"))
    except Exception as e:
        logging.error(f"Error fetching feed {feed_name}: {e}")
        return str(e), 500


@app.route("/clean", methods=["POST"])
def clean_feed():
    feed_name = request.form.get("feed_name")
    if not feed_name:
        return "Feed name parameter is missing", 400

    try:
        session.query(FeedData).filter_by(feed_name=feed_name).delete()
        session.commit()
        return redirect(url_for("index"))
    except Exception as e:
        logging.error(f"Error cleaning feed {feed_name}: {e}")
        return str(e), 500


def cleanup_old_entries():
    """Delete entries older than 30 days."""
    cutoff = datetime.now(timezone) - timedelta(days=30)
    deleted = session.query(FeedData).filter(FeedData.created < cutoff).delete()
    session.commit()
    if deleted:
        logging.info(f"Cleaned up {deleted} entries older than 30 days")


def schedule_jobs():
    scheduler = BackgroundScheduler()
    for feed in config["feeds"]:
        cron_expression = feed["cron"]
        # Check if cron expression has 6 fields (includes seconds)
        if len(cron_expression.split()) == 6:
            # Use interval trigger for second-level schedules
            # Format: "*/20 * * * * *" - first field is seconds
            seconds_field = cron_expression.split()[0]
            seconds = int(seconds_field.lstrip("*/"))
            scheduler.add_job(
                func=parse_and_store_feed,
                trigger=IntervalTrigger(seconds=seconds),
                args=[feed["name"], feed["url_to_fetch"], feed["url_prefix"]],
            )
        else:
            scheduler.add_job(
                func=parse_and_store_feed,
                trigger=CronTrigger.from_crontab(cron_expression),
                args=[feed["name"], feed["url_to_fetch"], feed["url_prefix"]],
            )

    # Cleanup old entries daily at 3am
    scheduler.add_job(
        func=cleanup_old_entries,
        trigger=CronTrigger(hour=3, minute=0),
    )

    scheduler.start()


if __name__ == "__main__":
    schedule_jobs()
    # app.run(host='localhost', port=8088)
    parser = argparse.ArgumentParser(
        description="Run the RSS feed generator web application."
    )
    parser.add_argument(
        "--host", type=str, default="localhost", help="Host to run the web application"
    )
    parser.add_argument(
        "--port", type=int, default=8080, help="Port to run the web application"
    )
    args = parser.parse_args()

    # app.run(host=args.host, port=args.port)
    serve(app, host=args.host, port=args.port)
