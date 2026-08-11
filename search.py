"""
Trend Aggregator
-----------------
Pulls trending topics / keywords from Google Trends, YouTube, and Reddit
using free, official (or free-tier) access. No paid APIs required.

SETUP (one-time):
1. pip install pytrends praw google-api-python-client

2. YouTube Data API key (free):
   - Go to https://console.cloud.google.com/
   - Create a project -> Enable "YouTube Data API v3"
   - Create an API key (free tier: 10,000 units/day, plenty for this)
   - Paste it into YOUTUBE_API_KEY below

3. Reddit API credentials (free):
   - Go to https://www.reddit.com/prefs/apps
   - Click "create app" -> choose "script"
   - Copy the client_id (under the app name) and client_secret
   - Paste them into REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET below

Google Trends (pytrends) needs no key at all — it just talks to the public
trends.google.com endpoints.

USAGE:
   python trend_aggregator.py
   python trend_aggregator.py --niche "fitness" "personal finance" "ai tools"

MOMENTUM / "NEW & RISING" NOTE:
   Momentum requires comparing today's run to a previous run, so the script
   keeps a small local file (trend_history.json) next to itself. The first
   time you run it there's nothing to compare against yet — momentum data
   builds up from your second run onward. Run it daily (or via cron/Task
   Scheduler) for this to actually mean something.
"""

import argparse
import csv
import datetime as dt
import json
import math
import os
import sys
import time

# ---------------- CONFIG ----------------
YOUTUBE_API_KEY = "YOUR_YOUTUBE_API_KEY"
REDDIT_CLIENT_ID = "YOUR_REDDIT_CLIENT_ID"
REDDIT_CLIENT_SECRET = "YOUR_REDDIT_CLIENT_SECRET"
REDDIT_USER_AGENT = "trend-aggregator-script by u/yourusername"

DEFAULT_SUBREDDITS = ["all", "trending", "popular"]
DEFAULT_REGION = "US"  # 2-letter country code for Google Trends / YouTube
HISTORY_FILE = "trend_history.json"
RISING_THRESHOLD = 10  # min point/rank improvement to count as "rising"


# ---------------- GOOGLE TRENDS ----------------
def get_google_trends(region=DEFAULT_REGION, n=20):
    """Returns list of (keyword, source) tuples from Google Trends realtime/daily trends."""
    from pytrends.request import TrendReq

    pytrends = TrendReq(hl="en-US", tz=360)
    results = []
    try:
        df = pytrends.trending_searches(pn="united_states" if region == "US" else region.lower())
        for kw in df[0].tolist()[:n]:
            results.append((kw, "google_trends"))
    except Exception as e:
        print(f"[Google Trends] Error: {e}", file=sys.stderr)
    return results


def get_google_trends_by_topic(keywords, region=DEFAULT_REGION):
    """Compare relative interest for a list of seed keywords (your 'niches')."""
    from pytrends.request import TrendReq

    pytrends = TrendReq(hl="en-US", tz=360)
    rows = []
    # pytrends allows max 5 keywords per request
    for i in range(0, len(keywords), 5):
        batch = keywords[i:i + 5]
        try:
            pytrends.build_payload(batch, timeframe="now 7-d", geo=region)
            df = pytrends.interest_over_time()
            if not df.empty:
                latest = df.iloc[-1]
                for kw in batch:
                    rows.append((kw, "google_trends_score", int(latest.get(kw, 0))))
            time.sleep(1)  # be polite to the unofficial endpoint
        except Exception as e:
            print(f"[Google Trends topic] Error on {batch}: {e}", file=sys.stderr)
    return rows


# ---------------- YOUTUBE ----------------
def get_youtube_trending(region=DEFAULT_REGION, n=20):
    """Returns list of (title, channel, views, url) for trending videos."""
    from googleapiclient.discovery import build

    results = []
    if YOUTUBE_API_KEY == "YOUR_YOUTUBE_API_KEY":
        print("[YouTube] Skipped — add your API key in the config section.", file=sys.stderr)
        return results

    youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
    try:
        req = youtube.videos().list(
            part="snippet,statistics",
            chart="mostPopular",
            regionCode=region,
            maxResults=n,
        )
        res = req.execute()
        for item in res.get("items", []):
            title = item["snippet"]["title"]
            channel = item["snippet"]["channelTitle"]
            views = item["statistics"].get("viewCount", "0")
            url = f"https://www.youtube.com/watch?v={item['id']}"
            results.append((title, channel, views, url))
    except Exception as e:
        print(f"[YouTube] Error: {e}", file=sys.stderr)
    return results


def search_youtube_by_keyword(keyword, n=10):
    """Search YouTube for a specific niche keyword, sorted by view count (recent)."""
    from googleapiclient.discovery import build

    results = []
    if YOUTUBE_API_KEY == "YOUR_YOUTUBE_API_KEY":
        return results

    youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
    try:
        req = youtube.search().list(
            part="snippet",
            q=keyword,
            type="video",
            order="viewCount",
            publishedAfter=(dt.datetime.utcnow() - dt.timedelta(days=7)).isoformat("T") + "Z",
            maxResults=n,
        )
        res = req.execute()
        for item in res.get("items", []):
            title = item["snippet"]["title"]
            channel = item["snippet"]["channelTitle"]
            vid = item["id"]["videoId"]
            results.append((title, channel, f"https://www.youtube.com/watch?v={vid}"))
    except Exception as e:
        print(f"[YouTube search] Error on '{keyword}': {e}", file=sys.stderr)
    return results


def analyze_youtube_competition(keyword, n=15):
    """
    Pulls recent videos for a keyword and checks their view counts against
    the uploading channel's subscriber count. This is the actual signal for
    'can a new/faceless channel break in': videos that get views far beyond
    the channel's subscriber count mean the algorithm/topic is doing the
    work, not channel authority — that's a good sign for a new channel.
    Videos that only get views roughly equal to subscriber count mean the
    topic mostly reaches existing fans — harder for a newcomer to break into.
    """
    from googleapiclient.discovery import build

    if YOUTUBE_API_KEY == "YOUR_YOUTUBE_API_KEY":
        return None

    youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
    try:
        search_res = youtube.search().list(
            part="snippet",
            q=keyword,
            type="video",
            order="viewCount",
            publishedAfter=(dt.datetime.utcnow() - dt.timedelta(days=90)).isoformat("T") + "Z",
            maxResults=n,
        ).execute()

        video_ids = [item["id"]["videoId"] for item in search_res.get("items", [])]
        channel_ids = [item["snippet"]["channelId"] for item in search_res.get("items", [])]
        if not video_ids:
            return {"sample_size": 0}

        vid_stats = youtube.videos().list(part="statistics", id=",".join(video_ids)).execute()
        views_by_id = {v["id"]: int(v["statistics"].get("viewCount", 0)) for v in vid_stats.get("items", [])}

        unique_channel_ids = list(set(channel_ids))
        subs_by_channel = {}
        for i in range(0, len(unique_channel_ids), 50):  # API allows up to 50 ids per call
            batch = unique_channel_ids[i:i + 50]
            ch_stats = youtube.channels().list(part="statistics", id=",".join(batch)).execute()
            for c in ch_stats.get("items", []):
                subs = c["statistics"].get("subscriberCount")  # may be hidden
                subs_by_channel[c["id"]] = int(subs) if subs else None

        ratios = []
        small_channel_wins = 0  # videos from channels <20k subs that still got solid views
        for item, vid, cid in zip(search_res.get("items", []), video_ids, channel_ids):
            views = views_by_id.get(vid, 0)
            subs = subs_by_channel.get(cid)
            if subs and subs > 0:
                ratio = views / subs
                ratios.append(ratio)
                if subs < 20000 and views > 20000:
                    small_channel_wins += 1

        avg_views = sum(views_by_id.values()) / len(views_by_id) if views_by_id else 0
        avg_ratio = sum(ratios) / len(ratios) if ratios else 0

        return {
            "sample_size": len(video_ids),
            "avg_views_90d": round(avg_views),
            "avg_view_to_sub_ratio": round(avg_ratio, 2),
            "small_channel_breakthroughs": small_channel_wins,
        }
    except Exception as e:
        print(f"[YouTube competition] Error on '{keyword}': {e}", file=sys.stderr)
        return None


def get_trend_direction(keyword, region=DEFAULT_REGION):
    """
    Looks at 12 months of interest for a keyword and reports whether it's
    generally rising, falling, or flat — plus a shorter recent-window check
    (last ~12 weeks vs the 12 weeks before that) to catch a fresh uptick
    that the yearly average would smooth over.
    """
    from pytrends.request import TrendReq

    pytrends = TrendReq(hl="en-US", tz=360)
    try:
        pytrends.build_payload([keyword], timeframe="today 12-m", geo=region)
        df = pytrends.interest_over_time()
        if df.empty or keyword not in df.columns:
            return None
        values = df[keyword].tolist()

        half = len(values) // 2
        first_avg = sum(values[:half]) / half if half else 0
        second_avg = sum(values[half:]) / (len(values) - half) if (len(values) - half) else 0
        yearly_change_pct = round(((second_avg - first_avg) / first_avg) * 100, 1) if first_avg else 0

        recent = values[-12:]
        prior = values[-24:-12] if len(values) >= 24 else values[:max(len(values) - 12, 0)]
        recent_avg = sum(recent) / len(recent) if recent else 0
        prior_avg = sum(prior) / len(prior) if prior else 0
        recent_change_pct = round(((recent_avg - prior_avg) / prior_avg) * 100, 1) if prior_avg else 0

        return {
            "avg_interest": round(sum(values) / len(values), 1),
            "yearly_change_pct": yearly_change_pct,
            "recent_change_pct": recent_change_pct,
        }
    except Exception as e:
        print(f"[Trend direction] Error on '{keyword}': {e}", file=sys.stderr)
        return None


# ---------------- REDDIT ----------------
def get_reddit_trending(subreddits=DEFAULT_SUBREDDITS, n=15):
    """Returns list of (title, subreddit, score, url) for hot/top posts."""
    import praw

    results = []
    if REDDIT_CLIENT_ID == "YOUR_REDDIT_CLIENT_ID":
        print("[Reddit] Skipped — add your client id/secret in the config section.", file=sys.stderr)
        return results

    reddit = praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        user_agent=REDDIT_USER_AGENT,
    )
    for sub in subreddits:
        try:
            for post in reddit.subreddit(sub).hot(limit=n):
                if not post.stickied:
                    results.append((post.title, str(post.subreddit), post.score, post.url))
        except Exception as e:
            print(f"[Reddit] Error on r/{sub}: {e}", file=sys.stderr)
    return results


def search_reddit_by_keyword(keyword, n=10):
    import praw

    results = []
    if REDDIT_CLIENT_ID == "YOUR_REDDIT_CLIENT_ID":
        return results

    reddit = praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        user_agent=REDDIT_USER_AGENT,
    )
    try:
        for post in reddit.subreddit("all").search(keyword, sort="hot", time_filter="week", limit=n):
            results.append((post.title, str(post.subreddit), post.score, post.url))
    except Exception as e:
        print(f"[Reddit search] Error on '{keyword}': {e}", file=sys.stderr)
    return results


# ---------------- SCORING & MOMENTUM ----------------
def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


def normalize(value, max_value):
    """Log-scale normalize a raw count (views/upvotes) to 0-100."""
    if max_value <= 0 or value <= 0:
        return 0
    return round((math.log(value + 1) / math.log(max_value + 1)) * 100, 1)


def build_composite_general(google_rows, youtube_rows, reddit_rows):
    """
    Combine this run's three sources into one ranked list.
    Google Trends realtime has no volume number, so it's scored by rank
    position (1st = 100, last = lower). YouTube/Reddit use log-normalized
    view/upvote counts. All three land on a comparable 0-100 scale.
    """
    combined = []

    n = len(google_rows)
    for i, (kw, src) in enumerate(google_rows):
        score = round(100 - (i / max(n, 1)) * 100, 1)
        combined.append({"item": kw, "source": "google_trends", "score": score, "detail": ""})

    max_views = max([int(v) for *_ , v, _ in youtube_rows] or [1])
    for title, channel, views, url in youtube_rows:
        score = normalize(int(views), max_views)
        combined.append({"item": title, "source": "youtube", "score": score, "detail": f"{views} views ({channel})"})

    max_score_reddit = max([s for *_, s, _ in reddit_rows] or [1])
    for title, sub, score_r, url in reddit_rows:
        score = normalize(score_r, max_score_reddit)
        combined.append({"item": title, "source": "reddit", "score": score, "detail": f"{score_r} upvotes (r/{sub})"})

    combined.sort(key=lambda x: x["score"], reverse=True)
    return combined


def compute_momentum_general(google_rows, history, region):
    """
    Momentum for Google Trends realtime keywords only — it's the one source
    where the same keyword can plausibly reappear run over run at a new
    rank. YouTube/Reddit trending items are almost always different content
    each run, so 'momentum' for those isn't meaningful the same way.
    """
    key = f"google_realtime_{region}"
    prev = history.get(key, {})  # {keyword: rank}
    curr = {kw: i for i, (kw, src) in enumerate(google_rows)}

    new_items = []
    rising_items = []
    for kw, rank in curr.items():
        if kw not in prev:
            new_items.append(kw)
        else:
            improvement = prev[kw] - rank  # positive = moved up (lower rank number)
            if improvement >= 3:  # moved up at least 3 spots
                rising_items.append((kw, improvement))

    rising_items.sort(key=lambda x: x[1], reverse=True)
    history[key] = curr
    return new_items, rising_items


def compute_momentum_niche(niche_scores, history):
    """
    niche_scores: dict {niche: score} from this run's Google Trends interest.
    Compares to last run's scores for the same niches.
    """
    key = "niche_scores"
    prev = history.get(key, {})
    movers = []
    new_niches = []
    for niche, score in niche_scores.items():
        if niche not in prev:
            new_niches.append((niche, score))
        else:
            delta = score - prev[niche]
            if delta >= RISING_THRESHOLD:
                movers.append((niche, prev[niche], score, delta))
            elif delta <= -RISING_THRESHOLD:
                movers.append((niche, prev[niche], score, delta))  # falling too, shown with negative delta

    movers.sort(key=lambda x: x[3], reverse=True)
    history[key] = niche_scores
    return new_niches, movers


def score_niche_opportunity(niche, trend_data, competition_data, reddit_count):
    """
    Heuristic 0-100 opportunity score. This is a rough compass, not a
    guarantee — it combines three signals:
      - Is interest flat/rising (not declining)?
      - Are videos in this niche getting views beyond channel subscriber
        count (room for a newcomer), or do you need an existing audience?
      - Is there an active community around it (Reddit activity as proxy)?
    """
    score = 50  # neutral baseline
    notes = []

    if trend_data:
        if trend_data["recent_change_pct"] > 15:
            score += 15
            notes.append("recent uptick in search interest")
        elif trend_data["recent_change_pct"] < -15:
            score -= 15
            notes.append("search interest declining recently")
        if trend_data["yearly_change_pct"] > 10:
            score += 5
        elif trend_data["yearly_change_pct"] < -20:
            score -= 10
            notes.append("declining over the past year")
        if trend_data["avg_interest"] < 5:
            score -= 10
            notes.append("very low overall search volume")

    if competition_data and competition_data.get("sample_size", 0) > 0:
        ratio = competition_data.get("avg_view_to_sub_ratio", 0)
        if ratio > 3:
            score += 20
            notes.append("videos regularly out-earn channel subscriber count (good for newcomers)")
        elif ratio > 1:
            score += 8
        else:
            score -= 10
            notes.append("views mostly track existing subscriber base (harder to break in)")

        if competition_data.get("small_channel_breakthroughs", 0) >= 3:
            score += 10
            notes.append("multiple small channels breaking through recently")
    else:
        notes.append("no YouTube competition data (add API key, or too few recent videos)")

    if reddit_count >= 8:
        score += 5
        notes.append("active community discussion found")
    elif reddit_count == 0:
        score -= 5

    score = max(0, min(100, score))
    if score >= 70:
        tier = "Strong candidate"
    elif score >= 50:
        tier = "Worth exploring"
    else:
        tier = "Tough / saturated or declining"

    return score, tier, notes


# ---------------- OUTPUT ----------------
def save_csv(rows, filename, headers):
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)
    print(f"Saved {len(rows)} rows -> {filename}")


# ---------------- MAIN ----------------
def main():
    parser = argparse.ArgumentParser(description="Aggregate trending topics from Google, YouTube, Reddit.")
    parser.add_argument("--niche", nargs="*", help="Specific keywords/niches to check instead of general trending.")
    parser.add_argument("--region", default=DEFAULT_REGION, help="2-letter region code, e.g. US, GB, CA")
    args = parser.parse_args()

    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    history = load_history()

    if args.niche:
        print(f"\n=== Niche mode: {args.niche} ===\n")

        print("-- Google Trends interest scores --")
        g_rows = get_google_trends_by_topic(args.niche, region=args.region)
        for kw, _, score in g_rows:
            print(f"  {kw}: {score}")
        save_csv(g_rows, f"google_niche_scores_{timestamp}.csv", ["keyword", "source", "score"])

        yt_rows = []
        for kw in args.niche:
            print(f"\n-- YouTube results for '{kw}' --")
            for title, channel, url in search_youtube_by_keyword(kw):
                print(f"  {title} ({channel})")
                yt_rows.append((kw, title, channel, url))
        save_csv(yt_rows, f"youtube_niche_{timestamp}.csv", ["niche", "title", "channel", "url"])

        rd_rows = []
        rd_counts = {}
        for kw in args.niche:
            print(f"\n-- Reddit results for '{kw}' --")
            kw_results = search_reddit_by_keyword(kw)
            rd_counts[kw] = len(kw_results)
            for title, sub, score, url in kw_results:
                print(f"  [{score}] {title} (r/{sub})")
                rd_rows.append((kw, title, sub, score, url))
        save_csv(rd_rows, f"reddit_niche_{timestamp}.csv", ["niche", "title", "subreddit", "score", "url"])

        # ---- opportunity scoring: trend direction + competition analysis ----
        print("\n=== NICHE OPPORTUNITY REPORT ===")
        print("(Heuristic scoring — a compass, not a guarantee. See notes per niche.)\n")

        report_rows = []
        for kw in args.niche:
            trend_data = get_trend_direction(kw, region=args.region)
            time.sleep(1)
            comp_data = analyze_youtube_competition(kw)
            score, tier, notes = score_niche_opportunity(kw, trend_data, comp_data, rd_counts.get(kw, 0))

            print(f"--- {kw} ---")
            print(f"  Opportunity score: {score}/100 ({tier})")
            if trend_data:
                print(f"  Search trend: {trend_data['recent_change_pct']:+}% recent, "
                      f"{trend_data['yearly_change_pct']:+}% over 12mo (avg interest {trend_data['avg_interest']})")
            if comp_data and comp_data.get("sample_size", 0) > 0:
                print(f"  YouTube competition: avg {comp_data['avg_views_90d']} views/video (last 90d), "
                      f"view-to-sub ratio {comp_data['avg_view_to_sub_ratio']}, "
                      f"{comp_data['small_channel_breakthroughs']} small-channel breakthroughs")
            print(f"  Reddit activity: {rd_counts.get(kw, 0)} recent relevant posts found")
            for note in notes:
                print(f"  - {note}")
            print()

            report_rows.append((
                kw, score, tier,
                trend_data["recent_change_pct"] if trend_data else "",
                trend_data["yearly_change_pct"] if trend_data else "",
                comp_data.get("avg_view_to_sub_ratio", "") if comp_data else "",
                comp_data.get("small_channel_breakthroughs", "") if comp_data else "",
                rd_counts.get(kw, 0),
                "; ".join(notes),
            ))

        save_csv(report_rows, f"niche_opportunity_report_{timestamp}.csv", [
            "niche", "score", "tier", "recent_trend_pct", "yearly_trend_pct",
            "view_to_sub_ratio", "small_channel_breakthroughs", "reddit_posts_found", "notes",
        ])

        # ---- momentum for niches ----
        niche_scores = {kw: score for kw, _, score in g_rows}
        new_niches, movers = compute_momentum_niche(niche_scores, history)

        print("\n=== NEW & RISING (niches) ===")
        if new_niches:
            print("New this run (no prior data yet):")
            for niche, score in new_niches:
                print(f"  {niche}: {score}")
        if movers:
            print("Momentum vs last run:")
            for niche, prev_score, curr_score, delta in movers:
                direction = "UP" if delta > 0 else "DOWN"
                print(f"  {niche}: {prev_score} -> {curr_score} ({direction} {abs(delta)})")
        if not new_niches and not movers:
            print("  No prior run to compare yet, or no significant moves." if not history else
                  "  No significant moves since last run.")

        save_history(history)

    else:
        print("\n=== General trending ===\n")

        print("-- Google Trends (realtime) --")
        g_rows = get_google_trends(region=args.region)
        for kw, src in g_rows:
            print(f"  {kw}")
        save_csv(g_rows, f"google_trending_{timestamp}.csv", ["keyword", "source"])

        print("\n-- YouTube trending --")
        yt_rows = get_youtube_trending(region=args.region)
        for title, channel, views, url in yt_rows:
            print(f"  {title} ({channel}) - {views} views")
        save_csv(yt_rows, f"youtube_trending_{timestamp}.csv", ["title", "channel", "views", "url"])

        print("\n-- Reddit hot posts --")
        rd_rows = get_reddit_trending()
        for title, sub, score, url in rd_rows:
            print(f"  [{score}] {title} (r/{sub})")
        save_csv(rd_rows, f"reddit_trending_{timestamp}.csv", ["title", "subreddit", "score", "url"])

        # ---- unified composite ranking ----
        print("\n=== TOP 20 COMBINED (all sources, normalized 0-100) ===")
        composite = build_composite_general(g_rows, yt_rows, rd_rows)
        combo_rows = []
        for entry in composite[:20]:
            print(f"  [{entry['score']:>5}] ({entry['source']}) {entry['item'][:80]} {entry['detail']}")
            combo_rows.append((entry["score"], entry["source"], entry["item"], entry["detail"]))
        save_csv(combo_rows, f"combined_ranked_{timestamp}.csv", ["score", "source", "item", "detail"])

        # ---- momentum (Google Trends realtime only, see docstring) ----
        new_items, rising_items = compute_momentum_general(g_rows, history, args.region)

        print("\n=== NEW & RISING (Google Trends keywords) ===")
        if new_items:
            print("New this run (no prior data yet):")
            for kw in new_items[:15]:
                print(f"  {kw}")
        if rising_items:
            print("Rising (moved up in rank vs last run):")
            for kw, improvement in rising_items[:15]:
                print(f"  {kw} (+{improvement} spots)")
        if not new_items and not rising_items:
            print("  No prior run to compare yet — run this again later to see movement."
                  if not history.get(f"google_realtime_{args.region}") else
                  "  No significant movement since last run.")

        save_history(history)

    print("\nDone.")


if __name__ == "__main__":
    main()
