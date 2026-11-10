"""Discover unverified reports via configured RSS searches; no event facts are invented."""
import datetime as dt
import hashlib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

TZ=ZoneInfo('Asia/Shanghai')

def collect(db, config):
    now=dt.datetime.now(TZ); errors=[]; added=0
    for category, query in config['news']['queries'].items():
        url='https://news.google.com/rss/search?'+urllib.parse.urlencode({'q':query,'hl':'zh-CN','gl':'CN','ceid':'CN:zh-Hans'})
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'})
            with urllib.request.urlopen(req,timeout=15) as response: data=response.read(2_000_000)
            root=ET.fromstring(data)
            for item in root.findall('./channel/item'):
                title=(item.findtext('title') or '').strip(); link=(item.findtext('link') or '').strip()
                if not title or not link: continue
                published=None
                try: published=parsedate_to_datetime(item.findtext('pubDate')).astimezone(TZ).isoformat()
                except (TypeError,ValueError,OverflowError): pass
                key=hashlib.sha256(link.encode()).hexdigest()
                cursor=db.execute('INSERT OR IGNORE INTO news_events VALUES (?,?,?,?,?,?,?,?,?,?,?)',(key,title,category,published,now.isoformat(),now.isoformat(),'unverified',link,item.findtext('source'),None,query))
                added+=cursor.rowcount
                db.execute('UPDATE news_events SET last_seen_at=? WHERE event_key=?',(now.isoformat(),key))
            db.commit()
        except Exception as exc: errors.append(f'{category}: {exc}')
    return added,errors
