import pytest

from ai_voice_interpreter.streaming.normalizer import DeltaNormalizer
from ai_voice_interpreter.streaming.segmenter import TTSTextSegmenter


def test_true_delta_normalization_preserves_unicode_and_abbreviation() -> None:
    normalizer = DeltaNormalizer(cumulative=False)
    pieces = ["Dr.", " Smith", " said", ", “Hello!”"]
    deltas = [normalizer.push(piece).delta for piece in pieces]
    assert "".join(deltas) == "Dr. Smith said, “Hello!”"
    assert normalizer.text == "Dr. Smith said, “Hello!”"


def test_cumulative_normalization_removes_repeated_prefixes_and_chunks() -> None:
    normalizer = DeltaNormalizer(cumulative=True)
    assert normalizer.push("Hello").delta == "Hello"
    assert normalizer.push("Hello").delta == ""
    assert normalizer.push("Hello world").delta == " world"
    assert normalizer.text == "Hello world"


def test_cumulative_revision_is_flagged_without_replaying_text() -> None:
    normalizer = DeltaNormalizer(cumulative=True)
    normalizer.push("The project status")
    revised = normalizer.push("The project progress")
    assert revised.revised
    assert revised.delta == ""
    assert revised.text == "The project progress"


@pytest.mark.parametrize(
    "text",
    [
        "Short sentence.",
        "Visit https://example.com/docs and review version 2.1 before delivery.",
        "Dr. Smith confirmed item 3.14; then approved the plan!",
        "Wait... really?! Yes, absolutely.",
    ],
)
def test_segmenter_final_flush_never_loses_or_duplicates(text: str) -> None:
    segmenter = TTSTextSegmenter(min_chars=10, target_chars=24, max_chars=40)
    output: list[str] = []
    for token in [text[index : index + 3] for index in range(0, len(text), 3)]:
        output.extend(segmenter.feed(token, now=0.0))
    output.extend(segmenter.flush())
    assert "".join(output) == text
    assert all(output)


def test_segmenter_flushes_long_unpunctuated_text_on_word_boundary() -> None:
    text = "This sentence contains many complete words without any punctuation at all"
    segmenter = TTSTextSegmenter(min_chars=10, target_chars=20, max_chars=30)
    output = segmenter.feed(text, now=0.0) + segmenter.flush()
    assert "".join(output) == text
    assert output[0].endswith(" ")


def test_segmenter_max_wait_flushes_only_complete_word_boundary() -> None:
    segmenter = TTSTextSegmenter(min_chars=10, target_chars=16, max_chars=40, max_wait_ms=300)
    assert segmenter.feed("Hello project progress continues", now=0.0) == []
    output = segmenter.poll(now=0.31)
    assert output and output[0].endswith(" ")
    assert "".join(output + segmenter.flush()) == "Hello project progress continues"
