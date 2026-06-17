"""All X/Twitter DOM selectors live here.

X changes its markup periodically. When scraping breaks, this is the one file to fix.
"""

# Login / session detection
PROFILE_LINK = '[data-testid="AppTabBar_Profile_Link"]'
ACCOUNT_SWITCHER = '[data-testid="SideNav_AccountSwitcher_Button"]'

# Following list
USER_CELL = '[data-testid="UserCell"]'

# Tweets
TWEET = 'article[data-testid="tweet"]'
TWEET_TEXT = '[data-testid="tweetText"]'
SOCIAL_CONTEXT = '[data-testid="socialContext"]'   # "X reposted" banner
USER_NAME = '[data-testid="User-Name"]'
LIKE = '[data-testid="like"]'
RETWEET = '[data-testid="retweet"]'

# URLs
BASE = "https://x.com"


def profile_url(handle: str) -> str:
    return f"{BASE}/{handle.lstrip('@')}"


def following_url(handle: str) -> str:
    return f"{BASE}/{handle.lstrip('@')}/following"


def with_replies_url(handle: str) -> str:
    return f"{BASE}/{handle.lstrip('@')}/with_replies"
