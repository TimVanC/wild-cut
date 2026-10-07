"""Documentary mode on the synthetic 10-minute film: letterbox crop, filters, classification
accuracy, filtered shots never used, no HERO/AURA shot in two edits, length and drop checks."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlmodel import Session, select

from wildcut.claude import ClaudeClient, set_client_for_tests
from wildcut.db import Clip, Project, get_engine
from wildcut.documentary.bank import DocShotBatch, DocShotTag, analyze_documentary, estimate_edits
from wildcut.documentary.filters import text_score
from wildcut.documentary.letterbox import detect_letterbox
from wildcut.media import extract_frames
from wildcut.planner import edl as edlmod
from wildcut.services.planning import current_edl

ROOT = Path(__file__).resolve().parents[2]
FPS = 30


@pytest.fixture(scope="module")
def doc_assets():
    gt_path = ROOT / "tests_out" / "ground_truth_documentary.json"
    import sys

    sys.path.insert(0, str(ROOT / "tools"))
    import make_documentary_asset as gen

    if gt_path.exists():
        gt = json.loads(gt_path.read_text())
        if gt.get("version") == gen.DOC_VERSION and (ROOT / "tests_out" / gt["path"]).exists():
            gt["dir"] = str(ROOT / "tests_out")
            return gt
    gt = gen.make_documentary(ROOT / "tests_out")
    gt["dir"] = str(ROOT / "tests_out")
    return gt


class FakeDocClaude(ClaudeClient):
    """Classifies by the hidden ground-truth label of the keyframe time (what Claude would see)."""

    KIND_TO = {"action": ("hero", False, False), "repeat": ("hero", False, False), "closeup": ("aura", False, False),
               "landscape": ("broll", False, False), "other": ("other", False, False), "text": ("hero", False, True),
               "presenter": ("other", True, False)}

    def __init__(self, segments):
        super().__init__(api_key="fake", model="x")
        self.segments = segments
        self.pending: list[float] = []
        self.calls = 0

    def kind_at(self, t: float) -> str:
        for s in self.segments:
            if s["start"] <= t < s["end"]:
                return s["kind"]
        return "landscape"

    def structured(self, content, schema, budget=None, system=None, max_tokens=4000, note=""):
        self.calls += 1
        if budget:
            budget.add(0.01, note)
        # recover each shot's keyframe time from the text blocks our pipeline emits ("Shot k (d s)") is not
        # enough, so the test monkeypatches extract_frames to record the times (see test below)
        tags = []
        n = sum(1 for b in content if b.get("type") == "text" and b["text"].startswith("Shot "))
        for k in range(n):
            t = self.pending[k] if k < len(self.pending) else 0.0
            cat, people, text = self.KIND_TO[self.kind_at(t)]
            tags.append(DocShotTag(index=k, category=cat, species="disc" if cat in ("hero", "aura") else ("square" if cat == "other" else "none"),
                                   has_people=people, has_text=text, caption=f"{cat} shot", intensity=8 if cat == "hero" else 3, framing=7))
        self.pending = []
        return DocShotBatch(tags=tags)


@pytest.fixture(scope="module")
def bank(doc_assets, tmp_path_factory):
    import wildcut.documentary.bank as bankmod
    from wildcut.config import get_settings

    settings = get_settings()
    data = ROOT / "tests_out" / "docdata"   # persistent cache: delete it after changing the bank pipeline
    data.mkdir(parents=True, exist_ok=True)
    old = settings.data_dir
    settings.data_dir = data
    fake = FakeDocClaude(doc_assets["segments"])
    real_extract = bankmod.extract_frames

    def recording_extract(path, times, width=None):
        if len(times) == 1 and width == 448:
            fake.pending.append(times[0])
        return real_extract(path, times, width=width)

    bankmod.extract_frames = recording_extract
    try:
        b = analyze_documentary(Path(doc_assets["dir"]) / doc_assets["path"], doc_assets["duration"], "auto", client=fake, force=False)
    finally:
        bankmod.extract_frames = real_extract
        settings.data_dir = old
    b["_calls"] = fake.calls
    b["_data_dir"] = str(data)
    return b


def test_letterbox_detected(doc_assets):
    bars = detect_letterbox(Path(doc_assets["dir"]) / doc_assets["path"], doc_assets["duration"])
    assert abs(bars["top"] - doc_assets["letterbox"]["top"]) <= 4 and abs(bars["bottom"] - doc_assets["letterbox"]["bottom"]) <= 4
    assert bars["left"] == 0 and bars["right"] == 0


def test_text_detector_on_synthetic_frames(doc_assets):
    path = Path(doc_assets["dir"]) / doc_assets["path"]
    text_seg = next(s for s in doc_assets["segments"] if s["kind"] == "text")
    clean_seg = next(s for s in doc_assets["segments"] if s["kind"] == "action")
    f_text, f_clean = extract_frames(path, [(text_seg["start"] + text_seg["end"]) / 2, (clean_seg["start"] + clean_seg["end"]) / 2], width=960)
    assert text_score(f_text) >= 0.5
    assert text_score(f_clean) < 0.5


def _kind_of(gt, t):
    for s in gt["segments"]:
        if s["start"] <= t < s["end"]:
            return s
    return None


def test_bank_filters_and_classification(bank, doc_assets):
    shots = bank["shots"]
    assert bank["crop"]["rect"] is not None, "letterbox crop applied to the proxy"
    assert bank["timings"]["proxy"] >= 0
    # every ground-truth filtered segment maps to rejected shots; every clean one survives
    wrong = []
    total = 0
    for seg in doc_assets["segments"]:
        mid = (seg["start"] + seg["end"]) / 2
        hit = next((sh for sh in shots if sh["start"] - 0.3 <= mid <= sh["end"] + 0.3), None)
        if hit is None:
            continue
        total += 1
        expect_reject = seg["kind"] in ("text", "presenter", "black", "short", "repeat")
        if expect_reject != bool(hit["rejected"]):
            wrong.append((seg["kind"], seg["start"], hit["rejected"], hit["category"]))
            continue
        if not expect_reject:
            want = {"action": "hero", "closeup": "aura", "landscape": "broll", "other": "other"}[seg["kind"]]
            if hit["category"] != want:
                wrong.append((seg["kind"], seg["start"], hit["category"]))
    acc = 1 - len(wrong) / max(1, total)
    assert acc >= 0.9, (acc, wrong[:10])
    assert bank["rejected"]["duplicate"] >= 1 and bank["rejected"]["text"] >= 3 and bank["rejected"]["people"] >= 2
    assert bank["categories"]["hero"] >= 25 and bank["categories"]["aura"] >= 8 and bank["categories"]["broll"] >= 8
    est = estimate_edits(bank)
    assert est["supported"] >= 2


@pytest.fixture()
def doc_project(db, doc_assets, bank, monkeypatch):
    from wildcut.config import get_settings
    from wildcut.documentary.service import register

    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", Path(bank["_data_dir"]))
    set_client_for_tests(ClaudeClient(api_key="", model="x"))
    with Session(get_engine()) as s:
        p = Project(name="Disc documentary", style="phonk", aspect="9:16", target_length="65", mode="music",
                    song_path=str(Path(doc_assets["dir"]) / doc_assets["track"]["path"]),
                    options={"documentary": {"path": str(Path(doc_assets["dir"]) / doc_assets["path"]), "animal": "auto", "edits": "as_many", "target": 65}})
        s.add(p)
        s.commit()
        s.refresh(p)
        register(s, p, p.options["documentary"]["path"], "auto")
        pid = p.id
    yield pid
    set_client_for_tests(None)


def test_generate_two_distinct_edits(doc_project, doc_assets, bank):
    from wildcut.documentary.service import analyze, generate, get_documentary

    with Session(get_engine()) as s:
        p = s.get(Project, doc_project)
        analyze(s, p)   # cached bank: fast
        doc = get_documentary(s, p.id)
        assert doc.status == "ready" and doc.estimate["supported"] >= 2
        ids = generate(s, p, 2)
        assert len(ids) == 2
        used_hero_aura: list[set[int]] = []
        openers = []
        heroes = []
        for pid in ids:
            child = s.get(Project, pid)
            row = current_edl(s, child)
            edl = row.json
            clips = {c.id: c for c in s.exec(select(Clip).where(Clip.project_id == pid)).all()}
            # every clip in the edit maps to a kept, non-filtered shot
            idxs = set()
            for c in edl["clips"]:
                clip = clips[c["clip_id"]]
                idx = clip.tags["doc_shot_index"]
                shot = bank["shots"][idx]
                assert not shot["rejected"], f"filtered shot {idx} ({shot['rejected']}) leaked into an edit"
                seg = _kind_of(doc_assets, (clip.window_in + clip.window_out) / 2)
                assert seg["kind"] not in ("text", "presenter", "black", "short", "repeat"), seg
                # the clip's source range stays inside its shot window
                assert clip.window_in - 0.05 <= c["in"] <= c["out"] <= clip.window_out + 0.05
                if clip.tags["category"] in ("hero", "aura"):
                    idxs.add(idx)
            used_hero_aura.append(idxs)
            # structure: opens with broll (intro), hero on the drop with the title, broll never mid-edit
            roles = [c["role"] for c in edl["clips"]]
            cats = [clips[c["clip_id"]].tags["category"] for c in edl["clips"]]
            assert roles[0] == "intro" and cats[0] == "broll", (roles[:3], cats[:3])
            hero = next(c for c in edl["clips"] if c["role"] == "hero")
            assert clips[hero["clip_id"]].tags["category"] == "hero"
            assert abs(edlmod.peak_timeline(hero) - edl["markers"]["drop"]) <= 1 / FPS
            mid_cats = [cat for cat, role in zip(cats, roles) if role in ("build", "post", "hero")]
            assert "broll" not in mid_cats, "BROLL must not pad the middle"
            assert roles[-1] == "outro" and cats[-1] in ("aura", "broll")
            assert 58 <= edl["duration"] <= 72, edl["duration"]
            assert edl["text"][0]["text"] == "THE DISC"
            assert abs(edl["text"][0]["t"] - edl["markers"]["drop"]) <= 1 / FPS
            openers.append(clips[edl["clips"][0]["clip_id"]].tags["doc_shot_index"])
            heroes.append(clips[hero["clip_id"]].tags["doc_shot_index"])
        assert not (used_hero_aura[0] & used_hero_aura[1]), "a HERO/AURA shot appeared in both edits"
        assert openers[0] != openers[1] and heroes[0] != heroes[1]
        # the estimate drops after generation because shots are now used
        doc = get_documentary(s, p.id)
        assert doc.estimate["supported"] <= 2
