"""
X Followers Monitor
-------------------
Automates the collection and monitoring of followers for a given X (Twitter) account using Playwright.

- Requires Playwright and Python 3.8+.
- Needs a valid X (Twitter) session cookie (see README for setup).
- Set the username via the X_USERNAME environment variable (no default).
- Usage: python main.py [--debug]
"""
import json
from playwright.sync_api import sync_playwright
import time
from datetime import datetime
import os
import logging
import sys
import requests

# =====================
# Configurable Constants
# =====================

# Selectors (update if X changes their frontend)
CELL_SELECTOR = "div[data-testid=\"cellInnerDiv\"]"
NAME_SELECTOR = (
    "a[role=\"link\"] span.css-1jxf684.r-dnmrzs.r-1udh08x.r-1udbk01.r-3s2u2q.r-bcqeeo.r-1ttztb7.r-qvutc0.r-poiln3 > span"
)
USERNAME_SELECTOR = (
    "div[dir=\"ltr\"].css-146c3p1.r-dnmrzs.r-1udh08x.r-1udbk01.r-3s2u2q.r-bcqeeo.r-1ttztb7.r-qvutc0.r-37j5jr.r-a023e6.r-rjixqe.r-16dba41.r-18u37iz.r-1wvb978 > span"
)
TIMELINE_SELECTOR = "div[aria-label=\"Timeline: Followers\"]"

# Magic numbers as constants
SCROLL_SLEEP_SEC = 1.5
WAIT_NEW_CONTENT_TIMEOUT = 5
WAIT_NEW_CONTENT_INTERVAL = 0.2
NO_NEW_CONTENT_LIMIT = 10
SCROLL_LIMIT = 500
CHECKPOINT_INTERVAL = 15
INITIAL_SELECTOR_TIMEOUT = 15000

COOKIES_FILE = "cookies.json"
OUTPUT_FILE = "followers_data.json"
HISTORY_DIR = "followers_history"
LATEST_FILE = os.path.join(HISTORY_DIR, "latest.json")

# =====================
# Webhook Configuration
# =====================
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# =====================
# Logging Setup
# =====================

def setup_logging(debug=False):
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger(__name__)

# Parse CLI args for debug mode
DEBUG_MODE = "--debug" in sys.argv
logger = setup_logging(DEBUG_MODE)

# =====================
# Username (required)
# =====================
USERNAME = os.environ.get("X_USERNAME")
if not USERNAME:
    logger.error("X_USERNAME environment variable is required. Exiting.")
    sys.exit(1)

# Check for X_COOKIES or cookies.json
COOKIES_ENV = os.environ.get("X_COOKIES")
if not COOKIES_ENV and not os.path.exists(COOKIES_FILE):
    logger.error("X_COOKIES environment variable or cookies.json file is required. Exiting.")
    sys.exit(1)


def load_cookies():
    """Load cookies from file (UTF-8) or environment variable (JSON string)."""
    if os.path.exists(COOKIES_FILE):
        logger.info("Loading cookies from file")
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    elif os.environ.get("X_COOKIES"):
        logger.info("Loading cookies from environment variable")
        cookies_data = os.environ.get("X_COOKIES")
        cookies = json.loads(cookies_data)
        return cookies
    else:
        raise ValueError(
            "No cookies found. Please provide cookies.json or set X_COOKIES environment variable"
        )


def normalize_same_site(cookie):
    """Normalize the 'sameSite' attribute for Playwright compatibility."""
    val = cookie.get("sameSite")
    cookie["sameSite"] = {
        "no_restriction": "None",
        "lax": "Lax",
        "strict": "Strict",
    }.get(str(val).lower() if val is not None else None, "None")
    return cookie


def get_follower_data(page):
    """Extract follower name and username from the loaded page."""
    try:
        return page.evaluate(
            f"""() => {{
            const cells = document.querySelectorAll('{CELL_SELECTOR}');
            const results = [];
            cells.forEach(cell => {{
                const nameElement = cell.querySelector('{NAME_SELECTOR}');
                const usernameElement = cell.querySelector('{USERNAME_SELECTOR}');
                if (nameElement && usernameElement) {{
                    const name = nameElement.innerText.trim();
                    const username = usernameElement.innerText.trim().replace('@', '');
                    if (name && username && !name.includes('@')) {{
                        results.push({{name: name, username: username}});
                    }}
                }}
            }});
            return results;
        }}"""
        )
    except Exception as e:
        logger.error(f"Error extracting follower data: {e}")
        return []


def load_previous_data():
    """Load the most recent followers data from the latest history file, if it exists."""
    if os.path.exists(LATEST_FILE):
        with open(LATEST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def compare_followers(previous_data, current_data):
    """Compare previous and current follower lists, returning new and unfollowed users."""
    if not previous_data:
        return None

    prev_usernames = {f["username"] for f in previous_data["followers"]}
    curr_usernames = {f["username"] for f in current_data["followers"]}

    unfollowed = prev_usernames - curr_usernames
    new_followers = curr_usernames - prev_usernames

    unfollowed_data = [
        f for f in previous_data["followers"] if f["username"] in unfollowed
    ]
    new_follower_data = [
        f for f in current_data["followers"] if f["username"] in new_followers
    ]

    return {
        "unfollowed": unfollowed_data,
        "new_followers": new_follower_data,
        "unfollowed_count": len(unfollowed),
        "new_followers_count": len(new_followers),
    }


def send_to_discord(changes, username):
    """Send a summary of follower changes to a Discord webhook."""
    if not DISCORD_WEBHOOK_URL:
        logger.debug("Discord webhook URL not set. Skipping notification.")
        return

    if not changes:
        logger.debug("No changes to report to Discord.")
        return

    embeds = []

    # Helper to format user lists with proper newlines and length limits
    MAX_EMBED_DESC_LENGTH = 4000  # Discord embed description limit is 4096 characters

    def _format_users(users):
        """Return a newline-separated bullet list truncated to Discord limits."""
        lines = [f"• {u['name']} (@{u['username']})" for u in users]
        description = "\n".join(lines)
        # Truncate long descriptions to stay within Discord limits (safety margin)
        if len(description) > MAX_EMBED_DESC_LENGTH:
            description = description[: MAX_EMBED_DESC_LENGTH - 3] + "..."
        return description

    # Unfollowed users
    if changes["unfollowed_count"] > 0:
        description = _format_users(changes["unfollowed"])
        embeds.append(
            {
                "title": f"❌ {changes['unfollowed_count']} Unfollowed",
                "description": description,
                "color": 15158332,  # Red
            }
        )

    # New followers
    if changes["new_followers_count"] > 0:
        description = _format_users(changes["new_followers"])
        embeds.append(
            {
                "title": f"🎉 {changes['new_followers_count']} New Followers",
                "description": description,
                "color": 3066993,  # Green
            }
        )

    # Net change
    net_change = changes["new_followers_count"] - changes["unfollowed_count"]
    if net_change > 0:
        net_change_text = f"📈 Net Gain: +{net_change}"
        net_color = 3066993  # Green
    elif net_change < 0:
        net_change_text = f"📉 Net Loss: {net_change}"
        net_color = 15158332  # Red
    else:
        net_change_text = "➖ No Net Change"
        net_color = 8359053  # Grey

    embeds.append(
        {"title": net_change_text, "color": net_color}
    )

    data = {
        "username": "X Followers Monitor",
        "avatar_url": "https://developers.elementor.com/docs/assets/img/elementor-placeholder-image.png",
        "embeds": embeds,
    }

    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=data)
        response.raise_for_status()
        logger.info("Successfully sent notification to Discord.")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error sending notification to Discord: {e}")


def save_progress(follower_data, username):
    """Save current follower data to output, latest, and timestamped backup files."""
    os.makedirs(HISTORY_DIR, exist_ok=True)
    logger.info(f"Saving progress for {len(follower_data)} followers")

    followers_with_urls = []
    for name, uname in sorted(follower_data):
        followers_with_urls.append(
            {"name": name, "username": uname, "profile_url": f"https://x.com/{uname}"}
        )

    data = {
        "username": username,
        "timestamp": datetime.now().isoformat(),
        "total_followers": len(follower_data),
        "followers": followers_with_urls,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    with open(LATEST_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = os.path.join(HISTORY_DIR, f"followers_{timestamp}.json")
    with open(backup_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    logger.info(f"Data saved to {OUTPUT_FILE} and {backup_file}")


def smart_scroll(page):
    """Scroll the followers timeline to load more followers."""
    try:
        page.evaluate(
            f"""() => {{
            const currentScroll = window.pageYOffset;
            const viewportHeight = window.innerHeight;
            window.scrollTo(0, currentScroll + viewportHeight * 0.7);
            const timeline = document.querySelector('{TIMELINE_SELECTOR}');
            if (timeline) {{
                timeline.scrollBy(0, 400);
            }}
        }}"""
        )
    except Exception as e:
        logger.warning(f"Scroll JS error: {e}")
    time.sleep(SCROLL_SLEEP_SEC)


def wait_for_new_content(page, old_count, timeout=WAIT_NEW_CONTENT_TIMEOUT):
    """Wait for new follower cells to appear after scrolling, up to a timeout."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            if len(page.query_selector_all(CELL_SELECTOR)) > old_count:
                return True
        except Exception as e:
            logger.debug(f"wait_for_new_content: {e}")
        time.sleep(WAIT_NEW_CONTENT_INTERVAL)
    return False


def scroll_followers_list(page, username):
    """Scroll and collect all followers for the given username."""
    logger.info("Starting follower collection process...")
    try:
        page.wait_for_selector(CELL_SELECTOR, timeout=INITIAL_SELECTOR_TIMEOUT)
        logger.info("Initial followers loaded")
    except Exception as e:
        logger.warning(f"Could not find follower cells: {e}. Trying alternative approach...")

    follower_data = set()
    no_new_content_count = 0
    scroll_count = 0

    initial_follower_data = get_follower_data(page)
    for item in initial_follower_data:
        follower_data.add((item["name"], item["username"]))

    logger.info(f"Initial collection: {len(follower_data)} unique followers")

    while no_new_content_count < NO_NEW_CONTENT_LIMIT and scroll_count < SCROLL_LIMIT:
        scroll_count += 1
        logger.debug(f"Scroll #{scroll_count}")

        try:
            current_cells = len(page.query_selector_all(CELL_SELECTOR))
        except Exception as e:
            logger.warning(f"Error counting cells: {e}")
            current_cells = 0
        smart_scroll(page)
        wait_for_new_content(page, current_cells, timeout=WAIT_NEW_CONTENT_TIMEOUT)

        previous_count = len(follower_data)
        for item in get_follower_data(page):
            follower_data.add((item["name"], item["username"]))

        followers_added = len(follower_data) - previous_count
        logger.debug(f"New followers found: {followers_added}")

        if followers_added == 0:
            no_new_content_count += 1
            logger.debug(f"No new followers ({no_new_content_count}/{NO_NEW_CONTENT_LIMIT})")
        else:
            no_new_content_count = 0

        if scroll_count % CHECKPOINT_INTERVAL == 0:
            save_progress(follower_data, username)
            logger.info(
                f"Progress checkpoint: {len(follower_data)} followers collected"
            )

    logger.info(f"Scrolling completed! Total followers collected: {len(follower_data)}")
    save_progress(follower_data, username)
    return follower_data


def extract_username_from_url(url):
    """Extract the username from a given X (Twitter) profile URL."""
    parts = url.split("/")
    for i, part in enumerate(parts):
        if part == "x.com" and i + 1 < len(parts):
            return parts[i + 1]
    return "unknown"


def main():
    """Main entry point: launches browser, collects followers, compares with previous data, and logs changes."""
    logger.info("Starting X followers monitor")
    logger.info(f"Monitoring followers for username: {USERNAME}")

    # Ensure necessary directories exist
    os.makedirs(HISTORY_DIR, exist_ok=True)
    logger.info(f"Ensured {HISTORY_DIR} directory exists")

    try:
        cookies = load_cookies()
        cookies = [normalize_same_site(cookie) for cookie in cookies]
    except ValueError as e:
        logger.error(f"Error loading cookies: {e}")
        return
    except Exception as e:
        logger.error(f"Unexpected error loading cookies: {e}", exc_info=True)
        return

    try:
        with sync_playwright() as p:
            logger.info("Launching browser in headless mode")
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-web-security",
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                ],
            )
            context = browser.new_context(
                viewport={"width": 1920, "height": 4200},
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            context.add_cookies(cookies)
            page = context.new_page()

            url = f"https://x.com/{USERNAME}/followers"
            username = extract_username_from_url(url)
            logger.info(f"Navigating to {username}'s followers page")

            page.goto(url)
            page.wait_for_load_state("domcontentloaded")
            time.sleep(3)

            if "log in" in page.content().lower():
                logger.error("Not logged in properly. Check your cookies.")
                browser.close()
                return

            logger.info("Page loaded successfully")
            previous_data = load_previous_data()

            try:
                follower_data = scroll_followers_list(page, username)
                current_data = {
                    "username": username,
                    "timestamp": datetime.now().isoformat(),
                    "total_followers": len(follower_data),
                    "followers": [
                        {
                            "name": name,
                            "username": uname,
                            "profile_url": f"https://x.com/{uname}",
                        }
                        for name, uname in sorted(follower_data)
                    ],
                }

                if previous_data:
                    logger.info(f"Comparing with data from {previous_data['timestamp']}")
                    changes = compare_followers(previous_data, current_data)
                    if changes:
                        logger.info("=== CHANGES SINCE LAST RUN ===")
                        if changes["unfollowed_count"] > 0:
                            logger.info(
                                f"❌ {changes['unfollowed_count']} people unfollowed"
                            )
                            for user in changes["unfollowed"]:
                                logger.info(f"  - {user['name']} (@{user['username']})")
                        else:
                            logger.info("✅ No one unfollowed")

                        if changes["new_followers_count"] > 0:
                            logger.info(
                                f"🎉 {changes['new_followers_count']} new followers:"
                            )
                            for user in changes["new_followers"]:
                                logger.info(f"  - {user['name']} (@{user['username']})")
                        else:
                            logger.info("📊 No new followers")

                        net_change = (
                            changes["new_followers_count"] - changes["unfollowed_count"]
                        )
                        if net_change > 0:
                            logger.info(f"📈 Net gain: +{net_change} followers")
                        elif net_change < 0:
                            logger.info(f"📉 Net loss: {net_change} followers")
                        else:
                            logger.info("➖ No net change in followers")
                        
                        send_to_discord(changes, username)

                else:
                    logger.info("First run - no previous data to compare")

            except Exception as e:
                logger.error(f"Error during follower collection: {e}", exc_info=True)

            logger.info("Closing browser")
            browser.close()
    except Exception as e:
        logger.error(f"Fatal error in Playwright session: {e}", exc_info=True)


if __name__ == "__main__":
    main()
