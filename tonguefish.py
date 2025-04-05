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
from collections import defaultdict
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
    seen = set()

    @classmethod
    def configure(cls, cache_dir):
        cls.cache_dir = cache_dir

    @classmethod
    def get_cache_url(cls, feed_url):
        url_hash = hashlib.sha1(feed_url.encode("utf-8")).hexdigest()
        return os.path.join(cls.cache_dir, url_hash)

    @classmethod
    def get(cls, feed_url):
        cache_url = cls.get_cache_url(feed_url)
        logger.debug("Loading cache for url %s from %s...", feed_url, cache_url)
        if os.path.isfile(cache_url):
            cls.seen.add(cache_url)

            with open(cache_url, "rb") as f:
                feed_obj = pickle.load(f)  # TODO handle pickle version changing

            return feed_obj
        return None

    @classmethod
    def put(cls, feed_url, feed_obj, old_feed_url=None):
        cache_url = cls.get_cache_url(feed_url)
        logger.debug("Saving cache for url %s to %s...", feed_url, cache_url)
        cls.seen.add(cache_url)

        # We have to do this to stop pickle from blowing up because SAXParseException contains a closed file-like object
        # https://alligatr.co.uk/blog/valueerror/
        if feed_obj.bozo:
            logger.debug("%s: stripping error %s before pickling", feed_url, feed_obj.bozo_exception)
            del feed_obj["bozo_exception"]
            feed_obj["bozo"] = False

        with TempWriter(cache_url, "wb") as f:
            pickle.dump(feed_obj, f)

        if old_feed_url:
            cls.seen.remove(cls.get_cache_url(old_feed_url))

    @classmethod
    def clean(cls):
        for cache_url in glob.glob(os.path.join(cls.cache_dir, "*")):
            if cache_url not in cls.seen:
                os.remove(cache_url)


class Config:
    TOPLEVEL_ONLY = {"feeds", "categories", "groups"}
    IGNORE_FROM_TOPLEVEL = {"url", "title", "group", "timezone", "tzoffset"}
    IGNORE_FROM_CATEGORY = {"url", "title", "group", "category", "timezone", "tzoffset"}
    IGNORE_FROM_GROUP = {"url", "title", "group", "digest"}

    GROUPCATNORM = re.compile("^[^a-zA-Z_]*|[^a-zA-Z_0-9]")

    DEFAULTS = {
        "category": "uncategorised",
        "max_entry_num": 0,
        "max_entry_age": 0,
        "refresh_interval": 600,
        "full_content": 0,
        "max_img_width": 0,
        "sort": 0,
        "hide": 0,
    }

    @classmethod
    def normalize(cls, name):
        return cls.GROUPCATNORM.sub("", name.replace(" ", "_"))

    @classmethod
    def configure(cls, file_path):
        cls.file_path = file_path

        with open(cls.file_path) as f:
            data = f.read()
            cls.checksum = hashlib.sha1(data.encode("utf-8")).hexdigest()
            cls.conf = tomlkit.parse(data)

        # Use a single "now" for the whole run
        localtz = cls.get_timezone()
        if localtz:
            cls.now = datetime.now(localtz)
        else:
            # use system time
            cls.now = datetime.now().astimezone()
        logger.info("Running tonguefish at %s", cls.now.strftime("%a %d %b %Y, %H:%M"))

    @staticmethod
    def get_link(obj):
        if "link" in obj:
            return obj.link
        if "links" in obj:
            return obj.links[0].href
        raise ValueError("Could not find link for object.")

    @classmethod
    def get(cls, key, default=None):
        return cls.conf.get(key, default)

    @classmethod
    def get_timezone(cls, conf=None):
        conf = conf or cls.conf

        timezone = conf.get("timezone")
        if timezone:
            # IANA string (preferred)
            return ZoneInfo(timezone)

        tzoffset = conf.get("tzoffset")
        if tzoffset:
            # Fixed hour offset
            return timezone(timedelta(hours=tzoffset))

        return None

    @classmethod
    def get_feed_confs(cls):
        feed_confs = []

        for feed_conf in cls.get("feeds", []):
            conf = {}

            FEED_EXCLUDE = set(cls.TOPLEVEL_ONLY)

            if group := feed_conf.get("group"):
                # If this feed is in a group, start with the shared group conf
                conf.update(cls.get_group_conf(group, for_feed=True))

                # Ignore per-feed category later
                FEED_EXCLUDE |= {"category"}

            else:
                # Not in a group; construct individual feed prefs

                # First the defaults
                conf.update(cls.DEFAULTS)

                # Then the top-level prefs
                conf.update({k: v for (k, v) in cls.conf.items() if k not in cls.IGNORE_FROM_TOPLEVEL | cls.TOPLEVEL_ONLY})

                # Then per-category prefs
                category = feed_conf.get("category", conf["category"])
                if category_conf := cls.get("categories", {}).get(cls.normalize(category)):
                    conf.update({k: v for (k, v) in category_conf.items() if k not in cls.IGNORE_FROM_CATEGORY | cls.TOPLEVEL_ONLY})

            # Then the per-feed prefs
            conf.update({k: v for (k, v) in feed_conf.items() if k not in FEED_EXCLUDE})

            # Then include a reference to the original conf (for modifying the URL)
            conf["_original"] = feed_conf

            feed_confs.append(conf)

        return feed_confs

    @classmethod
    def get_group_conf(cls, group, for_feed=False):
        GROUP_EXCLUDE = cls.IGNORE_FROM_GROUP | cls.TOPLEVEL_ONLY if for_feed else cls.TOPLEVEL_ONLY
        # First the defaults
        conf = dict(cls.DEFAULTS)

        # Then the top-level prefs
        conf.update({k: v for (k, v) in cls.conf.items() if k not in cls.IGNORE_FROM_TOPLEVEL | cls.TOPLEVEL_ONLY})

        # Then determine the group category
        category = conf["category"]
        group_conf = None
        if group and (group_conf := cls.get("groups", {}).get(cls.normalize(group))):
            category = group_conf.get("category", category)

        # Then the per-category prefs
        if category_conf := cls.get("categories", {}).get(cls.normalize(category)):
            conf.update({k: v for (k, v) in category_conf.items() if k not in cls.IGNORE_FROM_CATEGORY | cls.TOPLEVEL_ONLY})

        # Then the per-group prefs
        if group_conf:
            conf.update({k: v for (k, v) in group_conf.items() if k not in GROUP_EXCLUDE})

        # Then apply the fixed category on top
        conf["category"] = category

        return conf

    @classmethod
    def save(cls):
        data = tomlkit.dumps(cls.conf)
        checksum = hashlib.sha1(data.encode("utf-8")).hexdigest()
        if checksum != cls.checksum:
            logger.warning("Writing out config, which has been modified.")
            with TempWriter(cls.file_path, "w") as f:
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

    INFO = Template("""
<p class="entryinfo">$entryinfo</p>
""")

    IMAGE = re.compile("<img .*?/?>")
    VIDEO = re.compile("<video .*?</video>")
    STYLE = re.compile(r"(.*?):(.*?)(?:;|$)")
    WXH = re.compile(r"(\d+)x(\d+)")

    def __init__(self, entry_obj, feed):
        self.entry_obj = entry_obj
        self.feed = feed
        self._content = None
        self._timetuple = None
        self._date_obj = None

    def ignore(self):
        rules = dict(self.feed.ignore_rules)
        if (rule := rules.pop("link", None)) and rule.search(self.get_link()):
            return True
        if (rule := rules.pop("content", None)) and rule.search(self.get_content()):
            return True
        for field, rule in rules.items():
            if rule.search(getattr(self.entry_obj, field)):
                return True
        return False

    def get_link(self):
        return Config.get_link(self.entry_obj)

    def get_title(self):
        title = self.entry_obj.title
        if rule := self.feed.strip_rules.get("title"):
            title = rule.sub("", title)
        return title

    def get_timetuple(self):
        if not self._timetuple:
            e = self.entry_obj
            timetuple = e.get("published_parsed", e.get("updated_parsed"))

            if not timetuple:
                date_raw = e.get("published", e.get("updated"))
                custom_format = self.feed.conf.get("date_format")

                if date_raw and custom_format:
                    try:
                        timetuple = datetime.strptime(date_raw, custom_format).timetuple()
                    except ValueError as err:
                        logger.debug("Could not parse publication date in feed %s: %s", self.feed.get_title(), err)

            self._timetuple = timetuple

        return self._timetuple

    def get_date_obj(self):
        if not self._date_obj:
            now = Config.now
            date_tuple = self.get_timetuple()

            if date_tuple:
                # Naive tuple in UTC (or custom feed tz) -> aware datetime in UTC (or custom feed tz) -> aware datetime in localtime
                self._date_obj = datetime.fromtimestamp(calendar.timegm(date_tuple), self.feed.get_timezone()).astimezone(now.tzinfo)
            else:
                # Fall back to time of feed fetch (bad, but what can you do?)
                self._date_obj = now
                logger.warning("Falling back to now as publication date in feed %s.", self.feed.get_title())

        return self._date_obj

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

                elif (w := img.get("width")) and (h := img.get("height")):
                    try:
                        width, height = float(w), float(h)
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
        if not self._content:
            e = self.entry_obj

            content_parts = []

            infoparts = []
            if author := e.get("author"):
                infoparts.append(author)
            infoparts.append(self.get_date_obj().strftime("%d %b %Y, %H:%M:%S"))
            entryinfo = ", ".join(infoparts)
            content_parts.append(self.INFO.safe_substitute(entryinfo=entryinfo))

            thumbnail = None

            for thumb_dict in e.get("media_thumbnail", []):
                small_thumbnail = (thumb_dict["width"] == thumb_dict["height"] == "72")
                thumbnail = self.THUMBNAIL.safe_substitute(thumb_dict, link=self.get_link())
                break

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

            if rule := self.feed.strip_rules.get("content"):
                content = rule.sub("", content)

            self._content = content
        return self._content

    def generate(self, out, feedtitle, max_age):
        # Default entry classes
        classes = ["entry"]

        # Process entry publication date
        date_obj = self.get_date_obj()

        age = Config.now - date_obj

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
        out.write(self.ENTRY.safe_substitute(classes=classes_str, date=date_str, link=self.get_link(), entrytitle=self.get_title(), entrycontent=content, feedtitle=feedtitle))


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

    @classmethod
    def configure(cls, action):
        no_update = False if action == "update" else True
        no_new = True if action == "generate" else False

        if no_update:
            logger.info("Will not update existing feeds.")

        if no_new:
            logger.info("Will not fetch missing feeds.")

        cls.no_update = no_update
        cls.no_new = no_new

        feeds = []
        groups = defaultdict(list)

        for feed_conf in Config.get_feed_confs():
            feed = Feed(feed_conf)

            if "digest" in feed.conf:
                feed = Digest(feed)

            if group := feed.conf.get("group"):
                group_id = Config.normalize(group)
                groups[group_id].append(feed)
            else:
                feeds.append(feed)

        for group_id, grouped_feeds in groups.items():
            group_conf = Config.get_group_conf(group_id)

            feed = Group(group_conf, group_id, grouped_feeds)

            if "digest" in feed.conf:
                feed = Digest(feed)

            feeds.append(feed)

        cls.feed_list = feeds

    def __init__(self, feed_conf):
        self.conf = feed_conf

        # Compile ignore and strip rules for entries
        self.ignore_rules = {}
        self.strip_rules = {}

        def merge_and_compile(conf_key, destination_dict):
            aggregator = defaultdict(list)
            for ruleset in self.conf.get(conf_key, {}).values():
                for field, rule in ruleset.items():
                    aggregator[field].append(rule)
            for field, rules in aggregator.items():
                destination_dict[field] = re.compile("|".join(rules))

        merge_and_compile("ignore", self.ignore_rules)
        merge_and_compile("strip", self.strip_rules)

        self.feed_obj = None

    def update_url(self, url):
        old_url = self.conf["url"]
        self.conf["_orig"]["url"] = url
        self.conf["_orig"]["url"].comment(f"# Updated automatically from {old_url}")
        self.conf["url"] = url

    def disable_url(self):
        old_url = self.conf["url"]
        self.conf["_orig"]["url_disabled"] = old_url
        self.conf["_orig"]["url_disabled"].comment("# This feed is gone and should be removed.")
        del self.conf["_orig"]["url"]
        del self.conf["url"]

    def get_classes(self):
        classes = ["feed", Config.normalize(self.conf.get("category"))]
        if self.conf["hide"]:
            classes.append("hide")
        return classes

    def get_timezone(self):
        return Config.get_timezone(self.conf) or timezone.utc

    def update_obj(self, old_feed_obj=None):
        url = self.conf["url"]

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
            Cache.put(url, feed_obj)
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

            Cache.put(url, feed_obj)
            return feed_obj

        elif feed_obj.status == 301:
            new_url = feed_obj.href
            logger.warning("%s has redirected permanently to %s -- updating config.", url, new_url)
            self.update_url(new_url)

            if not feed_obj.feed.keys():
                # The redirect is probably masking a 304
                if old_feed_obj:
                    logger.info("%s: no update required (probably).", url)
                    logger.debug(feed_obj)
                    # Save the old object to the new location
                    Cache.put(new_url, old_feed_obj, url)
                    return old_feed_obj
                raise ValueError("Received empty response with 301 status.")

            Cache.put(new_url, feed_obj, url)
            return feed_obj

        elif feed_obj.status == 410:
            self.disable_url()
            raise ValueError(f"{url}: Server returned 410 response (feed is gone).")

        else:
            raise ValueError(f"{url}: Server returned {feed_obj.status} response.")

    def get_obj(self):
        if "url" not in self.conf:
            raise ValueError("No URL found in feed config.")
        url = self.conf["url"]

        feed_obj = Cache.get(url)

        if feed_obj:
            if self.no_update:
                return feed_obj
            return self.update_obj(feed_obj)

        else:
            if self.no_new:
                raise ValueError(f"{url}: Feed is not in cache and fetching of missing feeds is disabled.")
            return self.update_obj()

    def fetch(self):
        logger.info("Fetching feed %s...", self.conf["url"])
        self.feed_obj = self.get_obj()

    def get_link(self):
        if not self.feed_obj:
            raise ValueError("No feed object available.")
        return Config.get_link(self.feed_obj.feed)

    def get_title(self):
        if not self.feed_obj:
            raise ValueError("No feed object available.")
        return self.conf.get("title") or self.feed_obj.feed.title

    def get_content(self):
        if not self.feed_obj:
            raise ValueError("No feed object available.")
        return self.CONTENT.safe_substitute(feedpageurl=self.get_link(), feedtitle=self.get_title(), feedurl=self.conf["url"])

    def get_entries(self):
        if not self.feed_obj:
            raise ValueError("No feed object available.")

        entries = []

        for e in self.feed_obj.entries:
            entry = Entry(e, self)
            if entry.ignore():
                continue
            entries.append(entry)

        if self.conf["sort"]:
            entries = sorted(entries, key=lambda e: e.get_timetuple(), reverse=True)

        return entries

    def generate(self, out):
        logger.info("Generating feed %s...", self.get_title())

        # Feed title
        title = self.get_title()

        # Feed classes
        classes = self.get_classes()

        # Write header
        out.write(self.HEADER.safe_substitute(feedtitle=title, feedcontent=self.get_content(), classes=" ".join(classes)))

        # Per-feed limits
        max_num = self.conf.get("max_entry_num", 0)
        max_age = self.conf.get("max_entry_age", 0)

        num_entries = 1

        # Process entries
        for entry in self.get_entries():
            # Stop if number limit exceeded
            if max_num and num_entries > max_num:
                break

            try:
                entry.generate(out, title, max_age)
                num_entries += 1

            except (KeyError, AttributeError, ValueError) as err:
                logger.warning("Couldn't parse entry: %s", err)
                logger.debug(traceback.format_exc())
                continue

        # Write footer
        out.write(self.FOOTER)


class Group(Feed):
    CONTENT = Template("""
<h1 class="grouptitle">Group: $grouptitle</h1>
$feeds
""")

    def __init__(self, group_conf, group_id, feeds):
        self.conf = group_conf
        self.group_id = group_id
        self.feeds = feeds

    def update_url(self, url):
        # This should never be called
        raise NotImplementedError()

    def disable_url(self):
        # This should never be called
        raise NotImplementedError()

    def update_obj(self, old_feed_obj=None):
        # This should never be called
        raise NotImplementedError()

    def get_classes(self):
        return ["group", *super().get_classes()]

    def fetch(self):
        logger.info("Fetching group %s...", self.get_title())
        for feed in self.feeds:
            feed.fetch()

    def get_obj(self):
        # This should never be called
        raise NotImplementedError()

    def get_link(self):
        # This should never be called
        raise NotImplementedError()

    def get_title(self):
        return self.conf.get("title", self.group_id.replace('_', ' ').capitalize())

    def get_content(self):
        title = self.get_title()
        feedcontents = "".join(feed.get_content() for feed in self.feeds)
        return self.CONTENT.safe_substitute(grouptitle=title, feeds=feedcontents)

    def get_entries(self):
        group_entries = []
        seen = set()

        for feed in self.feeds:
            for entry in feed.get_entries():
                if not entry.get_link() in seen:
                    group_entries.append(entry)
                    seen.add(entry.get_link())

        group_entries = sorted(group_entries, key=lambda e: e.get_timetuple(), reverse=True)
        return group_entries


class DigestEntry(Entry):
    def __init__(self, feed, link, title, timetuple, content):
        self.link = link
        self.title = title
        self.content = content

        now = Config.now
        self.date_obj = datetime.fromtimestamp(calendar.timegm(timetuple), feed.get_timezone()).astimezone(now.tzinfo)

    def ignore(self):
        # This should never be called
        raise NotImplementedError()

    def get_link(self):
        return self.link

    def get_title(self):
        return self.title

    def get_timetuple(self):
        # This should never be called
        raise NotImplementedError()

    def get_date_obj(self):
        return self.date_obj

    def fix_video(self, video_str):
        # This should never be called
        raise NotImplementedError()

    def fix_image(self, img_str):
        # This should never be called
        raise NotImplementedError()

    def get_content(self):
        return self.content


class Digest(Feed):
    def __init__(self, feed):
        self.feed = feed
        self.conf = feed.conf

    def update_url(self, url):
        self.feed.update_url(url)

    def disable_url(self):
        self.feed.disable_url()

    def get_classes(self):
        return ["digest", *self.feed.get_classes()]

    def fetch(self):
        self.feed.fetch()

    def get_link(self):
        return self.feed.get_link()

    def get_title(self):
        return f"{self.feed.get_title()} (digest)"

    def get_content(self):
        return self.feed.get_content()  # add special sauce?

    def get_entries(self):
        original_entries = self.feed.get_entries()

        digest_conf = self.conf["digest"]
        interval = digest_conf.get("interval", "day")

        # Group entries by interval
        digests = defaultdict(list)

        for entry in original_entries:
            dt = entry.get_timetuple()
            if not dt:
                logger.debug("Omitting entry from digest in feed %s because a publication date could not be parsed.", self.get_title())
                continue

            if interval == "hour":
                key = (dt.tm_year, dt.tm_yday, dt.tm_hour)
            elif interval == "day":
                key = (dt.tm_year, dt.tm_yday)
            elif interval == "week":
                key = (dt.tm_year, dt.tm_yday - dt.tm_wday)
            elif interval == "month":
                key = (dt.tm_year, dt.tm_mon)

            digests[key].append(entry)

        if not digests:
            logger.error("Could not create digest for feed %s. Using original feed.", self.get_title())
            return original_entries

        # Process grouped entries into digest entries
        digest_entries = []

        for _, entries in sorted(digests.items(), reverse=True):
            # Oldest first within each digest
            entries.reverse()

            dates = [e.get_timetuple() for e in entries]
            titles = [e.get_title() for e in entries]
            links = [e.get_link() for e in entries]
            entrycontents = [e.get_content() for e in entries]

            digest_date = max(dates)
            digest_content = "\n".join(f'<h1><a href="{l}">{t}</a></h1>\n{c}' for t, l, c in zip(titles, links, entrycontents))
            digest_link = None
            digest_title = None

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
                        digest_link = m.expand(digest_conf["link"])
                        digest_title = m.expand(digest_conf["title"])
                        break
                else:
                    # Ignore partial digests unless partial is set to 1
                    if not digest_conf.get("partial", False):
                        continue

            # Fall back to default link and/or title -- use the first real entry
            if not digest_title:
                digest_title = f"{titles[0]}..."

            if not digest_link:
                digest_link = links[0]

            digest_entries.append(DigestEntry(self.feed, digest_link, digest_title, digest_date, digest_content))

        return digest_entries


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

    FILTER = Template("""
<input type="radio" id="$id" name="$name" $checked />
<label for="$id">$label</label>
""")

    AGELABELS = ("Today", "This week", "This month", "This year")

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

    FOOTER = """
</div>
</body>
</html>
"""

    def __init__(self, input_dir, output_dir):
        self.input_dir = input_dir
        self.output_dir = output_dir

        self.stylesheet_paths = glob.glob(os.path.join(self.input_dir, "*.css"))
        self.favicon_paths = glob.glob(os.path.join(self.input_dir, "favicon.*"))
        self.filter_path = os.path.join(output_dir, "tonguefilter.css")
        self.output_path = os.path.join(output_dir, "index.html")

    def generate_age_filters(self):
        ageids = []
        ages = [{"name": "agefilter", "id": "allages", "label": "All ages", "checked": "checked"}]
        for agelabel in self.AGELABELS:
            ageid = agelabel.replace(" ", "").lower()
            ageids.append(ageid)
            ages.append({"name": "agefilter", "id": ageid, "label": agelabel, "checked": ""})

        # Age input html
        filter_html = "\n".join(self.FILTER.safe_substitute(a) for a in ages)

        # Age CSS
        filter_css = "".join(self.ENTRYFILTERCSSRULES.safe_substitute({"id": fi}) for fi in ageids)

        return filter_html, filter_css

    def generate_cat_filters(self):
        catids = set()

        for feed in Feed.feed_list:
            catids.add(Config.normalize(feed.conf.get("category")))

        categories = [{"name": "catfilter", "id": "allcats", "label": "All categories", "checked": "checked"}]
        for catid in sorted(catids):
            catlabel = Config.get("categories", {}).get(catid, {}).get("title", catid.replace("_", " ").capitalize())
            categories.append({"name": "catfilter", "id": catid, "label": catlabel, "checked": ""})

        # Category input HTML
        filter_html = "\n".join(self.FILTER.safe_substitute(c) for c in categories)

        # Category CSS
        filter_css = "".join(self.FEEDFILTERCSSRULES.safe_substitute({"id": fi}) for fi in catids)

        return filter_html, filter_css

    def generate(self):
        # Bail early if no feeds configured
        if not Feed.feed_list:
            logger.warning("No feeds configured. Nothing to do.")
            sys.exit(0)

        # Copy input stylesheets and favicons
        os.makedirs(self.output_dir, exist_ok=True)
        for input_file in (*self.stylesheet_paths, *self.favicon_paths):
            try:
                shutil.copy(input_file, self.output_dir)
            except shutil.SameFileError:
                pass  # File is the same

        # Create filters for entry ages and feed categories
        age_filters, age_filter_css = self.generate_age_filters()
        cat_filters, cat_filter_css = self.generate_cat_filters()

        # Write the CSS
        logger.info("Writing generated CSS for filter rules.")
        with TempWriter(self.filter_path, "w") as f:
            f.write(age_filter_css)
            f.write(cat_filter_css)

        # Refresh interval
        refresh_interval = Config.get("refresh_interval", 0)
        refresh = self.REFRESH.safe_substitute(interval=refresh_interval) if refresh_interval else ""

        # Generate stylesheet links
        stylesheets = "\n".join(self.STYLESHEET.safe_substitute(stylesheet=os.path.basename(s)) for s in (*self.stylesheet_paths, self.filter_path))

        # Generate favicon links
        favicons = "\n".join(self.FAVICON.safe_substitute(favicon=os.path.basename(s)) for s in self.favicon_paths)

        # Generate page header
        header = self.HEADER.safe_substitute(refresh=refresh, stylesheets=stylesheets, favicons=favicons, age_filters=age_filters, cat_filters=cat_filters)

        with TempWriter(self.output_path, "w") as out:
            out.write(header)

            for feed in Feed.feed_list:
                try:
                    feed.fetch()
                    feed.generate(out)
                except ValueError as err:
                    logging.warning("Skipping feed %s: %s", feed.get_title(), err)
                    continue

            out.write(self.FOOTER)


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

    TempWriter.configure(args.temp_dir)
    Config.configure(os.path.join(args.input_dir, "feeds.toml"))
    Cache.configure(args.cache_dir)
    Feed.configure(args.action)

    tonguefish = Tonguefish(args.input_dir, args.output_dir)
    tonguefish.generate()

    # Remove the temp directory if the generation was successful.
    # The output of interrupted runs is not removed, by design.
    TempWriter.clean()
    # Remove unused files from cache
    Cache.clean()
    # Save modified config
    Config.save()
