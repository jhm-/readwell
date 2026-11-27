-- Inventory listings: count consecutive runs an item has been missing from its page.
ALTER TABLE items ADD COLUMN missing_runs INTEGER NOT NULL DEFAULT 0;
