# -*- coding: utf-8 -*-
"""
세법 개정/고시/보도자료 감시 → ntfy 푸시
- 공식 출처 위주: 기재부 RSS, 국세청 보도/공지, 국가법령정보센터
- 제목/본문에 키워드 매칭되면 '새 글'만 푸시
- 중복 방지: .state/tax_state.json
"""
import os, json, hashlib, textwrap, requests
from urllib.parse import urljoin
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

try:
    import feedparser
except Exception:
    feedparser = None

# ---- 환경 ----
NTFY_URL   = os.environ.get("NTFY_URL", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
STATE_FILE = os.environ.get("STATE_FILE", ".state/tax_state.json")
MAX_ITEMS_PER_SOURCE = int(os.environ.get("MAX_ITEMS_PER_SOURCE", "30"))

# 관심 키워드(소문자 비교)
KEYWORDS = [
    "세법","개정","고시","보도자료","예규","해석","사전답변","판례",
    "법인세","부가세","부가가치세","소득세","원천세","지방세",
    "감면","공제","가산세","전자신고","연말정산",
    "transfer pricing","withholding","international tax"
]

# --- 공식 출처 중심 ---
SOURCES = [
    # 기획재정부 보도자료(RSS)
    {"name":"기재부_보도자료(RSS)","type":"rss",
     "url":"https://www.moef.go.kr/feeds/news_release.xml"},

    # 국세청 보도자료/공지(HTML)
    {"name":"국세청_보도자료","type":"html",
     "url":"https://www.nts.go.kr/nts/cm/cntnts/cntntsView.do?mi=11931",
     "item_selector":"div.bbsList li a","base":"https://www.nts.go.kr"},
    {"name":"국세청_공지사항","type":"html",
     "url":"https://www.nts.go.kr/nts/cm/cntnts/cntntsList.do?mi=11932",
     "item_selector":"div.bbsList li a","base":"https://www.nts.go.kr"},

    # 국가법령정보센터(법제처) 최근 공포 법령(HTML)
    {"name":"법제처_최근공포법령","type":"html",
     "url":"https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=&lsId=&efYd=&chrClsCd=",
     "item_selector":"a","base":"https://www.law.go.kr"},
]

SOURCES.extend([
    # 정부안 '입법예고' 공식 RSS (법제처 Open API)
    {
        "name": "법제처_입법예고(RSS)",
        "type": "rss",
        "url": "https://open.moleg.go.kr/data/xml/li_rssSH01.xml",
    },
    # 기획재정부 입법·행정예고 (HTML 목록 파싱)
    {
        "name": "기재부_입법·행정예고",
        "type": "html",
        "url": "https://www.moef.go.kr/lw/lap/TbPrvntcList.do?bbsId=MOSFBBS_000000000055&menuNo=7050300",
        "item_selector": "table a, .bbsList a, a[href*='TbPrvntcView']",
        "base": "https://www.moef.go.kr",
    },
])

HEADERS = {"User-Agent":"Mozilla/5.0 (TaxLawWatcher/1.0)"}

def ensure_dir(p):
    d = os.path.dirname(p)
    if d and not os.path.exists(d): os.makedirs(d, exist_ok=True)

def load_state():
    ensure_dir(STATE_FILE)
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE,"r",encoding="utf-8") as f: return json.load(f)
    return {"sent_ids":[]}

def save_state(st):
    ensure_dir(STATE_FILE)
    with open(STATE_FILE,"w",encoding="utf-8") as f: json.dump(st,f,ensure_ascii=False,indent=2)

def make_id(title,url):
    h=hashlib.sha256(); h.update((title.strip()+"|"+url.strip()).encode("utf-8"))
    return h.hexdigest()[:16]

def match_keywords(text):
    if not text: return False
    low=text.lower()
    return any(kw.lower() in low for kw in KEYWORDS)

def send_push(title, body):
    if not NTFY_TOPIC: 
        print("NTFY_TOPIC not set; skip"); return
    try:
        requests.post(f"{NTFY_URL.rstrip('/')}/{NTFY_TOPIC}",
                      data=body.encode("utf-8"),
                      headers={"Title": title}, timeout=20)
    except Exception as e:
        print("ntfy push failed:", repr(e))

def strip_html(s):
    import re; return re.sub("<[^<]+?>","",s or "").strip()

def fetch_rss(url):
    if not feedparser: return []
    feed=feedparser.parse(url); out=[]
    for e in feed.entries[:MAX_ITEMS_PER_SOURCE]:
        title=getattr(e,"title","") or ""; link=getattr(e,"link","") or url
        summ=getattr(e,"summary","") or getattr(e,"description","") or ""
        out.append({"title":title,"url":link,"summary":strip_html(summ)})
    return out

def fetch_html(url, sel="a", base=None):
    r=requests.get(url, headers=HEADERS, timeout=30); r.raise_for_status()
    soup=BeautifulSoup(r.text,"html.parser"); out=[]
    for a in soup.select(sel)[:MAX_ITEMS_PER_SOURCE]:
        t=a.get_text(" ",strip=True); href=a.get("href","")
        if not t or not href: continue
        out.append({"title":t,"url":urljoin(base or url, href),"summary":""})
    # dedup
    uniq={ (x["title"],x["url"]):x for x in out }
    return list(uniq.values())

def fetch_source(src):
    return fetch_rss(src["url"]) if src["type"]=="rss" else \
           fetch_html(src["url"], src.get("item_selector","a"), src.get("base"))

def chunk(s, maxlen=1500):
    lines=(s or "").splitlines(); cur=""; out=[]
    for line in lines:
        add=line+"\n"
        if len(cur)+len(add)>maxlen and cur:
            out.append(cur.rstrip()); cur=add
        else: cur+=add
    if cur: out.append(cur.rstrip())
    return out or ["(empty)"]

def main():
    state=load_state(); sent=set(state.get("sent_ids",[]))
    hits=[]
    for src in SOURCES:
        try:
            for it in fetch_source(src):
                iid=make_id(it["title"], it["url"])
                if iid in sent: continue
                ok=match_keywords(it["title"])
                if not ok:
                    try:
                        rt=requests.get(it["url"], headers=HEADERS, timeout=30)
                        if rt.ok:
                            body=BeautifulSoup(rt.text,"html.parser").get_text(" ",strip=True)
                            ok=match_keywords(body)
                    except Exception: pass
                if ok:
                    hits.append({"id":iid,"title":it["title"],"url":it["url"],"src":src["name"]})
        except Exception as e:
            print("source error:", src["name"], repr(e))

    if hits:
        today=(datetime.utcnow()+timedelta(hours=9)).strftime("%Y-%m-%d")
        top=hits[:10]
        lines=[f"• ({h['src']}) {textwrap.shorten(h['title'],200,'…')}\n{h['url']}" for h in top]
        body="\n\n".join(lines)
        for i, ch in enumerate(chunk(body),1):
            title = f"{today} 세법/공지 업데이트" + ("" if i==1 and len(body)<=1500 else f" ({i})")
            send_push(title, ch)
        for h in top: sent.add(h["id"])
        state["sent_ids"]=list(sent); save_state(state)
    else:
        print("no new tax updates")

if __name__=="__main__":
    main()
