"""Roundups may pool length, never uncertainty, source attribution or quotas."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pendulum
import pytest
from bs4 import BeautifulSoup
from test_editorial import reply
from test_editorial import writer as model_writer
from test_pipeline import entry_for, setup_pipeline
from test_routing import assessment_for
from test_wordpress import client_fixture

from rss_to_wp import cli, roundups
from rss_to_wp.content_policy import ContentRejectedError
from rss_to_wp.editorial import (
    POLICY_VERSION,
    BriefTooShortError,
    DraftRequiredError,
    RoundupPlan,
    RoundupReview,
    require_approved_roundup,
    roundup_body,
)
from rss_to_wp.rewriter.openai_client import OpenAIRewriter
from rss_to_wp.storage import DedupeStore


def sources_for(article, feed_config, count=3):
    sources = []
    for i in range(1, count + 1):
        entry = entry_for(article) | {"link": f"https://example.test/brief/{i}"}
        sources.append(
            {
                "policy": POLICY_VERSION,
                "entry_key": f"link:{entry['link']}",
                "fingerprint": roundups.entry_fingerprint(entry, feed_config.source_name),
                "feed_url": feed_config.url,
                "source_url": entry["link"],
                "source_name": feed_config.source_name,
                "source_published_at": entry["published"],
                "title": entry["title"],
                "content": entry["summary"],
                "image_urls": [],
                "assessment": assessment_for(article, route="roundup", image_count=0).model_dump(),
            }
        )
    return sources


def reviewed_roundup(article, review, sources):
    paragraphs = [p.get_text() for p in BeautifulSoup(article["body"], "html.parser").find_all("p")]
    sections = [
        {
            "source_id": i + 1,
            "heading": f"Library service notice {i + 1}",
            "paragraphs": [paragraphs[i % 3]],
        }
        for i in range(len(sources))
    ]
    sources = [{**s, "source_id": i + 1} for i, s in enumerate(sources)]
    review = {
        **review,
        "coherent": True,
        "briefs": [
            {
                "source_id": i + 1,
                "faithful": True,
                "current": True,
                "valuable": True,
                "properly_attributed": True,
                "not_duplicate": True,
            }
            for i in range(len(sources))
        ],
    }
    article = {**article, "body": roundup_body(sections, sources, credits=False), "review": review}
    return article, sections, sources


@pytest.mark.parametrize("uncertain", [False, True])
def test_short_useful_brief_queues_but_uncertainty_goes_to_draft(
    uncertain, monkeypatch, settings, feed_config, article, review, context, image_bytes
):
    wp, writer = setup_pipeline(monkeypatch, article, review, context, image_bytes)
    reading = assessment_for(article, route="roundup")
    if uncertain:
        reading.uncertainties = ["Opening date is ambiguous"]
    writer.assess_source.side_effect = None
    writer.assess_source.return_value = reading
    result = cli.process_entry(entry_for(article), feed_config, settings, writer, wp, False, Mock())
    if uncertain:
        assert result["status"] == "draft" and not result.get("queued_roundup")
    else:
        assert result["queued_roundup"]
        wp.create_editorial_draft.assert_not_called()
    wp.create_post.assert_not_called()
    writer.rewrite.assert_not_called()


@pytest.mark.parametrize("safe_to_wait", [False, True])
def test_evidence_floor_only_pools_briefs_explicitly_cleared_to_wait(
    safe_to_wait, article, context
):
    reading = assessment_for(article, route="continue", image_count=0)
    reading.requires_immediate_attention = not safe_to_wait
    writer = OpenAIRewriter("unused")
    writer._request = Mock(return_value=reading)
    result = writer.assess_source(
        "Notice", "A short but useful notice with complete facts.", context, []
    )
    assert result.route == ("roundup" if safe_to_wait else "continue")


@pytest.mark.parametrize("failure", ["length", "urgent", "factual"])
def test_only_a_short_complete_nonurgent_length_failure_is_requeued(
    failure, monkeypatch, settings, feed_config, article, review, context, image_bytes
):
    wp, writer = setup_pipeline(monkeypatch, article, review, context, image_bytes)
    reading = assessment_for(article, route="continue")
    reading.requires_immediate_attention = failure == "urgent"
    writer.assess_source.side_effect = None
    writer.assess_source.return_value = reading
    writer.rewrite.side_effect = (
        DraftRequiredError("Factual uncertainty")
        if failure == "factual"
        else BriefTooShortError("Article is too short")
    )
    result = cli.process_entry(
        entry_for(article) | {"summary": "Supported facts. " * 45},
        feed_config,
        settings,
        writer,
        wp,
        False,
        Mock(),
    )
    if failure == "length":
        assert result["queued_roundup"] and result["candidate"]["assessment"]["route"] == "roundup"
        wp.create_editorial_draft.assert_not_called()
    else:
        assert result["status"] == "draft" and not result.get("queued_roundup")
    wp.create_post.assert_not_called()


def test_uncleared_roundup_is_held_instead_of_queued(article, context):
    reading = assessment_for(article, route="roundup", image_count=0)
    reading.requires_immediate_attention = True
    writer = OpenAIRewriter("unused")
    writer._request = Mock(return_value=reading)
    result = writer.assess_source(
        "Urgent notice", "An active emergency needs attention now.", context, []
    )
    assert result.route == "draft" and result.uncertainties


@pytest.mark.parametrize("dry_run", [False, True])
def test_queue_survives_runs_without_consuming_a_publication_or_reassessing(
    dry_run, tmp_path, monkeypatch, settings, feed_config, article, review, context, image_bytes
):
    wp, writer = setup_pipeline(monkeypatch, article, review, context, image_bytes)
    writer.assess_source.side_effect = None
    writer.assess_source.return_value = assessment_for(article, route="roundup")
    entry = entry_for(article)
    monkeypatch.setattr(
        cli,
        "parse_feed",
        Mock(
            return_value=SimpleNamespace(
                entries=[entry],
                feed={"title": "Corinth Library on Facebook", "link": feed_config.source_url},
            )
        ),
    )
    pool, budget = {}, {"posts": 0, "candidates": 0, "drafts": 0}
    store = DedupeStore(tmp_path / "queue.db")
    for _ in range(2):
        assert cli.process_feed(
            feed_config,
            settings,
            store,
            writer,
            wp,
            dry_run,
            48,
            Mock(),
            budget=budget,
            roundup_pool=pool,
        ) == (0, 1, 0)
    assert len(pool) == 1 and budget == {"posts": 0, "candidates": 1, "drafts": 0}
    writer.assess_source.assert_called_once()
    assert store.get_processed_count() == 0
    assert len(store.load_roundup_candidates()) == (0 if dry_run else 1)


def test_pool_expiration_policy_and_uncertainty_are_not_reused(tmp_path, article, feed_config):
    store = DedupeStore(tmp_path / "queue.db")
    sources = sources_for(article, feed_config, 4)
    sources[1]["policy"] = "old-policy"
    sources[2]["assessment"]["uncertainties"] = ["uncertain date"]
    for source in sources:
        store.queue_roundup(source)
    # Save expired record last: it must still be excluded in read-only previews.
    sources[3]["source_published_at"] = pendulum.now("UTC").subtract(hours=49).isoformat()
    store.queue_roundup(sources[3])
    assert len(roundups.load_pool(store, [feed_config], 48, dry_run=True)) == 1
    assert len(store.load_roundup_candidates()) == 4
    assert len(roundups.load_pool(store, [feed_config], 48, dry_run=False)) == 1
    assert len(store.load_roundup_candidates()) == 1
    store.clear_all()
    assert store.load_roundup_candidates() == []


@pytest.mark.parametrize("ids", [[1, 2], [1, 1, 2], [1, 2, 9]])
def test_planner_cannot_invent_repeat_or_use_too_few_sources(ids):
    writer = OpenAIRewriter("unused")
    writer._request = Mock(
        return_value=RoundupPlan(source_ids=ids, headline="Library service roundup", reason="Topic")
    )
    with pytest.raises(RuntimeError, match="Invalid roundup"):
        writer.plan_roundup([{"source_id": i} for i in (1, 2, 3)], pendulum.now("UTC").isoformat())


@pytest.mark.parametrize(
    "fault",
    [
        "coherent",
        "faithful",
        "current",
        "valuable",
        "properly_attributed",
        "not_duplicate",
        "missing",
    ],
)
def test_one_bad_brief_blocks_the_entire_roundup(fault, article, review, feed_config):
    combined, _, sources = reviewed_roundup(article, review, sources_for(article, feed_config))
    data = combined["review"]
    if fault == "coherent":
        data["coherent"] = False
    elif fault == "missing":
        data["briefs"][0]["source_id"] = 9
    else:
        data["briefs"][0][fault] = False
    with pytest.raises(DraftRequiredError):
        require_approved_roundup(
            RoundupReview.model_validate(data), [s["source_id"] for s in sources], 90
        )


@pytest.mark.parametrize("stock", [False, True])
def test_writer_and_independent_editor_get_all_original_evidence(
    stock, article, review, feed_config, context, image_bytes
):
    combined, sections, sources = reviewed_roundup(
        article, review, sources_for(article, feed_config)
    )
    metadata = {k: combined[k] for k in ("excerpt", "seo_title", "meta_description")}
    if stock:
        metadata["meta_description"] = "Too long " * 30
    proposal = {k: v for k, v in combined.items() if k not in {"body", "review", *metadata}}
    proposal["headline"] = proposal["headline"].replace(" ", "\u00a0", 1) + "\n"
    writer = model_writer(
        reply(proposal | {"sections": sections}),
        reply({k + "_options": ["Short", v] for k, v in metadata.items()}),
        reply(combined["review"]),
    )
    if stock:
        context = {
            **context,
            "image_provenance": {
                "kind": "pexels_stock",
                "photo_id": 123,
                "url": "https://images.pexels.com/photos/123/books.jpeg",
                "photo_url": "https://www.pexels.com/photo/books-123/",
                "photographer": "Example Photographer",
                "photographer_url": "https://www.pexels.com/@example",
                "description": "Stack of books",
                "width": 3000,
                "height": 2000,
            },
        }
    result, actual_sections = writer.rewrite_roundup(
        sources,
        RoundupPlan(
            source_ids=[1, 2, 3], headline=article["headline"], reason="Useful local briefs"
        ),
        context=context,
        image_bytes=image_bytes,
        source_images=[{"source_id": i, "image_id": 1, "bytes": image_bytes} for i in (1, 2, 3)],
    )
    assert result["body"] == combined["body"] and actual_sections == sections
    assert result["headline"] == combined["headline"]
    messages = writer.client.chat.completions.create.call_args_list[2].kwargs["messages"][1][
        "content"
    ]
    assert sum(p.get("image_url", {}).get("detail") == "high" for p in messages) == 3
    assert all(f"Original source {i}, image 1" in str(messages) for i in (1, 2, 3))
    if stock:
        assert result["meta_description"] == metadata["excerpt"]
        assert "Example Photographer" in messages[0]["text"]
        assert "Stock illustration" in messages[0]["text"]


def test_roundup_cannot_omit_or_repeat_a_source_section(article, review, feed_config):
    _, sections, sources = reviewed_roundup(article, review, sources_for(article, feed_config))
    with pytest.raises(DraftRequiredError):
        roundup_body(sections[:2], sources)
    with pytest.raises(DraftRequiredError):
        roundup_body([sections[0], sections[0], sections[2]], sources)
    sources[1]["source_url"] = sources[0]["source_url"] + "/?utm_source=rss"
    with pytest.raises(DraftRequiredError):
        roundup_body(sections, sources)


def test_overlong_roundup_metadata_is_held_before_review(
    article, review, feed_config, context, image_bytes
):
    combined, sections, sources = reviewed_roundup(
        article, review, sources_for(article, feed_config)
    )
    metadata = {k: combined[k] for k in ("excerpt", "seo_title", "meta_description")}
    metadata["meta_description"] = "X" * 166
    metadata["excerpt"] = "Y" * 200
    proposal = {k: v for k, v in combined.items() if k not in {"body", "review", *metadata}}
    writer = model_writer(
        reply(proposal | {"sections": sections}),
        reply({k + "_options": [v] for k, v in metadata.items()}),
    )
    with pytest.raises(DraftRequiredError):
        writer.rewrite_roundup(
            sources,
            RoundupPlan(source_ids=[1, 2, 3], headline=article["headline"], reason="Common topic"),
            context=context,
            image_bytes=image_bytes,
            source_images=[],
        )
    assert writer.client.chat.completions.create.call_count == 2


def service_setup(monkeypatch, tmp_path, article, review, feed_config, context, image_bytes):
    candidates = sources_for(article, feed_config)
    combined, sections, numbered = reviewed_roundup(article, review, candidates)
    store = DedupeStore(tmp_path / "queue.db")
    for candidate in candidates:
        store.queue_roundup(candidate)
    pool = {c["source_url"]: c for c in candidates}
    wp, writer = setup_pipeline(monkeypatch, article, review, context, image_bytes)
    writer.plan_roundup.return_value = RoundupPlan(
        source_ids=[1, 2, 3], headline=article["headline"], reason="Common library topic"
    )
    writer.rewrite_roundup.return_value = combined, sections
    monkeypatch.setattr(roundups, "_refresh_sources", Mock(return_value=(numbered, None)))
    monkeypatch.setattr(
        roundups,
        "_featured_image",
        Mock(return_value=(image_bytes, "https://example.test/image.jpg", None, numbered[0])),
    )
    return pool, store, wp, writer


@pytest.mark.parametrize("dry_run", [False, True])
def test_roundup_uses_one_slot_and_consumes_all_sources_only_on_success(
    dry_run, monkeypatch, tmp_path, article, review, feed_config, context, image_bytes, settings
):
    pool, store, wp, writer = service_setup(
        monkeypatch, tmp_path, article, review, feed_config, context, image_bytes
    )
    budget, decisions = {"posts": 4, "drafts": 0, "candidates": 6}, []
    assert roundups.process_roundups(
        pool, [feed_config], settings, store, writer, wp, dry_run, 48, budget, decisions
    ) == (1, 0, 0)
    assert budget["posts"] == 5 and not pool
    assert store.get_processed_count() == (0 if dry_run else 3)
    assert len(store.load_roundup_candidates()) == (3 if dry_run else 0)
    if dry_run:
        wp.upload_media.assert_not_called()
        wp.create_post.assert_not_called()
    else:
        assert len(wp.create_post.call_args.kwargs["roundup_sources"]) == 3
        assert len({p["wp_post_id"] for p in store.get_recent_entries()}) == 1


@pytest.mark.parametrize(
    "failure", ["quota", "duplicate", "changed", "api", "draft_full", "review", "no_group"]
)
def test_roundup_failures_cannot_publish_or_lose_sources(
    failure, monkeypatch, tmp_path, article, review, feed_config, context, image_bytes, settings
):
    pool, store, wp, writer = service_setup(
        monkeypatch, tmp_path, article, review, feed_config, context, image_bytes
    )
    budget, decisions = {"posts": 0, "drafts": 0}, []
    if failure == "quota":
        budget["posts"] = 5
    elif failure == "duplicate":
        wp.check_duplicate_by_source_url.side_effect = [True, False, False]
    elif failure == "changed":
        roundups._refresh_sources.return_value = None, next(iter(pool))
    elif failure == "api":
        writer.rewrite_roundup.side_effect = RuntimeError("OpenAI unavailable")
    elif failure in {"draft_full", "review"}:
        writer.rewrite_roundup.side_effect = DraftRequiredError("One brief failed factual checks")
        budget["drafts"] = 3 if failure == "draft_full" else 0
    else:
        writer.plan_roundup.return_value = RoundupPlan(
            source_ids=[], headline="", reason="Unrelated subjects"
        )
    result = roundups.process_roundups(
        pool, [feed_config], settings, store, writer, wp, False, 48, budget, decisions
    )
    wp.create_post.assert_not_called()
    wp.upload_media.assert_not_called()
    if failure == "review":
        assert result == (1, 0, 0) and budget["drafts"] == 1
        assert len(wp.create_editorial_draft.call_args.kwargs["roundup_sources"]) == 3
    else:
        assert store.get_processed_count() == 0
        assert len(store.load_roundup_candidates()) >= 2
        wp.create_editorial_draft.assert_not_called()
    if failure == "api":
        assert result == (0, 0, 1)
    if failure == "no_group":
        roundups.process_roundups(
            pool, [feed_config], settings, store, writer, wp, False, 48, budget, decisions
        )
        writer.plan_roundup.assert_called_once()


def test_refresh_rejects_edited_and_expired_source(monkeypatch, article, feed_config):
    candidates = sources_for(article, feed_config)
    entries = [
        {
            "title": s["title"],
            "summary": s["content"],
            "link": s["source_url"],
            "published": s["source_published_at"],
        }
        for s in candidates
    ]
    feed = SimpleNamespace(
        entries=entries,
        feed={"title": "Corinth Library on Facebook", "link": feed_config.source_url},
    )
    monkeypatch.setattr(roundups, "parse_feed", Mock(return_value=feed))
    assert roundups._refresh_sources(candidates, [feed_config], 48)[1] is None
    entries[1]["summary"] += " Important correction."
    assert (
        roundups._refresh_sources(candidates, [feed_config], 48)[1] == candidates[1]["source_url"]
    )
    entries[1]["summary"] = candidates[1]["content"]
    entries[1]["published"] = pendulum.now("UTC").subtract(hours=50).isoformat()
    assert (
        roundups._refresh_sources(candidates, [feed_config], 48)[1] == candidates[1]["source_url"]
    )


@pytest.mark.parametrize("blocked", [False, True])
def test_wordpress_checks_every_source_and_renders_attribution(
    blocked, article, review, feed_config, context
):
    combined, sections, sources = reviewed_roundup(
        article, review, sources_for(article, feed_config)
    )
    wp = client_fixture(article, review, context)
    if blocked:
        wp.find_source_posts.side_effect = lambda url: (
            [{"id": 9, "status": "draft"}] if url == sources[2]["source_url"] else []
        )
    result = wp.create_post(
        article=combined,
        source_url=sources[0]["source_url"],
        source_name="Corinth Library",
        related=[],
        featured_media_id=123,
        roundup_sources=sources,
        roundup_sections=sections,
    )
    if blocked:
        assert result["duplicate"]
        assert not any(c.args[0] == "POST" for c in wp._request.call_args_list)
    else:
        assert result["status"] == "publish"
        assert all(s["source_url"] in result["content"] for s in sources)
        assert result["content"].count("Source: <a") == 3
        assert "roundup-staging-v1" in result["content"]
        assert not wp._is_staging_draft({**result, "status": "draft"}, sources[0]["source_url"])


def test_refresh_checks_access_errors_in_every_field(monkeypatch, article, feed_config):
    sources = sources_for(article, feed_config)
    entries = [
        {
            "title": s["title"],
            "summary": s["content"],
            "link": s["source_url"],
            "published": s["source_published_at"],
        }
        for s in sources
    ]
    entries[1]["description"] = "Access denied"
    monkeypatch.setattr(
        roundups,
        "parse_feed",
        Mock(
            return_value=SimpleNamespace(
                entries=entries,
                feed={"title": "Corinth Library on Facebook", "link": feed_config.source_url},
            )
        ),
    )
    assert roundups._refresh_sources(sources, [feed_config], 48)[1] == sources[1]["source_url"]


def test_refresh_keeps_image_only_brief_evidence(monkeypatch, article, feed_config):
    source = sources_for(article, feed_config, 1)[0]
    entry = {
        "title": "",
        "summary": '<img src="https://example.test/notice.jpg">',
        "link": source["source_url"],
        "published": source["source_published_at"],
    }
    source.update(
        title="",
        content=entry["summary"],
        image_urls=["https://example.test/notice.jpg"],
        fingerprint=roundups.entry_fingerprint(entry, feed_config.source_name),
        assessment=assessment_for(article, route="roundup", image_count=1).model_dump(),
    )
    monkeypatch.setattr(
        roundups,
        "parse_feed",
        Mock(
            return_value=SimpleNamespace(
                entries=[entry],
                feed={"title": "Corinth Library on Facebook", "link": feed_config.source_url},
            )
        ),
    )
    refreshed, changed = roundups._refresh_sources([source], [feed_config], 48)
    assert changed is None and refreshed[0]["image_urls"] == source["image_urls"]


def test_four_distinct_sources_each_get_a_section_and_link(article, review, feed_config):
    _, sections, sources = reviewed_roundup(article, review, sources_for(article, feed_config, 4))
    rendered = roundup_body(sections, sources)
    assert rendered.count("<h2>") == 4 and rendered.count("Source: <a") == 4
    assert all(s["source_url"] in rendered for s in sources)


def test_failed_roundup_draft_contains_all_sources_and_cannot_auto_resume(
    article, review, feed_config, context
):
    sources = sources_for(article, feed_config)
    wp = client_fixture(article, review, context)
    assessment = assessment_for(article, route="draft", image_count=0)
    result = wp.create_editorial_draft(
        assessment=assessment.model_dump(),
        source_url=sources[0]["source_url"],
        source_name=sources[0]["source_name"],
        issues=["One brief needs more verification"],
        source_images=[],
        source_published_at=sources[0]["source_published_at"],
        roundup_sources=sources,
    )
    assert result["status"] == "draft"
    payload = next(
        c.kwargs["json"] for c in wp._request.call_args_list if c.args == ("POST", "posts")
    )
    assert all(s["source_url"] in payload["content"] for s in sources)
    assert "editorial-hold" in payload["content"]
    assert not wp._is_staging_draft(payload, sources[0]["source_url"])


def test_wordpress_cannot_publish_tampered_roundup_or_expired_brief(
    article, review, feed_config, context
):
    combined, sections, sources = reviewed_roundup(
        article, review, sources_for(article, feed_config)
    )
    wp = client_fixture(article, review, context)
    for changed in ("body", "date", "review"):
        data, evidence = deepcopy(combined), deepcopy(sources)
        if changed == "body":
            data["body"] += "<p>Unreviewed extra claim.</p>"
        elif changed == "date":
            evidence[1]["source_published_at"] = pendulum.now("UTC").subtract(hours=50).isoformat()
        else:
            data["review"]["briefs"][1]["faithful"] = False
        with pytest.raises(ContentRejectedError):
            wp.create_post(
                article=data,
                source_url=evidence[0]["source_url"],
                source_name="Library",
                related=[],
                featured_media_id=123,
                roundup_sources=evidence,
                roundup_sections=sections,
            )
    assert wp._request.call_count == 0
