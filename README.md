# tonguefish

Yet another static RSS and Atom feed aggregator

## What's this?

This is a static generator which produces an extremely compact bird's-eye view of all your feeds on a single webpage. I hacked this together in a couple of days, and it's very alpha, but it's usable (at least by me).

## That sounds like a lot of stuff on one page.

Image links in item content are rewritten to load lazily, so that they are only fetched if you view the item, and `tonguefish` can request resized images from servers that offer them. However, if you have lots of feeds or your feeds are very busy, you can configure limits on how many entries are included. See below for details.

## Why's it called `tonguefish`?

Because it's flat.

# Basic features

Hovering over titles displays the content provided in the feed. You can filter the results by age (approximate) and by category.

All the navigation is done with pure CSS (no JavaScript), so you can use this without a webserver, by opening a local file.

There is no state; links appear as visited or unvisited according to your browser history. This is imperfect for various reasons, but a core design principle: it's trivial to implement, and eliminates the inbox zero mentality (which I personally dislike) from feed browsing.

Feeds are configured in a [TOML](https://toml.io/en/) file. Currently there is no tool for adding a feed from a URL, or for importing feeds from an OPML file, but I may add this option to the script.

The page can be configured to refresh periodically using a meta header. Updating the feeds has to be configured separately (for example with `cron`).

Parsed feed objects are pickled and saved in a cache. `tonguefish` attempts to use an eTag and / or `Last-Modified` header from the cached object to avoid downloading a feed that hasn't changed.

`tonguefish` detects when a feed has redirected permanently or been removed, and edits the config file to update or disable the URL, respectively.

## Basic feed configuration

Feeds are listed in a TOML AoT (array of tables). Each table in the array must at minimum contain a URL. A feed can also have exactly one category.

```toml
[[feeds]]
url = "https://example.com/rss"
category = "blog"
```

Most feed options can be specified globally (at the top level), in the category section, or in a single feed (although some of these options may not make sense). A more specific option will take precedence over a more general one.

```toml
[categories.blog]
max_entry_num = 20

[categories.blog.digest]
interval = "week"
```

Exceptions:
1. `url` and `title` can only be set per feed.
2. `timezone` and `tzoffset` are used to set your local time at the top level, a feed's local time at the feed level, and are ignored at the category level.
3. You can't set a category at the category level. You can, however, set a default category at the top level (which will replace `uncategorised`).

## Transformations

`tonguefish` can perform transformations on feeds to fix certain annoyances. 

### Limits

You can set limits on the maximum age of entries (in days) to be included, and/or the maximum number of entries to be included per feed.

You can set the `full_content` option to include the full text content of each entry, rather than just the summary (by default it's unset). However, there is no consistency in the way that different feeds use these fields: some don't provide the full content, and some put the full content *in* the summary, so either way you will probably see a mixture of content lengths. The option may be more helpful as a per-feed setting.

`tonguefish` recognises some image URLs as being rewritable to request a smaller image size from the server (currently only specific WordPress URLs). Set `max_img_width` to enable this (adjust the value to the typical pixel width of the feed preview area in your browser window).

```toml
# Set a global limit on the number of entries to include in each feed (0 to disable)
max_entry_num = 10

# Set a global limit on the age of entries to include in each feed (0 to disable)
max_entry_age = 365 # days

# Include full content of entries, if it's available
full_content = 1

# Resize images to this width before fetching, if possible
max_img_width = 500 # px

[[feeds]]
url = "https://example.com/rss"

# Include more entries from this feed
max_entry_num = 20

# No age limit on entries from this feed
max_entry_age = 0

# Don't include the full content from this feed
full_content = 0
```

Limits are applied after groups, digests, and ignore rules (see below).

### Title

The feed can be renamed.

```toml
[[feeds]]
url = "https://example.com/rss"
title = "Cool Example Feed"
```

### Bad dates

If a feed has publication dates in a format that `feedparser` doesn't recognise, you can specify your own format (to pass to `strptime`).

If a feed had publication dates in its local time rather than UTC, you can specify the timezone (using the same formats as your local timezone at the top level; see below).

```toml
[[feeds]]
url = "https://example.com/whatarestandards/rss"
date_format = "%A %b %d %Y %H:%M:%S"
timezone = "Africa/Johannesburg"
```

### Ignore

You can ignore entries that match certain patterns. Currently one regex is allowed per field (you can combine multiple regexes to apply to a single field with `|`).

```toml
[[feeds]]
url = "https://example.com/rss"

[feeds.ignore]
title = '[Ch]eese'
description = 'gouda|cheddar|gorgonzola'
```

### Digest

Multiple entries from one feed can be aggregated into digests (hourly, daily, weekly, or monthly). The intended use case is magazines which have no issue feed but post multiple entries for a single issue on one day, or over the course of a month, or webcomics which post a batch of updates at a time. An hourly digest could be useful for a very busy feed.

Ignore rules are applied before the digest aggregation. You can configure rules for extracting an identifier from a field in one of the component entries (entries will be checked until the first match is found) and using it to construct an aggregate link and title. If the oldest entry with this information falls off the end of the feed, the preceding entries which would be in the same digest will be discarded unless you set the `partial` property, which will cause them to be included with the fallback title and URL, which are copied from the first (oldest) item in the digest.

```toml
[[feeds]]
url = "https://example.com/comic/rss"

[feeds.digest]
interval = "day"

[[feeds]]
url = "https://example.com/magazine/rss"

[feeds.digest]
interval = "month"
id_source = "link"
id_find = 'https://example.com/magazine/editorial_issue_(\d+)/'
link = 'https://example.com/magazine/issues/\1/'
title = 'Issue \1'
```

### Group

Entries from multiple feeds can be aggregated into a single feed. The intended use case is grouping individual account feeds from a website which does not offer a single feed for all your subscriptions.

Additional custom properties (such as title, category, ignore or digest rules) that are defined on individual feeds in the same group are merged together and applied only to the final grouped feed. They should only be defined on a single feed in the group; merging multiple definitions is unsupported and can give unpredictable results.

```toml
[[feeds]]
url = "https://example.com/comic/rss"
group = "example"
title = "Cool example feeds"

[[feeds]]
url = "https://example.com/magazine/rss"
group = "example"

[[feeds]]
url = "https://example.com/news/rss"
group = "example"
```

### Hide

You can prevent a feed from appearing in the main `All categories` view (you can still see it if you select the category it belongs to). It makes the most sense to do this for an entire category.

```toml
[categories.news]
hide = 1
```

## How to use

### Prerequisites

`tonguefish` needs Python 3, [`feedparser`](https://feedparser.readthedocs.io/en/latest/) and [`tomlkit`](https://tomlkit.readthedocs.io/en/latest/). On Windows you will also need the `tzdata` package if you want to use IANA strings to configure local timezones.

You need an input directory, an output directory, and a cache directory. The input directory must contain at minimum a `feeds.toml` file and a copy of or symbolic link to the `tonguefish.css` stylesheet. Custom CSS can be placed in separate files in the input directory; they will be copied to the output directory. The `tonguefish.py` script is standalone, and can be run from / moved to any working directory.

`tonguefish` can be run with all downloads disabled (useful for changing configuration of existing feeds, or development of `tonguefish` itself), or with updates disabled but fetching of missing feeds enabled (useful for adding new feeds).

Because the page doesn't use any JavaScript, you can use it without a webserver, just by opening the file in your browser. Because the browser syncs visited state for you, you can run a copy locally on each device (you only have to sync your `feeds.toml` and any custom CSS). You can also host it on a webserver if you want to, but that's outside the scope of these instructions.

### Install

These are example instructions for Linux (a recent Ubuntu LTS release). `tonguefish` has not been tested on other operating systems; it probably works anywhere you can install Python 3.

You should probably do this inside a virtualenv instead of using `sudo`; do as I say, not as I do.

```shell
# Get the Python dependencies
sudo pip install feedparser tomlkit

# Get the repository
git clone https://github.com/confluence/tonguefish.git
```

### Configure

```shell
cd tonguefish
mkdir input output cache

# Set up basic config
ln -s ../tonguefish.css input

# Now edit feeds.toml to add your feeds
vim input/feeds.toml
```

### Run

```shell
# Run tonguefish
./tonguefish.py input output cache

# Open the generated webpage in your preferred browser
xdg-open output/index.html
```

### Adjust feed configuration

Once your current feeds have been downloaded and cached, you can re-run `tonguefish` with updates disabled while refining your configuration, so that you can reuse the cached feeds without constantly re-downloading, which would be annoying both for you and for the servers.

```shell
# Edit feeds.toml
vim input/feeds.toml

# Run tonguefish with all downloads disabled
./tonguefish.py --no-update --no-new input output cache
```

### Configure local time

If you're running `tonguefish` on a local computer, you can probably just let it guess the local time offset from your system. If you're running it on a remote computer in a different time zone, you can configure a specific local time for it to use.

```toml
# Set a timezone (IANA string)
timezone = "Africa/Johannesburg"

# Set a timezone (fixed hour offset)
tzoffset = 2
```

### Set up updates

Once you're happy with your configuration, you can make `tonguefish` run at regular intervals. Here is an example that configures your local user's `cron` to run `tonguefish` every hour on the hour.

```shell
# Edit your crontab
crontab -e
```

Enter this line (substituting full paths as appropriate), and save the crontab:

```cron
@hourly /path/to/tonguefish.py /path/to/input /path/to/output /path/to/cache >> /path/to/logfile 2>&1
```

A log can be useful for debugging, but if you don't want to log output, replace `>> /path/to/logfile` with `> /dev/null`. By default `tonguefish` will only print warnings and errors.

### Set up page refresh

You can configure a refresh interval in seconds at the top level of `feeds.toml`.

```toml
# How often to refresh the page (0 to disable; this does NOT update the feeds; you have to conigure that elsewhere)
refresh_interval = 600 # seconds
```

Technically you don't need to refresh more frequently than the feed update interval, but since it's difficult to align these events perfectly, I recommend refreshing a couple of times per update to make sure that you pick up changes within a reasonable time frame.

### Add a new feed

Edit `feeds.toml` to add your new feed, and then run `tonguefish` with updates disabled and new feed fetching enabled.

```shell
# Edit feeds.toml
vim input/feeds.toml

# Run tonguefish with updates disabled
./tonguefish.py --no-update input output cache
```

## Troubleshooting

You can run `tonguefish` with increased verbosity to see more information.

```shell
# Show info messages
./tonguefish.py -v input output cache

# Show debug messages
./tonguefish.py -vv input output cache
```

## Known issues

* There's no ordering of feeds. The order in the configuration is preserved, except that group feeds are added at the end.
* If the Python bindings for `libxml2` are installed, `feedparser` uses a more strict parser which chokes on feeds with missing namespace declarations. I could try to fix this by rewriting the feeds before parsing, but it's going to be annoying. In the meantime, a workaround is to automate downloading the feed to a local file and fixing the namespace, and use the local file path in the config instead.
