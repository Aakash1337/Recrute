"""M3 tailoring: mega-resume ingest, focused resume, cover letter, form answers, truthfulness
guard, and the CP2 packet orchestrator. LLM access goes through `LLMRouter.complete` only."""

from recrute.tailor.answer_questions import AnswerSet, answer_questions
from recrute.tailor.answers import AnswerBank, add_answer, load_answer_bank, match_question
from recrute.tailor.common import JobContext
from recrute.tailor.cover_letter import CoverLetter, needs_cover_letter, write_cover_letter
from recrute.tailor.ingest import (
    BlockingFlagsError,
    IngestResult,
    ProposalChanged,
    accept_proposed,
    ingest_resume,
    load_profile,
    proposal_digest,
    read_proposal_flags,
    save_profile,
)
from recrute.tailor.packet import (
    build_packet,
    latest_packet_version,
    list_packet_versions,
    load_packet,
    packet_dir,
    packet_file,
)
from recrute.tailor.render import RenderResult, render_cover_letter, render_resume
from recrute.tailor.select import select_resume, validate_selection
from recrute.tailor.verify import Claim, collect_claims, verify

__all__ = [
    "AnswerBank",
    "AnswerSet",
    "BlockingFlagsError",
    "Claim",
    "CoverLetter",
    "IngestResult",
    "JobContext",
    "RenderResult",
    "accept_proposed",
    "ProposalChanged",
    "proposal_digest",
    "add_answer",
    "answer_questions",
    "build_packet",
    "collect_claims",
    "ingest_resume",
    "latest_packet_version",
    "list_packet_versions",
    "load_answer_bank",
    "load_packet",
    "load_profile",
    "match_question",
    "needs_cover_letter",
    "packet_dir",
    "packet_file",
    "read_proposal_flags",
    "render_cover_letter",
    "render_resume",
    "save_profile",
    "select_resume",
    "validate_selection",
    "verify",
    "write_cover_letter",
]
