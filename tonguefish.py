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
import uuid
import traceback
import urllib
import xml.etree.ElementTree as ET

from string import Template
from zoneinfo import ZoneInfo
from datetime import datetime, timezone, timedelta
from collections import defaultdict, ChainMap
from collections.abc import MutableMapping

import tomlkit
import feedparser

logger = logging.getLogger("tonguefish")


class FakeObj(MutableMapping):
    def __init__(self):
        super().__setattr__("_d", defaultdict(FakeObj))

    def __delitem__(self, key):
        del self._d[key]

    def __getitem__(self, key):
        return self._d[key]

    def __iter__(self):
        return self._d.__iter__()

    def __len__(self):
        return len(self._d)

    def __setitem__(self, key, value):
        self._d[key] = value

    def __getattr__(self, name):
        try:
            return self._d[name]
        except KeyError:
            raise AttributeError(f"'FakeObj' object has no attribute '{name}'")

    def __setattr__(self, name, value):
        self._d[name] = value


class TempWriter:
    @classmethod
    def configure(cls, temp_dir):
        cls.temp_dir = os.path.join(temp_dir, f"tonguefish-{uuid.uuid4().hex}")

    @classmethod
    def clean(cls):
        shutil.rmtree(cls.temp_dir)

    def __init__(self, path, mode="w"):
        self.path = path
        self.temp_path = os.path.join(self.temp_dir, path)
        self.mode = mode
        self.temp_file = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.temp_path), exist_ok=True)
        self.temp_file = open(self.temp_path, self.mode)
        return self.temp_file

    def __exit__(self, exc_type, exc_value, traceback):
        if self.temp_file:
            self.temp_file.close()
            if not exc_type:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                shutil.move(self.temp_path, self.path)


class Cache:
    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self.seen = set()

    def get_cache_url(self, feed_url):
        url_hash = hashlib.sha1(feed_url.encode("utf-8")).hexdigest()
        return os.path.join(self.cache_dir, url_hash)

    def get(self, feed_url):
        cache_url = self.get_cache_url(feed_url)
        logger.debug("Loading cache for url %s from %s...", feed_url, cache_url)
        if os.path.isfile(cache_url):
            self.seen.add(cache_url)

            with open(cache_url, "rb") as f:
                feed_obj = pickle.load(f)  # TODO handle pickle version changing

            return feed_obj
        return None

    def put(self, feed_url, feed_obj, old_feed_url=None):
        cache_url = self.get_cache_url(feed_url)
        logger.debug("Saving cache for url %s to %s...", feed_url, cache_url)
        self.seen.add(cache_url)

        # We have to do this to stop pickle from blowing up because SAXParseException contains a closed file-like object
        # https://alligatr.co.uk/blog/valueerror/
        if feed_obj.bozo:
            logger.debug("%s: stripping error %s before pickling", feed_url, feed_obj.bozo_exception)
            del feed_obj["bozo_exception"]
            feed_obj["bozo"] = False

        with TempWriter(cache_url, "wb") as f:
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
            logger.debug("Updating feed %s...", url)
            etag = old_feed_obj.get("etag")
            modified = old_feed_obj.get("modified")
            feed_obj = feedparser.parse(url, etag=etag, modified=modified)
        else:
            logger.debug("Fetching new feed %s...", url)
            feed_obj = feedparser.parse(url)

        if feed_obj.status == 200:
            logger.info("%s: updated.", url)
            self.cache.put(url, feed_obj)
            return feed_obj

        elif old_feed_obj and feed_obj.status == 304:
            logger.info("%s: no update required.", url)
            return old_feed_obj

        elif feed_obj.status == 302:
            new_url = feed_obj.href
            logger.warning("%s has redirected temporarily to %s. Not editing config.", url, new_url)

            if not feed_obj.feed.keys():
                # The redirect is probably masking a 304
                if old_feed_obj:
                    logger.info("%s: no update required (probably).", url)
                    logger.debug(feed_obj)
                    return old_feed_obj
                raise ValueError("Received empty response with 302 status.")

            self.cache.put(url, feed_obj)
            return feed_obj

        elif feed_obj.status == 301:
            new_url = feed_obj.href
            logger.warning("%s has redirected permanently to %s -- updating config.", url, new_url)
            feed.update_url(new_url)

            if not feed_obj.feed.keys():
                # The redirect is probably masking a 304
                if old_feed_obj:
                    logger.info("%s: no update required (probably).", url)
                    logger.debug(feed_obj)
                    # Save the old object to the new location
                    self.cache.put(new_url, old_feed_obj, url)
                    return old_feed_obj
                raise ValueError("Received empty response with 301 status.")

            self.cache.put(new_url, feed_obj, url)
            return feed_obj

        elif feed_obj.status == 410:
            feed.disable_url()
            raise ValueError(f"{url}: Server returned 410 response (feed is gone).")

        else:
            raise ValueError(f"{url}: Server returned {feed_obj.status} response.")

    def get_feed_obj(self, feed):
        if "url" not in feed.conf:
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


class Config:
    @staticmethod
    def get_timezone(conf):
        timezone = conf.get("timezone")
        if timezone:
            # IANA string (preferred)
            return ZoneInfo(timezone)

        tzoffset = conf.get("tzoffset")
        if tzoffset:
            # Fixed hour offset
            return timezone(timedelta(hours=tzoffset))

        return None

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
            with TempWriter(file_path, "w") as f:
                f.write(data)


class Entry:
    ENTRY = Template("""
<li class="$classes">
    <span class="published">$date</span>
    <a href="$link">$entrytitle</a>
    <div class="entrycontent">
        <h1 class="feedtitle">$feedtitle</h1>
        <h1 class="entrytitle"><a href="$link">$entrytitle</a></h1>
        $entrycontent
    </div>
</li>
""")

    THUMBNAIL = Template("""
<a href="$link">
    <img src="$url" width="$width" height="$height" />
</a>
""")

    IMAGE = re.compile("<img .*?/?>")
    VIDEO = re.compile("<video .*?</video>")
    STYLE = re.compile(r"(.*?):(.*?)(?:;|$)")
    WXH = re.compile(r"(\d+)x(\d+)")

    def __init__(self, entry_obj, feed):
        self.entry_obj = entry_obj
        self.feed = feed
        self._content = None

    def ignore(self):
        if self.feed.ignore_link_rule and self.feed.ignore_link_rule.search(Feed.get_link(self.entry_obj)):
            return True
        if self.feed.ignore_content_rule and self.feed.ignore_content_rule.search(self.get_content()):
            return True
        for field, rule in self.feed.ignore_rules.items():
            if rule.search(getattr(self.entry_obj, field)):
                return True
        return False

    def get_timetuple(self):
        e = self.entry_obj
        timetuple = e.get("published_parsed", e.get("updated_parsed"))

        if not timetuple:
            date_raw = e.get("published", e.get("updated"))
            custom_format = self.feed.conf.get("date_format")

            if date_raw and custom_format:
                try:
                    timetuple = datetime.strptime(date_raw, custom_format).timetuple()
                except ValueError as err:
                    logger.debug("Could not parse publication date in feed %s: %s", self.feed.feed_id, err)

        return timetuple

    def fix_video(self, video_str):
        video = ET.fromstring(video_str)

        # Don't preload videos
        video.set("preload", "none")
        video.attrib.pop("autoplay", None)

        return ET.tostring(video, encoding="unicode")

    def fix_image(self, img_str):
        img = ET.fromstring(img_str)

        # Load images lazily
        img.set("loading", "lazy")

        # Maybe try fetching a smaller image from the server, and adjust the size
        if max_width := self.feed.conf.get("max_img_width"):
            width = height = None
            src = urllib.parse.urlparse(img.get("src"))

            if src.netloc == "images.nebula.tv":
                query = urllib.parse.parse_qs(src.query)
                width, height = max_width, max_width * 9 / 16
                query["width"] = max_width
                img.set("src", src._replace(query=urllib.parse.urlencode(query, doseq=True)).geturl())

            elif src.netloc == "substackcdn.com":
                if (width := float(img.get("width", 0))) and (height := float(img.get("height", 0))) and width > max_width:
                    width, height = max_width, height * max_width / width

                    path_parts = src.path.split('/')
                    params = path_parts[3].split(',')

                    for i, p in enumerate(params):
                        if p.startswith("w_"):
                            params[i] = f"w_{max_width}"
                            break

                    path_parts[3] = ','.join(params)
                    img.set("src", src._replace(path='/'.join(path_parts)).geturl())

            elif "/wp-content/" in src.path:
                # Redirect to WP cache, otherwise the resizing will not work
                if src.netloc != "i0.wp.com":
                    src = src._replace(scheme="https", netloc="i0.wp.com", path=f"{src.netloc}{src.path}")

                query = urllib.parse.parse_qs(src.query)

                if "resize" in query:
                    width, height = (int(n) for n in query["resize"][0].split(","))
                    del query["resize"]

                elif "w" in query:
                    width = int(query["w"][0])

                elif (width := img.get("width")) and (height := img.get("height")):
                    try:
                        width, height = float(width), float(height)
                    except ValueError:
                        width = height = None

                elif m := self.WXH.search(src.path):
                    width, height = (int(n) for n in m.groups())

                if not width or (width and width > max_width):
                    if height:
                        height = height * max_width / width
                    if width:
                        width = max_width
                    query["w"] = max_width

                img.set("src", src._replace(query=urllib.parse.urlencode(query, doseq=True)).geturl())

            if width and height:
                img.set("width", str(width))
                img.set("height", str(height))

        # Maybe set aspect ratio (for correct CSS resizing later)
        width, height = img.get("width"), img.get("height")
        if width and height:
            try:
                width, height = float(width), float(height)
            except ValueError:
                logger.debug("Couldn't set aspect ratio for non-numeric width %r and height %r.", width, height)
            else:
                aspect_ratio = height / width
                style = dict(self.STYLE.findall(img.get("style", "")))
                style["--aspect-ratio"] = str(aspect_ratio)
                img.set("style", " ".join(f"{k}: {v};" for k, v in style.items()))
        else:
            logger.debug("Couldn't set aspect ratio for width %r and height %r.", width, height)

        return ET.tostring(img, encoding="unicode")

    def get_content(self):
        if self._content:
            return self._content

        e = self.entry_obj

        thumbnail = None

        for thumb_dict in e.get("media_thumbnail", []):
            small_thumbnail = (thumb_dict["width"] == thumb_dict["height"] == "72")
            thumbnail = self.THUMBNAIL.safe_substitute(thumb_dict, link=Feed.get_link(e))
            break

        content_parts = []

        if self.feed.conf.get("full_content", False):
            if thumbnail and not small_thumbnail:
                content_parts.append(thumbnail)

            for content_dict in e.get("content", []):
                content_value = content_dict.get("value")
                content_type = content_dict.get("type")

                if content_type == "text/html" and content_value:
                    content_parts.append(content_value)
                    break
            else:
                content_parts.append(e.get("description", ""))

        else:
            if thumbnail and small_thumbnail:
                content_parts.append(thumbnail)

            content_parts.append(e.get("description", ""))

        content = "".join(content_parts)

        for image in self.IMAGE.findall(content):
            content = content.replace(image, self.fix_image(image))

        for video in self.VIDEO.findall(content):
            content = content.replace(video, self.fix_video(video))

        self._content = content
        return self._content

    def generate(self, out, now, feedtitle, feed_tz, max_age):
        e = self.entry_obj

        # Default entry classes
        classes = ["entry"]

        # Process entry publication date
        date_tuple = self.get_timetuple()

        if date_tuple:
            # Naive tuple in UTC (or custom feed tz) -> aware datetime in UTC (or custom feed tz) -> aware datetime in localtime
            date_obj = datetime.fromtimestamp(calendar.timegm(date_tuple), feed_tz).astimezone(now.tzinfo)
        else:
            # Fall back to time of feed fetch (bad, but what can you do?)
            date_obj = now
            logger.warning("Falling back to now as publication date in feed %s.", self.feed.feed_id)

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

        # Get content with fixes applied
        content = self.get_content()

        # Write entry
        out.write(self.ENTRY.safe_substitute(classes=classes_str, date=date_str, link=Feed.get_link(e), entrytitle=e.title, entrycontent=content, feedtitle=feedtitle))


class Feed:
    HEADER = Template("""
<div class="$classes">
<h1 class="feedtitle">$feedtitle</h1>
<div class="feedcontent">
    $feedcontent
</div>
<ul>
""")

    CONTENT = Template("""
<h1 class="feedtitle"><a href="$feedpageurl">$feedtitle</a>: <a class="feedurl" href="$feedurl">$feedurl</a></h1>
""")

    FOOTER = """
</ul>
</div>
"""

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

    @staticmethod
    def get_link(obj):
        if "link" in obj:
            return obj.link
        if "links" in obj:
            return obj.links[0].href
        raise ValueError("Could not find link for object.")

    def __init__(self, main_conf, feed_conf):
        self.main_conf = main_conf
        self.orig_conf = feed_conf
        self.conf = {}
        self.feed_id = None

        self.ignore_rules = {}
        self.ignore_link_rule = None
        self.ignore_content_rule = None

        self.calculate_conf()

    def calculate_conf(self):
        # Construct combined prefs from top-level, category and feed entries

        # Clear in place
        self.conf.clear()

        # Apply top-level prefs first
        self.conf.update({k: v for (k, v) in self.main_conf.conf.items() if k not in self.IGNORE_TOPLEVEL})

        # Then category prefs
        category = self.orig_conf.get("category", "uncategorised")
        category_conf = self.main_conf.get("categories", {}).get(category)
        if category_conf:
            self.conf.update({k: v for (k, v) in category_conf.items() if k not in self.IGNORE_CATEGORY})

        # Finally the per-feed prefs
        self.conf.update(self.orig_conf)

        self.feed_id = self.conf["url"]

    def calculate_ignore(self):
        self.ignore_rules = {field: re.compile(regex) for (field, regex) in self.conf.get("ignore", {}).items()}
        self.ignore_link_rule = self.ignore_rules.pop("link", None)
        self.ignore_content_rule = self.ignore_rules.pop("content", None)

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

    def get_content(self, feed_obj):
        # TODO make a helper for this too
        title = self.conf.get("title") or feed_obj.feed.title
        return self.CONTENT.safe_substitute(feedpageurl=Feed.get_link(feed_obj.feed), feedtitle=title, feedurl=self.conf["url"])

    def get_classes(self):
        classes = ["feed", self.conf.get("category", "uncategorised")]
        if "hide" in self.conf:
            classes.append("hide")
        return classes

    def generate(self, parser, out, now):
        logger.info("Generating feed %s...", self.feed_id)
        feed_obj = self.get_obj(parser)

        # Create ignore filter
        self.calculate_ignore()

        # Feed title
        title = self.conf.get("title") or feed_obj.feed.title

        # Feed classes
        classes = self.get_classes()

        # Write header
        out.write(self.HEADER.safe_substitute(feedtitle=title, feedcontent=self.get_content(feed_obj), classes=" ".join(classes)))

        # Per-feed limits
        max_num = self.conf.get("max_entry_num", 0)
        max_age = self.conf.get("max_entry_age", 0)

        # Feed timezone
        feed_tz = Config.get_timezone(self.conf) or timezone.utc

        num_entries = 0

        # Process entries
        for i, e in enumerate(feed_obj.entries):
            try:
                entry = Entry(e, self)

                # Skip ignored
                if entry.ignore():
                    continue

                entry.generate(out, now, title, feed_tz, max_age)

                # Stop if number limit exceeded
                num_entries += 1
                if max_num and num_entries > max_num:
                    break

            except (KeyError, AttributeError, ValueError) as err:
                logger.warning("Couldn't parse entry %s: %s", i, err)
                logger.debug(traceback.format_exc())
                continue

        # Write footer
        out.write(self.FOOTER)


class Group(Feed):
    CONTENT = Template("""
<h1 class="grouptitle">Group: $grouptitle</h1>
$feeds
""")

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
        del self.conf["url"]  # The grouped feed has no url
        self.feed_id = self.conf["group"]
        del self.conf["group"]  # The grouped feed is not itself in the group

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
            except ValueError as err:
                logging.debug(err)
                continue
        if not feed_objs:
            raise ValueError("No valid feed found in group %s.", self.feed_id)

        group_obj = FakeObj()
        group_obj.feed.title = self.feed_id.replace("_", " ").capitalize()

        seen = set()
        entries = []

        for f in feed_objs:
            for e in f.entries:
                l = Feed.get_link(e)
                if l not in seen:
                    entries.append(e)
                    seen.add(l)

        group_obj.entries = sorted(entries, key=lambda e: Entry(e, self).get_timetuple(), reverse=True)
        group_obj.feed_objs = feed_objs
        return group_obj

    def get_classes(self):
        return ["group", *super().get_classes()]

    def get_content(self, feed_obj):
        title = self.conf.get("title") or feed_obj.feed.title
        feedcontents = "".join(feed.get_content(f_obj) for (feed, f_obj) in zip(self.feeds, feed_obj.feed_objs))
        return self.CONTENT.safe_substitute(grouptitle=title, feeds=feedcontents)


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

    def get_classes(self):
        return ["digest", *self.feed.get_classes()]

    def get_obj(self, parser):
        feed_obj = parser.get_feed_obj(self.feed)

        digest_obj = FakeObj()
        digest_obj.feed.title = feed_obj.feed.title
        digest_obj.entries = []

        digest_conf = self.conf["digest"]
        interval = digest_conf.get("interval", "day")

        self.calculate_ignore()

        # Group entries by interval
        digest_entries = defaultdict(list)
        for e in feed_obj.entries:
            entry = Entry(e, self)

            # Skip ignored
            if entry.ignore():
                continue

            dt = entry.get_timetuple()
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

            digest_entries[key].append(entry)

        if not digest_entries:
            logger.error("Could not create digest for feed %s. Using original feed.", self.feed_id)
            return feed_obj

        # Remove ignore from self (because we ignored before digesting)
        if "ignore" in self.conf:
            del self.conf["ignore"]
            self.calculate_ignore()

        # Process grouped entries into digest entries
        for _, entries in sorted(digest_entries.items(), reverse=True):
            # Oldest first within each digest
            entries.reverse()

            dates = [e.get_timetuple() for e in entries]
            titles = [e.entry_obj.title for e in entries]
            links = [Feed.get_link(e.entry_obj) for e in entries]
            entrycontents = [e.get_content() for e in entries]

            digest_e = FakeObj()
            digest_e.published_parsed = [sum(l)//len(l) for l in zip(*dates)]
            digest_e.description = "\n".join(f'<h1><a href="{l}">{t}</a></h1>\n{d}' for t, l, d in zip(titles, links, entrycontents))

            # Try to generate title and link
            if "id_find" in digest_conf and "id_source" in digest_conf:
                sources = {
                    "link": links,
                    "title": titles,
                    "content": entrycontents,
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

        return digest_obj


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
        with TempWriter(file_path, "w") as f:
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
    $favicons
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

    FAVICON = Template("""
<link rel="icon" href="$favicon"/>
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
        self.favicon_paths = glob.glob(os.path.join(self.input_dir, "favicon.*"))
        self.filter_path = os.path.join(output_dir, "tonguefilter.css")
        self.output_path = os.path.join(output_dir, "index.html")

        self.conf = Config(self.conf_path)
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
        localtz = Config.get_timezone(self.conf.conf)
        if localtz:
            now = datetime.now(localtz)
        else:
            # use system time
            now = datetime.now().astimezone()
        logger.info("Running tonguefish at %s", now.strftime("%a %d %b %Y, %H:%M"))

        # Copy input stylesheets and favicons
        os.makedirs(self.output_dir, exist_ok=True)
        for input_file in (*self.stylesheet_paths, *self.favicon_paths):
            try:
                shutil.copy(input_file, self.output_dir)
            except shutil.SameFileError:
                pass  # File is the same

        # Create filters for entry ages and feed categories
        filters = Filters(self.get_catids())
        # Write the CSS
        filters.write_css(self.filter_path)

        # Refresh interval
        refresh_interval = self.conf.get("refresh_interval", 0)
        refresh = self.REFRESH.safe_substitute(interval=refresh_interval) if refresh_interval else ""

        # Generate stylesheet links
        stylesheets = "\n".join(self.STYLESHEET.safe_substitute(stylesheet=os.path.basename(s)) for s in (*self.stylesheet_paths, self.filter_path))

        # Generate favicon links
        favicons = "\n".join(self.FAVICON.safe_substitute(favicon=os.path.basename(s)) for s in self.favicon_paths)

        # Generate page header
        header = self.HEADER.safe_substitute(refresh=refresh, stylesheets=stylesheets, favicons=favicons, age_filters=filters.age_filters, cat_filters=filters.cat_filters)

        with TempWriter(self.output_path, "w") as out:
            out.write(header)

            for feed in self.feeds:
                try:
                    feed.generate(parser, out, now)
                except ValueError as err:
                    logging.warning("Skipping feed %s: %s", feed.feed_id, err)
                    continue

            out.write(self.FOOTER)

        parser.cache.clean()
        self.conf.save(self.conf_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog='tonguefish', description='A static RSS and Atom feed aggregator that outputs a single compact webpage.')
    parser.add_argument('-a', "--action", help="Your chosen action. 'generate' writes out the output files. 'new' additionally downloads missing feeds. 'update' additionally updates existing feeds.  (default: %(default)s)", choices=("update", "new", "generate"), default="update")
    parser.add_argument('-i', '--input_dir', help="The directory which contains feeds.toml, tonguefish.css, and any custom CSS files (default: %(default)s).", default="./input")
    parser.add_argument('-o', '--output_dir', help="The directory where index.html will be written and CSS files will be copied (default: %(default)s).", default="./output")
    parser.add_argument('-c', '--cache_dir', help="The directory where cached feed objects will be stored (default: %(default)s).", default="./cache")
    parser.add_argument('-t', '--temp_dir', help="The directory where partial files will be written before being moved to the output directory, cache, or input directory (default: %(default)s).", default="/tmp")
    parser.add_argument('-v', '--verbose', action='count', default=0, help="Increase the logging verbosity. By default only warnings and errors are printed. -v turns on info. -vv turns on debug.")
    args = parser.parse_args()

    log_level = logging.WARNING
    if args.verbose == 1:
        log_level = logging.INFO
    elif args.verbose >= 2:
        log_level = logging.DEBUG

    logging.basicConfig(level=log_level)

    no_update = False if args.action == "update" else True
    no_new = True if args.action == "generate" else False

    if no_update:
        logger.info("Will not update existing feeds.")

    if no_new:
        logger.info("Will not fetch missing feeds.")

    TempWriter.configure(args.temp_dir)

    tonguefish = Tonguefish(args.input_dir, args.output_dir, args.cache_dir)
    tonguefish.generate(no_update, no_new)

    # Remove the temp directory if the generation was successful.
    # The output of interrupted runs is not removed, by design.
    TempWriter.clean()
