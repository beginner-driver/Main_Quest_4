-- 이슈 레이더 DB 스키마 (PRD §9.3)
-- 원칙: item은 전역, thread는 세트 소속, item_thread로 연결.
--       전역 중복 제거를 쓰면 "세트 A가 먼저 수집한 기사가 세트 B에서 안 보이는" 누락이 생긴다.

PRAGMA foreign_keys = ON;

-- 키워드 세트: 키워드 + 그 세트 전용 중요도 기준 + 주제 맥락
CREATE TABLE IF NOT EXISTS keyword_set (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',   -- 주제 맥락. 프롬프트에 주입된다
    keywords    TEXT NOT NULL,              -- 쉼표 구분
    criteria    TEXT NOT NULL DEFAULT '',   -- 중요도 기준 A~F + 트리거
    org_names   TEXT NOT NULL DEFAULT '',   -- A등급 감지 대상 조직명 (비면 A 비활성)
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 수집 항목 원본. 전역이며 dedup_key UNIQUE로 중복 저장을 DB가 막는다.
CREATE TABLE IF NOT EXISTS item (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source           TEXT NOT NULL DEFAULT 'naver_news',  -- 게시판 확장 시 board_* 추가
    title            TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',            -- 네이버 스니펫. 재요약하지 않는다
    link             TEXT NOT NULL DEFAULT '',
    origin_link      TEXT NOT NULL DEFAULT '',
    press            TEXT NOT NULL DEFAULT '',
    published_at     TEXT NOT NULL,
    collected_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    dedup_key        TEXT NOT NULL UNIQUE,                -- origin_link or link
    matched_keywords TEXT NOT NULL DEFAULT ''             -- 쉼표 구분. 재수집 시 병합된다
);
CREATE INDEX IF NOT EXISTS idx_item_published ON item(published_at DESC);

-- 같은 사안 묶음. 세트에 소속되어 다른 주제 세트와 섞이지 않는다.
CREATE TABLE IF NOT EXISTS thread (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword_set_id   INTEGER NOT NULL REFERENCES keyword_set(id),
    run_id           INTEGER,
    title            TEXT NOT NULL,
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL,
    label            TEXT NOT NULL DEFAULT '신규',   -- 신규 / 후속 / 기존  (코드가 판정, §3.5)
    importance       TEXT NOT NULL DEFAULT '참고',   -- 최우선 / 필수 / 참고 (LLM이 판정)
    reason           TEXT NOT NULL DEFAULT '',
    agent_label      TEXT,   -- 에이전트 원안. 사람 확정값과 비교해 수정률을 계산한다
    agent_importance TEXT,
    agent_code       TEXT,   -- 모델이 고른 항목 (A~F/none). 등급은 코드가 매핑한다
    agent_law        TEXT,   -- code=B일 때 모델이 댄 법령명. 기사 원문과 대조해 검증한다
    status           TEXT NOT NULL DEFAULT 'pending', -- pending / judged / held / confirmed
    confirmed_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_thread_set ON thread(keyword_set_id, last_seen DESC);

-- N:M. 기사 하나가 여러 세트의 묶음에 속할 수 있다.
CREATE TABLE IF NOT EXISTS item_thread (
    item_id   INTEGER NOT NULL REFERENCES item(id),
    thread_id INTEGER NOT NULL REFERENCES thread(id),
    PRIMARY KEY (item_id, thread_id)
);

-- 실행 1건
CREATE TABLE IF NOT EXISTS run (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword_set_id INTEGER NOT NULL REFERENCES keyword_set(id),
    started_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    finished_at    TEXT,
    hours          INTEGER NOT NULL DEFAULT 24,
    item_count     INTEGER NOT NULL DEFAULT 0,
    thread_count   INTEGER NOT NULL DEFAULT 0,
    tokens_in      INTEGER NOT NULL DEFAULT 0,
    tokens_out     INTEGER NOT NULL DEFAULT 0,
    cost           REAL    NOT NULL DEFAULT 0,
    truncated      TEXT NOT NULL DEFAULT '',   -- 상한 도달 키워드. 비어 있지 않으면 화면 경고
    failed         TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'running'  -- running / done / interrupted / limit
);

-- 실행 trace. 어떤 도구를 왜 불렀고 뭘 받았나 (PRD §7.2②)
CREATE TABLE IF NOT EXISTS run_step (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         INTEGER NOT NULL REFERENCES run(id),
    seq            INTEGER NOT NULL,
    tool           TEXT NOT NULL,
    reason         TEXT NOT NULL DEFAULT '',   -- 왜 호출했나. 관찰 가능성의 핵심
    input_summary  TEXT NOT NULL DEFAULT '',
    output_summary TEXT NOT NULL DEFAULT '',
    duration_ms    INTEGER NOT NULL DEFAULT 0,
    success        INTEGER NOT NULL DEFAULT 1,
    error          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_step_run ON run_step(run_id, seq);
