import asyncio
import json
import os
import httpx
from playwright.async_api import async_playwright
from datetime import datetime

LARAVEL_URL = os.environ.get('LARAVEL_URL', '').rstrip('/')
IMPORT_KEY  = os.environ.get('IMPORT_KEY', '')
FB_COOKIES  = os.environ.get('FB_COOKIES', '')
FB_PAGES    = os.environ.get('FB_PAGES', '')   # comma-separated page usernames
REGION      = os.environ.get('REGION', 'nacc')

def parse_cookies() -> list:
    """Convert EditThisCookie JSON array to Playwright cookie format."""
    if not FB_COOKIES:
        return []
    try:
        raw = json.loads(FB_COOKIES)
        if isinstance(raw, list):
            cookies = []
            for c in raw:
                cookie = {
                    'name'   : c['name'],
                    'value'  : c['value'],
                    'domain' : c.get('domain', '.facebook.com'),
                    'path'   : c.get('path', '/'),
                    'secure' : c.get('secure', True),
                    'httpOnly': c.get('httpOnly', False),
                }
                cookies.append(cookie)
            return cookies
        return []
    except Exception as e:
        print(f'Cookie parse error: {e}')
        return []

async def scrape_page(page, page_name: str) -> list:
    """Scrape a Facebook public page using Playwright."""
    posts = []
    url   = f'https://www.facebook.com/{page_name}'
    print(f'Scraping {url}...')

    try:
        await page.goto(url, wait_until='domcontentloaded', timeout=30000)
        # Wait for feed to load
        await page.wait_for_timeout(3000)

        # Scroll down a bit to load more posts
        await page.evaluate('window.scrollBy(0, 800)')
        await page.wait_for_timeout(2000)

        # Extract posts via JavaScript evaluation
        extracted = await page.evaluate('''() => {
            const posts = [];

            // Try multiple selectors for post containers
            const selectors = [
                'div[data-pagelet^="FeedUnit"]',
                'div[role="article"]',
                'div[data-ad-preview="message"]',
            ];

            let items = [];
            for (const sel of selectors) {
                items = Array.from(document.querySelectorAll(sel));
                if (items.length > 0) break;
            }

            items.slice(0, 10).forEach(item => {
                // Get text content
                const textEl = item.querySelector('div[data-ad-comet-preview="message"]') ||
                               item.querySelector('div[data-ad-preview="message"]') ||
                               item.querySelector('[dir="auto"]');
                const content = textEl ? textEl.innerText.trim() : '';

                // Get image
                const imgEl = item.querySelector('img[src*="scontent"]') ||
                              item.querySelector('img[src*="fbcdn"]');
                const image = imgEl ? imgEl.src : null;

                // Get post URL
                const linkEl = item.querySelector('a[href*="/posts/"]') ||
                               item.querySelector('a[href*="/permalink/"]') ||
                               item.querySelector('a[href*="story_fbid"]');
                const postUrl = linkEl ? linkEl.href : '';

                // Get timestamp
                const timeEl = item.querySelector('abbr') ||
                               item.querySelector('a[role="link"] span');
                const timestamp = timeEl ? timeEl.getAttribute('data-utime') || timeEl.innerText : '';

                // Get post ID from URL
                let postId = '';
                if (postUrl) {
                    const match = postUrl.match(/\/posts\/(\d+)/) ||
                                  postUrl.match(/story_fbid=(\d+)/) ||
                                  postUrl.match(/permalink\/(\d+)/);
                    postId = match ? match[1] : btoa(postUrl).slice(0, 20);
                }
                if (!postId && content) {
                    postId = btoa(content.slice(0, 30)).replace(/[^a-zA-Z0-9]/g, '').slice(0, 20);
                }

                if (content || image) {
                    posts.push({ id: postId, content, image, postUrl, timestamp });
                }
            });

            return posts;
        }''')

        print(f'Found {len(extracted)} posts for {page_name}')

        # Get page name from title
        title = await page.title()
        page_display_name = title.split('|')[0].strip() if '|' in title else page_name

        for p in extracted:
            posts.append({
                'external_id': f'fb_{page_name}_{p["id"]}' if p['id'] else f'fb_{page_name}_{hash(p["content"][:30])}',
                'type'       : 'facebook',
                'region'     : REGION,
                'page_id'    : page_name,
                'page_name'  : page_display_name,
                'content'    : p['content'],
                'image'      : p['image'],
                'images'     : [p['image']] if p['image'] else [],
                'reactions'  : {'like': 0, 'comment': 0, 'share': 0},
                'post_url'   : p['postUrl'],
                'posted_at'  : p['timestamp'] or datetime.utcnow().isoformat(),
            })

    except Exception as e:
        print(f'Error scraping {page_name}: {type(e).__name__}: {e}')

    return posts


async def send_to_laravel(posts: list) -> bool:
    """POST scraped posts to Laravel import endpoint."""
    if not posts:
        print('No posts to send')
        return True
    if not LARAVEL_URL or not IMPORT_KEY:
        print('ERROR: LARAVEL_URL or IMPORT_KEY not set')
        return False

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f'{LARAVEL_URL}/api/social/import',
                json={'posts': posts},
                headers={
                    'X-Import-Key': IMPORT_KEY,
                    'Content-Type': 'application/json',
                },
            )
            print(f'Laravel import response: {resp.status_code} — {resp.text[:200]}')
            return resp.status_code == 200
    except Exception as e:
        print(f'Failed to send to Laravel: {e}')
        return False


async def main():
    pages = [p.strip() for p in FB_PAGES.split(',') if p.strip()]
    if not pages:
        print('No FB_PAGES configured, nothing to do.')
        return

    print(f'Pages to scrape: {pages}')
    cookies = parse_cookies()
    print(f'Cookies loaded: {len(cookies)}')

    all_posts = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
        )
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            viewport={'width': 1280, 'height': 800},
            locale='en-US',
        )

        if cookies:
            await context.add_cookies(cookies)
            print('Cookies set on browser context')

        page = await context.new_page()

        for page_name in pages:
            posts = await scrape_page(page, page_name)
            all_posts.extend(posts)
            await asyncio.sleep(2)  # small delay between pages

        await browser.close()

    print(f'Total posts scraped: {len(all_posts)}')
    await send_to_laravel(all_posts)


if __name__ == '__main__':
    asyncio.run(main())
