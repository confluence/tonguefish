#!/usr/bin/env python3

import sys
import os
import calendar
import hashlib
import re
import glob
import shutil
import pickle
import logging
import argparse

from string import Template
from zoneinfo import ZoneInfo
from datetime import datetime, timezone, timedelta
from collections import defaultdict, ChainMap
from itertools import chain

import tomlkit
import feedparser

logger = logging.getLogger("tonguefish")

class Cache:
    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self.seen = set()
        
    def get_cache_url(self, feed_url):
        url_hash = hashlib.sha1(feed_url.encode("utf-8")).hexdigest()
        return os.path.join(self.cache_dir, url_hash)
        
    def get(self, feed_url):
        cache_url = self.get_cache_url(feed_url)
        if os.path.isfile(cache_url):
            self.seen.add(cache_url)
            
            with open(cache_url, "rb") as f:
                feed_obj = pickle.load(f) # TODO handle pickle version changing
            
            return feed_obj
        return None
    
    def put(self, feed_url, feed_obj, old_feed_url=None):
        cache_url = self.get_cache_url(feed_url)
        self.seen.add(cache_url)
        
        # We have to do this to stop pickle from blowing up because SAXParseException contains a closed file-like object
        # https://alligatr.co.uk/blog/valueerror/
        if feed_obj.bozo:
            logger.debug("%s: stripping error %s before pickling", feed_url, feed_obj.bozo_exception)
            del feed_obj["bozo_exception"]
            feed_obj["bozo"] = False
        
        with open(cache_url, "wb") as f:
            pickle.dump(feed_obj, f)
            
        if old_feed_url:
            self.seen.remove(self.get_cache_url(old_feed_url))
    
    def clean(self):
        for cache_url in glob.glob(os.path.join(self.cache_dir, "*")):
            if cache_url not in self.seen:
                os.remove(cache_url)


class Feedparser:
    def __init__(self, cache_dir, no_update, no_new):
        self.cache = Cache(cache_dir)
        self.no_update = no_update
        self.no_new = no_new
    
    def update(self, feed, old_feed_obj=None):
        url = feed.conf["url"]
        
        if old_feed_obj:
            etag = old_feed_obj.get("etag")
            modified = old_feed_obj.get("modified")
            feed_obj = feedparser.parse(url, etag=etag, modified=modified)
        else:
            feed_obj = feedparser.parse(url)
        
        if feed_obj.status == 200:
            logger.info("%s: updated.", url)
            self.cache.put(url, feed_obj)
            return feed_obj
        
        elif old_feed_obj and feed_obj.status == 304:
            logger.info("%s: no update required.", url)
            return old_feed_obj
        
        elif feed_obj.status == 301:
            new_url = feed_obj.href
            logger.warning("%s has redirected permanently to %s -- updating config.", url, new_url)
            feed.update_url(new_url)
            self.cache.put(new_url, feed_obj, url)
            return feed_obj
            
        elif feed_obj.status == 410:
            feed.disable_url()
            raise ValueError(f"{url}: Server returned 410 response (feed is gone).")
        
        else:
            raise ValueError(f"{url}: Server returned {feed_obj.status} response.")

    def get_feed_obj(self, feed):
        if not "url" in feed.conf:
            raise ValueError("No URL found in feed config.")
        url = feed.conf["url"]
        
        feed_obj = self.cache.get(url)
        
        if feed_obj:
            if self.no_update:
                return feed_obj
            return self.update(feed, feed_obj)
        
        else:
            if self.no_new:
                raise ValueError(f"{url}: Feed is not in cache and fetching of missing feeds is disabled.")
            return self.update(feed)


class TimeZoneMixIn:
    def get_timezone(self):
        timezone = self.conf.get("timezone")
        if timezone:
            # IANA string (preferred)
            return ZoneInfo(timezone)
        
        tzoffset = self.conf.get("tzoffset")
        if tzoffset:
            # Fixed hour offset
            return timezone(timedelta(hours=tzoffset))
        
        return None


class MainConfig(TimeZoneMixIn):
    def __init__(self, file_path):
        with open(file_path) as f:
            data = f.read()
            self.checksum = hashlib.sha1(data.encode("utf-8")).hexdigest()
            self.conf = tomlkit.parse(data)
        
    def get(self, key, default=None):
        return self.conf.get(key, default)
    
    def rename_default_category(self, old_name, new_name):
        self.conf["category"] == new_name
        self.conf["category"].comment(f"# Updated automatically from {old_name}")
        
    def rename_category_key(self, old_name, new_name):
        self.conf["categories"][new_name] = self.conf["categories"][old_name]
        del self.conf["categories"][old_name]
        self.conf["categories"][new_name].comment(f"# Updated automatically from {old_name}")
    
    def save(self, file_path):
        data = tomlkit.dumps(self.conf)
        checksum = hashlib.sha1(data.encode("utf-8")).hexdigest()
        if checksum != self.checksum:
            logger.warning("Writing out config, which has been modified.")
            with open(file_path, "w") as f:
                f.write(data)


class Feed(TimeZoneMixIn):
    FEEDHEADER = Template("""
<div class="$classes">
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
    
    IGNORE_TOPLEVEL = {"feeds", "categories", "url", "title", "timezone", "tzoffset"}
    IGNORE_CATEGORY = {"url", "title", "category", "timezone", "tzoffset"}
    
    @staticmethod
    def get_feeds(main_conf):
        feeds = []
        groups = defaultdict(list)
        
        for feed_conf in main_conf.get("feeds", []):
            feed = Feed(main_conf, feed_conf)
            group = feed.conf.get("group")
            
            if group:
                groups[group].append(feed)
            else:
                feeds.append(feed)
            
        for group, grouped_feeds in groups.items():
            feeds.append(Group(grouped_feeds))
        
        return [(Digest(feed) if "digest" in feed.conf else feed) for feed in feeds]
    
    def __init__(self, main_conf, feed_conf):
        self.main_conf = main_conf
        self.orig_conf = feed_conf
        self.conf = {}
        self.feed_id = None
        self.calculate_conf()
        
    def calculate_conf(self):
        # Construct combined prefs from top-level, category and feed entries
        
        # Clear in place
        self.conf.clear()
        
        # Apply top-level prefs first
        self.conf.update({k:v for (k, v) in self.main_conf.conf.items() if k not in self.IGNORE_TOPLEVEL})
        
        # Then category prefs
        category = self.orig_conf.get("category", "uncategorised")
        category_conf = self.main_conf.get("categories", {}).get(category)
        if category_conf:
            self.conf.update({k:v for (k, v) in category_conf.items() if k not in self.IGNORE_CATEGORY})
        
        # Finally the per-feed prefs
        self.conf.update(self.orig_conf)
        
        self.feed_id = self.conf["url"]

    def update_url(self, url):
        old_url = self.orig_conf["url"]
        self.orig_conf["url"] = url
        self.orig_conf["url"].comment(f"# Updated automatically from {old_url}")
        self.calculate_conf()
    
    def disable_url(self):
        old_url = self.orig_conf["url"]
        self.orig_conf["url_disabled"] = old_url
        self.orig_conf["url_disabled"].comment("# This feed is gone and should be removed.")
        del self.orig_conf["url"]
        self.calculate_conf()
        
    def rename_category(self, old_name, new_name):
        self.orig_conf["category"] = new_name
        self.orig_conf["category"].comment(f"# Updated automatically from {old_name}")
        self.calculate_conf()
            
    def get_obj(self, parser):
        return parser.get_feed_obj(self)
    
    def get_ignore(self):
        return [(field, re.compile(regex)) for (field, regex) in self.conf.get("ignore", {}).items()]
    
    def get_timetuple(self, entry):
        timetuple = entry.get("published_parsed", entry.get("updated_parsed"))
        
        if not timetuple:
            date_raw = entry.get("published", entry.get("updated"))
            custom_format = self.conf.get("date_format")
            
            if date_raw and custom_format:
                try:
                    timetuple = datetime.strptime(date_raw, custom_format).timetuple()
                except ValueError as e:
                    logger.debug("Could not parse publication date in feed %s: %s", self.feed_id, e)
                    
        return timetuple
    
    def generate(self, parser, out, now):
        feed_obj = self.get_obj(parser)
        
        # Create ignore filter
        ignore = self.get_ignore()
        
        # Feed title
        title = self.conf.get("title") or feed_obj.feed.title
        
        # Feed classes
        classes = ["feed", self.conf.get("category", "uncategorised")]
        if "hide" in self.conf:
            classes.append("hide")
            
        # Write header
        out.write(self.FEEDHEADER.safe_substitute(feedtitle=title, classes=" ".join(classes)))
        
        # Per-feed limits
        max_num = self.conf.get("max_entry_num", 0)
        max_age = self.conf.get("max_entry_age", 0)
        
        # Feed timezone
        feed_tz = self.get_timezone() or timezone.utc
        
        num_entries = 0
        
        # Process entries
        for e in feed_obj.entries:
            # Skip ignored
            if any(r.search(e.get(f, "")) for f, r in ignore):
                continue
            
            # Stop if number limit exceeded
            num_entries += 1
            if max_num and num_entries > max_num:
                break;
            
            self.write_entry(e, out, now, title, feed_tz, max_age)
        
        # Write footer
        out.write(self.FEEDFOOTER)
        

    def write_entry(self, e, out, now, feedtitle, feed_tz, max_age):
        # Default entry classes
        classes = ["entry"]
        
        # Process entry publication date
        date_tuple = self.get_timetuple(e)
        
        if date_tuple:
            # Naive tuple in UTC (or custom feed tz) -> aware datetime in UTC (or custom feed tz) -> aware datetime in localtime
            date_obj = datetime.fromtimestamp(calendar.timegm(date_tuple), feed_tz).astimezone(now.tzinfo)
        else:
            # Fall back to time of feed fetch (bad, but what can you do?)
            date_obj = now
            logger.warning("Falling back to now as publication date in feed %s.", self.feed_id)
        
        age = now - date_obj
        
        if max_age and age.days > max_age:
            return
        
        if age.days < 1:
            classes.append("today")
        
        if age.days < 7:
            classes.append("thisweek")
            
        # Todo make this more precise?
            
        if age.days < 30:
            classes.append("thismonth")
            
        if age.days < 365:
            classes.append("thisyear")
        
        # This will be in localtime
        date_str = date_obj.strftime("%b %d")
        
        classes_str = " ".join(classes)
        
        # Don't load images for hidden elements
        description = e.description.replace("<img ", "<img loading='lazy' ") # TODO parse this more nicely?
        
        # Write entry
        out.write(self.ENTRY.safe_substitute(classes=classes_str, date=date_str, link=e.link, title=e.title, blurb=description, feedtitle=feedtitle))


class FakeObj:
    def get(self, name, default=None):
        return getattr(self, name, default) 


class Group(Feed):
    def __init__(self, feeds):
        self.feeds = feeds
        self.conf = {}
        self.feed_id = None
        self.calculate_conf()
        
    def calculate_conf(self):
        # Construct combined prefs from individual feed entries
        
        # Clear in place
        self.conf.clear()
        
        # Apply individual feed prefs in sequence
        self.conf.update(dict(ChainMap(*(f.conf for f in self.feeds))))
        del self.conf["url"] # The grouped feed has no url
        self.feed_id = self.conf["group"]
        del self.conf["group"] # The grouped feed is not itself in the group
    
    def update_url(self, url):
        # This should never be called
        raise NotImplementedError()
    
    def disable_url(self):
        # This should never be called
        raise NotImplementedError()
    
    def rename_category(self, old_name, new_name):
        for feed in self.feeds:
            if feed.conf.get("category") == old_name:
                feed.rename_category(old_name, new_name)
        self.calculate_conf()
    
    def get_obj(self, parser):
        feed_objs = []
        for feed in self.feeds:
            try:
                feed_obj = parser.get_feed_obj(feed)
                feed_objs.append(feed_obj)
            except ValueError as e:
                logging.debug(e)
                continue
        if not feed_objs:
            raise ValueError("No valid feed found in group %s.", self.feed_id)
        
        group_obj = FakeObj()
        group_obj.feed = FakeObj()
        group_obj.feed.title = self.feed_id.capitalize()
        group_obj.entries = sorted(chain.from_iterable(f.entries for f in feed_objs), key=lambda e: self.get_timetuple(e), reverse=True)
        
        return group_obj
        

class Digest(Feed):
    def __init__(self, feed):
        self.feed = feed
        self.conf = {}
        self.feed_id = None
        self.calculate_conf()
    
    def calculate_conf(self):
        # Copy prefs from base feed
        
        # Clear in place
        self.conf.clear()
        
        # Update from base feed
        self.conf.update(self.feed.conf)
            
        self.feed_id = self.feed.feed_id
        
    def update_url(self, url):
        self.feed.update_url(url)
        self.calculate_conf()

    def disable_url(self):
        self.feed.disable_url()
        self.calculate_conf()
        
    def rename_category(self, old_name, new_name):
        self.feed.rename_category(old_name, new_name)
        self.calculate_conf()

    def get_obj(self, parser):
        feed_obj = parser.get_feed_obj(self.feed)
        
        digest_obj = FakeObj()
        digest_obj.feed = FakeObj()
        digest_obj.feed.title = feed_obj.feed.title
        digest_obj.entries = []
        
        
        digest_conf = self.conf["digest"]
        interval = digest_conf.get("interval", "day")
        ignore = self.feed.get_ignore()
        
        # Group entries by interval
        digest_entries = defaultdict(list)
        for e in feed_obj.entries:
            # Skip ignored
            if any(r.search(e.get(f, "")) for f, r in ignore):
                continue
            
            dt = self.get_timetuple(e)
            if not dt:
                logger.debug("Omitting entry from digest in feed %s because a publication date could not be parsed.", self.feed_id)
                continue
            
            if interval == "hour":
                key = (dt.tm_year, dt.tm_yday, dt.tm_hour)
            elif interval == "day":
                key = (dt.tm_year, dt.tm_yday)
            elif interval == "week":
                key = (dt.tm_year, dt.tm_yday - dt.tm_wday)
            elif interval == "month":
                key = (dt.tm_year, dt.tm_mon)
            
            digest_entries[key].append(e)
            
        if not digest_entries:
            logger.error("Could not create digest for feed %s. Using original feed.", self.feed_id)
            return feed_obj
            
        # Remove ignore from self (because we ignored before digesting)
        if "ignore" in self.conf:
            del self.conf["ignore"]
        
        # Process grouped entries into digest entries
        for _, entries in sorted(digest_entries.items(), reverse=True):
            # Oldest first within each digest
            entries.reverse()
            
            dates = [self.get_timetuple(e) for e in entries]
            titles = [e.title for e in entries]
            links = [e.link for e in entries]
            descriptions = [e.description for e in entries]
            
            digest_e = FakeObj()
            digest_e.published_parsed = [sum(l)//len(l) for l in zip(*dates)]
            digest_e.description = "\n".join(f'<h1><a href="{l}">{t}</a></h1>\n{d}' for t, l, d in zip(titles, links, descriptions))
                    
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
                        digest_e.link = m.expand(digest_conf["link"])
                        digest_e.title = m.expand(digest_conf["title"])
                        break
                else:
                    # Ignore partial digests unless partial is set to 1
                    if not digest_conf.get("partial", False):
                        continue
            
            # Fall back to default link and/or title -- use the first real entry
            if not digest_e.get("title"):
                digest_e.title = f"{titles[0]}..."
            
            if not digest_e.get("link"):
                digest_e.link = links[0]
            
            digest_obj.entries.append(digest_e)
            
        return digest_obj;

class Filters:
    FILTER = Template("""
<input type="radio" id="$id" name="$name" $checked />
<label for="$id">$label</label>
""")
    
    ENTRYFILTERCSSRULES = Template("""
#filters:has(#$id:checked)~#main li:not(.$id) {
    display: none;
}

#filters:has(#$id:checked)~#main div.feed:not(:has(li.$id)) {
    display: none;
}
""")

    FEEDFILTERCSSRULES = Template("""
#filters:has(#$id:checked)~#main div.feed:not(.$id) {
    display: none;
}
""")

    AGELABELS = ("Today", "This week", "This month", "This year")
    
    def __init__(self, catids):
        # Age inputs
        ageids = []
        ages = [{"name": "agefilter", "id": "allages", "label": "All ages", "checked": "checked"}]
        for agelabel in self.AGELABELS:
            ageid = agelabel.replace(" ", "").lower()
            ageids.append(ageid)
            ages.append({"name": "agefilter", "id": ageid, "label": agelabel, "checked": ""})
        
        # Age input html
        self.age_filters = "\n".join(self.FILTER.safe_substitute(a) for a in ages)
        
        # Age CSS
        self.age_filter_css = "".join(self.ENTRYFILTERCSSRULES.safe_substitute({"id": fi}) for fi in ageids)
        
        # Category inputs
        categories = [{"name": "catfilter", "id": "allcats", "label": "All categories", "checked": "checked"}]
        for catid in sorted(catids):
            catlabel = catid.replace("_", " ").capitalize()
            categories.append({"name": "catfilter", "id": catid, "label": catlabel, "checked": ""})
            
        # Category input HTML
        self.cat_filters = "\n".join(self.FILTER.safe_substitute(c) for c in categories)
        
        # Category CSS
        self.cat_filter_css = "".join(self.FEEDFILTERCSSRULES.safe_substitute({"id": fi}) for fi in catids)
    
    def write_css(self, file_path):
        logger.info("Writing generated CSS for filter rules.")
        with open(file_path, "w") as f:
            f.write(self.age_filter_css)
            f.write(self.cat_filter_css)


class Tonguefish:
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
$age_filters
<hr/>
$cat_filters
</div>
<div id="main">
""")

    REFRESH = Template("""
<meta http-equiv="refresh" content="$interval">
""")

    STYLESHEET = Template("""
<link rel="stylesheet" href="$stylesheet"/>
""")

    FOOTER = """
</div>
</body>
</html>
"""
    def __init__(self, input_dir, output_dir, cache_dir):
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.cache_dir = cache_dir
        
        self.conf_path = os.path.join(input_dir, "feeds.toml")
        self.stylesheet_paths = glob.glob(os.path.join(self.input_dir, "*.css"))
        self.filter_path = os.path.join(output_dir, "tonguefilter.css")
        self.output_path = os.path.join(output_dir, "index.html")
        
        self.conf = MainConfig(self.conf_path)
        self.feeds = Feed.get_feeds(self.conf)
    
    def get_catids(self):
        # Characters to remove from category names
        CATIDREMOVE = re.compile("^[^a-zA-Z_]*|[^a-zA-Z_0-9]")
        
        catids = set()
        
        oldcatid = self.conf.get("category")
        if oldcatid:
            catid = CATIDREMOVE.sub("", oldcatid.replace(" ", "_"))
            if catid != oldcatid:
                logger.warning("Invalid default category name %s. Correcting to %s.", oldcatid, catid)
                self.conf.rename_default_category(oldcatid, catid)
        
        for oldcatid in self.conf.get("categories", {}).keys():
            catid = CATIDREMOVE.sub("", oldcatid.replace(" ", "_"))
            if catid != oldcatid:
                logger.warning("Invalid category name %s in categories. Correcting to %s.", oldcatid, catid)
                self.conf.rename_category_key(oldcatid, catid)
                
        for feed in self.feeds:
            oldcatid = feed.conf.get("category")
            if oldcatid:
                catid = CATIDREMOVE.sub("", oldcatid.replace(" ", "_"))
                catids.add(catid)
                if catid != oldcatid:
                    logger.warning("Invalid category name %s in feed %s. Correcting to %s.", oldcatid, feed.feed_id, catid)
                    feed.rename_category(oldcatid, catid)
            else:
                catids.add("uncategorised")
                
        return catids
    
    def generate(self, no_update, no_new):
        # Bail early if no feeds configured
        if not self.feeds:
            logger.warning("No feeds configured. Nothing to do.")
            sys.exit(0)
        
        parser = Feedparser(self.cache_dir, no_update, no_new)
            
        # Use a single "now" for the whole run
        localtz = self.conf.get_timezone()
        if localtz:
            now = datetime.now(localtz)
        else:
            # use system time
            now = datetime.now().astimezone()
        logger.info("Running tonguefish at %s", now.strftime("%a %d %b %Y, %H:%M"))
        
        # Copy input stylesheets
        for stylesheet in self.stylesheet_paths:
            try:
                shutil.copy(stylesheet, self.output_dir)
            except shutil.SameFileError:
                pass # File is the same
        
        # Create filters for entry ages and feed categories
        filters = Filters(self.get_catids())
        # Write the CSS
        filters.write_css(self.filter_path)

        # Refresh interval
        refresh_interval = self.conf.get("refresh_interval", 0)
        refresh = self.REFRESH.safe_substitute(interval=refresh_interval) if refresh_interval else ""
        
        # Generate stylesheet links
        stylesheets = "\n".join(self.STYLESHEET.safe_substitute(stylesheet=os.path.basename(s)) for s in (*self.stylesheet_paths, self.filter_path))

        # Generate page header
        header = self.HEADER.safe_substitute(refresh=refresh, stylesheets=stylesheets, age_filters=filters.age_filters, cat_filters=filters.cat_filters)
        
        with open(self.output_path, "w") as out:
            out.write(header)
            
            for feed in self.feeds:
                try:
                    feed.generate(parser, out, now)
                except ValueError as e:
                    logging.warning("Skipping feed %s: %s", feed.feed_id, e)
                    continue
                    
            out.write(self.FOOTER)
            
        self.conf.save(self.conf_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog='tonguefish', description='A static RSS and Atom feed aggregator')
    parser.add_argument('input_dir', help="The directory which contains feeds.toml, tonguefish.css, and any custom CSS files.")
    parser.add_argument('output_dir', help="The directory where index.html will be written and CSS files will be copied.")
    parser.add_argument('cache_dir', help="The directory where cached feed objects will be stored.")
    parser.add_argument('--no-update', action='store_true', help="Don't update existing feeds.")
    parser.add_argument('--no-new', action='store_true', help="Don't fetch missing feeds.")
    parser.add_argument('-v', '--verbose', action='count', default=0, help="Increase the logging verbosity. By default only warnings and errors are printed. -v turns on info. -vv turns on debug.")
    args = parser.parse_args()
    
    log_level = logging.WARNING
    if args.verbose == 1:
        log_level = logging.INFO
    elif args.verbose >= 2:
        log_level = logging.DEBUG
    
    logging.basicConfig(level=log_level)
    
    if args.no_update:
        logger.info("NO UPDATE option is enabled. Will not update existing feeds.")
    
    if args.no_new:
        logger.info("NO NEW option is enabled. Will not fetch missing feeds.")
    
    tonguefish = Tonguefish(args.input_dir, args.output_dir, args.cache_dir)
    tonguefish.generate(args.no_update, args.no_new)
