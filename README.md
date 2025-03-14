# tonguefish

Yet another static RSS and Atom feed aggregator

## What's this?

This is a static generator which produces an extremely compact bird's-eye view of all your feeds on a single webpage. 

## That sounds like a lot of stuff on one page.

Image links in item content are rewritten to load lazily, so that they are only fetched if you view the item. However, if you have lots of feeds or your feeds are very busy, you can configure limits on how many entries are included.

## Why's it called `tonguefish`?

Because it's flat.

# Basic features

Hovering over titles displays the content provided in the feed. You can filter the results by age (approximate) and by category.

All the navigation is done with pure CSS (no JavaScript), so you can use this without a webserver, by opening a local file.

There is no state; links appear as visited or unvisited according to your browser history. This is imperfect for various reasons, but a core design principle: it's trivial to implement, and eliminates the inbox zero mentality (which I personally dislike) from feed browsing.

Feeds are configured in a TOML file. Currently there is no tool for adding a feed from a URL, but I may write one.

The page can be configured to refresh periodically using a meta header. Updating the feeds has to be configured separately (for example with `cron`).

Parsed feed objects are pickled and saved in a cache. `tonguefish` attempts to use an eTag and / or `Last-Modified` header from the cached object to avoid downloading a feed that hasn't changed.

`tonguefish` detects when a feed has redirected permanently or been removed, and edits the config file to update or disable the URL, respectively.

## Basic feed configuration

Feeds are listed in a TOML AoT (array of tables). Each table in the array must at minimum contain a URL. A feed can also have a category.

```toml
[[feeds]]
url = "https://example.com/rss"
category = "blog"
```

## Transformations

`tonguefish` can perform transformations on feeds to fix certain annoyances.

### Limits

You can set limits on the maximum age of entries (in days) to be included, and/or the maximum number of entries to be included per feed. The top-level options can be overridden per feed. A value of `0` disables the option.

```toml
# Set a global limit on the number of entries to include in each feed (0 to disable)
max_entry_num = 10

# Set a global limit on the age of entries to include in each feed (0 to disable)
max_entry_age = 365 # days

[[feeds]]
url = "https://example.com/rss"

# Include more entries from this feed
max_entry_num = 20
# No age limit on entries from this feed
max_entry_age = 0
```

Limits are applied after groups, digests, and ignore rules (see below).

### Title

The feed can be renamed.

```toml
[[feeds]]
url = "https://example.com/rss"
title = "Cool Example Feed"
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

Multiple entries from one feed can be aggregated into daily or monthly digests. The intended use case is magazines which have no issue feed but post multiple entries for a single issue on one day, or over the course of a month, or webcomics which post a batch of updates at a time.

Ignore rules are applied before the digest aggregation. You can configure rules for extracting an identifier from a field in one of the component entries (entries will be checked until the first match is found) and using it to construct an aggregate link and title. If the oldest entry with this information falls off the end of the eed, the preceding entries which would be in the same digest will be discarded unless you set the `partial` property, which will cause them to be included with the fallback title and URL, which are copied from the first (oldest) item in the digest.

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

Groups from multiple feeds can be aggregated into a single feed. The intended use case is grouping individual account feeds from a website which does not offer a single feed for all your subscriptions.

Additional custom properties (such as title, category, ignore or digest rules) that are defined on feeds in the same group are merged together and applied only to the final grouped feed. They should only be defined on a single group in the feed; merging multiple definitions is unsupported and can give unpredictable results.

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

## How to use

### Prerequisites

`tonguefish` needs Python 3, `feedparser` and `tomlkit`.

*Known issue: if the Python bindings for `libxml2` are installed, `feedparser` uses a more strict parser which chokes on feeds with missing namespace declarations. I will try to fix this by rewriting the feeds before parsing.*

You need an input directory, an output directory, and a cache directory. The input directory must contain at minimum a `feeds.toml` file and a copy of or symbolic link to the `tonguefish.css` stylesheet. Custom CSS can be placed in separate files in the input directory; they will be copied to the output directory. The `tonguefish.py` script is standalone, and can be run from / moved to any working directory.

`feeds.toml` must have a timezone configured.

`tonguefish` can be run with feed updates disabled (useful for changing configuration of existing feeds, or development of `tonguefish` itself).

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
echo "timezone = \"`cat /etc/timezone`\"" > input/feeds.toml

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

# Run tonguefish with updates disabled
./tonguefish.py input output cache NOUPDATE
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

A log can be useful for debugging, but if you don't want to log output, replace `>> /path/to/logfile` with `> /dev/null`.

### Set up page refresh

Add a top-level `refresh`
