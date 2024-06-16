import json
import argparse
import requests
import hashlib
import replicate
import os
import pytz
import logging
from datetime import datetime
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from flask import Flask, Response, request, render_template, redirect, url_for
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker
from croniter import croniter
from waitress import serve

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s - %(message)s',
    handlers=[
        logging.FileHandler("app.log"),
        logging.StreamHandler()
    ]
)

logging.getLogger('httpx').setLevel(logging.WARNING)

app = Flask(__name__)
Base = declarative_base()

# Define the SQLite database model
class FeedData(Base):
    __tablename__ = 'feed_data'
    id = Column(Integer, primary_key=True)
    feed_name = Column(String)
    title = Column(String)
    link = Column(String)
    additional_info = Column(Text)
    hash = Column(String, unique=True)
    skip_ai = Column(Boolean, default=False)
    created = Column(DateTime)

class FeedMeta(Base):
    __tablename__ = 'feed_meta'
    id = Column(Integer, primary_key=True)
    feed_name = Column(String, unique=True)
    last_fetched = Column(DateTime)


# Setup the SQLite database
engine = create_engine('sqlite:///feeds.db')
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
session = Session()

# Load configuration
with open('config.json') as config_file:
    config = json.load(config_file)

# Get the API token from environment variable
replicate_api_token = os.getenv("REPLICATE_API_TOKEN")
if not replicate_api_token:
    raise ValueError("REPLICATE_API_TOKEN environment variable is not set")


# Get the timezone from the configuration file
timezone = pytz.timezone(config.get("timezone", "UTC"))

def is_interesting_title(title):
    try:
        input = {
            "prompt": f"""Your task is to determine if a book title pertains to one or more of the following topics:
        - software development
        - machine learning
        - artificial intelligence (AI)
        - DevOps
        - programming languages
        - front-end development
        - software development best practices
        - large language models (such as chatgpt, llama)
        - data science
        - networking
        - software architecture
        - technical leadership

        You should only respond with "yes" or "no". The title must strictly relate to the specified topics and may cover multiple topics listed. 
        Do not include any additional text, explanations, or comments.


        This is the book title: "{title}"
        """,
        "system_prompt":"You are an expert and helpful software developer and data expert"}

        output = replicate.run(
            "meta/meta-llama-3-70b-instruct",
            input=input
        )

        # Check if 'yes' or 'no' appears in the response
        response = ' '.join(output).strip().lower()
        is_interesting = 'yes' in response

        # Log the response and the result
        logging.info(f"full AI Response: {output}")
        logging.info(f"AI Response: {response}")
        logging.info(f"Parsed to {'True' if is_interesting else 'False'}")
        
        return is_interesting
    except Exception as e:
        logging.error(f"Error while checking title with AI: {e}")
        return False


def fetch_html(url):
    try:
        response = requests.get(url)
        response.raise_for_status()  # Raise an error for bad status codes
        return response.text
    except requests.RequestException as e:
        logging.error(f"Error fetching HTML from {url}: {e}")
        return None

def generate_hash(title, link, additional_info):
    hash_input = f'{title}{link}{additional_info}'.encode('utf-8')
    return hashlib.sha256(hash_input).hexdigest()


def parse_and_store_feed(feed_name, url_to_fetch, url_prefix):
    logging.info(f"Fetching feed: {feed_name} from {url_to_fetch}")
    html_content = fetch_html(url_to_fetch)
    if html_content is None:
        logging.error(f"Failed to fetch feed: {feed_name}")
        return
    soup = BeautifulSoup(html_content, 'html.parser')

    rows = soup.find_all('div', class_='row')
    for row in rows:
        article = row.find('div', class_='article')
        if not article:
            continue

        h1_tag = article.find('h1')
        if not h1_tag:
            continue

        title_tag = h1_tag.find('a', class_='title-link')
        if not title_tag:
            continue

        title = title_tag.get_text(strip=True)
        link = title_tag['href']

        text_center_divs = article.find_all('div', class_='text-center')
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
            continue

        # Check if the year is 2024 and language is English or not present
        if '2024' not in additional_info:
            continue
        if 'English' not in additional_info:
            continue

        logging.info(f"Processing: {title}")
        ai_ok = is_interesting_title(title)
        if not ai_ok:
            logging.info(f"Title not selected by AI: {title}")

        
        feed_data = FeedData(
            feed_name=feed_name, 
            title=title, 
            link=f'{url_prefix}{link}', 
            additional_info=additional_info,
            hash=entry_hash,
            skip_ai=not ai_ok,
            created=datetime.now(timezone)
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
    fg.title(f'{feed_name} RSS Feed')
    fg.link(href=f'http://{feed_name}.rss')
    fg.description(f'This is the RSS feed for {feed_name}')

    feed_entries = session.query(FeedData).filter_by(feed_name=feed_name, skip_ai=False).order_by(FeedData.created.desc()).limit(100).all()
    for entry in feed_entries:
        fe = fg.add_entry()
        fe.title(entry.title)
        fe.link(href=entry.link)
        fe.description(entry.additional_info)
        fe.guid(entry.link) 

    rss_feed = fg.rss_str(pretty=True)
    return rss_feed.decode('utf-8')

@app.route('/')
def index():
    feed_meta = session.query(FeedMeta).all()
    return render_template('index.html', feeds=feed_meta)

@app.route('/<feed_name>/rss.xml')
def rss_feed(feed_name):
    try:
        rss_feed = generate_rss_feed(feed_name)
        return Response(rss_feed, mimetype='application/rss+xml')
    except Exception as e:
        logging.error(f"Error generating RSS feed for {feed_name}: {e}")
        return str(e), 500

@app.route('/fetch_all_feeds', methods=['POST'])
def fetch_all_feeds():
    try:
        for feed in config['feeds']:
            parse_and_store_feed(feed['name'], feed['url_to_fetch'], feed['url_prefix'])
        return "All feeds fetched successfully", 200
    except Exception as e:
        logging.error(f"Error fetching all feeds: {e}")
        return str(e), 500

@app.route('/fetch_feed', methods=['POST'])
def fetch_feed():
    feed_name = request.form.get('feed_name')
    feed = next((f for f in config['feeds'] if f['name'] == feed_name), None)
    if not feed:
        return "Feed not found", 404

    try:
        parse_and_store_feed(feed['name'], feed['url_to_fetch'], feed['url_prefix'])
        return redirect(url_for('index'))
    except Exception as e:
        logging.error(f"Error fetching feed {feed_name}: {e}")
        return str(e), 500

@app.route('/clean', methods=['POST'])
def clean_feed():
    feed_name = request.form.get('feed_name')
    if not feed_name:
        return "Feed name parameter is missing", 400

    try:
        session.query(FeedData).filter_by(feed_name=feed_name).delete()
        session.commit()
        return redirect(url_for('index'))
    except Exception as e:
        logging.error(f"Error cleaning feed {feed_name}: {e}")
        return str(e), 500

def schedule_jobs():
    scheduler = BackgroundScheduler()
    for feed in config['feeds']:
        cron_expression = feed['cron']
        scheduler.add_job(
            func=parse_and_store_feed,
            trigger=CronTrigger.from_crontab(cron_expression),
            args=[feed['name'], feed['url_to_fetch'], feed['url_prefix']]
        )
    scheduler.start()

if __name__ == '__main__':
    schedule_jobs()
    #app.run(host='localhost', port=8088)
    parser = argparse.ArgumentParser(description='Run the RSS feed generator web application.')
    parser.add_argument('--host', type=str, default='localhost', help='Host to run the web application')
    parser.add_argument('--port', type=int, default=8080, help='Port to run the web application')
    args = parser.parse_args()

    #app.run(host=args.host, port=args.port)
    serve(app, host=args.host, port=args.port)

