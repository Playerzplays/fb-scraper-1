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

    # Try m.facebook.com first — simpler DOM than www
    for base_url in [f'https://m.facebook.com/{page_name}', f'https://www.facebook.com/{page_name}']:
        print(f'Trying {base_url}...')
        try:
            await page.goto(base_url, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(5000)

            # Scroll to trigger lazy loading
            await page.evaluate('window.scrollBy(0, 1000)')
            await page.wait_for_timeout(3000)

            # Save screenshot for debugging
            await page.screenshot(path=f'/tmp/{page_name}.png')
            print(f'Screenshot saved for {page_name}')

            # Get page title
            title = await page.title()
            print(f'Page title: {title}')

            # Try to extract posts via JavaScript
            extracted = await page.evaluate('''() => {
                const posts = [];
                const results = { found: [], tried: [] };

                // Try many different selectors
                const selectors = [
                    // m.facebook.com selectors
                    'article',
                    'div[data-ft]',
                    'div._55wo',
                    'div._1xnd',
                    // www.facebook.com selectors
                    'div[data-pagelet^="FeedUnit"]',
                    'div[role="article"]',
                    'div[data-ad-preview="message"]',
                    'div[data-testid="story-subtitled-story-container"]',
                    // Generic
                    'div[id^="u_0_"]',
                ];

                for (const sel of selectors) {
                    const els = document.querySelectorAll(sel);
                    results.tried.push(`${sel}: ${els.length}`);
                    if (els.length > 0 && els.length < 50) {
                        results.found.push(sel);
                        Array.from(els).slice(0, 10).forEach(item => {
                            const text = item.innerText ? item.innerText.trim().slice(0, 500) : '';
                            const img  = item.querySelector('img');
                            const link = item.querySelector('a[href*="story"]') ||
                                         item.querySelector('a[href*="posts"]') ||
                                         item.querySelector('a[href*="permalink"]');
                            if (text.length > 20 || img) {
                                posts.push({
                                    content : text,
                                    image   : img ? img.src : null,
                                    postUrl : link ? link.href : '',
                                    id      : Math.random().toString(36).slice(2),
                                    selector: sel,
                                });
                            }
                        });
                        if (posts.length > 0) break;
                    }
                }
                return { posts, debug: results };
            }''')

            print(f'Selectors tried: {extracted["debug"]["tried"]}')
            print(f'Selectors found: {extracted["debug"]["found"]}')
            print(f'Posts extracted: {len(extracted["posts"])}')

            if extracted['posts']:
                page_display_name = title.split('|')[0].strip() if '|' in title else page_name
                for p in extracted['posts']:
                    posts.append({
                        'external_id': f'fb_{page_name}_{p["id"]}',
                        'type'       : 'facebook',
                        'region'     : REGION,
                        'page_id'    : page_name,
                        'page_name'  : page_display_name,
                        'content'    : p['content'],
                        'image'      : p['image'],
                        'images'     : [p['image']] if p['image'] else [],
                        'reactions'  : {'like': 0, 'comment': 0, 'share': 0},
                        'post_url'   : p['postUrl'],
                        'posted_at'  : datetime.utcnow().isoformat(),
                    })
                print(f'Done with {base_url} — found {len(posts)} posts')
                break  # stop trying other URLs if we got posts

        except Exception as e:
            print(f'Error with {base_url}: {type(e).__name__}: {e}')
            continue

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
