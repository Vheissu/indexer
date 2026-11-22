"""Scoring proposals with the model trained on past reviews (training.py), to drop the likely
rejections before anyone reviews them."""

from dataclasses import replace
from datetime import date

from indexer.survivors import proposals
from indexer.survivors.run import Survivor
from indexer.training import ProposalModel, parse_proposals


def keep_likely(
    survivors: list[Survivor], model: ProposalModel, crawl: str, today: date
) -> tuple[list[Survivor], int]:
    """The survivors scoring at least the model's threshold, each with its score, best first,
    and how many were dropped. Each is scored on its text exactly as a reviewer reads it in the
    proposal file."""
    texts = [p.text for p in parse_proposals(proposals.render_entries(survivors, crawl, today))]
    scored = [
        replace(s, score=score) for s, score in zip(survivors, model.scores(texts), strict=True)
    ]
    kept = sorted(
        (s for s in scored if (s.score or 0.0) >= model.threshold), key=lambda s: -(s.score or 0.0)
    )
    return kept, len(scored) - len(kept)
