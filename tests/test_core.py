import os
import tempfile
import time
import unittest

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["DEMO_MODE"] = "0"
os.environ.setdefault("REDDIT_CLIENT_ID", "id")
os.environ.setdefault("REDDIT_CLIENT_SECRET", "secret")

import httpx  # noqa: E402

from app import budget, collector, db, generator, reddit_client, scoring, spam  # noqa: E402

db.init_db()


def listing(posts, after=None):
    return {"kind": "Listing", "data": {"after": after, "children": [{"kind": "t3", "data": p} for p in posts]}}


def raw_post(pid, sub="test", age_min=30, score=10, comments=2):
    return {"id": pid, "subreddit": sub, "title": f"post {pid}", "selftext": "", "author": "a", "url": "https://x.com/a",
            "permalink": f"/r/{sub}/comments/{pid}/", "is_self": False, "created_utc": time.time() - age_min * 60,
            "score": score, "num_comments": comments, "upvote_ratio": 0.9, "over_18": False}


class GeneratorTest(unittest.TestCase):
    def test_weighted_len(self):
        self.assertEqual(generator.weighted_len("abc"), 3)
        self.assertEqual(generator.weighted_len("中文"), 4)
        self.assertEqual(generator.weighted_len("a https://example.com/very/long/url"), 2 + 23)

    def test_fit_truncates_and_keeps_link(self):
        out = generator.fit("长" * 300, "https://reddit.com/x")
        self.assertLessEqual(generator.weighted_len(out), 280)
        self.assertTrue(out.endswith("https://reddit.com/x"))
        long_out = generator.fit("长" * 250, "", max_weight=600)
        self.assertEqual(generator.weighted_len(long_out), 500)


class SchedulingTest(unittest.TestCase):
    def test_next_check_policy(self):
        s = db.get_settings()
        self.assertEqual(collector.next_check_at(0, 0, 10 * 60, s), 25 * 60)
        track = s["track_hours"] * 3600
        self.assertEqual(collector.next_check_at(0, 0, 3 * 3600, s), track)  # early drop -> final snapshot
        self.assertEqual(collector.next_check_at(0, 70, 3 * 3600, s), 3 * 3600 + 15 * 60)  # hot posts stay frequent
        self.assertIsNone(collector.next_check_at(0, 0, track, s))


class ScoringTest(unittest.TestCase):
    def test_score_driven_by_comments_only(self):
        t = time.time() - 3600
        hot, _ = scoring.compute({"subreddit": "x", "created_utc": t, "score": 5, "num_comments": 300, "upvote_ratio": 0.9}, [])
        cold, _ = scoring.compute({"subreddit": "x", "created_utc": t, "score": 5000, "num_comments": 1, "upvote_ratio": 0.9}, [])
        self.assertGreater(hot, 75)
        self.assertLess(cold, 30)

    def test_in_batch_and_url_deduplication(self):
        now = int(time.time())
        with db.tx() as c:
            c.execute("DELETE FROM posts WHERE id IN ('dup1', 'dup2', 'url1', 'url2')")
            # Two crossposts with identical title in the same scoring batch
            c.execute(
                """INSERT INTO posts(id, subreddit, title, url, post_type, created_utc, first_seen, tracking, score, num_comments, upvote_ratio, status)
                   VALUES ('dup1', 'tech', 'OpenAI releases GPT-6 weights today', 'https://news.ycombinator.com/1', 'link', ?, ?, 1, 500, 300, 0.95, 'tracking'),
                          ('dup2', 'ai', 'OpenAI releases GPT-6 weights today!', 'https://news.ycombinator.com/2', 'link', ?, ?, 1, 200, 150, 0.92, 'tracking')""",
                (now - 1800, now, now - 1800, now),
            )
        promoted = scoring.score_posts(["dup1", "dup2"])
        self.assertEqual(promoted, ["dup1"])
        # Same external article URL with completely different title
        with db.tx() as c:
            c.execute(
                """INSERT INTO posts(id, subreddit, title, url, post_type, created_utc, first_seen, tracking, score, num_comments, upvote_ratio, status)
                   VALUES ('url2', 'world', 'A completely different headline for the same article', 'https://www.news.ycombinator.com/1?utm_source=reddit', 'link', ?, ?, 1, 400, 260, 0.94, 'tracking')""",
                (now - 1800, now),
            )
        promoted2 = scoring.score_posts(["url2"])
        self.assertEqual(promoted2, [])


class SpamTest(unittest.TestCase):
    s = db.get_settings()

    def post(self, **kw):
        return {"title": "A perfectly normal news headline", "selftext": "", "url": "https://news.example.com/a", "domain": "news.example.com",
                "flair": None, "stickied": False, "distinguished": None, "promoted": False, **kw}

    def test_clean_post_passes(self):
        self.assertIsNone(spam.check(self.post(), self.s))
        # Ensure t.me does not false-positive on normal URLs/domains containing 't.me' substring
        self.assertIsNone(spam.check(self.post(url="https://it.media/tech/about.me", domain="it.media", selftext="Check out get.meta and best.method"), self.s))

    def test_catches_junk(self):
        self.assertIn("promo code", spam.check(self.post(title="Use promo code X for 20% off"), self.s))
        self.assertIn("bit.ly", spam.check(self.post(url="https://bit.ly/abc"), self.s))
        self.assertIn("t.me", spam.check(self.post(selftext="Join our group at https://t.me/signals_vip now"), self.s))
        self.assertIn("t.me", spam.check(self.post(selftext="Join t.me/signals_vip for more"), self.s))
        self.assertEqual(spam.check(self.post(stickied=True), self.s), "置顶帖")
        self.assertIn("flair", spam.check(self.post(flair="Self-Promotion"), self.s))
        self.assertEqual(spam.check(self.post(title="FREE GIFT CARDS FOR EVERYONE CLICK NOW"), self.s), "标题全大写")
        self.assertIn("刷屏", spam.check(self.post(), self.s, author_posts=10))
        self.assertIsNotNone(spam.check_engagement({"created_utc": time.time() - 3600, "upvote_ratio": 0.2}, self.s))

    def test_extract_images(self):
        d = {"is_gallery": True, "gallery_data": {"items": [{"media_id": "a"}]}, "media_metadata": {"a": {"s": {"u": "https://i.redd.it/a.jpg"}}},
             "preview": {"images": [{"source": {"url": "https://preview.redd.it/b.jpg"}}]}}
        self.assertEqual(reddit_client.extract_images(d), ["https://i.redd.it/a.jpg", "https://preview.redd.it/b.jpg"])


class RedditClientTest(unittest.TestCase):
    def test_collect_and_refresh_counts_calls(self):
        with db.tx() as c:
            c.execute("INSERT OR IGNORE INTO subreddits(name, added_at) VALUES('test', 0), ('other', 0)")
        seen = []

        def handler(req: httpx.Request):
            seen.append(req.url.path)
            if req.url.path == "/api/v1/access_token":
                return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
            if req.url.path.endswith("/new"):
                assert req.url.path == "/r/other+test/new"
                return httpx.Response(200, json=listing([raw_post("a1"), raw_post("a2", score=900, comments=400)]))
            if req.url.path == "/api/info":
                ids = req.url.params["id"].split(",")
                return httpx.Response(200, json=listing([raw_post(i[3:], score=50) for i in ids]))
            return httpx.Response(404)

        client = reddit_client.RedditClient()
        client._http = httpx.Client(transport=httpx.MockTransport(handler))
        before = budget.status()["used_total"]
        res = collector.collect_new(client)
        self.assertEqual(res["new_posts"], 2)
        self.assertEqual(res["calls"], 1)
        with db.tx() as c:
            c.execute("UPDATE posts SET next_check=0")
        res = collector.refresh_due(client)
        self.assertEqual((res["calls"], res["refreshed"]), (1, 2))
        self.assertEqual(budget.status()["used_total"] - before, 2)  # token request is not counted
        prune_res = collector.prune_old_data()
        self.assertIn("deleted_snapshots", prune_res)

    def test_redditdate_client_normalize_and_collect(self):
        sample = {
            "id": "1smtrc4",
            "title": "China built 100 GW of solar in Q1",
            "content": "",
            "author": "solar_watcher",
            "subreddit": "technology",
            "score": 420,
            "upvote_ratio": 0.96,
            "comment_count": 88,
            "published_at": "2026-04-16T02:27:47Z",
            "url": "https://electrek.co/2026/04/15/china-solar/",
            "reddit_url": "https://www.reddit.com/r/technology/comments/1smtrc4/china_built/",
            "flair": "Energy",
            "is_nsfw": False,
            "is_stickied": False,
            "media": [{"type": "image", "url": "https://i.redd.it/solar.jpg", "thumbnail_url": "https://b.thumbs.redditmedia.com/t.jpg"}],
        }
        norm = reddit_client.normalize_redditdate_post(sample)
        self.assertEqual(norm["id"], "1smtrc4")
        self.assertEqual(norm["num_comments"], 88)
        self.assertEqual(norm["domain"], "electrek.co")
        self.assertEqual(norm["post_type"], "link")
        self.assertIn("https://i.redd.it/solar.jpg", norm["images"])


if __name__ == "__main__":
    unittest.main()

