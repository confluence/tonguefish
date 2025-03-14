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
CONF_UPDATED = False

CONF = os.path.join(INDIR, "feeds.toml")
OUTFILE = os.path.join(OUTDIR, "index.html")
CSS = glob.glob(os.path.join(INDIR, "*.css"))

NOUPDATE = len(sys.argv) > 4 and sys.argv[4] == "NOUPDATE"
if NOUPDATE:
    print("NOUPDATE option is enabled. Will not check for updates or fetch missing feeds.")
    
# Whole page templates

HEADER = Template("""
<!doctype html>
<html lang="en">
<head>
    <title>Tonguefish</title>
    <meta charset="UTF-8"/>
    $refresh
    $stylesheets
</head>
<body>
<div id="filters">
$agefilters
<hr/>
$catfilters
</div>
<div id="main">
""")

REFRESH = Template("""
<meta http-equiv="refresh" content="$interval">
""")

STYLESHEET = Template("""
<link rel="stylesheet" href="$stylesheet"/>
""")

FILTER = Template("""
<input type="radio" id="$id" name="$name" $checked />
<label for="$id">$label</label>
""")

AGES = (
    {"name": "agefilter", "id": "allages", "label": "All ages", "checked": "checked"},
    {"name": "agefilter", "id": "today", "label": "Today", "checked": ""},
    {"name": "agefilter", "id": "thisweek", "label": "This week", "checked": ""},
    {"name": "agefilter", "id": "thismonth", "label": "This month", "checked": ""},
    {"name": "agefilter", "id": "thisyear", "label": "This year", "checked": ""},
)

AGEFILTERIDS = (a["id"] for a in AGES)

AGEFILTERS = "\n".join(FILTER.safe_substitute(a) for a in AGES)

FOOTER = """
</div>
</body>
</html>
"""

FILTERCSSRULES = Template("""
#filters:has(#$id:checked)~#main li:not(.$id) {
    display: none;
}

#filters:has(#$id:checked)~#main div.feed:not(:has(li.$id)) {
    display: none;
}
""")

FILTERCSSFILE = os.path.join(OUTDIR, "tonguefilter.css")

# Per-feed templates

FEEDHEADER = Template("""
<div class="feed">
<h1 class="feedtitle">$feedtitle</h1>
<ul>
""")

FEEDFOOTER = """
</ul>
</div>
"""

# Entry templates

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
        # = = MODIFYING CONFIG = = #
        feed_conf["url"] = feed_obj.href
        feed_conf["url"].comment(f"# Updated automatically from {old_url}")
        CONF_UPDATED = True
        SEEN_CACHE.remove(cache_url)
        cache_url = get_cache_url(feed_conf["url"])
        SEEN_CACHE.add(cache_url)
        dump_feed_obj(feed_obj, cache_url)
        return feed_obj
        
    elif feed_obj.status == 410:
        old_url = feed_conf["url"]
        print(f"{old_url} is gone -- updating config.")
        # = = MODIFYING CONFIG = = #
        feed_conf["url_disabled"] = old_url
        feed_conf["url_disabled"].comment("# This feed is gone and should be removed.")
        del feed_conf["url"]
        CONF_UPDATED = True
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


def get_digest(feed_conf, feed_obj):
    digest_conf = feed_conf["digest"]
    interval = digest_conf.get("interval")
    
    if interval not in ["day", "month"]:
        print(f"Invalid digest interval: {interval}. Not digesting.")
        return feed_conf, feed_obj
    
    fake_obj = FakeObj()
    fake_obj.feed = FakeObj()
    fake_obj.feed.title = feed_obj.feed.title
    fake_obj.entries = []
    
    # Note: this is not a deep copy; original conf children included in fake conf
    fake_conf = dict(feed_conf)
    
    # Ignore before grouping
    ignore = []
    for field, regex in feed_conf.get("ignore", {}).items():
        ignore.append((field, re.compile(regex)))
    # Don't repeat ignore in the main loop
    if ignore:
        del fake_conf["ignore"]
    
    # Group entries by interval
    digest_entries = defaultdict(list)
    for e in feed_obj.entries:
        # Skip ignored
        if any(r.search(e.get(f, "")) for f, r in ignore):
            continue
        
        dt = e.get("published_parsed", e.get("updated_parsed", None))
        
        if interval == "day":
            key = (dt.tm_year, dt.tm_yday)
        elif interval == "month":
            key = (dt.tm_year, dt.tm_mon)
        
        digest_entries[key].append(e)
    
    # Process grouped entries into digest entries
    for _, entries in sorted(digest_entries.items(), reverse=True):
        # Oldest first within each digest
        entries.reverse()
        
        dates = [e.published_parsed for e in entries]
        titles = [e.title for e in entries]
        links = [e.link for e in entries]
        descriptions = [e.description for e in entries]
        
        fake_e = FakeObj()
        fake_e.published_parsed = [sum(l)//len(l) for l in zip(*dates)]
        fake_e.description = "\n".join(f'<h1><a href="{l}">{t}</a></h1>\n{d}' for t, l, d in zip(titles, links, descriptions))
                
        # Try to generate title and link
        if "id_find" in digest_conf and "id_source" in digest_conf:
            sources = {
                "link": links,
                "title": titles,
                "description": descriptions,
            }
            
            id_find = re.compile(digest_conf["id_find"])
            
            for s in sources[digest_conf["id_source"]]:
                m = id_find.search(s)
                if m:
                    fake_e.link = m.expand(digest_conf["link"])
                    fake_e.title = m.expand(digest_conf["title"])
                    break
            else:
                # Ignore partial digests unless partial is set to 1
                if not digest_conf.get("partial", False):
                    continue
        
        # Fall back to default link and/or title -- use the first real entry
        if not fake_e.get("title"):
            fake_e.title = f"{titles[0]}..."
        
        if not fake_e.get("link"):
            fake_e.link = links[0]
        
        fake_obj.entries.append(fake_e)
        
    return fake_conf, fake_obj;


def get_group(name, feeds):
    feed_confs, feed_objs = zip(*feeds)
    
    for f in feed_objs:
        for e in f.entries:
            e["title"] = f"{f.feed.title}: {e.title}"
    
    fake_obj = FakeObj()
    fake_obj.feed = FakeObj()
    fake_obj.feed.title = name.capitalize()
    fake_obj.entries = sorted(chain.from_iterable(f.entries for f in feed_objs), key=lambda e: e.get("published_parsed", e.get("updated_parsed")), reverse=True)
    
    # Note: this is not a deep copy; original conf children included in fake conf
    fake_conf = dict(ChainMap(*feed_confs))
    del fake_conf["url"] # The grouped feed has no url
    del fake_conf["group"] # The grouped feed is not itself in the group
    fake_conf["group_obj"] = fake_obj # Use the ready-made obj instead
    
    return fake_conf


###############################################################################
## MAIN                                                                      ##
###############################################################################

# Read config file
with open(CONF) as f:
    conf = tomlkit.parse(f.read())

# Bail early if no feeds configured
if not "feeds" in conf:
    sys.exit("No feeds configured. Nothing to do.")
    
# Use a single "now" for the whole run
now = datetime.now(ZoneInfo(conf["timezone"]))
print("Running tonguefish at", now.strftime("%a %d %b %Y, %H:%M"))

# Copy input stylesheets
for stylesheet in CSS:
    try:
        shutil.copy(stylesheet, OUTDIR)
    except shutil.SameFileError:
        pass # File is the same

# Refresh interval
refresh_interval = conf.get("refresh_interval", 0)
if refresh_interval:
    refresh = REFRESH.safe_substitute(interval=refresh_interval)
else:
    refresh = ""

# Collect categories
catids = set()
# Don't register grouped feeds with no category as uncategorised unless no feeds in the group have a category
group_has_category = defaultdict(bool)

# Characters to remove from category names
CATIDREMOVE = re.compile("^[^a-zA-Z_]*|[^a-zA-Z_0-9]")

# Find category names and normalise in config (must be allowed class names)
for feed_conf in conf["feeds"]:
    group = feed_conf.get("group")
    catid = feed_conf.get("category")
    
    if group:
        group_has_category[group] |= bool(catid)
    
    if catid:
        oldcatid = catid
        catid = catid.replace(" ", "_")
        catid = CATIDREMOVE.sub("", catid)
        catids.add(catid)
        if catid != oldcatid:
            print(f"Invalid category name {oldcatid}. Correcting to {catid}.")
            # = = MODIFYING CONFIG = = #
            feed_conf["category"] = catid
            feed_conf["category"].comment(f"# automatically corrected from '{oldcatid}'")
            CONF_UPDATED = True
    elif not group:
        catids.add("uncategorised")
        
if not all(group_has_category.values()):
    catids.add("uncategorised")

# Category inputs
categories = [
    {"name": "catfilter", "id": "allcats", "label": "All categories", "checked": "checked"},
]

for catid in sorted(catids):
    catlabel = catid.replace("_", " ").capitalize()
    categories.append(
        {"name": "catfilter", "id": catid, "label": catlabel, "checked": ""}
    )

# Generate category HTML
catfilters = "\n".join(FILTER.safe_substitute(c) for c in categories)

# Input options which unset filters (no CSS for these)
showall = ("allages", "allcats")

# Generate CSS rules for age and category filters
generatedcss = "".join(FILTERCSSRULES.safe_substitute({"id": fi}) for fi in (*AGEFILTERIDS, *catids) if not fi in showall)

# # # WRITE GENERATED CSS # # #
with open(FILTERCSSFILE, "w") as f:
    f.write(generatedcss)

# Generate stylesheet links
stylesheets = "\n".join(STYLESHEET.safe_substitute(stylesheet=os.path.basename(s)) for s in (*CSS, FILTERCSSFILE))

# Generate page header
header = HEADER.safe_substitute(refresh=refresh, stylesheets=stylesheets, agefilters=AGEFILTERS, catfilters=catfilters)

# Group feeds will be appended to this
all_feeds = conf["feeds"][:]
# Feeds for groups will be aggregated here
groups = defaultdict(list)

# Process the feeds
with open(OUTFILE, "w") as out:
     # # # WRITE PAGE HEADER # # #
    out.write(header)
    
    # Process feeds (generated groups will be appended to normal feeds inside the loop)
    for i, feed_conf in enumerate(all_feeds, 1):
        if "group_obj" in feed_conf:
            # Handle appended group, which will have a constructed object
            feed_obj = feed_conf["group_obj"]
        else:
            # Normal feed; try to get it
            try:
                feed_obj = get_feed_obj(feed_conf)
            except ValueError as e:
                print(f"Error at feed {i}: {e}")
                continue
        
        # Add grouped feeds to group dict, to be processed at the end
        if "group" in feed_conf:
            groups[feed_conf["group"]].append((feed_conf, feed_obj))
        
        # If this is the last feed in the config, create and append groups (before exiting loop)
        if i == len(conf["feeds"]):
            all_feeds.extend(get_group(n, fs) for n, fs in groups.items())
        
        # Then proceed to skip over the feed if it's grouped
        if "group" in feed_conf:
            continue
        
        # Then process digest
        if "digest" in feed_conf:
            feed_conf, feed_obj = get_digest(feed_conf, feed_obj)
            
        # Then create ignore filter
        ignore = []
        for field, regex in feed_conf.get("ignore", {}).items():
            ignore.append((field, re.compile(regex)))
        
        # Feed title
        feedtitle = feed_conf.get("title") or feed_obj.feed.title
        
        # # # WRITE FEED HEADER # # #
        out.write(FEEDHEADER.safe_substitute(feedtitle=feedtitle))
        
        # Per-feed limits
        max_num = feed_conf.get("max_entry_num", conf.get("max_entry_num", 0))
        max_age = feed_conf.get("max_entry_age", conf.get("max_entry_age", 0))
        num_entries = 0
        
        # Default feed classes
        entry_classes = ["entry", feed_conf.get("category", "uncategorised")]
        
        # Process entries
        for e in feed_obj.entries:
            # Skip ignored
            if any(r.search(e.get(f, "")) for f, r in ignore):
                continue
            
            # Add classes for age filters
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
            
            # Don't load images for hidden elements
            description = e.description.replace("<img ", "<img loading='lazy' ") # TODO parse this more nicely?
            
            # # # WRITE ENTRY # # #
            out.write(ENTRY.safe_substitute(classes=classes_str, date=date_str, link=e.link, title=e.title, blurb=description, feedtitle=feedtitle))
            
            num_entries += 1
            if max_num and num_entries >= max_num:
                break;
        
        # # # WRITE FEED FOOTER # # #
        out.write(FEEDFOOTER)
    
    # # # WRITE PAGE FOOTER # # #
    out.write(FOOTER)


# Prune unneeded cache files
for cache_url in glob.glob(os.path.join(CACHEDIR, "*")):
    if cache_url not in SEEN_CACHE:
        os.remove(cache_url)


# Write out the config, which may have been modified
if CONF_UPDATED:
    with open(CONF, "w") as f:
        f.write(tomlkit.dumps(conf))
