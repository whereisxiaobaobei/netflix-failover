import itertools
import unittest

from netflix_probe import (
    aggregate_title_results,
    classify_page,
    decision,
    group_state,
)


TITLE_ID = "70143836"
TITLE = "Breaking Bad"
URL = "https://www.netflix.com/tw-en/title/70143836"
VIDEO = "https://occ-0-325-395.1.nflxso.net/dnm/example.mp4?r=abc"


def title_page(*, video=VIDEO, text="<h1>Breaking Bad</h1>", extra_head="", title=TITLE, og_url=URL):
    video_meta = f'<meta property="og:video" content="{video}">' if video is not None else ""
    return (
        '<!doctype html><html><head>'
        f'<title>Watch {title} | Netflix</title>'
        f'<meta data-rh="true" property="og:title" content="Watch {title} | Netflix">'
        f'<meta data-rh="true" property="og:url" content="{og_url}" id="meta-url">'
        f'{video_meta}{extra_head}</head><body>{text}</body></html>'
    )


class PageClassificationTests(unittest.TestCase):
    def classify(self, body, *, status=200, url=URL, title_id=TITLE_ID, expected=TITLE):
        return classify_page(body, status, url, title_id, expected)

    def test_observed_positive_page(self):
        self.assertEqual(self.classify(title_page()), "unlocked")

    def test_observed_negative_with_apostrophe_variants_and_split_text(self):
        for apostrophe in ("’", "'", "&#39;"):
            body = title_page(
                video=None,
                text=f"<h2>Oh no!</h2><p>This title currently isn{apostrophe}t "
                "available to watch in your <b>country</b>.</p><h1>Breaking Bad</h1>",
            )
            with self.subTest(apostrophe=apostrophe):
                self.assertEqual(self.classify(body), "restricted")

    def test_region_variant(self):
        body = title_page(video=None, text="Oh no! This title is not available in your current region.")
        self.assertEqual(self.classify(body), "restricted")

    def test_conflicting_evidence_is_unknown(self):
        body = title_page(text="Oh no! This title currently isn’t available to watch in your country.")
        self.assertEqual(self.classify(body), "unknown")

    def test_missing_evidence_is_unknown(self):
        for text in ("Breaking Bad", "Oh no!", "This title is not available in your country.", "Sign In"):
            with self.subTest(text=text):
                self.assertEqual(self.classify(title_page(video=None, text=text)), "unknown")

    def test_script_error_strings_do_not_make_positive_conflict(self):
        for tag in ("script", "style", "noscript", "template"):
            text = f"<{tag}>Oh no! This title is not available in your country.</{tag}><h1>Breaking Bad</h1>"
            with self.subTest(tag=tag):
                self.assertEqual(self.classify(title_page(text=text)), "unlocked")

    def test_script_meta_string_cannot_supply_positive_evidence(self):
        fake = '<script>const example = \'<meta property="og:video" content="' + VIDEO + '">\';</script>'
        self.assertEqual(self.classify(title_page(video=None, text=fake)), "unknown")

    def test_body_svg_title_is_not_the_document_title(self):
        body = title_page(text="<svg><title>Netflix logo</title></svg><h1>Breaking Bad</h1>")
        self.assertEqual(self.classify(body), "unlocked")

    def test_body_meta_cannot_supply_positive_evidence(self):
        body = title_page(video=None, text=f'<meta property="og:video" content="{VIDEO}">')
        self.assertEqual(self.classify(body), "unknown")

    def test_other_meta_keys_do_not_count(self):
        for key in ("og:video:url", "og:video:secure_url", "x-og:video", "og:video:width"):
            body = title_page(video=None, extra_head=f'<meta property="{key}" content="{VIDEO}">')
            with self.subTest(key=key):
                self.assertEqual(self.classify(body), "unknown")

    def test_http_errors_remain_unknown_with_otherwise_valid_html(self):
        for status in (0, 206, 301, 302, 403, 404, 429, 500, 503):
            with self.subTest(status=status):
                self.assertEqual(self.classify(title_page(), status=status), "unknown")

    def test_final_url_must_bind_exact_title_on_netflix(self):
        urls = (
            "https://www.netflix.com/login",
            "https://www.netflix.com/title/701438360",
            "https://www.netflix.com/title/80057281",
            "https://www.netflix.com.evil.test/title/70143836",
            "https://www.netflix.com@evil.test/title/70143836",
            "http://www.netflix.com/title/70143836",
            "https://www.netflix.com:444/title/70143836",
            "not-a-url",
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.classify(title_page(), url=url), "unknown")

    def test_common_region_and_query_url_variants(self):
        for prefix in ("", "tw/", "tw-en/", "us/", "ja-jp/"):
            url = f"https://www.netflix.com/{prefix}title/70143836?trackId=1"
            with self.subTest(url=url):
                self.assertEqual(self.classify(title_page(og_url=url), url=url), "unlocked")

    def test_og_url_must_bind_same_title(self):
        for url in ("", "https://www.netflix.com/title/80057281", "https://example.net/title/70143836"):
            with self.subTest(url=url):
                self.assertEqual(self.classify(title_page(og_url=url)), "unknown")

    def test_title_name_must_match_without_prefix_collisions(self):
        for title in ("Breaking Badly", "Stranger Things", "Breaking Bad Trailer", "Sign In"):
            with self.subTest(title=title):
                self.assertEqual(self.classify(title_page(title=title)), "unknown")

    def test_expected_title_and_html_entities(self):
        body = title_page(title="Test &amp; Title")
        self.assertEqual(self.classify(body, expected="Test & Title"), "unlocked")

    def test_video_must_be_valid_https_netflix_media(self):
        for video in (
            "", "javascript:alert(1)", "http://occ-1.nflxso.net/test.mp4",
            "https://nflxso.net.evil.test/test.mp4", "https://evil.test/test.mp4",
            "https://occ-1.nflxso.net/not-a-video", "https://user@occ-1.nflxso.net/test.mp4",
        ):
            with self.subTest(video=video):
                self.assertEqual(self.classify(title_page(video=video)), "unknown")

    def test_conflicting_duplicate_metadata_is_unknown(self):
        cases = (
            '<meta property="og:title" content="Watch Stranger Things | Netflix">',
            '<meta property="og:url" content="https://www.netflix.com/title/80057281">',
            '<meta property="og:video" content="https://occ-1.nflxso.net/other.mp4">',
            f'<meta property="og:video" property="bogus" content="{VIDEO}">',
        )
        for extra_head in cases:
            with self.subTest(extra_head=extra_head):
                self.assertEqual(self.classify(title_page(extra_head=extra_head)), "unknown")

    def test_identical_duplicate_metadata_is_allowed(self):
        extra = f'<meta property="og:video" content="{VIDEO}">'
        self.assertEqual(self.classify(title_page(extra_head=extra)), "unlocked")

    def test_challenge_body_and_incomplete_html_are_unknown(self):
        for body in (
            title_page(text="Verify you are human"),
            title_page().replace("</html>", ""),
            title_page().split("</head>")[0],
            "", " ", "<html><head><title>Sign In</title></head><body>Sign In</body></html>",
        ):
            with self.subTest(body=body[:80]):
                self.assertEqual(self.classify(body), "unknown")


class StateMachineTests(unittest.TestCase):
    def test_licensed_title_availability_aggregation(self):
        cases = (
            ("unlocked", ("unlocked", "unlocked"), "unlocked"),
            ("unlocked", ("unlocked", "restricted"), "unlocked"),
            ("unlocked", ("unknown", "unlocked"), "unlocked"),
            ("unlocked", ("restricted", "restricted"), "restricted"),
            ("unlocked", ("unknown", "restricted"), "unknown"),
            ("unlocked", ("unknown", "unknown"), "unknown"),
            ("unknown", ("unlocked", "unlocked"), "unknown"),
            ("restricted", ("restricted", "restricted"), "unknown"),
            ("unlocked", (), "unknown"),
            ("unlocked", ("unlocked",), "unknown"),
            ("unlocked", ("bogus", "unlocked"), "unknown"),
        )
        for control, titles, expected in cases:
            with self.subTest(control=control, titles=titles):
                self.assertEqual(aggregate_title_results(control, titles), expected)

    def test_every_mixed_group_is_unknown(self):
        states = ("unlocked", "restricted", "unknown")
        for members in itertools.product(states, repeat=3):
            expected = members[0] if len(set(members)) == 1 else "unknown"
            with self.subTest(members=members):
                self.assertEqual(group_state(members), expected)
        self.assertEqual(group_state([]), "unknown")
        self.assertEqual(group_state(["bogus"]), "unknown")

    def test_switch_requires_restricted_primary_and_unlocked_backup(self):
        for primary, backup in itertools.product(("unlocked", "restricted", "unknown"), repeat=2):
            with self.subTest(primary=primary, backup=backup):
                expected = "switch" if (primary, backup) == ("restricted", "unlocked") else "keep"
                self.assertEqual(decision(primary, backup), expected)


if __name__ == "__main__":
    unittest.main()
