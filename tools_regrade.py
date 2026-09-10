"""저장된 모델 응답으로 등급만 다시 계산한다. LLM을 다시 부르지 않는다.

    python tools_regrade.py [세트이름]

등급 규칙(core/agent.py의 grade_of)을 고친 뒤, 30분짜리 재실행 없이
기존 판정에 새 규칙을 적용해 보는 용도다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import agent, db  # noqa: E402

name = sys.argv[1] if len(sys.argv) > 1 else "사회연대경제"
conn = db.connect()
db.init_db(conn)
s = db.get_set(conn, name)
if not s:
    sys.exit(f"세트 '{name}' 없음")

rows = [dict(r) for r in conn.execute(
    "SELECT id, title, importance, agent_code, agent_law FROM thread"
    " WHERE keyword_set_id=? AND agent_code IS NOT NULL", (s["id"],))]
if not rows:
    sys.exit("저장된 모델 응답이 없습니다. 새 판으로 한 번 실행한 뒤 쓰세요.")

a_on = bool(s["org_names"])
changed = []
for r in rows:
    src = r["title"] + " " + " ".join(
        (i["description"] or "") for i in db.thread_items(conn, r["id"]))
    grade, why = agent.grade_of(
        {"code": r["agent_code"], "law": r["agent_law"]}, src, a_enabled=a_on)
    if grade != r["importance"]:
        changed.append((r["title"], r["importance"], grade, why))
        conn.execute("UPDATE thread SET importance=?, agent_importance=? WHERE id=?",
                     (grade, grade, r["id"]))
conn.commit()

print(f"{len(rows)}건 재계산 · {len(changed)}건 변경\n")
for title, before, after, why in changed:
    print(f"  {before} → {after}  {title[:44]}")
    if why:
        print(f"      {why}")
dist = {r["i"]: r["n"] for r in conn.execute(
    "SELECT importance i, COUNT(*) n FROM thread WHERE keyword_set_id=? GROUP BY i",
    (s["id"],))}
print(f"\n등급 분포: {dist}")
