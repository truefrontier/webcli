"""Builds the synthetic NotebookLM RPC answers in this folder (the shapes live answers have, none of their content).
Run it after changing a shape: python tests/fixtures/notebooklm/make.py"""

import json
from pathlib import Path

NB = "11111111-2222-4333-8444-555555555555"
S1, S2 = "aaaaaaaa-1111-4111-8111-111111111111", "bbbbbbbb-2222-4222-8222-222222222222"
A_AUDIO, A_REPORT, A_VIDEO = ("cccccccc-0000-4000-8000-00000000000" + c for c in "123")
A_QUIZ, A_MAP, A_SLIDES, A_PENDING = ("dddddddd-0000-4000-8000-00000000000" + c for c in "1234")
MEDIA = "https://lh3.googleusercontent.com/notebooklm/FAKEmedia"


def frame(rpcid, payload):
    return ")]}'\n\n" + json.dumps([["wrb.fr", rpcid, json.dumps(payload), None, None, None, "generic"]])


def source(sid, title, kind, words, status=2):
    return [[sid], title, [None, words, [1773486116, 0], None, kind, None, None, None], [None, status]]


def artifact(aid, title, kind, status, created, **slots):
    row = [aid, title, kind, [[S1]], status] + [None] * 14
    row[15] = [created, 0]
    for index, value in slots.items():
        row[int(index[1:])] = value
    return row


notebooks = [[["Synthetic research notebook", [source(S1, "Doc one", 8, 763)], NB, "📓", None,
               [1, None, None, None, None, [1773486121, 0], None, None, [1773486115, 0]]],
              ["thought\nSecond notebook", [], "99999999-2222-4333-8444-555555555555", None, None, None]]]
notebook = [["Synthetic research notebook", [source(S1, "Doc one", 8, 763), source(S2, "Video", 9, 120, 1)], NB]]
source_text = [[[S1], "Doc one", [None, 763, None, None, 8]], None, None,
               [[[[0, 10, [[[0, 10, ["First para."]]]]]], [[11, 30, [[[11, 30, ["Second ", "para."]]]]]]]]]
guide = [[[None, ["A short synthetic summary."], [["alpha", "beta", "gamma"]]]]]
notes = [[[f"note-1", [f"note-1", "A saved note.", [1, None, [1773486200, 0]], None, "My note"]],
          ["note-2", ["note-2", json.dumps({"name": "Root", "children": []}), None, None, "Map note"]],
          ["note-3", None, 2]]]
artifacts = [[
    artifact(A_AUDIO, "Deep dive", 1, 3, 1773486300,
             s6=[None, ["prompt", 2], MEDIA + "a0", MEDIA + "a1", True,
                 [[MEDIA + "-dash", 4, "application/dash+xml"], [MEDIA + "-m4a", 1, "audio/mp4"]], [600, 0]]),
    artifact(A_REPORT, "Study Guide", 2, 3, 1773486400, s7=["# Study Guide\n\nBody."]),
    artifact(A_VIDEO, "Explainer", 3, 3, 1773486250,
             s8=[None, MEDIA + "-thumb", None, MEDIA + "-poster",
                 [[MEDIA + "-v1", 1, "video/mp4"], [MEDIA + "-v2", 2], [MEDIA + "-v4", 4, "video/mp4"]], [85, 0]]),
    artifact(A_QUIZ, "Quiz", 4, 3, 1773486500, s9=[None, [2, None, None, None, None, None, None, [2, 2]]]),
    artifact(A_MAP, "Mind map", 4, 3, 1773486100, s9=[None, [4]]),
    artifact(A_SLIDES, "Deck", 8, 3, 1773486000, s16=[None, None, None, MEDIA + "-pdf", MEDIA + "-pptx"]),
    artifact(A_PENDING, None, 7, 1, 1773486600),
]]
quiz_html = ('<div data-app-data="' + json.dumps({"quiz": [{"question": "Q1?", "answerOptions": []}]})
             .replace('"', "&quot;") + '"></div>')
quiz = [[A_QUIZ, "Quiz", 4, None, 3, None, None, None, None, [quiz_html, [2]]]]
mind_map = [[A_MAP, "Mind map", 4, None, 3, None, None, None, None,
             [None, [4], None, json.dumps({"name": "Root", "children": [{"name": "Leaf"}]})]]]
created = [artifact("eeeeeeee-0000-4000-8000-000000000001", None, 2, 1, 1773486700)]


def answer_chunk(text, final, cites):
    inner = [[text, None, ["conv-1", "turn-1", 1], None, [None, None, None, cites, 1]], None, None, None, final]
    return ["wrb.fr", None, json.dumps(inner)]


cite = [["chunk-1"], [None, None, 0.9, [[None, 0, 10]], [[[0, 10, [[[0, 10, ["First para."]]]]]]], [[[S1]]]]]
chunks = [answer_chunk("Partial", False, []), answer_chunk("The full answer [1].", True, [cite])]
answer = ")]}'\n\n" + "\n".join(json.dumps([chunk]) for chunk in chunks)

here = Path(__file__).parent
for name, rpcid, payload in [("notebooks", "wXbhsf", notebooks), ("notebook", "rLM1Ne", notebook),
                             ("source", "hizoJc", source_text), ("guide", "tr032e", guide), ("notes", "cFji9", notes),
                             ("artifacts", "gArtLc", artifacts), ("quiz", "v9rmvd", quiz),
                             ("mind_map", "v9rmvd", mind_map), ("created", "R7cb6c", created)]:
    (here / f"{name}.txt").write_text(frame(rpcid, payload))
(here / "answer.txt").write_text(answer)
