#!/usr/bin/env python3

import sys
import os
import calendar
import hashlib
import re
import glob
import shutil
import pickle

from string import Template
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
from collections import defaultdict, ChainMap
from itertools import chain

import tomlkit
import feedparser

###############################################################################
## CONSTANTS                                                                 ##
###############################################################################

try:
    INDIR, OUTDIR, CACHEDIR = sys.argv[1:4]
except ValueError:
    sys.exit("usage: tonguefish.py <inputdir> <outputdir> <cachedir> [NOUPDATE]")

SEEN_CACHE = set()

CONF = os.path.join(INDIR, "feeds.toml")
OUTFILE = os.path.join(OUTDIR, "index.html")
CSS = glob.glob(os.path.join(INDIR, "*.css"))

NOUPDATE = len(sys.argv) > 4 and sys.argv[4] == "NOUPDATE"
if NOUPDATE:
    print("NOUPDATE option is enabled. Will not check for updates or fetch missing feeds.")

# TODO read these from the conf file!

# TODO dynamic filters
# TODO categories
# TODO default filter view
# TODO category and age filters should be independent (two rows)
# TODO write out category and age filter CSS dynamically into separate files

HEADER = Template("""
<!doctype html>
<html lang="en">
<head>
    <title>Tonguefish</title>
    <meta charset="UTF-8"/>
    $stylesheets
</head>
<body>
<input type="radio" id="all" name="filter" checked />
<label for="all">All entries</label>
<input type="radio" id="today" name="filter" />
<label for="today">Today</label>
<input type="radio" id="thisweek" name="filter" />
<label for="thisweek">This week</label>
<input type="radio" id="thismonth" name="filter" />
<label for="thismonth">This month</label>
<input type="radio" id="thisyear" name="filter" />
<label for="thisyear">This year</label>
<div id="main">
""")

STYLESHEET = Template("""
<link rel="stylesheet" href="$stylesheet"/>
""")

FOOTER = """
</div>
</body>
</html>
"""

FEEDHEADER = Template("""
<div class="feed">
<h1 class="feedtitle">$feedtitle</h1>
<ul>
""")

FEEDFOOTER = """
</ul>
</div>
"""

ENTRY = Template("""
<li class="$classes">
    <span class="published">$date</span>
    <a href="$link">$title</a>
    <div class="blurb">
        <h1 class="feedtitle">$feedtitle</h1>
        <h1 class="blurbtitle"><a href="$link">$title</a></h1>
        $blurb
    </div>
</li>
""")

###############################################################################
## FUNCTIONS & CLASSES                                                       ##
###############################################################################

def dump_feed_obj(feed_obj, cache_url):
    # We have to do this to stop pickle from blowing up because SAXParseException contains a closed file-like object
    # https://alligatr.co.uk/blog/valueerror/
    if feed_obj.bozo:
        print(f"{feed_obj.feed.link}: stripping error {feed_obj.bozo_exception} before pickling")
        del feed_obj["bozo_exception"]
        feed_obj["bozo"] = False
    
    with open(cache_url, "wb") as f:
        pickle.dump(feed_obj, f)


def get_cache_url(url):
    url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return os.path.join(CACHEDIR, url_hash)


def update_feed(cache_url, feed_conf, old_feed_obj=None):
    url = feed_conf["url"]
    
    if old_feed_obj:
        etag = old_feed_obj.get("etag")
        modified = old_feed_obj.get("modified")
        feed_obj = feedparser.parse(url, etag=etag, modified=modified)
    else:
        feed_obj = feedparser.parse(url)
    
    if feed_obj.status == 200:
        print(f"{url}: updated.")
        SEEN_CACHE.add(cache_url)
        dump_feed_obj(feed_obj, cache_url)
        return feed_obj
    
    elif old_feed_obj and feed_obj.status == 304:
        print(f"{url}: no update required.")
        return old_feed_obj
    
    elif feed_obj.status == 301:
        old_url = feed_conf["url"]
        print(f"{old_url} has redirected permanently to {feed_obj.href} -- updating config.")
        feed_conf["url"] = feed_obj.href
        feed_conf["url"].comment(f"# Updated automatically from {old_url}")
        SEEN_CACHE.remove(cache_url)
        cache_url = get_cache_url(feed_conf["url"])
        SEEN_CACHE.add(cache_url)
        dump_feed_obj(feed_obj, cache_url)
        return feed_obj
        
    elif feed_obj.status == 410:
        old_url = feed_conf["url"]
        print(f"{old_url} is gone -- updating config.")
        feed_conf["url_disabled"] = old_url
        feed_conf["url_disabled"].comment("# This feed is gone and should be removed.")
        del feed_conf["url"]
        raise ValueError("Server returned 410 response (feed is gone).")
    
    else:
        raise ValueError(f"Server returned {feed_obj.status} response.")


def get_feed_obj(feed_conf):
    if not "url" in feed_conf:
        raise ValueError("No URL found in feed config.")
    
    url = feed_conf["url"]
    cache_url = get_cache_url(url)
    
    if os.path.isfile(cache_url):
        SEEN_CACHE.add(cache_url)
        
        with open(cache_url, "rb") as f:
            feed_obj = pickle.load(f) # TODO handle pickle version changing, which necessitates reload
        
        if NOUPDATE:
            return feed_obj
        
        return update_feed(cache_url, feed_conf, feed_obj)
    else:
        if NOUPDATE:
            raise ValueError("Feed is not in cache and will not be fetched because NOUPDATE is enabled.")
        
        return update_feed(cache_url, feed_conf)


class FakeObj:
    def get(self, name, default=None):
        return getattr(self, name, default)


def get_digest(feed_obj, ignore, ignore_source):
    fake_obj = FakeObj()
    fake_obj.feed = FakeObj()
    fake_obj.feed.title = feed_obj.feed.title
    fake_obj.entries = []
    
    digest_entries = defaultdict(list)
    
    dc = feed_conf["digest"]
    
    for e in feed_obj.entries:
        if ignore and ignore.search(e.get(ignore_source)):
            continue
        
        dt = e.get("published_parsed", e.get("updated_parsed", None))
        digest_entries[(dt.tm_year, dt.tm_yday)].append(e)
    
    for _, entries in sorted(digest_entries.items(), reverse=True):
        dates = [e.published_parsed for e in entries]
        titles = [e.title for e in entries]
        links = [e.link for e in entries]
        descriptions = [e.description for e in entries]
        
        fake_e = FakeObj()
        fake_e.published_parsed = [sum(l)//len(l) for l in zip(*dates)]
        fake_e.description = "\n".join(f'<h1><a href="{l}">{t}</a></h1>\n{d}' for t, l, d in zip(titles, links, descriptions))
        
        # TODO: handle default digest name title and link with no special parsing (day and main feed link)
        
        sources = {
            "link": links,
            "title": titles,
            "description": descriptions,
        }
        
        id_find = re.compile(dc["id_find"])
        
        for s in sources[dc["id_source"]]:
            m = id_find.search(s)
            if m:
                fake_e.link = m.expand(dc["link"])
                fake_e.title = m.expand(dc["title"])
                break
        
        if fake_e.get("link"):
            fake_obj.entries.append(fake_e)
        
    return fake_obj;


def get_group(name, feeds):
    feed_confs, feed_objs = zip(*feeds)
    
    for f in feed_objs:
        for e in f.entries:
            e.title = f"{f.feed.title}: {e.title}"
    
    fake_obj = FakeObj()
    fake_obj.feed = FakeObj()
    fake_obj.feed.title = name.title()
    fake_obj.entries = sorted(chain.from_iterable(f.entries for f in feed_objs), key=lambda e: e.get("published_parsed", e.get("updated_parsed")), reverse=True)
    
    fake_conf = dict(ChainMap(*(f["group"] for f in feed_confs)))
    fake_conf["group_obj"] = fake_obj
    
    return fake_conf


###############################################################################
## MAIN                                                                      ##
###############################################################################


with open(CONF) as f:
    conf = tomlkit.parse(f.read())


for stylesheet in CSS:
    try:
        shutil.copy(stylesheet, OUTDIR)
    except shutil.SameFileError:
        pass # File is the same

stylesheets = "\n".join(STYLESHEET.safe_substitute(stylesheet=os.path.basename(s)) for s in CSS)
header = HEADER.safe_substitute(stylesheets=stylesheets)
now = datetime.now(ZoneInfo(conf["timezone"]))
all_feeds = conf["feeds"][:]
groups = defaultdict(list)

# Process the feeds
with open(OUTFILE, "w") as out:
    out.write(header)
    
    for i, feed_conf in enumerate(all_feeds, 1):
        if "group_obj" in feed_conf:
            feed_obj = feed_conf["group_obj"]
        else:
            try:
                feed_obj = get_feed_obj(feed_conf)
            except ValueError as e:
                print(f"Error at feed {i}: {e}")
                continue
        
        ignore = None
        if "ignore" in feed_conf:
            ignore = re.compile(feed_conf["ignore"]["find"])
            ignore_source = feed_conf["ignore"]["source"]
                
        if "group" in feed_conf:
            groups[feed_conf["group"]["name"]].append((feed_conf, feed_obj))
            if i == len(conf["feeds"]):
                all_feeds.extend(get_group(n, fs) for n, fs in groups.items())
            continue
        
        if "digest" in feed_conf:
            feed_obj = get_digest(feed_obj, ignore, ignore_source)
            ignore = None # Ignore is applied once only to the original items
        
        feedtitle = feed_conf.get("title") or feed_obj.feed.title
        out.write(FEEDHEADER.safe_substitute(feedtitle=feedtitle))
        
        max_num = feed_conf.get("max_entry_num", conf.get("max_entry_num", 0))
        max_age = feed_conf.get("max_entry_age", conf.get("max_entry_age", 0))
        num_entries = 0
        
        entry_classes = ["entry"]
        category = feed_conf.get("category")
        if category:
            entry_classes.append(category)
        
        for e in feed_obj.entries:
            if ignore and ignore.search(e.get(ignore_source)):
                continue
            
            classes = entry_classes[:]
            date_tuple = e.get("published_parsed", e.get("updated_parsed"))
            date_obj = datetime.fromtimestamp(calendar.timegm(date_tuple), timezone.utc)
            age = now - date_obj
            
            if max_age and age.days > max_age:
                continue
            
            if age.days < 1:
                classes.append("today")
            
            if age.days < 7:
                classes.append("thisweek")
                
            # Todo make this more precise?
                
            if age.days < 30:
                classes.append("thismonth")
                
            if age.days < 365:
                classes.append("thisyear")
            
            date_str = date_obj.strftime("%b %d")
            
            classes_str = " ".join(classes)
            
            description = e.description.replace("<img ", "<img loading='lazy' ") # TODO parse this more nicely?
            
            out.write(ENTRY.safe_substitute(classes=classes_str, date=date_str, link=e.link, title=e.title, blurb=description, feedtitle=feedtitle))
            
            num_entries += 1
            if max_num and num_entries >= max_num:
                break;
        
        out.write(FEEDFOOTER)
        
        if i == len(conf["feeds"]):
            all_feeds.extend(get_group(n, fs) for n, fs in groups.items())
    
    out.write(FOOTER)


# Prune unneeded cache files
for cache_url in glob.glob(os.path.join(CACHEDIR, "*")):
    if cache_url not in SEEN_CACHE:
        os.remove(cache_url)


# Write out the config, which may have been modified
with open(CONF, "w") as f:
    f.write(tomlkit.dumps(conf))
