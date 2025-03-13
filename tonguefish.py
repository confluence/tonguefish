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
from collections import defaultdict

import tomlkit
import feedparser

try:
    INDIR, OUTDIR, CACHEDIR = sys.argv[1:4]
except ValueError:
    sys.exit("usage: tonguefish.py <inputdir> <outputdir> <cachedir>")

SEEN_CACHE = set()

CONF = os.path.join(INDIR, "feeds.toml")
OUTFILE = os.path.join(OUTDIR, "index.html")
ERRORFILE = os.path.join(OUTDIR, "errors")
CSS = glob.glob(os.path.join(INDIR, "*.css"))

# TODO read these from the conf file!

# TODO dynamic filters
# TODO categories
# TODO thismonth, thisyear
# TODO remove limits by default? Less slow now that images are lazy?
# TODO default filter view
# TODO category and age filters should be independent (two rows)
# TODO category filters should apply to whole feeds (give feed a category class)
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

def dump_feed_obj(feed_obj, path):
    # We have to do this to stop pickle from blowing up because SAXParseException contains a closed file-like object
    # https://alligatr.co.uk/blog/valueerror/
    if feed_obj.bozo:
        with open(ERRORFILE, "a") as f:
            f.write(f"{feed_obj.feed.link}: stripping error {feed_obj.bozo_exception} before pickling\n")
        del feed_obj["bozo_exception"]
        feed_obj["bozo"] = False
    
    with open(path, "wb") as f:
        pickle.dump(feed_obj, f)

def get_feed_obj(feed_conf):
    url = feed_conf["url"]
    url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()
    cache_url = os.path.join(CACHEDIR, url_hash)
    SEEN_CACHE.add(cache_url)
    
    if os.path.isfile(cache_url):
        with open(cache_url, "rb") as f:
            feed_obj = pickle.load(f) # TODO handle pickle version changing, which necessitates reload
        # TODO uncomment after testing is done
        #etag = feed_obj.get("etag")
        #modified = feed_obj.get("modified")
        #print(f"+++ feed {feed_obj.feed.link} etag {etag} modified {modified}")
        #newer_feed_obj = feedparser.parse(url, etag=etag, modified=modified)
        #print(f"+++ status of request {newer_feed_obj.status}")
        #if newer_feed_obj.status == 200: # TODO handle redirects / dead links
            #feed_obj = newer_feed_obj
            #dump_feed_obj(feed_obj, cache_url)
    else:
        feed_obj = feedparser.parse(url)
        if feed_obj.status == 200: # TODO handle redirects / dead links
            dump_feed_obj(feed_obj, cache_url)
    
    return feed_obj

class FakeObj:
    def get(self, name, default=None):
        return getattr(self, name, default)

def get_digest(feed_conf):
    feed_obj = get_feed_obj(feed_conf)
    
    fake_obj = FakeObj()
    fake_obj.feed = FakeObj()
    fake_obj.feed.title = feed_obj.feed.title
    fake_obj.entries = []
    
    digest_entries = defaultdict(list)
    
    ignore = None
    ignore_source = None
    
    if "ignore" in feed_conf:
        ignore = re.compile(feed_conf["ignore"]["find"])
        ignore_source = feed_conf["ignore"]["source"]
    
    dc = feed_conf["digest"]
    
    for e in feed_obj.entries:
        if ignore and ignore.search(e.get(ignore_source)):
            continue
        
        dt = e.get("published_parsed", e.get("updated_parsed", None))
        digest_entries[(dt.tm_year, dt.tm_yday)].append(e)
    
    for _, entries in reversed(sorted(digest_entries.items())):
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

def get_group(feed_conf):
    pass # TODO

with open(CONF) as f:
    conf = tomlkit.parse(f.read())

for stylesheet in CSS:
    try:
        shutil.copy(stylesheet, OUTDIR)
    except shutil.SameFileError:
        pass # File is the same

try:
    os.remove(ERRORFILE)
except FileNotFoundError:
    pass # File does not exist

NOW = datetime.now(ZoneInfo(conf["timezone"]))

with open(OUTFILE, "w") as out:
    stylesheets = "\n".join(STYLESHEET.safe_substitute(stylesheet=os.path.basename(s)) for s in CSS)
    header = HEADER.safe_substitute(stylesheets=stylesheets)
    
    out.write(header)
    
    for feed_conf in conf["feeds"]:
        if not "url" in feed_conf:
            continue
        
        # TODO handle grouped feeds
        
        if "digest" in feed_conf:
            feed_obj = get_digest(feed_conf)
        else:
            feed_obj = get_feed_obj(feed_conf)
        
        feedtitle = feed_conf.get("title") or feed_obj.feed.title
        out.write(FEEDHEADER.safe_substitute(feedtitle=feedtitle))
        
        max_num = feed_conf.get("max_entry_num", conf["max_entry_num"])
        max_age = feed_conf.get("max_entry_age", conf["max_entry_age"])
        num_entries = 0
        
        entry_classes = ["entry"]
        category = feed_conf.get("category")
        if category:
            entry_classes.append(category)
        
        for e in feed_obj.entries:
            classes = entry_classes[:]
            date_tuple = e.get("published_parsed", e.get("updated_parsed", None))
            date_obj = datetime.fromtimestamp(calendar.timegm(date_tuple), timezone.utc)
            age = NOW - date_obj
            
            if age.days > max_age:
                continue
            
            if age.days < 1:
                classes.append("today")
            
            if age.days < 7:
                classes.append("thisweek")
            
            date_str = date_obj.strftime("%b %d")
            
            classes_str = " ".join(classes)
            
            description = e.description.replace("<img ", "<img loading='lazy' ") # TODO parse this more nicely?
            
            out.write(ENTRY.safe_substitute(classes=classes_str, date=date_str, link=e.link, title=e.title, blurb=description, feedtitle=feedtitle))
            
            num_entries += 1
            if num_entries > max_num:
                break;
        
        out.write(FEEDFOOTER)
    
    out.write(FOOTER)

for cache_url in glob.glob(os.path.join(CACHEDIR, "*")):
    if cache_url not in SEEN_CACHE:
        os.remove(cache_url)
