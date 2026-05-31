import asyncio
import json
import os
import httpx
from playwright.async_api import async_playwright
from datetime import datetime

LARAVEL_URL = os.environ.get('LARAVEL_URL', '').rstrip('/')
IMPORT_KEY  = os.environ.get('IMPORT_KEY', '')
FB_COOKIES  = os.environ.get('FB_COOKIES', '')
FB_PAGES    = os.environ.get('FB_PAGES', '')
REGION      = os.environ.get('REGION', 'nacc')

def parse_cookies() -> list:
    if not FB_COOKIES:
        return []
    try:
        raw = json.loads(FB_COOKIES)
        if isinstance(raw, list):
            # Map EditThisCookie sameSite values to Playwright expected values
            same_site_map = {
                'no_restriction': 'None',
                'lax'           : 'Lax',
                'strict'        : 'Strict',
                'unspecified'   : 'None',
            }
            cookies = []
            for c in raw:
                same_site = same_site_map.get(
                    c.get('sameSite', 'no_restriction').lower(), 'None'
                )
                cookie = {
                    'name'    : c['name'],
                    'value'   : c['value'],
                    'domain'  : c.get('domain', '.facebook.com'),
                    'path'    : c.get('path', '/'),
                    'secure'  : c.get('secure', True),
                    'httpOnly': c.get('httpOnly', False),
                    'sameSite': same_site,
                }
                cookies.append(cookie)
            return cookies
        return []
    except Exception as e:
        print(f'Cookie parse error: {e}')
        return []

async def dismiss_popups(page):
    """Handle Facebook popups — click Continue to log in, close others."""

    # First try to click "Continue as [Name]" to actually log in
    try:
        continue_btn = page.locator('div[role="dialog"] div[role="button"]').first
        if await continue_btn.count() > 0:
            text = await continue_btn.inner_text()
            if 'continue' in text.lower():
                await continue_btn.click(timeout=5000)
                print(f'Clicked login confirmation: {text}')
                await page.wait_for_timeout(3000)
                return
    except Exception as e:
        print(f'Continue button not found: {e}')

    # Otherwise dismiss any other popups
    popup_selectors = [
        '[aria-label="Close"]',
        'div[role="dialog"] [aria-label="Close"]',
        '[data-testid="cookie-policy-manage-dialog-accept-button"]',
        'button[data-cookiebanner="accept_button"]',
    ]
    for sel in popup_selectors:
        try:
            el = page.locator(sel)
            if await el.count() > 0:
                await el.first.click(timeout=3000)
                print(f'Dismissed popup: {sel}')
                await page.wait_for_timeout(1500)
        except Exception:
            continue
    await page.keyboard.press('Escape')
    await page.wait_for_timeout(1000)

async def scrape_page(page, page_name: str) -> list:
    posts = []

    for base_url in [f'https://www.facebook.com/{page_name}', f'https://m.facebook.com/{page_name}']:
        print(f'Trying {base_url}...')
        try:
            await page.goto(base_url, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(5000)

            # Dismiss popups before scraping
            await dismiss_popups(page)

            # Scroll multiple times to load more posts
            for _ in range(3):
                await page.evaluate('window.scrollBy(0, 800)')
                await page.wait_for_timeout(2000)

            # Click all "See more" buttons to expand truncated content
            try:
                see_more_buttons = page.locator('div[role="button"]:has-text("See more"), span:has-text("See more")')
                count = await see_more_buttons.count()
                for i in range(min(count, 10)):
                    try:
                        await see_more_buttons.nth(i).click(timeout=2000)
                        await page.wait_for_timeout(500)
                    except Exception:
                        continue
                print(f'Expanded {count} "See more" buttons')
            except Exception:
                pass

            # Screenshot for debugging
            await page.screenshot(path=f'/tmp/{page_name}.png')
            print(f'Screenshot saved')

            title = await page.title()
            print(f'Page title: {title}')

            extracted = await page.evaluate('''() => {
                const posts = [];
                const results = { found: [], tried: [] };

                const selectors = [
                    'div[data-ad-preview="message"]',
                    'div[role="article"]',
                    'div[data-pagelet^="FeedUnit"]',
                    'div[aria-posinset]',
                    'div[data-testid="post_message"]',
                ];

                for (const sel of selectors) {
                    const els = document.querySelectorAll(sel);
                    results.tried.push(`${sel}: ${els.length}`);
                    if (els.length > 0 && els.length < 50) {
                        results.found.push(sel);
                        Array.from(els).slice(0, 10).forEach((item, idx) => {
                            const text = item.innerText ? item.innerText.trim().slice(0, 1000) : '';

                            // Get actual post image — exclude emoji/icon images
                            // Real post images have width > 100 or are from specific CDN paths
                            let image = null;
                            const imgs = item.querySelectorAll('img[src*="scontent"], img[src*="fbcdn"]');
                            for (const img of imgs) {
                                const src = img.src || '';
                                // Skip emoji and small icon images
                                if (src.includes('emoji') || src.includes('icon') || 
                                    src.includes('/16/') || src.includes('/20/') ||
                                    src.includes('/24/') || src.includes('/32/')) continue;
                                // Prefer images with larger dimensions
                                if (img.naturalWidth > 100 || img.width > 100 || 
                                    src.includes('_n.') || src.includes('p720x720') ||
                                    src.includes('p526x296') || src.includes('p480x')) {
                                    image = src;
                                    break;
                                }
                                // Accept any scontent image as fallback
                                if (!image && src.includes('scontent')) {
                                    image = src;
                                }
                            }

                            // Get post URL
                            let postUrl = '';
                            let postId  = '';
                            const links = item.querySelectorAll('a[href]');
                            for (const link of links) {
                                const href = link.href || '';
                                const match = href.match(/\/posts\/(\d+)/) ||
                                              href.match(/story_fbid[=%](\d+)/) ||
                                              href.match(/permalink\/(\d+)/);
                                if (match) {
                                    postId  = match[1];
                                    postUrl = href;
                                    break;
                                }
                            }

                            // Use content hash as stable fallback ID
                            if (!postId && text) {
                                postId = btoa(unescape(encodeURIComponent(text.slice(0, 50)))).replace(/[^a-zA-Z0-9]/g, '').slice(0, 24);
                            }
                            if (!postId) postId = `idx_${idx}`;

                            if (text.length > 20 || image) {
                                posts.push({
                                    content : text,
                                    image   : image,
                                    postUrl : postUrl,
                                    id      : postId,
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
                        'post_url'   : p['postUrl'] or None,
                        'posted_at'  : datetime.utcnow().isoformat(),
                    })
                print(f'Done — found {len(posts)} posts from {base_url}')
                break

        except Exception as e:
            print(f'Error with {base_url}: {type(e).__name__}: {e}')
            continue

    return posts


async def send_to_laravel(posts: list) -> bool:
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
        print('No FB_PAGES configured.')
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

        # Navigate to facebook.com first so cookies are applied to the right domain
        if cookies:
            await page.goto('https://www.facebook.com', wait_until='domcontentloaded', timeout=30000)
            await context.add_cookies(cookies)
            print(f'Navigated to facebook.com and set {len(cookies)} cookies')
            await page.wait_for_timeout(2000)

        for page_name in pages:
            posts = await scrape_page(page, page_name)
            all_posts.extend(posts)
            await asyncio.sleep(2)

        await browser.close()

    print(f'Total posts scraped: {len(all_posts)}')
    await send_to_laravel(all_posts)


if __name__ == '__main__':
    asyncio.run(main())
