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

JS_EXTRACT = (
    "() => {"
    "  function extractPostId(href) {"
    "    if (!href) return '';"
    "    var parts, idx, after;"
    "    if (href.indexOf('/posts/') !== -1) {"
    "      parts = href.split('/posts/');"
    "      if (parts[1]) return parts[1].split('?')[0].split('/')[0].replace(/[^0-9]/g,'');"
    "    }"
    "    idx = href.indexOf('story_fbid');"
    "    if (idx !== -1) {"
    "      after = href.substring(idx + 10).replace('%3D','=');"
    "      if (after.charAt(0) === '=') return after.substring(1).split('&')[0].replace(/[^0-9]/g,'');"
    "    }"
    "    if (href.indexOf('/permalink/') !== -1) {"
    "      parts = href.split('/permalink/');"
    "      if (parts[1]) return parts[1].split('?')[0].split('/')[0].replace(/[^0-9]/g,'');"
    "    }"
    "    return '';"
    "  }"
    "  function hashStr(text) {"
    "    var hash = 0, str = text.substring(0, 100);"
    "    for (var i = 0; i < str.length; i++) {"
    "      hash = ((hash << 5) - hash) + str.charCodeAt(i); hash = hash & hash;"
    "    }"
    "    return Math.abs(hash).toString(36);"
    "  }"
    "  function getBestImage(item) {"
    "    var imgs = item.querySelectorAll('img'), fallback = null;"
    "    for (var j = 0; j < imgs.length; j++) {"
    "      var src = imgs[j].src || '', alt = (imgs[j].alt || '').toLowerCase();"
    "      if (!src || src.indexOf('emoji') !== -1) continue;"
    "      if (src.indexOf('p40x40') !== -1 || src.indexOf('p50x50') !== -1 || src.indexOf('p80x80') !== -1) continue;"
    "      if (src.indexOf('/16/') !== -1 || src.indexOf('/20/') !== -1 || src.indexOf('/24/') !== -1 || src.indexOf('/32/') !== -1) continue;"
    "      if (alt.indexOf('profile picture') !== -1 || alt.indexOf('cover photo') !== -1) continue;"
    "      if (src.indexOf('scontent') !== -1 || src.indexOf('fbcdn') !== -1) {"
    "        var large = imgs[j].naturalWidth > 200 || imgs[j].width > 200 ||"
    "          src.indexOf('p720x') !== -1 || src.indexOf('p526x') !== -1 || src.indexOf('p480x') !== -1;"
    "        if (large) return src;"
    "        if (!fallback) fallback = src;"
    "      }"
    "    }"
    "    return fallback;"
    "  }"
    "  var posts = [], tried = [];"
    "  var selectors = ['div[data-ad-preview=\"message\"]','div[role=\"article\"]','div[data-pagelet^=\"FeedUnit\"]'];"
    "  for (var s = 0; s < selectors.length; s++) {"
    "    var sel = selectors[s];"
    "    var els = document.querySelectorAll(sel);"
    "    tried.push(sel + ': ' + els.length);"
    "    if (els.length === 0 || els.length >= 100) continue;"
    "    var items = Array.prototype.slice.call(els, 0, 10);"
    "    for (var i = 0; i < items.length; i++) {"
    "      var item = items[i];"
    "      var text = (item.innerText || '').trim().substring(0, 1000);"
    "      if (text.substring(text.length - 8) === 'See more') text = text.substring(0, text.length - 8).trim();"
    "      if (text.substring(text.length - 8) === 'See less') text = text.substring(0, text.length - 8).trim();"
    "      var image = getBestImage(item);"
    "      var postUrl = '', postId = '';"
    "      var links = item.querySelectorAll('a[href]');"
    "      for (var k = 0; k < links.length; k++) {"
    "        var pid = extractPostId(links[k].href || '');"
    "        if (pid) { postId = pid; postUrl = links[k].href; break; }"
    "      }"
    "      if (!postId && text) postId = hashStr(text);"
    "      if (!postId) postId = 'idx_' + i + '_' + Date.now();"
    "      if (text.length > 20 || image) posts.push({content: text, image: image, postUrl: postUrl, id: postId});"
    "    }"
    "    if (posts.length > 0) break;"
    "  }"
    "  return {posts: posts, tried: tried};"
    "}"
)

def parse_cookies() -> list:
    if not FB_COOKIES:
        return []
    try:
        raw = json.loads(FB_COOKIES)
        if isinstance(raw, list):
            same_site_map = {'no_restriction': 'None', 'lax': 'Lax', 'strict': 'Strict', 'unspecified': 'None'}
            cookies = []
            for c in raw:
                cookie = {
                    'name'    : c['name'],
                    'value'   : c['value'],
                    'domain'  : c.get('domain', '.facebook.com'),
                    'path'    : c.get('path', '/'),
                    'secure'  : c.get('secure', True),
                    'httpOnly': c.get('httpOnly', False),
                    'sameSite': same_site_map.get(c.get('sameSite', 'no_restriction').lower(), 'None'),
                }
                cookies.append(cookie)
            return cookies
        return []
    except Exception as e:
        print(f'Cookie parse error: {e}')
        return []

async def dismiss_popups(page):
    try:
        btn = page.locator('div[role="dialog"] div[role="button"]').first
        if await btn.count() > 0:
            text = await btn.inner_text()
            if 'continue' in text.lower():
                await btn.click(timeout=5000)
                print(f'Clicked: {text}')
                await page.wait_for_timeout(3000)
                return
    except Exception:
        pass
    for sel in ['[aria-label="Close"]', '[data-testid="cookie-policy-manage-dialog-accept-button"]']:
        try:
            el = page.locator(sel)
            if await el.count() > 0:
                await el.first.click(timeout=3000)
                print(f'Dismissed: {sel}')
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
            await dismiss_popups(page)
            for _ in range(6):
                await page.evaluate('window.scrollBy(0, 800)')
                await page.wait_for_timeout(2000)
            try:
                see_more = page.locator('div[role="button"]:has-text("See more")')
                count = await see_more.count()
                for i in range(min(count, 10)):
                    try:
                        await see_more.nth(i).click(timeout=2000)
                        await page.wait_for_timeout(500)
                    except Exception:
                        continue
                print(f'Expanded {count} See more buttons')
            except Exception:
                pass
            await page.screenshot(path=f'/tmp/{page_name}.png')
            title = await page.title()
            print(f'Title: {title}')
            extracted = await page.evaluate(JS_EXTRACT)
            print(f'Selectors: {extracted["tried"]}')
            print(f'Posts: {len(extracted["posts"])}')
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
                print(f'Done: {len(posts)} posts from {base_url}')
                break
        except Exception as e:
            print(f'Error {base_url}: {type(e).__name__}: {e}')
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
                headers={'X-Import-Key': IMPORT_KEY, 'Content-Type': 'application/json'},
            )
            print(f'Laravel: {resp.status_code} — {resp.text[:200]}')
            return resp.status_code == 200
    except Exception as e:
        print(f'Laravel error: {e}')
        return False

async def main():
    pages = [p.strip() for p in FB_PAGES.split(',') if p.strip()]
    if not pages:
        print('No FB_PAGES set')
        return
    print(f'Pages: {pages}')
    cookies = parse_cookies()
    print(f'Cookies: {len(cookies)}')
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
            print('Cookies set')
        page = await context.new_page()
        for page_name in pages:
            posts = await scrape_page(page, page_name)
            all_posts.extend(posts)
            await asyncio.sleep(2)
        await browser.close()
    print(f'Total: {len(all_posts)} posts')
    await send_to_laravel(all_posts)

if __name__ == '__main__':
    asyncio.run(main())
