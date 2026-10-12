-- records.db v=1: documents index (life-memory slice 1, doc #59).
--
-- Own schema ladder, independent of conversations.db (Decision #3): this
-- file is a derived, rebuildable mirror of paperless-ngx today and the
-- seed of the life-memory data plane later (email, transactions land
-- here, not in conversations.db). `RecordsStore` refuses to open a
-- version newer than it knows (FC-35). Mirror of `_MIGRATIONS` in
-- store.py — fresh DBs run this file; live DBs run the ladder.

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS records_schema_version (
    version     INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS documents (
    id               INTEGER PRIMARY KEY,
    source           TEXT NOT NULL DEFAULT 'paperless',   -- bolt-on point for later slices
    source_id        TEXT NOT NULL,                       -- paperless document id (stringly, source-generic)
    title            TEXT NOT NULL,
    correspondent    TEXT NOT NULL DEFAULT '',            -- resolved NAME (FTS column)
    document_type    TEXT NOT NULL DEFAULT '',            -- resolved NAME (FTS column)
    tags             TEXT NOT NULL DEFAULT '[]',          -- JSON array of resolved names (FTS column: the
                                                         --   unicode61 tokenizer strips the JSON punctuation)
    content          TEXT NOT NULL,                       -- extracted text as delivered by the source
    checksum         TEXT,
    created          TEXT,                                -- document date, YYYY-MM-DD (paperless `created`)
    added            TEXT,
    modified         TEXT,                                -- ISO, from source
    original_filename TEXT,
    mime_type        TEXT,
    page_count       INTEGER,
    indexed_at       TEXT NOT NULL,
    UNIQUE (source, source_id)
);

-- Decision #10: porter stemming (receipt/receipts). Decision #11: metadata
-- columns indexed alongside body text, weighted by bm25() at query time.
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    title, correspondent, document_type, tags, content,
    content='documents',
    content_rowid='id',
    tokenize='porter unicode61'
);

-- ai/ad/au triggers, symmetric, ALL rows (exclusion happens before
-- insert — TLC-30). No repository method writes documents_fts directly.
CREATE TRIGGER IF NOT EXISTS documents_fts_ai AFTER INSERT ON documents
BEGIN
    INSERT INTO documents_fts (rowid, title, correspondent, document_type, tags, content)
        VALUES (new.id, new.title, new.correspondent, new.document_type, new.tags, new.content);
END;

CREATE TRIGGER IF NOT EXISTS documents_fts_ad AFTER DELETE ON documents
BEGIN
    INSERT INTO documents_fts (documents_fts, rowid, title, correspondent, document_type, tags, content)
        VALUES ('delete', old.id, old.title, old.correspondent, old.document_type, old.tags, old.content);
END;

CREATE TRIGGER IF NOT EXISTS documents_fts_au AFTER UPDATE ON documents
BEGIN
    INSERT INTO documents_fts (documents_fts, rowid, title, correspondent, document_type, tags, content)
        VALUES ('delete', old.id, old.title, old.correspondent, old.document_type, old.tags, old.content);
    INSERT INTO documents_fts (rowid, title, correspondent, document_type, tags, content)
        VALUES (new.id, new.title, new.correspondent, new.document_type, new.tags, new.content);
END;

CREATE TABLE IF NOT EXISTS sync_state (
    source     TEXT PRIMARY KEY,
    cursor     TEXT,                                      -- last `modified` ISO seen
    updated_at TEXT NOT NULL
);

INSERT OR IGNORE INTO records_schema_version (version) VALUES (1);
