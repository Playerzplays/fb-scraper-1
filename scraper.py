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
    "  function getBestImage(item) {"
    "    var imgs=item.querySelectorAll('img'),fallback=null;"
    "    for(var j=0;j<imgs.length;j++){"
    "      var src=imgs[j].src||'',alt=(imgs[j].alt||'').toLowerCase();"
    "      if(!src||src.indexOf('emoji')!==-1||src.indexOf('rsrc.php')!==-1) continue;"
    "      if(src.indexOf('p40x40')!==-1||src.indexOf('p50x50')!==-1||src.indexOf('p80x80')!==-1) continue;"
    "      if(src.indexOf('/16/')!==-1||src.indexOf('/20/')!==-1||src.indexOf('/24/')!==-1||src.indexOf('/32/')!==-1||src.indexOf('/40/')!==-1) continue;"
    "      if(alt.indexOf('profile picture')!==-1||alt.indexOf('cover photo')!==-1) continue;"
    "      if(src.indexOf('scontent')!==-1||src.indexOf('fbcdn')!==-1){"
    "        var w=imgs[j].naturalWidth||imgs[j].width||0;"
    "        if(w>200||src.indexOf('p720x')!==-1||src.indexOf('p526x')!==-1||src.indexOf('p480x')!==-1||src.indexOf('_n.jpg')!==-1) return src;"
    "        if(!fallback) fallback=src;"
    "      }"
    "    }"
    "    return fallback;"
    "  }"
    "  function getDate(item){"
    "    var abbr=item.querySelector('abbr[data-utime]');"
    "    if(abbr){var ts=parseInt(abbr.getAttribute('data-utime'));if(ts) return new Date(ts*1000).toISOString();}"
    "    var t=item.querySelector('time');if(t) return t.getAttribute('datetime')||'';"
    "    return '';"
    "  }"
    "  function hashStr(t){var h=0;for(var i=0;i<Math.min(t.length,100);i++){h=((h<<5)-h)+t.charCodeAt(i);h=h&h;}return Math.abs(h).toString(36);}"
    "  function getPostId(item){"
    "    var links=item.querySelectorAll('a[href]');"
    "    for(var k=0;k<links.length;k++){"
    "      var href=links[k].href||'';"
    "      if(href.indexOf('/posts/')!==-1){var p=href.split('/posts/');if(p[1]) return p[1].split('?')[0].replace(/[^0-9]/g,'');}"
    "      var idx=href.indexOf('story_fbid');if(idx!==-1){var a=href.substring(idx+10).replace('%3D','=');if(a.charAt(0)==='=') return a.substring(1).split('&')[0].replace(/[^0-9]/g,'');}"
    "      if(href.indexOf('/permalink/')!==-1){var p2=href.split('/permalink/');if(p2[1]) return p2[1].split('?')[0].replace(/[^0-9]/g,'');}"
    "    }"
    "    return '';"
    "  }"
    "  var posts=[],tried=[];"
    "  var sels=['div[role=\"article\"]','article','div[data-ft]'];"
    "  for(var s=0;s<sels.length;s++){"
    "    var els=document.querySelectorAll(sels[s]);"
    "    tried.push(sels[s]+': '+els.length);"
    "    if(els.length===0||els.length>=100) continue;"
    "    var items=Array.prototype.slice.call(els,0,15);"
    "    for(var i=0;i<items.length;i++){"
    "      var item=items[i];"
    "      var image=getBestImage(item);"
    "      var date=getDate(item);"
    "      var text=(item.innerText||'').trim().substring(0,1500);"
    "      if(text.substring(text.length-8)==='See more') text=text.substring(0,text.length-8).trim();"
    "      if(text.substring(text.length-8)==='See less') text=text.substring(0,text.length-8).trim();"
    "      if(text.indexOf('Author\\n')===0) text=text.substring(8).trim();"
    "      text=text.split('\\n').filter(function(l){return l.trim()!=='See translation'&&l.trim()!=='Rate this translation'&&l.trim()!=='Sponsored';}).join('\\n').trim();"
    "      var lines=text.split('\\n').filter(function(l){return l.trim().length>0;});"
    "      var last=lines[lines.length-1]||'';"
    "      var tsRe=new RegExp('^[0-9]+(m|h|d|w)$');"
    "      if(lines.length<=4&&tsRe.test(last.trim())&&text.length<200) continue;"
    "      if(text.length<30&&!image) continue;"
    "      var postId=getPostId(item);"
    "      if(!postId&&text) postId=hashStr(text);"
    "      if(!postId) postId='fb_'+i+'_'+Date.now();"
    "      var dup=false;for(var d=0;d<posts.length;d++){if(posts[d].id===postId){dup=true;break;}}"
    "      if(!dup) posts.push({text:text,image:image,id:postId,date:date});"
    "    }"
    "  }"
    "  return{posts:posts,tried:tried};"
    "}"
)


def parse_cookies() -> list:
    if not FB_COOKIES:
        return []
    try:
        raw = json.loads(FB_COOKIES)
        if isinstance(raw, list):
            same_site_map = {'no_restriction': 'None', 'lax': 'Lax', 'strict': 'Strict', 'unspecified': 'None'}
            return [{
                'name'    : c['name'],
                'value'   : c['value'],
                'domain'  : c.get('domain', '.facebook.com'),
                'path'    : c.get('path', '/'),
                'secure'  : c.get('secure', True),
                'httpOnly': c.get('httpOnly', False),
                'sameSite': same_site_map.get(c.get('sameSite', 'no_restriction').lower(), 'None'),
            } for c in raw if 'name' in c and 'value' in c]
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
                await page.wait_for_timeout(1500)
        except Exception:
            continue
    await page.keyboard.press('Escape')
    await page.wait_for_timeout(1000)


async def scrape_page(page, page_name: str) -> list:
    posts = []
    for base_url in [
        f'https://m.facebook.com/{page_name}',
        f'https://www.facebook.com/{page_name}',
    ]:
        print(f'Trying {base_url}...')
        try:
            await page.goto(base_url, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(5000)
            await dismiss_popups(page)

            max_attempts = 15
            for attempt in range(max_attempts):
                await page.evaluate('window.scrollBy(0, 800)')
                await page.wait_for_timeout(1500)
                count = await page.evaluate(
                    "(function(){var a=document.querySelectorAll('div[role=\"article\"]').length;"
                    " var b=document.querySelectorAll('article').length;"
                    " return Math.max(a,b);})()"
                )
                print(f'Scroll {attempt+1}: {count} elements')
                if count >= 10:
                    break

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
            print(f'Posts found: {len(extracted["posts"])}')

            if extracted['posts']:
                page_display_name = title.split('|')[0].strip() if '|' in title else page_name
                for p in extracted['posts']:
                    posts.append({
                        'external_id': f'fb_{page_name}_{p["id"]}',
                        'type'       : 'facebook',
                        'region'     : REGION,
                        'page_id'    : page_name,
                        'page_name'  : page_display_name,
                        'content'    : p['text'],
                        'image'      : p['image'],
                        'images'     : [p['image']] if p['image'] else [],
                        'reactions'  : {'like': 0, 'comment': 0, 'share': 0},
                        'post_url'   : None,
                        'posted_at'  : p['date'] or datetime.utcnow().isoformat(),
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
