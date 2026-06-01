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

    for sel in ['[aria-label="Close"]', '[data-testid="cookie-policy-manage-dialog-accept-button"]']:
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

# JavaScript to extract posts — using string methods instead of regex literals
JS_EXTRACT = """
() => {
    function extractPostId(href) {
        if (!href) return '';
        var idx, after, parts;
        if (href.indexOf('/posts/') !== -1) {
            parts = href.split('/posts/');
            if (parts[1]) {
                var id = parts[1].split('?')[0].split('/')[0];
                return id.replace(/[^0-9]/g, '');
            }
        }
        idx = href.indexOf('story_fbid');
        if (idx !== -1) {
            after = href.substring(idx + 10).replace('%3D','=');
            if (after.charAt(0) === '=') {
                return after.substring(1).split('&')[0].split('%')[0].replace(/[^0-9]/g,'');
            }
        }
        if (href.indexOf('/permalink/') !== -1) {
            parts = href.split('/permalink/');
            if (parts[1]) return parts[1].split('?')[0].split('/')[0].replace(/[^0-9]/g,'');
        }
        return '';
    }

    function hashStr(text) {
        var hash = 0;
        var str = text.substring(0, 100);
        for (var i = 0; i < str.length; i++) {
            hash = ((hash << 5) - hash) + str.charCodeAt(i);
            hash = hash & hash;
        }
        return Math.abs(hash).toString(36);
    }

    function getBestImage(item) {
        var imgs = item.querySelectorAll('img');
        var fallback = null;
        for (var j = 0; j < imgs.length; j++) {
            var img = imgs[j];
            var src = img.src || '';
            var alt = (img.alt || '').toLowerCase();
            if (!src) continue;
            if (src.indexOf('emoji') !== -1) continue;
            if (src.indexOf('p40x40') !== -1 || src.indexOf('p50x50') !== -1 ||
                src.indexOf('p80x80') !== -1 || src.indexOf('p100x100') !== -1) continue;
            if (alt.indexOf('profile picture') !== -1 || alt.indexOf('cover photo') !== -1) continue;
            if (src.indexOf('/16/') !== -1 || src.indexOf('/20/') !== -1 ||
                src.indexOf('/24/') !== -1 || src.indexOf('/32/') !== -1) continue;
            if (src.indexOf('scontent') !== -1 || src.indexOf('fbcdn') !== -1) {
                var isLarge = img.naturalWidth > 200 || img.width > 200 ||
                    src.indexOf('p720x') !== -1 || src.indexOf('p526x') !== -1 ||
                    src.indexOf('p480x') !== -1;
                if (isLarge) return src;
                if (!fallback) fallback = src;
            }
        }
        return fallback;
    }

    var posts = [];
    var tried = [];
    var selectors = [
        'div[data-ad-preview="message"]',
        'div[role="article"]',
        'div[data-pagelet^="FeedUnit"]',
        'div[data-testid="post_message"]'
    ];

    for (var s = 0; s < selectors.length; s++) {
        var sel = selectors[s];
        var els = document.querySelectorAll(sel);
        tried.push(sel + ': ' + els.length);
        if (els.length === 0 || els.length >= 100) continue;

        var items = Array.prototype.slice.call(els, 0, 10);
        for (var i = 0; i < items.length; i++) {
            var item = items[i];

            // Get text — use innerText directly (most reliable)
            var text = (item.innerText || '').trim().substring(0, 1000);
            // Remove trailing "See more" / "See less"
            if (text.lastIndexOf('See more') === text.length - 8) text = text.substring(0, text.length - 8).trim();
            if (text.lastIndexOf('See less') === text.length - 8) text = text.substring(0, text.length - 8).trim();

            var image = getBestImage(item);

            // Get post URL + ID
            var postUrl = '', postId = '';
            var links = item.querySelectorAll('a[href]');
            for (var k = 0; k < links.length; k++) {
                var href = links[k].href || '';
                var pid = extractPostId(href);
                if (pid) { postId = pid; postUrl = href; break; }
            }
            if (!postId && text) postId = hashStr(text);
            if (!postId) postId = 'idx_' + i + '_' + Date.now();

            if (text.length > 20 || image) {
                posts.push({ content: text, image: image, postUrl: postUrl, id: postId });
            }
        }
        if (posts.length > 0) break;
    }
    return { posts: posts, tried: tried };
}
"""
() => {
    function extractPostId(href) {
        if (!href) return '';
        var parts;
        // /posts/123456
        if (href.indexOf('/posts/') !== -1) {
            parts = href.split('/posts/');
            if (parts[1]) return parts[1].split('?')[0].split('/')[0].replace(/[^0-9]/g,'');
        }
        // story_fbid=123456 or story_fbid%3D123456
        var fbidIdx = href.indexOf('story_fbid');
        if (fbidIdx !== -1) {
            var after = href.substring(fbidIdx + 10);
            after = after.replace('%3D','=');
            if (after[0] === '=') {
                return after.substring(1).split('&')[0].split('%')[0].replace(/[^0-9]/g,'');
            }
        }
        // /permalink/123456
        if (href.indexOf('/permalink/') !== -1) {
            parts = href.split('/permalink/');
            if (parts[1]) return parts[1].split('?')[0].split('/')[0].replace(/[^0-9]/g,'');
        }
        return '';
    }

    function hashText(text) {
        var hash = 0;
        var str = text.substring(0, 100);
        for (var i = 0; i < str.length; i++) {
            hash = ((hash << 5) - hash) + str.charCodeAt(i);
            hash = hash & hash;
        }
        return Math.abs(hash).toString(36);
    }

    function skipImage(src, alt) {
        if (!src) return true;
        var skipPaths = ['emoji', '/icon', '/16/', '/20/', '/24/', '/32/', '/40/', '/48/',
                         'p40x40', 'p50x50', 'p80x80', 'p100x100'];
        for (var i = 0; i < skipPaths.length; i++) {
            if (src.indexOf(skipPaths[i]) !== -1) return true;
        }
        if (alt && (alt.toLowerCase().indexOf('profile picture') !== -1 ||
                    alt.toLowerCase().indexOf('cover photo') !== -1)) return true;
        return false;
    }

    var posts = [];
    var tried = [];
    var selectors = [
        'div[role="article"]',
        'div[data-ad-preview="message"]',
        'div[data-pagelet^="FeedUnit"]',
        'div[data-testid="post_message"]'
    ];

    for (var s = 0; s < selectors.length; s++) {
        var sel = selectors[s];
        var els = document.querySelectorAll(sel);
        tried.push(sel + ': ' + els.length);
        if (els.length > 0 && els.length < 100) {
            var items = Array.prototype.slice.call(els, 0, 10);
            for (var i = 0; i < items.length; i++) {
                var item = items[i];

                // Get post text — try specific message container first
                var text = '';
                var msgEl = item.querySelector('[data-ad-comet-preview="message"]') ||
                            item.querySelector('[data-ad-preview="message"]') ||
                            item.querySelector('[data-testid="post_message"]');
                if (msgEl) {
                    text = (msgEl.innerText || '').trim()
                        .replace(/\\nSee less$/, '').replace(/\\nSee more$/, '').trim();
                }
                if (!text) {
                    var clone = item.cloneNode(true);
                    var btns = clone.querySelectorAll('[role="button"], form, nav, footer');
                    for (var b = 0; b < btns.length; b++) btns[b].remove();
                    var lines = (clone.innerText || '').split('\\n')
                        .map(function(l){ return l.trim(); })
                        .filter(function(l){ return l.length > 2; });
                    var start = (lines[0] === 'Author' || lines[0] === 'Sponsored') ? 2 : 1;
                    text = lines.slice(start).join('\\n').trim().substring(0, 1000);
                }

                // Get post image
                var image = null;
                var imgs = item.querySelectorAll('img');
                for (var j = 0; j < imgs.length; j++) {
                    var img = imgs[j];
                    var src = img.src || '';
                    var alt = img.alt || '';
                    if (skipImage(src, alt)) continue;
                    if (src.indexOf('scontent') !== -1 || src.indexOf('fbcdn') !== -1) {
                        var isLarge = img.naturalWidth > 200 || img.width > 200 ||
                            src.indexOf('p720x') !== -1 || src.indexOf('p526x') !== -1 ||
                            src.indexOf('p480x') !== -1 || src.indexOf('_n.jpg') !== -1 ||
                            src.indexOf('_n.png') !== -1;
                        if (isLarge) { image = src; break; }
                        if (!image) image = src;
                    }
                }

                // Get post URL and ID
                var postUrl = '', postId = '';
                var links = item.querySelectorAll('a[href]');
                for (var k = 0; k < links.length; k++) {
                    var href = links[k].href || '';
                    var id = extractPostId(href);
                    if (id) { postId = id; postUrl = href; break; }
                }
                if (!postId && text) postId = hashText(text);
                if (!postId) postId = 'idx_' + i + '_' + Date.now();

                if (text.length > 20 || image) {
                    posts.push({ content: text, image: image, postUrl: postUrl, id: postId });
                }
            }
            if (posts.length > 0) break;
        }
    }
    return { posts: posts, tried: tried };
}
"""

async def scrape_page(page, page_name: str) -> list:
    posts = []

    for base_url in [f'https://www.facebook.com/{page_name}', f'https://m.facebook.com/{page_name}']:
        print(f'Trying {base_url}...')
        try:
            await page.goto(base_url, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(5000)

            await dismiss_popups(page)

            for _ in range(3):
                await page.evaluate('window.scrollBy(0, 800)')
                await page.wait_for_timeout(2000)

            # Click See more buttons
            try:
                see_more = page.locator('div[role="button"]:has-text("See more"), span:has-text("See more")')
                count = await see_more.count()
                for i in range(min(count, 10)):
                    try:
                        await see_more.nth(i).click(timeout=2000)
                        await page.wait_for_timeout(500)
                    except Exception:
                        continue
                print(f'Expanded {count} "See more" buttons')
            except Exception:
                pass

            await page.screenshot(path=f'/tmp/{page_name}.png')
            title = await page.title()
            print(f'Page title: {title}')

            extracted = await page.evaluate(JS_EXTRACT)

            print(f'Selectors tried: {extracted["tried"]}')
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

        for page_name in pages:
            posts = await scrape_page(page, page_name)
            all_posts.extend(posts)
            await asyncio.sleep(2)

        await browser.close()

    print(f'Total posts scraped: {len(all_posts)}')
    await send_to_laravel(all_posts)


if __name__ == '__main__':
    asyncio.run(main())
